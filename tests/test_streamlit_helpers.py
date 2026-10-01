"""Tests for the two pure helpers inside streamlit_app.py.

The module is a Streamlit script — importing it runs st.set_page_config() and builds the real
agents (Postgres + LLM) at module scope — so the `streamlit_app` fixture stubs `streamlit` and
`agent` in sys.modules first. See tests/conftest.py.
"""

from types import SimpleNamespace

import pytest

from conftest import CSV_FIELDNAMES, job_row, read_csv, write_csv


def _interrupt(name):
    """The HITLRequest shape HumanInTheLoopMiddleware raises."""
    return SimpleNamespace(value={"action_requests": [{"name": name}]})


# --- advance_phase --------------------------------------------------------------------------


def test_no_jobs_csv_means_the_search_failed(streamlit_app):
    assert streamlit_app.advance_phase({}) == "search_failed"
    assert streamlit_app.advance_phase({"pathToJobsCsv": ""}) == "search_failed"


def test_a_missing_csv_wins_over_a_pending_interrupt(streamlit_app):
    # The csv check comes first: without a csv there is nothing to review or apply to, whatever
    # the graph happens to be paused on.
    response = {"__interrupt__": [_interrupt("resume_corrections_and_download")]}
    assert streamlit_app.advance_phase(response) == "search_failed"


def test_resume_tailoring_interrupt_moves_to_reviewing(streamlit_app):
    response = {
        "pathToJobsCsv": "/tmp/jobs.csv",
        "__interrupt__": [_interrupt("resume_corrections_and_download")],
    }
    assert streamlit_app.advance_phase(response) == "reviewing"


def test_request_application_review_interrupt_moves_to_applying_wait(streamlit_app):
    response = {
        "pathToJobsCsv": "/tmp/jobs.csv",
        "__interrupt__": [_interrupt("request_application_review")],
    }
    assert streamlit_app.advance_phase(response) == "applying_wait"


@pytest.mark.parametrize("interrupts", [None, []])
def test_no_interrupt_means_the_run_finished(streamlit_app, interrupts):
    response = {"pathToJobsCsv": "/tmp/jobs.csv", "__interrupt__": interrupts}
    assert streamlit_app.advance_phase(response) == "done"


def test_an_unrecognized_interrupt_falls_through_to_done(streamlit_app):
    response = {"pathToJobsCsv": "/tmp/jobs.csv", "__interrupt__": [_interrupt("some_new_tool")]}
    assert streamlit_app.advance_phase(response) == "done"


# --- update_job_row -------------------------------------------------------------------------


@pytest.fixture
def two_row_csv(tmp_path):
    rows = [job_row(url="https://x/1"), job_row(url="https://x/2", company="Globex")]
    return write_csv(tmp_path / "jobs.csv", rows, CSV_FIELDNAMES)


def test_a_new_column_is_appended_and_back_filled(streamlit_app, two_row_csv):
    streamlit_app.update_job_row(two_row_csv, "https://x/1", apply_resume_corrections=True)

    rows, fieldnames = read_csv(two_row_csv)
    assert fieldnames == [*CSV_FIELDNAMES, "apply_resume_corrections"]
    assert [r["apply_resume_corrections"] for r in rows] == ["True", ""]


def test_booleans_are_written_as_python_strings(streamlit_app, two_row_csv):
    # "True"/"False" is exactly what resume_corrections_and_download's
    # `.strip().lower() != "true"` gate reads back, so the casing here matters.
    streamlit_app.update_job_row(two_row_csv, "https://x/1", apply_resume_corrections=True)
    streamlit_app.update_job_row(two_row_csv, "https://x/2", apply_resume_corrections=False)

    rows, _ = read_csv(two_row_csv)
    assert [r["apply_resume_corrections"] for r in rows] == ["True", "False"]


def test_only_the_matching_row_is_updated(streamlit_app, two_row_csv):
    streamlit_app.update_job_row(two_row_csv, "https://x/2", tailored_resume_path="/r2.pdf")

    rows, _ = read_csv(two_row_csv)
    assert [r["tailored_resume_path"] for r in rows] == ["", "/r2.pdf"]
    assert rows[0]["company"] == "Acme"  # untouched columns survive the rewrite


def test_an_existing_column_is_overwritten_not_duplicated(streamlit_app, two_row_csv):
    streamlit_app.update_job_row(two_row_csv, "https://x/1", apply_resume_corrections=True)
    streamlit_app.update_job_row(two_row_csv, "https://x/1", apply_resume_corrections=False)

    rows, fieldnames = read_csv(two_row_csv)
    assert fieldnames.count("apply_resume_corrections") == 1
    assert rows[0]["apply_resume_corrections"] == "False"


def test_multiple_fields_update_together(streamlit_app, two_row_csv):
    streamlit_app.update_job_row(
        two_row_csv, "https://x/1", apply_resume_corrections=True, tailored_resume_path="/r1.pdf"
    )

    rows, _ = read_csv(two_row_csv)
    assert rows[0]["apply_resume_corrections"] == "True"
    assert rows[0]["tailored_resume_path"] == "/r1.pdf"


def test_an_unknown_url_is_a_no_op_that_preserves_every_row(streamlit_app, two_row_csv):
    before, _ = read_csv(two_row_csv)
    streamlit_app.update_job_row(two_row_csv, "https://x/does-not-exist", apply_resume_corrections=True)

    rows, _ = read_csv(two_row_csv)
    assert len(rows) == len(before)
    # The column is still created and back-filled, just never set on any row.
    assert [r["apply_resume_corrections"] for r in rows] == ["", ""]
