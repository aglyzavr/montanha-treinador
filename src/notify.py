"""Delivery: Telegram (primary) and optional email."""
import os
import smtplib
from email.message import EmailMessage

import requests


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set in .env")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=30,
    )
    r.raise_for_status()


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
