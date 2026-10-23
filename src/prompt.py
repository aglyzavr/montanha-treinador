"""Builds the system + user prompts for the daily and weekly jobs.

Principle: hand the model finished figures and a resolved plan context. It
should spend its reasoning on judgement, never on summing activity rows or
working out which training week it is.
"""
import json
import re
from datetime import date, timedelta
from statistics import median

from . import llm
from . import plan as planmod
from . import metrics as metricsmod
from . import readiness as readinessmod


def load_principles(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_strength_reference(path: str) -> str:
    """The ch.6-8 strength/ME content (data/strength_reference_uphill_athlete.md).

    Only injected into the prompt on days the plan actually calls for strength
    (see `_plans_strength`). Without this the model was given a bare label
    like "easy_run_plus_strength_A" with no exercises behind it -- it had
    nothing concrete to prescribe.
    """
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _plans_strength(planned: dict) -> bool:
    return planmod.strength_in(planned) is not None


# --------------------------------------------------------------------------
# Strength: what is due today, resolved in Python.
#
# Two ways strength went missing: (1) the Monday slot sits inside a
# "rest_or_walk" day and the model treated the whole day as rest; (2) a slot on
# a day the app wasn't opened, or that was downgraded, was never offered again.
# strength_status() counts the week so far; strength_due() decides whether
# today carries the plan's own slot or a make-up for a missed one.
# --------------------------------------------------------------------------

MAKEUP_BLACKOUT_DAYS = 14    # principles: no new strength load 10-14 days pre-race


def strength_status(plan_doc, today, week_notes, week_activities) -> dict:
    """Planned vs done for this Mon-Sun week, strictly before today."""
    week_start = today - timedelta(days=today.weekday())
    slots = (planmod.strength_slots(plan_doc, week_start, today - timedelta(days=1))
             if today > week_start else [])
    done = {d for d in metricsmod.strength_done_dates(week_notes, week_activities)
            if week_start.isoformat() <= d < today.isoformat()}
    missed = [(d.isoformat(), s) for d, s in slots if d.isoformat() not in done]
    owed = max(0, min(len(slots) - len(done), len(missed)))
    freq = (planmod.resolve(plan_doc, today).get("strength_focus") or {}).get("frequency_per_week")
    if isinstance(freq, int):
        owed = min(owed, max(0, freq - len(done)))
    return {"planned_so_far": len(slots), "done_this_week": len(done),
            "missed": missed, "owed": owed, "frequency": freq}


def _is_hard_day(ctx: dict) -> bool:
    t = str((ctx.get("planned_today") or {}).get("type") or "").lower()
    if any(h in t for h in ("long", "race", "quality")):
        return True
    day = (ctx.get("weekday") or "").lower()
    return bool(day) and any(q.lower().startswith(day) for q in ctx.get("quality_sessions") or [])


def strength_due(plan_doc, today, status: dict = None):
    """None, or {"kind": "today"|"makeup", "slot", "wording", "light", ...}.

    A make-up is offered only on an easy/rest day that is not the day before a
    long run, race or quality session (ME soreness lasts 2-3 days), and never
    inside the pre-race blackout.
    """
    ctx = planmod.resolve(plan_doc, today)
    if ctx.get("strength_today"):
        return {"kind": "today", **ctx["strength_today"]}
    if not status or status.get("owed", 0) <= 0 or not ctx.get("planned_today"):
        return None
    dtr = ctx.get("days_to_race")
    if dtr is not None and 0 <= dtr <= MAKEUP_BLACKOUT_DAYS:
        return None
    tomorrow = planmod.resolve(plan_doc, today + timedelta(days=1))
    if _is_hard_day(ctx) or _is_hard_day(tomorrow):
        return None
    missed_on, slot = status["missed"][-1]
    return {"kind": "makeup", **slot, "missed_on": missed_on,
            "tomorrow_is_strength": bool(tomorrow.get("strength_today"))}


def _stage_hint(last_strength_note) -> str:
    if last_strength_note:
        return (f"last logged strength session: {last_strength_note['date']} "
                f"\"{last_strength_note['text']}\" -- continue from that stage/exercises.")
    return ("no strength session with details logged in the last 60 days -- default to "
            "the Stage 1 circuits and tell the athlete to self-test with the assessment "
            "tests in the reference, then note the stage in the check-in strength detail.")


def build_strength_block(due, ctx, status, last_strength_note=None) -> str:
    """The short, required strength section placed right after the running
    prescription. The (long) reference content goes at the end of the prompt."""
    if not due:
        return ""
    slot = f"strength {due['slot']}" if due.get("slot") else "strength"
    if due["kind"] == "today":
        L = [f"TODAY'S STRENGTH SESSION -- REQUIRED ({slot})",
             f"  plan's own wording: \"{due['wording']}\"",
             "  This is separate from the running verdict. A rest, walk or easy day",
             "  still includes this session. Only the strength downgrade ladder in",
             "  your principles can reduce or skip it, and a skip must name the reason."]
    else:
        L = [f"MAKE-UP STRENGTH SESSION OWED ({slot} planned {due['missed_on']}, "
             f"not logged as done)",
             "  Today has no strength slot of its own, but this week's planned session",
             "  has no evidence of being done. Propose it today alongside today's",
             "  session, unless the strength downgrade ladder says skip. If it was",
             "  done and just not logged, tell the athlete to tick strength in the",
             "  check-in instead."]
        if due.get("tomorrow_is_strength"):
            L.append("  Tomorrow is also a strength day: keep today's make-up to the reduced")
            L.append("  circuit so the two do not stack.")
    if due.get("light"):
        L.append("  LIGHT / MAINTENANCE ONLY: reduced sets, bodyweight, nothing new or heavy.")
    sf = ctx.get("strength_focus") or {}
    if sf.get("focus") or sf.get("note"):
        L.append("  phase strength program: "
                 + "; ".join(x for x in (sf.get("focus"), sf.get("note")) if x))
    if status:
        planned = status["planned_so_far"] + (1 if due["kind"] == "today" else 0)
        L.append(f"  strength this week: {status['done_this_week']} done before today, "
                 f"{planned} planned up to and including today")
    L.append(f"  stage: {_stage_hint(last_strength_note)}")
    return "\n\n" + "\n".join(L) + "\n" + STRENGTH_FORMATTING


STRENGTH_FORMATTING = """
STRENGTH FORMATTING (required whenever a strength block is present above):
- Header line "STRENGTH:" names the circuit and stage you prescribe, or
  "skipped: <reason from the strength downgrade ladder>".
- After your 2-5 sentence message, add a line "Today's strength session:"
  followed by a numbered list of every exercise in the circuit (full stage
  circuit, or the reduced/core-only version if the ladder applies today). Each
  line gives exercise name, sets x reps (or hold time), and rest, e.g.:
    1. Plank from knees -- 6 x 15s hold, rest 1 min between sets.
    2. Windshield wiper (bent knees) -- 6 sets max reps, rest 1 min.
- Copy exercise names, sets/reps and rest exactly from the strength reference
  content at the end of this prompt. Do not invent exercises or numbers; if a
  figure is not specified for the stage, say so.
- Never write only a stage name without the list underneath it.
"""


_STRENGTH_LIST = re.compile(
    r"today['’]?s strength session\W*(.*)", re.I | re.S)
_NUMBERED = re.compile(r"^\s*(?:[-*]\s*)?\**\s*\d+[.)]\s*\S", re.M)
# "skipped: sleep 4h" -- a bare "skipped" has no ladder reason and is rejected.
_REASONED_SKIP = re.compile(r"skip\w*\s*[:\-–—(]\s*\S.{2,}", re.I)


def strength_answered(text: str) -> bool:
    """Did the answer deliver the strength section (or an explicit, reasoned skip)?"""
    head = llm.parse_header(text or "").get("STRENGTH", "").strip()
    if _REASONED_SKIP.match(head):
        return True
    m = _STRENGTH_LIST.search(text or "")
    return bool(m and _NUMBERED.search(m.group(1)))


STRENGTH_RETRY = (
    "Your answer left out the required strength section. Reply again in the same "
    "format, keeping your running call, and this time include the STRENGTH: header "
    "line and, after the message, \"Today's strength session:\" with the numbered "
    "exercise list copied from the strength reference -- or, if the strength "
    "downgrade ladder genuinely applies, \"STRENGTH: skipped: <reason>\".")


def reference_section(reference: str, heading_prefix: str) -> str:
    """One '## ' section of the strength reference, heading included."""
    if not reference:
        return ""
    for part in re.split(r"(?m)^(?=## )", reference):
        if part.startswith("## " + heading_prefix):
            return part.strip()
    return ""


def strength_fallback(due, reference, last_strength_note=None) -> str:
    """Appended by Python when the model omitted strength twice. Only the plan's
    wording and the reference's own text -- nothing invented."""
    slot = f"strength {due['slot']}" if due.get("slot") else "strength"
    if due["kind"] == "today":
        lead = f"The plan calls for {slot} today (\"{due['wording']}\")."
    else:
        lead = (f"{slot.capitalize()} planned {due['missed_on']} has not been logged "
                f"as done -- make it up today if you feel fine.")
    stage = (f"Use the stage you last logged ({last_strength_note['text']})."
             if last_strength_note else "No stage logged yet: use Stage 1.")
    light = " Keep it light: reduced sets, bodyweight only." if due.get("light") else ""
    parts = [f"Today's strength session (added by the app -- the coach's answer left it "
             f"out): {lead} {stage}{light} One pass of the circuit, ~25-35 min; drop to "
             f"core only if legs are sore or sleep was under 5 h."]
    for h in ("Core (ch. 7)", "Stage 1-3 general strength programs",
              "Full general-strength workout template"):
        sec = reference_section(reference, h)
        if sec:
            parts.append(sec)
    return "\n\n".join(parts)


def load_plan(path: str) -> dict:
    return planmod.load(path)


# --------------------------------------------------------------------------
# Today's running prescription, resolved in Python.
#
# The strength path already had this: on strength days the model is handed the
# actual circuit and told it may not answer with a bare stage name. The running
# path had no equivalent, so a day whose plan says {"type": "long_run",
# "description": "Highest volume day"} produced messages like "Execute the full
# long run as planned" -- technically obedient, useless to run by. The duration,
# vert and focus were sitting in `long_run_progression_A3` the whole time; the
# model was just never required to write them out.
#
# Same principle as plan.py: resolve the numbers here, hand the model finished
# figures, and let it spend its reasoning on whether to downgrade them.
# --------------------------------------------------------------------------

# "12-14km", "12–14 km", "≈45 min", "20–30 min" as written in
# weekly_day_pattern_default descriptions.
_KM_RANGE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)\s*km", re.I)
_KM_ONE = re.compile(r"[≈~]?\s*(\d+(?:[.,]\d+)?)\s*km", re.I)
_MIN_RANGE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)\s*min", re.I)
_MIN_ONE = re.compile(r"[≈~]?\s*(\d+)\s*min", re.I)


