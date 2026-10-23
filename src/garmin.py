"""
ISOLATED Garmin layer -- the ONLY module that talks to Garmin Connect.

If Garmin changes their API and the coach breaks, you fix it HERE and nowhere
else. Uses Garmin's official Personal API (Oct 2026) over plain `requests`.

Auth model:
  * Create a key once at https://connect.garmin.com/app/settings/personalApiToken
    (30 days .. 1 year) and put it in .env as GARMIN_API_KEY.
  * connect() exchanges the key for a 24 h bearer token on every run. There is
    no login, no password and no MFA -- and nothing to cache.

What the Personal API does NOT provide (all 404, verified 2026-10-23 with
scripts/probe_personal_api.py): Training Readiness, recovery time, acute load /
ACWR, training status, VO2max, hill/endurance score, lactate threshold. The
coach replaces readiness with src/readiness.py and computes ACWR from stored
activity loads. Rows fetched by the old garminconnect client keep their values,
and normalize() still reads that older payload shape so --reparse keeps working.

Every field path is best-effort and the full raw JSON is always stored, so a
payload change costs you a `.get()` path here and never any history.
"""
import os
from datetime import date, datetime, timezone

import requests

HOST = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_PAGE = "https://connect.garmin.com/app/settings/personalApiToken"


class GarminAuthError(RuntimeError):
    """The API key is missing, expired or deleted."""


class Client:
    """A bearer-token session against the Personal API host."""

    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/json"})

    def get(self, path, params=None):
        r = self.s.get(HOST + path, params=params, timeout=60)
        if r.status_code in (401, 403):
            raise GarminAuthError(f"Garmin API key rejected (HTTP {r.status_code})")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()


def _token_from(body):
    if isinstance(body, str):
        return body
    for k in ("access_token", "accessToken", "token"):
        if isinstance(body, dict) and body.get(k):
            return body[k]
    return None


def connect() -> Client:
    """Exchange GARMIN_API_KEY for a bearer token. One request per run."""
    key = os.environ.get("GARMIN_API_KEY", "").strip()
    if not key:
        raise GarminAuthError(f"GARMIN_API_KEY is not set in .env -- create one at {KEY_PAGE}")
    r = requests.post(HOST + TOKEN_PATH, headers={"Authorization": f"ApiKey {key}"},
                      timeout=30)
    if r.status_code in (400, 401, 403):
        raise GarminAuthError(f"Garmin API key rejected (HTTP {r.status_code}) -- "
                              f"it may have expired; create a new one at {KEY_PAGE}")
    r.raise_for_status()
    try:
        token = _token_from(r.json())
    except ValueError:
        token = r.text.strip().strip('"')
    if not token:
        raise GarminAuthError("Garmin token exchange returned no token")
    return Client(token)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except GarminAuthError:
        raise  # an expired key must stop the run, not degrade to empty data
    except Exception as e:  # never let one endpoint kill the whole run
        return {"_error": str(e)}


def _activities(g: Client, start: str, end: str, page: int = 100) -> list:
    out, offset = [], 0
    while True:
        batch = g.get("/activitylist-service/activities/search/activities",
                      {"startDate": start, "endDate": end, "start": offset, "limit": page})
        if not isinstance(batch, list) or not batch:
            return out
        out.extend(batch)
        if len(batch) < page:
            return out
        offset += page


def activity_detail(g: Client, activity_id) -> dict:
    """The parts of /activity/{id} the list doesn't carry: watch RPE / feel,
    stamina, and the run / walk / stand split. One request per activity.
    metadataDTO (profile, images, device) is dropped -- nothing in it is used."""
    d = g.get(f"/activity-service/activity/{activity_id}") or {}
    return {"summaryDTO": d.get("summaryDTO"), "splitSummaries": d.get("splitSummaries")}


def with_details(g: Client, acts):
    if isinstance(acts, list):
        for a in acts:
            if isinstance(a, dict) and a.get("activityId"):
                a["detail"] = _safe(activity_detail, g, a["activityId"])
    return acts


def fetch_hr_zones(g: Client) -> dict:
    """The RUNNING heart-rate zones configured on the watch (zone time in every
    activity is counted against these)."""
    zones = g.get("/biometric-service/heartRateZones/v2") or []
    by_sport = {z.get("sport"): z for z in zones if isinstance(z, dict)}
    return by_sport.get("RUNNING") or by_sport.get("DEFAULT") or {}


