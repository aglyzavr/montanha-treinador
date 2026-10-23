#!/usr/bin/env python3
"""
Check the Garmin Personal API key in .env, once, after creating or renewing it.

    python scripts/setup_garmin_key.py

1. Create a key at https://connect.garmin.com/app/settings/personalApiToken
   (choose 1 year). It is shown only once -- copy it.
2. Put it in .env as GARMIN_API_KEY=...
3. Run this. It exchanges the key for a token and fetches last night's sleep.

There is no login, password or MFA any more. Garmin emails you about a week
before the key expires; create a new one and replace it in .env.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src import garmin  # noqa: E402


def main():
    print("Exchanging GARMIN_API_KEY for a token ...")
    try:
        g = garmin.connect()
    except garmin.GarminAuthError as e:
        sys.exit(str(e))
    print("Key OK.")

    # Smoke test.
    d = date.today()
    m = garmin.normalize(garmin.fetch_day(g, d))
    if m["sleep_duration_min"]:
        print(f"Smoke test OK -- last night: {m['sleep_duration_min']} min sleep, "
              f"score {m['sleep_score']}, HRV {m['hrv_overnight']}, RHR {m['resting_hr']}")
    else:
        y = garmin.normalize(garmin.fetch_day(g, d - timedelta(days=1)))
        print("Smoke test OK -- no sleep for today yet (sync the watch); "
              f"yesterday: {y['sleep_duration_min']} min, HRV {y['hrv_overnight']}")
    print("Done. You can now run:  python -m src.daily")


if __name__ == "__main__":
    main()
