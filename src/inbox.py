"""
Telegram reply ingestion -- the manual data Garmin cannot give us.

Strength sessions, foot condition and subjective soreness are not in any
Garmin payload. Without them the coach was inventing them (one weekly review
claimed "strength sessions completed as prescribed" with zero evidence).

Design: pull, not push. There is no daemon and no webhook. Each scheduled run
calls getUpdates once with a stored offset, drains anything you have sent since
the last run, and files it. Telegram retains updates for 24 h, so the daily job
alone is enough to never lose a reply. Idempotent -- update_id is UNIQUE, so
re-polling cannot double-count.

You can just type naturally. Recognised prefixes:

    s / strength / gym     -> strength session   "s: squats 5x5 @ 80"
    feet / foot            -> foot durability    "feet: small hotspot L heel"
    sore / soreness 1-5    -> soreness           "sore 3"
    rpe 1-10               -> session RPE        "rpe 7"
    (anything else)        -> freeform note

Prefix a message with a date to backdate it: "2026-08-01 s: deadlifts".
"""
import os
import re
from datetime import date, datetime, timedelta

import requests

API = "https://api.telegram.org/bot{token}/{method}"

_KINDS = [
    ("strength", re.compile(r"^\s*(?:s|strength|gym|str)\b[:\-\s]*(.*)$", re.I | re.S)),
    ("feet",     re.compile(r"^\s*(?:feet|foot|feet log)\b[:\-\s]*(.*)$", re.I | re.S)),
    ("soreness", re.compile(r"^\s*(?:sore|soreness|dom+s)\b[:\-\s]*(.*)$", re.I | re.S)),
    ("rpe",      re.compile(r"^\s*(?:rpe|effort)\b[:\-\s]*(.*)$", re.I | re.S)),
    ("note",     re.compile(r"^\s*(?:n|note)\b[:\-\s]*(.*)$", re.I | re.S)),
]
_DATE_PREFIX = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s+(.*)$", re.S)
_YESTERDAY = re.compile(r"^\s*(?:yest|yesterday)\b[:\-\s]*(.*)$", re.I | re.S)


def _tz():
    try:
        from zoneinfo import ZoneInfo
        from .config import CONFIG
        return ZoneInfo(CONFIG.get("timezone", "UTC"))
    except Exception:
        return None


def parse(text: str, sent_at: datetime) -> tuple:
    """(date_iso, kind, value, cleaned_text). Never raises on odd input."""
    d = sent_at.date()
    body = text or ""

    m = _DATE_PREFIX.match(body)
    if m:
        try:
            d = date.fromisoformat(m.group(1))
            body = m.group(2)
        except ValueError:
            pass
    else:
        m = _YESTERDAY.match(body)
        if m:
            d = d - timedelta(days=1)
            body = m.group(1)

    for kind, rx in _KINDS:
        m = rx.match(body)
        if m:
            rest = (m.group(1) or "").strip()
            value = None
            num = re.search(r"\b(\d+(?:\.\d+)?)\b", rest)
            if kind in ("soreness", "rpe") and num:
                value = num.group(1)
            elif kind == "strength":
                value = "done"
            return d.isoformat(), kind, value, rest or body.strip()

    return d.isoformat(), "note", None, body.strip()


def poll(store, limit: int = 100) -> int:
    """Drain new Telegram messages into manual_notes. Returns count ingested.

    Non-fatal by contract: a Telegram outage must never take down the training
    review, so every failure path returns 0 rather than raising.
    """
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        return 0

    offset = store.get_state("tg_offset")
    params = {"limit": limit, "timeout": 0}
    if offset:
        params["offset"] = int(offset) + 1

    try:
        r = requests.get(API.format(token=token, method="getUpdates"),
                         params=params, timeout=30)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception:
        return 0

    tz = _tz()
    count, last_id = 0, None
    for u in updates:
        last_id = u.get("update_id", last_id)
        msg = u.get("message") or u.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/"):
            continue
        if chat_id and str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # ignore anyone who is not you

        sent = datetime.fromtimestamp(msg.get("date", 0), tz=tz) if msg.get("date") \
            else datetime.now(tz)
        d, kind, value, clean = parse(text, sent)
        store.add_note(d, kind, value, clean, update_id=u.get("update_id"))
        count += 1

    if last_id is not None:
        store.set_state("tg_offset", last_id)
    return count


def summarize_notes(notes: list) -> dict:
    """Tally what we actually have evidence for this week."""
    out = {"strength": 0, "feet": [], "soreness": [], "rpe": [], "notes": []}
    for n in notes:
        k = n.get("kind")
        if k == "strength":
            out["strength"] += 1
        elif k == "feet":
            out["feet"].append(f"{n['date']}: {n['text']}")
        elif k == "soreness" and n.get("value"):
            out["soreness"].append((n["date"], n["value"]))
        elif k == "rpe" and n.get("value"):
            out["rpe"].append((n["date"], n["value"]))
        else:
            out["notes"].append(f"{n['date']}: {n['text']}")
    return out


PROMPT_FOOTER = (
    "\n\nLog by replying: `s: <strength>` · `feet: <state>` · `sore 1-5` · `rpe 1-10`"
)
