"""When was the data last copied to the backup SSD?

The SSD is only plugged in now and then, so backups can't be automatic. What
can be automatic is noticing they've lapsed: data/last_backup.txt is written by
scripts/backup.py, and the daily message adds one line once it is older than
backup.remind_after_days. The Mac setup had no backup at all, which is how
plan.json was lost -- this exists so that doesn't happen silently again.
"""
from datetime import datetime
from pathlib import Path

from .config import project_path

STAMP = "data/last_backup.txt"


def last_backup(path: str = None):
    p = Path(path or project_path(STAMP))
    try:
        return datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def mark(when: datetime = None, path: str = None):
    p = Path(path or project_path(STAMP))
    p.write_text((when or datetime.now()).isoformat(timespec="seconds"), encoding="utf-8")


def reminder(cfg: dict, now: datetime = None, last=...) -> str:
    """One line for the daily message, or "" when the backup is recent enough."""
    limit = (cfg.get("backup") or {}).get("remind_after_days", 14)
    if not limit:
        return ""
    if last is ...:
        last = last_backup()
    if last is None:
        return "Backup: never copied to the SSD. Plug it in and run backup.cmd."
    days = ((now or datetime.now()) - last).days
    if days < limit:
        return ""
    return f"Backup: last copy to the SSD was {days} days ago. Plug it in and run backup.cmd."
