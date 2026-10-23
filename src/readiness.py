"""
Local readiness score -- the replacement for Garmin's Training Readiness.

The Personal API has no readiness, recovery time or acute load, so this is
computed here, in Python, from signals it does provide. Every part carries its
own reason, so the model can explain the call instead of quoting one number.

  part        weight  signal
  hrv           30    last night vs your 60-day mean (z-score); below Garmin's
                      balanced range is scored low regardless
  sleep         20    sleep score, scaled down when you slept less than need
  rhr           15    resting HR vs its 7-day average (+5 bpm scores 0)
  battery       10    body battery at wake
  load          25    the last 3 days' training load (weighted 1 / 0.5 / 0.25)
                      vs your 28-day daily mean

Load carries the weight Garmin gave recovery time: checked against 90 days of
stored Garmin readiness (2026-10-23), a yesterday-only load at weight 10 let
the score bounce back the day after a rest day following a 200-min run (74 vs
Garmin's 13); the decayed 3-day load at 25 tracks Garmin better (r 0.41 -> 0.50).

Parts with no data are left out and the rest re-weighted; fewer than three
parts gives no score at all (missing data is never guessed). Two adjustments
sit on top: -10 when load is ramping (local ACWR > 1.5, or Garmin's sleep need
citing HIGH_ACWR), and a hard override to "easy" when HRV is below range AND
resting HR is up 3+ bpm -- the combination the principles treat as a stop sign.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean, pstdev

WEIGHTS = {"hrv": 30, "sleep": 20, "rhr": 15, "battery": 10, "load": 25}
LOAD_DECAY = (1.0, 0.5, 0.25)   # yesterday, the day before, three days ago
MIN_PARTS = 3
HRV_HISTORY_MIN = 14     # days of HRV before a z-score means anything
ACWR_RAMP = 1.5
RAMP_PENALTY = 10
BANDS = ((75, "go: quality or long session OK"),
         (50, "aerobic only: keep it easy-moderate, no intensity"),
         (0, "easy or rest"))


def _n(x):
    return x if isinstance(x, (int, float)) else None


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _hrv(m, history):
    hrv, low = _n(m.get("hrv_overnight")), _n(m.get("hrv_baseline_low"))
    if hrv is None:
        return None
    if low is not None and hrv < low:
        return 0.2, f"{hrv} ms is below your balanced range ({low}-{m.get('hrv_baseline_high')})"
    past = [h["hrv_overnight"] for h in history[-60:] if _n(h.get("hrv_overnight"))]
    if len(past) >= HRV_HISTORY_MIN and pstdev(past) > 0:
        mu, sd = mean(past), pstdev(past)
        z = (hrv - mu) / sd
        return _clamp(0.7 + 0.15 * z, 0.3, 1.0), f"{hrv} ms vs 60-day mean {round(mu)} (z {z:+.1f})"
    return 0.7, f"{hrv} ms, inside your balanced range"


def _sleep(m):
    score, dur, need = _n(m.get("sleep_score")), _n(m.get("sleep_duration_min")), \
        _n(m.get("sleep_need_min"))
    if score is None:
        return None
    s, why = score / 100, f"score {score}"
    if dur and need:
        s *= min(1.0, dur / need)
        why += f", {dur} of {need} min needed"
    return s, why


def _rhr(m, history):
    rhr = _n(m.get("resting_hr"))
    base = _n(m.get("rhr_7d_avg"))
    if base is None:
        past = [h["resting_hr"] for h in history[-7:] if _n(h.get("resting_hr"))]
        base = round(mean(past), 1) if len(past) >= 4 else None
    if rhr is None or base is None:
        return None
    delta = rhr - base
    return _clamp(1 - delta / 5) if delta > 0 else 1.0, f"{rhr} bpm vs 7-day {base} ({delta:+g})"


def _battery(m):
    wake = _n(m.get("body_battery_wake"))
    if wake is None:
        return None
    why = f"{wake} at wake"
    if _n(m.get("body_battery_charge")) is not None:
        why += f", +{m['body_battery_charge']} overnight"
    return wake / 100, why


def _load(load_by_date, today):
    def day(i):
        return load_by_date.get((today - timedelta(days=i)).isoformat()) or 0
    chronic = sum(day(i) for i in range(1, 29)) / 28
    if chronic <= 0:
        return None
    recent = [day(i) for i in range(1, len(LOAD_DECAY) + 1)]
    r = sum(w * v for w, v in zip(LOAD_DECAY, recent)) / (chronic * sum(LOAD_DECAY))
    return _clamp(1.5 - 0.5 * r), (f"last 3 days {'/'.join(str(round(v)) for v in recent)} "
                                   f"vs 28-day daily mean {round(chronic)} ({r:.1f}x)")


def compute(m: dict, history: list, load_by_date: dict, today: date,
            acwr: dict = None) -> dict | None:
    """Readiness for `today`. history = metric rows BEFORE today, oldest first."""
    if not m:
        return None
    found = {"hrv": _hrv(m, history), "sleep": _sleep(m), "rhr": _rhr(m, history),
             "battery": _battery(m), "load": _load(load_by_date or {}, today)}
    parts = {k: v for k, v in found.items() if v is not None}
    if len(parts) < MIN_PARTS:
        return None

    total_w = sum(WEIGHTS[k] for k in parts)
    score = 100 * sum(WEIGHTS[k] * s for k, (s, _) in parts.items()) / total_w

    adjustments, ramp = [], []
    ratio = (acwr or {}).get("ratio")
    if isinstance(ratio, (int, float)) and ratio > ACWR_RAMP:
        ramp.append(f"ACWR {ratio}")
    if m.get("sleep_need_feedback") == "HIGH_ACWR":
        ramp.append("Garmin sleep need cites HIGH_ACWR")
    if ramp:
        score -= RAMP_PENALTY
        adjustments.append(f"-{RAMP_PENALTY}: load ramping ({', '.join(ramp)})")
    score = round(_clamp(score, 0, 100))
    band = next(label for floor, label in BANDS if score >= floor)

    override = None
    hrv, low = _n(m.get("hrv_overnight")), _n(m.get("hrv_baseline_low"))
    rhr_part = parts.get("rhr")
    rhr_up = rhr_part is not None and rhr_part[0] <= 0.4   # +3 bpm or more
    if hrv is not None and low is not None and hrv < low and rhr_up:
        override = "HRV below range AND resting HR up 3+ bpm -> easy or rest, whatever the score"
        band = BANDS[-1][1]

    return {
        "score": score,
        "band": band,
        "parts": [{"name": k, "points": round(WEIGHTS[k] * s), "max": WEIGHTS[k], "detail": why}
                  for k, (s, why) in parts.items()],
        "missing": [k for k in WEIGHTS if k not in parts],
        "adjustments": adjustments,
        "override": override,
    }


def series(metrics: list, load_by_date: dict, days: int) -> dict:
    """{date: score} for the last `days` rows of a date-sorted metrics list."""
    out = {}
    for i in range(max(0, len(metrics) - days), len(metrics)):
        m = metrics[i]
        r = compute(m, metrics[:i], load_by_date, date.fromisoformat(m["date"]))
        out[m["date"]] = r["score"] if r else None
    return out


def describe(r: dict | None) -> str:
    """Prompt lines for the recovery block."""
    if not r:
        return ("  readiness (local): not enough data today -- judge from the "
                "individual signals above")
    L = [f"  readiness (computed locally, NOT Garmin's Training Readiness): "
         f"{r['score']}/100 -> {r['band']}"]
    for p in r["parts"]:
        L.append(f"    {p['name']}: {p['points']}/{p['max']} -- {p['detail']}")
    if r["missing"]:
        L.append(f"    no data for: {', '.join(r['missing'])} (left out, rest re-weighted)")
    for a in r["adjustments"]:
        L.append(f"    {a}")
    if r["override"]:
        L.append(f"    OVERRIDE: {r['override']}")
    return "\n".join(L)
