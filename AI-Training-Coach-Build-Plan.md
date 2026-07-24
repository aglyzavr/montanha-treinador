# Personal AI Training Coach — Build Plan

*A private, local-first coach that reads your Garmin data every morning and tells you what to do today, then reviews and corrects your plan every Sunday.*

Owner: Aleksei · Last updated: 2026-07-21 · Status: Design locked, ready to build

---

## 1. What we're building

A small program that runs on your MacBook while you sleep. Each morning it pulls last night's sleep and yesterday's training from Garmin, reasons about it using the principles from your chosen book and your current training plan, and sends you one message: **train / go easy / rest**, with the specific session and the reasoning. Every Sunday it looks back at the whole week and proposes corrections to the plan.

**Two outputs, nothing else:**

| When | What you receive |
|------|------------------|
| Every day, ~06:30 | Today's call: session type, details, and *why* — based on sleep, yesterday's load, and where you are in the plan. |
| Every Sunday | A weekly review: planned vs. actual, fatigue trend, and concrete adjustments to next week's plan. |

There is **no app UI to build**. The interface is the Telegram message. The "app" is a couple of Python scripts plus a local model.

---

## 2. Design principles (the constraints we agreed on)

- **Local & private.** All health data and all reasoning stay on your Mac. The only thing that leaves the machine is the final text message.
- **Solo user.** No accounts, multi-tenancy, auth, or hosting concerns. Simplest thing that works.
- **Quality over speed.** It runs while you sleep, so we spend that time budget on a high-precision model + thinking mode, not on fast generation.
- **One fragile dependency, isolated.** Garmin access is the only thing likely to break over time; we wall it off so a fix is a one-file change.
- **Runs on the Mac, not the server.** The M4's 48 GB unified memory is the best local-inference hardware you have. Hetzner stays optional (see §14).

---

## 3. Architecture at a glance

```
                 ┌─────────────────────────────────────────────┐
                 │              MacBook Pro M4 (48 GB)           │
                 │                                               │
  Garmin Connect │   ┌──────────────┐      ┌────────────────┐   │
  ───────────────┼──▶│ garmin.py    │─────▶│  SQLite (memory)│  │
  (sleep, HRV,   │   │ (fragile,    │      │  metrics,      │   │
  load, activ.)  │   │  isolated)   │      │  activities,   │   │
                 │   └──────────────┘      │  recs, plan,   │   │
                 │                          │  reviews)      │   │
  coach_         │   ┌──────────────┐      └───────┬────────┘   │
  principles.md ─┼──▶│ prompt build │◀─────────────┘            │
  plan.json ─────┼──▶│              │                           │
                 │   └──────┬───────┘                           │
                 │          ▼                                   │
                 │   ┌──────────────┐   Ollama                  │
                 │   │  LLM call    │──▶ Qwen3.6 35B-A3B (Q8,    │
                 │   │              │    thinking mode on)       │
                 │   └──────┬───────┘                           │
                 │          ▼                                   │
                 │   ┌──────────────┐                           │
                 │   │ notify.py    │──────────────────────────┼──▶ Telegram (you)
                 │   └──────────────┘                           │
                 │                                               │
                 │   launchd + pmset wake  ── triggers daily/Sun │
                 └─────────────────────────────────────────────┘
```

Data flow, in words: a scheduler wakes the Mac → fetch Garmin → store in SQLite → assemble a prompt from principles + plan + recent data → local model reasons → format → send to Telegram → log the recommendation back to SQLite.

---

## 4. Locked technical decisions

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12+ | Best ecosystem for Garmin + Ollama + scheduling glue. |
| Garmin access | `python-garminconnect` ≥ 0.3.6 | Actively maintained; rebuilt its login on `curl_cffi` after Garmin's 2026 bot crackdown. **`garth` is deprecated — do not use directly.** |
| Local model | **Qwen3.6 35B-A3B**, Q8 quant (fallback Q6_K), **thinking mode ON** | Newer 2026 reasoning model; beats Llama 3.3 70B on reasoning and fits 48 GB at near-lossless quality. |
| Model runtime | Ollama | One-command install/run on Apple Silicon; simple local HTTP API for scripting. |
| Memory / storage | SQLite (single file) | Zero-setup persistence; gives the weekly review real history to reason over. |
| Book knowledge | Distilled `coach_principles.md` in the system prompt | Far simpler than RAG and sufficient for a bounded coaching decision. RAG over the full PDF is a later upgrade. |
| Training plan | `plan.json` (versioned in SQLite) | Structured, machine-editable so the Sunday job can amend it. |
| Delivery | Telegram bot (primary); email optional for weekly review | Free, instant, rich text, trivial API. |
| Scheduling | macOS `launchd` + `pmset` scheduled wake | Native, no daemons; wakes the Mac to run the job. |
| Host | MacBook (Hetzner optional later) | 48 GB unified memory is ideal for local inference. |

