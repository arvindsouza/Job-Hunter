"""Natural-language orchestrator: route EM/TPM, then scrape, tailor, and re-score."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue

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
        "constraints": [],
        "refresh_filters": False,
        "stop_requested": False,
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
                "constraints": list(AGENTS[role_id].get("constraints") or [])[-12:],
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
            AGENTS[role_id]["constraints"] = [
                item for item in (saved.get("constraints") or []) if isinstance(item, str) and item.strip()
            ][-12:]
            AGENTS[role_id]["running"] = False
            AGENTS[role_id]["inflight"] = 0
            AGENTS[role_id]["refresh_filters"] = False
            AGENTS[role_id]["stop_requested"] = False
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
        bits.append(
            f"focus on the top {plan.get('top_n')} in the {plan.get('min_rating')}-{plan.get('max_rating')} range"
            + (" (every row in that band)" if plan.get("all") else "")
        )
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
    start = re.search(r"\bstart(?:\s+the)?\s+(tpm|em)\b", text)
    if start:
        return ["tpm" if start.group(1) == "tpm" else "em"]
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


def _rating_bounds_from_text(text: str) -> tuple[int | None, int | None]:
    blob = (text or "").lower()
    band = re.search(r"\b(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})\b", blob)
    if band:
        low = max(0, min(100, int(band.group(1))))
        high = max(low, min(100, int(band.group(2))))
        return low, high
    below = re.search(
        r"\b(?:rated?\s+)?(\d{1,3})\s*(?:and\s+)?(?:below|under|or less|or fewer)\b"
        r"|\b(?:below|under|at most|max(?:imum)?(?:\s+of)?)\s+(\d{1,3})\b",
        blob,
    )
    if below:
        n = int(below.group(1) or below.group(2))
        return 0, max(0, min(100, n))
    above = re.search(
        r"\b(?:rated?\s+)?(\d{1,3})\s*(?:\+|and\s+)?(?:above|over|or more)\b"
        r"|\b(?:above|over|at least|min(?:imum)?(?:\s+of)?)\s+(\d{1,3})\b",
        blob,
    )
    if above:
        n = int(above.group(1) or above.group(2))
        return max(0, min(100, n)), 100
    return None, None


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
    want_all = bool(re.search(r"\b(all|every|entire|everything)\b", text))
    number_mentioned = bool(numbers) or want_all
    top_n = 200 if want_all else next((n for n in numbers if 1 <= n <= 100), 10)
    if top_n > 50 and not want_all:
        top_n = min(top_n, 100)
    time_mentioned = bool(re.search(r"24\s*h|today|past day|last day|\bweek\b", text))
    time_range = "24h" if re.search(r"24\s*h|today|past day|last day", text) else "week"
    bound_min, bound_max = _rating_bounds_from_text(text)
    if bound_min is not None and bound_max is not None:
        min_rating, max_rating = bound_min, bound_max
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
            r"fresh(?:\s+run)?|scrape|pull|fetch|refresh|search|"
            r"this week|find(?:\s+me)?|get me|look for|hunt|"
            r"new search|search for|search again|"
            r"new jobs?|new listings?|new roles?"
            r")\b",
            text,
        )
    )
    scrape = bool(force_scrape) and not force_skip
    skip_tailor = bool(re.search(r"skip tailor|don'?t tailor|without tailor", text))
    skip_reassess = bool(re.search(r"skip re-?score|skip re-?assess", text))
    list_only = bool(
        re.search(
            r"just list|only list|list only|only fetch|only scrape|"
            r"show me (?:the )?(?:table|jobs|listings)|"
            r"what(?:'s| is) in the table|how many",
            text,
        )
    )
    mentioned_tailor = bool(re.search(r"tailor|custom resume|rewrite resume", text))
    mentioned_reassess = bool(
        re.search(r"re-?score|re-?assess|re-?analy|which to apply|tell me which", text)
    )
    process_table = bool(
        re.search(r"\b(process|handle|continue|go ahead|do (?:them|those|the jobs))\b", text)
    )
    work = (scrape or force_skip or process_table or mentioned_tailor or mentioned_reassess) and not list_only
    tailor = (mentioned_tailor or work) and not skip_tailor and not list_only
    reassess = (mentioned_reassess or tailor or work) and not skip_reassess and not list_only

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
        if bound_min is not None and bound_max is not None:
            merged["min_rating"] = min_rating
            merged["max_rating"] = max_rating
        elif want_all:
            merged["min_rating"] = merged.get("min_rating", 0)
            merged["max_rating"] = merged.get("max_rating", 100)
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
        top_n = 200 if want_all else max(1, min(200, int(top_n if top_n not in (None, "all") else fallback.get("top_n") or 10)))
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
    bound_min, bound_max = _rating_bounds_from_text(message)
    if bound_min is not None and bound_max is not None:
        min_rating, max_rating = bound_min, bound_max
    time_range = data.get("time_range") if data.get("time_range") in ("24h", "week") else fallback.get("time_range") or "week"
    scrape = bool(data.get("scrape"))
    tailor = bool(data.get("tailor"))
    reassess = bool(data.get("reassess")) or tailor
    intent = str(data.get("intent") or fallback.get("intent") or "hunt").lower()
    if intent not in ("hunt", "filter", "answer", "stop"):
        intent = "hunt" if (scrape or tailor or reassess) else "answer"
    act = data.get("act")
    if act is None:
        act = intent in ("hunt", "filter", "stop") or scrape or tailor or reassess
    reply = str(data.get("reply") or "").strip()
    steps = _normalize_steps(data.get("steps"), scrape=scrape, tailor=tailor, reassess=reassess, intent=intent)
    question = str(data.get("question") or "").strip()
    plan = {
        "roles": roles,
        "scrape": scrape,
        "tailor": tailor,
        "reassess": reassess,
        "top_n": top_n,
        "min_rating": min_rating,
        "max_rating": max_rating,
        "time_range": time_range,
        "max_pulls": min(200, max(50, min(int(top_n or 10), 50) * 5)),
        "all": want_all,
        "act": bool(act),
        "intent": intent,
        "steps": steps,
        "question": question,
        "followup": bool(data.get("followup") or fallback.get("followup")),
    }
    plan["summary"] = _plan_summary(plan)
    plan["reply"] = reply or _describe_plan(plan, followup=plan["followup"])
    return plan


COMPILE_SYSTEM = """You compile a user's chat into an execution plan for Bengaluru EM and/or TPM job agents.
You rationalize the request, then output a sequence of steps. You do not invent a third role.

