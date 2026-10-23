"""
Deterministic weekly aggregation.

Everything the coach compares against the plan is computed here, in Python,
and handed to the model as a finished figure. The model's job is judgement,
not arithmetic -- it previously had to mentally sum ~15 activity lines and
consistently under-reported volume.

Uphill-specific metrics, and why each one is here:

  vert_gain_m / vert_loss_m  Descent is tracked separately. Eccentric load is
                             what destroys quads on a course like DUT (2,300 m
                             of descending), and it is invisible in a km total.
  vert_density_m_per_km      Terrain specificity. DUT is ~43 m/km; a 70 km week
                             on the flat is not the same 70 km week.
  vam_m_per_h                Metres of ascent per hour -- the cleanest measure
                             of uphill fitness. Tracked against HR so a rising
                             VAM at equal HR means the climbing engine is
                             actually improving.
  easy_pct                   Share of HR-zone time in Z1-Z2. The principles
                             demand 80-90% below AeT and nothing checked it.
  heat_sessions              A3 mandates 2-3 sessions/wk in afternoon heat.
  b2b_long_days              Consecutive long days -- the year 2/3 focus.
  acwr                       7-day load vs 28-day average. Injury-risk canary.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# A run counts as "long" at 90+ minutes; b2b pairs are what build ultra
# durability, so the threshold is deliberately reachable rather than heroic.
LONG_RUN_MIN = 90
# Afternoon window that satisfies the plan's heat-block requirement.
HEAT_HOURS = (15, 19)
HEAT_TEMP_C = 24
RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
             "virtual_run", "ultra_run")
STRENGTH_TYPES = ("strength_training", "indoor_cardio", "fitness_equipment",
                  "gym", "bouldering", "hiit")


def last_complete_week(today: date = None) -> tuple:
    """(monday, sunday) of the last COMPLETE calendar week.

    Run it any day and you get the same answer for that week: on Mon 3 Aug it
    returns Mon 27 Jul - Sun 2 Aug. This is the fix for the rolling
    `today - 6 days` window, which produced a Tue-Mon slice that matched no
    week the plan is written in and always dropped Sunday.
    """
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    return last_monday, last_monday + timedelta(days=6)


def _f(x, default=0.0):
    return float(x) if isinstance(x, (int, float)) else default


def _is_run(a) -> bool:
    return (a.get("type") or "") in RUN_TYPES


def _is_strength(a) -> bool:
    return (a.get("type") or "") in STRENGTH_TYPES


def strength_done_dates(notes: list, activities: list) -> set:
    """ISO dates with evidence of a strength session: a check-in/Telegram note
    of kind "strength", or a Garmin strength-type activity. "no_strength"
    notes are deliberately not counted (see app.save_checkin)."""
    days = {n["date"] for n in notes or [] if n.get("kind") == "strength"}
    days |= {a["date"] for a in activities or [] if _is_strength(a) and a.get("date")}
    return days


# Garmin exercise categories by what they load. Lower-body is what matters for
# the running plan: a heavy leg session within ~48 h of a long run or a hard
# descent stacks on the same quads, a session of rows and curls does not.
LOWER_BODY = {"SQUAT", "DEADLIFT", "LUNGE", "HIP_RAISE", "CALF_RAISE", "STEP_UP",
              "HIP_STABILITY", "OLYMPIC_LIFT", "LEG_CURL", "LEG_PRESS", "HIP_SWING"}
CORE = {"PLANK", "CRUNCH", "SIT_UP", "CORE", "LEG_RAISE", "HIP_STABILITY_CORE"}
UPPER_BODY = {"BENCH_PRESS", "PUSH_UP", "PULL_UP", "ROW", "CURL", "TRICEPS_EXTENSION",
              "SHOULDER_PRESS", "LATERAL_RAISE", "SHRUG", "CHEST_FLY", "SHOULDER_STABILITY"}
# Banded and similar "equipment" categories carry the movement in subCategory.
_EQUIPMENT_CATEGORIES = {"BANDED_EXERCISES", "SUSPENSION", "TOTAL_BODY"}
# Single-leg and eccentric work: what protects quads and hips on long descents.
_TRAIL_SPECIFIC = ("SINGLE_LEG", "LUNGE", "STEP_UP", "STEP_DOWN", "CALF_RAISE",
                   "ROMANIAN", "SPLIT_SQUAT", "PISTOL", "LATERAL_WALK")
LEG_HEAVY_SETS = 6


def strength_exercises(a) -> list:
    """Per-exercise rows from Garmin (as corrected in Connect), or []."""
    import json
    try:
        return json.loads(a.get("strength_sets_json") or "[]")
    except (TypeError, ValueError):
        return []


def exercise_group(e: dict) -> str:
    cat = e.get("cat") or ""
    if cat in _EQUIPMENT_CATEGORIES:
        cat = e.get("ex") or cat
    if cat in LOWER_BODY:
        return "lower"
    if cat in CORE:
        return "core"
    if cat in UPPER_BODY:
        return "upper"
    return "other"   # warm-up, cardio, carries, UNKNOWN


def exercise_name(e: dict) -> str:
    return (e.get("ex") or e.get("cat") or "unknown").replace("_", " ").lower()


def is_trail_specific(e: dict) -> bool:
    text = f"{e.get('cat') or ''} {e.get('ex') or ''}"
    return any(k in text for k in _TRAIL_SPECIFIC)


def strength_split(a) -> dict:
    """{lower, upper, core, other} -> number of sets in one session."""
    out = {"lower": 0, "upper": 0, "core": 0, "other": 0}
    for e in strength_exercises(a):
        out[exercise_group(e)] += _f(e.get("sets"), 0)
    return {k: int(v) for k, v in out.items()}


def describe_strength(a, max_items: int = 8) -> str:
    """'legs 6 / upper 9 / core 3 sets: kettlebell deadlift 3x38 reps @20kg, ...'"""
    ex = strength_exercises(a)
    if not ex:
        return ""
    sp = strength_split(a)
    bits = []
    for e in [e for e in ex if exercise_group(e) != "other"][:max_items]:
        if e.get("reps"):
            work = f"{e['sets']}x, {e['reps']} reps"
        else:
            work = f"{e['sets']}x, {e.get('s')}s total"
        bits.append(f"{exercise_name(e)} {work}" + (f" @{e['kg']:g}kg" if e.get("kg") else ""))
    return (f"legs {sp['lower']} / upper {sp['upper']} / core {sp['core']} sets"
            + (f" ({sp['other']} unclassified)" if sp["other"] else "")
            + (": " + "; ".join(bits) if bits else ""))


def lift_progression(activities: list, group: str = "lower", sessions: int = 3) -> dict:
    """{exercise: ['2026-08-25 30 reps @20kg', ...]} -- the last few sessions of
    each lift in `group`, oldest first. Shows overload (or a stall) as measured
    numbers instead of the model's recollection."""
    out = {}
    for a in sorted((a for a in activities if _is_strength(a)), key=lambda a: a.get("date") or ""):
        for e in strength_exercises(a):
            if exercise_group(e) != group:
                continue
            work = (f"{e['reps']} reps" if e.get("reps") else f"{e.get('s')}s") \
                + (f" @{e['kg']:g}kg" if e.get("kg") else "")
            out.setdefault(exercise_name(e), []).append(f"{a['date']} {e['sets']}x {work}")
    return {k: v[-sessions:] for k, v in out.items()}


