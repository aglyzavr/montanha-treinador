# Montanha Coach on Windows

Same coach as on the Mac, still 100% local: the model runs in Ollama on this PC,
the data stays in `data\` on this PC, and backups go only to your own SSD.
What leaves the PC: the Garmin download from your own account, and a Telegram
message only when you press "Send to phone".

## First-time setup (about 30 min, mostly the 22 GB model download)

1. **Restore your data** into `data\`: `plan.json`, `coach.db`,
   `strength_reference_uphill_athlete.md` (and any `plan.json.*.bak`).
   From an SSD snapshot: copy the contents of the newest dated folder.
2. In this folder, open a terminal and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   It installs Python and Ollama if missing, creates `.venv`, opens `.env` in
   Notepad if it doesn't exist yet, downloads the model, checks your Garmin
   API key (create it first: README step 4), runs the tests, puts a
   **Montanha Coach** shortcut on the desktop, and finishes with a health check
   plus a Telegram test message. Re-run it any time; it skips what's done.

## Every morning

Double-click **Montanha Coach** on the desktop. A page opens in your browser
(served by this PC, reachable only from this PC):

1. Tap what you know about yesterday (feet) and this morning (legs). Skip
   anything you don't know; it's recorded as not logged, never assumed. The
   model starts loading while you tap. Strength isn't asked: record it on the
   watch and the **Strength** tab shows the week, your stage per region (set
   by the coach from your Garmin sets) and the last 6 weeks.
2. Press **Get today's call**. If last night's sleep hasn't synced to Garmin
   yet, it tells you to open Garmin Connect on your phone and try again
   (or run without sleep data). Thinking takes a few minutes on this PC.
3. Read the call. **Send to phone** pushes it to Telegram if you want it with
   you on the run.

When last week hasn't been reviewed yet, a **Review last week** button
appears. It can adjust next week's plan within the safe bounds, always with a
backup of the old plan.

## Other things

| What | How |
|---|---|
| Is everything OK? | `.venv\Scripts\python -m src.doctor` |
| Back up | plug in the SSD, double-click `backup.cmd` |
| First backup ever | `.venv\Scripts\python scripts\backup.py --init E:\` (your SSD's letter) |
| App log | `logs\app.log` |
| Old terminal way (sends to Telegram) | `.venv\Scripts\python -m src.daily` / `-m src.weekly` |
| Weekly prompt without the model | `.venv\Scripts\python -m src.weekly --dry-run` |

Nothing of the coach runs in the background. Ollama doesn't start at login
(setup.ps1 removes its Startup shortcut); the app starts it when you open it
and, when the app exits, unloads the model and stops Ollama. The app exits when
you press **Done, close the coach**, or about 3 minutes after you close the
tab (a run in progress finishes first). Double-click the shortcut to start
again. The terminal jobs (`-m src.daily` / `-m src.weekly`) do the same.
Check: `Get-Process ollama*, pythonw` shows nothing once it's closed.

Without the tray app Ollama no longer updates itself; update it now and then
with `winget upgrade Ollama.Ollama`, then re-run `setup.ps1` (the installer
puts the Startup shortcut back; `python -m src.doctor` warns if it's there).

## How this differs from the Mac

| | Mac (M4, 48 GB) | This PC (RTX 5060 8 GB + 32 GB RAM) |
|---|---|---|
| Model | `qwen3.6:35b-a3b-q8_0` | `qwen3.6:35b` (Q4), split GPU + RAM |
| Speed | fast | a few minutes per message |
| How it runs | 06:30 launchd job, Telegram | morning check-in app, Telegram optional |
| Logging feet / soreness | Telegram reply codes | taps in the app |
| Strength | Telegram reply codes | from the watch; Strength tab in the app |

## Privacy notes

- `.env` (Garmin API key, bot token) is never backed up or
  committed. Keep the `.env` values in your password manager.
- The SSD holds your health history unencrypted. Consider BitLocker To Go on it
  (right-click the drive > *Turn on BitLocker*).
- Visual Studio's Copilot chat sends the files it reads to GitHub. Don't point
  it at `data\` (plan.json, coach.db), or turn Copilot off for this folder.
- The GitHub repo is public. `data\` is git-ignored, but the code and
  `coach_principles.md` are visible; make the repo private if that matters.
