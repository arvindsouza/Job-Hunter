"""Single-job tools for the EM/TPM agents. Batch tailor of many jobs is not allowed here."""

from __future__ import annotations

import time

from common import (
    ROLE_META,
    agent_is_running,
    fetch_job_details,
    load_agent_config,
    read_jobs_file,
    start_fresh_run,
    update_ui_job_row,
)
from resume_tailor import load_tailored_resume, reassess_jobs_batch, tailor_jobs_batch

TOOLS = ("scrape", "list_fits", "fetch_jd", "tailor_one", "reassess_one", "update_row", "stop")
MAX_AGENT_TAILOR_BATCH = 1


def _role_rows(role_id: str) -> list[dict]:
    snapshot = read_jobs_file()
    return list((snapshot.get("jobs") or {}).get(ROLE_META[role_id]["search"]) or [])


def _pick_jobs(rows: list[dict], top_n: int, min_rating: int, max_rating: int = 100) -> list[dict]:
    ranked = []
    for row in rows:
        rating = row.get("rating")
        if rating is None or rating == "":
            ranked.append(row)
            continue
        try:
            score = float(rating)
        except (TypeError, ValueError):
            ranked.append(row)
            continue
        if score < min_rating or score > max_rating:
            continue
        ranked.append(row)
    ranked.sort(key=lambda job: job.get("rating") if job.get("rating") is not None else -1, reverse=True)
    return ranked[:top_n]


def _wait_for_agent(role_id: str, timeout: int = 2400) -> bool:
    deadline = time.time() + timeout
    seen_running = False
    started_deadline = time.time() + 90
    while time.time() < deadline:
        running = agent_is_running(role_id)
        if running:
            seen_running = True
        elif seen_running:
            time.sleep(1)
            return True
        elif time.time() > started_deadline:
            return False
        time.sleep(1)
    return seen_running


def _job_fields(args: dict) -> tuple[str, str, str]:
    return (
        str(args.get("title") or "").strip(),
        str(args.get("company") or "").strip(),
        str(args.get("link") or "").strip(),
    )


def _write_row(role_id: str, title: str, company: str, link: str, updates: dict) -> dict | None:
    return update_ui_job_row(ROLE_META[role_id]["search"], title, company, link, updates)


