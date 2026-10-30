"""Tailor the uploaded resume to one job listing and write a PDF into Results/."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fpdf import FPDF

from llm import (
    _fit_result_from_payload,
    analyze_tailored_resumes_fit_batch,
    complete_json,
    load_context,
    llm_available,
)
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


def _save_tailored_resume(company: str, title: str, data: dict) -> dict:
    pdf_path = results_pdf_path(company, title)
    write_resume_pdf(pdf_path, data)
    pdf_path.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "filename": pdf_path.name,
        "path": str(pdf_path),
        "folder": str(RESULTS_DIR),
    }


def _fit_ui_row(fit: dict, pdf_name: str) -> dict:
    note = " | ".join(
        part
        for part in [
            (fit.get("note") or "").strip(),
            f"Re-analyzed with tailored resume ({pdf_name})",
        ]
        if part
    )
    return {
        "rating": fit.get("score"),
        "why": fit.get("reasoning") or fit.get("why") or "",
        "weak_vs_jd": ", ".join((fit.get("missed") or [])[:6]),
        "note": note,
        "reassessed": True,
    }


def _chunks(items: list, size: int = 5):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _tailor_prompt(role: str, resume_text: str, context: dict, packed: list[dict], score: bool) -> dict:
    extra = (
        " Also score each tailored resume against its JD in the same object "
        "(rating, decision, why, reasoning, matched, missed, note)."
        if score
        else ""
    )
    return complete_json(
        (
            "You rewrite the same source resume for several job applications. "
            "Stay faithful to the source. Do not invent employers, titles, degrees, dates, or skills. "
            "You may reorder, tighten, and emphasize relevant work. Return JSON only. "
            "Use double-quoted JSON keys only."
        ),
        (
            f"Target role family: {ROLE_LABELS[role]}.\n"
            f"CANDIDATE PROFILE JSON:\n{json.dumps(context, ensure_ascii=False)[:8000]}\n\n"
            f"ORIGINAL RESUME TEXT:\n{resume_text}\n\n"
            f"JOBS JSON ({len(packed)} listings):\n{json.dumps(packed, ensure_ascii=False)}\n\n"
            "Return JSON: {\"jobs\": [ ... one object per job ... ]}.\n"
            "Each object must include id and resume, where resume has:\n"
            "name, contact (one line of email/phone/location),\n"
            "summary (4-6 sentences tailored to that job),\n"
            "skills (array of short skill phrases, 10-18 items, job-relevant first),\n"
            "experience (array of {title, company, dates, bullets: 3-6 strings}),\n"
            "education (array of strings or {school, degree, dates}),\n"
            "additional (optional array of short extras).\n"
            f"{extra}"
        ),
        timeout=240,
    )


def _pack_tailor_jobs(indexed_jobs: list[tuple[int, dict]]) -> list[dict]:
    packed = []
    for global_index, job in indexed_jobs:
        packed.append(
            {
                "id": f"j{global_index + 1}",
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "description": (job.get("description") or "").strip()[:1800]
                or "(No job description. Tailor using the title and company only.)",
            }
        )
    return packed


def _save_from_payload_row(job: dict, row: dict | None, score: bool) -> dict:
    resume = (row or {}).get("resume") if isinstance(row, dict) else None
    if not isinstance(resume, dict) or not resume.get("name"):
        return {"ok": False, "error": "LLM returned no resume."}
    try:
        saved = _save_tailored_resume(job.get("company", ""), job.get("title", ""), resume)
    except Exception as exc:
        return {"ok": False, "error": f"Could not write PDF: {exc}"}
    if score:
        fit = _fit_result_from_payload(row, job.get("description", ""))
        fit["reassessed"] = True
        saved["row"] = _fit_ui_row(fit, saved["filename"])
    return saved


def tailor_jobs_batch(role: str, jobs: list[dict], score: bool = False) -> list[dict]:
    """Rewrite many resumes, falling back to smaller chunks if one LLM call fails."""
    if role not in ROLE_LABELS:
        return [{"ok": False, "error": "role must be em or tpm"} for _ in jobs]
    resume_path = ASSETS_DIR / role / "resume.txt"
    if not resume_path.exists():
        return [{"ok": False, "error": "Upload a resume for this role first."} for _ in jobs]
    if not llm_available():
        return [{"ok": False, "error": "Add GROQ_API_KEY or GEMINI_API_KEY to .env"} for _ in jobs]
    if not jobs:
        return []

    resume_text = resume_path.read_text(encoding="utf-8")[:10000]
    context = load_context(role) or {}
    results: list[dict] = [{"ok": False, "error": "LLM returned no resume."} for _ in jobs]

    def run_chunk(indexed: list[tuple[int, dict]]) -> None:
        if not indexed:
            return
        packed = _pack_tailor_jobs(indexed)
        try:
            payload = _tailor_prompt(role, resume_text, context, packed, score)
            rows = payload.get("jobs") if isinstance(payload, dict) else None
            by_id = {}
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("id"):
                        by_id[str(row["id"])] = row
            missing: list[tuple[int, dict]] = []
            for global_index, job in indexed:
                row = by_id.get(f"j{global_index + 1}")
                saved = _save_from_payload_row(job, row, score)
                if saved.get("ok"):
                    results[global_index] = saved
                else:
                    missing.append((global_index, job))
            if missing and len(indexed) > 1:
                for item in missing:
                    run_chunk([item])
            elif missing and len(indexed) == 1:
                results[missing[0][0]] = {
                    "ok": False,
                    "error": saved.get("error") or "LLM returned no resume.",
                }
        except Exception as exc:
            if len(indexed) > 1:
                mid = max(1, len(indexed) // 2)
                run_chunk(indexed[:mid])
                run_chunk(indexed[mid:])
            else:
                global_index, job = indexed[0]
                results[global_index] = {
                    "ok": False,
                    "error": f"{job.get('title')}: {exc}",
                }

    for chunk in _chunks(list(enumerate(jobs)), 5):
        run_chunk(chunk)
    return results


def tailor_resume_pdf(role: str, title: str, company: str, description: str) -> dict:
    results = tailor_jobs_batch(
        role,
        [{"title": title, "company": company, "description": description}],
        score=False,
    )
    return results[0] if results else {"ok": False, "error": "Could not tailor resume."}


def load_tailored_resume(company: str, title: str) -> dict | None:
    path = results_pdf_path(company, title).with_suffix(".json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reassess_jobs_batch(role: str, jobs: list[dict]) -> list[dict]:
    if role not in ROLE_LABELS:
        return [{"ok": False, "error": "role must be em or tpm"} for _ in jobs]
    if not llm_available():
        return [{"ok": False, "error": "Add GROQ_API_KEY or GEMINI_API_KEY to .env"} for _ in jobs]
    prepared = []
    results: list[dict] = []
    for job in jobs:
        tailored = load_tailored_resume(job.get("company", ""), job.get("title", ""))
        if not tailored:
            results.append({"ok": False, "error": "Tailor the resume for this job first."})
            continue
        results.append(None)
        prepared.append(
            {
                "index": len(results) - 1,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "description": job.get("description", ""),
                "resume": tailored,
            }
        )
    def run_reassess(chunk: list[dict]) -> None:
        if not chunk:
            return
        try:
            fits = analyze_tailored_resumes_fit_batch(ROLE_LABELS[role], chunk)
            missing = []
            for item, fit in zip(chunk, fits):
                if not fit:
                    missing.append(item)
                    continue
                pdf_name = results_pdf_path(item["company"], item["title"]).name
                results[item["index"]] = {
                    "ok": True,
                    "filename": pdf_name,
                    "row": _fit_ui_row(fit, pdf_name),
                }
            if missing and len(chunk) > 1:
                for item in missing:
                    run_reassess([item])
            elif missing and len(chunk) == 1:
                results[missing[0]["index"]] = {"ok": False, "error": "LLM returned no re-score."}
        except Exception as exc:
            if len(chunk) > 1:
                mid = max(1, len(chunk) // 2)
                run_reassess(chunk[:mid])
                run_reassess(chunk[mid:])
            else:
                results[chunk[0]["index"]] = {"ok": False, "error": str(exc)}

    for chunk in _chunks(prepared, 5):
        run_reassess(chunk)
    return [
        item if item is not None else {"ok": False, "error": "LLM returned no re-score."}
        for item in results
    ]


def reassess_with_tailored_resume(role: str, title: str, company: str, description: str) -> dict:
    results = reassess_jobs_batch(
        role,
        [{"title": title, "company": company, "description": description}],
    )
    return results[0] if results else {"ok": False, "error": "Could not re-assess job."}
