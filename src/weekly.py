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
from . import (garmin, store, prompt, llm, notify, metrics, plan as planmod, inbox, runtime,
               streams, strength)

SCORES_MAX_AGE_DAYS = 14
PROGRESSION_DAYS = 42      # lift history shown in the review
RHR_STALE_BPM = 4          # watch's resting-HR setting vs measured


def _zones(z: dict, week_metrics: list) -> dict | None:
    """The watch's running zones, flagged when its resting HR is out of date."""
    if not z or not z.get("zone3Floor"):
        return None
    measured = [m["rhr_7d_avg"] for m in week_metrics if m.get("rhr_7d_avg")]
    used = z.get("restingHeartRateUsed")
    return {
        "z1": z.get("zone1Floor"), "z2": z.get("zone2Floor"), "z3": z.get("zone3Floor"),
        "z4": z.get("zone4Floor"), "z5": z.get("zone5Floor"),
        "lthr": z.get("lactateThresholdHeartRateUsed"), "max": z.get("maxHeartRateUsed"),
        "rhr_used": used, "rhr_measured": measured[-1] if measured else None,
        "rhr_stale": bool(used and measured and abs(used - measured[-1]) >= RHR_STALE_BPM),
    }


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


def run(today: date = None, dry_run: bool = False, deliver: bool = True,
        progress=lambda step: None):
    """deliver=False (the app): return {"message", "summary"} instead of sending."""
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

    # 2. Backfill the window. (Garmin's weekly fitness scores -- hill/endurance
    # score, VO2max, training status -- aren't in the Personal API.)
    progress("Fetching Garmin data")
    zones = {}
    try:
        g = garmin.connect()
        backfill(g, db, week_start, week_end)
        try:
            zones = garmin.fetch_hr_zones(g)
        except Exception as e:
            print(f"HR zone settings unavailable: {e}")
        aet = streams.aet_from_plan(planmod.resolve(
            planmod.load(project_path(cfg["paths"]["plan"])), week_start))
        streams.sync(g, db, db.activities_between(week_start.isoformat(),
                                                  week_end.isoformat()), aet)
        for w in garmin.fetch_weights(g, week_end - timedelta(days=40), week_end):
            db.upsert_weight(w)
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
    # Scores stopped updating with the switch to the Personal API; a stale row
    # must not be presented as this week's fitness.
    scores = [s for s in db.latest_scores(before=week_end.isoformat(), limit=2)
              if s["date"] >= (week_end - timedelta(days=SCORES_MAX_AGE_DAYS)).isoformat()]

    coverage = db.data_coverage((week_end - timedelta(days=27)).isoformat(),
                                week_end.isoformat())
    summary = metrics.summarize(activities, ctx, load_history=load_hist,
                                week_start=week_start, week_end=week_end,
                                coverage_days=coverage)
    gaps = metrics.missing_days(activities, week_metrics, week_start, week_end)

    # Planned strength vs evidence, so "0 of 2" is a computed fact rather than
    # something the model has to notice from an empty manual log.
    slots = planmod.strength_slots(plan_doc, week_start, week_end)
    done = metrics.strength_done_dates(notes, activities)
    summary["strength_planned"] = len(slots)
    summary["strength_done"] = len(done)
    summary["strength_missed"] = ([d.isoformat() for d, _ in slots if d.isoformat() not in done]
                                  if len(done) < len(slots) else [])
    summary["strength_content"] = metrics.strength_content(activities)
    summary["lift_progression"] = metrics.lift_progression(db.activities_between(
        (week_end - timedelta(days=PROGRESSION_DAYS)).isoformat(), week_end.isoformat()))
    # The coach's stage decisions: a region that met its target moves up from
    # next Monday (stored before the model call, so a dry run changes nothing).
    stages = strength.review(db, week_end, apply=not dry_run)
    summary["strength_stages"] = strength.describe(stages)
    summary["movement"] = metrics.movement(week_metrics)
    summary["hr_zones"] = _zones(zones, week_metrics)
    summary["durability"] = metrics.durability(activities)
    weights = db.weights_between((week_end - timedelta(days=40)).isoformat(),
                                 week_end.isoformat())
    summary["weight"] = metrics.weight_trend(weights, week_end)
    summary["sweat_tests"] = metrics.sweat_tests(weights, activities)

    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))
    system, user = prompt.build_weekly(principles, plan_doc, summary, week_metrics,
                                       recs, week_start, week_end,
                                       gaps=gaps, notes=notes, scores=scores)

    if dry_run:
        print(user)
        return summary

    progress("Thinking (local model)")
    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        if deliver:
            notify.send(cfg, runtime.failure_message("weekly (model call)", e))
            e.notified = True
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
            with open(plan_path, "w", encoding="utf-8") as f:
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
        message += "\n\nNo strength or foot data logged this week." + (
            inbox.PROMPT_FOOTER if deliver else "")

    db.add_weekly_review(
        week_start.isoformat(), week_end.isoformat(),
        header.get("ADHERENCE", ""), header.get("FATIGUE_TREND", ""),
        header.get("CHANGES", ""), text, json.dumps(summary, default=str),
    )

    if not deliver:
        return {"message": message, "summary": summary}
    notify.send(cfg, message, subject="Montanha coach — weekly review")
    print("Weekly review sent.")
    return summary


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    runtime.run_job("weekly", run, dry_run=dry, model=not dry)
