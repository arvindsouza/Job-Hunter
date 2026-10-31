"""Free-tier LLM helpers for resume context and JD-vs-resume scoring."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "Assets"
ENV_PATH = PROJECT_ROOT / ".env"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_MODELS = (
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
)
_GEMINI_MODELS_CACHE: tuple[str, ...] | None = None
GROQ_MODELS = (
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)

_ENV_LOADED = False


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def groq_key() -> str:
    load_env()
    return (os.environ.get("GROQ_API_KEY") or "").strip()


def gemini_key() -> str:
    load_env()
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def llm_available() -> bool:
    return bool(groq_key() or gemini_key())


def llm_provider_name() -> str:
    if groq_key():
        return "groq"
    if gemini_key():
        return "gemini"
    return "none"


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM did not return JSON")
    blob = cleaned[start : end + 1]
    blob = re.sub(r",\s*([}\]])", r"\1", blob)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        quoted = re.sub(r"([\{,]\s*)([A-Za-z_][\w]*)\s*:", r'\1"\2":', blob)
        return json.loads(quoted)


def _http_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _complete_groq(system: str, user: str, timeout: int = 60) -> str:
    key = groq_key()
    last_error = None
    for model in GROQ_MODELS:
        try:
            data = _http_json(
                GROQ_URL,
                {
                    "model": model,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(last_error or "Groq request failed")


def _list_gemini_models(key: str) -> tuple[str, ...]:
    global _GEMINI_MODELS_CACHE
    if _GEMINI_MODELS_CACHE:
        return _GEMINI_MODELS_CACHE
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        _GEMINI_MODELS_CACHE = GEMINI_MODELS
        return _GEMINI_MODELS_CACHE
    skip = ("tts", "image", "lyria", "computer-use", "robotics", "deep-research", "transcribe")
    found: list[str] = []
    for item in payload.get("models") or []:
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        name = str(item.get("name") or "").split("/")[-1]
        lowered = name.lower()
        if any(part in lowered for part in skip):
            continue
        found.append(name)
    preferred = [name for name in GEMINI_MODELS if name in found]
    rest = [name for name in found if name not in preferred]
    _GEMINI_MODELS_CACHE = tuple(preferred + rest) or GEMINI_MODELS
    return _GEMINI_MODELS_CACHE


def _complete_gemini(system: str, user: str, timeout: int = 60) -> str:
    key = gemini_key()
    last_error = None
    prompt = f"{system}\n\n{user}"
    for model in _list_gemini_models(key):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}"
        )
        try:
            data = _http_json(
                url,
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "responseMimeType": "application/json",
                        "maxOutputTokens": 65536,
                    },
                },
                {"Content-Type": "application/json"},
                timeout=timeout,
            )
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts)
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(last_error or "Gemini request failed")


def complete_json(system: str, user: str, attempts: int = 3, timeout: int = 60) -> dict:
    if not llm_available():
        raise RuntimeError(
            "No LLM API key. Add GROQ_API_KEY or GEMINI_API_KEY to a .env file "
            "in the project root."
        )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            raw = (
                _complete_groq(system, user, timeout=timeout)
                if groq_key()
                else _complete_gemini(system, user, timeout=timeout)
            )
            return _parse_json(raw)
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if "429" in message or "rate" in message.lower():
                time.sleep(2 * (attempt + 1))
                continue
            if attempt + 1 < attempts:
                time.sleep(1)
                continue
            break
    raise RuntimeError(last_error or "LLM request failed")


def context_path(role: str) -> Path:
    return ASSETS_DIR / role / "context.json"


def analyses_dir(role: str) -> Path:
    path = ASSETS_DIR / role / "analyses"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_context(role: str) -> dict | None:
    path = context_path(role)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_context(role: str, payload: dict) -> dict:
    folder = ASSETS_DIR / role
    folder.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["role"] = role
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    context_path(role).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def analyze_resume(role: str, resume_text: str, role_label: str) -> dict:
    clipped = (resume_text or "").strip()[:14000]
    if not clipped:
        raise RuntimeError("Resume text is empty")
    data = complete_json(
        (
            "You extract a hiring-relevant professional profile from a resume. "
            "Return JSON only. Be specific and faithful to the document. "
            "Do not invent employers, titles, or skills."
        ),
        (
            f"Target search role: {role_label} ({role}).\n"
            "Read this resume and return JSON with:\n"
            "summary (5-8 sentences covering career arc, seniority, domains, and how they lead),\n"
            "years_experience (number or null),\n"
            "current_level (string),\n"
            "target_roles (array of strings),\n"
            "leadership (array of concrete leadership/management strengths),\n"
            "domains (array of product/industry domains),\n"
            "tech_stack (array of tools/languages/platforms actually evidenced),\n"
            "achievements (array of outcome-oriented bullets),\n"
            "management_style (short string),\n"
            "must_have_in_next_role (array),\n"
            "gaps (array of likely gaps vs the target search role).\n\n"
            f"RESUME:\n{clipped}"
        ),
    )
    data["method"] = "llm"
    data["provider"] = llm_provider_name()
    return save_context(role, data)


def ensure_resume_context(role: str, role_label: str) -> dict | None:
    existing = load_context(role)
    if existing and existing.get("method") == "llm" and existing.get("summary"):
        return existing
    resume_path = ASSETS_DIR / role / "resume.txt"
    if not resume_path.exists():
        return existing
    text = resume_path.read_text(encoding="utf-8")
    if not llm_available():
        if existing:
            return existing
        return save_context(
            role,
            {
                "method": "none",
                "summary": "",
                "error": (
                    "No LLM API key. Create a .env with GROQ_API_KEY "
                    "(https://console.groq.com/keys) or GEMINI_API_KEY "
                    "(https://aistudio.google.com/apikey)."
                ),
            },
        )
    return analyze_resume(role, text, role_label)


def _job_cache_key(title: str, company: str, description: str) -> str:
    blob = f"{title}\n{company}\n{description}".encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _context_blob(context: dict) -> dict:
    return {
        "summary": context.get("summary"),
        "years_experience": context.get("years_experience"),
        "current_level": context.get("current_level"),
        "target_roles": context.get("target_roles"),
        "leadership": context.get("leadership"),
        "domains": context.get("domains"),
        "tech_stack": context.get("tech_stack"),
        "achievements": context.get("achievements"),
        "management_style": context.get("management_style"),
        "must_have_in_next_role": context.get("must_have_in_next_role"),
        "gaps": context.get("gaps"),
    }


def _coerce_score(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(float(value)))))
    text = str(value).strip()
    match = re.search(r"(\d{1,3})(?:\s*/\s*100)?", text)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def _fit_result_from_payload(data: dict, description: str) -> dict:
    nested = data.get("resume") if isinstance(data.get("resume"), dict) else {}
    score = None
    for source in (data, nested):
        if not isinstance(source, dict):
            continue
        for key in ("rating", "score", "fit_rating", "fit"):
            score = _coerce_score(source.get(key))
            if score is not None:
                break
        if score is not None:
            break
    if score is None:
        score = 0
    matched = [str(item) for item in (data.get("matched") or nested.get("matched") or []) if str(item).strip()]
    missed = [str(item) for item in (data.get("missed") or nested.get("missed") or []) if str(item).strip()]
    why = str(data.get("why") or nested.get("why") or "").strip()
    reasoning = str(data.get("reasoning") or nested.get("reasoning") or "").strip()
    note = str(data.get("note") or nested.get("note") or "").strip()
    return {
        "score": score,
        "title_note": why,
        "matched": matched,
        "missed": missed,
        "jd_gaps": missed[:8],
        "had_description": bool((description or "").strip()),
        "why": why,
        "reasoning": reasoning,
        "note": note,
        "decision": str(data.get("decision") or "").strip().lower(),
        "method": "llm",
        "provider": llm_provider_name(),
    }


def _cache_path(role: str, title: str, company: str, description: str) -> Path:
    return analyses_dir(role) / f"{_job_cache_key(title, company, description)}.json"


def _read_cached_fit(role: str, title: str, company: str, description: str) -> dict | None:
    path = _cache_path(role, title, company, description)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(cached, dict) and "score" in cached:
        return cached
    return None


def _write_cached_fit(role: str, title: str, company: str, description: str, result: dict) -> None:
    _cache_path(role, title, company, description).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


def _rows_by_id(payload: dict) -> dict[str, dict]:
    rows = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = [payload] if isinstance(payload, dict) and payload.get("id") else []
    by_id = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            by_id[str(row["id"])] = row
    return by_id


def analyze_tailored_resumes_fit_batch(role_label: str, items: list[dict]) -> list[dict | None]:
    """Score many tailored-resume/JD pairs in one LLM request."""
    if not items:
        return []
    packed = []
    for index, item in enumerate(items):
        tailored = item.get("resume") or {}
        packed.append(
            {
                "id": f"j{index + 1}",
                "title": item.get("title", ""),
                "company": item.get("company", ""),
                "resume": {
                    "summary": tailored.get("summary"),
                    "skills": (tailored.get("skills") or [])[:16],
                    "experience": (tailored.get("experience") or [])[:5],
                },
                "description": (item.get("description") or "").strip()[:1800]
                or "(No job description was available. Judge from the title and company only.)",
            }
        )
    payload = complete_json(
        (
            "You are a hiring-side career coach. Score each job against its tailored resume. "
            "Do not assume facts that are not in that resume. Return JSON only."
        ),
        (
            f"Search role being hunted: {role_label}.\n"
            f"JOBS JSON ({len(packed)} listings):\n{json.dumps(packed, ensure_ascii=False)}\n\n"
            "Return JSON: {\"jobs\": [ ... one object per job ... ]}.\n"
            "Each object must include:\n"
            "id (the job id you were given),\n"
            "rating (integer 0-100: 80+ strong apply, 65-79 apply, 50-64 maybe, below 50 skip),\n"
            "decision (apply|maybe|skip),\n"
            "why (one or two sentences on the strongest fit),\n"
            "reasoning (3-5 sentences of actual comparison against this tailored resume),\n"
            "matched (array of 3-8 strengths the tailored resume now shows for this JD),\n"
            "missed (array of JD requirements still weakly evidenced),\n"
            "note (one practical caveat or empty string)."
        ),
        timeout=180,
    )
    by_id = _rows_by_id(payload)
    results: list[dict | None] = []
    for index, item in enumerate(items):
        row = by_id.get(f"j{index + 1}")
        if not row:
            results.append(None)
            continue
        fit = _fit_result_from_payload(row, item.get("description", ""))
        fit["reassessed"] = True
        results.append(fit)
    return results


def analyze_tailored_resume_fit(
    role_label: str,
    tailored_resume: dict,
    title: str,
    company: str,
    description: str,
) -> dict:
    results = analyze_tailored_resumes_fit_batch(
        role_label,
        [
            {
                "resume": tailored_resume,
                "title": title,
                "company": company,
                "description": description,
            }
        ],
    )
    if results and results[0]:
        return results[0]
    raise RuntimeError("LLM returned no re-score for this job")


def analyze_job_fit(
    role: str,
    role_label: str,
    context: dict,
    title: str,
    company: str,
    description: str,
) -> dict:
    cached = _read_cached_fit(role, title, company, description)
    if cached:
        return cached
    results = analyze_jobs_fit_batch(
        role,
        role_label,
        context,
        [{"title": title, "company": company, "description": description}],
    )
    if results and results[0]:
        return results[0]
    raise RuntimeError("LLM returned no score for this job")


def analyze_jobs_fit_batch(
    role: str,
    role_label: str,
    context: dict,
    jobs: list[dict],
) -> list[dict | None]:
    """Score many jobs in one LLM request. Cached jobs are not sent again."""
    results: list[dict | None] = [None] * len(jobs)
    pending: list[tuple[int, str, dict]] = []
    for index, job in enumerate(jobs):
        title = job.get("title", "")
        company = job.get("company", "")
        description = job.get("description", "")
        cached = _read_cached_fit(role, title, company, description)
        if cached:
            results[index] = cached
            continue
        job_id = f"j{index + 1}"
        pending.append((index, job_id, job))
    if not pending:
        return results

    packed = []
    for _index, job_id, job in pending:
        description = (job.get("description") or "").strip()
        packed.append(
            {
                "id": job_id,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "description": description[:2200]
                or "(No job description was available. Judge from the title and company only.)",
            }
        )
    payload = complete_json(
        (
            "You are a hiring-side career coach. Compare one candidate profile to many jobs. "
            "Score each job independently. Reason about seniority, scope, domain, leadership, "
            "and must-have skills. Do not reward keyword stuffing. Return JSON only."
        ),
        (
            f"Search role being hunted: {role_label}.\n"
            f"CANDIDATE PROFILE JSON:\n{json.dumps(_context_blob(context), ensure_ascii=False)}\n\n"
            f"JOBS JSON ({len(packed)} listings):\n{json.dumps(packed, ensure_ascii=False)}\n\n"
            "Return JSON: {\"jobs\": [ ... one object per job ... ]}.\n"
            "Each object must include:\n"
            "id (the job id you were given),\n"
            "rating (integer 0-100: 80+ strong apply, 65-79 apply, 50-64 maybe, below 50 skip),\n"
            "decision (apply|maybe|skip),\n"
            "why (one or two sentences on the strongest fit),\n"
            "reasoning (3-5 sentences of actual comparison, not a skill list),\n"
            "matched (array of 3-8 profile strengths that the JD needs),\n"
            "missed (array of important JD requirements weakly evidenced on the resume),\n"
            "note (one practical caveat or empty string)."
        ),
        timeout=180,
    )
    by_id = _rows_by_id(payload)

    for index, job_id, job in pending:
        row = by_id.get(job_id)
        if not row:
            continue
        result = _fit_result_from_payload(row, job.get("description", ""))
        _write_cached_fit(role, job.get("title", ""), job.get("company", ""), job.get("description", ""), result)
        results[index] = result
    return results