def _num(s):
    return float(str(s).replace(",", "."))


def _stated(description: str):
    """Distance/duration the plan states in prose, e.g. "12-14km, 300-400m vert".

    Only what is literally written. Nothing inferred.
    """
    out = {"km": None, "duration_min": None}
    if not description:
        return out
    m = _KM_RANGE.search(description)
    if m:
        out["km"] = (_num(m.group(1)), _num(m.group(2)))
    else:
        m = _KM_ONE.search(description)
        if m:
            out["km"] = (_num(m.group(1)), _num(m.group(1)))
    m = _MIN_RANGE.search(description)
    if m:
        out["duration_min"] = (int(m.group(1)), int(m.group(2)))
    else:
        m = _MIN_ONE.search(description)
        if m:
            out["duration_min"] = (int(m.group(1)), int(m.group(1)))
    return out


def _run_history(activity_history, min_duration_min=50):
    """Past runs reduced to (speed_kmh, vert_per_km, hours). Walks excluded."""
    out = []
    for a in activity_history or []:
        if "running" not in str(a.get("type") or "").lower():
            continue
        dur = a.get("duration_min") or 0
        km = a.get("distance_km") or 0
        if dur < min_duration_min or km <= 0:
            continue
        hours = dur / 60.0
        out.append((km / hours, (a.get("elevation_gain_m") or 0) / km, hours))
    return out


