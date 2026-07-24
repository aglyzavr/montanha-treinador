"""Local model calls via Ollama, plus small output parsers."""
import json
import re

import requests


def chat(cfg: dict, system: str, user: str) -> str:
    m = cfg["model"]
    payload = {
        "model": m["name"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": bool(m.get("think", False)),  # thinking/reasoning mode
        "options": {"temperature": m.get("temperature", 0.4)},
    }
    resp = requests.post(
        m["host"].rstrip("/") + "/api/chat",
        json=payload,
        timeout=m.get("timeout_seconds", 900),
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if not content:
        # Some thinking models put everything under "thinking" if content is empty.
        content = (msg.get("thinking") or "").strip()
    return content


def parse_header(text: str) -> dict:
    """Parse the 'KEY: value' lines before the first '---' separator."""
    head = text.split("---", 1)[0]
    out = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().upper()] = v.strip()
    return out


def human_part(text: str) -> str:
    """The message meant for the athlete: everything after the first '---',
    but before an '---UPDATED_PLAN---' block if present."""
    body = text.split("---", 1)[1] if "---" in text else text
    body = body.split("---UPDATED_PLAN---", 1)[0]
    return body.strip()


def extract_json_block(text: str):
    """Pull the first ```json ...``` fenced block and parse it. Returns dict or None."""
    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except Exception:
        return None
