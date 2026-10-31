"""Shared LinkedIn scrape, scoring, and live UI helpers for the EM and TPM agents."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from llm import (
    analyze_job_fit,
    analyze_jobs_fit_batch,
    ensure_resume_context,
    llm_available,
    llm_provider_name,
    load_env,
)
from resume_extract import list_resume_summaries, load_profile, save_resume
from resume_tailor import load_tailored_resume, reassess_with_tailored_resume, tailor_resume_pdf

load_env()

SEM_SEARCH = "Software Engineering Manager"
TPM_SEARCH = "Technical Program Manager"
ALL_ROLES = [SEM_SEARCH, TPM_SEARCH]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = PROJECT_ROOT / "UI"
ASSETS_DIR = PROJECT_ROOT / "Assets"
BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = UI_DIR / "settings.json"
DEFAULT_TIME_RANGE = "week"
DEFAULT_MAX_PULLS = 50
MIN_MAX_PULLS = 5
MAX_MAX_PULLS = 200
TIME_RANGE_SECONDS = {
    "24h": 86400,
    "week": 604800,
}
INSTAHYRE_API = "https://www.instahyre.com/api/v1/job_search"
INSTAHYRE_SKILLS = {
    "em": "Engineering Manager",
    "tpm": "Technical Program Manager",
}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
UI_HOST = "127.0.0.1"
UI_PORT = 8765
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
TITLE_FIT_MAX = 20
MIN_RATING = 50
UI_STATE_LOCK = threading.Lock()
AGENT_LOCK = threading.Lock()
AGENT_PROCS: dict[str, subprocess.Popen] = {}

JOB_CARD_SELECTORS = [
    "div.base-card.job-search-card",
    "div.base-search-card",
    "div.job-search-card",
    "li.jobs-search-results__list-item",
    "ul.jobs-search__results-list > li",
]


def title_fit_sem(title: str) -> tuple[int, str]:
    t = title.lower()
    if "software engineering manager" in t or "engineering manager" in t:
        return TITLE_FIT_MAX, "title is an Engineering Manager role"
    if re.search(r"\bem\b", t):
        return 18, "title is Engineering Manager"
    if "technical lead" in t or "engineering lead" in t or "dev manager" in t:
        return 12, "title is a lead/manager-adjacent role"
    if "program manager" in t or "tpm" in t:
        return 6, "title is program management, not SEM"
    if "manager" in t:
        return 10, "title includes manager"
    return 4, "title is a weak SEM match"


def title_fit_tpm(title: str) -> tuple[int, str]:
    t = title.lower()
    if "technical program manager" in t or re.search(r"\btpm\b", t):
        return TITLE_FIT_MAX, "title is a Technical Program Manager role"
    if "technical program" in t:
        return 18, "title is a technical program role"
    if "program manager" in t:
        return 14, "title is Program Manager"
    if "project manager" in t or "delivery manager" in t:
        return 10, "title is delivery/project management"
    if "engineering manager" in t:
        return 8, "title is Engineering Manager, not TPM"
    if "product manager" in t:
        return 6, "title is Product Manager, not TPM"
    if "manager" in t:
        return 8, "title includes manager"
    return 4, "title is a weak TPM match"


ROLE_META = {
    "em": {
        "search": SEM_SEARCH,
        "label": "Engineering Manager",
        "title_fit_fn": title_fit_sem,
    },
    "tpm": {
        "search": TPM_SEARCH,
        "label": "Technical Program Manager",
        "title_fit_fn": title_fit_tpm,
    },
}


def load_agent_config(role_id: str) -> dict | None:
    """Build a scoring config from Assets/<role>. None if no resume is uploaded."""
    if role_id not in ROLE_META:
        raise ValueError("role_id must be em or tpm")
    resume_txt = ASSETS_DIR / role_id / "resume.txt"
    if not resume_txt.exists():
        return None
    profile = load_profile(role_id) or {}
    meta = ROLE_META[role_id]
    try:
        context = ensure_resume_context(role_id, meta["label"]) or {}
    except Exception as exc:
        context = {"method": "none", "summary": "", "error": str(exc)}
    groups = profile.get("skill_groups") or []
    gaps = [tuple(item) for item in profile.get("gap_signals") or [] if len(item) == 2]
    return {
        "role_id": role_id,
        "search": meta["search"],
        "label": meta["label"],
        "title_fit_fn": meta["title_fit_fn"],
        "skill_groups": groups,
        "gap_signals": gaps,
        "context": context,
        "resume_path": str(ASSETS_DIR / role_id / "resume.pdf"),
        "source_filename": profile.get("source_filename", "resume.pdf"),
    }


def score_job_keywords(title: str, description: str, config: dict) -> dict:
    blob = f"{title}\n{description}".lower()
    title_points, title_note = config["title_fit_fn"](title)
    matched: list[str] = []
    missed: list[str] = []
    skill_points = 0
    skill_groups = config.get("skill_groups") or []
    skill_max = sum(group["weight"] for group in skill_groups)

    for group in skill_groups:
        hits = [p for p in group["patterns"] if re.search(p, blob, flags=re.I)]
        if hits:
            skill_points += group["weight"]
            matched.append(group["name"])
        else:
            missed.append(group["name"])

    raw = title_points + skill_points
    total_max = TITLE_FIT_MAX + skill_max
    score = round(100 * raw / total_max) if total_max else 0
    if not description.strip():
        score = min(score, 45)

    gaps = [
        label
        for pattern, label in config.get("gap_signals", [])
        if re.search(pattern, blob, flags=re.I)
    ]
    return {
        "score": score,
        "title_note": title_note,
        "matched": matched,
        "missed": missed,
        "jd_gaps": gaps,
        "had_description": bool(description.strip()),
        "why": title_note,
        "reasoning": "",
        "note": "",
        "method": "keywords",
    }


def score_job(job: dict, description: str, config: dict) -> dict:
    context = config.get("context") or {}
    if llm_available() and context.get("method") == "llm" and context.get("summary"):
        try:
            return analyze_job_fit(
                config["role_id"],
                config["label"],
                context,
                job.get("title", ""),
                job.get("company", ""),
                description,
            )
        except Exception as exc:
            fallback = score_job_keywords(job.get("title", ""), description, config)
            fallback["note"] = f"LLM scoring failed ({exc}); used keyword fallback."
            return fallback
    return score_job_keywords(job.get("title", ""), description, config)


def score_jobs(jobs: list[dict], config: dict) -> None:
    context = config.get("context") or {}
    use_llm = llm_available() and context.get("method") == "llm" and context.get("summary")
    llm_results: list[dict | None] = [None] * len(jobs)
    if use_llm:
        try:
            llm_results = analyze_jobs_fit_batch(
                config["role_id"],
                config["label"],
                context,
                jobs,
            )
        except Exception:
            llm_results = [None] * len(jobs)
    for index, job in enumerate(jobs):
        result = llm_results[index] if index < len(llm_results) else None
        if result:
            job.update(result)
        else:
            fallback = score_job_keywords(job.get("title", ""), job.get("description", ""), config)
            fallback["note"] = "Missing from batch LLM response; used keyword fallback."
            job.update(fallback)


CHROME_PROFILE_ROOT = PROJECT_ROOT / ".chrome-profile"


def chrome_profile_dir(role_id: str) -> Path:
    return CHROME_PROFILE_ROOT / role_id
APPLY_URL_HINTS = (
    "apply",
    "career",
    "job",
    "greenhouse",
    "lever.co",
    "myworkday",
    "workday",
    "ashbyhq",
    "icims",
    "taleo",
    "smartrecruiters",
    "jobvite",
    "recruitee",
    "boards.greenhouse",
    "successfactors",
    "oraclecloud",
)


def is_linkedin_url(url: str) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return "linkedin.com" in host or "licdn.com" in host


def decode_embedded_url(raw: str) -> str:
    text = html.unescape(raw.strip().rstrip(".,);"))
    text = text.replace("\\u002f", "/").replace("\\/", "/")
    try:
        text = json.loads(f'"{text}"')
    except json.JSONDecodeError:
        pass
    if "linkedin.com/safety/go" in text or "linkedin.com/redir" in text:
        query = parse_qs(urlparse(text).query)
        for key in ("url", "dest", "u"):
            if query.get(key):
                return unquote(query[key][0])
    return text


def follow_apply_redirect(page, url: str) -> str:
    if not url or not is_linkedin_url(url):
        return url
    try:
        response = page.request.get(url, timeout=15000, max_redirects=10)
        final_url = response.url
        if final_url and not is_linkedin_url(final_url):
            return final_url
    except Exception:
        return url
    return url


def page_looks_like_easy_apply(page, html_source: str) -> bool:
    if re.search(
        r'"isEasyApply"\s*:\s*true|"easyApplyEnabled"\s*:\s*true|"easyApplyUrl"\s*:',
        html_source,
        flags=re.I,
    ):
        return True
    markers = [
        page.get_by_role("button", name=re.compile(r"easy apply", re.I)),
        page.get_by_role("link", name=re.compile(r"easy apply", re.I)),
        page.locator("[data-tracking-control-name*='easy_apply']"),
        page.locator("button:has-text('Easy Apply'), a:has-text('Easy Apply')"),
    ]
    for locator in markers:
        try:
            if locator.count() > 0:
                return True
        except Exception:
            continue
    return False


def collect_external_urls(html_source: str) -> list[str]:
    found: list[str] = []
    patterns = [
        r"https?://[^\s\"'<>\\]+",
        r"https:\\u002f\\u002f[^\s\"'\\]+",
    ]
    for pattern in patterns:
        found.extend(re.findall(pattern, html_source, flags=re.I))
    for pattern in [
        r'"companyApplyUrl"\s*:\s*"(.*?)"',
        r'"companyApplyInUrl"\s*:\s*"(.*?)"',
        r'"externalApplyUrl"\s*:\s*"(.*?)"',
        r'"applyUrl"\s*:\s*"(.*?)"',
        r'data-company-apply-url="(.*?)"',
    ]:
        found.extend(re.findall(pattern, html_source, flags=re.I))
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in found:
        url = decode_embedded_url(raw)
        if not url.startswith("http") or is_linkedin_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def pick_company_apply_url(candidates: list[str]) -> str:
    if not candidates:
        return ""

    def score(url: str) -> int:
        low = url.lower()
        points = 1
        for hint in APPLY_URL_HINTS:
            if hint in low:
                points += 4
        return points

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0]


def dismiss_apply_modal(page) -> None:
    for selector in [
        "button[aria-label='Dismiss']",
        "button.modal__dismiss",
        "button:has-text('Not now')",
        "button:has-text('Skip')",
    ]:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=1500)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def click_apply_for_company_url(page, linkedin_url: str) -> str:
    """Click Apply and keep the first non-LinkedIn URL that opens."""
    selectors = [
        "button.jobs-apply-button",
        "button.jobs-apply-button--top-card",
        "a.jobs-apply-button",
        "button:has-text('Apply on company website')",
        "a:has-text('Apply on company website')",
        "button:has-text('Apply')",
        "a.apply-button",
        "button.top-card-layout__cta--primary",
    ]
    origin = page.url
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0 or not locator.is_visible():
                continue
            context = page.context
            new_page = None
            try:
                with context.expect_page(timeout=4500) as popup:
                    locator.click(timeout=2500)
                new_page = popup.value
            except Exception:
                locator.click(timeout=2500)
            if new_page is not None:
                new_page.wait_for_load_state("domcontentloaded", timeout=12000)
                url = new_page.url
                new_page.close()
                if url.startswith("http") and not is_linkedin_url(url):
                    return url
            page.wait_for_timeout(800)
            if page.url.startswith("http") and not is_linkedin_url(page.url):
                found = page.url
                page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
                return found
            dismiss_apply_modal(page)
            if page.url != origin and is_linkedin_url(page.url):
                page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            dismiss_apply_modal(page)
            continue
    return ""


def extract_apply_target(page, linkedin_url: str) -> tuple[str, bool]:
    html_source = page.content()
    easy_apply = page_looks_like_easy_apply(page, html_source)
    if easy_apply:
        return linkedin_url, True

    company_url = pick_company_apply_url(collect_external_urls(html_source))
    if not company_url:
        for selector in [
            "a[data-tracking-control-name*='offsite']",
            "a.apply-button",
            "a[href*='externalApply']",
            "a[href*='/jobs/apply']",
            "a:has-text('Apply on company website')",
            "a:has-text('Apply')",
        ]:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                href = locator.get_attribute("href") or ""
                href = urljoin(linkedin_url, href)
                if href.startswith("http") and not is_linkedin_url(href):
                    company_url = href
                    break
                resolved = follow_apply_redirect(page, href)
                if resolved and not is_linkedin_url(resolved):
                    company_url = resolved
                    break
            except Exception:
                continue

    if not company_url:
        company_url = click_apply_for_company_url(page, linkedin_url)

    if company_url and not is_linkedin_url(company_url):
        return company_url, False
    return linkedin_url, easy_apply


def is_instahyre_url(url: str) -> bool:
    return bool(url) and "instahyre.com" in urlparse(url).netloc.lower()


def http_get(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={**BROWSER_HEADERS, "Accept": "application/json,text/html;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def strip_html_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def posted_in_selected_range(posted_at: str | None, source: str | None) -> bool:
    if source != "Instahyre" or not posted_at:
        return True
    try:
        posted = date.fromisoformat(posted_at[:10])
    except ValueError:
        return True
    days = 1 if load_settings()["time_range"] == "24h" else 7
    return posted >= date.today() - timedelta(days=days)


def instahyre_in_bangalore(locations: str) -> bool:
    text = (locations or "").lower()
    return "bangalore" in text or "bengaluru" in text


def extract_instahyre_jobs(role_id: str, title_fit_fn, limit: int | None = None) -> list[dict[str, str]]:
    skill = INSTAHYRE_SKILLS.get(role_id, "")
    search_query = ROLE_META[role_id]["search"]
    jobs: list[dict[str, str]] = []
    offset = 0
    pages = 0
    limit = max_pulls() if limit is None else max(0, int(limit))
    if limit == 0:
        return jobs
    while pages < 12 and len(jobs) < limit:
        query = urllib.parse.urlencode({"skills": skill, "offset": offset, "limit": 35})
        try:
            payload = json.loads(http_get(f"{INSTAHYRE_API}?{query}").decode("utf-8", errors="replace"))
        except Exception:
            break
        objects = payload.get("objects") or []
        if not objects:
            break
        for item in objects:
            title = (item.get("candidate_title") or item.get("title") or "").strip()
            locations = item.get("locations") or ""
            if not title or not instahyre_in_bangalore(locations):
                continue
            title_points, _ = title_fit_fn(title)
            if title_points < 10:
                continue
            employer = item.get("employer") or {}
            link = item.get("public_url") or ""
            jobs.append(
                {
                    "title": title,
                    "company": employer.get("company_name") or "Unknown company",
                    "link": link or "Link not found",
                    "applicants": "Not shown",
                    "search": search_query,
                    "source": "Instahyre",
                }
            )
            if len(jobs) >= limit:
                break
        pages += 1
        if len(objects) < 35:
            break
        offset += 35
    return jobs


def fetch_instahyre_details(url: str) -> dict:
    details = {"description": "", "apply_url": url, "easy_apply": False, "posted_at": ""}
    if not url or url == "Link not found":
        return details
    try:
        page_html = http_get(url).decode("utf-8", errors="replace")
    except Exception:
        return details
    for block in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        page_html,
        flags=re.S | re.I,
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        details["posted_at"] = str(data.get("datePosted") or "")
        details["description"] = strip_html_text(str(data.get("description") or ""))
        details["apply_url"] = url
        break
    return details


def linkedin_job_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"(\d{8,})$", path)
    return match.group(1) if match else ""


def fetch_linkedin_guest_details(url: str) -> dict:
    details = {"description": "", "apply_url": url.split("?")[0] if url else url, "easy_apply": False, "posted_at": ""}
    job_id = linkedin_job_id(url)
    if not job_id:
        return details
    try:
        page_html = http_get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        ).decode("utf-8", errors="replace")
    except Exception:
        return details
    match = re.search(
        r'<div[^>]*show-more-less-html__markup[\s\S]*?</div>',
        page_html,
        flags=re.I,
    )
    if match:
        details["description"] = strip_html_text(match.group(0))
    details["easy_apply"] = bool(re.search(r"easy apply", page_html, flags=re.I))
    return details


def fetch_job_details(page, url: str) -> dict:
    details = {"description": "", "apply_url": url, "easy_apply": False, "posted_at": ""}
    if not url or url == "Link not found":
        return details
    if is_instahyre_url(url):
        return fetch_instahyre_details(url)
    if is_linkedin_url(url):
        guest = fetch_linkedin_guest_details(url)
        if guest.get("description"):
            return guest
    if page is None:
        return details
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        for selector in [
            "div.show-more-less-html__markup",
            "div.description__text",
            "section.description",
            "div.jobs-description__content",
            "article",
        ]:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            text = locator.inner_text(timeout=3000).strip()
            if len(text) > 80:
                details["description"] = re.sub(r"\s+", " ", text)
                break
        details["apply_url"] = url
        details["easy_apply"] = page_looks_like_easy_apply(page, page.content())
    except Exception:
        return details
    return details


def attach_resume_scores(page, jobs: list[dict], config: dict) -> None:
    ready: list[dict] = []
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        publish_ui(ready, f"{config['label']}: fetching JDs ({index}/{total})", True, config["search"])
        details = fetch_job_details(page, job["link"])
        if not posted_in_selected_range(details.get("posted_at"), job.get("source")):
            continue
        job["description"] = details["description"]
        job["apply_url"] = details["apply_url"]
        job["easy_apply"] = details["easy_apply"]
        ready.append(job)
    publish_ui(ready, f"{config['label']}: scoring {len(ready)} jobs in one LLM request", True, config["search"])
    score_jobs(ready, config)
    publish_ui(ready, f"{config['label']}: scored {len(ready)} jobs", True, config["search"])
    jobs[:] = ready


def normalize_time_range(value: str | None) -> str:
    if value in TIME_RANGE_SECONDS:
        return value
    return DEFAULT_TIME_RANGE


def default_settings() -> dict:
    return {"time_range": DEFAULT_TIME_RANGE, "max_pulls": DEFAULT_MAX_PULLS}


def normalize_max_pulls(value) -> int:
    try:
        pulls = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PULLS
    return max(MIN_MAX_PULLS, min(MAX_MAX_PULLS, pulls))


def max_pulls() -> int:
    return load_settings()["max_pulls"]


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return default_settings()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    return {
        "time_range": normalize_time_range(data.get("time_range")),
        "max_pulls": normalize_max_pulls(data.get("max_pulls", DEFAULT_MAX_PULLS)),
    }


def save_settings(updates: dict) -> dict:
    with UI_STATE_LOCK:
        settings = load_settings()
        if "time_range" in updates:
            settings["time_range"] = normalize_time_range(updates.get("time_range"))
        if "max_pulls" in updates:
            settings["max_pulls"] = normalize_max_pulls(updates.get("max_pulls"))
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        return settings


def build_search_url(job_title: str) -> str:
    seconds = TIME_RANGE_SECONDS[load_settings()["time_range"]]
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(job_title)}"
        "&location=Bengaluru%2C%20Karnataka%2C%20India"
        "&geoId=105214831"
        f"&f_TPR=r{seconds}"
        "&position=1"
        "&pageNum=0"
    )


def extract_linkedin_guest_jobs(search_query: str, limit: int | None = None) -> list[dict[str, str]]:
    """Public LinkedIn job cards; works without a logged-in Chrome session."""
    seconds = TIME_RANGE_SECONDS[load_settings()["time_range"]]
    limit = max_pulls() if limit is None else max(0, int(limit))
    jobs: list[dict[str, str]] = []
    if limit == 0:
        return jobs
    seen: set[str] = set()
    for start in range(0, limit + 25, 25):
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote(search_query)}"
            "&location=Bengaluru%2C%20Karnataka%2C%20India"
            "&geoId=105214831"
            f"&f_TPR=r{seconds}"
            f"&start={start}"
        )
        try:
            page_html = http_get(url).decode("utf-8", errors="replace")
        except Exception:
            break
        chunks = re.split(r'class="[^"]*job-search-card', page_html)
        page_count = 0
        for chunk in chunks[1:]:
            title_match = re.search(r"base-search-card__title[^>]*>([\s\S]*?)</h3>", chunk)
            company_match = re.search(r"base-search-card__subtitle[^>]*>([\s\S]*?)</h4>", chunk)
            href_match = re.search(r'href="([^"]+/jobs/view/[^"]+)"', chunk)
            urn_match = re.search(r"urn:li:jobPosting:(\d+)", chunk)
            applicant_match = re.search(
                r"job-search-card__applicant-count[^>]*>([\s\S]*?)</span>",
                chunk,
            )
            title = strip_html_text(title_match.group(1) if title_match else "")
            if not title:
                continue
            company = strip_html_text(company_match.group(1) if company_match else "") or "Unknown company"
            if href_match:
                link = html.unescape(href_match.group(1)).split("?")[0]
            elif urn_match:
                link = f"https://www.linkedin.com/jobs/view/{urn_match.group(1)}"
            else:
                link = "Link not found"
            applicant_text = strip_html_text(applicant_match.group(1) if applicant_match else "") or "Not shown"
            if should_skip_job(applicant_text):
                continue
            key = link if link != "Link not found" else f"{title}|{company}"
            if key in seen:
                continue
            seen.add(key)
            jobs.append(
                {
                    "title": title,
                    "company": company,
                    "link": link,
                    "applicants": applicant_text,
                    "search": search_query,
                    "source": "LinkedIn",
                }
            )
            page_count += 1
            if len(jobs) >= limit:
                return jobs
        if page_count == 0:
            break
    return jobs


def parse_applicant_count(text: str) -> int | None:
    if not text:
        return None
    lowered = text.lower()
    if not any(word in lowered for word in ("applicant", "people clicked apply", "be among")):
        return None
    if "100+" in lowered or "over 100" in lowered or "100 plus" in lowered:
        return 100
    match = re.search(r"(\d[\d,]*)\s*\+?\s*(?:applicant|people clicked)", lowered)
    if match:
        return int(match.group(1).replace(",", ""))
    match = re.search(r"(?:applicant[s]?|people clicked apply)[^\d]{0,20}(\d[\d,]*)", lowered)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def should_skip_job(applicant_text: str) -> bool:
    count = parse_applicant_count(applicant_text)
    if count is None:
        return False
    return count > 99


def first_text(card, selectors: list[str]) -> str:
    for selector in selectors:
        locator = card.locator(selector).first
        if locator.count() == 0:
            continue
        text = locator.inner_text(timeout=2000).strip()
        if text:
            return re.sub(r"\s+", " ", text)
    return ""


def first_href(card, selectors: list[str]) -> str:
    for selector in selectors:
        locator = card.locator(selector).first
        if locator.count() == 0:
            continue
        href = locator.get_attribute("href")
        if href:
            return urljoin("https://www.linkedin.com", href.split("?")[0])
    return ""


def wait_for_job_cards(page):
    last_error = None
    for selector in JOB_CARD_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=6000)
            cards = page.locator(selector)
            if cards.count() > 0:
                return cards
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise PlaywrightTimeoutError(
        "Job listing cards never appeared. LinkedIn may be showing a login "
        "wall, a CAPTCHA, or a cookie banner. Accept cookies in the open "
        "browser window and try again."
    ) from last_error


def dismiss_cookie_banner(page) -> None:
    labels = ["Accept", "Accept all", "Accept cookies", "Allow all", "I agree", "OK"]
    for label in labels:
        button = page.get_by_role("button", name=re.compile(label, re.I))
        try:
            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=2000)
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def extract_jobs(page, search_query: str) -> list[dict[str, str]]:
    cards = wait_for_job_cards(page)
    page.wait_for_timeout(2000)
    jobs: list[dict[str, str]] = []
    for index in range(cards.count()):
        card = cards.nth(index)
        title = first_text(
            card,
            [
                "h3.base-search-card__title",
                "a.job-card-list__title--link",
                "a.job-card-container__link",
                "h3",
                "strong",
            ],
        )
        company = first_text(
            card,
            [
                "h4.base-search-card__subtitle",
                "a.hidden-nested-link",
                "span.job-card-container__primary-description",
                "h4",
            ],
        )
        link = first_href(
            card,
            [
                "a.base-card__full-link",
                "a.base-search-card__title",
                "a.job-card-list__title--link",
                "a.job-card-container__link",
                "a[href*='/jobs/view/']",
                "a[href*='/jobs/']",
            ],
        )
        applicant_text = first_text(
            card,
            [
                "span.job-search-card__applicant-count",
                "span.num-applicants__caption",
                "span.jobs-unified-top-card__applicant-count",
                ".job-search-card__listdate",
            ],
        )
        if not applicant_text:
            full_card_text = re.sub(r"\s+", " ", card.inner_text())
            match = re.search(
                r"(.{0,40}(?:applicant|people clicked apply).{0,20})",
                full_card_text,
                flags=re.I,
            )
            applicant_text = match.group(0).strip() if match else "Not shown"
        if not title or should_skip_job(applicant_text):
            continue
        jobs.append(
            {
                "title": title,
                "company": company or "Unknown company",
                "link": link or "Link not found",
                "applicants": applicant_text,
                "search": search_query,
                "source": "LinkedIn",
            }
        )
    return jobs


def job_to_ui_row(job: dict) -> dict:
    row = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "link": job.get("link", ""),
        "easy_apply": bool(job.get("easy_apply")),
        "rating": job.get("score"),
        "why": job.get("reasoning")
        or job.get("why")
        or " | ".join(
            part
            for part in [
                job.get("title_note", ""),
                f"Resume hits: {', '.join(job['matched'])}" if job.get("matched") else "",
            ]
            if part
        ),
        "weak_vs_jd": ", ".join(job.get("missed", [])[:6]),
        "note": " | ".join(
            part
            for part in [
                job.get("note", ""),
                (
                    "JD was hidden (login wall). Score used the title only."
                    if job.get("had_description") is False and not job.get("reasoning")
                    else (f"JD asks extra: {', '.join(job['jd_gaps'])}" if job.get("jd_gaps") and job.get("method") == "keywords" else "")
                ),
            ]
            if part
        ),
    }
    if job.get("reassessed"):
        row["reassessed"] = True
    return row


def write_jobs_snapshot(snapshot: dict) -> None:
    UI_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2)
    (UI_DIR / "jobs.json").write_text(payload, encoding="utf-8")
    (UI_DIR / "jobs-data.js").write_text(
        "window.JOB_DATA = " + json.dumps(snapshot.get("jobs") or {}, indent=2) + ";\n",
        encoding="utf-8",
    )


def update_ui_job_row(role_search: str, title: str, company: str, link: str, updates: dict) -> dict | None:
    with UI_STATE_LOCK:
        snapshot = read_jobs_file()
        rows = snapshot.get("jobs", {}).get(role_search) or []
        updated = None
        for row in rows:
            if row.get("title") != title or row.get("company") != company:
                continue
            if link and row.get("link") and row.get("link") != link:
                continue
            row.update(updates)
            updated = row
            break
        if updated is None:
            updated = {"title": title, "company": company, "link": link, "easy_apply": False}
            updated.update(updates)
            rows.append(updated)
        rows.sort(key=lambda job: job.get("rating") if job.get("rating") is not None else -1, reverse=True)
        snapshot["status"] = f"Re-analyzed {title} at {company} with tailored resume"
        write_jobs_snapshot(snapshot)
        return updated


def empty_jobs() -> dict[str, list]:
    return {role: [] for role in ALL_ROLES}


def read_jobs_file() -> dict:
    path = UI_DIR / "jobs.json"
    if not path.exists():
        return {"status": "Idle", "running": False, "running_by_role": {}, "jobs": empty_jobs()}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "Idle", "running": False, "running_by_role": {}, "jobs": empty_jobs()}
    jobs = data.get("jobs") or empty_jobs()
    for role in ALL_ROLES:
        jobs.setdefault(role, [])
    data["jobs"] = jobs
    data.setdefault("running_by_role", {})
    return data


def publish_ui(jobs: list[dict], status: str, running: bool, role: str) -> None:
    """Replace this role's table. The other role is left unchanged."""
    rows = []
    for job in jobs:
        score = job.get("score")
        if score is not None and score < MIN_RATING:
            continue
        rows.append(job_to_ui_row(job))
    rows.sort(key=lambda job: job.get("rating") if job.get("rating") is not None else -1, reverse=True)

    with UI_STATE_LOCK:
        snapshot = read_jobs_file()
        snapshot["jobs"][role] = rows
        running_by_role = snapshot.get("running_by_role") or {}
        running_by_role[role] = running
        snapshot["running_by_role"] = running_by_role
        snapshot["running"] = any(running_by_role.values())
        snapshot["status"] = status
        write_jobs_snapshot(snapshot)