def fetch_day(g: Client, cdate: date) -> dict:
    """Fetch the raw payloads for a single calendar date."""
    d = cdate.isoformat()
    return {
        "date": d,
        "source": "personal_api",
        "sleep": _safe(g.get, "/sleep-service/sleep/dailySleep", {"date": d}),
        "hrv": _safe(g.get, f"/hrv-service/hrv/{d}"),
        "stats": _safe(g.get, "/usersummary-service/v2/dailysummaries",
                       {"startDate": d, "endDate": d}),
        "heart_rate": _safe(g.get, "/wellness-service/wellness/dailyHeartRate", {"date": d}),
        "body_battery": _safe(g.get, "/wellness-service/wellness/bodyBattery/reports/daily",
                              {"startDate": d, "endDate": d}),
        "activities": with_details(g, _safe(_activities, g, d, d)),
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


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _minutes(seconds):
    return round(seconds / 60) if isinstance(seconds, (int, float)) else None


def _as_dict(x):
    """Garmin returns dict, list-of-dict, or an {'_error': ...} stub. Normalize."""
    if isinstance(x, list):
        x = x[0] if x else {}
    return x if isinstance(x, dict) and "_error" not in x else {}


def _for_date(x, d: str):
    """The entry for date d from a range response (list, or {'data': [...]})."""
    if isinstance(x, dict) and isinstance(x.get("data"), list):
        x = x["data"]
    if isinstance(x, list):
        for e in x:
            if isinstance(e, dict) and d in (e.get("calendarDate"), e.get("date")):
                return e
        return {}
    return _as_dict(x)


def _gmt_ms(ts):
    """Garmin timestamps: epoch ms, or '2026-10-20T07:10:57.0' strings in GMT."""
    if isinstance(ts, (int, float)):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.split(".")[0]).replace(
                tzinfo=timezone.utc).timestamp() * 1000
        except ValueError:
            return None
    return None


# A body-battery reading counts as "at wake" if it is within this of sleep end.
# Garmin logs ~6 points a day; on most days one falls 2-9 min after waking.
BB_WAKE_WINDOW_MS = 60 * 60 * 1000


def body_battery_at_wake(report: dict, sleep_end_gmt):
    """Reading nearest the end of sleep, from the daily body-battery report."""
    end = _gmt_ms(sleep_end_gmt)
    values = report.get("bodyBatteryValuesArray") if isinstance(report, dict) else None
    if end is None or not isinstance(values, list):
        return None
    best = None
    for v in values:
        if not (isinstance(v, list) and len(v) >= 2 and _num(v[0]) and _num(v[1]) is not None):
            continue
        gap = abs(v[0] - end)
        if gap <= BB_WAKE_WINDOW_MS and (best is None or gap < best[0]):
            best = (gap, v[1])
    return best[1] if best else None


def _sleep_charge(report: dict):
    """Overnight body-battery recharge, from the report's SLEEP event."""
    for e in (report.get("bodyBatteryActivityEvent") or []) if isinstance(report, dict) else []:
        if isinstance(e, dict) and e.get("eventType") == "SLEEP":
            return _num(e.get("bodyBatteryImpact"))
    return None


