"""Process-level plumbing shared by the daily and weekly jobs.

  * keep_awake(): the Windows equivalent of the Mac's `caffeinate -i`. The
    model call takes minutes on this PC; if you walk away while it thinks,
    Windows' idle timer can put the PC to sleep mid-call. Asking Windows to
    stay up for the duration of the job prevents that.
  * run_job(): (terminal path, `python -m src.daily`) turns any crash into a short Telegram message saying what broke
    and what to do. Before this, a failure anywhere except the Garmin fetch or
    the model call meant no message at all -- indistinguishable from "the PC was
    off". The alert carries the error and a fix hint, never health data.
"""
import os
import re
import sys
import traceback
from contextlib import contextmanager

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake():
    if sys.platform != "win32":
        yield
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        k32.SetThreadExecutionState(_ES_CONTINUOUS)


# (pattern in "<ExceptionType>: <message>", what to tell the athlete)
_HINTS = [
    (r"ContextOverflow", "The prompt no longer fits the model's context window. "
                         "Raise model.num_ctx in config.yaml."),
    (r"11434|ConnectionRefused|Max retries exceeded.*localhost",
     "Ollama isn't running. Start the Ollama app (or run `ollama serve`), then "
     "run `python -m src.doctor`."),
    (r"model .* not found|404 Client Error.*api/chat",
     "The model isn't installed. Run `ollama pull <model.name from config.yaml>`."),
    (r"ReadTimeout|timed out", "The model took longer than model.timeout_seconds. "
                               "Close heavy apps or raise the timeout."),
    (r"plan\.json", "data/plan.json is missing or unreadable. Restore it from the SSD backup."),
    (r"401|403|GarthHTTPError|Unauthorized|GarminConnectAuthentication",
     "Garmin login expired. Run `python scripts/setup_garmin_login.py` once."),
    (r"429|Too Many Requests", "Garmin is rate-limiting. Don't re-run; tomorrow's run will backfill."),
    (r"TELEGRAM", "Telegram settings missing in .env."),
]


def hint_for(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for pattern, hint in _HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return hint
    return "Run `python -m src.doctor` on the PC and check logs\\ for the full traceback."


def redact(text: str) -> str:
    """Error strings from `requests` include the full URL -- for Telegram that
    URL contains the bot token. Never let a secret reach a log or a message."""
    for key in ("TELEGRAM_TOKEN", "GARMIN_PASSWORD", "SMTP_PASSWORD"):
        val = os.environ.get(key)
        if val:
            text = text.replace(val, "***")
    return re.sub(r"bot\d+:[\w-]+", "bot***", text)


def failure_message(job: str, exc: BaseException) -> str:
    err = redact(f"{type(exc).__name__}: {exc}")
    if len(err) > 300:
        err = err[:300] + "..."
    return (f"Montanha coach: the {job} run failed, so there is no recommendation "
            f"this time.\n\nError: {err}\n\nFix: {hint_for(exc)}")


def run_job(job: str, fn, *args, **kwargs):
    from .config import CONFIG
    from . import notify
    with keep_awake():
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            print(redact(traceback.format_exc()), file=sys.stderr)
            if not getattr(exc, "notified", False):
                try:
                    notify.send(CONFIG, failure_message(job, exc))
                except Exception as send_exc:
                    print(redact(f"Could not send failure alert: {send_exc}"), file=sys.stderr)
            sys.exit(1)
