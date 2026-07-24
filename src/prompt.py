"""Builds the system + user prompts for the daily and weekly jobs."""
import json
from datetime import date


def load_principles(path: str) -> str:
    with open(path, "r") as f:
        return f.read().strip()


def load_plan(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


OUTPUT_CONTRACT = """\
Reply in EXACTLY this format:

VERDICT: <train | easy | rest>
SESSION: <one line: the concrete session, or "rest">
CONFIDENCE: <high | medium | low>
FLAGS: <comma-separated notable signals, or "none">
---
<2-5 sentence message to the athlete explaining the call, warm and direct>
"""


def _fmt_metrics(m: dict) -> str:
    if not m:
        return "  (no data)"
    keys = [
        ("sleep_score", "sleep score"), ("sleep_duration_min", "sleep min"),
        ("deep_min", "deep"), ("rem_min", "rem"), ("hrv_overnight", "HRV"),
        ("resting_hr", "resting HR"), ("training_readiness", "readiness"),
        ("stress_avg", "avg stress"),
    ]
    parts = [f"{label}={m.get(k)}" for k, label in keys if m.get(k) is not None]
    return "  " + ", ".join(parts) if parts else "  (no data)"


def _fmt_activities(acts: list) -> str:
    if not acts:
        return "  (none)"
    lines = []
    for a in acts:
        lines.append(
            f"  - {a.get('type')}: {a.get('duration_min')} min, "
            f"{a.get('distance_km')} km, avgHR {a.get('avg_hr')}, "
            f"load {a.get('training_load')}, aerobicTE {a.get('aerobic_te')}"
        )
    return "\n".join(lines)


def _fmt_trend(recent: list) -> str:
    if not recent:
        return "  (no history yet)"
    lines = []
    for m in recent[-10:]:
        lines.append(
            f"  {m.get('date')}: sleep={m.get('sleep_score')} "
            f"HRV={m.get('hrv_overnight')} RHR={m.get('resting_hr')} "
            f"readiness={m.get('training_readiness')}"
        )
    return "\n".join(lines)


def build_daily(principles, plan, today_metrics, yesterday_activities, recent):
    system = (
        principles
        + "\n\n---\n"
        + OUTPUT_CONTRACT
    )

    today = date.today()
    weekday = today.strftime("%a")
    planned = (plan.get("weekly_template", {}) or {}).get(weekday, {})

    user = f"""Today is {today.isoformat()} ({weekday}).

PLAN CONTEXT
  goal: {plan.get('goal')}
  phase: {plan.get('phase')}, week: {plan.get('week_index')}
  planned session today: {planned or '(none)'}
  zones: {plan.get('zones')}

LAST NIGHT / TODAY'S RECOVERY
{_fmt_metrics(today_metrics)}

YESTERDAY'S TRAINING
{_fmt_activities(yesterday_activities)}

RECENT TREND (recovery over last days)
{_fmt_trend(recent)}

Decide today's session. When signals conflict or data is missing, choose the easier option.
"""
    return system, user


WEEKLY_CONTRACT = """\
Reply in EXACTLY this structure:

ADHERENCE: <one line: planned vs actual>
FATIGUE_TREND: <one line>
CHANGES: <one line summary of what you changed, or "none">
---
<the weekly review message to the athlete, 4-8 sentences>
---UPDATED_PLAN---
```json
<the full updated plan JSON (same schema as the input plan); if no change, return it unchanged>
```
"""


def build_weekly(principles, plan, week_metrics, week_activities, week_recs, week_start):
    system = principles + "\n\n---\n" + WEEKLY_CONTRACT

    user = f"""Weekly review for the week starting {week_start}.

CURRENT PLAN
```json
{json.dumps(plan, indent=2)}
```

RECOVERY THIS WEEK
{_fmt_trend(week_metrics)}

ACTIVITIES THIS WEEK
{_fmt_activities(week_activities)}

DAILY VERDICTS GIVEN THIS WEEK
{chr(10).join('  ' + (r.get('date') or '') + ': ' + (r.get('session_type') or '') for r in week_recs) or '  (none)'}

Assess adherence and fatigue, then propose concrete adjustments to next week's plan.
Advance week_index by 1 and adjust sessions/targets as warranted. Be conservative.
"""
    return system, user