> **Verify at build time:** the exact Ollama tag (e.g. `qwen3.6:35b-a3b-q8_0`) and the current thinking-mode toggle for this model version — model tags and options move fast. Confirm on ollama.com/library.

---

## 5. Repository structure

```
ai-coach/
├── README.md
├── pyproject.toml            # deps: garminconnect, requests, python-dotenv, (ollama client optional)
├── .env                      # secrets: GARMIN_EMAIL, GARMIN_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
├── config.yaml               # model name/quant, schedule times, thresholds, timezone
├── data/
│   ├── coach.db              # SQLite (created on first run)
│   ├── coach_principles.md   # distilled book rules (you write this once)
│   └── plan.json             # current training plan (you write this once)
├── src/
│   ├── garmin.py             # ISOLATED fragile layer: login + fetch → normalized dicts
│   ├── store.py              # SQLite read/write; schema + migrations
│   ├── prompt.py             # builds daily/weekly prompts from principles + plan + data
│   ├── llm.py                # Ollama call, thinking mode, output parsing
│   ├── notify.py             # Telegram (+ optional email) sender
│   ├── daily.py              # entry point: the morning job
│   └── weekly.py             # entry point: the Sunday review job
├── scripts/
│   ├── setup_garmin_login.py # one-time interactive login (handles MFA, caches token)
│   ├── com.aleksei.coach.daily.plist
│   └── com.aleksei.coach.weekly.plist
└── logs/
    └── coach.log
```

---

## 6. Data model (SQLite)

```sql
-- one row per day of body/readiness signals
CREATE TABLE daily_metrics (
  date TEXT PRIMARY KEY,           -- YYYY-MM-DD
  sleep_score INTEGER,
  sleep_duration_min INTEGER,
  deep_min INTEGER, rem_min INTEGER, light_min INTEGER, awake_min INTEGER,
  hrv_overnight REAL,
  resting_hr INTEGER,
  body_battery_low INTEGER, body_battery_high INTEGER,
  training_readiness INTEGER,
  stress_avg INTEGER,
  raw_json TEXT                    -- full payload, for reprocessing later
);

-- one row per activity (0..n per day)
CREATE TABLE activities (
  id TEXT PRIMARY KEY,
  date TEXT,
  type TEXT,                       -- run, ride, strength, ...
  duration_min REAL, distance_km REAL,
  avg_hr INTEGER, max_hr INTEGER,
  training_load REAL, aerobic_te REAL, anaerobic_te REAL,
  raw_json TEXT
);

-- what the coach said, for continuity and audit
CREATE TABLE recommendations (
  date TEXT, kind TEXT,            -- 'daily' | 'weekly'
  plan_week INTEGER,
  session_type TEXT,              -- train | easy | rest | ...
  recommendation_text TEXT,
  model TEXT, created_at TEXT,
  PRIMARY KEY (date, kind)
);

-- plan history so we can see how it evolved
CREATE TABLE plan_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_date TEXT,
  plan_json TEXT,
  change_summary TEXT,
  created_at TEXT
);

-- Sunday reviews
CREATE TABLE weekly_reviews (
  week_start TEXT PRIMARY KEY,
  adherence_summary TEXT,
  fatigue_trend TEXT,
  changes_made TEXT,
  review_text TEXT,
  created_at TEXT
);
```

This is the "accumulated knowledge": the daily job reads the last ~7–14 days to give context; the weekly job reads the full week and the plan history.

---

## 7. Knowledge inputs (you write these once)

**`coach_principles.md`** — the distilled book. Not the whole text; the *decision rules*. For example: how to read a low HRV / poor sleep morning, when a hard session should be moved vs. cut, how to sequence intensity, what "recovery" looks like, red-flag combinations. Aim for 1–3 pages of dense, imperative guidance. This becomes the coach's judgment.

**`plan.json`** — your current plan as structure the model can read and edit:

```json
{
  "goal": {"event": "Marathon", "date": "2026-11-01"},
  "phase": "Base",
  "week_index": 3,
  "weekly_template": {
    "Mon": {"session": "rest"},
    "Tue": {"session": "intervals", "detail": "6x800m @ 5k pace"},
    "Wed": {"session": "easy", "detail": "45 min Z2"},
    "...": "..."
  },
  "targets": {"weekly_km": 55, "long_run_km": 22},
  "zones": {"z2_hr": [130, 145], "threshold_hr": [162, 172]}
}
```

