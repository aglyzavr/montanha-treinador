#!/usr/bin/env python3
"""
Recover history and populate the new columns.

Two independent jobs:

  --reparse   Re-runs normalize() over the raw_json ALREADY in the database.
              Every new field (elevation gain/loss, HR zone seconds, cadence,
              temperature, recovery time, HRV baseline) was present in the
              stored payloads all along and simply never extracted, so this
              backfills them without a single Garmin request.

  --fetch N   Pulls any day in the last N days that has no metrics row at all.
              This is what recovers days lost to a missed daily run -- e.g.
              2026-07-31 and 2026-08-01, which vanished because the job did not
              run on the following mornings and nothing ever went back for them.

  --details N Fetches each stored activity's detail (watch RPE / feel, stamina,
              run / walk split) for the last N days, where it is missing. One
              request per activity, paced 1/s.

  --weights N Pulls Garmin weigh-ins for the last N days.

  --streams N Fetches per-second data for runs/hikes of 40+ min in the last N
              days and computes the durability summary (src/streams.py).
              --reanalyze recomputes every stored stream, e.g. after the plan's
              AeT changes. No network.

(Garmin's weekly fitness scores used to come with --scores; the Personal API
doesn't have them, so stored ones are only re-read by --reparse.)

Usage:
    python scripts/backfill.py --reparse
    python scripts/backfill.py --fetch 60
    python scripts/backfill.py --reparse --fetch 60
    python scripts/backfill.py --details 120
    python scripts/backfill.py --streams 120
    python scripts/backfill.py --reanalyze
    python scripts/backfill.py --weights 365
"""
import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG, project_path  # noqa: E402
from src import garmin, store, streams, plan as planmod  # noqa: E402


def reparse(db) -> tuple:
    """Re-normalize stored raw payloads into the new columns."""
    days = acts = 0
    with db._conn() as c:
        rows = c.execute("SELECT date, raw_json FROM daily_metrics").fetchall()
    for r in rows:
        try:
            db.upsert_metrics(garmin.normalize(json.loads(r["raw_json"])))
            days += 1
        except Exception as e:
            print(f"  metrics {r['date']}: {e}")

    with db._conn() as c:
        rows = c.execute("SELECT id, date, raw_json FROM activities").fetchall()
    for r in rows:
        try:
            payload = {"date": r["date"], "activities": [json.loads(r["raw_json"])]}
            for a in garmin.normalize_activities(payload):
                db.upsert_activity(a)
                acts += 1
        except Exception as e:
            print(f"  activity {r['id']}: {e}")

    with db._conn() as c:
        rows = c.execute("SELECT date, raw_json FROM fitness_scores").fetchall()
    for r in rows:
        try:
            db.upsert_scores(garmin.normalize_scores(json.loads(r["raw_json"])))
        except Exception as e:
            print(f"  scores {r['date']}: {e}")
    return days, acts


def fetch(db, days: int) -> list:
    end = date.today()
    start = end - timedelta(days=days)
    missing = db.missing_activity_dates(start, end)
    if not missing:
        print("No missing days in range.")
        return []

    g = garmin.connect()
    filled = []
    for d in missing:
        try:
            raw = garmin.fetch_day(g, d)
            db.upsert_metrics(garmin.normalize(raw))
            for a in garmin.normalize_activities(raw):
                db.upsert_activity(a)
            db.mark_fetched(d.isoformat())
            filled.append(d.isoformat())
            print(f"  fetched {d}")
        except Exception as e:
            print(f"  FAILED {d}: {e}")
    return filled


def fetch_details(db, days: int) -> int:
    start = (date.today() - timedelta(days=days)).isoformat()
    todo = [a for a in db.activities_between(start, date.today().isoformat())
            if "detail" not in json.loads(a["raw_json"] or "{}")]
    if not todo:
        print("Every activity in range already has its detail.")
        return 0
    print(f"  {len(todo)} activities to fetch (~{len(todo)} s)")
    g = garmin.connect()
    done = 0
    for a in todo:
        raw = json.loads(a["raw_json"])
        try:
            raw["detail"] = garmin.activity_detail(g, a["id"])
        except garmin.GarminAuthError:
            raise
        except Exception as e:
            print(f"  FAILED {a['date']} {a['id']}: {e}")
            continue
        for row in garmin.normalize_activities({"date": a["date"], "activities": [raw]}):
            db.upsert_activity(row)
        done += 1
        time.sleep(1)
    return done


WEIGHT_CHUNK_DAYS = 90


def _plan_aet():
    return streams.aet_from_plan(planmod.resolve(
        planmod.load(project_path(CONFIG["paths"]["plan"])), date.today()))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reparse", action="store_true",
                    help="re-extract new columns from stored raw_json (no network)")
    ap.add_argument("--fetch", type=int, metavar="DAYS",
                    help="fetch missing days from the last DAYS days")
    ap.add_argument("--streams", type=int, metavar="DAYS",
                    help="fetch + analyse per-second data for runs in the last DAYS days")
    ap.add_argument("--weights", type=int, metavar="DAYS",
                    help="fetch Garmin weigh-ins for the last DAYS days")
    ap.add_argument("--reanalyze", action="store_true",
                    help="recompute every stored stream with the plan's current AeT (no network)")
    ap.add_argument("--details", type=int, metavar="DAYS",
                    help="fetch activity detail (RPE, stamina, run/walk) for the last DAYS days")
    args = ap.parse_args()

    if not (args.reparse or args.fetch or args.details or args.streams or args.reanalyze
            or args.weights):
        ap.print_help()
        return

    db = store.Store(project_path(CONFIG["paths"]["db"]))
    db.init()
    print(f"Database: {project_path(CONFIG['paths']['db'])}")

    if args.reparse:
        print("Re-parsing stored payloads...")
        d, a = reparse(db)
        print(f"  updated {d} metric days and {a} activities")

    if args.fetch:
        print(f"Fetching missing days (last {args.fetch})...")
        filled = fetch(db, args.fetch)
        print(f"  recovered {len(filled)} day(s)")

    if args.details:
        print(f"Fetching activity detail (last {args.details} days)...")
        print(f"  updated {fetch_details(db, args.details)} activities")

    if args.streams:
        start = (date.today() - timedelta(days=args.streams)).isoformat()
        acts = db.activities_between(start, date.today().isoformat())
        todo = [a for a in acts if streams.wants_stream(a)
                and str(a["id"]) not in db.stream_ids()]
        print(f"Fetching per-second data for {len(todo)} runs/hikes (~{len(todo)} s)...")
        n = streams.sync(garmin.connect(), db, todo, _plan_aet(), pace_s=1)
        print(f"  analysed {n}")

    if args.weights:
        g, n, end = garmin.connect(), 0, date.today()
        while end > date.today() - timedelta(days=args.weights):
            start = max(end - timedelta(days=WEIGHT_CHUNK_DAYS - 1),
                        date.today() - timedelta(days=args.weights))
            for w in garmin.fetch_weights(g, start, end):
                db.upsert_weight(w)
                n += 1
            end = start - timedelta(days=1)
            time.sleep(1)
        print(f"Weigh-ins: stored {n}")

    if args.reanalyze:
        aet = _plan_aet()
        print(f"Re-analysing stored streams with AeT {aet}...")
        print(f"  updated {streams.reanalyze(db, aet)}")


if __name__ == "__main__":
    main()