intent:
- hunt: fetch, search, tailor, re-score, or process the table
- filter: skip/ignore constraints for a run that is already in progress (too technical, ignore EM titles)
- answer: a question about what already happened or what is in the table (did you..., have you..., how many...)
- stop: they said stop

If they say "everything 80 and below" / "80 and below" / "under 80": min_rating=0, max_rating=80.
"everything" means every row IN THAT BAND, not 0-100.
If they name TPM, roles=["tpm"] even if the UI tab is EM. If they name both, roles=["em","tpm"].
If they say start the TPM (or EM) agent and send it my question: intent=answer, that role, question=the previous user question.

steps: ordered objects {"tool": "scrape"|"list_fits"|"process_fits"|"report"|"stop"}.
- scrape only if they want new jobs
- list_fits then process_fits for tailor/re-score
- report for answer
Default hunt: scrape (if needed), list_fits, process_fits.
Return JSON only."""


def _normalize_steps(raw, scrape: bool, tailor: bool, reassess: bool, intent: str) -> list[dict]:
    allowed = {"scrape", "list_fits", "process_fits", "report", "stop"}
    steps: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                tool = item
            elif isinstance(item, dict):
                tool = str(item.get("tool") or item.get("action") or "")
            else:
                continue
            if tool in allowed and tool not in {step["tool"] for step in steps}:
                steps.append({"tool": tool})
    if steps:
        return steps
    if intent == "answer":
        return [{"tool": "report"}]
    if intent == "stop":
        return [{"tool": "stop"}]
    if intent == "filter":
        return []
    if scrape:
        steps.append({"tool": "scrape"})
    steps.append({"tool": "list_fits"})
    if tailor or reassess:
        steps.append({"tool": "process_fits"})
    return steps


def _previous_user_text(current: str) -> str:
    with CHAT_LOCK:
        users = [str(item.get("text") or "").strip() for item in CHAT_LOG if item.get("role") == "user"]
    current = (current or "").strip()
    for text in reversed(users):
        if text and text != current:
            return text
    return current


def _compile_instruction(message: str, view_role: str, role_hint: str = "") -> dict:
    hint = role_hint if role_hint in ("em", "tpm") else (view_role if view_role in ("em", "tpm") else "em")
    inventory = _inventory(None)
    with CHAT_LOCK:
        last = dict(AGENTS[hint].get("last_plan") or {})
        recent = list(CHAT_LOG[-20:])
    fallback = _heuristic_plan(message, inventory, view_role or hint, last_plan=last)
    text = (message or "").lower()
    if re.search(r"\b(did you|have you|was it|were they|how many|status)\b", text):
        fallback["intent"] = "answer"
        fallback["act"] = False
        fallback["scrape"] = False
    elif re.search(r"^\s*stop\b", text):
        fallback["intent"] = "stop"
        fallback["act"] = True
    else:
        fallback["intent"] = "hunt" if fallback.get("act") else "answer"
    fallback["question"] = message
    if re.search(r"\bstart(?:\s+the)?\s+(tpm|em)\b", text) and re.search(r"\bquestion\b", text):
        fallback["intent"] = "answer"
        fallback["question"] = _previous_user_text(message)
        fallback["act"] = False
        fallback["scrape"] = False
    fallback["steps"] = _normalize_steps(
        None,
        scrape=bool(fallback.get("scrape")),
        tailor=bool(fallback.get("tailor")),
        reassess=bool(fallback.get("reassess")),
        intent=str(fallback.get("intent") or "hunt"),
    )
    if not llm_available():
        return fallback
    try:
        data = complete_json(
            COMPILE_SYSTEM,
            (
                f"UI role hint: {view_role or hint}. This is a hint, not an override if the user named a role.\n"
                f"Table inventory JSON: {inventory}\n"
                f"Last plan JSON: {last}\n"
                f"Recent chat JSON: {recent}\n"
                f"Previous user question: {_previous_user_text(message)}\n"
                f"Latest user message: {message}\n\n"
                "Return JSON with:\n"
                "reply (1-3 sentences),\n"
                "intent (hunt|filter|answer|stop),\n"
                "roles (array of em and/or tpm),\n"
                "question (string, for intent=answer),\n"
                "act (boolean),\n"
                "followup (boolean),\n"
                "scrape, tailor, reassess (booleans),\n"
                "top_n (integer or all),\n"
                "all (boolean: every row in the selected rating band),\n"
                "min_rating, max_rating (0-100),\n"
                "time_range (24h or week),\n"
                "steps (array of {tool})."
            ),
            timeout=60,
        )
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    return _normalize_llm_plan(
        data, fallback, message=message, view_role=view_role, last_plan=last
    )


def _conversation_plan(message: str, inventory: dict, view_role: str, role_id: str) -> dict:
    plan = _compile_instruction(message, view_role, role_id)
    plan["roles"] = [role_id]
    return plan


def _answer_from_table(role_id: str, question: str, plan: dict) -> None:
    from common import ROLE_META, read_jobs_file

    label = ROLE_META[role_id]["label"]
    rows = list((read_jobs_file().get("jobs") or {}).get(ROLE_META[role_id]["search"]) or [])
    min_rating = int(plan.get("min_rating") or 0)
    max_rating = int(plan.get("max_rating") or 100)
    band = []
    for row in rows:
        rating = row.get("rating")
        if rating is None or rating == "":
            band.append(row)
            continue
        try:
            score = float(rating)
        except (TypeError, ValueError):
            band.append(row)
            continue
        if min_rating <= score <= max_rating:
            band.append(row)
    tailored = [
        row
        for row in band
        if "tailor" in str(row.get("note") or "").lower() or row.get("reassessed")
    ]
    facts = {
        "role": role_id,
        "listed": len(rows),
        "in_band": len(band),
        "min_rating": min_rating,
        "max_rating": max_rating,
        "tailored_in_band": len(tailored),
        "sample": [
            {
                "title": row.get("title"),
                "company": row.get("company"),
                "rating": row.get("rating"),
                "note": row.get("note"),
            }
            for row in tailored[:8]
        ],
    }
    if llm_available():
        try:
            payload = complete_json(
                (
                    f"You are the {label} agent. Answer only from the facts JSON. "
                    "If the work was incomplete, say so plainly. Return JSON "
                    '{"reply": "2-6 sentences"}.'
                ),
                json.dumps({"question": question, "facts": facts}, ensure_ascii=False)[:8000],
                attempts=2,
                timeout=45,
            )
            reply = str((payload or {}).get("reply") or "").strip()
            if reply:
                _append("assistant", reply, role_id)
                return
        except Exception:
            pass
    _append(
        "assistant",
        (
            f"{label}: {facts['listed']} listed, {facts['in_band']} in the "
            f"{min_rating}-{max_rating} band, {facts['tailored_in_band']} of those tailored."
        ),
        role_id,
    )


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


AGENT_MAX_TAILORS = 30
AGENT_MAX_MINUTES = 20
AGENT_MAX_LLM_FAILS = 3
AGENT_EARLY_STOP_HIGH = 10
AGENT_EARLY_STOP_SCORE = 90
AGENT_MAX_POOL_WORKERS = 6
AGENT_JOBS_PER_WORKER = 8

DECIDE_SYSTEM = """You are one Bengaluru job-hunt agent (EM or TPM). You only work that agent's table.
Each turn you observe live state and pick the next tool. You are not locked to a script.
Allowed tools: scrape, list_fits, fetch_jd, assess_one, tailor_one, reassess_one, update_row, stop.
Return JSON only: {"tool": "...", "args": {}, "reason": "..."}.

