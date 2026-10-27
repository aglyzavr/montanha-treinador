"""
Strength stages: set by the coach from Garmin results, never picked by the athlete.

The strength reference (Training for the Uphill Athlete, ch. 6-7) gives legs,
upper body and core each their own Stage 1-3 program, and says what each stage
has to look like before the next one. Garmin records every set (as corrected
in Connect), so the stage is decided here from those numbers:

- seeding: the first time a region is read, its stage is inferred from recent
  Garmin sessions (infer_stage) and stored with the evidence;
- moving up: the weekly review promotes a region once its target was met in
  2 of the last 3 sessions at that stage (review), and stores why;
- holding: 3+ weeks at a stage with no improvement is flagged, not promoted
  (reference: physio assessment rather than progressing).

The check-in used to ask "what did you do?" only to learn the stage; nothing
else the coach needed came from that answer.
"""
from datetime import date, timedelta

from . import metrics as M
from . import plan as planmod

REGIONS = ("legs", "upper", "core")
GROUP = {"legs": "lower", "upper": "upper", "core": "core"}
LOOKBACK_DAYS = 60          # sessions considered for seeding and progress
STALL_DAYS = 21             # reference: no improvement after 3 weeks -> physio
TREND_WEEKS = 6
RECENT_SESSIONS = 6


def _cat(e):
    return e.get("cat") or ""


def _ex(e):
    return e.get("ex") or ""


def _per_set(e):
    return (e.get("reps") or 0) / e["sets"] if e.get("sets") else 0


def _hold(e):
    """Average plank hold per set, seconds."""
    return (e.get("s") or 0) / e["sets"] if e.get("sets") else 0


def _is_plank(e):
    return _cat(e) == "PLANK"


def _is_pushup(e):
    return _cat(e) == "PUSH_UP"


def _pushup_kind(e):
    ex = _ex(e)
    return "incline" if "INCLINE" in ex else "knee" if "KNEE" in ex else "full"


def _is_squat(e):
    return "SQUAT" in (_cat(e), _ex(e)) or "LATERAL_WALK" in _ex(e)


def _is_step_or_lunge(e):
    text = f"{_cat(e)} {_ex(e)}"
    return any(k in text for k in ("LUNGE", "STEP_UP", "STEP_DOWN"))


# What each stage has to show in Garmin's sets before the next one: `sets` sets
# of at least `value` per set. metric(e) is the per-set figure, or None when the
# row is not this stage's exercise.
TARGETS = {
    ("legs", 1): {"what": "band/bodyweight squats", "sets": 4, "value": 20, "unit": " reps",
                  "metric": lambda e: _per_set(e) if _is_squat(e) and not e.get("kg") else None,
                  "source": "reference Stage 1: 4x20"},
    ("legs", 2): {"what": "bodyweight step-ups/lunges", "sets": 3, "value": 30, "unit": " reps",
                  "metric": lambda e: (_per_set(e) if _is_step_or_lunge(e) and not e.get("kg")
                                       else None),
                  "source": "reference Stage 2: progress 3x15 to 3x30 before advancing"},
    ("upper", 1): {"what": "incline push-ups", "sets": 3, "value": 10, "unit": " reps",
                   "metric": lambda e: (_per_set(e) if _is_pushup(e)
                                        and _pushup_kind(e) == "incline" else None),
                   "source": "the reference sets no number here; 3x10 is the app's bar"},
    ("upper", 2): {"what": "full push-ups", "sets": 3, "value": 5, "unit": " reps",
                   "metric": lambda e: (_per_set(e) if _is_pushup(e)
                                        and _pushup_kind(e) == "full" else None),
                   "source": "reference: 5+ clean push-ups = Stage 3"},
    ("core", 1): {"what": "plank holds", "sets": 3, "value": 30, "unit": " s",
                  "metric": lambda e: _hold(e) if _is_plank(e) else None,
                  "source": "reference: plank 30-60 s = Stage 2"},
    ("core", 2): {"what": "plank holds", "sets": 3, "value": 60, "unit": " s",
                  "metric": lambda e: _hold(e) if _is_plank(e) else None,
                  "source": "reference: plank over 60 s = Stage 3"},
}
TOP_STAGE = 3
TOP_NEXT = ("top stage: keep it, and build specific strength (hill sprints) and the "
            "muscular-endurance program from the reference")