def run_tool(role_id: str, name: str, args: dict | None = None, plan: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    if name not in TOOLS:
        return {"ok": False, "tool": name, "error": f"Unknown tool {name}."}
    if name == "tailor_one" and isinstance(args.get("jobs"), list) and len(args["jobs"]) > MAX_AGENT_TAILOR_BATCH:
        return {
            "ok": False,
            "tool": name,
            "error": "Agent path cannot tailor a batch; call tailor_one once per job.",
        }
    handler = {
        "scrape": _scrape,
        "list_fits": _list_fits,
        "fetch_jd": _fetch_jd,
        "tailor_one": _tailor_one,
        "reassess_one": _reassess_one,
        "update_row": _update_row,
        "stop": _stop,
    }[name]
    result = handler(role_id, args, plan)
    result.setdefault("tool", name)
    return result


def _scrape(role_id: str, _args: dict, _plan: dict) -> dict:
    label = ROLE_META[role_id]["label"]
    if not load_agent_config(role_id):
        return {"ok": False, "error": f"Upload a {label} resume first."}
    started = start_fresh_run(role_id)
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error") or "Could not start scrape."}
    if not _wait_for_agent(role_id):
        return {"ok": False, "error": "Scrape timed out."}
    rows = _role_rows(role_id)
    return {"ok": True, "jobs": len(rows), "scraped": True}


def _list_fits(role_id: str, args: dict, plan: dict) -> dict:
    top_n = int(args.get("top_n") or plan.get("top_n") or 30)
    min_rating = int(args.get("min_rating") or plan.get("min_rating") or 50)
    max_rating = int(args.get("max_rating") or plan.get("max_rating") or 100)
    rows = _role_rows(role_id)
    fits = _pick_jobs(rows, top_n, min_rating, max_rating)
    return {"ok": True, "jobs": len(rows), "fits": fits}


def _fetch_jd(role_id: str, args: dict, _plan: dict) -> dict:
    title, company, link = _job_fields(args)
    if not title or not company:
        return {"ok": False, "error": "title and company are required."}
    try:
        details = fetch_job_details(None, link)
    except Exception as exc:
        return {"ok": False, "error": f"Could not fetch JD for {title}: {exc}", "title": title, "company": company}
    description = details.get("description") or ""
    return {
        "ok": True,
        "title": title,
        "company": company,
        "link": link,
        "description": description,
    }


def _tailor_one(role_id: str, args: dict, plan: dict) -> dict:
    title, company, link = _job_fields(args)
    if not title or not company:
        return {"ok": False, "error": "title and company are required."}
    description = str(args.get("description") or "")
    if not description and link:
        fetched = _fetch_jd(role_id, args, plan)
        if not fetched.get("ok"):
            return fetched
        description = fetched.get("description") or ""
    packed = [{"title": title, "company": company, "link": link, "description": description, "note": args.get("note")}]
    try:
        outcomes = tailor_jobs_batch(role_id, packed, score=bool(plan.get("reassess", True)))
    except Exception as exc:
        return {"ok": False, "error": f"{title}: {exc}", "title": title, "company": company}
    outcome = outcomes[0] if outcomes else {"ok": False, "error": "Could not tailor resume."}
    if not outcome.get("ok"):
        outcome.setdefault("title", title)
        outcome.setdefault("company", company)
        return outcome
    row_update = outcome.get("row") or {
        "note": " | ".join(
            part
            for part in [
                str(args.get("note") or "").strip(),
                f"Tailored resume saved ({outcome.get('filename') or 'Results/'})",
            ]
            if part
        ),
    }
    updated = _write_row(role_id, title, company, link, row_update)
    outcome["row"] = updated or row_update
    outcome["title"] = title
    outcome["company"] = company
    outcome["link"] = link
    outcome["tailored"] = True
    return outcome


def _reassess_one(role_id: str, args: dict, _plan: dict) -> dict:
    title, company, link = _job_fields(args)
    if not title or not company:
        return {"ok": False, "error": "title and company are required."}
    if not load_tailored_resume(company, title):
        return {"ok": False, "error": "Tailor the resume for this job first.", "title": title, "company": company}
    description = str(args.get("description") or "")
    if not description and link:
        fetched = _fetch_jd(role_id, args, _plan)
        if fetched.get("ok"):
            description = fetched.get("description") or ""
    packed = [{"title": title, "company": company, "link": link, "description": description}]
    try:
        outcomes = reassess_jobs_batch(role_id, packed)
    except Exception as exc:
        return {"ok": False, "error": f"{title}: {exc}", "title": title, "company": company}
    outcome = outcomes[0] if outcomes else {"ok": False, "error": "Could not re-assess job."}
    if not outcome.get("ok"):
        outcome.setdefault("title", title)
        outcome.setdefault("company", company)
        return outcome
    updated = _write_row(role_id, title, company, link, outcome.get("row") or {})
    outcome["row"] = updated or outcome.get("row")
    outcome["title"] = title
    outcome["company"] = company
    outcome["link"] = link
    outcome["reassessed"] = True
    return outcome


def _update_row(role_id: str, args: dict, _plan: dict) -> dict:
    title, company, link = _job_fields(args)
    updates = args.get("updates") if isinstance(args.get("updates"), dict) else {}
    if not title or not company or not updates:
        return {"ok": False, "error": "title, company, and updates are required."}
    updated = _write_row(role_id, title, company, link, updates)
    return {"ok": True, "row": updated, "title": title, "company": company}


def _stop(_role_id: str, args: dict, _plan: dict) -> dict:
    return {"ok": True, "stop": True, "reason": str(args.get("reason") or "stopped")}
