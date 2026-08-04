"""
ISOLATED Garmin layer -- the ONLY module that talks to Garmin Connect.

If Garmin changes their API/auth and the coach breaks, you fix it HERE and
nowhere else. Uses python-garminconnect (>=0.3.6).

Auth model:
  * Run scripts/setup_garmin_login.py ONCE. It logs in (handling MFA
    interactively) and caches a token under GARMIN_TOKENSTORE (~/.garminconnect).
  * connect() below RESUMES from that token on every scheduled run, without a
    fresh login -- this avoids Garmin's 429 / bot defenses.

Every field path is best-effort and the full raw JSON is always stored, so a
payload change costs you a `.get()` path here and never any history.
"""
import os
from datetime import date

from garminconnect import Garmin


def _tokenstore() -> str:
    return os.path.expanduser(os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect"))


def connect() -> Garmin:
    """Resume from the cached token; fall back to a credential login.

    NOTE: the previous version of this function had no `return` on the success
    path, so it returned None whenever token resumption worked, and every run
    silently fell through to a full credential login -- exactly the behaviour
    that trips Garmin's rate limiter. Both paths now return.
    """
    store = _tokenstore()
    try:
        g = Garmin()
        if hasattr(g, "resume_login"):
            g.resume_login()
        else:
            g.login(store)
        return g
    except Exception:
        pass

    # Fallback: full login. Only works without MFA (no TTY in a launchd job).
    g = Garmin(
        email=os.environ["GARMIN_EMAIL"],
        password=os.environ["GARMIN_PASSWORD"],
    )
    g.login()
    try:
        g.garth.dump(store)
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


def fetch_fitness_scores(g: Garmin, cdate: date) -> dict:
    """Weekly-cadence uphill / endurance metrics. One call each, all optional.

    hill_score and endurance_score are Garmin's own uphill-running and
    ultra-capacity metrics -- the most directly relevant numbers Garmin
    produces for this athlete. lactate_threshold tracks the AeT-LT gap that is
    the stated primary objective of training year 1.
    """
    d = cdate.isoformat()
    start = (cdate.replace(day=1)).isoformat()
    return {
        "date": d,
        "hill_score": _safe(g.get_hill_score, start, d),
        "endurance_score": _safe(g.get_endurance_score, start, d),
        "training_status": _safe(g.get_training_status, d),
        "lactate_threshold": _safe(g.get_lactate_threshold, start, d),
        "running_tolerance": _safe(g.get_running_tolerance, d),
        "max_metrics": _safe(g.get_max_metrics, d),
        "race_predictions": _safe(g.get_race_predictions),
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


def normalize(day: dict) -> dict:
    """Extract the fields we store. Raw JSON is kept for reprocessing."""
    import json

    sleep = _as_dict(day.get("sleep"))
    ds = _as_dict(sleep.get("dailySleepDTO"))
    scores = _as_dict(ds.get("sleepScores"))
    stats = _as_dict(day.get("stats"))
    readiness = _as_dict(day.get("readiness"))
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
        "resting_hr": stats.get("restingHeartRate"),
        "rhr_7d_avg": stats.get("lastSevenDaysAvgRestingHeartRate"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_wake": stats.get("bodyBatteryAtWakeTime"),
        "stress_avg": stats.get("averageStressLevel"),
        "waking_respiration": _num(stats.get("avgWakingRespirationValue")),
        # --- readiness + load balance (previously we kept only `score`) ---
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
            "raw_json": json.dumps(a, default=str),
        })
    return out


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

    These endpoints are the least stable part of the API and are all wrapped in
    _safe(), so a shape change degrades to None rather than breaking the run.
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
