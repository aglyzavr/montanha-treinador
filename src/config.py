"""Loads non-secret config (config.yaml) and secrets (.env)."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

with open(ROOT / "config.yaml", "r") as _f:
    CONFIG = yaml.safe_load(_f)


def env(key: str, default=None):
    return os.environ.get(key, default)


def project_path(rel: str) -> str:
    """Resolve a path from config (relative to the project root) to absolute."""
    return str(ROOT / rel)