def _daily_stats(day: dict) -> dict:
    """Resting HR / body battery / stress / respiration from either API shape."""
    raw = day.get("stats")
    if isinstance(raw, dict) and "data" in raw:   # Personal API v2/dailysummaries
        s = _for_date(raw, day["date"])
        hr = _as_dict(day.get("heart_rate"))
        bb_report = _for_date(day.get("body_battery"), day["date"])
        sleep = _as_dict(day.get("sleep"))
        return {
            "resting_hr": _dig(s, "heartRate", "restingValue"),
            "rhr_7d_avg": hr.get("lastSevenDaysAvgRestingHeartRate"),
            "body_battery_low": _dig(s, "bodyBattery", "minValue"),
            "body_battery_high": _dig(s, "bodyBattery", "maxValue"),
            "body_battery_wake": body_battery_at_wake(
                bb_report, sleep.get("sleepEndTimestampGMT")),
            "body_battery_charge": _sleep_charge(bb_report),
            "stress_avg": _dig(s, "stress", "avgLevel"),
            "waking_respiration": _num(_dig(s, "respiration", "avgValue")),
            # --- daily movement: load the activity list never shows ---
            "steps": _dig(s, "movement", "steps", "value"),
            "floors_up_m": _num(_dig(s, "movement", "floorsAscended", "distanceInMeters")),
            "floors_down_m": _num(_dig(s, "movement", "floorsDescended", "distanceInMeters")),
            "intensity_moderate_min": _dig(s, "intensityMinutes", "moderate"),
            "intensity_vigorous_min": _dig(s, "intensityMinutes", "vigorous"),
            "stress_rest_pct": (round(_dig(s, "stress", "restProportion") * 100, 1)
                                if _num(_dig(s, "stress", "restProportion")) is not None
                                else None),
        }
    s = _as_dict(raw)                              # garminconnect usersummary
    return {
        "resting_hr": s.get("restingHeartRate"),
        "rhr_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate"),
        "body_battery_low": s.get("bodyBatteryLowestValue"),
        "body_battery_high": s.get("bodyBatteryHighestValue"),
        "body_battery_wake": s.get("bodyBatteryAtWakeTime"),
        "body_battery_charge": _num(s.get("bodyBatteryDuringSleep")),
        "stress_avg": s.get("averageStressLevel"),
        "waking_respiration": _num(s.get("avgWakingRespirationValue")),
        "steps": s.get("totalSteps"),
        "floors_up_m": _num(s.get("floorsAscendedInMeters")),
        "floors_down_m": _num(s.get("floorsDescendedInMeters")),
        "intensity_moderate_min": s.get("moderateIntensityMinutes"),
        "intensity_vigorous_min": s.get("vigorousIntensityMinutes"),
        "stress_rest_pct": _num(s.get("restStressPercentage")),
    }


def normalize(day: dict) -> dict:
    """Extract the fields we store. Raw JSON is kept for reprocessing."""
    import json

    sleep = _as_dict(day.get("sleep"))
    # garminconnect wrapped the sleep summary in dailySleepDTO; the Personal
    # API returns that same object unwrapped.
    ds = _as_dict(sleep.get("dailySleepDTO")) if "dailySleepDTO" in sleep else sleep
    scores = _as_dict(ds.get("sleepScores"))
    readiness = _as_dict(day.get("readiness"))     # garminconnect-era rows only
    hrv = _as_dict(day.get("hrv"))
    hrv_sum = _as_dict(hrv.get("hrvSummary"))
    hrv_base = _as_dict(hrv_sum.get("baseline"))

    return {
        "date": day["date"],
        # --- sleep ---
        "sleep_score": _dig(scores, "overall", "value"),
        "sleep_duration_min": _minutes(ds.get("sleepTimeSeconds")),
        "deep_min": _minutes(ds.get("deepSleepSeconds")),
        "rem_min": _minutes(ds.get("remSleepSeconds")),
        "light_min": _minutes(ds.get("lightSleepSeconds")),
        "awake_min": _minutes(ds.get("awakeSleepSeconds")),
        # sleepNeed.actual is ALREADY in minutes (480 = 8 h), unlike every
        # other sleep field on this payload, which is in seconds.
        "sleep_need_min": _num(_dig(ds, "sleepNeed", "actual")),
        # Why Garmin raised sleep need. "HIGH_ACWR" is the one place the
        # Personal API still lets Garmin's own load ratio show through.
        "sleep_need_feedback": _dig(ds, "sleepNeed", "trainingFeedback"),
        # Garmin's own reading of the night, e.g. NEGATIVE_LATE_BED_TIME: tells
        # the coach whether a poor night was training or life.
        "sleep_feedback": ds.get("sleepScoreFeedback"),
        "sleep_insight": ds.get("sleepScoreInsight"),
        "avg_sleep_stress": _num(ds.get("avgSleepStress")),
        "awake_count": _num(ds.get("awakeCount")),
        "sleep_respiration": _num(ds.get("averageRespirationValue")),
        "deep_pct": _dig(scores, "deepPercentage", "value"),
        "rem_pct": _dig(scores, "remPercentage", "value"),
        # --- HRV, always alongside YOUR baseline (an absolute number is noise) ---
        "hrv_overnight": hrv_sum.get("lastNightAvg"),
        "hrv_weekly_avg": hrv_sum.get("weeklyAvg"),
        "hrv_baseline_low": hrv_base.get("balancedLow"),
        "hrv_baseline_high": hrv_base.get("balancedUpper"),
        "hrv_status": hrv_sum.get("status"),
        # --- cardiac / autonomic ---
        **_daily_stats(day),
        # --- Garmin readiness: only present on garminconnect-era rows ---
        "training_readiness": readiness.get("score"),
        "readiness_level": readiness.get("level"),
        "readiness_feedback": readiness.get("feedbackShort"),
        "recovery_time_min": readiness.get("recoveryTime"),
        "acute_load": readiness.get("acuteLoad"),
        "acwr_pct": readiness.get("acwrFactorPercent"),
        "raw_json": json.dumps(day, default=str),
    }


