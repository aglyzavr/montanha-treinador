"""
Morning check-in app: a small page served by this PC, for this PC only.

    python -m src.app            (or the "Montanha Coach" desktop shortcut)

Why it exists: the 06:30 schedule was never used. It ran before the watch had
synced last night's sleep, so the real workflow was always "run it by hand
later". The app makes that the designed path: open it after waking, log
yesterday in a few taps, press one button, read the call. Telegram becomes
optional ("Send to phone").

The Telegram reply codes (`s:`, `sore 3`) were never used either -- a syntax
you have to remember is a form nobody fills in. The check-in form replaces
them with taps; whatever is skipped stays unlogged (rule 4), never assumed.

Privacy: binds to 127.0.0.1 only -- not reachable from the network. No new
dependencies (stdlib http.server). The page loads nothing from the internet.
POSTs need a custom header and a localhost Host, so a web page open in another
tab can't trigger runs or messages (CSRF / DNS-rebinding guard).
"""
import json
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from .config import CONFIG, project_path
from . import store, llm, notify, metrics, plan as planmod, runtime, backup_status

PAGE = Path(__file__).with_name("app_page.html")
IDLE_EXIT_SECONDS = 4 * 3600      # nobody has used it for 4 h -> exit quietly

FEET_STATES = ("fine", "hotspot", "blister", "pain")


def _port() -> int:
    return int((CONFIG.get("app") or {}).get("port", 8765))


def _db():
    db = store.Store(project_path(CONFIG["paths"]["db"]))
    db.init()
    return db


# --------------------------------------------------------------------------
# Check-in: yesterday's training facts + this morning's legs
# --------------------------------------------------------------------------
# Dates follow what each answer is about: strength, feet and RPE describe
# YESTERDAY's session; soreness and the free note describe THIS MORNING.

def read_checkin(db, today: date) -> dict:
    y = (today - timedelta(days=1)).isoformat()
    t = today.isoformat()
    notes = [n for n in db.notes_between(y, t) if n.get("update_id") is None]
    get = lambda d, k: next((n for n in reversed(notes)
                             if n["date"] == d and n["kind"] == k), None)
    out = {"strength": None, "strength_text": "", "soreness": None,
           "feet": None, "feet_text": "", "rpe": None, "note": ""}
    s = get(y, "strength")
    if s:
        out["strength"] = "yes"
        out["strength_text"] = s["text"] if s["text"] != "strength session" else ""
    if get(y, "no_strength"):
        out["strength"] = "no"
    so = get(t, "soreness")
    out["soreness"] = int(float(so["value"])) if so and so["value"] else None
    f = get(y, "feet")
    if f:
        state, _, rest = (f["text"] or "").partition(": ")
        out["feet"] = state if state in FEET_STATES else "fine"
        out["feet_text"] = rest
    r = get(y, "rpe")
    out["rpe"] = int(float(r["value"])) if r and r["value"] else None
    n = get(t, "note")
    out["note"] = n["text"] if n else ""
    return out


def save_checkin(db, today: date, form: dict):
    y = (today - timedelta(days=1)).isoformat()
    t = today.isoformat()
    s = form.get("strength")
    detail = (form.get("strength_text") or "").strip()
    # "No" is stored as its own kind: counting it as kind=strength would make
    # inbox.summarize_notes tally a session that never happened.
    db.replace_app_note(y, "strength", "done" if s == "yes" else None,
                        (detail or "strength session") if s == "yes" else None)
    db.replace_app_note(y, "no_strength", "none" if s == "no" else None,
                        "no strength session" if s == "no" else None)
    so = form.get("soreness")
    db.replace_app_note(t, "soreness", str(int(so)) if so else None,
                        f"soreness {int(so)}/5 this morning" if so else None)
    feet = form.get("feet")
    ftxt = (form.get("feet_text") or "").strip()
    db.replace_app_note(y, "feet", None,
                        (f"{feet}: {ftxt}" if ftxt else feet) if feet in FEET_STATES else None)
    rpe = form.get("rpe")
    db.replace_app_note(y, "rpe", str(int(rpe)) if rpe else None,
                        f"session RPE {int(rpe)}/10" if rpe else None)
    note = (form.get("note") or "").strip()
    db.replace_app_note(t, "note", None, note or None)


# --------------------------------------------------------------------------
# Jobs: one at a time (two copies of the model don't fit in 32 GB)
# --------------------------------------------------------------------------

class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = {"kind": None, "state": "idle"}
        self.last = {}            # kind -> finished job (for "Send to phone")

    def snapshot(self):
        with self.lock:
            j = dict(self.current)
        if j.get("state") == "running":
            j["elapsed"] = int(time.time() - j["started"])
        return j

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.current.get("state") == "running":
                return False
            self.current = {"kind": kind, "state": "running", "step": "Starting",
                            "started": time.time()}

        def progress(step):
            with self.lock:
                self.current["step"] = step

        def work():
            with runtime.keep_awake():
                try:
                    result = fn(progress)
                    done = {"kind": kind, "state": "done", "result": result}
                except Exception as exc:
                    from .daily import SleepNotSynced
                    print(runtime.redact(traceback.format_exc()), file=sys.stderr)
                    if isinstance(exc, SleepNotSynced):
                        done = {"kind": kind, "state": "needs_sleep", "error": str(exc)}
                    else:
                        done = {"kind": kind, "state": "error",
                                "error": runtime.redact(f"{type(exc).__name__}: {exc}")[:400],
                                "hint": runtime.hint_for(exc)}
            with self.lock:
                done["finished"] = time.time()
                self.current = done
                if done["state"] == "done":
                    self.last[kind] = done
        threading.Thread(target=work, daemon=True).start()
        return True