def strength_content(activities: list) -> dict | None:
    """The week's Garmin-recorded strength by body region."""
    sessions = [a for a in activities if _is_strength(a) and strength_exercises(a)]
    if not sessions:
        return None
    total = {"lower": 0, "upper": 0, "core": 0, "other": 0}
    trail = set()
    for a in sessions:
        for k, v in strength_split(a).items():
            total[k] += v
        trail |= {exercise_name(e) for e in strength_exercises(a) if is_trail_specific(e)}
    return {"sessions": len(sessions), "sets": total, "trail_specific": sorted(trail)}


def movement(week_metrics: list) -> dict | None:
    """All-day steps and floors -- includes activities, so it is total daily
    load, not 'extra' load. Floors are barometric metres climbed on foot."""
    rows = [m for m in week_metrics if m.get("steps") is not None]
    if not rows:
        return None
    return {
        "days": len(rows),
        "steps": sum(int(_f(m.get("steps"))) for m in rows),
        "floors_up_m": round(sum(_f(m.get("floors_up_m")) for m in rows)),
        "floors_down_m": round(sum(_f(m.get("floors_down_m")) for m in rows)),
        "vigorous_min": sum(int(_f(m.get("intensity_vigorous_min"))) for m in rows),
        "moderate_min": sum(int(_f(m.get("intensity_moderate_min"))) for m in rows),
    }


