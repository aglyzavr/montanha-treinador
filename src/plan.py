"""
Deterministic plan resolution.

The model must NEVER have to work out which phase or week it is. That is
arithmetic, and LLMs get it wrong (it previously reported "75 km target" for a
week whose real target was 65 km). This module answers, in Python:

    given a date -> which plan, which sub-phase, which week within that phase,
    what is the volume/vert target, what is the long-run target, what are the
    HR zones, what quality sessions are prescribed.

Tolerant by design: plan.json is hand-maintained and its shapes vary between
blocks (some sub_phases carry explicit dates, some only a week count; some
windows use ISO dates, some use "March 2027"). Anything unparseable is skipped
rather than raised -- a missing target is reported as None and the prompt says
"unknown", which is always better than a confident wrong number.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def load(path: str) -> dict:
    # Explicit utf-8: Windows defaults to cp1252 and plan.json carries "—", "ç", "Ã".
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_date(value, *, end=False):
    """Parse '2026-08-03', 'March 2027', or 'mid-January 2027'. None if hopeless.

    `end=True` snaps a month-only string to the last day of that month so a
    window like {"start": "March 2027", "end": "August 2027"} is inclusive.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    v = value.strip()
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        pass
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", v)
    if m and m.group(1).lower() in _MONTHS:
        mo, yr = _MONTHS[m.group(1).lower()], int(m.group(2))
        if not end:
            return date(yr, mo, 1)
        nxt = date(yr + (mo == 12), (mo % 12) + 1, 1)
        return nxt - timedelta(days=1)
    return None


def _window(obj):
    """(start, end) for a plan or sub-phase, from any of the shapes in use."""
    w = obj.get("window") or obj.get("dates") or obj
    start = _parse_date(w.get("start_date") or w.get("start"))
    end = _parse_date(w.get("end_date") or w.get("end"), end=True)
    return start, end


def _sub_phases(plan: dict) -> list:
    """Sub-phases live at plan level in some blocks and under `window` in others."""
    subs = plan.get("sub_phases")
    if not subs and isinstance(plan.get("window"), dict):
        subs = plan["window"].get("sub_phases")
    return subs or []


def _phase_windows(plan: dict) -> list:
    """Sub-phases with resolved (start, end). Phases lacking dates are laid end
    to end from the plan start using their `weeks` count."""
    subs = _sub_phases(plan)
    plan_start, _ = _window(plan)
    cursor = plan_start
    out = []
    for sp in subs:
        start, end = _window(sp)
        if start is None and cursor is not None:
            start = cursor
        if end is None and start is not None and sp.get("weeks"):
            end = start + timedelta(weeks=int(sp["weeks"])) - timedelta(days=1)
        if start and end:
            cursor = end + timedelta(days=1)
            out.append((start, end, sp))
    return out


def _nth(seq, idx1):
    """1-based lookup that returns None instead of raising."""
    if isinstance(seq, list) and 1 <= idx1 <= len(seq):
        return seq[idx1 - 1]
    return None


def _long_run_target(plan: dict, phase_id: str, week_index: int):
    """Find `long_run_progression_<PHASE>` and pull this week's row."""
    if not phase_id:
        return None
    row = _nth(plan.get(f"long_run_progression_{phase_id}"), week_index)
    if not isinstance(row, dict):
        return None
    minutes = row.get("duration_min")
    if minutes is None and row.get("duration_h") is not None:
        minutes = round(float(row["duration_h"]) * 60)
    return {
        "duration_min": minutes,
        "vert_m": row.get("vert_m"),
        "focus": row.get("focus") or row.get("effort"),
    }


# --------------------------------------------------------------------------
# Strength slots, resolved in Python.
#
# Strength used to exist only as a phrase inside a day's description, e.g. a
# Monday {"type": "rest_or_walk", "description": "Rest ... + strength B"}. The
# model read that as a rest day: no Monday recommendation from 2026-08-03 to
# 2026-09-14 mentioned strength at all, even on a readiness-83 morning. Like
# the phase/week arithmetic above, "is there a strength session today, and
# which one" is now answered here and handed to the model as a fact.
# --------------------------------------------------------------------------

