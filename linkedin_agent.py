"""
Beginner LinkedIn job scout (Playwright, synchronous).

Opens a visible Chromium window, loads the public job search for
Software Engineering Manager and Technical Program Manager roles in
Bengaluru, India posted in the past week, then prints roles that do
not look like they already have 100+ applicants.

Software Engineering Manager jobs are scored 0-100 against
Resume_Updated_09_22.pdf. Technical Program Manager jobs are scored
against Arvin_Anthony_DSouza_TPM_Resume.pdf. Results stream to a local UI server at http://127.0.0.1:8765/ while
the scraper runs. The page polls GET /api/jobs every couple of seconds.

Note: LinkedIn often shows a sign-in wall or cookie banner. Keep the
browser window in view so you can accept cookies if asked. This script
only reads the public listings page; it does not log in or apply.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Public jobs search (not the homepage).
# Bengaluru, India (geoId=105214831) + past week (f_TPR=r604800).
JOB_TITLES = [
    "Software Engineering Manager",
    "Technical Program Manager",
]
SEM_SEARCH = "Software Engineering Manager"
TPM_SEARCH = "Technical Program Manager"
SEM_RESUME_PATH = r"C:\Users\Arvin\Downloads\Resume_Updated_09_22.pdf"
TPM_RESUME_PATH = r"C:\Users\Arvin\Downloads\Arvin_Anthony_DSouza_TPM_Resume.pdf"
UI_DIR = Path(__file__).resolve().parent / "UI"
UI_HOST = "127.0.0.1"
UI_PORT_START = 8765
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
UI_STATE_LOCK = threading.Lock()
UI_STATE: dict = {
    "status": "Starting",
    "running": False,
    "jobs": {
        "Software Engineering Manager": [],
        "Technical Program Manager": [],
    },
}

# Skills taken from Resume_Updated_09_22.pdf (SEM + Technical Lead background).
# Each group can add up to `weight` points. Title fit is scored separately.
SEM_SKILL_GROUPS = [
    {
        "name": "C# / .NET",
        "weight": 12,
        "patterns": [r"\bc#\b", r"c sharp", r"\.net", r"dotnet", r"asp\.net"],
    },
    {
        "name": "WPF / desktop apps",
        "weight": 6,
        "patterns": [r"\bwpf\b", r"winforms", r"windows desktop", r"desktop application"],
    },
    {
        "name": "SQL / data",
        "weight": 5,
        "patterns": [r"\bsql\b", r"database", r"postgresql", r"sql server"],
    },
    {
        "name": "Angular / React / TypeScript",
        "weight": 6,
        "patterns": [r"angular", r"react", r"typescript", r"\bhtml\b", r"\bcss\b"],
    },
    {
        "name": "Azure DevOps / Agile",
        "weight": 8,
        "patterns": [r"azure devops", r"\badop\b", r"\bagile\b", r"\bscrum\b", r"sprint", r"backlog"],
    },
    {
        "name": "Cloud (GCP / Alibaba / Azure)",
        "weight": 6,
        "patterns": [r"google cloud", r"\bgcp\b", r"alibaba", r"\bazure\b", r"cloud"],
    },
    {
        "name": "People / team leadership",
        "weight": 12,
        "patterns": [
            r"people management",
            r"engineering manager",
            r"team lead",
            r"mentorship",
            r"mentor",
            r"hiring",
            r"talent",
            r"direct report",
            r"people manager",
        ],
    },
    {
        "name": "Program / delivery management",
        "weight": 8,
        "patterns": [
            r"program management",
            r"program manager",
            r"cross-functional",
            r"roadmap",
            r"stakeholder",
            r"capacity planning",
        ],
    },
    {
        "name": "Architecture",
        "weight": 5,
        "patterns": [r"architect", r"scalable", r"system design", r"technical liaison"],
    },
    {
        "name": "Computer vision / ML",
        "weight": 6,
        "patterns": [
            r"computer vision",
            r"\bcv\b",
            r"machine learning",
            r"\bml\b",
            r"onnx",
            r"inference",
        ],
    },
    {
        "name": "Industrial / energy domain",
        "weight": 6,
        "patterns": [
            r"scada",
            r"industrial automation",
            r"oil",
            r"\bgas\b",
            r"energy",
            r"oilfield",
            r"upstream",
        ],
    },
]

TITLE_FIT_MAX = 20

SEM_GAP_SIGNALS = [
    (r"\bjava\b", "Java"),
    (r"\bpython\b", "Python"),
    (r"\bgolang\b|\bgo lang\b", "Go"),
    (r"kubernetes|\bk8s\b", "Kubernetes"),
    (r"\baws\b", "AWS"),
    (r"\bmicroservices\b", "Microservices"),
    (r"\bkafka\b", "Kafka"),
    (r"\bfintech\b", "Fintech"),
    (r"\bsaas\b", "SaaS"),
]

# Skills taken from Arvin_Anthony_DSouza_TPM_Resume.pdf.
TPM_SKILL_GROUPS = [
    {
        "name": "End-to-end program lifecycle",
        "weight": 12,
        "patterns": [
            r"end-to-end",
            r"program lifecycle",
            r"program delivery",
            r"program ownership",
            r"delivery lifecycle",
        ],
    },
    {
        "name": "Cross-functional / stakeholder leadership",
        "weight": 12,
        "patterns": [
            r"cross-functional",
            r"cross functional",
            r"stakeholder",
            r"executive",
            r"liaison",
            r"distributed team",
        ],
    },
    {
        "name": "Agile / Scrum / Azure DevOps",
        "weight": 10,
        "patterns": [
            r"azure devops",
            r"\badop\b",
            r"\bagile\b",
            r"\bscrum\b",
            r"sprint",
            r"backlog",
        ],
    },
    {
        "name": "Roadmap / milestones / governance",
        "weight": 8,
        "patterns": [r"roadmap", r"milestone", r"governance", r"critical path", r"on-time"],
    },
    {
        "name": "Dependencies / risk / delivery metrics",
        "weight": 8,
        "patterns": [r"dependenc", r"risk", r"mitigat", r"bottleneck", r"metrics"],
    },
    {
        "name": "Capacity / resource / budget",
        "weight": 7,
        "patterns": [
            r"capacity planning",
            r"resource allocation",
            r"resource scaling",
            r"budget",
            r"operational overhead",
        ],
    },
    {
        "name": "Pre-sales / scoping / system design",
        "weight": 6,
        "patterns": [
            r"pre-sales",
            r"presales",
            r"scoping",
            r"system design",
            r"initiation",
            r"requirements",
        ],
    },
    {
        "name": "AI/ML / computer vision",
        "weight": 6,
        "patterns": [
            r"machine learning",
            r"\bml\b",
            r"\bai\b",
            r"automl",
            r"computer vision",
            r"onnx",
        ],
    },
    {
        "name": "Industrial / energy / SCADA",
        "weight": 6,
        "patterns": [
            r"industrial automation",
            r"scada",
            r"oil",
            r"\bgas\b",
            r"energy",
            r"enterprise software",
        ],
    },
    {
        "name": "Cloud (GCP / Alibaba)",
        "weight": 5,
        "patterns": [r"google cloud", r"\bgcp\b", r"alibaba", r"cloud infrastructure", r"\bcloud\b"],
    },
]

TPM_GAP_SIGNALS = [
    (r"\bjira\b", "Jira"),
    (r"\bpmp\b|\bpmi\b", "PMP/PMI"),
    (r"\bsafe agile\b|\bscaled agile\b", "SAFe"),
    (r"\bpmo\b", "PMO"),
    (r"\baws\b", "AWS"),
    (r"kubernetes|\bk8s\b", "Kubernetes"),
    (r"\bjava\b", "Java"),
    (r"\bfintech\b", "Fintech"),
    (r"\bsilicon\b|\bsemiconductor\b", "Semiconductor"),
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


def score_job(
    title: str,
    description: str,
    skill_groups: list[dict],
    title_fit_fn,
    gap_signals: list[tuple[str, str]],
) -> dict:
    blob = f"{title}\n{description}".lower()
    title_points, title_note = title_fit_fn(title)

    matched: list[str] = []
    missed: list[str] = []
    skill_points = 0
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

    gaps = [label for pattern, label in gap_signals if re.search(pattern, blob, flags=re.I)]
    return {
        "score": score,
        "title_note": title_note,
        "matched": matched,
        "missed": missed,
        "jd_gaps": gaps,
        "had_description": bool(description.strip()),
    }


SCORE_PROFILES = {
    SEM_SEARCH: {
        "resume_path": SEM_RESUME_PATH,
        "skill_groups": SEM_SKILL_GROUPS,
        "title_fit_fn": title_fit_sem,
        "gap_signals": SEM_GAP_SIGNALS,
        "label": "Software Engineering Manager",
    },
    TPM_SEARCH: {
        "resume_path": TPM_RESUME_PATH,
        "skill_groups": TPM_SKILL_GROUPS,
        "title_fit_fn": title_fit_tpm,
        "gap_signals": TPM_GAP_SIGNALS,
        "label": "Technical Program Manager",
    },
}


def is_linkedin_url(url: str) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return "linkedin.com" in host


def decode_embedded_url(raw: str) -> str:
    text = html.unescape(raw.strip())
    try:
        text = json.loads(f'"{text}"')
    except json.JSONDecodeError:
        text = text.replace("\\/", "/")
    if "linkedin.com/safety/go" in text or "linkedin.com/redir" in text:
        query = parse_qs(urlparse(text).query)
        for key in ("url", "dest", "u"):
            if query.get(key):
                return unquote(query[key][0])
    return text


def follow_apply_redirect(page, url: str) -> str:
    """Resolve LinkedIn apply redirects to the company site without clicking Apply."""
    if not url or not is_linkedin_url(url):
        return url
    try:
        response = page.request.get(url, timeout=15000)
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


def extract_apply_target(page, linkedin_url: str) -> tuple[str, bool]:
    """Return (apply_url, easy_apply). Keep LinkedIn only for Easy Apply."""
    html_source = page.content()
    easy_apply = page_looks_like_easy_apply(page, html_source)

    company_url = ""
    for pattern in [
        r'"companyApplyUrl"\s*:\s*"(.*?)"',
        r'"companyApplyInUrl"\s*:\s*"(.*?)"',
        r'"externalApplyUrl"\s*:\s*"(.*?)"',
        r'"applyUrl"\s*:\s*"(.*?)"',
        r'data-company-apply-url="(.*?)"',
    ]:
        match = re.search(pattern, html_source, flags=re.I)
        if not match:
            continue
        candidate = decode_embedded_url(match.group(1))
        if candidate.startswith("http") and not is_linkedin_url(candidate):
            company_url = candidate
            break
        if candidate.startswith("http") and is_linkedin_url(candidate):
            resolved = follow_apply_redirect(page, candidate)
            if resolved and not is_linkedin_url(resolved):
                company_url = resolved
                break

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
                if not href.startswith("http"):
                    continue
                if not is_linkedin_url(href):
                    company_url = href
                    break
                resolved = follow_apply_redirect(page, href)
                if resolved and not is_linkedin_url(resolved):
                    company_url = resolved
                    break
            except Exception:
                continue

    if company_url:
        return company_url, False
    return linkedin_url, easy_apply


def fetch_job_details(page, url: str) -> dict:
    """Open a public job page and collect description plus the real apply URL."""
    details = {"description": "", "apply_url": url, "easy_apply": False}
    if not url or url == "Link not found":
        return details
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        apply_url, easy_apply = extract_apply_target(page, url)
        details["apply_url"] = apply_url
        details["easy_apply"] = easy_apply
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
    except Exception:
        return details
    return details


def attach_resume_scores(page, jobs: list[dict], search_query: str) -> None:
    """Visit listings for one search and attach a 0-100 resume skills score."""
    profile = SCORE_PROFILES[search_query]
    target_jobs = [job for job in jobs if job.get("search") == search_query]
    if not target_jobs:
        return

    total = len(target_jobs)
    for index, job in enumerate(target_jobs, start=1):
        publish_ui(jobs, f"Scoring {profile['label']} ({index}/{total})")
        details = fetch_job_details(page, job["link"])
        job["description"] = details["description"]
        job["apply_url"] = details["apply_url"]
        job["easy_apply"] = details["easy_apply"]
        job.update(
            score_job(
                job["title"],
                details["description"],
                skill_groups=profile["skill_groups"],
                title_fit_fn=profile["title_fit_fn"],
                gap_signals=profile["gap_signals"],
            )
        )
        publish_ui(jobs, f"Scored {profile['label']} ({index}/{total})")


def build_search_url(job_title: str) -> str:
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(job_title)}"
        "&location=Bengaluru%2C%20Karnataka%2C%20India"
        "&geoId=105214831"
        "&f_TPR=r604800"
        "&position=1"
        "&pageNum=0"
    )

JOB_CARD_SELECTORS = [
    "div.base-card.job-search-card",
    "div.base-search-card",
    "div.job-search-card",
    "li.jobs-search-results__list-item",
    "ul.jobs-search__results-list > li",
]


def parse_applicant_count(text: str) -> int | None:
    """Return the applicant number if the text looks like applicant metadata."""
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
    """Skip jobs that already have 100+ applicants (or any count over 99)."""
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
            clean = href.split("?")[0]
            return urljoin("https://www.linkedin.com", clean)
    return ""


def wait_for_job_cards(page):
    last_error = None
    for selector in JOB_CARD_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=15000)
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
    """Click common cookie buttons if they show up. Safe to ignore if missing."""
    labels = [
        "Accept",
        "Accept all",
        "Accept cookies",
        "Allow all",
        "I agree",
        "OK",
    ]
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
    # Give lazy-loaded metadata a moment to fill in.
    page.wait_for_timeout(2000)

    jobs: list[dict[str, str]] = []
    total = cards.count()

    for index in range(total):
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
            # Fall back to any text on the card that mentions applicants.
            full_card_text = re.sub(r"\s+", " ", card.inner_text())
            match = re.search(
                r"(.{0,40}(?:applicant|people clicked apply).{0,20})",
                full_card_text,
                flags=re.I,
            )
            applicant_text = match.group(0).strip() if match else "Not shown"

        if not title:
            continue

        if should_skip_job(applicant_text):
            continue

        jobs.append(
            {
                "title": title,
                "company": company or "Unknown company",
                "link": link or "Link not found",
                "applicants": applicant_text,
                "search": search_query,
            }
        )

    return jobs


def job_to_ui_row(job: dict) -> dict:
    return {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "link": job.get("apply_url") or job.get("link", ""),
        "easy_apply": bool(job.get("easy_apply")),
        "rating": job.get("score"),
        "why": " | ".join(
            part
            for part in [
                job.get("title_note", ""),
                (
                    f"Resume hits: {', '.join(job['matched'])}"
                    if job.get("matched")
                    else ""
                ),
            ]
            if part
        ),
        "weak_vs_jd": ", ".join(job.get("missed", [])[:6]),
        "note": (
            "JD was hidden (login wall). Score used the title only."
            if job.get("had_description") is False
            else (
                f"JD asks extra: {', '.join(job['jd_gaps'])}"
                if job.get("jd_gaps")
                else ""
            )
        ),
    }


def ui_snapshot(jobs: list[dict], status: str, running: bool) -> dict:
    payload_jobs: dict[str, list[dict]] = {}
    for search_query in JOB_TITLES:
        group = [job for job in jobs if job.get("search") == search_query]
        group.sort(key=lambda job: job.get("score", -1), reverse=True)
        payload_jobs[search_query] = [job_to_ui_row(job) for job in group]
    return {"status": status, "running": running, "jobs": payload_jobs}


def publish_ui(jobs: list[dict], status: str, running: bool = True) -> None:
    """Update in-memory API state and write the UI data files."""
    snapshot = ui_snapshot(jobs, status, running)
    with UI_STATE_LOCK:
        UI_STATE.clear()
        UI_STATE.update(snapshot)

    UI_DIR.mkdir(parents=True, exist_ok=True)
    (UI_DIR / "jobs.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    jobs_only = snapshot["jobs"]
    (UI_DIR / "jobs-data.js").write_text(
        "window.JOB_DATA = " + json.dumps(jobs_only, indent=2) + ";\n",
        encoding="utf-8",
    )


class UiRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api/jobs", "/jobs.json"):
            with UI_STATE_LOCK:
                body = json.dumps(UI_STATE).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def start_ui_server() -> tuple[ThreadingHTTPServer, str]:
    UI_DIR.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for port in range(UI_PORT_START, UI_PORT_START + 15):
        try:
            server = ThreadingHTTPServer((UI_HOST, port), UiRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://{UI_HOST}:{port}/"
            return server, url
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"Could not start UI server: {last_error}") from last_error


def open_in_chrome(url: str) -> None:
    subprocess.Popen([CHROME_PATH, url], close_fds=True)


def main() -> int:
    publish_ui([], "Starting UI server", running=True)
    server, ui_url = start_ui_server()
    print(f"Live UI: {ui_url}")
    print("Leave this window open. Press Ctrl+C when you are done viewing results.\n")
    open_in_chrome(ui_url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        all_jobs: list[dict[str, str]] = []
        seen_links: set[str] = set()

        try:
            for index, job_title in enumerate(JOB_TITLES):
                publish_ui(all_jobs, f"Searching {job_title}")
                search_url = build_search_url(job_title)
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                if index == 0:
                    dismiss_cookie_banner(page)
                for job in extract_jobs(page, job_title):
                    key = job["link"] if job["link"] != "Link not found" else f"{job['title']}|{job['company']}"
                    if key in seen_links:
                        continue
                    seen_links.add(key)
                    all_jobs.append(job)
                    publish_ui(all_jobs, f"Found {len(all_jobs)} listing(s); searching {job_title}")
            for search_query in SCORE_PROFILES:
                attach_resume_scores(page, all_jobs, search_query)
            publish_ui(all_jobs, "Done. Tables are up to date.", running=False)
        except PlaywrightTimeoutError as exc:
            publish_ui(all_jobs, f"Timed out: {exc}", running=False)
            print(f"\nTimed out waiting for the page.\n{exc}", file=sys.stderr)
            return 1
        finally:
            page.wait_for_timeout(2000)
            browser.close()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