---

## 8. The LLM layer

**Model:** Qwen3.6 35B-A3B via Ollama, Q8 quant, thinking mode enabled (so it deliberates — that's the quality we're buying with the overnight time budget).

**System prompt** (stable): coach persona + the full contents of `coach_principles.md` + an explicit output contract.

**Daily user prompt** (assembled fresh): today's date and plan position (phase, week, planned session) + last night's sleep + yesterday's activities + rolling 7-day readiness/load trend + last few days' recommendations (for continuity).

**Weekly user prompt:** the whole week's planned vs. actual + trend summary + current `plan.json` → ask for adjustments.

**Output contract** (for reliable parsing): a short structured header the code can log, followed by the human message. e.g.

```
VERDICT: easy
SESSION: 40 min Z2 easy run, cap HR at 145
CONFIDENCE: high
FLAGS: HRV 12% below baseline, sleep 5h58m
---
Good morning. Your HRV dipped and you slept under 6 hours after yesterday's
intervals, so we're swapping today's tempo for an easy Z2 run ...
```

The `VERDICT/SESSION/FLAGS` block goes into `recommendations`; the prose goes to Telegram.

---

## 9. Delivery

**Telegram (primary):**

1. Message `@BotFather` → `/newbot` → get the bot **token**.
2. Send your new bot any message, then read your **chat_id** (via `getUpdates` once).
3. Send with a plain HTTPS POST to `https://api.telegram.org/bot<TOKEN>/sendMessage` (`chat_id`, `text`, `parse_mode=Markdown`).

