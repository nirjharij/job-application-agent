import base64
import os
from types import SimpleNamespace

import pytest

from tools import resume_handler_agent_tools as rh
from tools.resume_handler_agent_tools import (
    _BOLD_FLAG,
    _DEFAULT_STYLE,
    _SERIF_FLAG,
    _classify_resume_line,
    _extract_style_profile,
    _write_resume_pdf,
    analyze_resume_and_make_suggestions,
    generate_tailored_resume_for_row,
    resume_corrections_and_download,
)

from conftest import CSV_FIELDNAMES, FakeRuntime, job_row, read_csv, write_csv

PDF_B64 = base64.b64encode(b"%PDF-1.4 not really a pdf").decode()


# --- _classify_resume_line ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,is_first,expected",
    [
        ("Jane Doe", True, "name"),
        ("- ITEM", True, "name"),  # the first-line rule wins over every other branch
        ("- built a thing", False, "bullet"),
        ("* built a thing", False, "bullet"),
        ("EXPERIENCE", False, "header"),
        ("WORK EXPERIENCE", False, "header"),
        ("A" * 60, False, "header"),
        ("A" * 61, False, "body"),  # the <= 60 length guard
        ("2019 - 2021", False, "body"),  # uppercase-equal, but no letters
        ("12345", False, "body"),  # uppercase-equal but no letters
        ("Senior Engineer at Acme", False, "body"),
    ],
)
def test_classify_resume_line(line, is_first, expected):
    assert _classify_resume_line(line, is_first_nonempty=is_first) == expected


def test_a_date_range_is_body_not_header():
    # "2019 - 2021" equals its own .upper(), but any(c.isalpha()) is False, so it falls through
    # to body rather than being rendered as a bold section header.
    assert _classify_resume_line("2019 - 2021", is_first_nonempty=False) == "body"
    assert _classify_resume_line("2019 TO 2021", is_first_nonempty=False) == "header"


# --- _extract_style_profile -----------------------------------------------------------------


def _span(text="x", size=11, flags=0, color=0, bbox=(50, 100, 150, 112)):
    return {"text": text, "size": size, "flags": flags, "color": color, "bbox": bbox}


class _FakeDoc:
    def __init__(self, page, page_count):
        self.page_count = page_count
        self._page = page
        self.closed = False

    def __getitem__(self, index):
        return self._page

    def close(self):
        self.closed = True


def _fake_doc(monkeypatch, spans_per_line, page_width=595.0, page_count=1):
    blocks = [{"lines": [{"spans": spans} for spans in spans_per_line]}]
    page = SimpleNamespace(
        rect=SimpleNamespace(width=page_width), get_text=lambda _: {"blocks": blocks}
    )
    doc = _FakeDoc(page, page_count)
    monkeypatch.setattr(rh.fitz, "open", lambda **kwargs: doc)
    return doc


def test_undecodable_pdf_falls_back_to_the_default_style():
    assert _extract_style_profile("!!!not base64!!!") is _DEFAULT_STYLE


def test_empty_document_falls_back_to_the_default_style(monkeypatch):
    _fake_doc(monkeypatch, [], page_count=0)
    assert _extract_style_profile(PDF_B64) is _DEFAULT_STYLE


def test_document_without_text_falls_back_to_the_default_style(monkeypatch):
    _fake_doc(monkeypatch, [[_span(text="   ")]])
    assert _extract_style_profile(PDF_B64) is _DEFAULT_STYLE


def test_fallbacks_return_the_shared_default_dict(monkeypatch):
    # Not a copy — a caller mutating a returned style would corrupt the module default for
    # every later resume. Pinned so the aliasing is a deliberate choice, not an accident.
    _fake_doc(monkeypatch, [], page_count=0)
    assert _extract_style_profile(PDF_B64) is _extract_style_profile("!!!bad!!!") is _DEFAULT_STYLE


def test_body_size_is_the_most_common_span_size(monkeypatch):
    _fake_doc(
        monkeypatch,
        [
            [_span("Jane Doe", size=20)],
            [_span("a", size=11), _span("b", size=11), _span("c", size=11)],
            [_span("d", size=9)],
        ],
    )
    style = _extract_style_profile(PDF_B64)
    assert style["body_size"] == 11
    assert style["name_size"] == 20


