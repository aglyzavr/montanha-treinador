"""
Per-second activity data -> a short, deterministic durability summary.

Garmin's /activity/{id}/details returns ~1 point per second (26 channels: HR,
speed, grade-adjusted speed, elevation, vertical speed, power, cadence, ground
contact, stride, temperature, performance condition, ...). The raw response is
kept gzipped (store.activity_streams) so everything here can be recomputed when
a formula or the plan's AeT changes (scripts/backfill.py --reanalyze). Only the
summary below ever reaches the prompt.

What is computed, and why:
  below_aet_pct   share of HR time at or below the PLAN's AeT. The watch's zone
                  minutes are counted against its own zones (Z2 tops out below
                  the plan's AeT), so this is the honest "easy" share.
  climbs          VAM and HR on grades > 8 %, first half vs second half: does
                  climbing speed hold at the same HR late in a long effort?
  descent         speed on grades < -8 %: downhill skill / eccentric durability.
  form            stride, ground contact and cadence (running samples only),
                  first quarter vs last: neuromuscular fade.
  heat            temperature first vs last quarter, so a hot finish is not
                  read as lost fitness.
  aet_test        whole-run HR:pace decoupling on self-paced runs is an
                  artifact and is deliberately NOT computed. Only a segment that
                  looks like a real AeT test -- after a 15 min warm-up, 60 min
                  at a steady HR near the plan's AeT, on flat ground -- gets a
                  Pa:HR drift figure (GAP per beat, first 30 vs last 30 min;
                  under 5 % = at or below AeT).
"""
from __future__ import annotations

import gzip
import json
from statistics import mean, median

STREAM_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
                "ultra_run", "hiking")
MIN_DURATION_MIN = 40

CLIMB_GRADE, DESCENT_GRADE = 0.08, -0.08
GRADE_WINDOW_S = 30
MIN_SEGMENT_MIN = 5        # climbing/descending time needed per half to report
RUNNING_CADENCE = 140      # steps/min; below this the athlete is hiking
WARMUP_S, TEST_S = 15 * 60, 60 * 60
TEST_HR_BAND = 5           # bpm around the window's median HR
TEST_STEADY_SHARE = 0.9
TEST_HR_BELOW_AET, TEST_HR_ABOVE_AET = 10, 3
TEST_MAX_M_PER_KM = 15     # "flat"


def wants_stream(a: dict) -> bool:
    return (a.get("type") in STREAM_TYPES
            and (a.get("duration_min") or 0) >= MIN_DURATION_MIN)


def pack(payload: dict) -> bytes:
    return gzip.compress(json.dumps(payload, separators=(",", ":")).encode())


def unpack(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob))


def downsampled(payload: dict) -> bool:
    got, total = payload.get("metricsCount"), payload.get("totalMetricsCount")
    return isinstance(got, int) and isinstance(total, int) and got < 0.9 * total


def decode(payload: dict) -> dict:
    """{channel: [values]} -- mapped by metricsIndex, never by position: the
    descriptor order differs between activities."""
    idx = {m["key"]: m["metricsIndex"] for m in payload.get("metricDescriptors") or []}
    rows = [p.get("metrics") or [] for p in payload.get("activityDetailMetrics") or []]
    return {k: [r[i] if i < len(r) else None for r in rows] for k, i in idx.items()}


def aet_from_plan(ctx: dict):
    """Top of the plan's upper-aerobic zone (Z2 tops out at AeT)."""
    z = (ctx or {}).get("hr_zones") or {}
    band = z.get("z2_upper_aerobic_bpm")
    return band[1] if isinstance(band, list) and len(band) == 2 else None


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _avg(vals):
    vals = [v for v in vals if _num(v) is not None]
    return mean(vals) if vals else None


def _med(vals):
    vals = [v for v in vals if _num(v) is not None]
    return median(vals) if vals else None


def _grades(ele, dist):
    """Grade per sample over a ~30 s window (single-sample grade is noise)."""
    n, out = len(ele), [None] * len(ele)
    for i in range(n):
        j = min(n - 1, i + GRADE_WINDOW_S)
        if None in (ele[i], ele[j], dist[i], dist[j]):
            continue
        dd = dist[j] - dist[i]
        if dd > 10:
            out[i] = (ele[j] - ele[i]) / dd
    return out


def _r(x, nd=0):
    return None if x is None else (round(x) if nd == 0 else round(x, nd))


