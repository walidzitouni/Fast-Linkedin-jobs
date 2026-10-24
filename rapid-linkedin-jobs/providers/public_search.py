"""Best-effort parser for publicly accessible LinkedIn job pages.

This module deliberately does not log in, use cookies, solve CAPTCHAs, rotate
proxies or user agents, or bypass access controls. It only reads public HTML
that LinkedIn serves without authentication:

- the normal ``/jobs/search/`` result pages,
- LinkedIn's public guest endpoints ``/jobs-guest/jobs/api/seeMoreJobPostings/search``
  and ``/jobs-guest/jobs/api/jobPosting/{id}``, which return the same job data
  a logged-out browser sees.

Anti-block strategy is *reduction and politeness*, not evasion: job cards are
parsed straight from the search listing (one request per results page instead
of one per job), every request is paced with a randomized delay, and the whole
run aborts as soon as LinkedIn answers with a blocking status code.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.linkedin.com"
JOB_URL_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)?linkedin\.com/jobs/view/[^/?#]+", re.I)
JOB_ID_RE = re.compile(r"(?:/jobs/view/(?:[^/?#]*-)?|jobPosting:)(\d+)", re.I)


def _is_linkedin_host(hostname: str | None) -> bool:
    return bool(hostname) and (hostname == "linkedin.com" or hostname.endswith(".linkedin.com"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BLOCK_STATUSES = {401, 403, 429, 999}


class PublicPageError(RuntimeError):
    pass


class BlockedError(PublicPageError):
    """LinkedIn is rate-limiting or blocking automated access; stop the run."""


class PoliteClient:
    """Small HTTP client with randomized pacing and a block circuit breaker.

    One consistent header set is used for the whole run (no fingerprint
    rotation). The first sign of blocking triggers a single long backoff; a
    second one raises BlockedError so callers stop instead of hammering.
    """

    def __init__(self, min_delay: float = 3.0, max_delay: float = 7.0,
                 timeout: int = 15) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.min_delay = max(0.5, float(min_delay))
        self.max_delay = max(self.min_delay, float(max_delay))
        self.timeout = timeout
        self.requests_made = 0

    def _pace(self) -> None:
        if self.requests_made:
            time.sleep(random.uniform(self.min_delay, self.max_delay))

    def get(self, url: str, referer: str | None = None) -> requests.Response:
        headers = {"Referer": referer} if referer else None
        for attempt in (1, 2):
            self._pace()
            try:
                response = self.session.get(
                    url, headers=headers, timeout=self.timeout, allow_redirects=True,
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise PublicPageError(f"Network error: {exc}") from exc
                time.sleep(random.uniform(5, 10))
                continue
            self.requests_made += 1
            if response.status_code in BLOCK_STATUSES:
                if attempt == 2:
                    raise BlockedError(
                        f"LinkedIn returned HTTP {response.status_code}: automated "
                        "access is being blocked. The run stopped to protect your "
                        "connection; wait a few hours before scanning again."
                    )
                time.sleep(random.uniform(20, 40))
                continue
            if response.status_code >= 400:
                raise PublicPageError(f"LinkedIn returned HTTP {response.status_code}.")
            final = urlparse(response.url)
            if not _is_linkedin_host(final.hostname):
                raise PublicPageError("Unexpected redirect away from LinkedIn; not parsed.")
            return response
        raise PublicPageError("Unreachable request state.")  # pragma: no cover


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _search_params(keyword: str, location: str, date_filter: str,
                   employment_types: list[str], remote_types: list[str],
                   start: int = 0) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "keywords": keyword.strip(),
        "location": location.strip(),
        "start": max(0, start),
    }
    if date_filter == "Past 24 hours":
        params["f_TPR"] = "r86400"
    elif date_filter == "Past week":
        params["f_TPR"] = "r604800"
    elif date_filter == "Past month":
        params["f_TPR"] = "r2592000"

    type_map = {
        "Full-time": "F", "Part-time": "P", "Contract": "C",
        "Temporary": "T", "Internship": "I", "Apprenticeship": "O",
    }
    selected = [type_map[x] for x in employment_types if x in type_map]
    if selected:
        params["f_JT"] = ",".join(selected)

    remote_map = {"On-site": "1", "Remote": "2", "Hybrid": "3"}
    selected_remote = [remote_map[x] for x in remote_types if x in remote_map]
    if selected_remote:
        params["f_WT"] = ",".join(selected_remote)
    return params


def build_search_url(keyword: str, location: str, date_filter: str,
                     employment_types: list[str], remote_types: list[str],
                     start: int = 0) -> str:
    """Build a normal LinkedIn jobs-search URL for browser/manual use."""
    params = _search_params(keyword, location, date_filter, employment_types,
                            remote_types, start)
    return f"{BASE}/jobs/search/?{urlencode(params)}"


def build_guest_search_url(keyword: str, location: str, date_filter: str,
                           employment_types: list[str], remote_types: list[str],
                           start: int = 0) -> str:
    """Public guest endpoint returning only job-card HTML (no login needed)."""
    params = _search_params(keyword, location, date_filter, employment_types,
                            remote_types, start)
    return f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search?{urlencode(params)}"


def _node_text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _canonical_job_url(href: str, job_id: str = "") -> str:
    parsed = urlparse(href)
    if job_id:
        return f"{BASE}/jobs/view/{job_id}"
    return f"{BASE}{parsed.path.rstrip('/')}/"


def parse_job_cards(html: str, keyword: str = "") -> list[dict[str, Any]]:
    """Extract job cards from search-results HTML (guest endpoint or full page).

    Cards carry title, company, location, posting date and the job URL, which
    is enough to list offers without requesting each job page individually.
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for card in soup.select("div.base-card"):
        link = card.select_one("a.base-card__full-link")
        href = (link.get("href", "") if link else "").strip()
        if not JOB_URL_RE.match(href):
            continue
        urn = card.get("data-entity-urn", "") or ""
        match = JOB_ID_RE.search(urn) or JOB_ID_RE.search(href)
        job_id = match.group(1) if match else ""
        time_node = card.select_one("time")
        records.append({
            "title": _node_text(card.select_one("h3.base-search-card__title")),
            "company": _node_text(card.select_one("h4.base-search-card__subtitle")),
            "location": _node_text(card.select_one("span.job-search-card__location")),
            "description": "",
            "employment_type": "",
            "date_posted": (time_node.get("datetime", "") if time_node else ""),
            "job_url": _canonical_job_url(href, job_id),
            "source": "LinkedIn public search",
            "fetched_at": _utcnow(),
            "search_keyword": keyword,
            "job_id": job_id,
        })
    return records


