# Montanha Coach — Claude project description & instructions

Two blocks below. Paste the first into the project's **Description** field, the second into **Instructions**.

---

## 1. Description

> Private, local-first AI training coach for an ultra-trail athlete (Aleksei, Lisbon) building toward UTMB 100M in 2029/30, via DUT 53K in October 2026. A Python job pulls Garmin sleep/recovery/activity data each morning, resolves where today sits in a multi-year plan, computes every figure deterministically, and hands finished numbers to a local Ollama model that supplies judgement only — then sends today's call (train / easy / rest) to Telegram. A Monday job reviews the last complete week and applies a bounded adjustment to the plan. Everything runs on the athlete's Mac; only the final message text leaves the machine.

---

## 2. Instructions

### What this project is

Montanha Coach is a single-athlete, local-first training coach. Two entry points:

- `python -m src.daily` — 06:30 daily. Garmin fetch → resolve plan context → build prompt → local LLM → Telegram message → SQLite.
- `python -m src.weekly` — Monday 07:00. Reviews the **last complete Mon–Sun week**, computes weekly metrics, asks the model for a review + bounded plan adjustment, writes `plan.json` (with a timestamped `.bak`), sends the review.

Both are scheduled via launchd plists in `scripts/`. Logs in `logs/`. The athlete reads the Telegram message and nothing else — that message is the product.

### Architecture

```
src/config.py   config.yaml (non-secret) + .env (secrets); ROOT/project_path()
src/garmin.py   Garmin fetch + normalize — the only fragile part, deliberately isolated
src/store.py    SQLite (data/coach.db); additive, idempotent migrations; raw_json always kept
src/plan.py     date -> plan/phase/week/targets (deterministic); apply_adjustment() with bounds
src/metrics.py  weekly aggregation, ACWR, intensity distribution (deterministic)
src/inbox.py    ingests Telegram replies (strength / feet / soreness / rpe / note)
src/prompt.py   builds daily & weekly prompts; prescription block; distance estimate
src/llm.py      Ollama /api/chat (thinking mode) + header/JSON parsers
src/notify.py   Telegram (4096-char chunking) / optional email
data/           coach_principles.md, plan.json, strength_reference_*.md, coach.db
tests/          test_coach.py — pure-function tests, no network
```

### Hard rules — do not violate these

**1. Arithmetic lives in Python, never in the model.**
`plan.py` and `metrics.py` do all calendar logic and all totals. The model receives finished figures and spends its reasoning on judgement alone. The system prompt instructs it never to compute its own totals. Never solve a numbers problem by asking the model to be more careful — fix it in Python and inject the result.