def normalize_activities(day: dict) -> list:
    """Normalized activity rows, with the uphill-relevant fields extracted.

    elevation_gain_m / elevation_loss_m and the HR-zone seconds were all
    already present in the payload and simply never pulled out -- which is why
    weekly vert and intensity distribution were invisible to the coach.
    """
    import json

    acts = day.get("activities")
    if not isinstance(acts, list):
        return []

    out = []
    for a in acts:
        if not isinstance(a, dict):
            continue
        dur = a.get("duration")
        moving = a.get("movingDuration")
        dist = a.get("distance")
        out.append({
            "id": str(a.get("activityId")),
            "date": day["date"],
            "type": _dig(a, "activityType", "typeKey"),
            "name": a.get("activityName"),
            "start_local": a.get("startTimeLocal"),
            "duration_min": round(dur / 60, 1) if _num(dur) else None,
            "moving_min": round(moving / 60, 1) if _num(moving) else None,
            "distance_km": round(dist / 1000, 2) if _num(dist) else None,
            # --- the whole point: vertical ---
            "elevation_gain_m": _num(a.get("elevationGain")),
            "elevation_loss_m": _num(a.get("elevationLoss")),
            "max_elevation_m": _num(a.get("maxElevation")),
            # --- intensity distribution, in seconds per zone ---
            "z1_s": _num(a.get("hrTimeInZone_1")),
            "z2_s": _num(a.get("hrTimeInZone_2")),
            "z3_s": _num(a.get("hrTimeInZone_3")),
            "z4_s": _num(a.get("hrTimeInZone_4")),
            "z5_s": _num(a.get("hrTimeInZone_5")),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            # --- pace made fair across terrain ---
            "avg_speed_mps": _num(a.get("averageSpeed")),
            "gap_speed_mps": _num(a.get("avgGradeAdjustedSpeed")),
            # --- running economy / form decay over long efforts ---
            "avg_power": _num(a.get("avgPower")),
            "norm_power": _num(a.get("normPower")),
            "cadence": _num(a.get("averageRunningCadenceInStepsPerMinute")),
            "ground_contact_ms": _num(a.get("avgGroundContactTime")),
            "stride_length_cm": _num(a.get("avgStrideLength")),
            "vertical_osc_cm": _num(a.get("avgVerticalOscillation")),
            # --- heat block validation ---
            "max_temp_c": _num(a.get("maxTemperature")),
            "min_temp_c": _num(a.get("minTemperature")),
            # --- cost ---
            "training_load": a.get("activityTrainingLoad"),
            "aerobic_te": a.get("aerobicTrainingEffect"),
            "anaerobic_te": a.get("anaerobicTrainingEffect"),
            "te_label": a.get("trainingEffectLabel"),
            "body_battery_delta": _num(a.get("differenceBodyBattery")),
            "calories": _num(a.get("calories")),
            "location": a.get("locationName"),
            **_detail_fields(a.get("detail")),
            "strength_sets_json": _strength_sets(a.get("summarizedExerciseSets")),
            "raw_json": json.dumps(a, default=str),
        })
    return out


