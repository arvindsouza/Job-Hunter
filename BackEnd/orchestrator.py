"""Natural-language orchestrator: route EM/TPM, then scrape, tailor, and re-score."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from llm import complete_json, llm_available

CHAT_LOCK = threading.Lock()
CHAT_LOG: list[dict] = []
CHAT_FOCUS = None
CHAT_STATE_PATH = Path(__file__).resolve().parent.parent / "Assets" / "chat-state.json"


def _new_agent(role_id: str) -> dict:
    return {
        "role": role_id,
        "running": False,
        "inflight": 0,
        "plan": None,
        "last_plan": None,
        "messages": [],
        "queue": [],
        "status": "",
    }


AGENTS = {
    "em": _new_agent("em"),
    "tpm": _new_agent("tpm"),
}


def _persist_unlocked() -> None:
    CHAT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "focus": CHAT_FOCUS,
        "log": CHAT_LOG[-40:],
        "agents": {
            role_id: {
                "role": role_id,
                "running": AGENTS[role_id]["running"],
                "plan": AGENTS[role_id].get("plan"),
                "last_plan": AGENTS[role_id].get("last_plan"),
                "messages": list(AGENTS[role_id].get("messages") or [])[-40:],
                "queue": list(AGENTS[role_id].get("queue") or []),
                "status": AGENTS[role_id].get("status") or "",
            }
            for role_id in ("em", "tpm")
        },
    }
    tmp = CHAT_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CHAT_STATE_PATH)


def _load_chat() -> None:
    global CHAT_FOCUS, CHAT_LOG
    if not CHAT_STATE_PATH.exists():
        return
    try:
        data = json.loads(CHAT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    was_running = False
    with CHAT_LOCK:
        CHAT_FOCUS = data.get("focus") if data.get("focus") in ("em", "tpm") else None
        CHAT_LOG = [item for item in (data.get("log") or []) if isinstance(item, dict)][-40:]
        saved_agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        for role_id in ("em", "tpm"):
            saved = saved_agents.get(role_id) if isinstance(saved_agents.get(role_id), dict) else {}
            if saved.get("running"):
                was_running = True
            AGENTS[role_id]["plan"] = saved.get("plan")
            AGENTS[role_id]["last_plan"] = saved.get("last_plan")
            AGENTS[role_id]["messages"] = [
                item for item in (saved.get("messages") or []) if isinstance(item, dict)
            ][-40:]
            AGENTS[role_id]["queue"] = [
                item for item in (saved.get("queue") or []) if isinstance(item, dict)
            ]
            AGENTS[role_id]["status"] = str(saved.get("status") or "")
            AGENTS[role_id]["running"] = False
            AGENTS[role_id]["inflight"] = 0
        if was_running:
            CHAT_LOG.append(
                {
                    "role": "assistant",
                    "text": "Server restarted. Agents are idle; previous in-flight work did not finish.",
                }
            )
            del CHAT_LOG[:-40]
            _persist_unlocked()


def _agent_is_working(role_id: str) -> bool:
    from common import agent_is_running

    with CHAT_LOCK:
        chat_busy = bool(AGENTS[role_id]["running"] or AGENTS[role_id].get("queue"))
    return chat_busy or agent_is_running(role_id)


def chat_snapshot() -> dict:
    from common import agent_is_running

    scrape = {role_id: agent_is_running(role_id) for role_id in ("em", "tpm")}
    with CHAT_LOCK:
        running_by_agent = {
            role_id: bool(
                AGENTS[role_id]["running"]
                or AGENTS[role_id].get("queue")
                or scrape[role_id]
            )
            for role_id in ("em", "tpm")
        }
        running = any(running_by_agent.values())
        focus = CHAT_FOCUS
        plan = None
        last_plan = None
        for role_id in ("em", "tpm"):
            if running_by_agent[role_id]:
                plan = AGENTS[role_id]["plan"]
                focus = role_id
                break
        if focus in AGENTS:
            last_plan = AGENTS[focus]["last_plan"]
            plan = plan or AGENTS[focus]["plan"]
        return {
            "running": running,
            "running_by_agent": running_by_agent,
            "focus_role": focus,
            "plan": plan,
            "last_plan": last_plan,
            "messages": list(CHAT_LOG[-40:]),
            "agents": {
                role_id: {
                    "running": running_by_agent[role_id],
                    "plan": AGENTS[role_id]["plan"],
                    "status": AGENTS[role_id].get("status") or "",
                    "queue": len(AGENTS[role_id].get("queue") or []),
                }
                for role_id in ("em", "tpm")
            },
        }


def begin_agent_work(role_id: str, status: str = "") -> None:
    with CHAT_LOCK:
        agent = AGENTS[role_id]
        agent["inflight"] = int(agent.get("inflight") or 0) + 1
        agent["running"] = True
        if status:
            agent["status"] = status
        _persist_unlocked()


def end_agent_work(role_id: str) -> None:
    with CHAT_LOCK:
        agent = AGENTS[role_id]
        agent["inflight"] = max(0, int(agent.get("inflight") or 0) - 1)
        if agent["inflight"] == 0 and not agent.get("queue"):
            agent["running"] = False
            agent["status"] = ""
        _persist_unlocked()


def _append(speaker: str, text: str, agent: str | None = None) -> None:
    item = {"role": speaker, "text": text}
    if agent in ("em", "tpm"):
        item["agent"] = agent
    with CHAT_LOCK:
        CHAT_LOG.append(item)
        del CHAT_LOG[:-40]
        if agent in AGENTS:
            AGENTS[agent]["messages"].append(item)
            AGENTS[agent]["messages"] = AGENTS[agent]["messages"][-40:]
        _persist_unlocked()


def _set_agent(role_id: str, **kwargs) -> None:
    global CHAT_FOCUS
    with CHAT_LOCK:
        AGENTS[role_id].update(kwargs)
        if "running" in kwargs and kwargs["running"]:
            CHAT_FOCUS = role_id
        _persist_unlocked()


_load_chat()


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


def _mentioned_roles(message: str) -> list[str]:
    text = (message or "").lower()
    if re.search(r"\b(both(?:\s+roles)?|each role|em and tpm|tpm and em)\b", text):
        return ["em", "tpm"]
    roles: list[str] = []
    if re.search(r"\b(tpm|tpms|technical program|program manager)\b", text):
        roles.append("tpm")
    if re.search(
        r"\b(engineering managers?|eng managers?|engineering leads?|em roles?|em jobs?|em listings?)\b",
        text,
    ) or re.search(r"(?:^|[^\w])em(?:$|[^\w])", text):
        roles.append("em")
    return roles


def _constrain_roles(
    roles: list[str] | None,
    message: str,
    view_role: str = "",
    followup: bool = False,
    last_roles: list[str] | None = None,
) -> list[str]:
    mentioned = _mentioned_roles(message)
    if mentioned:
        return mentioned
    if followup:
        kept = [role for role in (last_roles or []) if role in ("em", "tpm")]
        if kept:
            return kept
    if view_role in ("em", "tpm"):
        return [view_role]
    if CHAT_FOCUS in ("em", "tpm"):
        return [CHAT_FOCUS]
    cleaned = [role for role in (roles or []) if role in ("em", "tpm")]
    return cleaned[:1] or ["em"]


def _heuristic_plan(message: str, inventory: dict, view_role: str = "", last_plan: dict | None = None) -> dict:
    text = message.lower()
    mentioned = _mentioned_roles(text)
    role_mentioned = bool(mentioned)
    last = last_plan or {}
    followup = bool(last) and _is_followup(text)
    roles = _constrain_roles(
        mentioned,
        message,
        view_role,
        followup=followup,
        last_roles=list(last.get("roles") or []),
    )

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
        if role_mentioned:
            plan["roles"] = roles
        else:
            plan["roles"] = _constrain_roles(
                plan.get("roles"),
                message,
                view_role,
                followup=True,
                last_roles=list(last.get("roles") or []),
            )

    plan["summary"] = _plan_summary(plan)
    plan["followup"] = followup
    plan["act"] = bool(plan.get("scrape") or plan.get("tailor") or plan.get("reassess"))
    plan["reply"] = _describe_plan(plan, followup=followup)
    return plan


def _normalize_llm_plan(
    data: dict,
    fallback: dict,
    message: str = "",
    view_role: str = "",
    last_plan: dict | None = None,
) -> dict:
    roles = _constrain_roles(
        [role for role in (data.get("roles") or []) if role in ("em", "tpm")],
        message,
        view_role,
        followup=bool(data.get("followup") or fallback.get("followup")),
        last_roles=list((last_plan or fallback).get("roles") or []),
    )
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


def _conversation_plan(message: str, inventory: dict, view_role: str, role_id: str) -> dict:
    with CHAT_LOCK:
        last = dict(AGENTS[role_id].get("last_plan") or {})
        recent = list(AGENTS[role_id].get("messages") or [])[-12:]
    fallback = _heuristic_plan(message, inventory, view_role, last_plan=last)
    fallback["roles"] = [role_id]
    if not llm_available():
        return fallback
    label = "Engineering Manager" if role_id == "em" else "Technical Program Manager"
    other = "TPM" if role_id == "em" else "EM"
    try:
        data = complete_json(
            (
                f"You are the Bengaluru {label} agent. You only fetch, score, tailor, "
                f"and re-score {label} jobs. Never touch {other} jobs or the {other} table. "
                "Be conversational. Use this agent's chat history. Follow-ups like "
                "'do the same with 30' inherit this agent's previous plan and only change "
                "what the user named. If they ask a question without asking you to act, "
                "set act=false and just reply. If they want work done, set act=true and "
                "choose scrape, tailor, and/or reassess. Do not scrape unless they want "
                "new jobs or a refresh. 'All items in the table' means no scrape, every row. "
                f"roles must always be [\"{role_id}\"]. Return JSON only."
            ),
            (
                f"This agent: {role_id}.\n"
                f"Table inventory JSON: {inventory}\n"
                f"Last action plan JSON: {last}\n"
                f"Recent chat JSON: {recent}\n"
                f"Latest user message: {message}\n\n"
                "Return JSON with:\n"
                "reply (1-3 conversational sentences),\n"
                "act (boolean),\n"
                "followup (boolean),\n"
                "scrape, tailor, reassess (booleans),\n"
                f"roles ([\"{role_id}\"] only),\n"
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
    plan = _normalize_llm_plan(
        data, fallback, message=message, view_role=view_role, last_plan=last
    )
    plan["roles"] = [role_id]
    return plan


def _inventory(role_id: str | None = None) -> dict:
    from common import ROLE_META, load_agent_config, read_jobs_file

    snapshot = read_jobs_file()
    jobs = snapshot.get("jobs") or {}
    inv = {}
    roles = [role_id] if role_id in ROLE_META else list(ROLE_META)
    for item in roles:
        meta = ROLE_META[item]
        rows = jobs.get(meta["search"]) or []
        inv[f"{item}_resume"] = bool(load_agent_config(item))
        inv[f"{item}_jobs"] = len(rows)
        inv[f"{item}_strong"] = len(
            [row for row in rows if isinstance(row.get("rating"), (int, float)) and row["rating"] >= 70]
        )
    return inv


DECIDE_SYSTEM = """You choose the next tool for a Bengaluru EM/TPM job-hunt agent.
Allowed tools: scrape, list_fits, fetch_jd, tailor_one, reassess_one, update_row, stop.
Return JSON only: {"tool": "...", "args": {}, "reason": "..."}.
Never tailor more than one job per call. Never pass a jobs array longer than 1.
Prefer stop when the goal is done or the last error cannot be fixed.
"""


def _job_args(job: dict) -> dict:
    return {
        "title": str(job.get("title") or "").strip(),
        "company": str(job.get("company") or "").strip(),
        "link": str(job.get("link") or "").strip(),
        "note": job.get("note"),
        "description": job.get("description") or "",
    }


def _job_key(job: dict) -> tuple[str, str]:
    return (str(job.get("title") or "").strip(), str(job.get("company") or "").strip())


def _rule_next(role_id: str, plan: dict, state: dict) -> dict:
    from resume_tailor import load_tailored_resume

    skipped = state.get("skipped") or set()
    if plan.get("scrape") and not state.get("scraped"):
        return {"tool": "scrape", "args": {}}
    if state.get("fits") is None:
        return {"tool": "list_fits", "args": {}}
    fits = state.get("fits") or []
    if not fits:
        return {"tool": "stop", "args": {"reason": f"no jobs at {plan.get('min_rating')}+ yet"}}
    if not plan.get("tailor") and not plan.get("reassess"):
        return {"tool": "stop", "args": {"reason": "list only"}}
    for job in fits:
        key = _job_key(job)
        if key in skipped or not key[0] or not key[1]:
            continue
        has_pdf = load_tailored_resume(job.get("company"), job.get("title"))
        if plan.get("tailor") and not has_pdf:
            return {"tool": "tailor_one", "args": _job_args(job)}
        if plan.get("reassess") and has_pdf and not job.get("reassessed"):
            return {"tool": "reassess_one", "args": _job_args(job)}
    return {"tool": "stop", "args": {"reason": "all selected jobs done"}}


def _llm_decide(role_id: str, plan: dict, state: dict, last: dict | None) -> tuple[dict | None, str | None]:
    from agent_tools import TOOLS

    user = json.dumps(
        {
            "role": role_id,
            "plan": {
                key: plan.get(key)
                for key in ("scrape", "tailor", "reassess", "top_n", "min_rating", "max_rating")
            },
            "inventory": _inventory(role_id),
            "scraped": bool(state.get("scraped")),
            "fit_count": len(state.get("fits") or []),
            "tailored": state.get("tailored") or 0,
            "reassessed": state.get("reassessed") or 0,
            "last": last,
            "errors": (state.get("errors") or [])[-3:],
        },
        ensure_ascii=False,
    )[:8000]
    try:
        payload = complete_json(DECIDE_SYSTEM, user, attempts=2, timeout=45)
    except Exception as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "LLM returned no decision."
    tool = str(payload.get("tool") or "")
    if tool not in TOOLS:
        return None, f"LLM picked invalid tool {tool!r}."
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    jobs = args.get("jobs")
    if isinstance(jobs, list) and jobs:
        first = jobs[0] if isinstance(jobs[0], dict) else {}
        args = {**first, **{k: v for k, v in args.items() if k != "jobs"}}
    return {"tool": tool, "args": args, "reason": str(payload.get("reason") or "")}, None


def _apply_tool_result(state: dict, outcome: dict) -> None:
    tool = outcome.get("tool")
    if tool == "scrape" and outcome.get("ok"):
        state["scraped"] = True
        state["jobs"] = int(outcome.get("jobs") or 0)
        state["fits"] = None
        return
    if tool == "list_fits" and outcome.get("ok"):
        state["jobs"] = int(outcome.get("jobs") or 0)
        state["fits"] = list(outcome.get("fits") or [])
        return
    if not outcome.get("ok"):
        return
    row = outcome.get("row") if isinstance(outcome.get("row"), dict) else {}
    key = (str(outcome.get("title") or "").strip(), str(outcome.get("company") or "").strip())
    for index, job in enumerate(state.get("fits") or []):
        if _job_key(job) != key:
            continue
        merged = dict(job)
        merged.update(row)
        state["fits"][index] = merged
        break
    if outcome.get("tailored") or tool == "tailor_one":
        state["tailored"] = int(state.get("tailored") or 0) + 1
    if outcome.get("reassessed") or (row.get("reassessed") and tool == "reassess_one"):
        state["reassessed"] = int(state.get("reassessed") or 0) + 1
        rating = row.get("rating")
        if isinstance(rating, (int, float)) and rating >= 80:
            state["apply"].append(f"{key[0]} at {key[1]} ({int(rating)}/100)")


def _run_role(role_id: str, plan: dict) -> dict:
    from agent_tools import run_tool
    from common import ROLE_META, load_agent_config

    label = ROLE_META[role_id]["label"]
    state = {
        "role": role_id,
        "jobs": 0,
        "tailored": 0,
        "reassessed": 0,
        "apply": [],
        "errors": [],
        "scraped": not bool(plan.get("scrape")),
        "fits": None,
        "skipped": set(),
        "llm_fails": 0,
    }
    if not load_agent_config(role_id):
        state["errors"].append(f"Upload a {label} resume first.")
        _append("assistant", f"{label}: skipped — no resume uploaded.", role_id)
        return state

    if plan.get("scrape"):
        _append("assistant", f"{label}: clearing the table and scraping new listings…", role_id)
    else:
        _append("assistant", f"{label}: skipping scrape and using the current table.", role_id)

    last: dict | None = None
    while True:
        if last is not None and not last.get("ok"):
            decided = None
            error = None
            if llm_available():
                decided, error = _llm_decide(role_id, plan, state, last)
            if decided:
                action = decided
                state["llm_fails"] = 0
            else:
                state["llm_fails"] = int(state.get("llm_fails") or 0) + 1
                if error:
                    state["errors"].append(error)
                skip_key = _job_key(last)
                if skip_key[0]:
                    state["skipped"].add(skip_key)
                action = _rule_next(role_id, plan, state)
            last = None
        else:
            action = _rule_next(role_id, plan, state)
        if action.get("tool") == "stop":
            reason = (action.get("args") or {}).get("reason") or "stopped"
            if "no jobs" in str(reason):
                _append("assistant", f"{label}: {reason}.", role_id)
            elif state["fits"] and not plan.get("tailor") and not plan.get("reassess"):
                names = ", ".join(
                    f"{job.get('title')} at {job.get('company')}" for job in (state["fits"] or [])[:5]
                )
                extra = f" (+{len(state['fits']) - 5} more)" if len(state["fits"]) > 5 else ""
                _append("assistant", f"{label}: {len(state['fits'])} fit(s): {names}{extra}", role_id)
            break

        if action.get("tool") == "list_fits":
            _set_agent(role_id, status=f"{label}: picking fits")
        elif action.get("tool") in ("tailor_one", "reassess_one"):
            args = action.get("args") or {}
            _set_agent(
                role_id,
                status=f"{label}: {action['tool']} {args.get('title')} at {args.get('company')}",
            )

        outcome = run_tool(role_id, action["tool"], action.get("args") or {}, plan)
        last = outcome
        if not outcome.get("ok"):
            err = str(outcome.get("error") or f"{action['tool']} failed")
            state["errors"].append(err)
            _append("assistant", f"{label}: {err}", role_id)
            continue
        _apply_tool_result(state, outcome)
        if outcome.get("tool") == "scrape":
            _append("assistant", f"{label}: scrape finished. Updating the table.", role_id)
        elif outcome.get("tool") == "list_fits":
            fits = state.get("fits") or []
            if fits:
                names = ", ".join(f"{job.get('title')} at {job.get('company')}" for job in fits[:5])
                extra = f" (+{len(fits) - 5} more)" if len(fits) > 5 else ""
                _append("assistant", f"{label}: working on {len(fits)} fit(s): {names}{extra}", role_id)
        last = outcome if not outcome.get("ok") else None
    return {
        "role": role_id,
        "jobs": state["jobs"],
        "tailored": state["tailored"],
        "reassessed": state["reassessed"],
        "apply": state["apply"],
        "errors": state["errors"],
    }


def _execute_role(role_id: str, message: str, view_role: str = "") -> None:
    from common import save_settings

    try:
        inventory = _inventory(role_id)
        plan = _conversation_plan(message, inventory, view_role, role_id)
        plan["roles"] = [role_id]
        _set_agent(role_id, plan=plan)
        _append(
            "assistant",
            plan.get("reply") or _describe_plan(plan, followup=bool(plan.get("followup"))),
            role_id,
        )
        if not plan.get("act"):
            return
        _set_agent(role_id, last_plan=plan)
        save_settings({"time_range": plan["time_range"], "max_pulls": plan["max_pulls"]})
        item = _run_role(role_id, plan)
        lines = [
            f"{role_id.upper()} done — that table is updated.",
            f"{item['jobs']} listed, {item['tailored']} tailored, {item['reassessed']} re-scored.",
        ]
        if item["apply"]:
            lines.append("Apply: " + "; ".join(item["apply"]))
        for error in item["errors"][:4]:
            if error:
                lines.append(str(error))
        _append("assistant", "\n".join(lines), role_id)
    except Exception as exc:
        _append("assistant", f"{role_id.upper()} stopped: {exc}", role_id)
    finally:
        end_agent_work(role_id)


def _dispatch_roles(message: str, view_role: str) -> list[str]:
    mentioned = _mentioned_roles(message)
    if mentioned:
        return mentioned
    preferred = view_role if view_role in ("em", "tpm") else (CHAT_FOCUS if CHAT_FOCUS in ("em", "tpm") else "em")
    if not _agent_is_working(preferred):
        return [preferred]
    other = "tpm" if preferred == "em" else "em"
    if not _agent_is_working(other):
        return [other]
    return [preferred]


def start_chat_instruction(
    message: str,
    view_role: str = "",
    requested_roles: list[str] | None = None,
) -> dict:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "Type an instruction first.", "chat": chat_snapshot()}
    explicit = [role for role in (requested_roles or []) if role in ("em", "tpm")]
    if view_role == "both" and not explicit:
        explicit = ["em", "tpm"]
    view = view_role if view_role in ("em", "tpm") else ""
    roles = explicit or _dispatch_roles(text, view)
    busy = [role_id for role_id in roles if _agent_is_working(role_id)]
    idle = [role_id for role_id in roles if not _agent_is_working(role_id)]
    if not idle:
        names = " and ".join(role_id.upper() for role_id in busy)
        return {
            "ok": False,
            "error": f"{names} agent is already running. Switch to the other tab or name that role.",
            "chat": chat_snapshot(),
        }
    if busy:
        names = " and ".join(role_id.upper() for role_id in busy)
        idle_names = " and ".join(role_id.upper() for role_id in idle)
        _append(
            "assistant",
            f"{names} is already working. Starting the {idle_names} agent in parallel.",
        )
    _append("user", text)
    global CHAT_FOCUS
    CHAT_FOCUS = idle[0]
    for role_id in idle:
        begin_agent_work(role_id, "Working…")
        with CHAT_LOCK:
            AGENTS[role_id]["messages"].append({"role": "user", "text": text})
            AGENTS[role_id]["messages"] = AGENTS[role_id]["messages"][-40:]
            _persist_unlocked()
        threading.Thread(target=_execute_role, args=(role_id, text, role_id), daemon=True).start()
    return {"ok": True, "chat": chat_snapshot()}
