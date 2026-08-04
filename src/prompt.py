"""Builds the system + user prompts for the daily and weekly jobs.

Principle: hand the model finished figures and a resolved plan context. It
should spend its reasoning on judgement, never on summing activity rows or
working out which training week it is.
"""
import json
from datetime import date

from . import plan as planmod
from . import metrics as metricsmod


def load_principles(path: str) -> str:
    with open(path, "r") as f:
        return f.read().strip()


def load_plan(path: str) -> dict:
    return planmod.load(path)


OUTPUT_CONTRACT = """\
Reply in EXACTLY this format:

VERDICT: <train | easy | rest>
SESSION: <one line: the concrete session, or "rest">
CONFIDENCE: <high | medium | low>
FLAGS: <comma-separated notable signals, or "none">
---
<2-5 sentence message to the athlete explaining the call, warm and direct>

Rules:
- Use ONLY the figures given. Never estimate a total that was not provided.
- If a value is marked unknown or missing, say so; do not fill the gap.
"""


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
            L.append(f"  sleep need: {m['sleep_need_min']} min")
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
    if m.get("training_readiness") is not None:
        L.append(f"  training readiness: {m['training_readiness']} "
                 f"({m.get('readiness_level')}) -- {m.get('readiness_feedback')}")
    if m.get("recovery_time_min"):
        hrs = round(m["recovery_time_min"] / 60, 1)
        L.append(f"  recovery time remaining: {hrs} h")
    if m.get("acute_load") is not None:
        L.append(f"  acute load: {m['acute_load']}, ACWR factor {m.get('acwr_pct')}%")
    if m.get("body_battery_wake") is not None:
        L.append(f"  body battery at wake: {m['body_battery_wake']} "
                 f"(low {m.get('body_battery_low')}, high {m.get('body_battery_high')})")
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


def _fmt_trend(recent: list) -> str:
    if not recent:
        return "  (no history yet)"
    lines = []
    for m in recent[-14:]:
        lines.append(
            f"  {m.get('date')}: sleep={m.get('sleep_score')} "
            f"HRV={m.get('hrv_overnight')} RHR={m.get('resting_hr')} "
            f"readiness={m.get('training_readiness')} "
            f"recovery_h={round((m.get('recovery_time_min') or 0)/60,1)}"
        )
    return "\n".join(lines)


def build_daily(principles, plan_doc, today_metrics, yesterday_activities,
                recent, today=None, notes=None):
    today = today or date.today()
    ctx = planmod.resolve(plan_doc, today)
    system = principles + "\n\n---\n" + OUTPUT_CONTRACT

    planned = ctx.get("planned_today")
    note_lines = "\n".join(f"  {n['date']} [{n['kind']}] {n['text']}"
                           for n in (notes or [])) or "  (none logged)"

    user = f"""Today is {today.isoformat()} ({today.strftime('%A')}).

PLAN CONTEXT (resolved from your plan -- these are facts, not estimates)
{planmod.describe(ctx)}
  today's prescribed session: {json.dumps(planned) if planned else '(none specified)'}

LAST NIGHT / TODAY'S RECOVERY
{_fmt_metrics(today_metrics)}

YESTERDAY'S TRAINING
{_fmt_activities(yesterday_activities)}

RECENT TREND
{_fmt_trend(recent)}

RECENT MANUAL LOG (strength / feet / soreness)
{note_lines}

Decide today's session. When signals conflict or data is missing, choose the
easier option. Reference the specific numbers above in your reasoning.
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
