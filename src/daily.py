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
from . import (garmin, store, prompt, llm, notify, plan as planmod, inbox, runtime,
               backup_status, metrics, readiness, streams)

# How far back each run looks for days that were never fetched. Was 10: the
# Mac was gone from race day (2026-10-03) for 12+ days, and 10 would have
# silently left the race itself out of the record. A day already fetched on a
# later date is skipped (store.missing_activity_dates), so a normal morning
# still fetches only yesterday; the wide window only costs requests when there
# is a real gap to fill.
BACKFILL_DAYS = 45
HISTORY_DAYS = 60   # lookback for the pace/vert-density distance estimate
READINESS_HISTORY_DAYS = 60   # HRV z-score baseline for the local readiness
STREAM_LOOKBACK_DAYS = 3      # runs whose per-second data is fetched if missing
WEIGHT_HISTORY_DAYS = 40      # 7-day average now vs 4 weeks earlier


class SleepNotSynced(RuntimeError):
    """Last night isn't in Garmin yet. Raised BEFORE the model call so the app
    can say "sync your watch and try again" instead of spending minutes
    reasoning about a night with no data. The 06:30 schedule hit exactly this:
    it often ran before the watch had synced."""


def sleep_synced(m: dict) -> bool:
    return bool(m.get("sleep_duration_min"))


STAGE_LOOKBACK_DAYS = 60
_GENERIC_STRENGTH_TEXT = {"", "done", "strength session", "strength", "gym"}


def _last_strength_note(db, today: date):
    """Most recent strength note that says what was done (stage/exercises).
    Looked up over 60 days: the 7-day manual-log window used to forget a
    logged stage after a week and fall back to Stage 1."""
    notes = db.notes_between((today - timedelta(days=STAGE_LOOKBACK_DAYS)).isoformat(),
                             today.isoformat())
    detailed = [n for n in notes if n.get("kind") == "strength"
                and (n.get("text") or "").strip().lower() not in _GENERIC_STRENGTH_TEXT]
    return detailed[-1] if detailed else None


def _last_strength_activity(db, today: date):
    """Most recent Garmin strength session with an exercise list -- the stage
    fallback when no check-in note says what was done."""
    acts = db.activities_between((today - timedelta(days=STAGE_LOOKBACK_DAYS)).isoformat(),
                                 today.isoformat())
    done = [a for a in acts if metrics._is_strength(a) and metrics.strength_exercises(a)]
    return done[-1] if done else None


def _ensure_strength(cfg, system, user, text, due, reference, last_note, progress):
    """Strength days must come back with a circuit or a reasoned skip.

    Checked in Python, like every other figure the athlete relies on: the
    model dropped the Monday slot every single week. One corrective retry,
    then the plan's slot and the reference circuits are appended verbatim so
    the session is never silently lost.
    """
    if prompt.strength_answered(text):
        return text
    print("Strength section missing from the answer; asking the model again.")
    progress("Strength section missing -- asking the model again")
    try:
        retry = llm.chat(cfg, system, user, followup=(text, prompt.STRENGTH_RETRY))
        if llm.parse_header(retry).get("VERDICT"):
            text = retry
    except Exception as e:
        print(f"Strength retry failed: {e}")
    if prompt.strength_answered(text):
        return text
    print("Strength still missing after retry; appending the plan's strength session.")
    return text.rstrip() + "\n\n" + prompt.strength_fallback(due, reference, last_note)