def agent_script_path(role_id: str) -> Path:
    name = "em_agent.py" if role_id == "em" else "tpm_agent.py"
    return BACKEND_DIR / name


def agent_is_running(role_id: str) -> bool:
    with AGENT_LOCK:
        proc = AGENT_PROCS.get(role_id)
        return proc is not None and proc.poll() is None


def agent_run_status() -> dict[str, bool]:
    return {"em": agent_is_running("em"), "tpm": agent_is_running("tpm")}


def start_fresh_run(role_id: str) -> dict:
    if role_id not in ROLE_META:
        return {"ok": False, "error": "role must be em or tpm"}
    if not load_agent_config(role_id):
        return {"ok": False, "error": "Upload a resume for this role first."}
    if agent_is_running(role_id):
        return {"ok": False, "error": "A fresh run is already in progress for this role."}

    meta = ROLE_META[role_id]
    publish_ui([], f"{meta['label']}: starting fresh run", True, meta["search"])
    command = [sys.executable, "-u", str(agent_script_path(role_id))]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
    )
    with AGENT_LOCK:
        AGENT_PROCS[role_id] = proc
    return {"ok": True, "status": f"{meta['label']} agent started"}


class UiRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, format, *args):
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api/jobs", "/jobs.json"):
            with UI_STATE_LOCK:
                payload = read_jobs_file()
            payload["agent_procs"] = agent_run_status()
            payload["settings"] = load_settings()
            from orchestrator import chat_snapshot

            payload["chat"] = chat_snapshot()
            self._send_json(payload)
            return
        if path == "/api/settings":
            self._send_json(load_settings())
            return
        if path == "/api/resumes":
            self._send_json(list_resume_summaries())
            return
        if path == "/api/runs":
            self._send_json(agent_run_status())
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Invalid settings"}, 400)
                return
            if "time_range" in body and body["time_range"] not in TIME_RANGE_SECONDS:
                self._send_json({"error": "time_range must be 24h or week"}, 400)
                return
            if "max_pulls" in body:
                try:
                    int(body["max_pulls"])
                except (TypeError, ValueError):
                    self._send_json({"error": "max_pulls must be a number"}, 400)
                    return
            self._send_json(save_settings(body))
            return
        if parsed.path == "/api/tailor":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Invalid request"}, 400)
                return
            role = str(body.get("role") or "").lower()
            title = str(body.get("title") or "").strip()
            company = str(body.get("company") or "").strip()
            link = str(body.get("link") or "").strip()
            if role not in ROLE_META:
                self._send_json({"error": "role must be em or tpm"}, 400)
                return
            if not title or not company:
                self._send_json({"error": "title and company are required"}, 400)
                return
            details = fetch_job_details(None, link)
            try:
                result = tailor_resume_pdf(role, title, company, details.get("description") or "")
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result, 200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/reassess":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Invalid request"}, 400)
                return
            role = str(body.get("role") or "").lower()
            title = str(body.get("title") or "").strip()
            company = str(body.get("company") or "").strip()
            link = str(body.get("link") or "").strip()
            if role not in ROLE_META:
                self._send_json({"error": "role must be em or tpm"}, 400)
                return
            if not title or not company:
                self._send_json({"error": "title and company are required"}, 400)
                return
            if not load_tailored_resume(company, title):
                self._send_json({"error": "Tailor the resume for this job first."}, 400)
                return
            details = fetch_job_details(None, link)
            try:
                result = reassess_with_tailored_resume(
                    role, title, company, details.get("description") or ""
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            if result.get("ok"):
                updated = update_ui_job_row(
                    ROLE_META[role]["search"], title, company, link, result["row"]
                )
                if updated is None:
                    self._send_json({"error": "Could not find this job in the table."}, 400)
                    return
                result["row"] = updated
            self._send_json(result, 200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/chat":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Invalid request"}, 400)
                return
            from orchestrator import start_chat_instruction

            result = start_chat_instruction(
                str(body.get("message") or ""),
                str(body.get("role") or "").lower(),
            )
            self._send_json(result, 200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            role = (parse_qs(parsed.query).get("role") or [""])[0].lower()
            result = start_fresh_run(role)
            self._send_json(result, 200 if result.get("ok") else 400)
            return
        if parsed.path != "/api/resume":
            self.send_error(404)
            return
        role = (parse_qs(parsed.query).get("role") or [""])[0].lower()
        if role not in ROLE_META:
            self._send_json({"error": "role must be em or tpm"}, 400)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 8 or length > 20_000_000:
            self._send_json({"error": "Upload a PDF under 20MB"}, 400)
            return
        data = self.rfile.read(length)
        if not data.startswith(b"%PDF"):
            self._send_json({"error": "File must be a PDF"}, 400)
            return
        filename = self.headers.get("X-Filename") or "resume.pdf"
        try:
            summary = save_resume(role, data, filename)
        except Exception as exc:
            self._send_json({"error": f"Could not read PDF: {exc}"}, 400)
            return
        self._send_json(summary)


def ui_server_url() -> str:
    return f"http://{UI_HOST}:{UI_PORT}/"


def ui_server_is_up() -> bool:
    try:
        urllib.request.urlopen(ui_server_url() + "api/jobs", timeout=1)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_ui_server() -> tuple[ThreadingHTTPServer | None, str, bool]:
    UI_DIR.mkdir(parents=True, exist_ok=True)
    if ui_server_is_up():
        return None, ui_server_url(), False
    server = ThreadingHTTPServer((UI_HOST, UI_PORT), UiRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ui_server_url(), True


def open_in_chrome(url: str) -> None:
    subprocess.Popen([CHROME_PATH, url], close_fds=True)


def run_agent(role_id: str) -> int:
    meta = ROLE_META[role_id]
    role = meta["search"]
    label = meta["label"]
    server, ui_url, started_server = ensure_ui_server()
    print(f"{label} agent")
    print(f"Live UI: {ui_url}")
    if started_server:
        print("This process is serving the UI. Press Ctrl+C when you are done.\n")

    config = load_agent_config(role_id)
    if not config:
        print("No resume uploaded for this role.")
        print("Open the Resumes tab, upload a PDF, then run this agent again.\n")
        open_in_chrome(ui_url + "#/resumes")
        if started_server:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                if server is not None:
                    server.shutdown()
        return 1

    print(f"Resume: {config['source_filename']}")
    context = config.get("context") or {}
    if context.get("method") == "llm":
        print(f"Resume context: Assets/{config['role_id']}/context.json ({llm_provider_name()})")
    elif not llm_available():
        print("No LLM API key; scoring will use keywords. Add GROQ_API_KEY or GEMINI_API_KEY to .env")
    elif context.get("error"):
        print(f"Resume context error: {context['error']}")
    if started_server:
        open_in_chrome(ui_url + ("#/tpm" if role_id == "tpm" else "#/em"))
    else:
        print("UI server already running; this agent will only scrape.\n")

    publish_ui([], f"{label}: starting", True, role)

    with sync_playwright() as playwright:
        profile_dir = chrome_profile_dir(role_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        found: list[dict] = []
        seen: set[str] = set()
        pull_limit = max_pulls()
        try:
            publish_ui(found, f"{label}: searching LinkedIn (max {pull_limit})", True, role)
            try:
                for job in extract_linkedin_guest_jobs(role, pull_limit):
                    if len(found) >= pull_limit:
                        break
                    key = job["link"] if job["link"] != "Link not found" else f"{job['title']}|{job['company']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(job)
                    publish_ui(found, f"{label}: found {len(found)} listing(s)", True, role)
            except Exception as exc:
                publish_ui(found, f"{label}: LinkedIn guest search failed: {exc}", True, role)
                print(f"\nLinkedIn guest search failed.\n{exc}", file=sys.stderr)
            try:
                page.goto(build_search_url(role), wait_until="domcontentloaded", timeout=20000)
                dismiss_cookie_banner(page)
                for job in extract_jobs(page, role):
                    if len(found) >= pull_limit:
                        break
                    key = job["link"] if job["link"] != "Link not found" else f"{job['title']}|{job['company']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(job)
                    publish_ui(found, f"{label}: found {len(found)} listing(s)", True, role)
            except PlaywrightTimeoutError as exc:
                publish_ui(found, f"{label}: LinkedIn browser timed out; using guest results", True, role)
                print(f"\nLinkedIn browser timed out.\n{exc}", file=sys.stderr)

            publish_ui(found, f"{label}: searching Instahyre", True, role)
            try:
                remaining = max(0, pull_limit - len(found))
                for job in extract_instahyre_jobs(role_id, config["title_fit_fn"], remaining or 0):
                    if len(found) >= pull_limit:
                        break
                    key = job["link"] if job["link"] != "Link not found" else f"instahyre|{job['title']}|{job['company']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(job)
                    publish_ui(found, f"{label}: found {len(found)} listing(s)", True, role)
            except Exception as exc:
                publish_ui(found, f"{label}: Instahyre search failed: {exc}", True, role)
                print(f"\nInstahyre search failed.\n{exc}", file=sys.stderr)

            if not found:
                raise PlaywrightTimeoutError("No jobs found on LinkedIn or Instahyre.")
            attach_resume_scores(page, found, config)
            publish_ui(found, f"{label}: done.", False, role)
        except PlaywrightTimeoutError as exc:
            publish_ui(found, f"{label}: timed out: {exc}", False, role)
            print(f"\nTimed out waiting for the page.\n{exc}", file=sys.stderr)
            return 1
        finally:
            page.wait_for_timeout(2000)
            context.close()

    if not started_server:
        return 0
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        if server is not None:
            server.shutdown()
        return 0