DENSITY_BANDS = [(0.75, 1.35), (0.6, 1.6), (0.5, 2.0)]  # widened until 4 peers
MIN_PEERS = 4
FADE_PER_HOUR = 0.09          # speed lost per hour beyond the peer set's median
FADE_FLOOR = 0.75
SPREAD = 0.10                 # +/- band around the central figure


def estimate_distance_km(duration_min, vert_m, activity_history):
    """(low, high) km this athlete would cover in `duration_min` at `vert_m`.

    An ESTIMATE from their own history, never a plan target -- the plan
    prescribes long runs in time and vert, not distance, and inventing a km
    target would be exactly the "confident wrong number" plan.py exists to
    prevent. Returned separately from the prescribed figures and labelled as
    such in the prompt so the model cannot present it as prescribed.

    Method:
      1. Match on vertical density, because 1100 m over 3 h is a different run
         from 400 m over 3 h. Density depends on distance and distance is what
         we are solving for, so iterate to a fixed point.
      2. Fade for duration. Most logged runs are 1-2 h; a 3 h run is slower per
         hour than any of them, and an estimate that ignores this reads high
         every time. FADE_PER_HOUR was calibrated by back-testing every run
         over 1h48 against only the data that preceded it -- at 0.09/h the
         +/-10% band contained the real distance in all six cases, including
         the 3h36 / 803 m run (predicted 21.9-25.7, actual 23.5).

    Returns None when history is too thin to say anything honest, which is the
    correct answer more often than a number is.
    """
    runs = _run_history(activity_history)
    if len(runs) < 5 or not duration_min:
        return None

    hours = duration_min / 60.0
    speed = median(r[0] for r in runs)
    peers = runs
    for _ in range(8):
        km = speed * hours
        if km <= 0:
            return None
        density = (vert_m or 0) / km
        peers = None
        if density > 0:
            for lo, hi in DENSITY_BANDS:
                match = [r for r in runs if lo * density <= r[1] <= hi * density]
                if len(match) >= MIN_PEERS:
                    peers = match
                    break
            if peers is None:
                # Nothing in their history resembles today's steepness. Falling
                # back to flat runs would quietly predict flat-run pace for a
                # climb they have never done. Say nothing instead.
                return None
        else:
            peers = runs
        new = median(r[0] for r in peers)
        if abs(new - speed) < 0.02:
            speed = new
            break
        speed = new

    peer_hours = median(r[2] for r in peers)
    if hours > peer_hours:
        speed *= max(FADE_FLOOR, 1 - FADE_PER_HOUR * (hours - peer_hours))

    km = speed * hours
    return (round(km * (1 - SPREAD), 1), round(km * (1 + SPREAD), 1))