def durability(activities: list) -> dict | None:
    """The week's per-second analyses: time-weighted share of HR time at or
    below the plan's AeT, the long runs in detail, and any AeT-test segment."""
    from . import streams
    done = [a for a in activities if a.get("analysis")]
    if not done:
        return None
    timed = [(a["analysis"]["duration_min"], a["analysis"]["below_aet_pct"]) for a in done
             if a["analysis"].get("below_aet_pct") is not None]
    mins = sum(m for m, _ in timed)
    return {
        "analysed": len(done),
        "aet_bpm": next((a["analysis"].get("aet_bpm") for a in done
                         if a["analysis"].get("aet_bpm")), None),
        "below_aet_pct": round(sum(m * p for m, p in timed) / mins) if mins else None,
        "long_runs": [f"{a['date']} \"{a.get('name')}\" {round(_f(a.get('duration_min')))} min: "
                      + streams.describe(a) for a in done
                      if _f(a.get("duration_min")) >= LONG_RUN_MIN],
        "aet_tests": [f"{a['date']}: " + streams.describe({"analysis": {"aet_test": a["analysis"]["aet_test"]}})
                      for a in done if a["analysis"].get("aet_test")],
    }


def _start_hour(a):
    s = a.get("start_local")
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).hour
        except ValueError:
            continue
    return None


def _vam(a):
    """Ascent rate in m/h. Only meaningful on genuinely climbing activities."""
    gain = _f(a.get("elevation_gain_m"))
    mins = _f(a.get("moving_min")) or _f(a.get("duration_min"))
    km = _f(a.get("distance_km"))
    if gain < 150 or mins <= 0 or km <= 0 or gain / km < 20:
        return None
    return round(gain / (mins / 60))