def _session_detail(header: dict):
    """SESSION plus the strength call, so the weekly review's list of daily
    verdicts shows strength too (no schema change)."""
    session = header.get("SESSION")
    strength = header.get("STRENGTH")
    if strength and strength.strip().lower() != "none planned":
        return f"{session or ''} | strength: {strength}".strip(" |")
    return session


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

    # Garmin's readiness and acute load don't exist in the Personal API; both
    # are computed here from stored history (see src/readiness.py).
    long_hist = db.recent_metrics(READINESS_HISTORY_DAYS + len(recent), end=today)
    loads = db.daily_load((today - timedelta(days=28 + len(recent))).isoformat(),
                          yesterday.isoformat())
    acwr = metrics._acwr(loads, yesterday, db.data_coverage(
        (yesterday - timedelta(days=27)).isoformat(), yesterday.isoformat()))
    today_readiness = readiness.compute(
        m_today, [m for m in long_hist if m["date"] < today.isoformat()], loads, today, acwr)
    readiness_trend = readiness.series(long_hist, loads, len(recent))

    plan_doc = planmod.load(project_path(cfg["paths"]["plan"]))
    principles = prompt.load_principles(project_path(cfg["paths"]["principles"]))
    notes = db.notes_between((today - timedelta(days=7)).isoformat(), today.isoformat())
    ctx = planmod.resolve(plan_doc, today)

    # Per-second data for recent runs: one request each, only for runs/hikes
    # of 40+ min not fetched yet. Never fatal -- the call works without it.
    progress("Analysing recent runs")
    try:
        streams.sync(g, db, db.activities_between(
            (today - timedelta(days=STREAM_LOOKBACK_DAYS)).isoformat(), today.isoformat()),
            streams.aet_from_plan(ctx))
    except Exception as e:
        print(f"Run analysis skipped: {e}")
    # Read yesterday back from the store so each activity carries its analysis.
    yest_acts = db.activities_between(yesterday.isoformat(), yesterday.isoformat())

    # Weigh-ins: one request for the last two weeks (catches late syncs).
    try:
        for w in garmin.fetch_weights(g, today - timedelta(days=14), today):
            db.upsert_weight(w)
    except Exception as e:
        print(f"Weigh-ins skipped: {e}")
    weights = db.weights_between((today - timedelta(days=WEIGHT_HISTORY_DAYS)).isoformat(),
                                 today.isoformat())
    weight_lines = metrics.describe_weight(metrics.weight_trend(weights, today),
                                           metrics.sweat_tests(weights, yest_acts))

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

    # This week's strength so far, so a missed slot is offered again later in
    # the week instead of silently disappearing.
    week_start = today - timedelta(days=today.weekday())
    strength_status = prompt.strength_status(
        plan_doc, today,
        db.notes_between(week_start.isoformat(), today.isoformat()),
        db.activities_between(week_start.isoformat(), today.isoformat()))
    last_strength_note = _last_strength_note(db, today)
    recent_acts = db.activities_between((today - timedelta(days=3)).isoformat(),
                                        yesterday.isoformat())
    last_strength_activity = _last_strength_activity(db, today)
    due = prompt.strength_due(plan_doc, today, strength_status) if strength_reference else None

    system, user = prompt.build_daily(principles, plan_doc, m_today, yest_acts,
                                      recent, today=today, notes=notes,
                                      strength_reference=strength_reference,
                                      activity_history=history,
                                      strength_status=strength_status,
                                      last_strength_note=last_strength_note,
                                      readiness=today_readiness, acwr=acwr,
                                      readiness_trend=readiness_trend,
                                      yesterday_metrics=m_yest,
                                      recent_activities=recent_acts,
                                      last_strength_activity=last_strength_activity,
                                      weight_lines=weight_lines)

    progress("Thinking (local model)")
    try:
        text = llm.chat(cfg, system, user)
    except Exception as e:
        if deliver:
            notify.send(cfg, runtime.failure_message("daily (model call)", e))
            e.notified = True
        raise

    if due:
        text = _ensure_strength(cfg, system, user, text, due, strength_reference,
                                last_strength_note, progress)

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
        session_detail=_session_detail(header),
        phase_id=ctx.get("phase_id"),
    )

    if not deliver:
        return {"message": message, "verdict": header.get("VERDICT"),
                "session": header.get("SESSION")}
    notify.send(cfg, message)
    print(f"Daily recommendation sent ({header.get('VERDICT')}).")


if __name__ == "__main__":
    runtime.run_job("daily", run)
