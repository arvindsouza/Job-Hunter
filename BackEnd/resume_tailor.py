"""Tailor the uploaded resume to one job listing and write a PDF into Results/."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fpdf import FPDF

from llm import analyze_tailored_resume_fit, complete_json, load_context, llm_available
from resume_extract import ROLE_LABELS, ASSETS_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "Results"
WINDOWS_FONTS = Path(r"C:\Windows\Fonts")


def _safe_filename_part(text: str, max_len: int = 50) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "", text or "")
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return (cleaned or "job")[:max_len]


def results_pdf_path(company: str, title: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{_safe_filename_part(company)}-{_safe_filename_part(title)}.pdf"
    return RESULTS_DIR / name


def _pick_font() -> tuple[str, str] | None:
    regular = WINDOWS_FONTS / "arial.ttf"
    if regular.exists():
        return str(regular), str(WINDOWS_FONTS / "arialbd.ttf") if (WINDOWS_FONTS / "arialbd.ttf").exists() else str(regular)
    return None


def _plain(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def write_resume_pdf(path: Path, data: dict) -> None:
    pdf = FPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    fonts = _pick_font()
    if fonts:
        pdf.add_font("Resume", "", fonts[0])
        pdf.add_font("Resume", "B", fonts[1])
        family = "Resume"
    else:
        family = "Helvetica"

    def heading(text: str) -> None:
        pdf.set_font(family, "B", 12)
        pdf.set_text_color(20, 40, 70)
        pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(180, 190, 200)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(3)
        pdf.set_text_color(30, 30, 30)

    def body(text: str, size: int = 10, bold: bool = False) -> None:
        if not text:
            return
        pdf.set_font(family, "B" if bold else "", size)
        pdf.multi_cell(0, 5, text)
        pdf.ln(1)

    name = _plain(data.get("name")) or "Resume"
    contact = _plain(data.get("contact"))
    pdf.set_font(family, "B", 18)
    pdf.cell(0, 10, name, new_x="LMARGIN", new_y="NEXT")
    if contact:
        pdf.set_font(family, "", 10)
        pdf.set_text_color(70, 70, 70)
        pdf.multi_cell(0, 5, contact)
        pdf.set_text_color(30, 30, 30)
        pdf.ln(2)

    summary = _plain(data.get("summary"))
    if summary:
        heading("Summary")
        body(summary)

    skills = data.get("skills") or []
    if isinstance(skills, dict):
        skill_lines = [f"{key}: {', '.join(map(str, value))}" for key, value in skills.items() if value]
    else:
        skill_lines = [_plain(item) for item in skills if _plain(item)]
    if skill_lines:
        heading("Skills")
        body(" • ".join(skill_lines) if not isinstance(skills, dict) else "\n".join(skill_lines))

    experience = data.get("experience") or []
    if experience:
        heading("Experience")
        for role in experience:
            title_line = " | ".join(
                part
                for part in [
                    _plain(role.get("title")),
                    _plain(role.get("company")),
                    _plain(role.get("dates")),
                ]
                if part
            )
            body(title_line, bold=True)
            for bullet in role.get("bullets") or []:
                text = _plain(bullet)
                if text:
                    body(f"• {text}")

    education = data.get("education") or []
    if education:
        heading("Education")
        for item in education:
            if isinstance(item, dict):
                body(
                    " | ".join(
                        part
                        for part in [item.get("school"), item.get("degree"), item.get("dates")]
                        if _plain(part)
                    )
                )
            else:
                body(_plain(item))

    extras = data.get("additional") or []
    if extras:
        heading("Additional")
        for item in extras:
            body(f"• {_plain(item)}" if _plain(item) else "")

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def tailor_resume_pdf(role: str, title: str, company: str, description: str) -> dict:
    if role not in ROLE_LABELS:
        return {"ok": False, "error": "role must be em or tpm"}
    resume_path = ASSETS_DIR / role / "resume.txt"
    if not resume_path.exists():
        return {"ok": False, "error": "Upload a resume for this role first."}
    if not llm_available():
        return {"ok": False, "error": "Add GROQ_API_KEY or GEMINI_API_KEY to .env"}
    resume_text = resume_path.read_text(encoding="utf-8")[:14000]
    context = load_context(role) or {}
    jd = (description or "").strip()[:8000] or "(No job description. Tailor using the title and company only.)"
    data = complete_json(
        (
            "You rewrite a resume for one job application. Stay faithful to the source resume. "
            "Do not invent employers, titles, degrees, dates, or skills. You may reorder, "
            "tighten, and emphasize relevant work. Return JSON only."
        ),
        (
            f"Target role family: {ROLE_LABELS[role]}.\n"
            f"JOB TITLE: {title}\nCOMPANY: {company}\n"
            f"JOB DESCRIPTION:\n{jd}\n\n"
            f"CANDIDATE PROFILE JSON:\n{json.dumps(context, ensure_ascii=False)[:8000]}\n\n"
            f"ORIGINAL RESUME TEXT:\n{resume_text}\n\n"
            "Return JSON with:\n"
            "name, contact (one line of email/phone/location),\n"
            "summary (4-6 sentences tailored to this job),\n"
            "skills (array of short skill phrases, 10-18 items, job-relevant first),\n"
            "experience (array of {title, company, dates, bullets: 3-6 strings}),\n"
            "education (array of strings or {school, degree, dates}),\n"
            "additional (optional array of short extras).\n"
            "Keep the resume to about one to two pages of content."
        ),
        timeout=180,
    )
    if not isinstance(data, dict) or not data.get("name"):
        return {"ok": False, "error": "LLM did not return a usable resume."}
    pdf_path = results_pdf_path(company, title)
    write_resume_pdf(pdf_path, data)
    pdf_path.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "filename": pdf_path.name,
        "path": str(pdf_path),
        "folder": str(RESULTS_DIR),
    }


def load_tailored_resume(company: str, title: str) -> dict | None:
    path = results_pdf_path(company, title).with_suffix(".json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reassess_with_tailored_resume(role: str, title: str, company: str, description: str) -> dict:
    if role not in ROLE_LABELS:
        return {"ok": False, "error": "role must be em or tpm"}
    if not llm_available():
        return {"ok": False, "error": "Add GROQ_API_KEY or GEMINI_API_KEY to .env"}
    tailored = load_tailored_resume(company, title)
    if not tailored:
        return {"ok": False, "error": "Tailor the resume for this job first."}
    fit = analyze_tailored_resume_fit(
        ROLE_LABELS[role],
        tailored,
        title,
        company,
        description,
    )
    pdf_name = results_pdf_path(company, title).name
    note = " | ".join(
        part
        for part in [
            (fit.get("note") or "").strip(),
            f"Re-analyzed with tailored resume ({pdf_name})",
        ]
        if part
    )
    return {
        "ok": True,
        "filename": pdf_name,
        "row": {
            "rating": fit.get("score"),
            "why": fit.get("reasoning") or fit.get("why") or "",
            "weak_vs_jd": ", ".join((fit.get("missed") or [])[:6]),
            "note": note,
            "reassessed": True,
        },
    }
