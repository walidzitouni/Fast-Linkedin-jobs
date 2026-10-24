"""Daily headless LinkedIn job scanner.

Reads config.json, scans every keyword against LinkedIn's public pages with
polite pacing, upserts results into the SQLite database, and writes a
formatted Excel report to exports/jobs_YYYY-MM-DD.xlsx (sheets: "New today"
and "All jobs"). Designed to run unattended from Windows Task Scheduler via
run_daily.bat.

Exit codes: 0 = success, 1 = fatal error, 2 = aborted because LinkedIn
started blocking automated access (results gathered so far are still saved).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from database import get_new_since, load_records, upsert_records, write_excel
from providers.public_search import (
    BlockedError, PoliteClient, search_public_jobs,
)

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
EXPORT_DIR = ROOT / "exports"

DEFAULT_CONFIG = {
    "keywords": [],
    "location": "France",
    "date_filter": "Past week",
    "employment_types": ["Internship"],
    "remote_types": [],
    "pages_per_keyword": 3,
    "fetch_descriptions": True,
    "max_descriptions_per_run": 60,
    "min_delay": 3,
    "max_delay": 7,
}


def load_config(path: Path) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            config.update(json.load(handle))
    config["keywords"] = list(dict.fromkeys(
        line.strip() for line in config["keywords"] if line.strip()
    ))
    return config


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("scanner")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        LOG_DIR / "scanner.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily LinkedIn public job scanner")
    parser.add_argument("--config", default=str(ROOT / "config.json"),
                        help="Path to config.json")
    parser.add_argument("--no-descriptions", action="store_true",
                        help="Skip fetching full job descriptions (faster, fewer requests)")
    parser.add_argument("--pages", type=int, default=None,
                        help="Override pages_per_keyword from the config")
    args = parser.parse_args()

    logger = setup_logging()
    config = load_config(Path(args.config))
    if args.pages is not None:
        config["pages_per_keyword"] = args.pages
    fetch_descriptions = bool(config["fetch_descriptions"]) and not args.no_descriptions

    if not config["keywords"]:
        logger.error("No keywords configured. Add them to %s", args.config)
        return 1

    run_start = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info("=== Scan started: %d keyword(s), location=%s, %s page(s)/keyword ===",
                len(config["keywords"]), config["location"], config["pages_per_keyword"])

    client = PoliteClient(min_delay=config["min_delay"], max_delay=config["max_delay"])
    known_urls = {record["job_url"] for record in load_records()}
    budget = {"left": int(config["max_descriptions_per_run"]) if fetch_descriptions else 0}

    all_records: list[dict] = []
    warnings: list[str] = []
    blocked = False
    try:
        for index, keyword in enumerate(config["keywords"], start=1):
            logger.info("[%d/%d] Searching: %s", index, len(config["keywords"]), keyword)
            records, issues = search_public_jobs(
                keyword=keyword,
                location=config["location"],
                date_filter=config["date_filter"],
                employment_types=config["employment_types"],
                remote_types=config["remote_types"],
                pages=config["pages_per_keyword"],
                progress=lambda msg: logger.info("  %s", msg),
                client=client,
                known_urls=known_urls,
                fetch_descriptions=fetch_descriptions,
                desc_budget=budget,
            )
            all_records.extend(records)
            warnings.extend(issues)
            logger.info("  -> %d job(s) collected for '%s'", len(records), keyword)
    except BlockedError as exc:
        blocked = True
        logger.error("BLOCKED: %s", exc)

    inserted, updated, _ = upsert_records(all_records)
    new_records = get_new_since(run_start)
    all_saved = load_records()

    stamp = datetime.now().strftime("%Y-%m-%d")
    xlsx_path: Path | str = EXPORT_DIR / f"jobs_{stamp}.xlsx"
    try:
        write_excel(all_saved, new_records, xlsx_path)
    except PermissionError:
        logger.error("Cannot write %s — it is open in Excel. Close it and re-run.", xlsx_path)
        xlsx_path = "(not written: file locked)"
    try:
        write_excel(all_saved, new_records, EXPORT_DIR / "jobs_latest.xlsx")
    except PermissionError:
        logger.warning("exports/jobs_latest.xlsx is open in Excel; dated report only this run.")

    logger.info("=== Summary ===")
    logger.info("Requests made:      %d", client.requests_made)
    logger.info("Jobs seen this run: %d", len(all_records))
    logger.info("New jobs saved:     %d", inserted)
    logger.info("Known jobs updated: %d", updated)
    logger.info("Total in database:  %d", len(all_saved))
    logger.info("Excel report:       %s", xlsx_path)
    if warnings:
        logger.warning("%d warning(s), first 10:", len(warnings))
        for warning in warnings[:10]:
            logger.warning("  - %s", warning)
    if blocked:
        logger.error("Run aborted early due to blocking. Saved partial results. "
                     "Wait a few hours before the next scan.")
        return 2
    logger.info("Scan finished successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