def _detail_fields(detail) -> dict:
    """Watch self-evaluation, stamina and run/walk split from activity_detail()."""
    s = _as_dict(_dig(detail, "summaryDTO", default={}))
    rpe = _num(s.get("directWorkoutRpe"))
    splits = {sp.get("splitType"): sp for sp in (_dig(detail, "splitSummaries", default=[]) or [])
              if isinstance(sp, dict)}
    run, walk = splits.get("RWD_RUN", {}), splits.get("RWD_WALK", {})
    return {
        # Garmin stores perceived effort x10 (athlete confirmed: 30 -> RPE 3).
        "rpe": rpe / 10 if rpe is not None else None,
        # 0 very weak / 25 weak / 50 normal / 75 strong / 100 very strong
        "feel": _num(s.get("directWorkoutFeel")),
        "stamina_start": _num(s.get("beginPotentialStamina")),
        "stamina_end": _num(s.get("endPotentialStamina")),
        "run_min": _minutes(run.get("duration")) if run else None,
        "walk_min": _minutes(walk.get("duration")) if walk else None,
        "run_gain_m": _num(run.get("elevationGain")),
        "walk_gain_m": _num(walk.get("elevationGain")),
    }


def _strength_sets(summary) -> str | None:
    """Per-exercise totals as Garmin Connect shows them (including the
    athlete's corrections there). Weight is grams in the payload; kg here."""
    import json
    if not isinstance(summary, list) or not summary:
        return None
    rows = []
    for e in summary:
        if not isinstance(e, dict) or not e.get("sets"):
            continue
        rows.append({"cat": e.get("category"), "ex": e.get("subCategory"),
                     "sets": e.get("sets"), "reps": e.get("reps"),
                     "kg": round(e["maxWeight"] / 1000, 1) if _num(e.get("maxWeight")) else 0,
                     "s": round(e["duration"] / 1000) if _num(e.get("duration")) else None})
    return json.dumps(rows) if rows else None


def _latest(payload, *value_keys):
    """Pull the most recent numeric value from Garmin's varied list/dict shapes."""
    if isinstance(payload, list):
        payload = payload[-1] if payload else None
    if not isinstance(payload, dict) or "_error" in payload:
        return None
    for k in value_keys:
        if payload.get(k) is not None:
            return payload[k]
    for nest in ("mostRecent", "latest"):
        sub = payload.get(nest)
        if isinstance(sub, dict):
            for k in value_keys:
                if sub.get(k) is not None:
                    return sub[k]
    return None


def normalize_scores(payload: dict) -> dict:
    """Flatten the weekly fitness-score payloads into stored columns.

    Only garminconnect ever fetched these (the Personal API has none of them);
    kept so `scripts/backfill.py --reparse` can still re-read stored history.
    """
    import json

    ts = _as_dict(payload.get("training_status"))
    status_map = _dig(ts, "mostRecentTrainingStatus", "latestTrainingStatusData", default={})
    latest_status = _as_dict(next(iter(status_map.values()), {})) if isinstance(status_map, dict) else {}
    bal_map = _dig(ts, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap", default={})
    load_bal = _as_dict(next(iter(bal_map.values()), {})) if isinstance(bal_map, dict) else {}
    heat = latest_status.get("heatAltitudeAcclimationDTO")

    return {
        "date": payload["date"],
        "hill_score": _latest(payload.get("hill_score"), "overallScore", "hillScore", "score"),
        "hill_strength": _latest(payload.get("hill_score"), "strengthScore"),
        "hill_endurance": _latest(payload.get("hill_score"), "enduranceScore"),
        "endurance_score": _latest(payload.get("endurance_score"), "overallScore", "score"),
        "vo2max": latest_status.get("vo2MaxValue")
                  or _latest(payload.get("max_metrics"), "vo2MaxValue"),
        "training_status": latest_status.get("trainingStatus"),
        "acute_load": load_bal.get("dailyTrainingLoadAcute"),
        "chronic_load": load_bal.get("dailyTrainingLoadChronic"),
        "acwr": load_bal.get("dailyAcuteChronicWorkloadRatio"),
        "load_ratio_feedback": load_bal.get("trainingStatusFeedbackPhrase"),
        "lactate_threshold_bpm": _latest(payload.get("lactate_threshold"),
                                         "heartRate", "lactateThresholdHeartRate"),
        "lactate_threshold_speed": _latest(payload.get("lactate_threshold"),
                                           "speed", "lactateThresholdSpeed"),
        "running_tolerance": _latest(payload.get("running_tolerance"),
                                     "runningTolerance", "score"),
        "heat_acclimation": heat.get("heatAcclimationPercentage") if isinstance(heat, dict) else None,
        "raw_json": json.dumps(payload, default=str),
    }