def _hr_guidance(hr_zones, effort_hint: str):
    """Actual bpm numbers for today's effort, not zone labels.

    "Keep it in Z1-Z2" is only actionable if you remember what Z2 is in bpm.
    The zone table is already in the prompt; this puts the two numbers that
    matter for today's session on the session line itself.
    """
    if not isinstance(hr_zones, dict):
        return None
    z1 = hr_zones.get("z1_easy_aerobic_bpm")
    z2 = hr_zones.get("z2_upper_aerobic_bpm")
    z3 = hr_zones.get("z3_sub_threshold_bpm")
    hint = (effort_hint or "").lower()

    if "z3" in hint or "z4" in hint or "threshold" in hint or "interval" in hint:
        parts = []
        if z3:
            parts.append(f"work reps {z3[0]}-{z3[1]} bpm")
        if hr_zones.get("z4_near_lt_bpm"):
            z4 = hr_zones["z4_near_lt_bpm"]
            parts.append(f"top end {z4[0]}-{z4[1]} bpm")
        if z1:
            parts.append(f"recoveries below {z1[1]} bpm")
        return "; ".join(parts) or None

    if not (z1 and z2):
        return None
    ceiling = z2[1]
    line = (f"average below {z2[0]} bpm, hard ceiling {ceiling} bpm "
            f"(Z1 {z1[0]}-{z1[1]}, upper Z2 {z2[0]}-{z2[1]})")
    if z3:
        line += f"; Z3 starts at {z3[0]} bpm -- do not cross it"
    return line


