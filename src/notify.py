"""Delivery: Telegram (primary) and optional email.

Telegram used to be called with parse_mode="Markdown" on raw model output. Any
unbalanced `_`, `*` or backtick -- common in free prose about "Z1_easy" or
*emphasis* -- makes Telegram reject the whole message with a 400, and the day's
recommendation vanishes with no error you would ever see. Messages are now sent
as plain text, split to fit the 4096-character limit, with a retry.
"""
import os
import smtplib
import time
from email.message import EmailMessage

import requests

TELEGRAM_LIMIT = 4096


def _chunks(text: str, limit: int = TELEGRAM_LIMIT):
    """Split on paragraph, then line, then hard boundary."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            out.append(buf)
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(para[:cut])
            para = para[cut:].lstrip("\n")
        buf = para
    if buf:
        out.append(buf)
    return out


def send_telegram(text: str, retries: int = 2):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set in .env")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for part in _chunks(text):
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": part,
                          "disable_web_page_preview": True},
                    timeout=30,
                )
                r.raise_for_status()
                last = None
                break
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(2 ** attempt)
        if last:
            raise last


def send_email(subject: str, body: str):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return  # email disabled
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", "")
    msg["To"] = os.environ.get("EMAIL_TO", "")
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def send(cfg: dict, text: str, subject: str = "Montanha coach"):
    """Route a message to the enabled channels."""
    delivery = cfg.get("delivery", {})
    if delivery.get("telegram"):
        send_telegram(text)
    if delivery.get("email"):
        send_email(subject, text)
