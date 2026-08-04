"""
Weekly job: review the LAST COMPLETE calendar week (Mon-Sun).

Runs Monday 07:00. The window is anchored to the calendar, not to the run
date, so a manual run on any day reviews the same completed week -- no more
rolling Tue-Mon slices that matched no week the plan is written in, and no
more silently dropping Sunday because its activities had not been fetched yet.

Order matters: backfill first, then aggregate. Any day inside the window that
was never fetched (a missed daily run, a laptop asleep) is pulled from Garmin
before the numbers are computed, and anything still missing is flagged to the
model so it reports a lower bound instead of a wrong total.
"""
import json
import shutil
from datetime import date, datetime, timedelta

from .config import CONFIG, project_path
from . import garmin, store, prompt, llm, notify, metrics, plan as planmod, inbox


def backfill(g, db, start: date, end: date, log=print) -> list:
    """Fetch every day in [start, end] we have no metrics row for."""
    missing = db.missing_activity_dates(start, end)
    filled = []
    for d in missing:
        if d >= date.today():
            continue  # today is not over; fetching it proves nothing
        try:
            raw = garmin.fetch_day(g, d)
            db.upsert_metrics(garmin.normalize(raw))
            for a in garmin.normalize_activities(raw):
                db.upsert_activity(a)
            db.mark_fetched(d.isoformat())
            filled.append(d.isoformat())
            log(f"  backfilled {d}")
        except Exception as e:
            log(f"  backfill failed for {d}: {e}")
    return filled


def run(today: date = None, dry_run: bool = False):
    cfg = CONFIG
    db = store.Store(project_path(cfg["paths"]["db"]))
    db.init()

    today = today or date.today()
    week_start, week_end = metrics.last_complete_week(today)
    print(f"Reviewing {week_start} .. {week_end} (last complete Mon-Sun)")

    # 1. Collect anything you sent by Telegram since the last run.
    try:
        n = inbox.poll(db)
        if n:
            print(f"Ingested {n} manual note(s).")
    except Exception as e:
        print(f"Telegram ingest skipped: {e}")

    # 2. Backfill the window, then refresh the weekly fitness scores.
    try:
        g = garmin.connect()
        backfill(g, db, week_start, week_end)
        try:
            db.upsert_scores(garmin.normalize_scores(
                garmin.fetch_fitness_scores(g, week_end)))
        except Exception as e:
            print(f"Fitness scores unavailable: {e}")
    except Exception as e:
        print(f"Garmin unavailable, reviewing on stored data only: {e}")

    # 3. Read the week back and compute every figure deterministically.
    plan_path = project_path(cfg["paths"]["plan"])
    plan_doc = planmod.load(plan_path)
    ctx = planmod.resolve(plan_doc, week_start)

    week_metrics = db.metrics_between(week_start.isoformat(), week_end.isoformat())
    activities = db.activities_between(week_start.isoformat(), week_end.isoformat())
    recs = db.recommendations_between(week_start.isoformat(), week_end.isoformat(), "daily")
    notes = db.notes_between(week_start.isoformat(), week_end.isoformat())
    load_hist = db.daily_load((week_end - timedelta(days=27)).isoformat(),
                              week_end.isoformat())
    scores = db.latest_scores(before=week_end.isoformat(), limit=2)

    coverage = db.data_coverage((week_end - timedelta(days=27)).isoformat(),
                                week_end.isoformat())
    summary = metrics.summarize(activities, ctx, load_history=load_hist,
                                week_start=week_start, week_end=week_end,
                                coverage_days=coverage)
    gaps = metrics.missing_days(activities, week_metrics, week_start, week_end)

    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))
    system, user = prompt.build_weekly(principles, plan_doc, summary, week_metrics,
                                       recs, week_start, week_end,
                                       gaps=gaps, notes=notes, scores=scores)

    if dry_run:
        print(user)
        return summary

    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        notify.send(cfg, f"Montanha coach: weekly model call failed: {e}")
        raise

    header = llm.parse_header(text)
    message = llm.human_part(text) or text

    # 4. Apply a bounded adjustment to NEXT week, with a backup first.
    next_ctx = planmod.resolve(plan_doc, week_end + timedelta(days=1))
    proposed = llm.extract_json_block(text) or {}
    adj = proposed.get("adjustments") if isinstance(proposed, dict) else None
    if adj:
        backup = f"{plan_path}.{datetime.now():%Y%m%d%H%M%S}.bak"
        shutil.copy2(plan_path, backup)
        plan_doc, log = planmod.apply_adjustment(plan_doc, next_ctx, adj)
        applied = [l for l in log if "->" in l or l.startswith("recorded")]
        if applied:
            with open(plan_path, "w") as f:
                json.dump(plan_doc, f, indent=2, ensure_ascii=False)
            db.add_plan_version(week_end.isoformat(), json.dumps(plan_doc),
                                "; ".join(log))
            message += "\n\nPlan updated: " + "; ".join(applied)
        else:
            message += "\n\nNo plan change applied (" + "; ".join(log) + ")"
    else:
        message += "\n\nNo plan change proposed."

    if gaps:
        message += f"\n\nNote: {len(gaps)} day(s) had no data ({', '.join(gaps)}) — totals are a lower bound."
    if not notes:
        message += "\n\nNo strength or foot data logged this week." + inbox.PROMPT_FOOTER

    db.add_weekly_review(
        week_start.isoformat(), week_end.isoformat(),
        header.get("ADHERENCE", ""), header.get("FATIGUE_TREND", ""),
        header.get("CHANGES", ""), text, json.dumps(summary, default=str),
    )

    notify.send(cfg, message, subject="Montanha coach — weekly review")
    print("Weekly review sent.")
    return summary


if __name__ == "__main__":
    import sys
    run(dry_run="--dry-run" in sys.argv)