def _is_run_day(planned: dict) -> bool:
    t = str((planned or {}).get("type") or "").lower()
    return bool(t) and t not in ("rest", "rest_or_walk")


def build_prescription(ctx: dict, activity_history=None) -> str:
    """The concrete session the plan calls for today, or "" on non-run days.

    Every figure here is either read from plan.json or, for the distance
    estimate alone, computed from stored activity rows -- and the estimate is
    flagged as an estimate.
    """
    planned = ctx.get("planned_today")
    if not _is_run_day(planned):
        return ""

    desc = planned.get("description") or ""
    stated = _stated(desc)
    lr = ctx.get("long_run_target") or {}
    is_long = "long" in str(planned.get("type") or "").lower()

    duration_min = lr.get("duration_min") if is_long else None
    vert_m = lr.get("vert_m") if is_long else None
    focus = lr.get("focus") if is_long else None

    L = [f"  session type: {planned.get('type')}",
         f"  plan's own wording: {desc or '(none)'}"]

    if duration_min:
        L.append(f"  PRESCRIBED DURATION: {duration_min} min "
                 f"({round(duration_min / 60, 2)} h) -- run to time, not distance")
    elif stated["duration_min"]:
        a, b = stated["duration_min"]
        L.append(f"  PRESCRIBED DURATION: {a} min" if a == b
                 else f"  PRESCRIBED DURATION: {a}-{b} min")
    else:
        L.append("  PRESCRIBED DURATION: not specified by the plan for this day "
                 "-- say so rather than inventing one")

    if vert_m is not None:
        L.append(f"  PRESCRIBED VERT: {vert_m} m D+")
    elif "vert" in desc.lower():
        L.append(f"  PRESCRIBED VERT: as stated in the plan wording above")
    else:
        L.append("  PRESCRIBED VERT: not specified for this day")

    if stated["km"]:
        a, b = stated["km"]
        L.append(f"  PRESCRIBED DISTANCE: {a:g} km" if a == b
                 else f"  PRESCRIBED DISTANCE: {a:g}-{b:g} km")
    else:
        est = estimate_distance_km(duration_min, vert_m, activity_history)
        if est:
            L.append(f"  DISTANCE: not prescribed. ESTIMATE ONLY {est[0]:g}-{est[1]:g} km, "
                     f"from this athlete's own past runs at similar vert density. "
                     f"Present it as an estimate, never as a target.")
        else:
            L.append("  DISTANCE: not prescribed by the plan and not enough "
                     "comparable history to estimate -- say distance is not "
                     "prescribed for this session.")

    if focus:
        L.append(f"  THIS WEEK'S SPECIFIC FOCUS (must appear in your message): {focus}")

    hr = _hr_guidance(ctx.get("hr_zones"), f"{focus or ''} {desc}")
    if hr:
        L.append(f"  HR TARGET: {hr}")

    quality = [q for q in (ctx.get("quality_sessions") or [])
               if q.lower().startswith(ctx["weekday"].lower())]
    for q in quality:
        L.append(f"  PHASE PRESCRIPTION FOR THIS WEEKDAY: {q}")

    if (duration_min or 0) >= 120 or any("fuel" in q.lower() for q in quality):
        L.append("  FUELING: this is a fueling-practice session -- give a "
                 "concrete instruction (carbs per hour, when to start, fluid), "
                 "not the word 'fuel'.")

    hb = ctx.get("heat_block")
    if hb:
        L.append("  HEAT: heat block is active -- state explicitly whether this "
                 "session should go in the heat window or be moved to the cool "
                 "part of the day, and why.")

    return f"""

TODAY'S PRESCRIBED SESSION -- RESOLVED FIGURES (facts, not estimates unless labelled)
{chr(10).join(L)}
"""


