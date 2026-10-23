"""Process-level plumbing shared by the daily and weekly jobs.

  * keep_awake(): the Windows equivalent of the Mac's `caffeinate -i`. The
    model call takes minutes on this PC; if you walk away while it thinks,
    Windows' idle timer can put the PC to sleep mid-call. Asking Windows to
    stay up for the duration of the job prevents that.
  * ollama_session(): Ollama runs only while the coach does. It used to start
    at login (Ollama's own Startup shortcut) and listen all day; now the app or
    job starts `ollama serve` itself and stops it -- model runners included --
    when it is done, even if Python crashes (Windows Job Object).
  * run_job(): (terminal path, `python -m src.daily`) turns any crash into a short Telegram message saying what broke
    and what to do. Before this, a failure anywhere except the Garmin fetch or
    the model call meant no message at all -- indistinguishable from "the PC was
    off". The alert carries the error and a fix hint, never health data.
"""
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

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


# --------------------------------------------------------------------------
# Ollama: start on demand, stop when done
# --------------------------------------------------------------------------

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def ollama_up(host: str) -> bool:
    import requests
    try:
        requests.get(host.rstrip("/") + "/api/tags", timeout=3)
        return True
    except Exception:
        return False


def find_ollama():
    found = shutil.which("ollama")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(local) if local.exists() else None


def _kill_on_close_job(proc):
    """Put proc in a Job Object that Windows kills -- with every process it
    spawns (the model runner) -- once our last handle to the job closes. That
    happens on a normal exit, and also when Python crashes or is killed from
    Task Manager, so `ollama serve` can never outlive the coach."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(info), ctypes.sizeof(info))
            or not k32.AssignProcessToJobObject(job, int(proc._handle))):
        k32.CloseHandle(job)
        return None
    return lambda: k32.CloseHandle(job)


def _unload(host: str, model: str):
    import requests
    try:
        requests.post(host.rstrip("/") + "/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


@contextmanager
def ollama_session(cfg: dict):
    """Ollama for the duration of the with-block, then gone.

    Already running (someone started it by hand) -> leave it exactly as it is.
    Otherwise start `ollama serve` bound to loopback only, and on exit unload
    the model and stop the server and its runners."""
    m = cfg["model"]
    host = m["host"]
    url = urlparse(host)
    if ollama_up(host) or url.hostname not in _LOOPBACK:
        yield
        return
    exe = find_ollama()
    if not exe:
        yield                      # the model call fails with an Ollama hint
        return
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{url.port or 11434}")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen([exe, "serve"], env=env, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    close_job = _kill_on_close_job(proc) if sys.platform == "win32" else None
    print("Started Ollama for this run", file=sys.stderr)
    try:
        for _ in range(60):
            if ollama_up(host) or proc.poll() is not None:
                break
            time.sleep(1)
        yield
    finally:
        if proc.poll() is None:
            _unload(host, m["name"])
        if close_job:
            close_job()            # kills serve + runners
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               creationflags=flags, capture_output=True)
            else:
                proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Stopped Ollama", file=sys.stderr)


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
    (r"GarminAuthError|GARMIN_API_KEY",
     "Garmin API key missing or expired. Create a new one at "
     "connect.garmin.com/app/settings/personalApiToken, put it in .env as "
     "GARMIN_API_KEY, then run `python scripts/setup_garmin_key.py`."),
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
    for key in ("TELEGRAM_TOKEN", "GARMIN_API_KEY", "SMTP_PASSWORD"):
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


def run_job(job: str, fn, *args, model: bool = True, **kwargs):
    from contextlib import nullcontext
    from .config import CONFIG
    from . import notify
    with keep_awake(), (ollama_session(CONFIG) if model else nullcontext()):
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