def fetch_job_details(client: PoliteClient, job_id: str,
                      referer: str | None = None) -> dict[str, str]:
    """Fetch full description + criteria from the public guest job endpoint."""
    url = f"{BASE}/jobs-guest/jobs/api/jobPosting/{job_id}"
    response = client.get(url, referer=referer)
    soup = BeautifulSoup(response.text, "html.parser")
    details: dict[str, str] = {}

    desc_node = (soup.select_one("div.show-more-less-html__markup")
                 or soup.select_one("div.description__text"))
    if desc_node:
        details["description"] = desc_node.get_text(" ", strip=True)

    for item in soup.select("li.job-criteria-item"):
        header = _node_text(item.select_one("h3.job-criteria-subheader")).lower()
        value = _node_text(item.select_one("span.job-criteria-text"))
        if "employment type" in header and value:
            details["employment_type"] = value
        elif "seniority" in header and value:
            details["seniority_level"] = value

    if not details.get("description"):
        raise PublicPageError("Guest job page exposed no description.")
    return details


def _walk_jsonld(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_jsonld(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_jsonld(item)


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if text:
                return str(text).strip()
    return ""


def parse_job_page(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    record: dict[str, Any] = {
        "title": "",
        "company": "",
        "location": "",
        "description": "",
        "employment_type": "",
        "date_posted": "",
        "job_url": url,
        "source": "LinkedIn public page",
        "fetched_at": _utcnow(),
    }

    # Prefer structured JobPosting data if the page exposes it.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk_jsonld(data):
            kind = obj.get("@type", [])
            if isinstance(kind, str):
                kind = [kind]
            if "JobPosting" not in kind:
                continue
            record["title"] = obj.get("title") or record["title"]
            org = obj.get("hiringOrganization") or {}
            if isinstance(org, dict):
                record["company"] = org.get("name") or record["company"]
            loc = obj.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            if isinstance(loc, dict):
                address = loc.get("address") or {}
                if isinstance(address, dict):
                    parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
                    record["location"] = ", ".join(str(x) for x in parts if x) or record["location"]
            record["description"] = BeautifulSoup(str(obj.get("description", "")), "html.parser").get_text(" ", strip=True)
            record["employment_type"] = ", ".join(obj.get("employmentType", [])) if isinstance(obj.get("employmentType"), list) else (obj.get("employmentType") or "")
            record["date_posted"] = obj.get("datePosted") or ""
            break

    # Public page metadata fallbacks.
    record["title"] = record["title"] or _first_text(soup, [
        'meta[property="og:title"]', "h1.top-card-layout__title",
        "h1.topcard__title", "h1",
    ])
    record["company"] = record["company"] or _first_text(soup, [
        'meta[name="author"]', ".topcard__org-name-link",
        ".top-card-layout__card a.topcard__org-name-link",
    ])
    record["description"] = record["description"] or _first_text(soup, [
        'meta[property="og:description"]', ".description__text",
        ".show-more-less-html__markup",
    ])
    record["location"] = record["location"] or _first_text(soup, [
        ".topcard__flavor--bullet", ".top-card-layout__second-subline",
    ])
    return record


def fetch_public_job(url: str, timeout: int = 12,
                     client: PoliteClient | None = None) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _is_linkedin_host(parsed.hostname):
        raise PublicPageError("Only https://www.linkedin.com job URLs are accepted.")
    if not parsed.path.startswith("/jobs/view/"):
        raise PublicPageError("URL must be a LinkedIn /jobs/view/ page.")

    client = client or PoliteClient(min_delay=0.5, max_delay=1.0, timeout=timeout)
    response = client.get(url, referer=f"{BASE}/jobs/search/")
    return parse_job_page(response.text, response.url)


def find_job_links_in_html(html: str) -> list[str]:
    """Extract job-view links only when they are present in returned HTML."""
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for node in soup.select("a[href]"):
        href = node.get("href", "").strip()
        if href.startswith("/jobs/view/"):
            href = BASE + href
        if JOB_URL_RE.match(href):
            parsed = urlparse(href)
            links.add(f"https://www.linkedin.com{parsed.path.rstrip('/')}/")
    return sorted(links)


def fetch_search_page(url: str, timeout: int = 12,
                      client: PoliteClient | None = None) -> tuple[list[str], str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _is_linkedin_host(parsed.hostname):
        raise PublicPageError("Only LinkedIn HTTPS URLs are accepted.")
    if not parsed.path.startswith("/jobs/search"):
        raise PublicPageError("URL must be a LinkedIn jobs-search URL.")
    client = client or PoliteClient(min_delay=0.5, max_delay=1.0, timeout=timeout)
    response = client.get(url)
    links = find_job_links_in_html(response.text)
    return links, response.text


def search_public_jobs(keyword: str, location: str, date_filter: str,
                       employment_types: list[str], remote_types: list[str],
                       pages: int = 1, page_size: int = 25,
                       progress=None,
                       client: PoliteClient | None = None,
                       known_urls: set[str] | None = None,
                       fetch_descriptions: bool = True,
                       desc_budget: dict | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Search public listing pages and return job records.

    Job cards are parsed directly from the search listing, so coverage costs
    one request per results page. Full descriptions are fetched only for jobs
    not in ``known_urls`` and only while ``desc_budget['left']`` allows.
    """
    client = client or PoliteClient()
    known_urls = known_urls or set()
    budget = desc_budget if desc_budget is not None else {"left": 10**9}
    pages = max(1, min(pages, 10))

    browser_url = build_search_url(keyword, location, date_filter,
                                   employment_types, remote_types)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    warnings: list[str] = []

    for page in range(pages):
        start = page * page_size
        guest_url = build_guest_search_url(keyword, location, date_filter,
                                           employment_types, remote_types, start)
        try:
            response = client.get(guest_url, referer=browser_url)
            cards = parse_job_cards(response.text, keyword=keyword)
            if not cards and page == 0:
                # Fallback: the regular search page carries the same cards.
                response = client.get(browser_url)
                cards = parse_job_cards(response.text, keyword=keyword)
        except BlockedError:
            raise
        except PublicPageError as exc:
            warnings.append(f"{keyword} / page {page + 1}: {exc}")
            break

        if progress:
            progress(f"{keyword} · page {page + 1}: {len(cards)} job card(s)")
        if not cards:
            break

        for card in cards:
            url = card["job_url"]
            if url in seen:
                continue
            seen.add(url)
            already_known = url in known_urls
            if (fetch_descriptions and not already_known
                    and card.get("job_id") and budget["left"] > 0):
                budget["left"] -= 1
                try:
                    details = fetch_job_details(client, card["job_id"],
                                                referer=browser_url)
                    card.update(details)
                    if progress:
                        progress(f"{keyword} · description {card['title'] or card['job_id']}")
                except BlockedError:
                    raise
                except PublicPageError as exc:
                    warnings.append(f"{card['job_url']}: {exc}")
            card.pop("job_id", None)
            records.append(card)
    return records, warnings