# Garmin can't see knee tracking, which the reference's leg tests are about. On
# the way up the coach asks for the test inside the session instead of a form.
FORM_CHECK = {1: "box step-down from knee height -- the knee must not cave in",
              2: "single-leg sit-to-stand from a bench -- the knee must not cave in"}


def sessions(activities: list) -> list:
    """Garmin strength sessions with exercise sets, oldest first."""
    return sorted((a for a in activities or []
                   if M._is_strength(a) and M.strength_exercises(a)),
                  key=lambda a: (a.get("date") or "", a.get("start_local") or ""))


def _region_rows(a, region):
    return [e for e in M.strength_exercises(a) if M.exercise_group(e) == GROUP[region]]


# --------------------------------------------------------------------------
# Seeding: where the coach starts each region
# --------------------------------------------------------------------------

def infer_stage(region: str, sess: list) -> tuple:
    """(stage, reason, since) from recent Garmin sessions, oldest first.

    Core and upper go by the best recent figure, like the reference's
    assessment tests. Legs go by what the latest session trained, since the
    leg tests are about form the watch can't see.
    """
    with_region = [a for a in sess if _region_rows(a, region)]
    if not with_region:
        return 1, f"no {region} work recorded by Garmin yet -- starting at Stage 1", None
    recent = with_region[-3:]
    since = recent[0]["date"]
    if region == "core":
        holds = [(_hold(e), a["date"]) for a in recent for e in M.strength_exercises(a)
                 if _is_plank(e)]
        if not holds:
            return 1, "core work recorded but no plank to measure -- starting at Stage 1", since
        best, d = max(holds)
        stage = 1 if best < 30 else 2 if best < 60 else 3
        return stage, f"best plank hold {best:.0f} s per set ({d})", since
    if region == "upper":
        push = [(e, a["date"]) for a in recent for e in M.strength_exercises(a) if _is_pushup(e)]
        full = [(_per_set(e), d) for e, d in push if _pushup_kind(e) == "full"]
        if full:
            best, d = max(full)
            return (3 if best >= 5 else 2), f"best {best:.0f} full push-ups per set ({d})", since
        if any(_pushup_kind(e) == "knee" for e, _ in push):
            return 2, "knee push-ups recorded", since
        if push:
            return 1, "incline push-ups recorded", since
        loaded = [e for a in recent for e in _region_rows(a, region) if e.get("kg")]
        if loaded:
            return 3, f"loaded upper-body work ({M.exercise_name(loaded[-1])})", since
        return 1, "upper-body work without push-ups or load -- starting at Stage 1", since
    last = with_region[-1]
    rows = _region_rows(last, region)
    loaded = [e for e in rows if e.get("kg") and (_is_step_or_lunge(e) or _is_squat(e))]
    if loaded:
        return 3, f"latest leg session ({last['date']}) was loaded: {M.exercise_name(loaded[0])}", \
            last["date"]
    if any(_is_step_or_lunge(e) for e in rows):
        return 2, f"latest leg session ({last['date']}) was bodyweight step-ups/lunges", last["date"]
    return 1, (f"latest leg session ({last['date']}): "
               + ", ".join(M.exercise_name(e) for e in rows)), since


