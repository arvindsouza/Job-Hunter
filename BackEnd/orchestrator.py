"""Natural-language orchestrator: route EM/TPM, then scrape, tailor, and re-score."""

from __future__ import annotations

import re
import threading
import time

from resume_tailor import reassess_jobs_batch, tailor_jobs_batch

CHAT_LOCK = threading.Lock()
CHAT_STATE = {
    "running": False,
    "focus_role": None,
    "plan": None,
    "messages": [],
}


def chat_snapshot() -> dict:
    with CHAT_LOCK:
        return {
            "running": CHAT_STATE["running"],
            "focus_role": CHAT_STATE["focus_role"],
            "plan": CHAT_STATE["plan"],
            "messages": list(CHAT_STATE["messages"]),
        }


def _append(role: str, text: str) -> None:
    with CHAT_LOCK:
        CHAT_STATE["messages"].append({"role": role, "text": text})
        CHAT_STATE["messages"] = CHAT_STATE["messages"][-40:]


def _set(**kwargs) -> None:
    with CHAT_LOCK:
        CHAT_STATE.update(kwargs)


def _heuristic_plan(message: str, inventory: dict) -> dict:
    text = message.lower()
    roles: list[str] = []
    if re.search(r"\b(tpm|technical program|program manager)\b", text):
        roles.append("tpm")
    if re.search(r"\b(em|engineering manager|eng manager|engineering lead)\b", text):
        roles.append("em")
    if not roles:
        focus = CHAT_STATE.get("focus_role")
        if focus in ("em", "tpm"):
            roles.append(focus)
        elif inventory.get("em_resume"):
            roles.append("em")
        elif inventory.get("tpm_resume"):
            roles.append("tpm")
        else:
            roles = ["em"]

    numbers = [int(n) for n in re.findall(r"\b(\d{1,3})\b", text)]
    top_n = next((n for n in numbers if 1 <= n <= 30), 10)
    time_range = "24h" if re.search(r"24\s*h|today|past day|last day", text) else "week"
    min_rating = 70 if re.search(r"strong|best|top", text) else 50

    force_skip = bool(re.search(r"existing|current (list|table|results)|already (have|listed)|from the table", text))
    force_scrape = bool(
        re.search(r"\b(fresh run|scrape|pull|this week|find me|get me|new search|search for)\b", text)
    )
    scrape = bool(force_scrape) and not force_skip

    tailor = bool(re.search(r"tailor|custom resume|rewrite resume", text)) and not re.search(
        r"skip tailor|don'?t tailor|without tailor", text
    )
    reassess = (
        bool(re.search(r"re-?score|re-?assess|re-?analy", text))
        or tailor
        or bool(re.search(r"which to apply|tell me which", text))
    ) and not re.search(r"skip re-?score|skip re-?assess", text)

    return {
        "roles": roles,
        "scrape": scrape,
        "tailor": tailor,
        "reassess": reassess,
        "top_n": top_n,
        "min_rating": min_rating,
        "time_range": time_range,
        "max_pulls": min(200, max(50, top_n * 5)),
        "summary": (
            f"{'Scrape' if scrape else 'Reuse'} {' & '.join(roles).upper()}, "
            f"keep top {top_n} at {min_rating}+"
            f"{', tailor' if tailor else ''}{', re-score' if reassess else ''}."
        ),
    }


def _inventory() -> dict:
    from common import ROLE_META, load_agent_config, read_jobs_file

    snapshot = read_jobs_file()
    jobs = snapshot.get("jobs") or {}
    inv = {}
    for role_id, meta in ROLE_META.items():
        rows = jobs.get(meta["search"]) or []
        inv[f"{role_id}_resume"] = bool(load_agent_config(role_id))
        inv[f"{role_id}_jobs"] = len(rows)
        inv[f"{role_id}_strong"] = len(
            [row for row in rows if isinstance(row.get("rating"), (int, float)) and row["rating"] >= 70]
        )
    return inv


def _wait_for_agent(role_id: str, timeout: int = 2400) -> bool:
    from common import agent_is_running

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


def _role_rows(role_id: str) -> list[dict]:
    from common import ROLE_META, read_jobs_file

    snapshot = read_jobs_file()
    return list((snapshot.get("jobs") or {}).get(ROLE_META[role_id]["search"]) or [])


def _pick_jobs(rows: list[dict], top_n: int, min_rating: int) -> list[dict]:
    ranked = []
    for row in rows:
        rating = row.get("rating")
        if rating is None or rating == "":
            continue
        try:
            score = float(rating)
        except (TypeError, ValueError):
            continue
        if score < min_rating:
            continue
        ranked.append(row)
    ranked.sort(key=lambda job: job.get("rating") if job.get("rating") is not None else -1, reverse=True)
    return ranked[:top_n]