_STRENGTH_SLOT = re.compile(r"(?i:strength)[\s_]*([AB])\b")
_NO_STRENGTH = re.compile(r"\bno strength\b", re.I)
# Not "bodyweight": the regular Monday slot is a "bodyweight/ME circuit".
_LIGHT_STRENGTH = re.compile(r"\b(light|maintenance|short)\b", re.I)


def strength_in(planned) -> dict:
    """{"slot", "wording", "light"} if this planned day includes strength, else None."""
    if not isinstance(planned, dict):
        return None
    blob = f"{planned.get('type') or ''} {planned.get('description') or ''}"
    if "strength" not in blob.lower() or _NO_STRENGTH.search(blob):
        return None
    m = _STRENGTH_SLOT.search(blob)
    return {
        "slot": m.group(1) if m else None,
        "wording": (planned.get("description") or planned.get("type") or "").strip(),
        "light": bool(_LIGHT_STRENGTH.search(blob)),
    }


def _strength_focus(plan_doc: dict, plan: dict, phase_id: str):
    """This phase's entry from `strength_program`, following a "See <plan_id>"
    reference (the Lizandro block points back at the GTSA block's program)."""
    prog = plan.get("strength_program")
    note = None
    if isinstance(prog, str):
        note = prog
        m = re.search(r"See\s+([A-Za-z0-9_\-]+)", prog)
        ref = next((p for p in plan_doc.get("plans") or []
                    if m and p.get("id") == m.group(1)), None)
        prog = ref.get("strength_program") if ref else None
    if not isinstance(prog, dict):
        return {"frequency_per_week": None, "focus": None, "note": note} if note else None

    focus = None
    for ph in prog.get("phases") or []:
        if phase_id and str(ph.get("phase", "")).split("_")[0] == phase_id:
            bits = [ph.get("focus")]
            bits += ph.get("examples") or []
            bits += ph.get("rules") or []
            bits.append(ph.get("pattern"))
            focus = "; ".join(str(b) for b in bits if b)
            break
    return {"frequency_per_week": prog.get("frequency_per_week"),
            "focus": focus, "note": note}


def _inherited_pattern(plans: list, match: dict):
    """(pattern, plan_id) of the latest earlier plan that has a day pattern.

    Safety net for a block written without `weekly_day_pattern_default`
    (Winter Base Phase B was): without it every day resolves to "nothing
    planned", which silently means no strength for the whole block.
    """
    start, _ = _window(match)
    best = None
    for p in plans:
        if p is match or not p.get("weekly_day_pattern_default"):
            continue
        _, end = _window(p)
        if start and end and end < start and (best is None or end > best[0]):
            best = (end, p)
    return (best[1]["weekly_day_pattern_default"], best[1].get("id")) if best else ({}, None)


def strength_slots(plan_doc: dict, week_start: date, upto: date) -> list:
    """[(date, strength_in(...)), ...] for planned strength days in
    week_start..upto inclusive."""
    out = []
    d = week_start
    while d <= upto:
        s = resolve(plan_doc, d).get("strength_today")
        if s:
            out.append((d, s))
        d += timedelta(days=1)
    return out