def analyze(payload: dict, aet_bpm=None) -> dict | None:
    s = decode(payload)
    hr = s.get("directHeartRate") or []
    t = s.get("sumDuration") or []
    n = len(hr)
    if n < 600 or len(t) != n or t[-1] is None:
        return None
    total = t[-1]
    ele = s.get("directElevation") or [None] * n
    dist = s.get("sumDistance") or [None] * n
    speed = s.get("directSpeed") or [None] * n
    vspeed = s.get("directVerticalSpeed") or [None] * n
    cad = s.get("directDoubleCadence") or [None] * n
    grade = _grades(ele, dist)
    half = [0 if (t[i] or 0) < total / 2 else 1 for i in range(n)]
    quarter = [min(3, int(4 * (t[i] or 0) / total)) for i in range(n)]
    running = [_num(cad[i]) is not None and cad[i] >= RUNNING_CADENCE for i in range(n)]

    out = {"duration_min": _r(total / 60), "points": n}

    if aet_bpm:
        hrs = [h for h in hr if _num(h)]
        if hrs:
            out["aet_bpm"] = aet_bpm
            out["below_aet_pct"] = _r(100 * sum(h <= aet_bpm for h in hrs) / len(hrs))

    climbs = {}
    for h, label in ((0, "first"), (1, "second")):
        idx = [i for i in range(n) if half[i] == h and grade[i] is not None
               and grade[i] > CLIMB_GRADE]
        if len(idx) >= MIN_SEGMENT_MIN * 60:
            climbs[label] = {"min": _r(len(idx) / 60), "vam": _r(3600 * _avg(vspeed[i] for i in idx)),
                         "hr": _r(_avg(hr[i] for i in idx)),
                         "hiking_pct": _r(100 * sum(not running[i] for i in idx) / len(idx))}
    if climbs:
        out["climbs"] = climbs

    down = [i for i in range(n) if grade[i] is not None and grade[i] < DESCENT_GRADE]
    if len(down) >= MIN_SEGMENT_MIN * 60:
        out["descent"] = {"min": _r(len(down) / 60),
                          "speed_mps": _r(_avg(speed[i] for i in down), 2)}

    form = {}
    for key, ch in (("stride_cm", "directStrideLength"), ("gct_ms", "directGroundContactTime"),
                    ("cadence", "directDoubleCadence")):
        vals = s.get(ch) or [None] * n
        q1 = _med(vals[i] for i in range(n) if quarter[i] == 0 and running[i])
        q4 = _med(vals[i] for i in range(n) if quarter[i] == 3 and running[i])
        if q1 is not None and q4 is not None:
            form[key] = [_r(q1, 1), _r(q4, 1)]
    if form:
        out["form_q1_q4"] = form

    temp = s.get("directAirTemperature") or [None] * n
    t1 = _avg(temp[i] for i in range(n) if quarter[i] == 0)
    t4 = _avg(temp[i] for i in range(n) if quarter[i] == 3)
    if t1 is not None and t4 is not None:
        out["temp_q1_q4"] = [_r(t1), _r(t4)]

    pc = [(t[i], v) for i, v in enumerate(s.get("directPerformanceCondition") or [])
          if _num(v) is not None]
    if len(pc) > 1200:
        out["perf_condition"] = [_r(_avg(v for _, v in pc[:600]), 1),
                                 _r(_avg(v for _, v in pc[-600:]), 1)]

    if aet_bpm:
        test = _aet_test(s, t, hr, ele, dist, aet_bpm)
        if test:
            out["aet_test"] = test
    return out