PRESCRIPTION_FORMATTING = """\

SESSION FORMATTING (required whenever a "TODAY'S PRESCRIBED SESSION" block is
present above): your message must contain a line beginning "Today's session:"
that states, in numbers, all of: duration, vert, HR ceiling in bpm, and the
week's specific focus if one is given. Add the distance estimate only if one
was supplied, marked as an estimate.

- The phrases "as planned", "the full session", "as prescribed" and equivalents
  are FORBIDDEN as a substitute for the numbers. The athlete reads this message
  instead of opening the plan; a message that points back at the plan has
  failed.
- If you downgrade the session, give the NEW numbers and the original ones:
  "2h00 instead of the prescribed 3h00, 600 m instead of 1100 m".
- If a figure is marked "not specified" above, say that it is not prescribed.
  Do not fill the gap.
"""


OUTPUT_CONTRACT = """\
Reply in EXACTLY this format:

VERDICT: <train | easy | rest>
SESSION: <one line, and it must carry the numbers: duration, vert, and HR
ceiling in bpm -- e.g. "Long run 180 min / 1100 m D+, avg <140 bpm, cap 155".
Write "rest" only for a rest verdict. Never write "as planned".>
STRENGTH: <when the user prompt has a strength block: the circuit and stage you
prescribe, e.g. "Stage 1 circuit + core, ~30 min", or "skipped: <reason>".
Otherwise "none planned". VERDICT rest is about running only -- it does not
cancel a strength session.>
CONFIDENCE: <high | medium | low>
FLAGS: <comma-separated notable signals, or "none">
---
<2-5 sentence message to the athlete explaining the call, warm and direct;
then, when a strength block is present, the "Today's strength session:" list>

Rules:
- Use ONLY the figures given. Never estimate a total that was not provided.
- If a value is marked unknown or missing, say so; do not fill the gap.
- A figure explicitly labelled ESTIMATE in the user prompt may be repeated, but
  only with the same label. Never promote it to a prescribed target.
- If the user prompt contains a "TODAY'S STRENGTH SESSION" or "MAKE-UP
  STRENGTH SESSION" block, follow its STRENGTH FORMATTING instructions --
  never just name a stage without listing what it consists of.
""" + PRESCRIPTION_FORMATTING


def _fmt_metrics(m: dict) -> str:
    """Today's recovery, each value shown against its own baseline where we have one."""
    if not m:
        return "  (no data)"
    L = []
    if m.get("sleep_score") is not None or m.get("sleep_duration_min"):
        L.append(f"  sleep: score {m.get('sleep_score')}, "
                 f"{m.get('sleep_duration_min')} min "
                 f"(deep {m.get('deep_min')}, rem {m.get('rem_min')}, "
                 f"awake {m.get('awake_min')} over {m.get('awake_count')} wakes)")
        if m.get("sleep_need_min"):
            L.append(f"  sleep need: {m['sleep_need_min']} min"
                     + (f" (Garmin raised it for: {m['sleep_need_feedback']})"
                        if m.get("sleep_need_feedback") not in (None, "NO_CHANGE") else ""))
        if m.get("avg_sleep_stress") is not None:
            L.append(f"  sleep stress: {m['avg_sleep_stress']}, "
                     f"respiration {m.get('sleep_respiration')}")
    hrv = m.get("hrv_overnight")
    if hrv is not None:
        base = ""
        if m.get("hrv_baseline_low") and m.get("hrv_baseline_high"):
            base = (f" [your balanced range {m['hrv_baseline_low']}-"
                    f"{m['hrv_baseline_high']}, status {m.get('hrv_status')}]")
        L.append(f"  HRV overnight: {hrv}, 7-day avg {m.get('hrv_weekly_avg')}{base}")
    if m.get("resting_hr") is not None:
        L.append(f"  resting HR: {m['resting_hr']} "
                 f"[7-day avg {m.get('rhr_7d_avg')}]")
    # Garmin's own readiness/recovery/load only exist on rows fetched before
    # the switch to the Personal API; the local score replaces them.
    if m.get("training_readiness") is not None:
        L.append(f"  Garmin training readiness: {m['training_readiness']} "
                 f"({m.get('readiness_level')}) -- {m.get('readiness_feedback')}")
    if m.get("recovery_time_min"):
        hrs = round(m["recovery_time_min"] / 60, 1)
        L.append(f"  recovery time remaining: {hrs} h")
    if m.get("acute_load") is not None:
        L.append(f"  acute load: {m['acute_load']}, ACWR factor {m.get('acwr_pct')}%")
    if m.get("body_battery_wake") is not None:
        charge = (f"+{m['body_battery_charge']} overnight, "
                  if m.get("body_battery_charge") is not None else "")
        L.append(f"  body battery at wake: {m['body_battery_wake']} "
                 f"({charge}day low {m.get('body_battery_low')}, "
                 f"high {m.get('body_battery_high')})")
    if m.get("stress_avg") is not None:
        L.append(f"  avg stress: {m['stress_avg']}")
    return "\n".join(L) if L else "  (no data)"