**2. Anything the athlete must act on is resolved to finished figures before it reaches the model.**
`prompt.build_prescription()` writes out duration, vert, HR ceiling in bpm, and the week's focus. `PRESCRIPTION_FORMATTING` forbids "as planned" / "the full session" / "as prescribed" as a substitute for numbers, and requires a `Today's session:` line. Rationale: the athlete reads Telegram, not `plan.json` — a message that points back at the plan has failed, even when it's technically correct. Same rule applies to strength days: the circuit is injected; a bare stage name is banned.

**3. Estimates are labelled and never promoted.**
Long runs are prescribed in time + vert, not distance. `estimate_distance_km()` derives a range from the athlete's own history, matched on vertical density (m/km) by fixed-point iteration, and returns `None` rather than guessing when history is thin or that steepness has never been trained. `FADE_PER_HOUR = 0.09` was calibrated by back-testing every run over 1h48 against prior-only data (6/6 within ±10%). **If you change that constant, re-run that back-test.**

**4. Missing data is stated, never filled.**
If a value is unknown, the message says unknown. Weekly totals with data gaps are reported as a lower bound. Strength/feet/soreness that were never logged are reported as unlogged, not assumed done. `activity_fetch` exists because a metrics row is not proof a day's training was seen — the 06:30 job stores that morning's sleep long before the day's training exists, so only a fetch made on a *later* calendar day counts.

**5. `src/garmin.py` is the only module that talks to Garmin.**
It uses Garmin's official Personal API: `connect()` exchanges `GARMIN_API_KEY` for a 24 h bearer token once per run (`scripts/setup_garmin_key.py` checks a new key). Do not loop retries against Garmin. The API has no readiness, recovery time, acute load, VO2max or hill/endurance score — `src/readiness.py` and `metrics._acwr` replace them; `scripts/probe_personal_api.py` re-checks coverage. Payload field names drift — when a metric comes back empty, inspect `raw_json` in the DB and adjust the `.get()`/`_dig()` paths rather than changing the fetch strategy.

**6. Never commit or print secrets.** `.env` holds the Garmin API key and the Telegram token. It is git-ignored. Don't echo it, don't paste values into commit messages or files, don't add it to examples (`.env.example` carries placeholders only).

**7. Store migrations are additive only.** Add to `_MIGRATIONS` as `(table, column, type)`; `init()` ALTERs anything missing. Never rewrite existing history, never drop a column, always keep `raw_json` so new fields can be backfilled from stored payloads with `scripts/backfill.py --reparse` (free, offline) instead of re-hitting Garmin.

**8. Plan edits are bounded and versioned.** The weekly model proposes an adjustment; `plan.apply_adjustment()` rejects out-of-bounds changes, `weekly.py` copies `plan.json` to a timestamped `.bak` first and records the new version in `plan_versions`. Don't let the model write the plan directly, and don't loosen the bounds without saying why.

**9. The weekly window is the last complete Monday–Sunday**, anchored to the calendar, not the run date. Running it on any day must give the same answer for that week. `tests/test_coach.py` locks this down — keep it locked.

**10. This is a decision aid, not medical advice.** Don't add anything that reads as diagnosis or prescribes through pain. Fatigue, soreness and injury signals should bias toward the conservative call.

### Working conventions

- Python ≥3.12, venv at `.venv`, `pip install -e .`. Dependencies stay minimal (requests, python-dotenv, PyYAML) — think hard before adding one.
- Run tests before and after any change to `plan.py`, `metrics.py`, `prompt.py` or `llm.py`: `source .venv/bin/activate && python -m pytest tests/ -q`.
- `python -m src.weekly --dry-run` prints the assembled prompt without calling the model — use it to verify prompt changes cheaply.
- New behaviour that affects what the athlete is told gets a test in `tests/test_coach.py`. Prompt-construction tests assert the *numbers appear in the prompt*, not that the model behaved.
- Comments in this codebase explain *why*, often citing the bug they fixed. Keep that style; when you fix something subtle, leave the reason.
- Config knobs go in `config.yaml`; anything secret goes in `.env`. Don't hardcode paths — use `config.project_path()`.
- Model output format is a contract: `VERDICT / SESSION / CONFIDENCE / FLAGS`, `---`, then the human message. `llm.parse_header()` and `human_part()` depend on it. Change the contract and the parsers together, and check `daily.py`'s use of `header.get(...)`.

### Athlete & plan context

- Aleksei, 35–39, Lisbon. VO2max ~54, resting HR 32, max HR 197, LTHR 178–179, AeT 148–152 (April 2026), AeT–LT gap 27 bpm against a year-1 target of 20–25.
- North star: UTMB 100M (170 km / 10,000 m D+), 2029 or 2030. Backup: CCC 100K. Year 1 (May 2026 – Apr 2027) is the Foundation Year: close the muscular-endurance and strength gap, foot durability. Key A race: **Douro Ultra Trail 53K, 2,300 m D+, 10 October 2026**.
- Coaching philosophy comes from *Training for the Uphill Athlete*, distilled into `data/coach_principles.md` — that file is the coach's judgement, so edit it deliberately.
- Heat matters: Lisbon summer evening runs log 26–32 °C. HR drift in the heat is thermoregulatory, not aerobic — never read it as fitness loss, and don't treat an afternoon session as a valid aerobic test.
- **AeT retests:** a valid test is a morning, cool, flat, rested-legs run — 15 min warmup, then HR locked at 148 (Garmin alert at 152) held 60 min, never chasing pace; compare Pa:Hr for min 0–30 vs 30–60, under 5% = at or below AeT. Whole-block decoupling figures from self-paced runs are artifacts — do not quote them. The 2026-08-15 attempt was inconclusive for exactly this reason; AeT stands at 148–152 until a valid retest moves it.

### When asked to change how the coach talks

Change `prompt.py` (the blocks and contracts), not the model temperature. The rules in `OUTPUT_CONTRACT`, `PRESCRIPTION_FORMATTING` and `WEEKLY_CONTRACT` exist because a specific message once failed the athlete — read the surrounding comments before removing a constraint.
