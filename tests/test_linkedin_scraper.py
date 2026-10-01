import json
from types import SimpleNamespace

import pytest

from utilities.linkedin import linkedin_scraper
from utilities.linkedin.linkedin_scraper import CSV_FIELDNAMES, LinkedInJobScraper

from conftest import read_csv

scraper = LinkedInJobScraper()

POSTING = """Acme GmbH
Backend Engineer
Berlin, Germany · 2 days ago · 47 applicants
Full-time · Mid-Senior level

About the job
   We need Python and SQL.
"""


def test_extract_json_reads_the_first_content_block():
    assert scraper._extract_json([{"text": json.dumps({"job_ids": ["1"]})}]) == {"job_ids": ["1"]}


def test_parse_job_posting_text_splits_a_typical_posting():
    company, location, description = scraper._parse_job_posting_text(POSTING)
    assert company == "Acme GmbH"
    assert location == "Berlin, Germany"
    assert description == "We need Python and SQL."


@pytest.mark.parametrize("text", ["", "   \n\n  \t "])
def test_parse_job_posting_text_handles_empty_input(text):
    assert scraper._parse_job_posting_text(text) == ("", "", text)


def test_company_is_the_first_non_blank_line():
    company, _, _ = scraper._parse_job_posting_text("\n\n   \nAcme GmbH\nBackend Engineer")
    assert company == "Acme GmbH"


def test_separator_must_have_surrounding_spaces():
    # The heuristic looks for " · " (U+00B7 padded); LinkedIn's unpadded variant is not a match.
    _, location, _ = scraper._parse_job_posting_text("Acme\nBerlin·2 days ago")
    assert location == ""


def test_separator_line_beyond_the_sixth_is_missed():
    # lines[:6] bounds the search, so a posting with a long preamble yields no location.
    text = "Acme\nl2\nl3\nl4\nl5\nl6\nBerlin · 2 days ago"
    _, location, _ = scraper._parse_job_posting_text(text)
    assert location == ""


def test_only_the_first_about_the_job_marker_splits():
    text = "Acme\nAbout the job\nfirst\nAbout the job\nsecond"
    _, _, description = scraper._parse_job_posting_text(text)
    assert description == "first\nAbout the job\nsecond"


def test_description_falls_back_to_the_raw_unstripped_text():
    # Asymmetry worth pinning: with the marker the description is .strip()ed, without it the
    # original text (leading whitespace and all) is returned verbatim.
    text = "  Acme\n  the whole body  "
    _, _, description = scraper._parse_job_posting_text(text)
    assert description == text


def _search_message(refs):
    payload = {"references": {"search_results": refs}}
    return SimpleNamespace(name="search_jobs", content=[{"text": json.dumps(payload)}])


def _details_message(url, posting=POSTING):
    payload = {"url": url, "sections": {"job_posting": posting}}
    return SimpleNamespace(name="get_job_details", content=[{"text": json.dumps(payload)}])


@pytest.fixture
def no_dedup(monkeypatch):
    def _apply(applied=()):
        monkeypatch.setattr(linkedin_scraper, "is_job_applied", lambda url: url in applied)

    _apply()
    return _apply


def test_tool_messages_to_csv_joins_titles_to_details(tmp_path, no_dedup):
    messages = [
        _search_message([{"kind": "job", "url": "/jobs/view/111/", "text": "Backend Engineer"}]),
        _details_message("https://www.linkedin.com/jobs/view/111/"),
    ]
    out = tmp_path / "jobs.csv"
    written = scraper.linkedin_tool_messages_to_csv(messages, str(out))

    rows, fieldnames = read_csv(out)
    assert written == 1
    assert fieldnames == CSV_FIELDNAMES
    assert rows[0]["title"] == "Backend Engineer"
    assert rows[0]["company"] == "Acme GmbH"
    assert rows[0]["location"] == "Berlin, Germany"
    assert rows[0]["url"] == "https://www.linkedin.com/jobs/view/111/"