def summarize(activities: list, plan_ctx: dict, load_history: dict = None,
              week_start: date = None, week_end: date = None,
              coverage_days: int = None) -> dict:
    """Aggregate one week of activities into plan-comparable figures."""
    runs = [a for a in activities if _is_run(a)]
    strength = [a for a in activities if _is_strength(a)]
    other = [a for a in activities
             if not _is_run(a) and not _is_strength(a)]

    run_km = sum(_f(a.get("distance_km")) for a in runs)
    run_min = sum(_f(a.get("moving_min")) or _f(a.get("duration_min")) for a in runs)
    gain = sum(_f(a.get("elevation_gain_m")) for a in runs)
    loss = sum(_f(a.get("elevation_loss_m")) for a in runs)
    all_km = sum(_f(a.get("distance_km")) for a in activities)
    all_min = sum(_f(a.get("moving_min")) or _f(a.get("duration_min")) for a in activities)

    # --- intensity distribution across every activity with zone data ---
    z = [sum(_f(a.get(f"z{i}_s")) for a in activities) for i in range(1, 6)]
    z_total = sum(z)
    easy_pct = round(100 * (z[0] + z[1]) / z_total) if z_total else None
    hard_pct = round(100 * (z[2] + z[3] + z[4]) / z_total) if z_total else None

    # --- longest run of the week, vs the plan's progression table ---
    longest = max(runs, key=lambda a: _f(a.get("moving_min")) or _f(a.get("duration_min")),
                  default=None)
    lr_target = plan_ctx.get("long_run_target") or {}
    longest_summary = None
    if longest:
        longest_summary = {
            "date": longest.get("date"),
            "name": longest.get("name"),
            "duration_min": round(_f(longest.get("moving_min"))
                                  or _f(longest.get("duration_min"))),
            "distance_km": round(_f(longest.get("distance_km")), 1),
            "vert_m": round(_f(longest.get("elevation_gain_m"))),
            "avg_hr": longest.get("avg_hr"),
            "vam": _vam(longest),
            "target_min": lr_target.get("duration_min"),
            "target_vert_m": lr_target.get("vert_m"),
        }

    # --- VAM across climbing runs ---
    vams = [(a.get("date"), _vam(a), a.get("avg_hr")) for a in runs]
    vams = [v for v in vams if v[1]]
    avg_vam = round(sum(v[1] for v in vams) / len(vams)) if vams else None

    # --- back-to-back long days ---
    long_days = sorted({a["date"] for a in runs
                        if (_f(a.get("moving_min")) or _f(a.get("duration_min"))) >= LONG_RUN_MIN})
    b2b = sum(1 for i in range(1, len(long_days))
              if date.fromisoformat(long_days[i]) - date.fromisoformat(long_days[i - 1])
              == timedelta(days=1))

    # --- heat-block compliance ---
    heat = [a for a in runs
            if (_start_hour(a) is not None and HEAT_HOURS[0] <= _start_hour(a) < HEAT_HOURS[1])
            or _f(a.get("max_temp_c")) >= HEAT_TEMP_C]

    # --- form decay: longest run vs the week's other runs ---
    form = None
    if longest and len(runs) > 1:
        others = [a for a in runs if a["id"] != longest["id"]]
        def mean(rows, key):
            vals = [_f(r.get(key)) for r in rows if _f(r.get(key))]
            return sum(vals) / len(vals) if vals else None
        base_cad, long_cad = mean(others, "cadence"), _f(longest.get("cadence")) or None
        base_gct, long_gct = mean(others, "ground_contact_ms"), _f(longest.get("ground_contact_ms")) or None
        if base_cad and long_cad:
            form = {
                "cadence_long": round(long_cad, 1),
                "cadence_other": round(base_cad, 1),
                "cadence_delta_pct": round(100 * (long_cad - base_cad) / base_cad, 1),
            }
            if base_gct and long_gct:
                form["gct_long"] = round(long_gct)
                form["gct_other"] = round(base_gct)
                form["gct_delta_pct"] = round(100 * (long_gct - base_gct) / base_gct, 1)

    target_km = plan_ctx.get("target_km")
    target_vert = plan_ctx.get("target_vert_m")

    out = {
        "week_start": week_start.isoformat() if week_start else None,
        "week_end": week_end.isoformat() if week_end else None,
        "sessions": len(activities),
        "runs": len(runs),
        "strength_sessions_garmin": len(strength),
        "other_sessions": len(other),
        "run_km": round(run_km, 1),
        "run_hours": round(run_min / 60, 1),
        "all_km": round(all_km, 1),
        "all_hours": round(all_min / 60, 1),
        "vert_gain_m": round(gain),
        "vert_loss_m": round(loss),
        "vert_density_m_per_km": round(gain / run_km, 1) if run_km else None,
        "target_km": target_km,
        "target_vert_m": target_vert,
        "km_vs_target_pct": round(100 * run_km / target_km) if target_km else None,
        "vert_vs_target_pct": round(100 * gain / target_vert) if target_vert else None,
        "easy_pct": easy_pct,
        "hard_pct": hard_pct,
        "zone_minutes": [round(s / 60) for s in z] if z_total else None,
        "longest_run": longest_summary,
        "avg_vam_m_per_h": avg_vam,
        "vam_sessions": vams,
        "b2b_long_days": b2b,
        "long_day_count": len(long_days),
        "heat_sessions": len(heat),
        "form_decay": form,
        "by_type": {},
        "daily_km": {},
    }

    for a in activities:
        t = a.get("type") or "unknown"
        e = out["by_type"].setdefault(t, {"n": 0, "km": 0.0, "min": 0.0, "vert": 0.0})
        e["n"] += 1
        e["km"] += _f(a.get("distance_km"))
        e["min"] += _f(a.get("moving_min")) or _f(a.get("duration_min"))
        e["vert"] += _f(a.get("elevation_gain_m"))
    for t, e in out["by_type"].items():
        e["km"], e["min"], e["vert"] = round(e["km"], 1), round(e["min"]), round(e["vert"])

    for a in runs:
        out["daily_km"][a["date"]] = round(out["daily_km"].get(a["date"], 0)
                                           + _f(a.get("distance_km")), 1)

    if load_history and week_end:
        out["acwr"] = _acwr(load_history, week_end, coverage_days)

    return out