Default hunt unless the user said list-only: scrape if they want new jobs and you have not scraped; then list_fits; then for each remaining fit assess_one, then tailor_one, then reassess_one; then stop.
Always assess_one before tailoring. Skip Engineering Manager titles on TPM (and TPM titles on EM). If the user said to skip too-technical or similar JDs, assess against that and skip those jobs.
Obey new mid-run instructions for remaining jobs. Never tailor more than one job per call. Never pass a jobs array longer than 1. Never touch the other role's table.
Prefer stop when the goal is done, budgets are hit, or the user asked to stop.
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


def _agent_constraints(role_id: str) -> list[str]:
    with CHAT_LOCK:
        return [item for item in (AGENTS[role_id].get("constraints") or []) if str(item).strip()]


def _consume_filter_refresh(role_id: str) -> bool:
    with CHAT_LOCK:
        flag = bool(AGENTS[role_id].get("refresh_filters"))
        AGENTS[role_id]["refresh_filters"] = False
        if flag:
            _persist_unlocked()
        return flag


def _stop_requested(role_id: str) -> bool:
    with CHAT_LOCK:
        return bool(AGENTS[role_id].get("stop_requested"))


def add_live_instruction(role_id: str, text: str) -> None:
    from common import ROLE_META

    cleaned = (text or "").strip()
    if not cleaned or role_id not in AGENTS:
        return
    with CHAT_LOCK:
        constraints = list(AGENTS[role_id].get("constraints") or [])
        constraints.append(cleaned)
        AGENTS[role_id]["constraints"] = constraints[-12:]
        AGENTS[role_id]["refresh_filters"] = True
        if re.search(r"^\s*stop\b", cleaned, re.I):
            AGENTS[role_id]["stop_requested"] = True
        _persist_unlocked()
    _append(
        "assistant",
        f"{ROLE_META[role_id]['label']}: I'll apply that to the rest of this run.",
        role_id,
    )