def test_relative_and_absolute_urls_resolve_to_the_same_job_id(tmp_path, no_dedup):
    # ref urls come back relative ("/jobs/view/111/") but details urls are absolute; .strip("/")
    # plus the last path segment is what makes the two join.
    messages = [
        _search_message([{"kind": "job", "url": "/jobs/view/111/", "text": "Backend Engineer"}]),
        _details_message("https://www.linkedin.com/jobs/view/111/"),
    ]
    out = tmp_path / "jobs.csv"
    scraper.linkedin_tool_messages_to_csv(messages, str(out))
    assert read_csv(out)[0][0]["title"] == "Backend Engineer"


def test_title_is_blank_when_no_search_ref_matched(tmp_path, no_dedup):
    out = tmp_path / "jobs.csv"
    scraper.linkedin_tool_messages_to_csv([_details_message("https://x/jobs/view/999/")], str(out))
    assert read_csv(out)[0][0]["title"] == ""


def test_non_job_refs_are_ignored(tmp_path, no_dedup):
    messages = [
        _search_message([{"kind": "company", "url": "/company/111/", "text": "Acme"}]),
        _details_message("https://www.linkedin.com/jobs/view/111/"),
    ]
    out = tmp_path / "jobs.csv"
    scraper.linkedin_tool_messages_to_csv(messages, str(out))
    assert read_csv(out)[0][0]["title"] == ""


def test_columns_linkedin_cannot_supply_are_blank(tmp_path, no_dedup):
    out = tmp_path / "jobs.csv"
    scraper.linkedin_tool_messages_to_csv([_details_message("https://x/jobs/view/111/")], str(out))
    row = read_csv(out)[0][0]
    assert all(
        row[c] == ""
        for c in ("employment_type", "date_posted", "salary_min", "salary_max", "salary_currency")
    )


def test_location_argument_is_the_fallback(tmp_path, no_dedup):
    message = _details_message("https://x/jobs/view/111/", posting="Acme\nno separator here")
    out = tmp_path / "jobs.csv"
    scraper.linkedin_tool_messages_to_csv([message], str(out), location="Munich")
    assert read_csv(out)[0][0]["location"] == "Munich"


def test_applied_jobs_are_excluded(tmp_path, no_dedup):
    no_dedup(applied={"https://www.linkedin.com/jobs/view/111/"})
    messages = [
        _details_message("https://www.linkedin.com/jobs/view/111/"),
        _details_message("https://www.linkedin.com/jobs/view/222/"),
    ]
    out = tmp_path / "jobs.csv"
    written = scraper.linkedin_tool_messages_to_csv(messages, str(out))

    rows, _ = read_csv(out)
    assert written == 1
    assert [r["url"] for r in rows] == ["https://www.linkedin.com/jobs/view/222/"]


def test_string_content_messages_are_skipped(tmp_path, no_dedup):
    # The isinstance(..., list) gate: a ToolMessage whose content is plain text is not MCP output.
    messages = [
        SimpleNamespace(name="get_job_details", content="not a content-block list"),
        _details_message("https://x/jobs/view/111/"),
    ]
    out = tmp_path / "jobs.csv"
    assert scraper.linkedin_tool_messages_to_csv(messages, str(out)) == 1


def test_malformed_json_in_one_message_does_not_abort(tmp_path, no_dedup):
    messages = [
        SimpleNamespace(name="get_job_details", content=[{"text": "{not json"}]),
        _details_message("https://x/jobs/view/111/"),
    ]
    out = tmp_path / "jobs.csv"
    assert scraper.linkedin_tool_messages_to_csv(messages, str(out)) == 1


def test_return_value_matches_the_rows_written(tmp_path, no_dedup):
    messages = [_details_message(f"https://x/jobs/view/{i}/") for i in (1, 2, 3)]
    out = tmp_path / "jobs.csv"
    written = scraper.linkedin_tool_messages_to_csv(messages, str(out))
    assert written == len(read_csv(out)[0]) == 3


def test_module_level_scraper_is_a_shared_singleton():
    assert isinstance(linkedin_scraper._scraper, LinkedInJobScraper)
