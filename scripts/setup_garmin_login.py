#!/usr/bin/env python3
"""
Run ONCE, interactively, to log in to Garmin Connect and cache a token.

    python scripts/setup_garmin_login.py

It handles MFA by prompting you for the code. After this succeeds, the daily
and weekly jobs resume from the cached token without logging in again (until
the token expires, ~1 year), which keeps you clear of Garmin's rate limits.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def get_mfa() -> str:
    return input("Enter Garmin MFA code (from email/authenticator): ").strip()


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env first.")

    tokenstore = os.path.expanduser(os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect"))

    print("Logging in to Garmin Connect ...")
    # prompt_mfa is called only if MFA is required.
    g = Garmin(email=email, password=password, prompt_mfa=get_mfa)
    g.login()

    try:
        g.garth.dump(tokenstore)
        print(f"Token cached at: {tokenstore}")
    except Exception as e:
        print(f"Warning: could not dump token ({e}). "
              f"Check the python-garminconnect README for the current token API.")

    # Smoke test.
    import datetime
    today = datetime.date.today().isoformat()
    try:
        sleep = g.get_sleep_data(today)
        print("Smoke test OK — fetched sleep payload:",
              "has data" if sleep else "empty (no sleep recorded yet today)")
    except Exception as e:
        print("Smoke test fetch failed:", e)

    print("Done. You can now run:  python -m src.daily")


if __name__ == "__main__":
    main()
