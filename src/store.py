"""SQLite persistence -- the coach's memory."""
import sqlite3
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
  date TEXT PRIMARY KEY,
  sleep_score INTEGER,
  sleep_duration_min INTEGER,
  deep_min INTEGER, rem_min INTEGER, light_min INTEGER, awake_min INTEGER,
  hrv_overnight REAL,
  resting_hr INTEGER,
  body_battery_low INTEGER, body_battery_high INTEGER,
  training_readiness INTEGER,
  stress_avg INTEGER,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS activities (
  id TEXT PRIMARY KEY,
  date TEXT,
  type TEXT,
  duration_min REAL, distance_km REAL,
  avg_hr INTEGER, max_hr INTEGER,
  training_load REAL, aerobic_te REAL, anaerobic_te REAL,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
  date TEXT, kind TEXT,
  plan_week INTEGER,
  session_type TEXT,
  recommendation_text TEXT,
  model TEXT, created_at TEXT,
  PRIMARY KEY (date, kind)
);
CREATE TABLE IF NOT EXISTS plan_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_date TEXT,
  plan_json TEXT,
  change_summary TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS weekly_reviews (
  week_start TEXT PRIMARY KEY,
  adherence_summary TEXT,
  fatigue_trend TEXT,
  changes_made TEXT,
  review_text TEXT,
  created_at TEXT
);
"""

_METRIC_COLS = [
    "date", "sleep_score", "sleep_duration_min", "deep_min", "rem_min",
    "light_min", "awake_min", "hrv_overnight", "resting_hr",
    "body_battery_low", "body_battery_high", "training_readiness",
    "stress_avg", "raw_json",
]
_ACTIVITY_COLS = [
    "id", "date", "type", "duration_min", "distance_km", "avg_hr", "max_hr",
    "training_load", "aerobic_te", "anaerobic_te", "raw_json",
]


class Store:
    def __init__(self, path: str):
        self.path = str(path)

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    def upsert_metrics(self, m: dict):
        vals = [m.get(k) for k in _METRIC_COLS]
        q = (f"INSERT OR REPLACE INTO daily_metrics ({','.join(_METRIC_COLS)}) "
             f"VALUES ({','.join(['?'] * len(_METRIC_COLS))})")
        with self._conn() as c:
            c.execute(q, vals)

    def upsert_activity(self, a: dict):
        if not a.get("id"):
            return
        vals = [a.get(k) for k in _ACTIVITY_COLS]
        q = (f"INSERT OR REPLACE INTO activities ({','.join(_ACTIVITY_COLS)}) "
             f"VALUES ({','.join(['?'] * len(_ACTIVITY_COLS))})")
        with self._conn() as c:
            c.execute(q, vals)

    def add_recommendation(self, d, kind, plan_week, session_type, text, model):
        from datetime import datetime
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO recommendations "
                "(date, kind, plan_week, session_type, recommendation_text, model, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (d, kind, plan_week, session_type, text, model, datetime.now().isoformat()),
            )

    def recent_metrics(self, days: int):
        since = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_metrics WHERE date >= ? ORDER BY date", (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def activities_between(self, start: str, end: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def recommendations_between(self, start: str, end: str, kind="daily"):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM recommendations WHERE date >= ? AND date <= ? AND kind = ? "
                "ORDER BY date", (start, end, kind),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_plan_version(self, effective_date, plan_json, change_summary):
        from datetime import datetime
        with self._conn() as c:
            c.execute(
                "INSERT INTO plan_versions (effective_date, plan_json, change_summary, created_at) "
                "VALUES (?,?,?,?)",
                (effective_date, plan_json, change_summary, datetime.now().isoformat()),
            )

    def add_weekly_review(self, week_start, adherence, fatigue, changes, review_text):
        from datetime import datetime
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO weekly_reviews "
                "(week_start, adherence_summary, fatigue_trend, changes_made, review_text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (week_start, adherence, fatigue, changes, review_text, datetime.now().isoformat()),
            )