def test_header_size_is_the_largest_bold_size_below_the_name(monkeypatch):
    _fake_doc(
        monkeypatch,
        [
            [_span("Jane Doe", size=20, flags=_BOLD_FLAG)],
            [_span("EXPERIENCE", size=14, flags=_BOLD_FLAG)],
            [_span("SKILLS", size=13, flags=_BOLD_FLAG)],
            [_span("body", size=11)],
        ],
    )
    assert _extract_style_profile(PDF_B64)["header_size"] == 14


def test_header_size_falls_back_when_nothing_is_bold(monkeypatch):
    _fake_doc(monkeypatch, [[_span("Jane", size=20)], [_span("a", size=11), _span("b", size=11)]])
    # min(body_size + 2, name_size)
    assert _extract_style_profile(PDF_B64)["header_size"] == 13


@pytest.mark.parametrize(
    "serif_spans,expected", [(3, "Times"), (1, "Helvetica")]
)
def test_family_follows_the_serif_majority(monkeypatch, serif_spans, expected):
    spans = [_span(f"s{i}", flags=_SERIF_FLAG) for i in range(serif_spans)]
    spans += [_span(f"p{i}") for i in range(4 - serif_spans)]
    _fake_doc(monkeypatch, [spans])
    assert _extract_style_profile(PDF_B64)["family"] == expected


def test_accent_color_is_decoded_from_the_packed_int(monkeypatch):
    _fake_doc(monkeypatch, [[_span("Jane", size=20, color=0x1A73E8)], [_span("body", size=11)]])
    assert _extract_style_profile(PDF_B64)["accent_color"] == (0x1A, 0x73, 0xE8)


def test_accent_color_defaults_to_black_when_all_text_is_black(monkeypatch):
    _fake_doc(monkeypatch, [[_span("Jane", size=20)], [_span("body", size=11)]])
    assert _extract_style_profile(PDF_B64)["accent_color"] == (0, 0, 0)


def test_name_centered_when_the_top_line_is_symmetrically_inset(monkeypatch):
    _fake_doc(monkeypatch, [[_span("Jane Doe", size=20, bbox=(220, 60, 375, 80))]], page_width=595.0)
    assert _extract_style_profile(PDF_B64)["name_centered"] is True


def test_name_not_centered_when_the_top_line_is_left_aligned(monkeypatch):
    _fake_doc(monkeypatch, [[_span("Jane Doe", size=20, bbox=(50, 60, 200, 80))]], page_width=595.0)
    assert _extract_style_profile(PDF_B64)["name_centered"] is False


# --- _write_resume_pdf ----------------------------------------------------------------------


def test_write_resume_pdf_produces_a_real_pdf(tmp_path):
    out = tmp_path / "resume.pdf"
    _write_resume_pdf("Jane Doe\n\nEXPERIENCE\n- Built a thing\nAt Acme.", str(out), _DEFAULT_STYLE)
    assert out.read_bytes().startswith(b"%PDF")


def test_write_resume_pdf_transliterates_non_latin1_characters(tmp_path):
    # fpdf2's core fonts are latin-1 only; smart quotes and bullets from LLM output would
    # otherwise blow up here rather than at review time.
    out = tmp_path / "resume.pdf"
    _write_resume_pdf("Jane\n\n• “led” – a team… €100k", str(out), _DEFAULT_STYLE)
    assert out.read_bytes().startswith(b"%PDF")


def test_write_resume_pdf_honors_a_custom_style(tmp_path):
    out = tmp_path / "resume.pdf"
    style = {**_DEFAULT_STYLE, "family": "Times", "accent_color": (26, 115, 232), "name_centered": True}
    _write_resume_pdf("Jane Doe\nEXPERIENCE\nbody", str(out), style)
    assert out.read_bytes().startswith(b"%PDF")


# --- generate_tailored_resume_for_row -------------------------------------------------------


