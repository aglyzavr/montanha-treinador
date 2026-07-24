"""
ISOLATED Garmin layer -- the ONLY module that talks to Garmin Connect.

If Garmin changes their API/auth and the coach breaks, you fix it HERE and
nowhere else. Uses python-garminconnect (>=0.3.6). `garth` is deprecated;
do not import it directly.

Auth model:
  * Run scripts/setup_garmin_login.py ONCE. It logs in (handling MFA
    interactively) and caches a token under GARMIN_TOKENSTORE (~/.garminconnect).
  * connect() below then RESUMES from that token on every scheduled run,
    without a fresh login -- this avoids Garmin's 429 / bot defenses.

The field paths in normalize() are best-effort. Payload shapes can vary by
account/version, so we also store the full raw JSON. After your first real
fetch, print the raw dicts and adjust the .get() paths if a field comes back
None when it shouldn't.
"""
import os
from datetime import date

from garminconnect import Garmin


def _tokenstore() -> str:
    return os.path.expanduser(os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect"))


def connect() -> Garmin:
    """Resume from the cached token; fall back to credential login."""
    # NOTE: python-garminconnect 0.3+ handles auth persistence internally via
    # resume_login(). The old garth.dump() is deprecated and no longer needed.
    try:
        g = Garmin()
        if hasattr(g, 'resume_login'):  # Newer versions use this for token resumption
            g.resume_login()  # reuses cached credentials from ~/.garminconnect
        else:  # Fallback to credential login (will cache automatically)
            g.login(email=os.environ["GARMIN_EMAIL"], password=os.environ.get("GARMIN_PASSWORD") or "")
    except Exception:
        # Fallback (only works if MFA is not required, since there is no TTY here).
        g = Garmin(
            email=os.environ["GARMIN_EMAIL"],
            password=os.environ["GARMIN_PASSWORD"],
        )
        g.login()
        try:
            g.garth.dump(tokenstore)
        except Exception:
            pass
        return g


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # never let one endpoint kill the whole run
        return {"_error": str(e)}


def fetch_day(g: Garmin, cdate: date) -> dict:
    """Fetch the raw payloads for a single calendar date."""
    d = cdate.isoformat()
    return {
        "date": d,
        "sleep": _safe(g.get_sleep_data, d),
        "hrv": _safe(g.get_hrv_data, d),
        "readiness": _safe(g.get_training_readiness, d),
        "stats": _safe(g.get_stats, d),
        "activities": _safe(g.get_activities_by_date, d, d),
    }


def _dig(d, *keys, default=None):
    """Walk nested dicts safely: _dig(x, 'a', 'b') -> x['a']['b'] or default."""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return default
    return cur


def normalize(day: dict) -> dict:
    """Extract the handful of fields we store. Raw JSON is kept for reprocessing."""
    import json

    sleep = day.get("sleep") if isinstance(day.get("sleep"), dict) else {}
    ds = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}

    stats = day.get("stats") if isinstance(day.get("stats"), dict) else {}

    readiness = day.get("readiness")
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    if not isinstance(readiness, dict):
        readiness = {}

    hrv = day.get("hrv") if isinstance(day.get("hrv"), dict) else {}
    hrv_summary = hrv.get("hrvSummary", {}) if isinstance(hrv, dict) else {}

    def minutes(seconds):
        return round(seconds / 60) if isinstance(seconds, (int, float)) else None

    return {
        "date": day["date"],
        "sleep_score": _dig(ds, "sleepScores", "overall", "value"),
        "sleep_duration_min": minutes(ds.get("sleepTimeSeconds")),
        "deep_min": minutes(ds.get("deepSleepSeconds")),
        "rem_min": minutes(ds.get("remSleepSeconds")),
        "light_min": minutes(ds.get("lightSleepSeconds")),
        "awake_min": minutes(ds.get("awakeSleepSeconds")),
        "hrv_overnight": hrv_summary.get("lastNightAvg"),
        "resting_hr": stats.get("restingHeartRate"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "training_readiness": readiness.get("score"),
        "stress_avg": stats.get("averageStressLevel"),
        "raw_json": json.dumps(day, default=str),
    }


def normalize_activities(day: dict) -> list:
    """Return a list of normalized activity dicts for the day."""
    import json

    acts = day.get("activities")
    if not isinstance(acts, list):
        return []
    out = []
    for a in acts:
        if not isinstance(a, dict):
            continue
        dur = a.get("duration")
        dist = a.get("distance")
        out.append({
            "id": str(a.get("activityId")),
            "date": day["date"],
            "type": _dig(a, "activityType", "typeKey"),
            "duration_min": round(dur / 60, 1) if isinstance(dur, (int, float)) else None,
            "distance_km": round(dist / 1000, 2) if isinstance(dist, (int, float)) else None,
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "training_load": a.get("activityTrainingLoad"),
            "aerobic_te": a.get("aerobicTrainingEffect"),
            "anaerobic_te": a.get("anaerobicTrainingEffect"),
            "raw_json": json.dumps(a, default=str),
        })
    return out