def current(db, today: date, sess: list = None) -> dict:
    """{region: {"stage", "since", "reason"}} -- seeding any region not yet set."""
    stored = db.strength_stages()
    if sess is None:
        sess = sessions(db.activities_between(
            (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat()))
    out = {}
    for r in REGIONS:
        if r not in stored:
            stage, reason, since = infer_stage(r, sess)
            since = since or today.isoformat()
            db.add_strength_stage(since, r, stage, "starting point: " + reason)
            stored[r] = {"stage": stage, "since": since, "reason": "starting point: " + reason}
        out[r] = stored[r]
    return out


# --------------------------------------------------------------------------
# Progress toward the next stage
# --------------------------------------------------------------------------

def progress(region: str, state: dict, sess: list, today: date) -> dict:
    stage = state["stage"]
    out = {"region": region, "stage": stage, "since": state["since"],
           "reason": state["reason"], "top": stage >= TOP_STAGE}
    if out["top"]:
        out["next"] = TOP_NEXT
        return out
    t = TARGETS[(region, stage)]
    out["target"] = f"{t['sets']} x {t['value']}{t['unit']} {t['what']}"
    out["source"] = t["source"]
    rows = []
    for a in sess:
        if a["date"] < state["since"]:
            continue
        hits = [(t["metric"](e), e.get("sets") or 0) for e in M.strength_exercises(a)]
        hits = [(v, s) for v, s in hits if v is not None]
        if not hits:
            continue
        value, sets = max(hits)
        met = any(v >= t["value"] and s >= t["sets"] for v, s in hits)
        # How close to the target, counting sets as well as reps/hold:
        # 3 x 20 against a 4 x 20 target is 75%, not 100%.
        share = max(min(v / t["value"], s / t["sets"], 1) for v, s in hits)
        rows.append({"date": a["date"], "value": round(value, 1), "sets": sets, "met": met,
                     "share": share})
    last3 = rows[-3:]
    out["sessions"] = last3
    out["ready"] = sum(r["met"] for r in last3) >= 2
    out["best"] = max((r["value"] for r in last3), default=None)
    out["pct"] = round(100 * max((r["share"] for r in last3), default=0))
    # Stalled = 3+ weeks at the stage, training it, without beating the first
    # of those sessions. Only the last 4 weeks count, so a break is not a stall.
    held = (today - date.fromisoformat(state["since"])).days
    window = [r for r in rows if r["date"] >= (today - timedelta(days=STALL_DAYS + 7)).isoformat()]
    out["stalled"] = (not out["ready"] and held >= STALL_DAYS and len(window) >= 3
                      and max(r["value"] for r in window[1:]) <= window[0]["value"])
    if out["ready"] and region == "legs":
        out["form_check"] = FORM_CHECK[stage]
    return out


def states(db, today: date) -> list:
    sess = sessions(db.activities_between(
        (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat()))
    cur = current(db, today, sess)
    return [progress(r, cur[r], sess, today) for r in REGIONS]


def review(db, week_end: date, apply: bool = True) -> list:
    """The weekly review's stage decisions: promote each region that is ready.
    The new stage starts the Monday after the reviewed week, so re-running the
    same review never promotes twice."""
    out = []
    for s in states(db, week_end):
        if s.get("ready") and not s["top"]:
            new_since = (week_end + timedelta(days=1)).isoformat()
            met = [r["date"] for r in s["sessions"] if r["met"]]
            reason = f"met {s['target']} on {', '.join(met)}"
            if apply:
                db.add_strength_stage(new_since, s["region"], s["stage"] + 1, reason)
            s = dict(s, promoted_to=s["stage"] + 1, promotion_reason=reason)
        out.append(s)
    return out


def _session_line(r, unit) -> str:
    return f"{r['date']} {r['sets']} sets, {r['value']:g}{unit}/set" + (" (met)" if r["met"] else "")


def describe(st: list) -> list:
    """Prompt lines, one per region."""
    L = []
    for s in st:
        line = f"{s['region']}: Stage {s['stage']} since {s['since']} ({s['reason']})"
        if s.get("promoted_to"):
            line += (f" -> MOVED UP to Stage {s['promoted_to']} from next Monday "
                     f"({s['promotion_reason']})")
        elif s["top"]:
            line += f"; {s['next']}"
        else:
            unit = TARGETS[(s["region"], s["stage"])]["unit"]
            done = (", ".join(_session_line(r, unit) for r in s["sessions"])
                    or "no matching sessions yet")
            line += f"; next stage needs {s['target']} in 2 of 3 sessions -- recent: {done}"
            if s.get("ready"):
                line += "; READY to move up"
            if s.get("stalled"):
                line += f"; NO IMPROVEMENT in {STALL_DAYS}+ days -- suggest a physio check, don't push"
        if s.get("form_check"):
            line += f"; before moving up, include this form check in the session: {s['form_check']}"
        L.append(line)
    return L


# --------------------------------------------------------------------------
# The app's Strength tab
# --------------------------------------------------------------------------

def _week_rows(plan_doc, week_start, today, done_dates):
    rows = []
    for d, slot in planmod.strength_slots(plan_doc, week_start, week_start + timedelta(days=6)):
        iso = d.isoformat()
        state = ("done" if iso in done_dates else "missed" if d < today
                 else "today" if d == today else "planned")
        rows.append({"date": iso, "day": d.strftime("%a"), "slot": slot.get("slot"),
                     "wording": slot.get("wording"), "light": slot.get("light"),
                     "state": state})
    return rows


def _streak(plan_doc, week_start, done_dates) -> int:
    """Complete weeks in a row, before this one, with every planned slot done.
    Weeks with no strength planned (recovery, race week) don't break it."""
    n = 0
    for k in range(1, 27):
        ws = week_start - timedelta(weeks=k)
        slots = planmod.strength_slots(plan_doc, ws, ws + timedelta(days=6))
        if not slots:
            continue
        done = sum(1 for i in range(7) if (ws + timedelta(days=i)).isoformat() in done_dates)
        if done < len(slots):
            break
        n += 1
    return n


def summary(db, today: date, plan_doc: dict) -> dict:
    from . import prompt   # prompt imports this module
    week_start = today - timedelta(days=today.weekday())
    hist_start = week_start - timedelta(weeks=26)
    notes = db.notes_between(hist_start.isoformat(), today.isoformat())
    acts = db.activities_between(hist_start.isoformat(), today.isoformat())
    done_dates = M.strength_done_dates(notes, acts)
    status = prompt.strength_status(
        plan_doc, today, [n for n in notes if n["date"] >= week_start.isoformat()],
        [a for a in acts if a["date"] >= week_start.isoformat()])

    sess = sessions(acts)
    runs = {a["date"] for a in acts if M._is_run(a)}
    recent = []
    for a in reversed(sess[-RECENT_SESSIONS:]):
        sp = M.strength_split(a)
        d = date.fromisoformat(a["date"])
        run_after = any((d + timedelta(days=i)).isoformat() in runs for i in (0, 1, 2))
        recent.append({"date": a["date"], "split": sp, "detail": M.describe_strength(a),
                       "rpe": a.get("rpe"),
                       "leg_heavy": sp["lower"] >= M.LEG_HEAVY_SETS and run_after})

    weeks = []
    for k in range(TREND_WEEKS - 1, -1, -1):
        ws = week_start - timedelta(weeks=k)
        in_week = [a for a in sess if ws.isoformat() <= a["date"] <= (ws + timedelta(days=6)).isoformat()]
        sets = {"lower": 0, "upper": 0, "core": 0}
        for a in in_week:
            for key, v in M.strength_split(a).items():
                if key in sets:
                    sets[key] += v
        weeks.append({"week": ws.isoformat(), "label": ws.strftime("%d %b"),
                      "sessions": len(in_week), **sets})
    trend_start = (week_start - timedelta(weeks=TREND_WEEKS - 1)).isoformat()
    in_trend = [a for a in sess if a["date"] >= trend_start]
    trail = sorted({M.exercise_name(e) for a in in_trend for e in M.strength_exercises(a)
                    if M.is_trail_specific(e)})

    return {
        "week": {"start": week_start.isoformat(),
                 "slots": _week_rows(plan_doc, week_start, today, done_dates),
                 "done": status["done_this_week"], "owed": status["owed"],
                 "frequency": status["frequency"],
                 "streak": _streak(plan_doc, week_start, done_dates)},
        "stages": states(db, today),
        "recent": recent,
        "trend": {"weeks": weeks, "trail_specific": trail,
                  "lifts": M.lift_progression(in_trend, "lower")},
    }