def _aet_test(s, t, hr, ele, dist, aet):
    """First 60-min window after warm-up that looks like a valid AeT test."""
    gap = s.get("directGradeAdjustedSpeed") or [None] * len(hr)
    n = len(hr)
    start = next((i for i in range(n) if (t[i] or 0) >= WARMUP_S), None)
    while start is not None:
        end = next((j for j in range(start, n) if (t[j] or 0) >= t[start] + TEST_S), None)
        if end is None:
            return None
        hrs = [h for h in hr[start:end] if _num(h)]
        if len(hrs) > 0.8 * (end - start):
            m = median(hrs)
            steady = sum(abs(h - m) <= TEST_HR_BAND for h in hrs) / len(hrs)
            climb = sum(max(0, (ele[i + 1] or 0) - (ele[i] or 0))
                        for i in range(start, end - 1) if None not in (ele[i], ele[i + 1]))
            km = ((dist[end] or 0) - (dist[start] or 0)) / 1000
            if (steady >= TEST_STEADY_SHARE and aet - TEST_HR_BELOW_AET <= m <= aet + TEST_HR_ABOVE_AET
                    and km > 0 and climb / km <= TEST_MAX_M_PER_KM):
                mid = (start + end) // 2
                eff = [_avg(gap[i] / hr[i] for i in rng if _num(gap[i]) and _num(hr[i]) and hr[i] > 0)
                       for rng in (range(start, mid), range(mid, end))]
                if None not in eff and eff[0] > 0:
                    return {"start_min": _r(t[start] / 60), "hr": _r(m),
                            "m_per_km": _r(climb / km), "drift_pct": _r(100 * (eff[0] - eff[1]) / eff[0], 1)}
        # step one minute on
        start = next((i for i in range(start, n) if (t[i] or 0) >= t[start] + 60), None)
    return None


def sync(g, db, activities: list, aet_bpm, log=print, pace_s: float = 0) -> int:
    """Fetch, store and analyse the stream of every qualifying activity that
    doesn't have one yet. One request each; failures are logged, not raised."""
    import time
    from . import garmin
    have = db.stream_ids()
    done = 0
    for a in activities:
        if not wants_stream(a) or str(a["id"]) in have:
            continue
        try:
            payload = garmin.fetch_stream(g, a["id"])
        except garmin.GarminAuthError:
            raise
        except Exception as e:
            log(f"  stream {a['date']} {a['id']} failed: {e}")
            continue
        if not payload:
            continue
        if downsampled(payload):
            log(f"  stream {a['date']} {a['id']}: Garmin returned a downsampled series "
                f"({payload.get('metricsCount')} of {payload.get('totalMetricsCount')} points); "
                "not stored")
            continue
        db.save_stream(a["id"], pack(payload))
        summary = analyze(payload, aet_bpm)
        if summary:
            db.save_analysis(a["id"], aet_bpm, summary)
        done += 1
        if pace_s:
            time.sleep(pace_s)
    return done


def reanalyze(db, aet_bpm) -> int:
    """Recompute every stored stream (new formula or new AeT). No network."""
    n = 0
    for activity_id in db.stream_ids():
        summary = analyze(unpack(db.get_stream(activity_id)), aet_bpm)
        if summary:
            db.save_analysis(activity_id, aet_bpm, summary)
            n += 1
    return n


def describe(a: dict) -> str:
    """One compact clause for an activity line. Empty when not analysed."""
    x = a.get("analysis")
    if not x:
        return ""
    bits = []
    if x.get("below_aet_pct") is not None:
        bits.append(f"{x['below_aet_pct']}% of HR time <= AeT {x['aet_bpm']}")
    c = x.get("climbs") or {}
    if "first" in c and "second" in c:
        h1, h2 = c["first"], c["second"]
        bits.append(f"climbs >8%: VAM {h1['vam']}->{h2['vam']} m/h at HR "
                    f"{h1['hr']}->{h2['hr']} (1st vs 2nd half, hiking {h1['hiking_pct']}%"
                    f"->{h2['hiking_pct']}%)")
    elif c:
        h = next(iter(c.values()))
        bits.append(f"climbs >8%: {h['min']} min, VAM {h['vam']} m/h at HR {h['hr']}")
    if x.get("descent"):
        bits.append(f"descents <-8%: {x['descent']['min']} min at {x['descent']['speed_mps']} m/s")
    f = x.get("form_q1_q4") or {}
    if f:
        bits.append("form 1st->last quarter: " + ", ".join(
            f"{k.replace('_', ' ')} {v[0]:g}->{v[1]:g}" for k, v in f.items()))
    if x.get("temp_q1_q4"):
        bits.append(f"temp {x['temp_q1_q4'][0]}->{x['temp_q1_q4'][1]}C")
    if x.get("perf_condition"):
        bits.append(f"Garmin performance condition {x['perf_condition'][0]:+g}->"
                    f"{x['perf_condition'][1]:+g}")
    if x.get("aet_test"):
        at = x["aet_test"]
        bits.append(f"AeT-TEST SEGMENT from min {at['start_min']}: 60 min steady at HR {at['hr']}, "
                    f"flat ({at['m_per_km']} m/km), Pa:HR drift {at['drift_pct']}% "
                    f"(under 5% = at or below AeT)")
    return "; ".join(bits)