# ACWR needs a real 28-day chronic base. With less history the denominator is
# an artefact -- 12 days of data divided by 4 weeks produced a "ratio" of 2.63
# that looked like a red-alert overtraining signal and meant nothing.
ACWR_MIN_COVERAGE = 21


def _acwr(load_history: dict, end: date, coverage_days: int = None):
    """Acute (7d) : chronic (28d mean of 7d blocks) workload ratio."""
    def total(days_back_start, days_back_end):
        s = end - timedelta(days=days_back_start)
        e = end - timedelta(days=days_back_end)
        return sum(v for k, v in load_history.items() if s.isoformat() <= k <= e.isoformat())

    acute = total(6, 0)
    chronic_total = total(27, 0)
    if chronic_total <= 0:
        return None

    if coverage_days is not None and coverage_days < ACWR_MIN_COVERAGE:
        return {
            "unreliable": True,
            "coverage_days": coverage_days,
            "reason": (f"only {coverage_days} of 28 days have data; "
                       "ACWR needs a full chronic base to mean anything"),
        }

    chronic_weekly = chronic_total / 4
    if chronic_weekly <= 0:
        return None
    return {
        "acute_7d": round(acute),
        "chronic_28d_weekly_avg": round(chronic_weekly),
        "ratio": round(acute / chronic_weekly, 2),
    }


def missing_days(activities: list, metrics: list, start: date, end: date) -> list:
    """Days in the window with no metrics row at all -- i.e. never fetched.

    Reported to the model explicitly so it says "2 days of data are missing"
    instead of silently treating them as rest and under-counting the week.
    """
    have = {m["date"] for m in metrics}
    out, cur = [], start
    while cur <= end:
        if cur.isoformat() not in have:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _pct(v, suffix="%"):
    return f"{v}{suffix}" if v is not None else "unknown"