JOBS = Jobs()


def run_daily(force: bool):
    from . import daily
    return lambda progress: daily.run(deliver=False, require_sleep=not force,
                                      progress=progress)


def run_weekly():
    from . import weekly
    return lambda progress: weekly.run(deliver=False, progress=progress)


# --------------------------------------------------------------------------
# Model warm-up: start loading ~22 GB the moment the page opens, so most of
# the load time is spent while you fill in the check-in.
# --------------------------------------------------------------------------

def ollama_state() -> dict:
    host = CONFIG["model"]["host"].rstrip("/")
    try:
        ps = requests.get(host + "/api/ps", timeout=3).json()
    except Exception:
        return {"up": False, "loaded": False}
    name = CONFIG["model"]["name"]
    loaded = any(m.get("name") in (name, name + ":latest") or m.get("model") == name
                 for m in ps.get("models", []))
    return {"up": True, "loaded": loaded}


def warm_model():
    m = CONFIG["model"]
    payload = {"model": m["name"], "keep_alive": "20m"}
    if m.get("num_ctx"):
        # Must match the real call's num_ctx, or Ollama reloads the model.
        payload["options"] = {"num_ctx": int(m["num_ctx"])}
    try:
        requests.post(m["host"].rstrip("/") + "/api/generate", json=payload, timeout=900)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Status for the page
# --------------------------------------------------------------------------

def status() -> dict:
    today = date.today()
    db = _db()
    out = {"date": today.isoformat(), "date_label": today.strftime("%A %d %B"),
           "model": CONFIG["model"]["name"], "job": JOBS.snapshot(),
           "ollama": ollama_state(), "backup": backup_status.reminder(CONFIG)}
    try:
        ctx = planmod.resolve(planmod.load(project_path(CONFIG["paths"]["plan"])), today)
        out["plan"] = {k: ctx.get(k) for k in ("plan_label", "phase_id", "phase_name",
                                               "week_in_phase", "days_to_race")}
    except Exception as e:
        out["plan"] = {"error": f"plan.json: {e}"}
    recs = db.recommendations_between(today.isoformat(), today.isoformat(), "daily")
    out["today"] = None
    if recs:
        r = recs[-1]
        out["today"] = {"message": llm.human_part(r["recommendation_text"] or ""),
                        "verdict": r.get("session_type"), "at": r.get("created_at")}
    out["checkin"] = read_checkin(db, today)
    from .daily import BACKFILL_DAYS
    out["missing_days"] = db.missing_days_count(today - timedelta(days=BACKFILL_DAYS),
                                                today - timedelta(days=2))
    ws, we = metrics.last_complete_week(today)
    out["weekly"] = {"week_start": ws.isoformat(), "week_end": we.isoformat(),
                     "label": f"{ws:%d %b} - {we:%d %b}",
                     "done": db.weekly_review_exists(ws.isoformat())}
    return out


def send_to_phone(kind: str) -> str:
    done = JOBS.last.get(kind)
    text = done["result"]["message"] if done else None
    if not text and kind == "daily":
        today = status().get("today")
        text = today and today["message"]
    if not text:
        raise RuntimeError("Nothing to send yet.")
    notify.send_telegram(text)
    return "Sent to Telegram."


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

LAST_REQUEST = [time.time()]


class Handler(BaseHTTPRequestHandler):
    server_version = "MontanhaCoach"

    def log_message(self, fmt, *args):     # keep the console/log quiet
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        LAST_REQUEST[0] = time.time()
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            try:
                self._json(status())
            except Exception as e:
                self._json({"error": runtime.redact(str(e))}, 500)
        elif self.path == "/api/job":
            self._json(JOBS.snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        LAST_REQUEST[0] = time.time()
        if not self._host_ok() or self.headers.get("X-Coach") != "1":
            return self._json({"error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad request"}, 400)
        try:
            if self.path == "/api/checkin":
                save_checkin(_db(), date.today(), body)
                self._json({"ok": True, "checkin": read_checkin(_db(), date.today())})
            elif self.path == "/api/daily":
                if body.get("checkin") is not None:
                    save_checkin(_db(), date.today(), body["checkin"])
                ok = JOBS.start("daily", run_daily(bool(body.get("force"))))
                self._json({"ok": ok, "error": None if ok else "A run is already in progress."})
            elif self.path == "/api/weekly":
                ok = JOBS.start("weekly", run_weekly())
                self._json({"ok": ok, "error": None if ok else "A run is already in progress."})
            elif self.path == "/api/send":
                self._json({"ok": True, "message": send_to_phone(body.get("kind", "daily"))})
            elif self.path == "/api/warm":
                threading.Thread(target=warm_model, daemon=True).start()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": runtime.redact(str(e)),
                        "hint": runtime.hint_for(e)}, 500)


def _already_running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _log_to_file_when_windowless():
    """The desktop shortcut uses pythonw (no console window), where stdout and
    stderr are None. Send them to logs/app.log so failures are inspectable."""
    if sys.stdout is None or sys.stderr is None:
        logs = Path(project_path("logs"))
        logs.mkdir(exist_ok=True)
        f = open(logs / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main():
    _log_to_file_when_windowless()
    port = _port()
    url = f"http://127.0.0.1:{port}/"
    if _already_running(port):
        webbrowser.open(url)            # second click on the shortcut
        return
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Montanha coach app on {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def idle_watch():
        while True:
            time.sleep(300)
            if (time.time() - LAST_REQUEST[0] > IDLE_EXIT_SECONDS
                    and JOBS.snapshot().get("state") != "running"):
                server.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