def _fmt_activities(acts: list) -> str:
    if not acts:
        return "  (none)"
    lines = []
    for a in acts:
        bits = [f"  - {a.get('type')}"]
        if a.get("name"):
            bits.append(f"\"{a['name']}\"")
        bits.append(f"{a.get('duration_min')} min, {a.get('distance_km')} km")
        if a.get("elevation_gain_m") is not None:
            bits.append(f"+{round(a['elevation_gain_m'])}m/-{round(a.get('elevation_loss_m') or 0)}m")
        bits.append(f"avgHR {a.get('avg_hr')}")
        z = [a.get(f"z{i}_s") or 0 for i in range(1, 6)]
        if sum(z):
            bits.append("zones(min) " + "/".join(str(round(s / 60)) for s in z))
        if a.get("training_load") is not None:
            bits.append(f"load {round(a['training_load'])}")
        if a.get("aerobic_te") is not None:
            bits.append(f"aeTE {round(a['aerobic_te'], 1)}")
        if a.get("anaerobic_te"):
            bits.append(f"anTE {round(a['anaerobic_te'], 1)}")
        if a.get("max_temp_c") is not None:
            bits.append(f"{round(a['max_temp_c'])}C")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _fmt_acwr(acwr) -> str:
    if not acwr:
        return ""
    if acwr.get("unreliable"):
        return f"\n  ACWR (local): not yet meaningful -- {acwr['reason']}"
    return (f"\n  ACWR (local, from activity training load): {acwr['ratio']} "
            f"(acute 7d {acwr['acute_7d']} vs chronic weekly "
            f"{acwr['chronic_28d_weekly_avg']})")


def _fmt_trend(recent: list, readiness_trend: dict = None) -> str:
    """readiness = the local score; Garmin's only for days that have no local one."""
    if not recent:
        return "  (no history yet)"
    lines = []
    for m in recent[-14:]:
        local = (readiness_trend or {}).get(m.get("date"))
        lines.append(
            f"  {m.get('date')}: sleep={m.get('sleep_score')} "
            f"HRV={m.get('hrv_overnight')} RHR={m.get('resting_hr')} "
            f"readiness={local if local is not None else m.get('training_readiness')} "
            f"bb_wake={m.get('body_battery_wake')}"
        )
    return "\n".join(lines)