def _run_role(role_id: str, plan: dict) -> dict:
    from common import ROLE_META, fetch_job_details, load_agent_config, start_fresh_run, update_ui_job_row

    label = ROLE_META[role_id]["label"]
    result = {"role": role_id, "jobs": 0, "tailored": 0, "reassessed": 0, "apply": [], "errors": []}
    if not load_agent_config(role_id):
        result["errors"].append(f"Upload a {label} resume first.")
        _append("assistant", f"{label}: skipped — no resume uploaded.")
        return result

    if plan["scrape"]:
        _append("assistant", f"{label}: scraping listings…")
        started = start_fresh_run(role_id)
        if not started.get("ok"):
            result["errors"].append(started.get("error") or "Could not start scrape.")
            _append("assistant", f"{label}: {result['errors'][-1]}")
            return result
        if not _wait_for_agent(role_id):
            result["errors"].append("Scrape timed out.")
            _append("assistant", f"{label}: scrape timed out.")
            return result
        _append("assistant", f"{label}: scrape finished. Updating the table.")
    else:
        _append("assistant", f"{label}: skipping scrape and using the current table.")

    rows = _role_rows(role_id)
    result["jobs"] = len(rows)
    chosen = _pick_jobs(rows, plan["top_n"], plan["min_rating"])
    if not chosen:
        _append("assistant", f"{label}: no jobs at {plan['min_rating']}+ yet.")
        return result

    names = ", ".join(f"{job.get('title')} at {job.get('company')}" for job in chosen[:5])
    extra = f" (+{len(chosen) - 5} more)" if len(chosen) > 5 else ""
    _append("assistant", f"{label}: working on {len(chosen)} fit(s): {names}{extra}")

    packed = []
    for job in chosen:
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "").strip()
        link = str(job.get("link") or "").strip()
        try:
            details = fetch_job_details(None, link)
            description = details.get("description") or ""
        except Exception as exc:
            result["errors"].append(f"Could not fetch JD for {title}: {exc}")
            description = ""
        packed.append({"title": title, "company": company, "link": link, "description": description})

    outcomes: list[dict] = [{"ok": False} for _ in packed]
    if plan["tailor"]:
        _append(
            "assistant",
            f"{label}: tailoring{' and re-scoring' if plan['reassess'] else ''} {len(packed)} job(s) in batched LLM calls.",
        )
        try:
            outcomes = tailor_jobs_batch(role_id, packed, score=plan["reassess"])
        except Exception as exc:
            result["errors"].append(f"Batch tailor failed, retrying one job at a time: {exc}")
            outcomes = []
            for job in packed:
                try:
                    outcomes.append(tailor_jobs_batch(role_id, [job], score=plan["reassess"])[0])
                except Exception as inner:
                    outcomes.append({"ok": False, "error": f"{job.get('title')}: {inner}"})
    elif plan["reassess"]:
        _append("assistant", f"{label}: re-scoring {len(packed)} job(s) in batched LLM calls.")
        try:
            outcomes = reassess_jobs_batch(role_id, packed)
        except Exception as exc:
            result["errors"].append(f"Batch re-score failed, retrying one job at a time: {exc}")
            outcomes = []
            for job in packed:
                try:
                    outcomes.append(reassess_jobs_batch(role_id, [job])[0])
                except Exception as inner:
                    outcomes.append({"ok": False, "error": f"{job.get('title')}: {inner}"})

    for job, outcome in zip(packed, outcomes):
        if not outcome or not outcome.get("ok"):
            result["errors"].append(outcome.get("error") if outcome else f"Failed {job['title']}")
            continue
        if plan["tailor"]:
            result["tailored"] += 1
        if plan["reassess"] and outcome.get("row"):
            updated = update_ui_job_row(
                ROLE_META[role_id]["search"],
                job["title"],
                job["company"],
                job["link"],
                outcome["row"],
            )
            rating = (updated or outcome["row"]).get("rating")
            result["reassessed"] += 1
            if isinstance(rating, (int, float)) and rating >= 80:
                result["apply"].append(f"{job['title']} at {job['company']} ({int(rating)}/100)")
    return result


def _execute(message: str) -> None:
    from common import save_settings

    try:
        inventory = _inventory()
        plan = _heuristic_plan(message, inventory)
        _set(plan=plan, focus_role=plan["roles"][0])
        save_settings({"time_range": plan["time_range"], "max_pulls": plan["max_pulls"]})
        _append("assistant", f"Plan: {plan['summary']}")

        summaries = []
        for role_id in plan["roles"]:
            summaries.append(_run_role(role_id, plan))

        lines = ["Done. Tables are updated."]
        for item in summaries:
            role = item["role"].upper()
            lines.append(
                f"{role}: {item['jobs']} listed, {item['tailored']} tailored, {item['reassessed']} re-scored."
            )
            if item["apply"]:
                lines.append("Apply: " + "; ".join(item["apply"]))
            for error in item["errors"][:4]:
                lines.append(error)
        _append("assistant", "\n".join(lines))
    except Exception as exc:
        _append("assistant", f"Stopped: {exc}")
    finally:
        _set(running=False)


def start_chat_instruction(message: str) -> dict:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "Type an instruction first.", "chat": chat_snapshot()}
    with CHAT_LOCK:
        if CHAT_STATE["running"]:
            return {"ok": False, "error": "Already running an instruction.", "chat": chat_snapshot()}
        CHAT_STATE["running"] = True
        CHAT_STATE["messages"].append({"role": "user", "text": text})
        CHAT_STATE["messages"] = CHAT_STATE["messages"][-40:]
    thread = threading.Thread(target=_execute, args=(text,), daemon=True)
    thread.start()
    return {"ok": True, "chat": chat_snapshot()}