Store `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

**Email (optional):** SMTP for the longer Sunday review if you prefer reading it in email. Same content, different channel.

---

## 10. Scheduling (macOS, runs while you sleep)

Two pieces: **wake the Mac**, then **run the job**.

**Wake** (once, in Terminal):
```bash
# wake every day at 06:25 so the job at 06:30 has a live machine
sudo pmset repeat wake MTWRFSU 06:25:00
```

**Run** — a `launchd` agent per job in `~/Library/LaunchAgents/`, e.g. `com.aleksei.coach.daily.plist` with `StartCalendarInterval` at 06:30 daily, and `com.aleksei.coach.weekly.plist` Sundays at 07:00. Wrap the job in `caffeinate -i` so the Mac doesn't sleep mid-run. Load with `launchctl load ~/Library/LaunchAgents/com.aleksei.coach.daily.plist`.

> Reliability note: if the lid is closed / no power, behavior varies. Keep it plugged in overnight. If you later want guaranteed 24/7, that's the Hetzner upgrade in §14.

---

## 11. The two jobs

**`daily.py` (morning):**
1. Fetch last night's sleep + yesterday's activities from `garmin.py`.
2. Upsert into SQLite.
3. Read plan position + last 7–14 days from `store.py`.
4. Build the daily prompt (`prompt.py`).
5. Call the model with thinking mode (`llm.py`).
6. Parse the verdict block; log to `recommendations`.
7. Send the prose to Telegram (`notify.py`).
8. On any failure: send yourself a short error message instead of failing silently.

**`weekly.py` (Sunday):**
1. Pull the past 7 days (metrics + activities + daily recs).
2. Compare planned vs. actual; summarize adherence + fatigue trend.
3. Build the weekly prompt including current `plan.json`.
4. Model proposes adjustments (as an updated `plan.json` + a change summary).
5. Write a new row to `plan_versions`, update `data/plan.json`, log to `weekly_reviews`.
6. Send the review to Telegram/email.

---

## 12. Build phases (start → end)

Each phase is independently testable. Ship one, verify its "Done when," move on.

### Phase 0 — Environment
- Install Python 3.12+, `uv` or `venv`, Ollama.
- Create repo skeleton (§5), `.env`, `config.yaml`.
- **Done when:** `python -c "import garminconnect"` works and `ollama --version` runs.

### Phase 1 — Garmin (do the risky part first)
- `scripts/setup_garmin_login.py`: interactive login, handle MFA, cache token to `~/.garminconnect/`.
- `garmin.py`: functions returning normalized dicts for sleep, HRV/readiness, and a day's activities. Reuse cached token; back off on HTTP 429; never re-login per run.
- **Done when:** running `garmin.py` prints last night's sleep and yesterday's activity for a real date.

### Phase 2 — Local model online
- `ollama pull` the chosen Qwen3.6 35B-A3B tag; confirm it fits and responds.
- `llm.py`: send a prompt via Ollama's API, thinking mode on, return text.
- **Done when:** a hardcoded prompt returns a sensible coaching answer in the terminal.

### Phase 3 — Daily recommendation, end to end (still terminal)
- Write `coach_principles.md` (v1) and `plan.json`.
- `prompt.py` assembles the daily prompt; wire fetch → prompt → model → printed verdict.
- **Done when:** one command prints today's verdict + rationale using real Garmin data.

### Phase 4 — Delivery
- Create the Telegram bot; `notify.py` sends a message.
- Route the daily output to Telegram.
- **Done when:** you receive today's recommendation on your phone.

### Phase 5 — Memory (SQLite)
- `store.py`: schema (§6), upserts, and read helpers (last N days, plan position).
- Persist metrics, activities, and each recommendation.
- **Done when:** re-running doesn't duplicate rows and the prompt includes real 7-day trend context.

### Phase 6 — Automation
- `launchd` daily agent + `pmset` wake + `caffeinate` wrapper.
- Failure path sends an error ping.
- **Done when:** you get the message a few mornings running without touching the laptop.

### Phase 7 — Weekly review + plan correction
- `weekly.py`: planned-vs-actual, model proposes plan edits, versioned write-back.
- Sunday `launchd` agent.
- **Done when:** Sunday delivers a review and `plan.json` + `plan_versions` update correctly.

### Phase 8 — Hardening & polish
- Retries/backoff on Garmin + Ollama; structured logging to `logs/coach.log`.
- Sanity guards (e.g., no data → say so, don't hallucinate a session).
- Tune `coach_principles.md` and output format from real messages.
- **Done when:** it survives a missing-data day and a transient network error gracefully.

### Phase 9 — Optional upgrades
- RAG over the full book (local embeddings + sqlite-vec/LanceDB) for faithful citations.
- Two-way feedback: reply to the Telegram message ("felt terrible", "skipped") and have it logged and considered.
- Hetzner failover for true 24/7 (see §14).

---

## 13. Risks & mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Garmin changes auth / breaks the library | Medium–High over time | Isolate all Garmin code in `garmin.py`; pin a working version; watch the library's releases; token reuse + 429 backoff to avoid triggering bot defenses. |
| Model output not parseable | Low–Medium | Strict output contract (§8) + a tolerant parser that falls back to sending raw prose. |
| Mac asleep / lid closed at run time | Medium | `pmset` wake + `caffeinate` + keep it plugged in; Hetzner failover if it matters. |
| Q8 too tight on 48 GB with other apps open | Low (overnight, headless) | Fallback to Q6_K; ensure nothing heavy runs during the job. |
| Health data leaking | Low (local) | Everything stays local; only final text goes to Telegram. Consider self-hosted email if even that's too much. |
| Bad advice / over-reliance | — | It's a decision aid, not a doctor. Principles file should encode conservative defaults (when in doubt, easier). |

---

## 14. Setup checklist (accounts, installs, secrets)

- [ ] Garmin Connect credentials (and MFA method ready for first login).
- [ ] Telegram account → bot token + chat_id.
- [ ] Ollama installed; model pulled and tested.
- [ ] `.env` populated (Garmin, Telegram).
- [ ] `coach_principles.md` written from your book.
- [ ] `plan.json` filled with your current plan.
- [ ] (Optional) SMTP creds if using email.
- [ ] (Optional) Hetzner + Tailscale if you later move scheduling off the Mac.

**About Hetzner:** not needed to start. If you later want the job to run even when the laptop is off, the pattern is: lightweight scheduler + Garmin fetch on Hetzner (always on), calling the Mac's Ollama over a private Tailscale network for inference — but that only helps when the Mac is awake and reachable, so it's a reliability upgrade, not a day-one need. Simpler alternative: run a smaller model directly on Hetzner if you accept lower quality. Recommendation: start fully on the Mac; revisit only if missed mornings bother you.

---

## 15. Suggested order of your first sessions

1. Phases 0–1 in one sitting (prove Garmin login — the only real unknown).
2. Phases 2–4 next (you'll have a working coach on your phone, even if manual).
3. Phase 5–6 (memory + automation) once the daily message feels right.
4. Phase 7 after a week of real data exists to review.
5. Phase 8 continuously; Phase 9 when you want more.

Get the daily message good before automating it — the content matters more than the plumbing.
