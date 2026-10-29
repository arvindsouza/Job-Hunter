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
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from resume_extract import list_resume_summaries, load_profile, save_resume

SEM_SEARCH = "Software Engineering Manager"
TPM_SEARCH = "Technical Program Manager"
ALL_ROLES = [SEM_SEARCH, TPM_SEARCH]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = PROJECT_ROOT / "UI"
ASSETS_DIR = PROJECT_ROOT / "Assets"
BACKEND_DIR = Path(__file__).resolve().parent
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
    """Build a scoring config from Assets/<role>/profile.json. None if not uploaded."""
    if role_id not in ROLE_META:
        raise ValueError("role_id must be em or tpm")
    profile = load_profile(role_id)
    if not profile:
        return None
    groups = profile.get("skill_groups") or []
    if not groups:
        return None
    gaps = [tuple(item) for item in profile.get("gap_signals") or [] if len(item) == 2]
    meta = ROLE_META[role_id]
    return {
        "search": meta["search"],
        "label": meta["label"],
        "title_fit_fn": meta["title_fit_fn"],
        "skill_groups": groups,
        "gap_signals": gaps,
        "resume_path": str(ASSETS_DIR / role_id / "resume.pdf"),
        "source_filename": profile.get("source_filename", "resume.pdf"),
    }


def score_job(title: str, description: str, config: dict) -> dict:
    blob = f"{title}\n{description}".lower()
    title_points, title_note = config["title_fit_fn"](title)
    matched: list[str] = []
    missed: list[str] = []
    skill_points = 0
    skill_groups = config["skill_groups"]
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
    }


def is_linkedin_url(url: str) -> bool:
    if not url:
        return False
    return "linkedin.com" in urlparse(url).netloc.lower()


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


def attach_resume_scores(page, jobs: list[dict], config: dict) -> None:
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        publish_ui(jobs, f"{config['label']}: scoring ({index}/{total})", True, config["search"])
        details = fetch_job_details(page, job["link"])
        job["description"] = details["description"]
        job["apply_url"] = details["apply_url"]
        job["easy_apply"] = details["easy_apply"]
        job.update(score_job(job["title"], details["description"], config))
        publish_ui(jobs, f"{config['label']}: scored ({index}/{total})", True, config["search"])


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
                f"Resume hits: {', '.join(job['matched'])}" if job.get("matched") else "",
            ]
            if part
        ),
        "weak_vs_jd": ", ".join(job.get("missed", [])[:6]),
        "note": (
            "JD was hidden (login wall). Score used the title only."
            if job.get("had_description") is False
            else (f"JD asks extra: {', '.join(job['jd_gaps'])}" if job.get("jd_gaps") else "")
        ),
    }


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
    """Merge this agent's rows into the shared UI payload without wiping the other role."""
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
        UI_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot, indent=2)
        (UI_DIR / "jobs.json").write_text(payload, encoding="utf-8")
        (UI_DIR / "jobs-data.js").write_text(
            "window.JOB_DATA = " + json.dumps(snapshot["jobs"], indent=2) + ";\n",
            encoding="utf-8",
        )


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
            self._send_json(payload)
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
    if started_server:
        open_in_chrome(ui_url + ("#/tpm" if role_id == "tpm" else "#/em"))
    else:
        print("UI server already running; this agent will only scrape.\n")

    publish_ui([], f"{label}: starting", True, role)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        page = context.new_page()
        found: list[dict] = []
        seen: set[str] = set()
        try:
            publish_ui(found, f"{label}: searching LinkedIn", True, role)
            page.goto(build_search_url(role), wait_until="domcontentloaded", timeout=45000)
            dismiss_cookie_banner(page)
            for job in extract_jobs(page, role):
                key = job["link"] if job["link"] != "Link not found" else f"{job['title']}|{job['company']}"
                if key in seen:
                    continue
                seen.add(key)
                found.append(job)
                publish_ui(found, f"{label}: found {len(found)} listing(s)", True, role)
            attach_resume_scores(page, found, config)
            publish_ui(found, f"{label}: done.", False, role)
        except PlaywrightTimeoutError as exc:
            publish_ui(found, f"{label}: timed out: {exc}", False, role)
            print(f"\nTimed out waiting for the page.\n{exc}", file=sys.stderr)
            return 1
        finally:
            page.wait_for_timeout(2000)
            browser.close()

    if not started_server:
        return 0
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        if server is not None:
            server.shutdown()
        return 0
