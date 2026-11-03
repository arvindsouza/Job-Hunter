"""Natural-language orchestrator: route EM/TPM, then scrape, tailor, and re-score."""

from __future__ import annotations

import re
import threading
import time

from llm import complete_json, llm_available
from resume_tailor import reassess_jobs_batch, tailor_jobs_batch

CHAT_LOCK = threading.Lock()
CHAT_STATE = {
    "running": False,
    "focus_role": None,
    "plan": None,
    "last_plan": None,
    "messages": [],
}


def chat_snapshot() -> dict:
    with CHAT_LOCK:
        return {
            "running": CHAT_STATE["running"],
            "focus_role": CHAT_STATE["focus_role"],
            "plan": CHAT_STATE["plan"],
            "last_plan": CHAT_STATE["last_plan"],
            "messages": list(CHAT_STATE["messages"]),
        }


def _append(role: str, text: str) -> None:
    with CHAT_LOCK:
        CHAT_STATE["messages"].append({"role": role, "text": text})
        CHAT_STATE["messages"] = CHAT_STATE["messages"][-40:]


def _set(**kwargs) -> None:
    with CHAT_LOCK:
        CHAT_STATE.update(kwargs)


def _plan_summary(plan: dict) -> str:
    roles = " & ".join(role.upper() for role in plan.get("roles") or ["em"])
    return (
        f"{'Scrape' if plan.get('scrape') else 'Reuse'} {roles}, "
        f"keep top {plan.get('top_n')} at {plan.get('min_rating')}-{plan.get('max_rating')}"
        f"{', tailor' if plan.get('tailor') else ''}"
        f"{', re-score' if plan.get('reassess') else ''}."
    )


def _describe_plan(plan: dict, followup: bool = False) -> str:
    roles = " and ".join(
        "TPM" if role == "tpm" else "EM" for role in plan.get("roles") or ["em"]
    )
    when = "the past day" if plan.get("time_range") == "24h" else "the past week"
    bits = []
    if followup:
        bits.append("I'll do the same kind of run")
    if plan.get("scrape"):
        bits.append(f"fetch up to {plan.get('max_pulls')} {roles} listings from {when}")
    else:
        bits.append(f"use the current {roles} table")
    if plan.get("all"):
        bits.append("use every row in the table")
    else:
        bits.append(f"focus on the top {plan.get('top_n')} in the {plan.get('min_rating')}-{plan.get('max_rating')} range")
    if plan.get("tailor") and plan.get("reassess"):
        bits.append("tailor those resumes and re-score them")
    elif plan.get("tailor"):
        bits.append("tailor those resumes")
    elif plan.get("reassess"):
        bits.append("re-score those rows")
    text = ", ".join(bits) + "."
    return text[0].upper() + text[1:]


def _is_followup(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"same|again|also|repeat|more|"
            r"like (?:that|before|last time)|as before|do that|"
            r"with \d+|for \d+|to \d+"
            r")\b",
            text,
        )
    )


def _heuristic_plan(message: str, inventory: dict, view_role: str = "") -> dict:
    text = message.lower()
    roles: list[str] = []
    role_mentioned = False
    if re.search(r"\b(tpm|technical program|program manager)\b", text):
        roles.append("tpm")
        role_mentioned = True
    if re.search(r"\b(em|engineering manager|eng manager|engineering lead)\b", text):
        roles.append("em")
        role_mentioned = True
    if not roles:
        if view_role in ("em", "tpm"):
            roles.append(view_role)
        else:
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
    band = re.search(r"\b(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})\b", text)
    want_all = bool(re.search(r"\b(all|every|entire|everything)\b", text))
    number_mentioned = bool(numbers) or want_all
    top_n = 200 if want_all else next((n for n in numbers if 1 <= n <= 50), 10)
    time_mentioned = bool(re.search(r"24\s*h|today|past day|last day|\bweek\b", text))
    time_range = "24h" if re.search(r"24\s*h|today|past day|last day", text) else "week"
    if band:
        min_rating = max(0, min(100, int(band.group(1))))
        max_rating = max(min_rating, min(100, int(band.group(2))))
    elif want_all:
        min_rating = 0
        max_rating = 100
    else:
        min_rating = 70 if re.search(r"strong|best|top", text) else 50
        max_rating = 100

    force_skip = bool(
        re.search(
            r"existing|current (list|table|results)|already (have|listed)|from the table|in the table|this table",
            text,
        )
    )
    force_scrape = bool(
        re.search(
            r"\b("
            r"fresh(?:\s+run)?|scrape|pull|fetch|refresh|"
            r"this week|find(?:\s+me)?|get me|look for|hunt|"
            r"new search|search for|search again|"
            r"new jobs?|new listings?|new roles?"
            r")\b",
            text,
        )
    )
    scrape = bool(force_scrape) and not force_skip
    skip_tailor = bool(re.search(r"skip tailor|don'?t tailor|without tailor", text))
    tailor = bool(re.search(r"tailor|custom resume|rewrite resume", text)) and not skip_tailor
    skip_reassess = bool(re.search(r"skip re-?score|skip re-?assess", text))
    reassess = (
        bool(re.search(r"re-?score|re-?assess|re-?analy", text))
        or tailor
        or bool(re.search(r"which to apply|tell me which", text))
    ) and not skip_reassess

    plan = {
        "roles": roles,
        "scrape": scrape,
        "tailor": tailor,
        "reassess": reassess,
        "top_n": top_n,
        "min_rating": min_rating,
        "max_rating": max_rating,
        "time_range": time_range,
        "max_pulls": min(200, max(50, min(top_n, 50) * 5)),
        "all": want_all,
    }

    last = CHAT_STATE.get("last_plan")
    followup = bool(last) and _is_followup(text)
    if followup:
        merged = dict(last)
        if role_mentioned:
            merged["roles"] = roles
        if number_mentioned:
            merged["top_n"] = top_n
            merged["max_pulls"] = min(200, max(50, min(top_n, 50) * 5))
            merged["all"] = want_all
        if want_all and not band:
            merged["min_rating"] = 0
            merged["max_rating"] = 100
        if band:
            merged["min_rating"] = min_rating
            merged["max_rating"] = max_rating
        if time_mentioned:
            merged["time_range"] = time_range
        if force_skip:
            merged["scrape"] = False
        elif force_scrape:
            merged["scrape"] = True
        if skip_tailor:
            merged["tailor"] = False
        elif tailor:
            merged["tailor"] = True
        if skip_reassess:
            merged["reassess"] = False
        elif reassess:
            merged["reassess"] = True
        plan = merged

    plan["summary"] = _plan_summary(plan)
    plan["followup"] = followup
    plan["act"] = bool(plan.get("scrape") or plan.get("tailor") or plan.get("reassess"))
    plan["reply"] = _describe_plan(plan, followup=followup)
    return plan