def resolve(plan_doc: dict, on: date) -> dict:
    """Everything the coach needs to know about where `on` sits in the plan."""
    ctx = {
        "date": on.isoformat(),
        "weekday": on.strftime("%a"),
        "plan_id": None, "plan_label": None, "goal": None, "race_date": None,
        "days_to_race": None, "phase_id": None, "phase_name": None,
        "phase_goal": None, "week_in_phase": None, "weeks_in_phase": None,
        "target_km": None, "target_vert_m": None, "long_run_target": None,
        "quality_sessions": [], "constraints": None, "heat_block": None,
        "hr_zones": None, "planned_today": None, "notes": None,
        "strength_today": None, "strength_focus": None,
        "day_pattern_inherited_from": None, "sub_phase_has_pattern": False,
    }

    plans = plan_doc.get("plans") or []
    match = None
    for p in plans:
        start, end = _window(p)
        if start and end and start <= on <= end:
            match = p
            break
    if match is None:
        return ctx

    ctx["plan_id"] = match.get("id")
    ctx["plan_label"] = match.get("label")
    ctx["hr_zones"] = match.get("hr_zones")

    goal = match.get("goal") or {}
    if goal:
        ctx["goal"] = goal.get("event")
        rd = _parse_date(goal.get("race_date"))
        if rd:
            ctx["race_date"] = rd.isoformat()
            ctx["days_to_race"] = (rd - on).days

    day_pattern = match.get("weekly_day_pattern_default") or {}
    if not day_pattern:
        day_pattern, ctx["day_pattern_inherited_from"] = _inherited_pattern(plans, match)
    ctx["planned_today"] = day_pattern.get(on.strftime("%a"))

    for start, end, sp in _phase_windows(match):
        if start <= on <= end:
            week_index = ((on - start).days // 7) + 1
            weeks = sp.get("weeks") or ((end - start).days // 7) + 1
            ctx.update({
                "phase_id": sp.get("id"),
                "phase_name": sp.get("name"),
                "phase_goal": sp.get("primary_goal") or sp.get("focus"),
                "week_in_phase": week_index,
                "weeks_in_phase": weeks,
                "target_km": _nth(sp.get("weekly_volume_pattern_km"), week_index)
                             or sp.get("volume_peak_km"),
                "target_vert_m": _nth(sp.get("weekly_vert_pattern_m"), week_index),
                "quality_sessions": sp.get("quality_sessions") or [],
                "constraints": sp.get("constraints"),
                "heat_block": sp.get("heat_block"),
                "notes": sp.get("notes") or sp.get("targets"),
                "long_run_target": _long_run_target(match, sp.get("id"), week_index),
            })
            # A sub-phase may carry its own weekly_day_pattern. Added for the
            # 2026 Lizandro 100K block: the taper week and race week need a
            # different day-by-day shape (race on Fri, no strength) than the
            # block default, and one block-level pattern can't express that.
            # Falls back to the block default when absent, so every existing
            # sub-phase resolves exactly as before.
            sp_pattern = sp.get("weekly_day_pattern")
            if isinstance(sp_pattern, dict) and on.strftime("%a") in sp_pattern:
                ctx["planned_today"] = sp_pattern[on.strftime("%a")]
                ctx["sub_phase_has_pattern"] = True
            break

    ctx["strength_today"] = strength_in(ctx["planned_today"])
    ctx["strength_focus"] = _strength_focus(plan_doc, match, ctx["phase_id"])
    return ctx


def apply_adjustment(plan_doc: dict, ctx: dict, adj: dict) -> tuple:
    """Apply a bounded, targeted edit to next week's targets. Returns (doc, log).

    The old design asked the model to regenerate the ENTIRE plan JSON and wrote
    it straight over plan.json. That is how you lose a three-year plan to one
    bad generation. Instead we accept only two numeric fields, only for the
    resolved phase and week, and only within +/-30% of the existing value.
    Anything else is rejected and reported rather than silently applied.
    """
    log = []
    if not isinstance(adj, dict) or not adj:
        return plan_doc, ["no adjustment proposed"]

    phase_id, week = ctx.get("phase_id"), ctx.get("week_in_phase")
    if not phase_id or not week:
        return plan_doc, ["rejected: no resolved phase/week to adjust"]

    target_plan = next((p for p in plan_doc.get("plans", [])
                        if p.get("id") == ctx.get("plan_id")), None)
    if target_plan is None:
        return plan_doc, ["rejected: plan id not found"]

    sp = next((s for s in _sub_phases(target_plan) if s.get("id") == phase_id), None)
    if sp is None:
        return plan_doc, [f"rejected: sub-phase {phase_id} not found"]

    limits = {
        "target_km": ("weekly_volume_pattern_km", 0, 150),
        "target_vert_m": ("weekly_vert_pattern_m", 0, 6000),
    }
    for key, (field, lo, hi) in limits.items():
        if key not in adj:
            continue
        try:
            new = float(adj[key])
        except (TypeError, ValueError):
            log.append(f"rejected {key}: not a number ({adj[key]!r})")
            continue
        if not (lo <= new <= hi):
            log.append(f"rejected {key}={new}: outside sane bounds {lo}-{hi}")
            continue
        pattern = sp.get(field)
        if not isinstance(pattern, list) or not (1 <= week <= len(pattern)):
            log.append(f"rejected {key}: no {field}[{week}] to adjust")
            continue
        old = pattern[week - 1]
        if isinstance(old, (int, float)) and old > 0 and not (0.7 * old <= new <= 1.3 * old):
            log.append(f"rejected {key}={new}: more than 30% from current {old}")
            continue
        pattern[week - 1] = round(new)
        log.append(f"{phase_id} week {week} {field}: {old} -> {round(new)}")

    if adj.get("notes"):
        sp["coach_notes"] = str(adj["notes"])[:500]
        log.append("recorded coach note")

    return plan_doc, log or ["no applicable changes"]


def describe(ctx: dict) -> str:
    """Render resolved context as prompt text. Unknowns are stated, not guessed."""
    def val(x, unit=""):
        return f"{x}{unit}" if x is not None else "unknown"

    lines = [
        f"  plan: {val(ctx['plan_label'])}",
        f"  goal: {val(ctx['goal'])}"
        + (f" on {ctx['race_date']} ({ctx['days_to_race']} days away)"
           if ctx.get("race_date") else ""),
        f"  phase: {val(ctx['phase_id'])} {ctx['phase_name'] or ''}".rstrip()
        + f" -- week {val(ctx['week_in_phase'])} of {val(ctx['weeks_in_phase'])}",
        f"  phase goal: {val(ctx['phase_goal'])}",
        f"  THIS WEEK'S TARGET: {val(ctx['target_km'], ' km')}, "
        f"{val(ctx['target_vert_m'], ' m vert')}",
    ]
    lr = ctx.get("long_run_target")
    if lr:
        lines.append(
            f"  long run target: {val(lr['duration_min'], ' min')}, "
            f"{val(lr['vert_m'], ' m vert')} -- {val(lr['focus'])}"
        )
    if ctx.get("quality_sessions"):
        lines.append("  prescribed quality sessions:")
        lines += [f"    - {q}" for q in ctx["quality_sessions"]]
    if ctx.get("constraints"):
        lines.append(f"  constraints: {ctx['constraints']}")
    # resolve() always copied sub-phase `notes` into ctx, but nothing ever
    # rendered it -- flagged 2026-08-21, fixed 2026-10-15 when the Lizandro
    # go/no-go check and race plan lived only in notes and the model never saw
    # them.
    if ctx.get("notes"):
        lines.append(f"  phase notes: {ctx['notes']}")
    sf = ctx.get("strength_focus") or {}
    if sf.get("focus") or sf.get("note"):
        lines.append("  strength program this phase: "
                     + "; ".join(x for x in (sf.get("focus"), sf.get("note")) if x))
    if ctx.get("heat_block"):
        hb = ctx["heat_block"]
        desc = hb.get("description") if isinstance(hb, dict) else hb
        lines.append(f"  heat block ACTIVE: {desc}")
    if ctx.get("hr_zones"):
        lines.append(f"  HR zones: {json.dumps(ctx['hr_zones'])}")
    return "\n".join(lines)