@pytest.fixture
def stub_llm(monkeypatch, fake_llm):
    llm = fake_llm("Jane Doe\n\nEXPERIENCE\n- Tailored bullet")
    monkeypatch.setattr(rh, "init_chat_model", lambda model: llm)
    return llm


async def test_generate_tailored_resume_sanitizes_the_filename(tmp_path, monkeypatch, stub_llm):
    monkeypatch.chdir(tmp_path)
    row = job_row(company="Acme GmbH", title="A/B Test Engineer", resume_corrections="- do x")

    filepath = await generate_tailored_resume_for_row(PDF_B64, row)

    assert os.path.basename(filepath) == "Acme_GmbH_A-B_Test_Engineer_tailored_resume.pdf"
    assert os.path.dirname(filepath) == str(tmp_path)
    assert os.path.exists(filepath)


async def test_generate_tailored_resume_sends_the_corrections_and_the_pdf(tmp_path, monkeypatch, stub_llm):
    monkeypatch.chdir(tmp_path)
    await generate_tailored_resume_for_row(PDF_B64, job_row(resume_corrections="- mirror their SQL wording"))

    (messages,) = stub_llm.calls
    text_block, file_block = messages[0].content
    assert "- mirror their SQL wording" in text_block["text"]
    assert file_block == {"type": "file", "mime_type": "application/pdf", "base64": PDF_B64}


# --- analyze_resume_and_make_suggestions ----------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [{}, {"pathToJobsCsv": "/tmp/x.csv"}, {"pdfBase64": PDF_B64}],
)
async def test_analyze_returns_a_guard_string_without_csv_or_pdf(state):
    assert await analyze_resume_and_make_suggestions.coroutine(runtime=FakeRuntime(state=state)) == (
        "No CSV file path key found"
    )


async def test_analyze_adds_suggestion_columns_to_every_row(jobs_csv, monkeypatch, fake_llm):
    llm = fake_llm("- lead with Python\n- mirror their wording")
    monkeypatch.setattr(rh, "init_chat_model", lambda model: llm)

    await analyze_resume_and_make_suggestions.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": jobs_csv, "pdfBase64": PDF_B64})
    )

    rows, fieldnames = read_csv(jobs_csv)
    assert fieldnames == [*CSV_FIELDNAMES, "resume_corrections", "apply_resume_corrections"]
    # Newlines are collapsed so a suggestion list survives a single csv cell.
    assert all(r["resume_corrections"] == "- lead with Python | - mirror their wording" for r in rows)
    assert len(llm.calls) == len(rows)


async def test_analyze_defaults_every_row_to_not_applied(jobs_csv, monkeypatch, fake_llm):
    monkeypatch.setattr(rh, "init_chat_model", lambda model: fake_llm("- do x"))

    await analyze_resume_and_make_suggestions.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": jobs_csv, "pdfBase64": PDF_B64})
    )

    # Written as Python False, read back as the string "False" — this is what the download gate reads.
    assert all(r["apply_resume_corrections"] == "False" for r in read_csv(jobs_csv)[0])


async def test_analyze_sends_each_job_description_to_the_model(jobs_csv, monkeypatch, fake_llm):
    llm = fake_llm("- do x")
    monkeypatch.setattr(rh, "init_chat_model", lambda model: llm)

    await analyze_resume_and_make_suggestions.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": jobs_csv, "pdfBase64": PDF_B64})
    )

    prompts = [messages[0].content[0]["text"] for messages in llm.calls]
    assert all("We need Python." in p for p in prompts)


async def test_analyze_on_an_empty_csv_raises(tmp_path):
    # Documents current behavior (bug): csv.DictReader.fieldnames is None for a header-less file,
    # so `reader.fieldnames + [...]` raises rather than producing an empty result.
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(TypeError):
        await analyze_resume_and_make_suggestions.coroutine(
            runtime=FakeRuntime(state={"pathToJobsCsv": str(empty), "pdfBase64": PDF_B64})
        )


# --- resume_corrections_and_download --------------------------------------------------------