def _normalize_llm_plan(data: dict, fallback: dict) -> dict:
    roles = [role for role in (data.get("roles") or []) if role in ("em", "tpm")]
    if not roles:
        roles = list(fallback.get("roles") or ["em"])
    top_n = data.get("top_n")
    want_all = bool(data.get("all") or top_n == "all" or top_n == "every")
    try:
        top_n = 200 if want_all else max(1, min(50, int(top_n if top_n not in (None, "all") else fallback.get("top_n") or 10)))
    except (TypeError, ValueError):
        top_n = 200 if want_all else int(fallback.get("top_n") or 10)
    try:
        min_rating = max(0, min(100, int(data.get("min_rating") if data.get("min_rating") is not None else fallback.get("min_rating") or 50)))
    except (TypeError, ValueError):
        min_rating = 0 if want_all else 50
    try:
        max_rating = max(min_rating, min(100, int(data.get("max_rating") if data.get("max_rating") is not None else fallback.get("max_rating") or 100)))
    except (TypeError, ValueError):
        max_rating = 100
    time_range = data.get("time_range") if data.get("time_range") in ("24h", "week") else fallback.get("time_range") or "week"
    scrape = bool(data.get("scrape"))
    tailor = bool(data.get("tailor"))
    reassess = bool(data.get("reassess")) or tailor
    act = data.get("act")
    if act is None:
        act = scrape or tailor or reassess
    reply = str(data.get("reply") or "").strip()
    plan = {
        "roles": roles,
        "scrape": scrape,
        "tailor": tailor,
        "reassess": reassess,
        "top_n": top_n,
        "min_rating": min_rating if not want_all else 0,
        "max_rating": max_rating if not want_all else 100,
        "time_range": time_range,
        "max_pulls": min(200, max(50, min(top_n, 50) * 5)),
        "all": want_all,
        "act": bool(act),
        "followup": bool(data.get("followup") or fallback.get("followup")),
    }
    plan["summary"] = _plan_summary(plan)
    plan["reply"] = reply or _describe_plan(plan, followup=plan["followup"])
    return plan


def _conversation_plan(message: str, inventory: dict, view_role: str = "") -> dict:
    fallback = _heuristic_plan(message, inventory, view_role)
    if not llm_available():
        return fallback
    history = CHAT_STATE.get("messages") or []
    recent = history[-12:]
    last = CHAT_STATE.get("last_plan")
    try:
        data = complete_json(
            (
                "You are the Bengaluru job-hunt agent. You only handle fetching jobs, "
                "scoring them, tailoring resumes, and re-scoring. Be conversational. "
                "Use the chat history. Follow-ups like 'do the same with 30' inherit the "
                "previous plan and only change what the user named. "
                "If they ask a question or chat without asking you to act, set act=false "
                "and just reply. If they want work done, set act=true and choose scrape, "
                "tailor, and/or reassess yourself. Do not scrape unless they want new jobs "
                "or a refresh. 'All items in the table' means no scrape, every row. "
                "Return JSON only."
            ),
            (
                f"Open tab: {view_role or 'unknown'}.\n"
                f"Table inventory JSON: {inventory}\n"
                f"Last action plan JSON: {last}\n"
                f"Recent chat JSON: {recent}\n"
                f"Latest user message: {message}\n\n"
                "Return JSON with:\n"
                "reply (1-3 conversational sentences),\n"
                "act (boolean),\n"
                "followup (boolean),\n"
                "scrape, tailor, reassess (booleans),\n"
                "roles (array of em and/or tpm),\n"
                "top_n (integer 1-50, or all),\n"
                "all (boolean),\n"
                "min_rating, max_rating (0-100),\n"
                "time_range (24h or week)."
            ),
            timeout=60,
        )
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    return _normalize_llm_plan(data, fallback)


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


