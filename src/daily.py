"""
Morning job: fetch -> reason -> send today's recommendation.

Two changes worth knowing about:
  * Today's activities are now stored, not just yesterday's. Previously
    fetch_day(today) was called and its activities thrown away, which is why a
    Sunday-evening weekly review could never see Sunday.
  * A short lookback backfills any day a previous run missed. Gaps used to be
    permanent -- if the Mac was asleep on Monday, Sunday's training was lost
    from the record forever.
"""
from datetime import date, timedelta

from .config import CONFIG, project_path
from . import garmin, store, prompt, llm, notify, plan as planmod, inbox, runtime, backup_status

# How far back each run looks for days that were never fetched. Was 10: the
# Mac was gone from race day (2026-10-03) for 12+ days, and 10 would have
# silently left the race itself out of the record. A day already fetched on a
# later date is skipped (store.missing_activity_dates), so a normal morning
# still fetches only yesterday; the wide window only costs requests when there
# is a real gap to fill.
BACKFILL_DAYS = 45
HISTORY_DAYS = 60   # lookback for the pace/vert-density distance estimate


class SleepNotSynced(RuntimeError):
    """Last night isn't in Garmin yet. Raised BEFORE the model call so the app
    can say "sync your watch and try again" instead of spending minutes
    reasoning about a night with no data. The 06:30 schedule hit exactly this:
    it often ran before the watch had synced."""


def sleep_synced(m: dict) -> bool:
    return bool(m.get("sleep_duration_min"))


def run(today: date = None, deliver: bool = True, require_sleep: bool = False,
        progress=lambda step: None):
    """deliver=True: the CLI path -- send to Telegram, alert on failure.
    deliver=False: the app path -- return the message; the app shows it and
    sends to the phone only if asked.
    require_sleep: stop before the model if last night isn't synced yet."""
    cfg = CONFIG
    db = store.Store(project_path(cfg["paths"]["db"]))
    db.init()

    today = today or date.today()
    yesterday = today - timedelta(days=1)

    try:
        n = inbox.poll(db)
        if n:
            print(f"Ingested {n} manual note(s).")
    except Exception as e:
        print(f"Telegram ingest skipped: {e}")

    progress("Fetching Garmin data")
    try:
        g = garmin.connect()
        today_raw = garmin.fetch_day(g, today)
        yest_raw = garmin.fetch_day(g, yesterday)
    except Exception as e:
        if deliver:
            notify.send(cfg, runtime.failure_message("daily (Garmin fetch)", e))
            e.notified = True
        raise

    m_today = garmin.normalize(today_raw)
    m_yest = garmin.normalize(yest_raw)
    db.upsert_metrics(m_today)
    db.upsert_metrics(m_yest)

    # Store BOTH days' activities. Today's used to be discarded.
    yest_acts = garmin.normalize_activities(yest_raw)
    for a in yest_acts:
        db.upsert_activity(a)
    db.mark_fetched(yesterday.isoformat())
    for a in garmin.normalize_activities(today_raw):
        db.upsert_activity(a)
    # Deliberately NOT marking today as fetched: at 06:30 today's training has
    # not happened yet, so this fetch proves nothing about the day.

    # Recover anything an earlier run missed.
    missing = db.missing_activity_dates(today - timedelta(days=BACKFILL_DAYS),
                                        today - timedelta(days=1))
    for i, d in enumerate(missing, 1):
        if len(missing) > 1:
            progress(f"Catching up missed days from Garmin ({i} of {len(missing)})")
        try:
            raw = garmin.fetch_day(g, d)
            db.upsert_metrics(garmin.normalize(raw))
            for a in garmin.normalize_activities(raw):
                db.upsert_activity(a)
            db.mark_fetched(d.isoformat())
            print(f"Backfilled {d}")
        except Exception as e:
            print(f"Backfill failed for {d}: {e}")

    # After the catch-up, so missed days are recovered even on a morning when
    # the watch hasn't synced yet.
    if require_sleep and not sleep_synced(m_today):
        raise SleepNotSynced("Last night's sleep isn't in Garmin yet.")

    recent = db.recent_metrics(cfg.get("context_days", 14), end=today)
    plan_doc = planmod.load(project_path(cfg["paths"]["plan"]))
    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))
    notes = db.notes_between((today - timedelta(days=7)).isoformat(), today.isoformat())
    ctx = planmod.resolve(plan_doc, today)

    # Feeds the distance estimate only. The plan prescribes long runs in time
    # and vert; how many km that comes to depends on this athlete's own pace at
    # this athlete's own vert density, so it is derived from their history and
    # labelled an estimate rather than guessed by the model.
    history = db.activities_between(
        (today - timedelta(days=HISTORY_DAYS)).isoformat(), yesterday.isoformat())

    strength_reference = None
    sref_path = cfg["paths"].get("strength_reference")
    if sref_path:
        try:
            strength_reference = prompt.load_strength_reference(project_path(sref_path))
        except FileNotFoundError:
            strength_reference = None

    system, user = prompt.build_daily(principles, plan_doc, m_today, yest_acts,
                                      recent, today=today, notes=notes,
                                      strength_reference=strength_reference,
                                      activity_history=history)

    progress("Thinking (local model)")
    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        if deliver:
            notify.send(cfg, runtime.failure_message("daily (model call)", e))
            e.notified = True
        raise

    header = llm.parse_header(text)
    message = llm.human_part(text) or text
    if deliver:
        # The app has its own check-in form and backup banner; the Telegram
        # path keeps the reply codes and the nudge.
        message += inbox.PROMPT_FOOTER
        nudge = backup_status.reminder(cfg)
        if nudge:
            message += "\n\n" + nudge

    db.add_recommendation(
        today.isoformat(), "daily",
        ctx.get("week_in_phase"),
        header.get("VERDICT"),
        text,
        cfg["model"]["name"],
        session_detail=header.get("SESSION"),
        phase_id=ctx.get("phase_id"),
    )

    if not deliver:
        return {"message": message, "verdict": header.get("VERDICT"),
                "session": header.get("SESSION")}
    notify.send(cfg, message)
    print(f"Daily recommendation sent ({header.get('VERDICT')}).")


if __name__ == "__main__":
    runtime.run_job("daily", run)
