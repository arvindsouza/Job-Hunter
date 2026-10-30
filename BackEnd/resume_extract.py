"""Extract skill profiles from uploaded resume PDFs into Assets/."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from llm import analyze_resume, load_context, llm_available

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "Assets"

ROLE_FOLDERS = {"em": "em", "tpm": "tpm"}
ROLE_LABELS = {
    "em": "Engineering Manager",
    "tpm": "Technical Program Manager",
}

SECTION_START = re.compile(
    r"(core competencies|technical skills|skills|competencies)\b",
    re.I,
)
SECTION_END = re.compile(
    r"\n\s*(professional experience|experience|education|work history|employment)\b",
    re.I,
)

# Job-description terms used only to flag extras not found on the uploaded resume.
JD_EXTRA_TERMS = [
    (r"\bjava\b", "Java"),
    (r"\bpython\b", "Python"),
    (r"\bgolang\b|\bgo lang\b", "Go"),
    (r"kubernetes|\bk8s\b", "Kubernetes"),
    (r"\baws\b", "AWS"),
    (r"\bmicroservices\b", "Microservices"),
    (r"\bkafka\b", "Kafka"),
    (r"\bfintech\b", "Fintech"),
    (r"\bsaas\b", "SaaS"),
    (r"\bjira\b", "Jira"),
    (r"\bpmp\b|\bpmi\b", "PMP/PMI"),
    (r"\bsafe agile\b|\bscaled agile\b", "SAFe"),
    (r"\bpmo\b", "PMO"),
    (r"\bsilicon\b|\bsemiconductor\b", "Semiconductor"),
]


def role_dir(role: str) -> Path:
    if role not in ROLE_FOLDERS:
        raise ValueError("Role must be em or tpm")
    path = ASSETS_DIR / ROLE_FOLDERS[role]
    path.mkdir(parents=True, exist_ok=True)
    return path


def pdf_bytes_to_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    text = "\n".join(pages)
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _skills_block(text: str) -> str:
    match = SECTION_START.search(text)
    if not match:
        return ""
    rest = text[match.end() :]
    end = SECTION_END.search(rest)
    if end:
        rest = rest[: end.start()]
    return rest.strip()


def parse_skill_lines(text: str) -> list[str]:
    block = _skills_block(text)
    source = block or text[:2500]
    chunks: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip(" -•●\t")
        if not line or len(line) < 2:
            continue
        if re.match(r"^(summary|objective|experience|education)\b", line, re.I):
            continue
        parts = re.split(r"\s*[,;|•●]\s*", line) if re.search(r"[,;|•●]", line) else [line]
        for part in parts:
            item = re.sub(r"\s+", " ", part).strip(" -•●:+")
            if re.fullmatch(r"\d+(\.\d+)?", item):
                continue
            if 1 < len(item) <= 80 and not item.lower().startswith("http"):
                chunks.append(item)
    skip_headers = {
        "core competencies",
        "technical skills",
        "& technical skills",
        "methodologies & tools",
        "technical domains",
        "skills",
    }
    merged: list[str] = []
    continuations = {
        "framework",
        "devops",
        "leadership",
        "management",
        "cloud",
        "platform",
        "code",
        "grooming",
        "scrum",
        "css",
    }
    for item in chunks:
        if item.strip().lower() in skip_headers:
            continue
        if merged and (
            merged[-1].endswith(("&", "(", "/", ","))
            or item.startswith((")", "&"))
            or item[:1].islower()
            or item.lower() in continuations
        ):
            glue = "" if item.startswith(")") else " "
            merged[-1] = f"{merged[-1]}{glue}{item}"
        else:
            merged.append(item)
    seen: set[str] = set()
    unique: list[str] = []
    for item in merged:
        key = item.lower().strip(" .")
        if key in seen or key in skip_headers:
            continue
        seen.add(key)
        unique.append(item.strip(" ."))
        if len(unique) >= 24:
            break
    return unique


def skill_to_group(skill: str, weight: int) -> dict:
    tokens = re.findall(r"[A-Za-z0-9+#./+-]+", skill)
    patterns: list[str] = []
    phrase = skill.strip()
    if len(phrase) > 2:
        patterns.append(re.escape(phrase.lower()))
    for tok in tokens:
        if len(tok) < 2:
            continue
        escaped = re.escape(tok.lower())
        if len(tok) <= 3:
            patterns.append(rf"\b{escaped}\b")
        else:
            patterns.append(escaped)
    if not patterns:
        patterns = [re.escape(skill.lower())]
    # de-dupe while preserving order
    ordered = list(dict.fromkeys(patterns))
    return {"name": skill[:80], "weight": weight, "patterns": ordered}


def build_skill_groups(skills: list[str]) -> list[dict]:
    if not skills:
        return []
    base = max(3, round(80 / len(skills)))
    return [skill_to_group(skill, base) for skill in skills]


def gap_signals_for_resume(resume_text: str) -> list[list[str]]:
    lowered = resume_text.lower()
    extras = []
    for pattern, label in JD_EXTRA_TERMS:
        if not re.search(pattern, lowered, flags=re.I):
            extras.append([pattern, label])
    return extras


def save_resume(role: str, data: bytes, filename: str) -> dict:
    folder = role_dir(role)
    safe_name = Path(filename).name or "resume.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    pdf_path = folder / "resume.pdf"
    pdf_path.write_bytes(data)
    text = pdf_bytes_to_text(data)
    (folder / "resume.txt").write_text(text, encoding="utf-8")
    skills = parse_skill_lines(text)
    llm_error = ""
    if llm_available():
        try:
            context = analyze_resume(role, text, ROLE_LABELS.get(role, role))
            llm_skills = [str(item) for item in (context.get("tech_stack") or []) if str(item).strip()]
            if llm_skills:
                skills = llm_skills
        except Exception as exc:
            llm_error = str(exc)
    profile = {
        "role": role,
        "source_filename": safe_name,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "skills": skills,
        "skill_groups": build_skill_groups(skills),
        "gap_signals": gap_signals_for_resume(text),
        "llm_error": llm_error,
    }
    (folder / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return profile_summary(role)


def profile_summary(role: str) -> dict:
    folder = ASSETS_DIR / role
    profile_path = folder / "profile.json"
    if not profile_path.exists():
        return {"role": role, "uploaded": False}
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    context = load_context(role) or {}
    skills = context.get("tech_stack") or profile.get("skills", [])
    return {
        "role": role,
        "uploaded": True,
        "filename": profile.get("source_filename", "resume.pdf"),
        "extracted_at": profile.get("extracted_at"),
        "skills": skills,
        "skill_count": len(skills),
        "summary": context.get("summary") or "",
        "current_level": context.get("current_level") or "",
        "llm": context.get("method") == "llm",
        "llm_error": context.get("error") or profile.get("llm_error") or "",
    }


def list_resume_summaries() -> dict:
    return {"em": profile_summary("em"), "tpm": profile_summary("tpm")}


def load_profile(role: str) -> dict | None:
    path = ASSETS_DIR / role / "profile.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