@pytest.fixture
def stub_generate(monkeypatch):
    """Patch the generator rather than the LLM — this tool's logic is the apply gate, not the model."""
    generated = []

    async def fake_generate(pdf_base64, row):
        path = f"/generated/{row['company']}_tailored_resume.pdf"
        generated.append((row["url"], path))
        return path

    monkeypatch.setattr(rh, "generate_tailored_resume_for_row", fake_generate)
    return generated


def _csv_with_flags(tmp_path, *flags):
    rows = [
        job_row(
            url=f"https://x/jobs/view/{i}/",
            company=f"Co{i}",
            resume_corrections="- do x",
            apply_resume_corrections=flag,
        )
        for i, flag in enumerate(flags)
    ]
    fieldnames = [*CSV_FIELDNAMES, "resume_corrections", "apply_resume_corrections"]
    return write_csv(tmp_path / "jobs.csv", rows, fieldnames)


@pytest.mark.parametrize("flag", ["True", "true", "TRUE", " true "])
async def test_truthy_flag_variants_generate_a_resume(tmp_path, stub_generate, flag):
    path = _csv_with_flags(tmp_path, flag)
    await resume_corrections_and_download.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": path, "pdfBase64": PDF_B64})
    )
    assert len(stub_generate) == 1


@pytest.mark.parametrize("flag", ["False", "false", "", "1", "yes"])
async def test_non_true_flags_are_skipped(tmp_path, stub_generate, flag):
    # "False" is what analyze_resume_and_make_suggestions writes, so this is the default path.
    path = _csv_with_flags(tmp_path, flag)
    await resume_corrections_and_download.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": path, "pdfBase64": PDF_B64})
    )
    assert stub_generate == []


async def test_only_approved_rows_get_a_tailored_path(tmp_path, stub_generate):
    path = _csv_with_flags(tmp_path, "True", "False", "True")
    await resume_corrections_and_download.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": path, "pdfBase64": PDF_B64})
    )

    rows, fieldnames = read_csv(path)
    assert "tailored_resume_path" in fieldnames
    assert [r["tailored_resume_path"] for r in rows] == [
        "/generated/Co0_tailored_resume.pdf",
        "",  # skipped rows get an empty string, never a missing key
        "/generated/Co2_tailored_resume.pdf",
    ]


async def test_rerunning_does_not_duplicate_the_tailored_path_column(tmp_path, stub_generate):
    path = _csv_with_flags(tmp_path, "True")
    runtime = FakeRuntime(state={"pathToJobsCsv": path, "pdfBase64": PDF_B64})

    await resume_corrections_and_download.coroutine(runtime=runtime)
    first = read_csv(path)[1]
    await resume_corrections_and_download.coroutine(runtime=runtime)

    assert read_csv(path)[1] == first
    assert first.count("tailored_resume_path") == 1


async def test_returns_a_command_carrying_the_saved_files(tmp_path, stub_generate):
    path = _csv_with_flags(tmp_path, "True", "True")
    result = await resume_corrections_and_download.coroutine(
        runtime=FakeRuntime(state={"pathToJobsCsv": path, "pdfBase64": PDF_B64}, tool_call_id="c1")
    )

    assert result.update["pathToJobsCsv"] == path
    assert sorted(result.update["tailoredResumeFiles"]) == [
        "/generated/Co0_tailored_resume.pdf",
        "/generated/Co1_tailored_resume.pdf",
    ]
    (message,) = result.update["messages"]
    assert message.tool_call_id == "c1"
    assert "Saved 2 tailored resume file(s)" in message.content


@pytest.mark.parametrize("state", [{}, {"pathToJobsCsv": "/tmp/x.csv"}, {"pdfBase64": PDF_B64}])
async def test_download_returns_a_guard_string_without_csv_or_pdf(state):
    assert await resume_corrections_and_download.coroutine(runtime=FakeRuntime(state=state)) == (
        "No CSV file path key found"
    )


async def test_download_on_an_empty_csv_raises(tmp_path):
    # Same latent None-fieldnames bug as analyze, via `"tailored_resume_path" in reader.fieldnames`.
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(TypeError):
        await resume_corrections_and_download.coroutine(
            runtime=FakeRuntime(state={"pathToJobsCsv": str(empty), "pdfBase64": PDF_B64})
        )
