# Rapid LinkedIn Jobs — daily automated job scanner

A local tool that scans LinkedIn's **public, no-login** job pages for your keywords, stores everything in SQLite, flags what's new each day, and produces a formatted **Excel report**. Run it on demand from the Streamlit dashboard, or automatically every day via Windows Task Scheduler.

Built for internship hunting: set your keywords once, let the daily scan collect every matching public offer, and open the Excel file instead of scrolling LinkedIn for hours.

## What it does
- Multiple keywords, location, posting-date, employment-type and work-arrangement filters
- Job **cards parsed directly from the search listing** (one request per results page, not one per job)
- Optional full descriptions fetched only for **new** jobs, capped per run
- SQLite persistence with URL deduplication and `first_seen` / `last_seen` history
- Daily Excel report (`exports/jobs_YYYY-MM-DD.xlsx`) with **"New today"** and **"All jobs"** sheets — styled headers, filters, clickable links
- Streamlit dashboard with Excel/CSV/JSON export
- Manual public job URL import

## Staying under the radar (anti-block design)
The tool protects your connection by being **polite**, not by evading defenses:
- Randomized 3–7 s delay between requests (configurable), one consistent browser identity
- Drastically fewer requests thanks to card-level parsing
- **Circuit breaker**: if LinkedIn answers 403/429/999, the run backs off once and then **stops** (exit code 2) instead of fighting the block
- It never logs in, never uses cookies/proxies, never solves CAPTCHAs, never bypasses access controls

If a run ends blocked, wait a few hours before scanning again. Running it once a day is the intended cadence — do not schedule it hourly.

## Quick start (Windows PowerShell)
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Edit `config.json` with your keywords, then either:

**Run a scan now (headless):**
```powershell
python scanner.py
```
Reports: `exports\jobs_latest.xlsx` (always up to date) + a dated snapshot per run · Log: `logs\scanner.log`

The dashboard also refreshes `exports\jobs_latest.xlsx` after every search, so the file always mirrors the database no matter how you scan. Keep that file closed while a scan runs — Excel locks open files.

**Or use the dashboard:**
```powershell
streamlit run app.py
```
Open the printed URL (usually http://localhost:8501). The sidebar reads/writes `config.json`, so the dashboard and the daily scanner share settings.

If PowerShell blocks venv activation:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Daily automation (Task Scheduler)
Run once **from an elevated PowerShell** (right-click → Run as administrator):
```powershell
powershell -ExecutionPolicy Bypass -File install_daily_task.ps1
```
This registers **RapidLinkedInJobs-Daily** (default 08:30 every day, edit `$RunTime` in the script to change). Useful commands:
```powershell
schtasks /Run /TN "RapidLinkedInJobs-Daily"      # test it now
schtasks /Delete /TN "RapidLinkedInJobs-Daily" /F  # remove it
```

## config.json reference
| Key | Meaning | Default |
|---|---|---|
| `keywords` | Search terms, one per entry | — |
| `location` | LinkedIn location filter | `France` |
| `date_filter` | `Any time`, `Past 24 hours`, `Past week`, `Past month` | `Past week` |
| `employment_types` | e.g. `Internship`, `Apprenticeship`, `Full-time` | `[Internship]` |
| `remote_types` | `On-site`, `Remote`, `Hybrid` (empty = all) | `[]` |
| `pages_per_keyword` | Result pages per keyword (25 jobs/page, max 10) | `3` |
| `fetch_descriptions` | Fetch full text for new jobs | `true` |
| `max_descriptions_per_run` | Cap on detail requests per run | `60` |
| `min_delay` / `max_delay` | Seconds of random wait between requests | `3` / `7` |

Scanner CLI overrides: `python scanner.py --no-descriptions --pages 1 --config other.json`

## Data
- SQLite database `jobs.sqlite3` in the project directory (auto-created, auto-migrated)
- Excel reports in `exports/`, logs in `logs/`

## Honest limitations
LinkedIn caps public search results (~1000 per query) and may serve incomplete HTML or rate-limit automated access even at polite volumes. This tool maximizes coverage *within public, unauthenticated data*; no tool can guarantee literally every LinkedIn offer without a licensed data source. When automated access is denied, the dashboard shows the search URLs to open in your browser and lets you save public job pages manually.
