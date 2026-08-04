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
from . import garmin, store, prompt, llm, notify, plan as planmod, inbox

BACKFILL_DAYS = 10


def run(today: date = None):
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

    try:
        g = garmin.connect()
        today_raw = garmin.fetch_day(g, today)
        yest_raw = garmin.fetch_day(g, yesterday)
    except Exception as e:
        notify.send(cfg, f"Montanha coach: Garmin fetch failed today: {e}")
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
    for d in db.missing_activity_dates(today - timedelta(days=BACKFILL_DAYS),
                                       today - timedelta(days=1)):
        try:
            raw = garmin.fetch_day(g, d)
            db.upsert_metrics(garmin.normalize(raw))
            for a in garmin.normalize_activities(raw):
                db.upsert_activity(a)
            db.mark_fetched(d.isoformat())
            print(f"Backfilled {d}")
        except Exception as e:
            print(f"Backfill failed for {d}: {e}")

    recent = db.recent_metrics(cfg.get("context_days", 14), end=today)
    plan_doc = planmod.load(project_path(cfg["paths"]["plan"]))
    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))
    notes = db.notes_between((today - timedelta(days=7)).isoformat(), today.isoformat())
    ctx = planmod.resolve(plan_doc, today)

    system, user = prompt.build_daily(principles, plan_doc, m_today, yest_acts,
                                      recent, today=today, notes=notes)

    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        notify.send(cfg, f"Montanha coach: model call failed: {e}")
        raise

    header = llm.parse_header(text)
    message = (llm.human_part(text) or text) + inbox.PROMPT_FOOTER

    db.add_recommendation(
        today.isoformat(), "daily",
        ctx.get("week_in_phase"),
        header.get("VERDICT"),
        text,
        cfg["model"]["name"],
        session_detail=header.get("SESSION"),
        phase_id=ctx.get("phase_id"),
    )

    notify.send(cfg, message)
    print(f"Daily recommendation sent ({header.get('VERDICT')}).")


if __name__ == "__main__":
    run()