def _run_role(role_id: str, plan: dict) -> dict:
    from common import ROLE_META, fetch_job_details, load_agent_config, start_fresh_run, update_ui_job_row

    label = ROLE_META[role_id]["label"]
    result = {"role": role_id, "jobs": 0, "tailored": 0, "reassessed": 0, "apply": [], "errors": []}
    if not load_agent_config(role_id):
        result["errors"].append(f"Upload a {label} resume first.")
        _append("assistant", f"{label}: skipped — no resume uploaded.")
        return result

    if plan["scrape"]:
        _append("assistant", f"{label}: clearing the table and scraping new listings…")
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
    chosen = _pick_jobs(rows, plan["top_n"], plan["min_rating"], plan.get("max_rating", 100))
    if not chosen:
        _append("assistant", f"{label}: no jobs at {plan['min_rating']}+ yet.")
        return result

    names = ", ".join(f"{job.get('title')} at {job.get('company')}" for job in chosen[:5])
    extra = f" (+{len(chosen) - 5} more)" if len(chosen) > 5 else ""
    _append("assistant", f"{label}: working on {len(chosen)} fit(s): {names}{extra}")

    if not plan["tailor"] and not plan["reassess"]:
        return result

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
        packed.append({"title": title, "company": company, "link": link, "description": description, "note": job.get("note")})

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
            err = (outcome or {}).get("error") or f"Failed {job['title']}"
            result["errors"].append(str(err))
            continue
        if plan["tailor"]:
            result["tailored"] += 1
        row_update = outcome.get("row")
        if plan["tailor"] and not row_update:
            row_update = {
                "note": " | ".join(
                    part
                    for part in [
                        str(job.get("note") or "").strip(),
                        f"Tailored resume saved ({outcome.get('filename') or 'Results/'})",
                    ]
                    if part
                ),
            }
        if not row_update:
            continue
        updated = update_ui_job_row(
            ROLE_META[role_id]["search"],
            job["title"],
            job["company"],
            job["link"],
            row_update,
        )
        if plan["reassess"] and (updated or {}).get("reassessed"):
            result["reassessed"] += 1
            rating = updated.get("rating")
            if isinstance(rating, (int, float)) and rating >= 80:
                result["apply"].append(f"{job['title']} at {job['company']} ({int(rating)}/100)")
    return result


def _execute(message: str, view_role: str = "") -> None:
    from common import save_settings

    try:
        inventory = _inventory()
        plan = _conversation_plan(message, inventory, view_role)
        _set(plan=plan, focus_role=(plan.get("roles") or [view_role or "em"])[0])
        _append("assistant", plan.get("reply") or _describe_plan(plan, followup=bool(plan.get("followup"))))
        if not plan.get("act"):
            return
        _set(last_plan=plan)
        save_settings({"time_range": plan["time_range"], "max_pulls": plan["max_pulls"]})

        summaries = []
        for role_id in plan["roles"]:
            summaries.append(_run_role(role_id, plan))

        lines = ["Done — the table is updated."]
        for item in summaries:
            role = item["role"].upper()
            lines.append(
                f"{role}: {item['jobs']} listed, {item['tailored']} tailored, {item['reassessed']} re-scored."
            )
            if item["apply"]:
                lines.append("Apply: " + "; ".join(item["apply"]))
            for error in item["errors"][:4]:
                if error:
                    lines.append(str(error))
        _append("assistant", "\n".join(lines))
    except Exception as exc:
        _append("assistant", f"Stopped: {exc}")
    finally:
        _set(running=False)


def start_chat_instruction(message: str, view_role: str = "") -> dict:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "Type an instruction first.", "chat": chat_snapshot()}
    with CHAT_LOCK:
        if CHAT_STATE["running"]:
            return {"ok": False, "error": "Already running an instruction.", "chat": chat_snapshot()}
        CHAT_STATE["running"] = True
        CHAT_STATE["messages"].append({"role": "user", "text": text})
        CHAT_STATE["messages"] = CHAT_STATE["messages"][-40:]
    thread = threading.Thread(target=_execute, args=(text, view_role), daemon=True)
    thread.start()
    return {"ok": True, "chat": chat_snapshot()}
