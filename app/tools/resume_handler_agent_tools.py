import asyncio
import base64
import csv
import os
from collections import Counter

import fitz  # PyMuPDF
from fpdf import FPDF
from langchain.messages import HumanMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from config import OUTPUT_DIRECTORY, get_llm

# fpdf2's core fonts only support latin-1 — LLM output commonly includes smart quotes, en/em
# dashes, ellipses, and bullets that aren't in that range, so they're transliterated to ASCII
# before rendering rather than crashing or silently mangling the PDF.
_PDF_CHAR_REPLACEMENTS = {
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "--",
    "…": "...", "•": "-",
}

_DEFAULT_STYLE = {
    "family": "Helvetica",
    "name_size": 18,
    "header_size": 13,
    "body_size": 11,
    "accent_color": (0, 0, 0),
    "name_centered": False,
}

_BOLD_FLAG = 1 << 4
_SERIF_FLAG = 1 << 2


def _extract_style_profile(pdf_base64: str) -> dict:
    """Inspect the original resume's own PDF (font sizes, bold headers, serif/sans family, an
    accent color, and whether the name banner is centered) so the tailored resume can be
    rendered to match its look, instead of a fixed generic style."""
    try:
        doc = fitz.open(stream=base64.b64decode(pdf_base64), filetype="pdf")
    except Exception:
        return _DEFAULT_STYLE
    if doc.page_count == 0:
        doc.close()
        return _DEFAULT_STYLE

    page = doc[0]
    page_width = page.rect.width
    sizes, bold_sizes, colors = [], [], []
    serif_hits = 0
    first_line_y = None
    top_line_spans = []

    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s["text"].strip()]
            if not spans:
                continue
            line_y = round(spans[0]["bbox"][1])
            if first_line_y is None:
                first_line_y = line_y
            for span in spans:
                size = round(span["size"])
                sizes.append(size)
                if span["flags"] & _BOLD_FLAG:
                    bold_sizes.append(size)
                if span["flags"] & _SERIF_FLAG:
                    serif_hits += 1
                if span["color"] != 0:
                    colors.append(span["color"])
                if line_y == first_line_y:
                    top_line_spans.append(span)
    doc.close()

    if not sizes:
        return _DEFAULT_STYLE

    body_size = Counter(sizes).most_common(1)[0][0]
    name_size = max(sizes)
    distinct_bold_sizes = sorted({s for s in bold_sizes if s < name_size}, reverse=True)
    header_size = distinct_bold_sizes[0] if distinct_bold_sizes else min(body_size + 2, name_size)
    family = "Times" if serif_hits > len(sizes) / 2 else "Helvetica"

    if colors:
        accent_int = Counter(colors).most_common(1)[0][0]
        accent_color = ((accent_int >> 16) & 255, (accent_int >> 8) & 255, accent_int & 255)
    else:
        accent_color = (0, 0, 0)

    name_centered = False
    if top_line_spans:
        left = min(s["bbox"][0] for s in top_line_spans)
        right = max(s["bbox"][2] for s in top_line_spans)
        left_gap, right_gap = left, page_width - right
        if left_gap > page_width * 0.08 and abs(left_gap - right_gap) < page_width * 0.08:
            name_centered = True

    return {
        "family": family,
        "name_size": name_size,
        "header_size": header_size,
        "body_size": body_size,
        "accent_color": accent_color,
        "name_centered": name_centered,
    }


def _classify_resume_line(stripped: str, is_first_nonempty: bool) -> str:
    if is_first_nonempty:
        return "name"
    if stripped.startswith(("-", "*")):
        return "bullet"
    if stripped == stripped.upper() and any(c.isalpha() for c in stripped) and len(stripped) <= 60:
        return "header"
    return "body"


def _write_resume_pdf(text: str, filepath: str, style: dict) -> None:
    for char, replacement in _PDF_CHAR_REPLACEMENTS.items():
        text = text.replace(char, replacement)
    text = text.encode("latin-1", errors="replace").decode("latin-1")

    family = style["family"]
    accent = style["accent_color"]
    black = (0, 0, 0)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    seen_first_line = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            pdf.ln(round(style["body_size"] * 0.6, 1))
            continue

        kind = _classify_resume_line(stripped, is_first_nonempty=not seen_first_line)
        seen_first_line = True

        if kind == "name":
            size, color, align = style["name_size"], accent, ("C" if style["name_centered"] else "L")
            pdf.set_font(family, style="B", size=size)
        elif kind == "header":
            size, color, align = style["header_size"], accent, "L"
            pdf.set_font(family, style="B", size=size)
        else:
            size, color, align = style["body_size"], black, "L"
            pdf.set_font(family, size=size)

        pdf.set_text_color(*color)
        if kind == "bullet":
            pdf.set_x(pdf.l_margin + 5)
        # multi_cell defaults to leaving the cursor at the right margin (new_x=XPos.RIGHT),
        # which starves every subsequent call of horizontal space — reset it back to the
        # left margin after each line instead.
        pdf.multi_cell(0, round(size * 0.6, 1), stripped, align=align, new_x="LMARGIN", new_y="NEXT")
    pdf.output(filepath)


