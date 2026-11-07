"""Single-job tools for the EM/TPM agents. Batch tailor of many jobs is not allowed here."""

from __future__ import annotations

import json
import re
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
from llm import complete_json, llm_available
from resume_tailor import load_tailored_resume, reassess_jobs_batch, tailor_jobs_batch

TOOLS = (
    "scrape",
    "list_fits",
    "fetch_jd",
    "assess_one",
    "tailor_one",
    "reassess_one",
    "update_row",
    "stop",
)
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
        "assess_one": _assess_one,
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


def _wrong_role_reason(role_id: str, title: str) -> str | None:
    text = (title or "").lower()
    if role_id == "tpm":
        looks_em = "engineering manager" in text or bool(re.search(r"\bem\b", text))
        looks_tpm = "tpm" in text or "program manager" in text or "technical program" in text
        if looks_em and not looks_tpm:
            return "title is an Engineering Manager role, not TPM"
    if role_id == "em":
        looks_tpm = bool(re.search(r"\btpm\b", text)) or "technical program" in text
        if looks_tpm:
            return "title is a TPM role, not Engineering Manager"
    return None


def _needs_instruction_filter(constraints: list[str]) -> bool:
    blob = " ".join(constraints).lower()
    return bool(
        re.search(
            r"\b("
            r"skip|ignore|avoid|except|"
            r"too technical|hands-?on|coding|leetcode|"
            r"not em|not tpm|only (?:em|tpm)"
            r")\b",
            blob,
        )
    )


def _assess_one(role_id: str, args: dict, plan: dict) -> dict:
    title, company, link = _job_fields(args)
    if not title or not company:
        return {"ok": False, "error": "title and company are required."}
    constraints = [str(item) for item in (plan.get("constraints") or []) if str(item).strip()]
    description = str(args.get("description") or "")
    wrong = _wrong_role_reason(role_id, title)
    skip = False
    reason = ""
    if wrong:
        skip = True
        reason = wrong
    elif _needs_instruction_filter(constraints) and llm_available():
        if not description and link:
            fetched = _fetch_jd(role_id, args, plan)
            if fetched.get("ok"):
                description = str(fetched.get("description") or "")
        label = ROLE_META[role_id]["label"]
        try:
            payload = complete_json(
                (
                    f"You filter one job for a Bengaluru {label} hunt. "
                    "Skip if the title or JD is clearly the other role (EM vs TPM), "
                    "or if it violates the user's instructions "
                    "(for example skip too-technical IC/coding-heavy JDs when asked). "
                    f"Keep a reasonable {label} match otherwise. "
                    'Return JSON only: {"skip": true or false, "reason": "short"}.'
                ),
                json.dumps(
                    {
                        "role": role_id,
                        "instructions": constraints[-8:],
                        "title": title,
                        "company": company,
                        "description": (description or "")[:2500],
                    },
                    ensure_ascii=False,
                )[:8000],
                attempts=2,
                timeout=45,
            )
        except Exception as exc:
            payload = {"skip": False, "reason": str(exc)}
        if isinstance(payload, dict) and payload.get("skip"):
            skip = True
            reason = str(payload.get("reason") or "does not match instructions").strip()
    if skip:
        note = f"Skipped: {reason}"
        row_update = {
            "note": " | ".join(
                part for part in [str(args.get("note") or "").strip(), note] if part
            ),
        }
        updated = _write_row(role_id, title, company, link, row_update)
        return {
            "ok": True,
            "skip": True,
            "reason": reason,
            "title": title,
            "company": company,
            "link": link,
            "description": description,
            "row": updated or row_update,
            "assessed": True,
        }
    return {
        "ok": True,
        "skip": False,
        "title": title,
        "company": company,
        "link": link,
        "description": description,
        "assessed": True,
    }


def _tailor_one(role_id: str, args: dict, plan: dict) -> dict:
    title, company, link = _job_fields(args)
    if not title or not company:
        return {"ok": False, "error": "title and company are required."}
    screened = _assess_one(role_id, args, plan)
    if screened.get("skip"):
        return screened
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
