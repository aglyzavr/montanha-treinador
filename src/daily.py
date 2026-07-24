"""Morning job: fetch -> reason -> send today's recommendation."""
from datetime import date, timedelta

from .config import CONFIG, project_path
from . import garmin, store, prompt, llm, notify


def run():
    cfg = CONFIG
    db = store.Store(project_path(cfg["paths"]["db"]))
    db.init()

    yesterday = date.today() - timedelta(days=1)

    try:
        g = garmin.connect()
        today_raw = garmin.fetch_day(g, date.today())     # last night's sleep, today's readiness
        yest_raw = garmin.fetch_day(g, yesterday)          # yesterday's training
    except Exception as e:
        notify.send(cfg, f"⚠️ Montanha coach: Garmin fetch failed today: {e}")
        raise

    m_today = garmin.normalize(today_raw)
    m_yest = garmin.normalize(yest_raw)
    db.upsert_metrics(m_today)
    db.upsert_metrics(m_yest)

    yest_acts = garmin.normalize_activities(yest_raw)
    for a in yest_acts:
        db.upsert_activity(a)

    recent = db.recent_metrics(cfg.get("context_days", 14))
    plan = prompt.load_plan(project_path(cfg["paths"]["plan"]))
    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))

    system, user = prompt.build_daily(principles, plan, m_today, yest_acts, recent)

    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        notify.send(cfg, f"⚠️ Montanha coach: model call failed: {e}")
        raise

    header = llm.parse_header(text)
    message = llm.human_part(text) or text

    db.add_recommendation(
        date.today().isoformat(), "daily",
        plan.get("week_index"),
        header.get("VERDICT"),
        text,
        cfg["model"]["name"],
    )

    notify.send(cfg, message)
    print("Daily recommendation sent.")


if __name__ == "__main__":
    run()