# NOTE (adapted per confirmed decision): `apply` defaults to False, not True — resume_corrections_and_download
# is gated by HumanInTheLoopMiddleware (see resume_handler_agent in agent.py), pausing before it runs at all.
# While paused, the Streamlit UI lets the user flip each row's `apply` True/False directly in the csv (no
# agent involvement); a single "Continue" action then always resumes with {"type": "approve"} regardless of
# what was picked — the interrupt exists so the human can review suggestions before generation, not to gate
# individual rows through the HITL decision itself. resume_corrections_and_download then processes whichever
# rows are apply=True at that point, exactly as originally written.


@tool
async def analyze_resume_and_make_suggestions(runtime: ToolRuntime) -> str:
    """Fetch job description and add changes to resume(passed as pdf) by tailoring it to each job description and save it to csv for human approval"""
    csv_path = runtime.state.get("pathToJobsCsv")
    pdf_base64 = runtime.state.get("pdfBase64")
    if not csv_path or not pdf_base64:
        return "No CSV file path key found"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames + ["resume_corrections"] + ["apply_resume_corrections"]

    llm = get_llm()

    async def get_suggestions(row):
        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        "You are a resume coach. Compare the attached resume to the job descriptions "
                        "and suggest specific, concise corrections or additions to better match the job. "
                        "Match their language, mirror their priorities, and keep it to one page. "
                        "Don’t make anything up — only use what’s already in my resume, just position it better."
                        "Return only a short bulleted list of suggestions.\n\n"
                        f"Job Description:\n{row['description']}\n"
                    ),
                },
                {
                    "type": "file",
                    "mime_type": "application/pdf",
                    "base64": pdf_base64,
                },
            ]
        )
        response = await llm.ainvoke([message])
        row["resume_corrections"] = response.content.replace("\n", " | ")
        row["apply_resume_corrections"] = False

    await asyncio.gather(*(get_suggestions(row) for row in rows))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return ToolMessage(
        f"Tailored resume suggestions for all jobs and saved to csv {csv_path}. "
        f"Next step: wait for the user to approve individual jobs before generating tailored files.",
        tool_call_id=runtime.tool_call_id,
    )


async def generate_tailored_resume_for_row(pdf_base64: str, row: dict) -> str:
    """Generate one tailored resume file for a single job row (never overwrites the original resume).

    Extracted from resume_corrections_and_download so it can be called for a single approved job
    (from the Streamlit Approve button) as well as in a full-csv batch (from the tool below).
    """
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "Rewrite the attached resume, applying the suggested corrections below. "
                    "Do not invent new experience or skills — only reorganize, rephrase, and "
                    "emphasize what is already in the original resume, per the suggestions. "
                    "Output only the complete rewritten resume text, ready to save as the final "
                    "document. Do not include any commentary, notes, explanations, or disclaimers "
                    "of any kind, before the resume, after it, or appended as a closing section — "
                    "the output must contain nothing but the resume itself.\n\n"
                    f"Suggested changes:\n{row['resume_corrections']}\n"
                ),
            },
            {
                "type": "file",
                "mime_type": "application/pdf",
                "base64": pdf_base64,
            },
        ]
    )
    llm = get_llm()
    response = await llm.ainvoke([message])

    filename = f"{row['company']}_{row['title']}_resume.pdf".replace(" ", "_").replace("/", "-")
    filepath = os.path.join(OUTPUT_DIRECTORY, filename)
    style = _extract_style_profile(pdf_base64)
    _write_resume_pdf(response.content, filepath, style)
    return filepath


@tool
async def resume_corrections_and_download(runtime: ToolRuntime) -> str:
    """Apply the suggested resume corrections and save each tailored resume as a new file \
        (never overwriting the original), one per job marked apply_resume_corrections=True in the jobs csv."""
    csv_path = runtime.state.get("pathToJobsCsv")
    pdf_base64 = runtime.state.get("pdfBase64")
    if not csv_path or not pdf_base64:
        return "No CSV file path key found"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames if "tailored_resume_path" in reader.fieldnames else [*reader.fieldnames, "tailored_resume_path"]

    saved_files = []

    async def apply_suggestions(row):
        row.setdefault("tailored_resume_path", "")
        if row.get("apply_resume_corrections", "").strip().lower() != "true":
            return
        filepath = await generate_tailored_resume_for_row(pdf_base64, row)
        row["tailored_resume_path"] = filepath
        saved_files.append(filepath)

    await asyncio.gather(*(apply_suggestions(row) for row in rows))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return Command(update={
        "pathToJobsCsv": csv_path,
        "tailoredResumeFiles": saved_files,
        "messages": [ToolMessage(
            f"Saved {len(saved_files)} tailored resume file(s): {', '.join(saved_files)}. "
            f"Next steps: Start applying to the jobs present in jobs csv file",
            tool_call_id=runtime.tool_call_id,
        )],
    })