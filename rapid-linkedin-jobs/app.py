from __future__ import annotations

import json
import re
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st

from database import (
    load_records, save_records, clear_records, write_excel,
    export_csv, export_json, export_xlsx_bytes,
)
from providers.public_search import (
    BlockedError, PoliteClient, PublicPageError,
    build_search_url, fetch_public_job, search_public_jobs,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
EXPORT_DIR = Path(__file__).resolve().parent / "exports"


def refresh_excel_report() -> str:
    """Rewrite exports/jobs_latest.xlsx from the database (called after saves)."""
    records = load_records()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    new = [r for r in records if str(r.get("first_seen") or "") >= cutoff]
    path = EXPORT_DIR / "jobs_latest.xlsx"
    try:
        write_excel(records, new, path)
    except PermissionError:
        return f"Could not update {path.name} — close it in Excel and run a search again."
    return ""


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


_config = load_config()

st.set_page_config(page_title="Rapid LinkedIn Jobs", page_icon="🔎", layout="wide")
st.title("Rapid LinkedIn Jobs")
st.caption("Local job-search dashboard · Public pages only · No login or access-control bypass")

with st.sidebar:
    st.header("Search configuration")
    keywords_text = st.text_area(
        "Keywords (one per line)",
        value="\n".join(_config.get("keywords") or [
            "Cybersecurity Engineer", "SOC Analyst", "Pentester", "Cloud Security",
        ]),
        height=130,
    )
    location = st.text_input("Location", value=_config.get("location", "France"))
    _date_options = ["Any time", "Past 24 hours", "Past week", "Past month"]
    date_filter = st.selectbox(
        "Posted within", _date_options,
        index=_date_options.index(_config.get("date_filter", "Any time"))
        if _config.get("date_filter") in _date_options else 0,
    )
    _emp_options = ["Full-time", "Part-time", "Contract", "Temporary", "Internship", "Apprenticeship"]
    employment_types = st.multiselect(
        "Employment type", _emp_options,
        default=[x for x in _config.get("employment_types", ["Internship", "Apprenticeship"])
                 if x in _emp_options],
    )
    remote_types = st.multiselect(
        "Work arrangement", ["On-site", "Remote", "Hybrid"],
        default=[x for x in _config.get("remote_types", []) if x in {"On-site", "Remote", "Hybrid"}],
    )
    pages = st.slider("Search pages per keyword", min_value=1, max_value=10,
                      value=int(_config.get("pages_per_keyword", 1)))
    fetch_descriptions = st.checkbox(
        "Fetch full descriptions (slower, more requests)",
        value=bool(_config.get("fetch_descriptions", False)),
    )
    run_search = st.button("Search public pages", type="primary", use_container_width=True)
    if st.button("Save settings to config.json", use_container_width=True):
        save_config({
            "keywords": [line.strip() for line in keywords_text.splitlines() if line.strip()],
            "location": location,
            "date_filter": date_filter,
            "employment_types": employment_types,
            "remote_types": remote_types,
            "pages_per_keyword": pages,
            "fetch_descriptions": fetch_descriptions,
            "max_descriptions_per_run": int(_config.get("max_descriptions_per_run", 60)),
            "min_delay": float(_config.get("min_delay", 3)),
            "max_delay": float(_config.get("max_delay", 7)),
        })
        st.success("Saved. The daily scanner now uses these settings.")
    st.caption("Public HTML may be incomplete or blocked. Requests are paced with random delays and the run stops at the first sign of blocking.")

keywords = [line.strip() for line in keywords_text.splitlines() if line.strip()]
keywords = list(dict.fromkeys(keywords))

tab_search, tab_manual, tab_results = st.tabs(
    ["Search", "Add public job URL", "Saved results"]
)

with tab_search:
    st.subheader("Search jobs")
    st.write("The app builds normal LinkedIn search URLs and attempts to parse only HTML that LinkedIn returns without authentication.")
    if keywords:
        previews = []
        for keyword in keywords:
            previews.append({
                "Keyword": keyword,
                "Search URL": build_search_url(keyword, location, date_filter, employment_types, remote_types),
            })
        st.dataframe(pd.DataFrame(previews), use_container_width=True, hide_index=True)
    else:
        st.info("Enter at least one keyword in the sidebar.")

    if run_search:
        if not keywords:
            st.error("Enter at least one keyword.")
        else:
            progress_text = st.empty()
            progress_bar = st.progress(0)
            found = []
            warnings = []
            blocked = False
            total = len(keywords)
            client = PoliteClient(
                min_delay=float(_config.get("min_delay", 3)),
                max_delay=float(_config.get("max_delay", 7)),
            )
            known_urls = {record["job_url"] for record in load_records()}
            budget = {"left": int(_config.get("max_descriptions_per_run", 60))}
            try:
                for i, keyword in enumerate(keywords):
                    progress_text.write(f"Searching keyword {i + 1}/{total}: {keyword}")
                    records, issues = search_public_jobs(
                        keyword=keyword,
                        location=location,
                        date_filter=date_filter,
                        employment_types=employment_types,
                        remote_types=remote_types,
                        pages=pages,
                        progress=lambda msg: progress_text.write(msg),
                        client=client,
                        known_urls=known_urls,
                        fetch_descriptions=fetch_descriptions,
                        desc_budget=budget,
                    )
                    found.extend(records)
                    warnings.extend(issues)
                    progress_bar.progress((i + 1) / total)
            except BlockedError as exc:
                blocked = True
                st.error(str(exc))

            # URL-based deduplication in-memory; SQLite also enforces unique job_url.
            unique = {}
            for record in found:
                url = record.get("job_url", "").split("?")[0].rstrip("/")
                if url:
                    record["job_url"] = url
                    unique[url] = record
            inserted = save_records(list(unique.values()))
            st.success(f"Fetched {len(found)} record(s); saved {inserted} new unique job(s).")
            report_issue = refresh_excel_report()
            if report_issue:
                st.warning(report_issue)
            else:
                st.caption(f"Excel report updated: `exports\\jobs_latest.xlsx`")
            if blocked:
                st.info("Partial results above were saved before the run stopped. Wait a few hours before scanning again.")
            if warnings:
                with st.expander(f"Warnings ({len(warnings)})", expanded=True):
                    for warning in warnings[:30]:
                        st.warning(warning)
                    if len(warnings) > 30:
                        st.write(f"... and {len(warnings) - 30} more warnings.")
            if not found and warnings:
                st.info("No jobs were extracted. LinkedIn may require browser interaction or restrict automated access. Use the search links above in your browser, then add public job URLs in the next tab.")

    st.markdown("### Open a search in your browser")
    if keywords:
        chosen_keyword = st.selectbox("Keyword", keywords, key="browser_keyword")
        browser_url = build_search_url(
            chosen_keyword, location, date_filter, employment_types, remote_types
        )
        st.link_button("Open LinkedIn job search", browser_url)

with tab_manual:
    st.subheader("Add a publicly accessible job page")
    st.write("Paste a public LinkedIn job URL. The app makes one normal HTTPS request; it won't sign in or work around access restrictions.")
    job_url = st.text_input("LinkedIn job URL", placeholder="https://www.linkedin.com/jobs/view/...")
    if st.button("Fetch and save job"):
        try:
            record = fetch_public_job(job_url.strip())
            record["search_keyword"] = ""
            if not record.get("title") and not record.get("company") and not record.get("description"):
                st.warning("The page loaded but no useful job fields were exposed in its HTML.")
            inserted = save_records([record])
            st.success("Saved new job." if inserted else "This job URL is already saved.")
            report_issue = refresh_excel_report()
            if report_issue:
                st.warning(report_issue)
            st.json(record)
        except PublicPageError as exc:
            st.error(str(exc))

with tab_results:
    st.subheader("Saved jobs")
    records = load_records()
    if not records:
        st.info("No saved jobs yet. Run a search or add a public job URL.")
    else:
        df = pd.DataFrame(records)
        new_24h = 0
        if "first_seen" in df.columns:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            new_24h = int(df["first_seen"].fillna("").astype(str).ge(cutoff).sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Unique jobs", len(df))
        c2.metric("New in last 24h", new_24h)
        c3.metric("Companies", df["company"].replace("", pd.NA).nunique())
        c4.metric("With title", int(df["title"].fillna("").astype(str).str.strip().ne("").sum()))

        filter_text = st.text_input("Filter saved results", placeholder="title, company, location...")
        if filter_text:
            mask = df.astype(str).apply(
                lambda col: col.str.contains(re.escape(filter_text), case=False, na=False)
            ).any(axis=1)
            view = df[mask]
        else:
            view = df

        visible_columns = [
            "title", "company", "location", "employment_type",
            "date_posted", "first_seen", "job_url", "search_keyword",
        ]
        st.dataframe(
            view[visible_columns],
            use_container_width=True,
            hide_index=True,
            column_config={
                "job_url": st.column_config.LinkColumn("Job URL"),
            },
        )

        left, mid, right, clear_col = st.columns([1, 1, 1, 1])
        left.download_button(
            "Download Excel", data=export_xlsx_bytes(view.to_dict("records")),
            file_name="linkedin_jobs.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        mid.download_button(
            "Download CSV", data=export_csv(view.to_dict("records")),
            file_name="linkedin_jobs.csv", mime="text/csv", use_container_width=True,
        )
        right.download_button(
            "Download JSON", data=export_json(view.to_dict("records")),
            file_name="linkedin_jobs.json", mime="application/json", use_container_width=True,
        )
        if clear_col.button("Clear saved database", use_container_width=True):
            clear_records()
            st.rerun()

st.divider()
st.caption("Use responsibly. Results are limited to data exposed by permitted public pages; this is not a guarantee of complete LinkedIn coverage.")