def _is_new_hunt(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"fresh(?:\s+run)?|scrape|pull|fetch|refresh|search|"
            r"find(?:\s+me)?|look for|hunt|new jobs?"
            r")\b",
            (text or "").lower(),
        )
    )


def _is_live_instruction(text: str) -> bool:
    blob = (text or "").lower()
    if re.search(r"\b(did you|have you|was it|were they|how many|start(?:\s+the)?\s+(?:tpm|em))\b", blob):
        return False
    return bool(
        re.search(
            r"\b("
            r"skip|ignore|avoid|except|don't|do not|"
            r"too technical|hands-?on|"
            r"not em|not tpm|only (?:em|tpm)"
            r")\b",
            blob,
        )
    )


def _fits_preview(state: dict) -> list[dict]:
    preview = []
    for job in (state.get("fits") or [])[:12]:
        preview.append(
            {
                "title": job.get("title"),
                "company": job.get("company"),
                "rating": job.get("rating"),
                "assessed": bool(job.get("_assessed")),
                "skip": bool(job.get("_skip")),
                "reassessed": bool(job.get("reassessed")),
            }
        )
    return preview


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
    max_tailors = min(int(plan.get("top_n") or AGENT_MAX_TAILORS), AGENT_MAX_TAILORS)
    for job in fits:
        key = _job_key(job)
        if key in skipped or job.get("_skip") or not key[0] or not key[1]:
            continue
        has_pdf = load_tailored_resume(job.get("company"), job.get("title"))
        if not job.get("_assessed"):
            return {"tool": "assess_one", "args": _job_args(job)}
        if plan.get("tailor") and not has_pdf:
            if int(state.get("tailored") or 0) >= max_tailors:
                continue
            return {"tool": "tailor_one", "args": _job_args(job)}
        if plan.get("reassess") and has_pdf and (
            not job.get("reassessed") or _missing_market_fields(job)
        ):
            return {"tool": "reassess_one", "args": _job_args(job)}
    return {"tool": "stop", "args": {"reason": "all selected jobs done"}}


def _missing_market_fields(job: dict) -> bool:
    if not str(job.get("ctc") or "").strip():
        return True
    return job.get("callback") in (None, "")


