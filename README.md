# Montanha Coach

A private, local-first AI training coach. Every morning it pulls your Garmin
sleep + yesterday's training, reasons with your book's principles and your plan
using a **local** LLM, and sends you today's call (train / easy / rest) on
Telegram. Every Sunday it reviews the week and updates your plan.

Everything runs on your Mac. The only thing that leaves the machine is the final
text message.

---

## What you need to do (the connection/credential parts are yours)

You'll set up four things: **Python deps**, **Ollama + model**, **Garmin login**,
**Telegram bot**. Then run it. Follow the steps in order.

### 0. Prerequisites

- macOS on your M4.
- [Homebrew](https://brew.sh) (optional but handy).
- Python 3.12+ (`python3 --version`).
- [Ollama](https://ollama.com/download) installed.

### 1. Install the project

```bash
cd montanha-coach                      # the folder this README is in
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Pull and test the local model

```bash
# CONFIRM the exact tag first at https://ollama.com/library (search "qwen3.6")
ollama pull qwen3.6:35b-a3b-q8_0       # ~37 GB; if memory is tight use the q6_k tag
ollama run qwen3.6:35b-a3b-q8_0 "Say hello in one sentence."
```

If the name in `config.yaml` (`model.name`) doesn't match a real tag, fix it there.
Also confirm the thinking-mode flag: this project sends `"think": true` to Ollama's
`/api/chat`. If your Ollama version errors on that, set `model.think: false` in
`config.yaml` (the model still reasons, just without the explicit thinking channel).

### 3. Create your `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in:

- `GARMIN_EMAIL`, `GARMIN_PASSWORD` — your Garmin Connect login.
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` — from step 5 below.
- (optional) `SMTP_*` + `EMAIL_TO` if you also want the weekly review emailed.

`.env` is git-ignored. Keep it private.

### 4. Log in to Garmin once (creates a cached token)

```bash
python scripts/setup_garmin_login.py
```

- Enter the MFA code if prompted.
- On success it caches a token at `~/.garminconnect` and runs a smoke test.
- After this, the daily/weekly jobs **resume from the token** and never re-login
  (this is deliberate — re-logging in every run can trip Garmin's rate limits).
- If it fails on login/token: `pip install -U garminconnect`, then check the
  [python-garminconnect README](https://github.com/cyberjunky/python-garminconnect) —
  the auth/token API is the one part that changes over time, and it's isolated in
  `src/garmin.py` + this script so a fix is small.

### 5. Create the Telegram bot

1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts → copy the
   **bot token** into `TELEGRAM_TOKEN`.
2. Send your new bot any message (say "hi") so it has a chat to reply to.
3. Get your chat id — open this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id":<number>...}` and put `<number>` in `TELEGRAM_CHAT_ID`.

Test delivery:

```bash
source .venv/bin/activate
python -c "from src.config import CONFIG; from src import notify; notify.send(CONFIG, 'Montanha coach is connected ✅')"
```

You should get a Telegram message.

### 6. Add your book + plan

- Edit `data/coach_principles.md` — replace the template with the distilled
  decision rules from your book (dense, imperative; this becomes the coach's judgment).
- Edit `data/plan.json` — put in your real current plan.

### 7. Backfill your history (one time)

```bash
source .venv/bin/activate
python scripts/backfill.py --reparse          # no network: re-extracts vert,
                                              # HR zones, cadence, temperature
                                              # etc. from payloads already saved
python scripts/backfill.py --fetch 60 --scores  # recovers days a missed run lost
```

`--reparse` is free and instant — every new metric was already sitting in the
stored `raw_json` and simply wasn't extracted.

### 8. First real run

```bash
python -m src.daily
python -m src.weekly --dry-run   # prints the prompt without calling the model
python -m src.weekly
```

This fetches Garmin → reasons → sends today's recommendation to Telegram, and
saves everything to `data/coach.db`.

### 9. Log what Garmin can't see

Strength sessions, foot condition and soreness aren't in any Garmin payload.
Reply to the bot in Telegram and the next run picks it up:

```
s: back squat 5x5 @85kg          → strength session
feet: hotspot left heel, taped   → foot durability log
sore 3                           → soreness 1-5
rpe 7                            → session RPE
2026-08-01 s: deadlifts          → backdate anything
```

Without these the coach reports them as unknown rather than assuming the work
was done.

---

## Automate it (runs while you sleep)

Two steps: wake the Mac, then schedule the jobs.

**Wake the Mac** (once):

```bash
sudo pmset repeat wake MTWRFSU 06:25:00
```

**Schedule the jobs** — edit the two plist files in `scripts/`, replacing
`__PROJECT_DIR__` with this folder's absolute path (run `pwd` to get it), then:

```bash
cp scripts/com.montanha.coach.daily.plist  ~/Library/LaunchAgents/
cp scripts/com.montanha.coach.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.montanha.coach.daily.plist
launchctl load ~/Library/LaunchAgents/com.montanha.coach.weekly.plist
```

Keep the Mac plugged in overnight. Logs go to `logs/`. To stop a job:
`launchctl unload ~/Library/LaunchAgents/com.montanha.coach.daily.plist`.

---

## How it fits together

```
src/garmin.py   Garmin fetch + normalize   (the only fragile part — isolated)
src/store.py    SQLite memory + additive migrations
src/plan.py     resolves date -> phase, week, km/vert target (deterministic)
src/metrics.py  computes the weekly numbers (deterministic)
src/inbox.py    ingests your Telegram replies (strength / feet / soreness)
src/prompt.py   builds the daily / weekly prompts from the above
src/llm.py      Ollama call (thinking mode) + output parsing
src/notify.py   Telegram / email delivery
src/daily.py    morning job  ->  python -m src.daily        (06:30 daily)
src/weekly.py   weekly job   ->  python -m src.weekly       (07:30 Mondays)
scripts/        Garmin login, backfill, launchd plist templates
data/           coach_principles.md, plan.json, coach.db
```

**Division of labour.** `plan.py` and `metrics.py` do all arithmetic and
calendar logic in Python; the model receives finished figures and spends its
reasoning on judgement alone. It is instructed never to compute its own totals.

**The weekly window** is always the last *complete* Monday–Sunday. Run it on
any day of the week and you get the same answer for that week.

## Troubleshooting

- **No Telegram message but no error:** check `delivery.telegram: true` in `config.yaml`
  and that the test in step 5 worked.
- **Model call times out:** first run loads ~37 GB into memory; `model.timeout_seconds`
  is already 900. Make sure nothing else heavy is running.
- **A metric is always empty:** payload field names vary. Run a day, inspect
  `raw_json` in `data/coach.db`, and adjust the `.get()` paths in `src/garmin.py`.
- **Garmin 401/429:** don't re-run the login script repeatedly. Wait, then rely on
  the cached token. Update the library if 401s persist.

## Notes

- This is a decision aid, not medical advice.
- Only the final message text leaves your Mac (via Telegram). All health data and
  reasoning stay local.
- See `../AI-Training-Coach-Build-Plan.md` for the full design and phase plan.
