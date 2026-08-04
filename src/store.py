"""SQLite persistence -- the coach's memory.

Migrations are additive and idempotent: init() creates anything missing and
ALTERs in any new column it doesn't find. Existing history is never rewritten,
and raw_json is always kept so new fields can be backfilled from stored
payloads without re-hitting Garmin.
"""
import sqlite3
from datetime import date, datetime, timedelta

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
CREATE TABLE IF NOT EXISTS fitness_scores (
  date TEXT PRIMARY KEY,
  hill_score REAL, hill_strength REAL, hill_endurance REAL,
  endurance_score REAL, vo2max REAL,
  training_status TEXT,
  acute_load REAL, chronic_load REAL, acwr REAL, load_ratio_feedback TEXT,
  lactate_threshold_bpm REAL, lactate_threshold_speed REAL,
  running_tolerance REAL, heat_acclimation REAL,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS manual_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT,
  received_at TEXT,
  kind TEXT,             -- strength | feet | soreness | rpe | note
  value TEXT,
  text TEXT,
  update_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- Records that we asked Garmin for a given day's ACTIVITIES, and when.
-- A metrics row is not proof: the daily job runs at 06:30 and stores that
-- morning's sleep long before the day's training exists. Only a fetch made on
-- a LATER calendar day can be trusted to have seen the whole day.
CREATE TABLE IF NOT EXISTS activity_fetch (
  date TEXT PRIMARY KEY,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE INDEX IF NOT EXISTS idx_manual_notes_date ON manual_notes(date);
"""

# Columns added after the first release. (table, column, type)
_MIGRATIONS = [
    ("daily_metrics", "sleep_need_min", "INTEGER"),
    ("daily_metrics", "avg_sleep_stress", "REAL"),
    ("daily_metrics", "awake_count", "INTEGER"),
    ("daily_metrics", "sleep_respiration", "REAL"),
    ("daily_metrics", "deep_pct", "INTEGER"),
    ("daily_metrics", "rem_pct", "INTEGER"),
    ("daily_metrics", "hrv_weekly_avg", "REAL"),
    ("daily_metrics", "hrv_baseline_low", "REAL"),
    ("daily_metrics", "hrv_baseline_high", "REAL"),
    ("daily_metrics", "hrv_status", "TEXT"),
    ("daily_metrics", "rhr_7d_avg", "INTEGER"),
    ("daily_metrics", "body_battery_wake", "INTEGER"),
    ("daily_metrics", "waking_respiration", "REAL"),
    ("daily_metrics", "readiness_level", "TEXT"),
    ("daily_metrics", "readiness_feedback", "TEXT"),
    ("daily_metrics", "recovery_time_min", "INTEGER"),
    ("daily_metrics", "acute_load", "REAL"),
    ("daily_metrics", "acwr_pct", "REAL"),
    ("activities", "name", "TEXT"),
    ("activities", "start_local", "TEXT"),
    ("activities", "moving_min", "REAL"),
    ("activities", "elevation_gain_m", "REAL"),
    ("activities", "elevation_loss_m", "REAL"),
    ("activities", "max_elevation_m", "REAL"),
    ("activities", "z1_s", "REAL"), ("activities", "z2_s", "REAL"),
    ("activities", "z3_s", "REAL"), ("activities", "z4_s", "REAL"),
    ("activities", "z5_s", "REAL"),
    ("activities", "avg_speed_mps", "REAL"),
    ("activities", "gap_speed_mps", "REAL"),
    ("activities", "avg_power", "REAL"),
    ("activities", "norm_power", "REAL"),
    ("activities", "cadence", "REAL"),
    ("activities", "ground_contact_ms", "REAL"),
    ("activities", "stride_length_cm", "REAL"),
    ("activities", "vertical_osc_cm", "REAL"),
    ("activities", "max_temp_c", "REAL"),
    ("activities", "min_temp_c", "REAL"),
    ("activities", "te_label", "TEXT"),
    ("activities", "body_battery_delta", "REAL"),
    ("activities", "calories", "REAL"),
    ("activities", "location", "TEXT"),
    ("recommendations", "session_detail", "TEXT"),
    ("recommendations", "phase_id", "TEXT"),
    ("weekly_reviews", "week_end", "TEXT"),
    ("weekly_reviews", "metrics_json", "TEXT"),
]

_METRIC_COLS = [
    "date", "sleep_score", "sleep_duration_min", "deep_min", "rem_min",
    "light_min", "awake_min", "sleep_need_min", "avg_sleep_stress",
    "awake_count", "sleep_respiration", "deep_pct", "rem_pct",
    "hrv_overnight", "hrv_weekly_avg", "hrv_baseline_low", "hrv_baseline_high",
    "hrv_status", "resting_hr", "rhr_7d_avg", "body_battery_low",
    "body_battery_high", "body_battery_wake", "stress_avg",
    "waking_respiration", "training_readiness", "readiness_level",
    "readiness_feedback", "recovery_time_min", "acute_load", "acwr_pct",
    "raw_json",
]
_ACTIVITY_COLS = [
    "id", "date", "type", "name", "start_local", "duration_min", "moving_min",
    "distance_km", "elevation_gain_m", "elevation_loss_m", "max_elevation_m",
    "z1_s", "z2_s", "z3_s", "z4_s", "z5_s", "avg_hr", "max_hr",
    "avg_speed_mps", "gap_speed_mps", "avg_power", "norm_power", "cadence",
    "ground_contact_ms", "stride_length_cm", "vertical_osc_cm",
    "max_temp_c", "min_temp_c", "training_load", "aerobic_te", "anaerobic_te",
    "te_label", "body_battery_delta", "calories", "location", "raw_json",
]
_SCORE_COLS = [
    "date", "hill_score", "hill_strength", "hill_endurance", "endurance_score",
    "vo2max", "training_status", "acute_load", "chronic_load", "acwr",
    "load_ratio_feedback", "lactate_threshold_bpm", "lactate_threshold_speed",
    "running_tolerance", "heat_acclimation", "raw_json",
]


def _now():
    return datetime.now().isoformat()


class Store:
    def __init__(self, path: str):
        self.path = str(path)

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
        """Create tables and apply additive column migrations."""
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, col, coltype in _MIGRATIONS:
                existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if col not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            # Seed the fetch log from history: any date that already has
            # activity rows was demonstrably fetched. Dates with neither rows
            # nor a marker stay unknown and get re-fetched once.
            c.execute(
                "INSERT OR IGNORE INTO activity_fetch (date, fetched_at) "
                "SELECT DISTINCT date, '' FROM activities"
            )

    # ---------- writes ----------

    def _upsert(self, table, cols, row):
        vals = [row.get(k) for k in cols]
        q = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
             f"VALUES ({','.join(['?'] * len(cols))})")
        with self._conn() as c:
            c.execute(q, vals)

    def upsert_metrics(self, m: dict):
        self._upsert("daily_metrics", _METRIC_COLS, m)

    def upsert_activity(self, a: dict):
        if not a.get("id") or a["id"] == "None":
            return
        self._upsert("activities", _ACTIVITY_COLS, a)

    def upsert_scores(self, s: dict):
        self._upsert("fitness_scores", _SCORE_COLS, s)

    def add_recommendation(self, d, kind, plan_week, session_type, text, model,
                           session_detail=None, phase_id=None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO recommendations "
                "(date, kind, plan_week, session_type, session_detail, phase_id, "
                " recommendation_text, model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (d, kind, plan_week, session_type, session_detail, phase_id,
                 text, model, _now()),
            )

    def add_plan_version(self, effective_date, plan_json, change_summary):
        with self._conn() as c:
            c.execute(
                "INSERT INTO plan_versions (effective_date, plan_json, change_summary, created_at) "
                "VALUES (?,?,?,?)",
                (effective_date, plan_json, change_summary, _now()),
            )

    def add_weekly_review(self, week_start, week_end, adherence, fatigue,
                          changes, review_text, metrics_json=None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO weekly_reviews "
                "(week_start, week_end, adherence_summary, fatigue_trend, "
                " changes_made, review_text, metrics_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (week_start, week_end, adherence, fatigue, changes,
                 review_text, metrics_json, _now()),
            )

    def add_note(self, d, kind, value, text, update_id=None):
        """Ignores duplicates by Telegram update_id, so polling is idempotent."""
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO manual_notes "
                "(date, received_at, kind, value, text, update_id) VALUES (?,?,?,?,?,?)",
                (d, _now(), kind, value, text, update_id),
            )

    # ---------- key/value state ----------

    def get_state(self, key, default=None):
        with self._conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def set_state(self, key, value):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                      (key, str(value)))

    # ---------- reads ----------

    def recent_metrics(self, days: int, end: date = None):
        end = end or date.today()
        since = (end - timedelta(days=days - 1)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
                (since, end.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    def metrics_between(self, start: str, end: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def activities_between(self, start: str, end: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date, start_local",
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

    def notes_between(self, start: str, end: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM manual_notes WHERE date >= ? AND date <= ? ORDER BY date, id",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_scores(self, before: str = None, limit: int = 2):
        q = "SELECT * FROM fitness_scores"
        args = ()
        if before:
            q += " WHERE date <= ?"
            args = (before,)
        q += " ORDER BY date DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def daily_load(self, start: str, end: str) -> dict:
        """{date: summed training_load} -- the input to ACWR."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT date, SUM(COALESCE(training_load,0)) AS load FROM activities "
                "WHERE date >= ? AND date <= ? GROUP BY date", (start, end),
            ).fetchall()
        return {r["date"]: r["load"] for r in rows}

    def mark_fetched(self, d: str):
        """Record that this day's activities were fetched, with the timestamp."""
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO activity_fetch (date, fetched_at) "
                      "VALUES (?,?)", (d, _now()))

    def missing_activity_dates(self, start: date, end: date) -> list:
        """Dates in [start, end] whose activities we cannot vouch for.

        A day counts as covered only if it was fetched on a LATER calendar day.
        Fetching 2026-07-31 at 06:30 on 2026-07-31 tells us nothing about the
        evening's training -- which is precisely how 31 July's run went missing
        while its metrics row made the day look complete.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT date, fetched_at FROM activity_fetch WHERE date >= ? AND date <= ?",
                (start.isoformat(), end.isoformat())).fetchall()
        covered = set()
        for r in rows:
            fetched_on = (r["fetched_at"] or "")[:10]
            # An empty timestamp is a seeded legacy row: it has activities, so
            # the day was genuinely observed.
            if not r["fetched_at"] or fetched_on > r["date"]:
                covered.add(r["date"])

        out, cur = [], start
        while cur <= end:
            if cur.isoformat() not in covered:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def data_coverage(self, start: str, end: str) -> int:
        """How many days in the window have a metrics row (for ACWR validity)."""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM daily_metrics WHERE date >= ? AND date <= ?",
                (start, end)).fetchone()[0]
