#!/usr/bin/env python3
"""
Find out what the official Garmin Personal API key actually gives us.

    python scripts/probe_personal_api.py                  # probes yesterday
    python scripts/probe_personal_api.py --date 2026-10-20

Read-only and standalone: nothing in the app changes. It exchanges
GARMIN_API_KEY (from .env) for a bearer token, calls every endpoint the coach
uses today (the same paths python-garminconnect calls) plus the paths the
Personal API documents, then runs the coach's own normalizers on whatever comes
back to show, column by column, what we would keep and what we would lose.

Output: a summary on screen, data/probe/report.md and the raw responses in
data/probe/<date>/. Calls are paced and never retried (see claude-project-setup.md).
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src import garmin  # noqa: E402  -- only the pure normalize* functions

TOKEN_URL = ("https://personal.connectapi.garmin.com/personalapitoken-service"
             "/personal-api/personal-api-key/token")
HOSTS = ("https://personal.connectapi.garmin.com", "https://connectapi.garmin.com")
PACE_S = 1.0
OUT = ROOT / "data" / "probe"


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def obtain_token(key: str) -> str:
    r = requests.post(TOKEN_URL, headers={"Authorization": f"ApiKey {key}"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed: HTTP {r.status_code}\n{r.text[:500]}")
    try:
        body = r.json()
    except ValueError:
        return r.text.strip().strip('"')
    if isinstance(body, str):
        return body
    for k in ("access_token", "accessToken", "token"):
        if body.get(k):
            return body[k]
    sys.exit(f"Token exchange: no token field in response keys {list(body)}")


class Probe:
    def __init__(self, token: str, outdir: Path):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.outdir = outdir
        self.results = []   # one row per endpoint
        self.host = None    # first host that answered 200 is tried first afterwards

    def get(self, name, path, params=None, binary=False):
        hosts = (self.host,) + tuple(h for h in HOSTS if h != self.host) if self.host else HOSTS
        tried = []
        for host in hosts:
            time.sleep(PACE_S)
            try:
                r = self.s.get(host + path, params=params, timeout=60)
            except requests.RequestException as e:
                tried.append(f"{host.split('//')[1]}: {type(e).__name__}")
                continue
            tried.append(f"{host.split('//')[1]}: {r.status_code}")
            if r.status_code == 200:
                self.host = self.host or host
                payload = self._save(name, r, binary)
                self.results.append({"name": name, "path": path, "ok": True,
                                     "status": " / ".join(tried), "shape": _shape(payload)})
                return payload
            if r.status_code == 429:
                break   # rate limited: stop, do not hammer
        self.results.append({"name": name, "path": path, "ok": False,
                             "status": " / ".join(tried), "shape": ""})
        return None

    def _save(self, name, r, binary):
        if binary:
            (self.outdir / f"{name}.zip").write_bytes(r.content)
            return {"_bytes": len(r.content)}
        try:
            payload = r.json()
        except ValueError:
            payload = {"_text": r.text[:2000]}
        (self.outdir / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str),
                                                  encoding="utf-8")
        return payload


def _shape(p):
    if isinstance(p, list):
        inner = p[0] if p and isinstance(p[0], dict) else {}
        return f"list[{len(p)}] " + ", ".join(list(inner)[:8])
    if isinstance(p, dict):
        return ", ".join(list(p)[:10]) + (" ..." if len(p) > 10 else "")
    return type(p).__name__


def probe_all(pr: Probe, d: date, display_name):
    ds, start = d.isoformat(), d.replace(day=1).isoformat()
    week_ago = (d - timedelta(days=6)).isoformat()
    dn = display_name

    # --- what fetch_day() uses today; first non-None candidate feeds normalize() ---
    day = {"date": ds}
    candidates = {
        "sleep": [("sleep (gc dailySleepData)", f"/wellness-service/wellness/dailySleepData/{dn}",
                   {"date": ds, "nonSleepBufferMinutes": 60}) if dn else None,
                  ("sleep (personal dailySleep)", "/sleep-service/sleep/dailySleep", {"date": ds})],
        "hrv": [("hrv", f"/hrv-service/hrv/{ds}", None)],
        "readiness": [("training readiness", f"/metrics-service/metrics/trainingreadiness/{ds}", None)],
        "stats": [("daily stats (gc usersummary)", f"/usersummary-service/usersummary/daily/{dn}",
                   {"calendarDate": ds}) if dn else None,
                  ("daily stats (personal v2/dailysummaries)", "/usersummary-service/v2/dailysummaries",
                   {"startDate": ds, "endDate": ds})],
        "activities": [("activities", "/activitylist-service/activities/search/activities",
                        {"startDate": ds, "endDate": ds, "start": 0, "limit": 100})],
    }
    sources = {}
    for key, cands in candidates.items():
        for c in filter(None, cands):
            payload = pr.get(c[0].replace(" ", "_").replace("/", "-"), c[1], c[2])
            if payload is not None and key not in day:
                day[key] = payload
                sources[key] = c[0]
        day.setdefault(key, {"_error": "not available"})

    # --- what fetch_fitness_scores() uses ---
    scores = {"date": ds}
    for key, name, path, params in [
        ("hill_score", "hill score", "/metrics-service/metrics/hillscore/stats",
         {"startDate": start, "endDate": ds, "aggregation": "daily"}),
        ("endurance_score", "endurance score", "/metrics-service/metrics/endurancescore/stats",
         {"startDate": start, "endDate": ds, "aggregation": "weekly"}),
        ("training_status", "training status", f"/metrics-service/metrics/trainingstatus/aggregated/{ds}", None),
        ("lactate_threshold", "lactate threshold", "/biometric-service/biometric/latestLactateThreshold", None),
        ("running_tolerance", "running tolerance", "/metrics-service/metrics/runningtolerance/stats",
         {"startDate": week_ago, "endDate": ds, "aggregation": "daily"}),
        ("max_metrics", "max metrics (VO2max)", f"/metrics-service/metrics/maxmet/daily/{ds}/{ds}", None),
        ("race_predictions", "race predictions",
         f"/metrics-service/metrics/racepredictions/latest/{dn}" if dn else None, None),
    ]:
        payload = pr.get(name.replace(" ", "_"), path, params) if path else None
        scores[key] = payload if payload is not None else {"_error": "not available"}
        if payload is not None:
            sources[key] = name

    # --- other Personal API endpoints worth knowing about ---
    pr.get("personal_sleep_summaries", "/sleep-service/sleep/dailySleepSummariesByDate",
           {"startDate": week_ago, "endDate": ds})
    pr.get("personal_hrv_range", f"/hrv-service/hrv/daily/{week_ago}/{ds}")
    pr.get("personal_stress_daily", f"/usersummary-service/stats/stress/daily/{week_ago}/{ds}")
    pr.get("personal_body_battery", "/wellness-service/wellness/bodyBattery/reports/daily",
           {"startDate": week_ago, "endDate": ds})
    acts = day.get("activities")
    if isinstance(acts, list) and acts:
        aid = acts[0].get("activityId")
        pr.get("activity_detail", f"/activity-service/activity/{aid}")
        pr.get("activity_fit", f"/download-service/files/activity/{aid}", binary=True)

    return day, scores, sources


def stored_values(d: date):
    """The garminconnect-era normalized values for the same date, if the DB has them."""
    db = ROOT / "data" / "coach.db"
    if not db.exists():
        return None, None, None
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT raw_json FROM daily_metrics WHERE date=?", (d.isoformat(),)).fetchone()
        old_m = garmin.normalize(json.loads(r["raw_json"])) if r and r["raw_json"] else None
        old_a = [json.loads(x["raw_json"]) for x in
                 con.execute("SELECT raw_json FROM activities WHERE date=?", (d.isoformat(),))]
        r = con.execute("SELECT raw_json FROM fitness_scores WHERE date<=? ORDER BY date DESC LIMIT 1",
                        (d.isoformat(),)).fetchone()
        old_s = garmin.normalize_scores(json.loads(r["raw_json"])) if r and r["raw_json"] else None
        return old_m, old_a, old_s
    except Exception as e:
        print(f"(could not read data/coach.db for comparison: {e})")
        return None, None, None
    finally:
        con.close()


def column_rows(new: dict, old, source_of):
    rows = []
    for col, v in new.items():
        if col in ("date", "raw_json"):
            continue
        ov = old.get(col) if old else None
        rows.append((col, v, ov, source_of(col)))
    return rows


def _fmt(v):
    if v is None:
        return "—"
    s = str(v)
    return s if len(s) <= 28 else s[:25] + "..."


def table(title, rows, with_old):
    head = "| column | personal API | " + ("garminconnect (db) | " if with_old else "") + "from |"
    sep = "|---|---|" + ("---|" if with_old else "") + "---|"
    lines = [f"### {title}", "", head, sep]
    for col, v, ov, src in rows:
        mark = "✅" if v is not None else ("❌" if (ov is not None or not with_old) else "·")
        lines.append(f"| {mark} {col} | {_fmt(v)} | " + (f"{_fmt(ov)} | " if with_old else "")
                     + f"{src or ''} |")
    got = sum(1 for r in rows if r[1] is not None)
    lines += ["", f"{got} of {len(rows)} columns filled.", ""]
    return lines


METRIC_SOURCE = {  # which fetch_day key each daily_metrics column comes from
    "sleep": ("sleep_", "deep_", "rem_", "light_", "awake_", "avg_sleep_stress"),
    "hrv": ("hrv_",),
    "readiness": ("training_readiness", "readiness_", "recovery_time", "acute_load", "acwr"),
    "stats": ("resting_hr", "rhr_", "body_battery", "stress_avg", "waking_resp"),
}


def metric_source(sources):
    def f(col):
        for key, prefixes in METRIC_SOURCE.items():
            if col.startswith(prefixes):
                return sources.get(key)
        return None
    return f


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console is cp1252

    key = os.environ.get("GARMIN_API_KEY")
    if not key:
        sys.exit("Set GARMIN_API_KEY in .env first "
                 "(create one at https://connect.garmin.com/app/settings/personalApiToken).")

    d = args.date
    outdir = OUT / d.isoformat()
    outdir.mkdir(parents=True, exist_ok=True)

    print("Exchanging API key for a bearer token ...")
    token = obtain_token(key)
    claims = jwt_claims(token)
    exp = datetime.fromtimestamp(claims["exp"]).isoformat(" ") if "exp" in claims else "?"
    scopes = claims.get("scope") or claims.get("scopes") or claims.get("authorities") or "?"
    print(f"  token expires {exp}; scopes: {scopes}")
    (outdir / "token_claims.json").write_text(json.dumps(claims, indent=2), encoding="utf-8")

    pr = Probe(token, outdir)
    prof = pr.get("social_profile", "/userprofile-service/socialProfile")
    display_name = (prof or {}).get("displayName") or claims.get("displayName") \
        or claims.get("user_name") or claims.get("userName")
    print(f"  display name: {display_name or 'NOT FOUND (display-name endpoints skipped)'}")

    print(f"Probing endpoints for {d} (paced, {PACE_S:.0f}s per call) ...")
    day, scores, sources = probe_all(pr, d, display_name)

    old_m, old_a, old_s = stored_values(d)
    with_old = any(x is not None for x in (old_m, old_a, old_s))

    new_m = garmin.normalize(day)
    new_a = garmin.normalize_activities(day)
    new_s = garmin.normalize_scores(scores)

    lines = [f"# Garmin Personal API coverage — {d}", "",
             f"Probed {datetime.now():%Y-%m-%d %H:%M}. Token expires {exp}. Scopes: `{scopes}`. "
             f"Data host that answered: `{pr.host or 'none'}`.", "",
             "## Endpoints", "", "| | endpoint | path | status | top-level keys |", "|---|---|---|---|---|"]
    for r in pr.results:
        lines.append(f"| {'✅' if r['ok'] else '❌'} | {r['name']} | `{r['path']}` | {r['status']} "
                     f"| {r['shape']} |")
    lines += ["", "## Columns the coach stores", "",
              "✅ filled from the Personal API · ❌ empty (but garminconnect had it, or no db to compare)"
              " · `·` empty in both", ""]
    lines += table("daily_metrics", column_rows(new_m, old_m, metric_source(sources)), with_old)
    a_new = new_a[0] if new_a else {k: None for k in garmin.normalize_activities(
        {"date": d.isoformat(), "activities": old_a or [{}]})[0]}
    a_old = garmin.normalize_activities({"date": d.isoformat(), "activities": old_a})[0] if old_a else None
    lines += table(f"activities (first of {len(new_a)} via API, {len(old_a or [])} in db)",
                   column_rows(a_new, a_old, lambda c: sources.get("activities")), with_old)
    score_src = {"hill_": "hill_score", "endurance": "endurance_score", "vo2": "training_status",
                 "training_status": "training_status", "acute": "training_status",
                 "chronic": "training_status", "acwr": "training_status", "load_ratio": "training_status",
                 "lactate": "lactate_threshold", "running_tol": "running_tolerance",
                 "heat": "training_status"}
    lines += table("fitness_scores (weekly)", column_rows(
        new_s, old_s, lambda c: next((sources.get(v) for k, v in score_src.items()
                                      if c.startswith(k)), None)), with_old)

    report = OUT / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nReport: {report}\nRaw responses: {outdir}")


if __name__ == "__main__":
    main()