def build_daily(principles, plan_doc, today_metrics, yesterday_activities,
                recent, today=None, notes=None, strength_reference=None,
                activity_history=None, strength_status=None,
                last_strength_note=None, readiness=None, acwr=None,
                readiness_trend=None):
    today = today or date.today()
    ctx = planmod.resolve(plan_doc, today)
    system = principles + "\n\n---\n" + OUTPUT_CONTRACT

    planned = ctx.get("planned_today")
    prescription_block = build_prescription(ctx, activity_history)
    note_lines = "\n".join(f"  {n['date']} [{n['kind']}] {n['text']}"
                           for n in (notes or [])) or "  (none logged)"

    # The required strength section sits right under the running prescription,
    # where the model reads it as part of today's session; the long reference
    # text goes at the end so it doesn't bury the recovery data.
    due = strength_due(plan_doc, today, strength_status) if strength_reference else None
    strength_block = build_strength_block(due, ctx, strength_status, last_strength_note)
    reference_block = f"""

STRENGTH REFERENCE CONTENT (for the strength session above -- copy circuits
from here, do not invent)
{strength_reference}
""" if due else ""

    user = f"""Today is {today.isoformat()} ({today.strftime('%A')}).

PLAN CONTEXT (resolved from your plan -- these are facts, not estimates)
{planmod.describe(ctx)}
  today's prescribed session: {json.dumps(planned) if planned else '(none specified)'}
{prescription_block}{strength_block}
LAST NIGHT / TODAY'S RECOVERY
{_fmt_metrics(today_metrics)}
{readinessmod.describe(readiness)}{_fmt_acwr(acwr)}

YESTERDAY'S TRAINING
{_fmt_activities(yesterday_activities)}

RECENT TREND
{_fmt_trend(recent, readiness_trend)}

RECENT MANUAL LOG (strength / feet / soreness)
{note_lines}
{reference_block}
Decide today's session. When signals conflict or data is missing, choose the
easier option. Strength/ME work is a different stimulus from running load
(mostly neuromuscular/local, not cardiovascular) -- follow the strength
downgrade rules in your principles rather than dropping it whenever running
gets downgraded. Reference the specific numbers above in your reasoning.

Write the session out in full. The athlete acts on this message alone and does
not open the plan -- if your message does not contain the duration, the vert
and a bpm ceiling, it has told them nothing.
"""
    return system, user


WEEKLY_CONTRACT = """\
Reply in EXACTLY this structure:

ADHERENCE: <one line: actual vs target, using the computed figures given>
FATIGUE_TREND: <one line>
CHANGES: <one line summary of what you are changing, or "none">
---
<the weekly review message to the athlete, 6-10 sentences. Cover: volume and
vert vs target, intensity distribution, the long run, uphill-specific progress
(vert density, VAM, hill score), and what next week must do differently.>
---UPDATED_PLAN---
```json
<a JSON object with ONLY the fields you are changing, in this shape:
{"plan_id": "<id>", "phase_id": "<id>", "adjustments": {"target_km": <n>,
 "target_vert_m": <n>, "notes": "<why>"}}
If nothing should change, return {"adjustments": {}}.>
```

Rules:
- Every number you state must come from the figures provided. Do not compute
  your own totals and do not estimate.
- If the manual log is empty, say explicitly that strength and foot data are
  unavailable. Never assume prescribed work was completed.
- If data gaps are flagged, treat all totals as lower bounds and say so.
"""


def build_weekly(principles, plan_doc, summary, week_metrics, week_recs,
                 week_start, week_end, gaps=None, notes=None, scores=None):
    """Weekly prompt built on precomputed figures rather than raw activity rows."""
    ctx = planmod.resolve(plan_doc, week_start)
    system = principles + "\n\n---\n" + WEEKLY_CONTRACT

    verdicts = "\n".join(
        f"  {r.get('date')}: {r.get('session_type')} -- {(r.get('session_detail') or '')}"
        for r in week_recs
    ) or "  (none)"

    user = f"""Weekly review for the COMPLETED calendar week
{week_start.isoformat()} (Mon) to {week_end.isoformat()} (Sun).

WHERE THIS WEEK SITS IN THE PLAN (resolved, authoritative)
{planmod.describe(ctx)}

WHAT ACTUALLY HAPPENED (all figures computed from stored activity data)
{metricsmod.describe(summary, gaps=gaps, notes=notes, scores=scores)}

RECOVERY THROUGH THE WEEK
{_fmt_trend(week_metrics)}

DAILY VERDICTS THE COACH GAVE
{verdicts}

Assess adherence and fatigue against the target above, then propose concrete
adjustments for next week. Be conservative. Prioritise the phase goal:
{ctx.get('phase_goal') or 'unknown'}.
"""
    return system, user
