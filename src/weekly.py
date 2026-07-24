"""Sunday job: review the week and propose plan corrections."""
import json
from datetime import date, timedelta

from .config import CONFIG, project_path
from . import store, prompt, llm, notify


def run():
    cfg = CONFIG
    db = store.Store(project_path(cfg["paths"]["db"]))
    db.init()

    today = date.today()
    week_start = (today - timedelta(days=6)).isoformat()
    week_end = today.isoformat()

    metrics = db.recent_metrics(7)
    activities = db.activities_between(week_start, week_end)
    recs = db.recommendations_between(week_start, week_end, kind="daily")

    plan_path = project_path(cfg["paths"]["plan"])
    plan = prompt.load_plan(plan_path)
    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))

    system, user = prompt.build_weekly(principles, plan, metrics, activities, recs, week_start)

    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        notify.send(cfg, f"⚠️ Montanha coach: weekly model call failed: {e}")
        raise

    header = llm.parse_header(text)
    message = llm.human_part(text) or text

    # Try to apply the model's updated plan. If parsing fails, keep the old plan
    # and just tell the user (never silently corrupt the plan).
    changes = header.get("CHANGES", "")
    updated = llm.extract_json_block(text)
    if updated and isinstance(updated, dict) and "weekly_template" in updated:
        with open(plan_path, "w") as f:
            json.dump(updated, f, indent=2)
        db.add_plan_version(week_end, json.dumps(updated), changes)
        message += "\n\n_Plan updated for next week._"
    else:
        message += "\n\n_(Could not auto-parse an updated plan — no changes applied. Review manually.)_"

    db.add_weekly_review(
        week_start,
        header.get("ADHERENCE", ""),
        header.get("FATIGUE_TREND", ""),
        changes,
        text,
    )

    notify.send(cfg, message, subject="Montanha coach — weekly review")
    print("Weekly review sent.")


if __name__ == "__main__":
    run()