def describe(s: dict, gaps: list = None, notes: list = None,
             scores: list = None) -> str:
    """Render the computed week as prompt text. All arithmetic already done."""
    L = []
    tgt_km = s.get("target_km")
    tgt_vert = s.get("target_vert_m")

    L.append("VOLUME (computed, not estimated)")
    L.append(f"  running: {s['run_km']} km / {s['run_hours']} h over {s['runs']} runs"
             + (f"  -- target {tgt_km} km ({_pct(s['km_vs_target_pct'])} of plan)"
                if tgt_km else "  -- no target in plan"))
    L.append(f"  all activity: {s['all_km']} km / {s['all_hours']} h over {s['sessions']} sessions")
    L.append("")
    L.append("VERTICAL")
    L.append(f"  ascent: {s['vert_gain_m']} m"
             + (f"  -- target {tgt_vert} m ({_pct(s['vert_vs_target_pct'])} of plan)"
                if tgt_vert else "  -- no target in plan"))
    L.append(f"  descent: {s['vert_loss_m']} m (eccentric load)")
    L.append(f"  vert density: {_pct(s['vert_density_m_per_km'], ' m/km')}"
             "   [DUT race demand is ~43 m/km]")
    if s.get("avg_vam_m_per_h"):
        L.append(f"  avg VAM on climbing runs: {s['avg_vam_m_per_h']} m ascent/h")
        for d, v, hr in s.get("vam_sessions", []):
            L.append(f"    {d}: {v} m/h at avg HR {hr}")
    L.append("")
    L.append("INTENSITY DISTRIBUTION (from HR zone time)")
    if s.get("easy_pct") is not None:
        L.append(f"  Z1-Z2 (easy/aerobic): {s['easy_pct']}%   Z3-Z5 (hard): {s['hard_pct']}%"
                 "   [principles require 80-90% easy]")
        L.append(f"  minutes per zone Z1..Z5: {s['zone_minutes']}")
    else:
        L.append("  no HR zone data available this week")
    L.append("")
    L.append("LONG RUN")
    lr = s.get("longest_run")
    if lr:
        L.append(f"  {lr['date']}: {lr['duration_min']} min, {lr['distance_km']} km, "
                 f"{lr['vert_m']} m vert, avg HR {lr['avg_hr']}"
                 + (f", VAM {lr['vam']} m/h" if lr.get("vam") else ""))
        if lr.get("target_min"):
            L.append(f"  plan target was {lr['target_min']} min / "
                     f"{lr.get('target_vert_m')} m vert")
    else:
        L.append("  no runs recorded")
    L.append("")
    L.append("DURABILITY / SPECIFICITY")
    L.append(f"  long days (>={LONG_RUN_MIN} min): {s['long_day_count']}, "
             f"back-to-back pairs: {s['b2b_long_days']}")
    L.append(f"  sessions in afternoon heat: {s['heat_sessions']}")
    if s.get("form_decay"):
        f = s["form_decay"]
        L.append(f"  form on long run vs other runs: cadence {f['cadence_long']} vs "
                 f"{f['cadence_other']} ({f['cadence_delta_pct']:+}%)"
                 + (f", ground contact {f['gct_long']} vs {f['gct_other']} ms "
                    f"({f['gct_delta_pct']:+}%)" if "gct_long" in f else ""))
    if s.get("acwr"):
        a = s["acwr"]
        if a.get("unreliable"):
            L.append(f"  ACWR: not yet meaningful -- {a['reason']}")
        else:
            L.append(f"  ACWR: {a['ratio']} (acute 7d {a['acute_7d']} vs chronic weekly "
                     f"{a['chronic_28d_weekly_avg']})")
    L.append("")
    L.append("SESSION BREAKDOWN BY TYPE")
    for t, e in sorted(s["by_type"].items(), key=lambda kv: -kv[1]["min"]):
        L.append(f"  {t}: {e['n']}x, {e['km']} km, {e['min']} min, {e['vert']} m vert")
    L.append(f"  daily running km: {s['daily_km'] or '(none)'}")

    if scores:
        L.append("")
        L.append("FITNESS SCORES (Garmin)")
        cur = scores[0]
        prev = scores[1] if len(scores) > 1 else {}
        for key, label in [("hill_score", "Hill Score (uphill ability)"),
                           ("endurance_score", "Endurance Score"),
                           ("vo2max", "VO2max"),
                           ("lactate_threshold_bpm", "Lactate threshold HR"),
                           ("running_tolerance", "Running tolerance"),
                           ("heat_acclimation", "Heat acclimation %")]:
            v = cur.get(key)
            if v is None:
                continue
            p = prev.get(key)
            delta = f" ({v - p:+.1f} vs previous)" if isinstance(p, (int, float)) else ""
            L.append(f"  {label}: {v}{delta}")
        if cur.get("training_status"):
            L.append(f"  Training status: {cur['training_status']}"
                     + (f" -- {cur['load_ratio_feedback']}" if cur.get("load_ratio_feedback") else ""))

    if s.get("strength_planned") is not None:
        L.append("")
        L.append("STRENGTH ADHERENCE (computed)")
        L.append(f"  {s['strength_done']} of {s['strength_planned']} planned strength sessions "
                 f"have evidence (check-in/Telegram or a Garmin strength activity)")
        if s.get("strength_missed"):
            L.append(f"  planned days with no strength logged that same day: "
                     f"{', '.join(s['strength_missed'])}")

    sc = s.get("strength_content")
    if sc:
        L.append("")
        L.append("STRENGTH CONTENT (Garmin exercise sets, as corrected in Connect)")
        st = sc["sets"]
        L.append(f"  {sc['sessions']} session(s): legs {st['lower']} / upper {st['upper']} / "
                 f"core {st['core']} sets" + (f", {st['other']} unclassified" if st["other"] else ""))
        L.append("  single-leg / eccentric (downhill protection): "
                 + (", ".join(sc["trail_specific"]) if sc["trail_specific"]
                    else "NONE this week"))
    if s.get("lift_progression"):
        L.append("  lower-body lifts, last sessions (oldest first):")
        for name, rows in sorted(s["lift_progression"].items()):
            L.append(f"    {name}: " + " -> ".join(rows))

    mv = s.get("movement")
    if mv:
        L.append("")
        L.append(f"DAILY MOVEMENT (all day incl. activities, {mv['days']} days with data)")
        L.append(f"  steps {mv['steps']:,} (avg {mv['steps'] // mv['days']:,}/day), "
                 f"climbed {mv['floors_up_m']} m / descended {mv['floors_down_m']} m on foot, "
                 f"intensity minutes {mv['moderate_min']} moderate / {mv['vigorous_min']} vigorous")

    du = s.get("durability")
    if du:
        L.append("")
        L.append(f"DURABILITY (per-second data, {du['analysed']} runs/hikes analysed)")
        if du.get("below_aet_pct") is not None:
            L.append(f"  time at or below the plan's AeT ({du['aet_bpm']} bpm): "
                     f"{du['below_aet_pct']}% of HR time -- the real 'easy' share")
        for line in du["long_runs"]:
            L.append(f"  {line}")
        if du["aet_tests"]:
            for line in du["aet_tests"]:
                L.append(f"  {line}")
        else:
            L.append("  no AeT-test segment this week (whole-run HR drift on self-paced "
                     "runs is not a valid AeT measure and is not computed)")

    z = s.get("hr_zones")
    if z:
        L.append("")
        L.append("WATCH HR ZONE SETTINGS (Garmin; zone minutes above are counted against these)")
        L.append(f"  Z1 {z['z1']}-{z['z2'] - 1}, Z2 {z['z2']}-{z['z3'] - 1}, Z3 {z['z3']}-{z['z4'] - 1}, "
                 f"Z4 {z['z4']}-{z['z5'] - 1}, Z5 {z['z5']}+ bpm; LTHR {z.get('lthr')}, "
                 f"max {z.get('max')}")
        L.append(f"  so 'easy' (Z1-Z2) above means below {z['z3']} bpm -- compare with the "
                 "plan's own AeT before calling the week easy enough")
        if z.get("rhr_stale"):
            L.append(f"  watch uses resting HR {z['rhr_used']} but measured 7-day is "
                     f"{z['rhr_measured']} (auto-update off) -- zone settings may be stale")

    L.append("")
    L.append("MANUAL LOG (strength, feet, soreness -- not available from Garmin)")
    if notes:
        for n in notes:
            L.append(f"  {n['date']} [{n['kind']}] {n['text']}")
    else:
        L.append("  NO ENTRIES. You have no evidence about strength sessions, foot")
        L.append("  condition, or subjective soreness this week. Say so explicitly.")
        L.append("  Do NOT assume prescribed strength work was completed.")

    if gaps:
        L.append("")
        L.append(f"!! DATA GAPS: no data at all for {', '.join(gaps)}.")
        L.append("   Volume and vert totals above are UNDERSTATED. Treat the")
        L.append("   comparison against plan target as a lower bound only.")

    return "\n".join(L)
