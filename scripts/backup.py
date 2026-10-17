#!/usr/bin/env python3
"""
Copy the coach's private data to the backup SSD. Nothing goes anywhere else.

    python scripts/backup.py              # find the SSD and back up
    python scripts/backup.py --init E:\\   # first time: mark drive E: as the backup drive
    python scripts/backup.py --list       # show snapshots on the SSD

The SSD is recognised by a folder called `montanha-backup` (backup.folder_name
in config.yaml) at its root, so it works whatever drive letter Windows assigns.

Each run makes a dated snapshot folder with:
  * coach.db         -- copied with SQLite's online-backup API, so it is
                        consistent even if a job happens to be writing
  * every other file in data/ (plan.json, its .bak versions, principles,
    strength reference)

What is deliberately NOT copied: .env (passwords, bot token) and the Garmin
login token. Keep the .env values in your password manager instead; the
Garmin token is recreated with scripts/setup_garmin_login.py.

To restore on a new machine: copy the contents of the newest snapshot folder
into data/.
"""
import argparse
import shutil
import sqlite3
import string
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import CONFIG, project_path  # noqa: E402
from src import backup_status  # noqa: E402

DATA = ROOT / "data"


def _folder_name() -> str:
    return (CONFIG.get("backup") or {}).get("folder_name", "montanha-backup")


def find_backup_dirs():
    """Every mounted drive with the marker folder at its root."""
    name = _folder_name()
    if sys.platform == "win32":
        roots = [Path(f"{d}:\\") for d in string.ascii_uppercase if d not in "AB"]
    else:  # macOS / Linux: removable media mount points
        roots = [p for base in ("/Volumes", "/media", "/mnt", "/run/media")
                 for pattern in ("*", "*/*") for p in Path(base).glob(pattern)
                 if p.is_dir()]
    found = []
    for r in roots:
        try:
            cand = r / name
            if cand.is_dir():
                found.append(cand)
        except OSError:
            continue           # empty card reader, disconnected network drive
    return found


def snapshot(dest_root: Path, keep: int) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = dest_root / stamp
    dest.mkdir(parents=True)
    copied = 0
    db_path = Path(project_path(CONFIG["paths"]["db"]))
    if db_path.exists():
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(dest / db_path.name))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        copied += 1
    for f in sorted(DATA.iterdir()):
        if not f.is_file() or f.name == db_path.name or f.name.startswith("coach.db-"):
            continue           # db done above; skip SQLite -wal/-shm side files
        shutil.copy2(f, dest / f.name)
        copied += 1
    if copied == 0:
        dest.rmdir()
        sys.exit("Nothing to back up: data/ is empty.")

    snaps = sorted(p for p in dest_root.iterdir() if p.is_dir() and p.name[:4].isdigit())
    for old in snaps[:-keep] if keep and len(snaps) > keep else []:
        shutil.rmtree(old)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", metavar="DRIVE", help=r"mark a drive as the backup SSD, e.g. E:\ ")
    ap.add_argument("--list", action="store_true", help="list snapshots on the SSD")
    args = ap.parse_args()

    if args.init:
        target = Path(args.init) / _folder_name()
        target.mkdir(exist_ok=True)
        print(f"Marked backup drive: {target}")

    dirs = find_backup_dirs()
    if not dirs:
        print(f"Backup SSD not found. Plug it in. First time? Run:\n"
              f"  python scripts/backup.py --init E:\\   (use the SSD's drive letter)")
        sys.exit(1)
    if len(dirs) > 1:
        sys.exit("More than one drive has a '{}' folder: {}. Remove the extra one."
                 .format(_folder_name(), ", ".join(map(str, dirs))))
    root = dirs[0]

    if args.list:
        for p in sorted(root.iterdir()):
            if p.is_dir():
                print(f"  {p.name}  ({len(list(p.iterdir()))} files)")
        return

    keep = (CONFIG.get("backup") or {}).get("keep", 30)
    dest = snapshot(root, keep)
    backup_status.mark()
    files = sorted(p.name for p in dest.iterdir())
    print(f"Backed up {len(files)} file(s) to {dest}:")
    for name in files:
        print(f"  {name}")
    print("Safe to unplug the SSD (eject it first in Windows).")


if __name__ == "__main__":
    main()