def _pool_workers(batch_size: int) -> int:
    n = max(0, int(batch_size or 0))
    if n <= 1:
        return 1
    return min(AGENT_MAX_POOL_WORKERS, max(2, (n + AGENT_JOBS_PER_WORKER - 1) // AGENT_JOBS_PER_WORKER))


def _pending_jobs(role_id: str, plan: dict, state: dict, tool: str) -> list[dict]:
    from resume_tailor import load_tailored_resume

    skipped = state.get("skipped") or set()
    max_tailors = min(int(plan.get("top_n") or AGENT_MAX_TAILORS), AGENT_MAX_TAILORS)
    already = int(state.get("tailored") or 0)
    pending: list[dict] = []
    for job in state.get("fits") or []:
        key = _job_key(job)
        if key in skipped or job.get("_skip") or not key[0] or not key[1]:
            continue
        has_pdf = load_tailored_resume(job.get("company"), job.get("title"))
        if tool == "tailor_one":
            if not plan.get("tailor") or has_pdf:
                continue
            if already + len(pending) >= max_tailors:
                continue
            pending.append(job)
        elif tool == "reassess_one":
            if not plan.get("reassess") or not has_pdf:
                continue
            if job.get("reassessed") and not _missing_market_fields(job):
                continue
            pending.append(job)
    return pending


def _announce_job_outcome(role_id: str, label: str, outcome: dict) -> None:
    tool = outcome.get("tool")
    title = str(outcome.get("title") or "")
    company = str(outcome.get("company") or "")
    if not outcome.get("ok"):
        _append("assistant", f"{label}: {outcome.get('error') or f'{tool} failed'}", role_id)
        return
    if outcome.get("skip"):
        _append(
            "assistant",
            f"{label}: skipping {title} at {company} — {outcome.get('reason') or 'filtered'}.",
            role_id,
        )
        return
    if tool not in ("tailor_one", "reassess_one"):
        return
    row = outcome.get("row") if isinstance(outcome.get("row"), dict) else {}
    rating = row.get("rating")
    parts = [f"{label}: updated {title} at {company}"]
    if outcome.get("filename"):
        parts.append(str(outcome["filename"]))
    if isinstance(rating, (int, float)):
        parts.append(f"{int(rating)}/100")
    _append("assistant", " — ".join(parts), role_id)


def _run_job_pool(
    role_id: str,
    tool: str,
    jobs: list[dict],
    tool_plan: dict,
    state: dict,
) -> dict | None:
    from agent_tools import run_tool
    from common import ROLE_META

    label = ROLE_META[role_id]["label"]
    if not jobs:
        return None
    workers = _pool_workers(len(jobs))
    verb = "tailor" if tool == "tailor_one" else "re-score"
    _append(
        "assistant",
        f"{label}: {verb} {len(jobs)} job(s) with {workers} worker(s).",
        role_id,
    )
    last: dict | None = None
    work_q: Queue = Queue()
    for job in jobs:
        work_q.put(job)
    state_lock = threading.Lock()
    last_box: dict = {"outcome": None}

    def _worker() -> None:
        while True:
            if _budget_stop(role_id, tool_plan, state):
                return
            try:
                job = work_q.get_nowait()
            except Empty:
                return
            title = str(job.get("title") or "")
            company = str(job.get("company") or "")
            _set_agent(role_id, status=f"{label}: {tool} {title} at {company}")
            try:
                outcome = run_tool(role_id, tool, _job_args(job), tool_plan)
            except Exception as exc:
                outcome = {
                    "ok": False,
                    "tool": tool,
                    "error": str(exc),
                    "title": title,
                    "company": company,
                }
            with state_lock:
                if not outcome.get("ok"):
                    state["errors"].append(str(outcome.get("error") or f"{tool} failed"))
                    last_box["outcome"] = outcome
                else:
                    last_box["outcome"] = None
                _apply_tool_result(state, outcome)
                _announce_job_outcome(role_id, label, outcome)

    _set_agent(role_id, status=f"{label}: {verb} {len(jobs)} with {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker) for _ in range(workers)]
        for future in futures:
            future.result()
    last = last_box["outcome"]
    return last


def _guard_action(role_id: str, action: dict, state: dict) -> dict:
    from agent_tools import TOOLS

    tool = str(action.get("tool") or "")
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    if tool not in TOOLS:
        return _rule_next(role_id, state.get("plan") or {}, state)
    jobs = args.get("jobs")
    if isinstance(jobs, list) and jobs:
        first = jobs[0] if isinstance(jobs[0], dict) else {}
        args = {**first, **{k: v for k, v in args.items() if k != "jobs"}}
    if tool in ("tailor_one", "reassess_one"):
        key = (str(args.get("title") or "").strip(), str(args.get("company") or "").strip())
        for job in state.get("fits") or []:
            if _job_key(job) != key:
                continue
            if job.get("_skip") or key in (state.get("skipped") or set()):
                return _rule_next(role_id, state.get("plan") or {}, state)
            break
    return {"tool": tool, "args": args, "reason": str(action.get("reason") or "")}


def _llm_decide(role_id: str, plan: dict, state: dict, last: dict | None) -> tuple[dict | None, str | None]:
    from agent_tools import TOOLS

    suggested = _rule_next(role_id, plan, state)
    user = json.dumps(
        {
            "role": role_id,
            "user_instructions": _agent_constraints(role_id)[-8:],
            "suggested": suggested,
            "plan": {
                key: plan.get(key)
                for key in ("scrape", "tailor", "reassess", "top_n", "min_rating", "max_rating", "act")
            },
            "inventory": _inventory(role_id),
            "scraped": bool(state.get("scraped")),
            "fit_count": len(state.get("fits") or []),
            "fits": _fits_preview(state),
            "tailored": state.get("tailored") or 0,
            "reassessed": state.get("reassessed") or 0,
            "skipped": [
                {"title": key[0], "company": key[1]} for key in list(state.get("skipped") or [])[:8]
            ],
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
    return _guard_action(role_id, {"tool": tool, "args": args, "reason": payload.get("reason")}, state), None


def _budget_stop(role_id: str, plan: dict, state: dict) -> dict | None:
    if _stop_requested(role_id):
        return {"tool": "stop", "args": {"reason": "stopped because you asked"}}
    elapsed = time.time() - float(state.get("started") or time.time())
    if elapsed >= AGENT_MAX_MINUTES * 60:
        return {"tool": "stop", "args": {"reason": f"hit the {AGENT_MAX_MINUTES}-minute budget"}}
    if int(state.get("llm_fails") or 0) >= AGENT_MAX_LLM_FAILS:
        return {
            "tool": "stop",
            "args": {"reason": f"hit {AGENT_MAX_LLM_FAILS} consecutive LLM decision failures"},
        }
    high = 0
    for job in state.get("fits") or []:
        rating = job.get("rating")
        if job.get("reassessed") and isinstance(rating, (int, float)) and rating >= AGENT_EARLY_STOP_SCORE:
            high += 1
    if high >= AGENT_EARLY_STOP_HIGH:
        return {
            "tool": "stop",
            "args": {"reason": f"{high} jobs already at {AGENT_EARLY_STOP_SCORE}+; stopping early"},
        }
    return None


def _reload_fit_row(role_id: str, state: dict, title: str, company: str, link: str) -> dict | None:
    from common import ROLE_META, read_jobs_file

    rows = (read_jobs_file().get("jobs") or {}).get(ROLE_META[role_id]["search"]) or []
    matched = None
    for row in rows:
        if row.get("title") != title or row.get("company") != company:
            continue
        if link and row.get("link") and row.get("link") != link:
            continue
        matched = row
        break
    if matched is None:
        return None
    for index, job in enumerate(state.get("fits") or []):
        if _job_key(job) != (title, company):
            continue
        merged = dict(job)
        merged.update(matched)
        state["fits"][index] = merged
        break
    return matched


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
    if outcome.get("skip"):
        for index, job in enumerate(state.get("fits") or []):
            if _job_key(job) != key:
                continue
            merged = dict(job)
            merged["_assessed"] = True
            merged["_skip"] = True
            state["fits"][index] = merged
            break
        state["skipped"].add(key)
        return
    if tool == "assess_one":
        for index, job in enumerate(state.get("fits") or []):
            if _job_key(job) != key:
                continue
            merged = dict(job)
            merged.update(row)
            merged["_assessed"] = True
            merged["_skip"] = bool(outcome.get("skip"))
            if outcome.get("description"):
                merged["description"] = outcome.get("description")
            state["fits"][index] = merged
            break
        if outcome.get("skip"):
            state["skipped"].add(key)
        return
    if outcome.get("tailored") or tool == "tailor_one":
        state["tailored"] = int(state.get("tailored") or 0) + 1
    if outcome.get("reassessed") or (row.get("reassessed") and tool == "reassess_one"):
        state["reassessed"] = int(state.get("reassessed") or 0) + 1
        rating = row.get("rating")
        if isinstance(rating, (int, float)) and rating >= 80:
            state["apply"].append(f"{key[0]} at {key[1]} ({int(rating)}/100)")


def pop_agent_queue(role_id: str) -> dict | None:
    with CHAT_LOCK:
        queue = AGENTS[role_id].setdefault("queue", [])
        if not queue:
            return None
        item = queue.pop(0)
        _persist_unlocked()
        return item if isinstance(item, dict) else None


def enqueue_agent_job(role_id: str, tool: str, args: dict) -> dict:
    from common import ROLE_META, load_agent_config

    if role_id not in AGENTS or tool not in ("tailor_one", "reassess_one"):
        return {"ok": False, "error": "Unsupported row action.", "chat": chat_snapshot()}
    if not load_agent_config(role_id):
        return {
            "ok": False,
            "error": f"Upload a {ROLE_META[role_id]['label']} resume first.",
            "chat": chat_snapshot(),
        }
    item = {
        "tool": tool,
        "args": args if isinstance(args, dict) else {},
        "plan": {"scrape": False, "tailor": tool == "tailor_one", "reassess": True},
    }
    start_worker = False
    with CHAT_LOCK:
        AGENTS[role_id].setdefault("queue", []).append(item)
        if not AGENTS[role_id]["running"]:
            AGENTS[role_id]["inflight"] = int(AGENTS[role_id].get("inflight") or 0) + 1
            AGENTS[role_id]["running"] = True
            AGENTS[role_id]["status"] = f"Queued {tool}"
            start_worker = True
        _persist_unlocked()
    title = str((args or {}).get("title") or "job")
    verb = "tailor" if tool == "tailor_one" else "re-score"
    _append("assistant", f"{ROLE_META[role_id]['label']}: queued {verb} for {title}.", role_id)
    if start_worker:
        threading.Thread(target=_run_queue_agent, args=(role_id,), daemon=True).start()
    return {"ok": True, "queued": True, "chat": chat_snapshot()}


def _run_queue_agent(role_id: str) -> None:
    try:
        plan = {
            "scrape": False,
            "tailor": True,
            "reassess": True,
            "top_n": 1,
            "min_rating": 0,
            "max_rating": 100,
        }
        item = _run_role(role_id, plan, queue_only=True)
        lines = [
            f"{role_id.upper()} queued work done.",
            f"{item['tailored']} tailored, {item['reassessed']} re-scored.",
        ]
        for error in (item.get("errors") or [])[:4]:
            if error:
                lines.append(str(error))
        _append("assistant", "\n".join(lines), role_id)
    except Exception as exc:
        _append("assistant", f"{role_id.upper()} queued work stopped: {exc}", role_id)
    finally:
        end_agent_work(role_id)


def _run_role(role_id: str, plan: dict, queue_only: bool = False) -> dict:
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
        "started": time.time(),
        "plan": plan,
    }
    if not load_agent_config(role_id):
        state["errors"].append(f"Upload a {label} resume first.")
        _append("assistant", f"{label}: skipped — no resume uploaded.", role_id)
        return state

    if not queue_only:
        if plan.get("scrape"):
            _append("assistant", f"{label}: clearing the table and scraping new listings…", role_id)
        else:
            _append("assistant", f"{label}: skipping scrape and using the current table.", role_id)

    last: dict | None = None
    while True:
        queued = pop_agent_queue(role_id)
        tool_plan = dict(plan)
        tool_plan["constraints"] = _agent_constraints(role_id)
        if _consume_filter_refresh(role_id):
            for job in state.get("fits") or []:
                if not job.get("_skip"):
                    job["_assessed"] = False
        if queued:
            action = {
                "tool": str(queued.get("tool") or ""),
                "args": queued.get("args") if isinstance(queued.get("args"), dict) else {},
            }
            if isinstance(queued.get("plan"), dict):
                tool_plan = {**tool_plan, **queued["plan"]}
                tool_plan["constraints"] = _agent_constraints(role_id)
            last = None
        elif queue_only:
            break
        else:
            suggested = _rule_next(role_id, plan, state)
            if last is not None and not last.get("ok"):
                skip_key = _job_key(last)
                if skip_key[0]:
                    state["skipped"].add(skip_key)
            if suggested.get("tool") == "assess_one" and plan.get("tailor"):
                suggested = {"tool": "tailor_one", "args": suggested.get("args") or {}}
            if suggested.get("tool") in ("tailor_one", "reassess_one"):
                action = suggested
            elif llm_available():
                decided, error = _llm_decide(role_id, plan, state, last)
                if decided:
                    action = decided
                    state["llm_fails"] = 0
                else:
                    state["llm_fails"] = int(state.get("llm_fails") or 0) + 1
                    if error:
                        state["errors"].append(error)
                    action = suggested
            else:
                action = suggested
            last = None
            action = _guard_action(role_id, action, state)
        if not queued:
            budget = _budget_stop(role_id, plan, state)
            if budget:
                action = budget
        if not queued and action.get("tool") in ("tailor_one", "reassess_one"):
            pending = _pending_jobs(role_id, plan, state, action["tool"])
            if pending:
                last = _run_job_pool(role_id, action["tool"], pending, tool_plan, state)
                continue
        if not queued and action.get("tool") == "stop":
            reason = (action.get("args") or {}).get("reason") or "stopped"
            if "budget" in str(reason) or "failures" in str(reason) or "stopping early" in str(reason):
                _append("assistant", f"{label}: {reason}.", role_id)
            elif "no jobs" in str(reason):
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
        elif action.get("tool") in ("assess_one", "tailor_one", "reassess_one"):
            args = action.get("args") or {}
            _set_agent(
                role_id,
                status=f"{label}: {action['tool']} {args.get('title')} at {args.get('company')}",
            )

        outcome = run_tool(role_id, action["tool"], action.get("args") or {}, tool_plan)
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
        elif outcome.get("tool") == "assess_one":
            title = str(outcome.get("title") or "")
            company = str(outcome.get("company") or "")
            if outcome.get("skip"):
                _append(
                    "assistant",
                    f"{label}: skipping {title} at {company} — {outcome.get('reason') or 'filtered'}.",
                    role_id,
                )
        elif outcome.get("tool") in ("tailor_one", "reassess_one"):
            title = str(outcome.get("title") or "")
            company = str(outcome.get("company") or "")
            row = _reload_fit_row(role_id, state, title, company, str(outcome.get("link") or ""))
            row = row or (outcome.get("row") if isinstance(outcome.get("row"), dict) else {})
            rating = row.get("rating")
            parts = [f"{label}: updated {title} at {company}"]
            if outcome.get("filename"):
                parts.append(str(outcome["filename"]))
            if isinstance(rating, (int, float)):
                parts.append(f"{int(rating)}/100")
            _append("assistant", " — ".join(parts), role_id)
        last = outcome if not outcome.get("ok") else None
    return {
        "role": role_id,
        "jobs": state["jobs"],
        "tailored": state["tailored"],
        "reassessed": state["reassessed"],
        "apply": state["apply"],
        "errors": state["errors"],
    }


def _execute_role(role_id: str, message: str, view_role: str = "", plan: dict | None = None) -> None:
    from common import save_settings

    try:
        if plan is None:
            plan = _conversation_plan(message, _inventory(role_id), view_role, role_id)
        plan = dict(plan)
        plan["roles"] = [role_id]
        intent = str(plan.get("intent") or "hunt")
        question = str(plan.get("question") or message)
        _set_agent(role_id, plan=plan, constraints=[message], stop_requested=False, refresh_filters=False)
        steps = plan.get("steps") or []
        if steps:
            step_names = ", ".join(str(step.get("tool")) for step in steps)
            _append(
                "assistant",
                (
                    plan.get("reply")
                    or _describe_plan(plan, followup=bool(plan.get("followup")))
                )
                + f" Steps: {step_names}. Band {plan.get('min_rating')}-{plan.get('max_rating')}.",
                role_id,
            )
        else:
            _append(
                "assistant",
                plan.get("reply") or _describe_plan(plan, followup=bool(plan.get("followup"))),
                role_id,
            )
        if intent == "answer":
            _answer_from_table(role_id, question, plan)
            return
        if intent == "filter":
            add_live_instruction(role_id, message)
            return
        if intent == "stop":
            with CHAT_LOCK:
                AGENTS[role_id]["stop_requested"] = True
                _persist_unlocked()
            _append("assistant", f"{role_id.upper()}: stop requested.", role_id)
            return
        if not plan.get("act"):
            return
        _set_agent(role_id, last_plan=plan)
        save_settings({"time_range": plan["time_range"], "max_pulls": plan["max_pulls"]})
        item = _run_role(role_id, plan)
        if any(step.get("tool") == "report" for step in steps):
            _answer_from_table(role_id, question or message, plan)
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
    named = _mentioned_roles(text)
    roles = named or explicit or _dispatch_roles(text, view)
    busy = [role_id for role_id in roles if _agent_is_working(role_id)]
    idle = [role_id for role_id in roles if not _agent_is_working(role_id)]
    _append("user", text)
    local_intent = "hunt"
    low = text.lower()
    if re.search(r"^\s*stop\b", low):
        local_intent = "stop"
    elif _is_live_instruction(text) and not _is_new_hunt(text):
        local_intent = "filter"
    elif re.search(
        r"\b(did you|have you|was it|were they|how many|what about)\b|"
        r"\bstart(?:\s+the)?\s+(?:tpm|em)\b.*\bquestion\b",
        low,
    ):
        local_intent = "answer"

    if local_intent == "filter" and (busy or idle):
        targets = busy or roles
        for role_id in targets:
            add_live_instruction(role_id, text)
        return {"ok": True, "chat": chat_snapshot()}
    if local_intent == "stop":
        for role_id in roles:
            with CHAT_LOCK:
                AGENTS[role_id]["stop_requested"] = True
                _persist_unlocked()
            _append("assistant", f"{role_id.upper()}: stop requested.", role_id)
        return {"ok": True, "chat": chat_snapshot()}
    if local_intent == "answer":
        compiled = _compile_instruction(
            text, view or (named[0] if named else ""), (named or explicit or ["em"])[0]
        )
        roles = named or [role for role in (compiled.get("roles") or []) if role in ("em", "tpm")] or roles
        question = str(compiled.get("question") or text)
        for role_id in roles:
            _answer_from_table(role_id, question, compiled)
        return {"ok": True, "chat": chat_snapshot()}

    if not idle:
        names = " and ".join(role_id.upper() for role_id in busy)
        return {
            "ok": False,
            "error": f"{names} agent is already running. Send a filter, ask a question, or wait.",
            "chat": chat_snapshot(),
        }
    if busy:
        names = " and ".join(role_id.upper() for role_id in busy)
        idle_names = " and ".join(role_id.upper() for role_id in idle)
        _append(
            "assistant",
            f"{names} is already working. Starting the {idle_names} agent in parallel.",
        )
    global CHAT_FOCUS
    CHAT_FOCUS = idle[0]
    for role_id in idle:
        begin_agent_work(role_id, "Working…")
        with CHAT_LOCK:
            AGENTS[role_id]["messages"].append({"role": "user", "text": text})
            AGENTS[role_id]["messages"] = AGENTS[role_id]["messages"][-40:]
            _persist_unlocked()
        threading.Thread(
            target=_execute_role,
            args=(role_id, text, role_id),
            daemon=True,
        ).start()
    return {"ok": True, "chat": chat_snapshot()}
