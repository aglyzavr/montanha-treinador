"""
Health check: is every piece the morning job needs in place?

    python -m src.doctor              # check everything, change nothing
    python -m src.doctor --send-test  # also send a Telegram test message

Read-only on purpose. It never logs in to Garmin (rule: login happens once, in
scripts/setup_garmin_login.py) -- it only checks the cached token exists. It
never prints a secret, only whether it is set.
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
    for mod in ("garminconnect", "requests", "dotenv", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            report(FAIL, f"package {mod}", "missing -- run setup.ps1 (pip install -e .)")
            return
    report(OK, "Python packages", "garminconnect, requests, python-dotenv, PyYAML")


def check_env(root: Path):
    if not (root / ".env").exists():
        report(FAIL, ".env", "missing -- copy .env.example to .env and fill it in")
        return
    for key in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
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


def check_garmin_token():
    store = Path(os.path.expanduser(os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect")))
    files = list(store.glob("*")) if store.is_dir() else []
    if not files:
        report(FAIL, "Garmin token", f"none at {store} -- run scripts/setup_garmin_login.py once")
        return
    age = (datetime.now() - datetime.fromtimestamp(max(f.stat().st_mtime for f in files))).days
    report(OK if age < 300 else WARN, "Garmin token",
           f"cached {age} days ago" + (" -- expires around a year; re-login soon" if age >= 300 else ""))


def check_ollama(cfg):
    import requests
    m = cfg["model"]
    host = m["host"].rstrip("/")
    try:
        tags = requests.get(host + "/api/tags", timeout=5).json()
    except Exception:
        report(FAIL, "Ollama", f"not reachable at {host} -- start the Ollama app")
        return
    report(OK, "Ollama", f"running at {host}")
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
    check_garmin_token()
    check_ollama(CONFIG)
    check_telegram(CONFIG, send_test)
    check_backup(CONFIG)
    fails, warns = results.count(FAIL), results.count(WARN)
    print(f"\n{fails} problem(s), {warns} warning(s)."
          + ("  Everything the morning job needs is in place." if not fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
