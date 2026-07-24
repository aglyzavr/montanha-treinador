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

### 7. First real run

```bash
source .venv/bin/activate
python -m src.daily
```

This fetches Garmin → reasons → sends today's recommendation to Telegram, and
saves everything to `data/coach.db`. Run the weekly review manually any time with:

```bash
python -m src.weekly
```

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
src/store.py    SQLite memory
src/prompt.py   builds the daily / weekly prompts from principles + plan + data
src/llm.py      Ollama call (thinking mode) + output parsing
src/notify.py   Telegram / email delivery
src/daily.py    morning job  ->  python -m src.daily
src/weekly.py   Sunday job   ->  python -m src.weekly
scripts/        one-time Garmin login + launchd plist templates
data/           coach_principles.md, plan.json, coach.db
```

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
