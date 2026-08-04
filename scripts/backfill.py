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

Usage:
    python scripts/backfill.py --reparse
    python scripts/backfill.py --fetch 60
    python scripts/backfill.py --reparse --fetch 60 --scores
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG, project_path  # noqa: E402
from src import garmin, store  # noqa: E402


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
    return days, acts


def fetch(db, days: int, with_scores: bool) -> list:
    end = date.today()
    start = end - timedelta(days=days)
    missing = db.missing_activity_dates(start, end)
    if not missing:
        print("No missing days in range.")
        if not with_scores:
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

    if with_scores:
        try:
            db.upsert_scores(garmin.normalize_scores(
                garmin.fetch_fitness_scores(g, end)))
            print("  fetched fitness scores")
        except Exception as e:
            print(f"  scores failed: {e}")
    return filled


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reparse", action="store_true",
                    help="re-extract new columns from stored raw_json (no network)")
    ap.add_argument("--fetch", type=int, metavar="DAYS",
                    help="fetch missing days from the last DAYS days")
    ap.add_argument("--scores", action="store_true",
                    help="also fetch hill/endurance/threshold scores")
    args = ap.parse_args()

    if not (args.reparse or args.fetch or args.scores):
        ap.print_help()
        return

    db = store.Store(project_path(CONFIG["paths"]["db"]))
    db.init()
    print(f"Database: {project_path(CONFIG['paths']['db'])}")

    if args.reparse:
        print("Re-parsing stored payloads...")
        d, a = reparse(db)
        print(f"  updated {d} metric days and {a} activities")

    if args.fetch or args.scores:
        print(f"Fetching missing days (last {args.fetch or 0})...")
        filled = fetch(db, args.fetch or 0, args.scores)
        print(f"  recovered {len(filled)} day(s)")


if __name__ == "__main__":
    main()
