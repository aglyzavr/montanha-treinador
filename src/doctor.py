"""
Health check: is every piece the morning job needs in place?

    python -m src.doctor              # check everything, change nothing
    python -m src.doctor --send-test  # also send a Telegram test message

Read-only on purpose. For Garmin it makes one request: exchanging the API key
for a token, which proves the key is still valid. It never prints a secret,
only whether it is set. If Ollama isn't running (the
normal state) it is started for the model check and stopped again.
"""
import os
import sys
from datetime import date, datetime
from pathlib import Path

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
results = []


def report(status, what, detail=""):
    results.append(status)
    print(f"  [{status}] {what}" + (f" -- {detail}" if detail else ""))


def check_python():
    v = sys.version_info
    report(OK if v >= (3, 12) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}")
    for mod in ("requests", "dotenv", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            report(FAIL, f"package {mod}", "missing -- run setup.ps1 (pip install -e .)")
            return
    report(OK, "Python packages", "requests, python-dotenv, PyYAML")
    # Windows has no system time-zone database; without tzdata the configured
    # timezone silently falls back and Telegram notes can land on the wrong day.
    try:
        from zoneinfo import ZoneInfo
        from .config import CONFIG
        ZoneInfo(CONFIG.get("timezone", "UTC"))
    except Exception as e:
        report(FAIL, "time zone", f"{e} -- run setup.ps1 (pip install -e .)")


def check_env(root: Path):
    if not (root / ".env").exists():
        report(FAIL, ".env", "missing -- copy .env.example to .env and fill it in")
        return
    for key in ("GARMIN_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        val = os.environ.get(key, "")
        placeholder = (not val or "example.com" in val or val.startswith("your-")
                       or val in ("123456789", "123456789:ABCdef_your_bot_token_here"))
        report(FAIL if placeholder else OK, f".env {key}", "not set" if placeholder else "set")


COVERAGE_WEEKS = 16


def plan_coverage_problems(plan_doc, start: date, weeks: int = COVERAGE_WEEKS) -> list:
    """Days ahead with nothing planned, and weeks with no strength slot.

    Winter Base Phase B was written without a day pattern: every day resolved
    to "nothing planned", which meant 13 weeks with no strength and nothing
    anywhere to say so. A week without strength is fine when a sub-phase spells
    out its own days (recovery, race week) -- that was a decision, not a gap.
    """
    from datetime import timedelta
    from . import plan as planmod
    problems = []
    first_monday = start - timedelta(days=start.weekday())
    for w in range(weeks):
        monday = first_monday + timedelta(weeks=w)
        ctxs = [planmod.resolve(plan_doc, monday + timedelta(days=i)) for i in range(7)]
        ctxs = [c for c in ctxs if c["date"] >= start.isoformat()]
        empty = [c["date"] for c in ctxs if not c.get("planned_today")]
        if empty:
            problems.append(f"no session planned on {empty[0]}"
                            + (f" (+{len(empty) - 1} more that week)" if len(empty) > 1 else ""))
            continue
        inherited = next((c for c in ctxs if c.get("day_pattern_inherited_from")), None)
        if inherited:
            problems.append(f"week of {monday}: {inherited['plan_id']} has no day pattern, "
                            f"borrowing {inherited['day_pattern_inherited_from']}'s")
        if (not any(c.get("strength_today") for c in ctxs)
                and not any(c.get("sub_phase_has_pattern") for c in ctxs)):
            problems.append(f"week of {monday}: no strength session planned")
    return problems


def check_plan_coverage(plan_doc, today: date):
    problems = plan_coverage_problems(plan_doc, today)
    if problems:
        report(WARN, f"plan coverage, next {COVERAGE_WEEKS} weeks",
               f"{problems[0]}" + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""))
        for p in problems[1:6]:
            print(f"         {p}")
    else:
        report(OK, f"plan coverage, next {COVERAGE_WEEKS} weeks",
               "every day has a session, every regular week has strength")


def check_data(cfg, project_path):
    paths = cfg["paths"]
    plan_file = Path(project_path(paths["plan"]))
    if not plan_file.exists():
        report(FAIL, "data/plan.json", "missing -- restore it from the SSD backup; "
               "nothing can be prescribed without it")
    else:
        from . import plan as planmod
        try:
            ctx = planmod.resolve(planmod.load(str(plan_file)), date.today())
            phase = ctx.get("phase_id") or "none"
            report(OK if ctx.get("phase_id") else WARN, "data/plan.json",
                   f"today resolves to phase {phase}"
                   + ("" if ctx.get("phase_id") else " -- today is outside every plan window"))
        except Exception as e:
            report(FAIL, "data/plan.json", f"unreadable: {e}")
        else:
            check_plan_coverage(planmod.load(str(plan_file)), date.today())
    for key, required in (("principles", True), ("strength_reference", False)):
        p = paths.get(key)
        if p:
            exists = Path(project_path(p)).exists()
            report(OK if exists else (FAIL if required else WARN), p,
                   "present" if exists else "missing")
    db = Path(project_path(paths["db"]))
    if db.exists():
        import sqlite3
        con = sqlite3.connect(str(db))
        try:
            first, last, n = con.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM daily_metrics").fetchone()
            report(OK, "data/coach.db", f"{n} days of metrics, {first} .. {last}")
        except Exception as e:
            report(WARN, "data/coach.db", f"present but unreadable: {e}")
        finally:
            con.close()
    else:
        report(WARN, "data/coach.db", "not there yet -- restore it from the SSD backup, or it is "
               "created empty on the first run (then: python scripts/backfill.py --fetch 120)")


def check_garmin_key():
    from . import garmin
    try:
        garmin.connect()
    except garmin.GarminAuthError as e:
        report(FAIL, "Garmin API key", str(e))
    except Exception as e:
        report(WARN, "Garmin API key", f"could not reach Garmin: {type(e).__name__}")
    else:
        report(OK, "Garmin API key", "accepted (Garmin emails you a week before it expires)")


def check_ollama(cfg):
    """Ollama is normally NOT running (the app starts it on demand), so start
    it for the check the same way and stop it again afterwards."""
    from . import runtime
    exe = runtime.find_ollama()
    if not exe and not runtime.ollama_up(cfg["model"]["host"]):
        report(FAIL, "Ollama", "not installed -- run setup.ps1")
        return
    was_up = runtime.ollama_up(cfg["model"]["host"])
    with runtime.ollama_session(cfg):
        _check_ollama_running(cfg, was_up)


def _check_ollama_running(cfg, was_up):
    import requests
    m = cfg["model"]
    host = m["host"].rstrip("/")
    try:
        tags = requests.get(host + "/api/tags", timeout=5).json()
    except Exception:
        report(FAIL, "Ollama", f"installed but would not start at {host} -- run setup.ps1")
        return
    report(OK, "Ollama", f"already running at {host} (started outside the coach)" if was_up
           else "installed; starts on demand and stops after each run")
    names = {t.get("name") for t in tags.get("models", [])}
    want = m["name"] if ":" in m["name"] else m["name"] + ":latest"
    if want not in names:
        report(FAIL, f"model {m['name']}", f"not pulled -- run: ollama pull {m['name']}")
        return
    try:
        info = requests.post(host + "/api/show", json={"model": m["name"]}, timeout=15).json()
        d = info.get("details", {})
        report(OK, f"model {m['name']}",
               f"{d.get('parameter_size', '?')} params, {d.get('quantization_level', '?')}")
    except Exception:
        report(OK, f"model {m['name']}", "installed")
    num_ctx = m.get("num_ctx")
    report(OK if num_ctx else WARN, "context window",
           f"num_ctx {num_ctx}" if num_ctx else "num_ctx not set -- Ollama's small default "
           "will silently cut the prompt")
    for var, want_val in (("OLLAMA_FLASH_ATTENTION", "1"), ("OLLAMA_KV_CACHE_TYPE", "q8_0")):
        val = os.environ.get(var)
        report(OK if val == want_val else WARN, var,
               val or "not set (setup.ps1 sets it; saves memory at a 32k window)")


def check_telegram(cfg, send_test):
    if not cfg.get("delivery", {}).get("telegram"):
        report(WARN, "Telegram", "delivery.telegram is false")
        return
    import requests
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return  # already reported under .env
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        ok = r.ok and r.json().get("ok")
        report(OK if ok else FAIL, "Telegram bot",
               "@" + r.json()["result"]["username"] if ok else "token rejected")
    except Exception as e:
        report(FAIL, "Telegram bot", f"unreachable ({type(e).__name__})")
        return
    if send_test:
        from . import notify
        try:
            notify.send(cfg, "Montanha coach is connected on the Windows PC.")
            report(OK, "Telegram test message", "sent -- check your phone")
        except Exception as e:
            from .runtime import redact
            report(FAIL, "Telegram test message", redact(str(e)))


def check_background(cfg):
    """Nothing of the coach should run when you aren't using it."""
    from urllib.parse import urlparse
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    autostart = [f.name for f in startup.glob("*") if "ollama" in f.name.lower()] if startup.is_dir() else []
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    if "ollama" in f"{name} {val}".lower():
                        autostart.append(f"registry Run: {name}")
                    i += 1
        except OSError:
            pass
    report(WARN if autostart else OK, "Ollama at login",
           f"starts at login ({', '.join(autostart)}) -- re-run setup.ps1 to remove"
           if autostart else "does not start at login")
    bind = os.environ.get("OLLAMA_HOST", "")
    host = urlparse(bind if "://" in bind else "http://" + bind).hostname if bind else None
    if host and host not in ("127.0.0.1", "localhost", "::1"):
        report(WARN, "OLLAMA_HOST", f"{bind} -- Ollama would be reachable from the network")
    if urlparse(cfg["model"]["host"]).hostname not in ("127.0.0.1", "localhost", "::1"):
        report(WARN, "model.host", f"{cfg['model']['host']} is not this PC")


def check_backup(cfg):
    from . import backup_status
    last = backup_status.last_backup()
    if last is None:
        report(WARN, "SSD backup", "never -- plug in the SSD and run backup.cmd")
        return
    days = (datetime.now() - last).days
    limit = (cfg.get("backup") or {}).get("remind_after_days", 14)
    report(OK if days < limit else WARN, "SSD backup", f"last {days} day(s) ago ({last:%Y-%m-%d})")


def main():
    send_test = "--send-test" in sys.argv
    try:
        from .config import CONFIG, ROOT, project_path
    except Exception as e:
        print(f"  [{FAIL}] config.yaml / .env: {e}")
        sys.exit(1)
    print("Montanha coach -- health check\n")
    check_python()
    check_env(ROOT)
    check_data(CONFIG, project_path)
    check_garmin_key()
    check_ollama(CONFIG)
    check_background(CONFIG)
    check_telegram(CONFIG, send_test)
    check_backup(CONFIG)
    fails, warns = results.count(FAIL), results.count(WARN)
    print(f"\n{fails} problem(s), {warns} warning(s)."
          + ("  Everything the morning job needs is in place." if not fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
