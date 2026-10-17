"""Loads non-secret config (config.yaml) and secrets (.env)."""
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# Every text file in this project is UTF-8. On Windows the default encoding is
# cp1252, which mis-reads "—"/"ç" in plan.json and principles and can crash a
# print() under Task Scheduler. Read explicitly and make stdout tolerant.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

with open(ROOT / "config.yaml", "r", encoding="utf-8") as _f:
    CONFIG = yaml.safe_load(_f)


def env(key: str, default=None):
    return os.environ.get(key, default)


def project_path(rel: str) -> str:
    """Resolve a path from config (relative to the project root) to absolute."""
    return str(ROOT / rel)
