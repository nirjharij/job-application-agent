import json
from types import SimpleNamespace

import pytest
import requests

from utilities import job_scraper
from utilities.job_scraper import get_job_links, parse_job_posting, scrape_jobs_to_csv
from utilities.linkedin_scraper import CSV_FIELDNAMES

from conftest import read_csv


def _search_html(*hrefs):
    items = "".join(
        f'<div data-at="job-item"><a data-at="job-item-title" href="{h}">Job</a></div>'
        for h in hrefs
    )
    return f"<html><body>{items}</body></html>"


def _detail_html(payload):
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script></head><body></body></html>"
    )


JOB_LD = {
    "@type": "JobPosting",
    "title": "Backend Engineer",
    "hiringOrganization": {"name": "Acme GmbH"},
    "jobLocation": {"address": {"addressLocality": "Berlin", "addressRegion": "Berlin"}},
    "employmentType": "FULL_TIME",
    "datePosted": "2026-01-15",
    "baseSalary": {"currency": "EUR", "value": {"minValue": 60000, "maxValue": 80000}},
    "description": "<p>We need <b>Python</b>.</p><p>And SQL.</p>",
}


def _stub_get(monkeypatch, pages):
    """pages: a single html string, or a list served one per _get call."""
    calls = []
    bodies = [pages] if isinstance(pages, str) else list(pages)

    def fake_get(url, timeout=15):
        calls.append(url)
        return SimpleNamespace(text=bodies[min(len(calls) - 1, len(bodies) - 1)])

    monkeypatch.setattr(job_scraper, "_get", fake_get)
    return calls


def test_get_job_links_absolutizes_hrefs(monkeypatch, no_sleep):
    _stub_get(monkeypatch, _search_html("/stellenangebote--a--1-inline.html"))
    assert get_job_links("https://www.stepstone.de/jobs?q=x") == [
        "https://www.stepstone.de/stellenangebote--a--1-inline.html"
    ]


def test_get_job_links_skips_items_without_a_title_link(monkeypatch, no_sleep):
    _stub_get(monkeypatch, '<div data-at="job-item"><span>no link</span></div>' + _search_html("/b"))
    assert get_job_links("https://www.stepstone.de/jobs?q=x") == ["https://www.stepstone.de/b"]


def test_get_job_links_honors_limit_mid_page(monkeypatch, no_sleep):
    _stub_get(monkeypatch, _search_html("/a", "/b", "/c"))
    links = get_job_links("https://www.stepstone.de/jobs?q=x", limit=2)
    assert links == ["https://www.stepstone.de/a", "https://www.stepstone.de/b"]


def test_get_job_links_stops_when_a_page_has_no_items(monkeypatch, no_sleep):
    calls = _stub_get(monkeypatch, [_search_html("/a"), "<html></html>", _search_html("/c")])
    links = get_job_links("https://www.stepstone.de/jobs?q=x", max_pages=3)
    assert links == ["https://www.stepstone.de/a"]
    assert len(calls) == 2  # the third page is never fetched


def test_get_job_links_appends_the_page_param_from_page_two(monkeypatch, no_sleep):
    calls = _stub_get(monkeypatch, _search_html("/a"))
    get_job_links("https://www.stepstone.de/jobs?q=x", max_pages=2)
    assert calls == ["https://www.stepstone.de/jobs?q=x", "https://www.stepstone.de/jobs?q=x&page=2"]


def test_parse_job_posting_returns_all_columns(monkeypatch):
    _stub_get(monkeypatch, _detail_html(JOB_LD))
    job = parse_job_posting("https://www.stepstone.de/a")

    assert job == {
        "title": "Backend Engineer",
        "company": "Acme GmbH",
        "location": "Berlin, Berlin",
        "employment_type": "FULL_TIME",
        "date_posted": "2026-01-15",
        "salary_min": 60000,
        "salary_max": 80000,
        "salary_currency": "EUR",
        "description": "We need\nPython\n.\nAnd SQL.",
        "url": "https://www.stepstone.de/a",
    }


def test_parse_job_posting_returns_empty_without_a_jobposting_block(monkeypatch):
    _stub_get(monkeypatch, "<html><head><script type='application/ld+json'>{}</script></head></html>")
    assert parse_job_posting("https://www.stepstone.de/a") == {}


@pytest.mark.parametrize(
    "address,expected",
    [
        ({"addressLocality": "Berlin", "addressRegion": "Berlin"}, "Berlin, Berlin"),
        ({"addressLocality": "Berlin"}, "Berlin"),
        ({"addressRegion": "Bayern"}, "Bayern"),
        ({"addressLocality": ""}, ""),  # filter(None, ...) drops "" as well as None
        ({}, ""),
    ],
)
def test_parse_job_posting_joins_locality_and_region(monkeypatch, address, expected):
    _stub_get(monkeypatch, _detail_html({**JOB_LD, "jobLocation": {"address": address}}))
    assert parse_job_posting("https://www.stepstone.de/a")["location"] == expected


def test_parse_job_posting_blanks_salary_when_basesalary_is_absent(monkeypatch):
    payload = {k: v for k, v in JOB_LD.items() if k != "baseSalary"}
    _stub_get(monkeypatch, _detail_html(payload))
    job = parse_job_posting("https://www.stepstone.de/a")
    assert (job["salary_min"], job["salary_max"], job["salary_currency"]) == (None, None, None)


def test_explicit_null_basesalary_raises(monkeypatch):
    # Documents current behavior (bug): `.get("baseSalary", {})` returns None for an explicit null,
    # and only `salary` on line 56 is guarded — `salary_currency` on line 72 is not.
    _stub_get(monkeypatch, _detail_html({**JOB_LD, "baseSalary": None}))
    with pytest.raises(AttributeError):
        parse_job_posting("https://www.stepstone.de/a")


def test_list_valued_joblocation_raises(monkeypatch):
    # Documents current behavior (bug): a list jobLocation is legal JobPosting JSON-LD.
    _stub_get(monkeypatch, _detail_html({**JOB_LD, "jobLocation": [{"address": {}}]}))
    with pytest.raises(AttributeError):
        parse_job_posting("https://www.stepstone.de/a")


def _stub_pipeline(monkeypatch, links, applied=(), parsed=None):
    parse_calls = []
    monkeypatch.setattr(job_scraper, "get_job_links", lambda *a, **k: list(links))
    monkeypatch.setattr(job_scraper, "is_job_applied", lambda url: url in applied)

    def fake_parse(url):
        parse_calls.append(url)
        default = {**dict.fromkeys(CSV_FIELDNAMES, ""), "url": url}
        result = (parsed or {}).get(url, default)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(job_scraper, "parse_job_posting", fake_parse)
    return parse_calls


def test_scrape_jobs_to_csv_writes_header_and_rows(monkeypatch, tmp_path, no_sleep):
    _stub_pipeline(monkeypatch, ["https://x/a", "https://x/b"])
    out = tmp_path / "jobs.csv"
    scrape_jobs_to_csv("https://www.stepstone.de/jobs?q=x", str(out))

    rows, fieldnames = read_csv(out)
    assert fieldnames == CSV_FIELDNAMES
    assert [r["url"] for r in rows] == ["https://x/a", "https://x/b"]


def test_scrape_jobs_to_csv_skips_applied_jobs_before_parsing(monkeypatch, tmp_path, no_sleep):
    parse_calls = _stub_pipeline(monkeypatch, ["https://x/a", "https://x/b"], applied={"https://x/a"})
    out = tmp_path / "jobs.csv"
    scrape_jobs_to_csv("https://www.stepstone.de/jobs?q=x", str(out))

    rows, _ = read_csv(out)
    assert [r["url"] for r in rows] == ["https://x/b"]
    assert parse_calls == ["https://x/b"]  # the applied link short-circuits before the fetch


def test_scrape_jobs_to_csv_writes_no_row_for_an_unparseable_page(monkeypatch, tmp_path, no_sleep):
    _stub_pipeline(monkeypatch, ["https://x/a", "https://x/b"], parsed={"https://x/a": {}})
    out = tmp_path / "jobs.csv"
    scrape_jobs_to_csv("https://www.stepstone.de/jobs?q=x", str(out))

    rows, _ = read_csv(out)
    assert [r["url"] for r in rows] == ["https://x/b"]


def test_scrape_jobs_to_csv_survives_a_request_error_on_one_job(monkeypatch, tmp_path, no_sleep):
    _stub_pipeline(
        monkeypatch,
        ["https://x/a", "https://x/b"],
        parsed={"https://x/a": requests.RequestException("boom")},
    )
    out = tmp_path / "jobs.csv"
    scrape_jobs_to_csv("https://www.stepstone.de/jobs?q=x", str(out))

    rows, _ = read_csv(out)
    assert [r["url"] for r in rows] == ["https://x/b"]


def test_stepstone_and_linkedin_share_one_csv_schema(monkeypatch, tmp_path, no_sleep):
    # The fieldnames are duplicated in job_scraper.scrape_jobs_to_csv and
    # linkedin_scraper.CSV_FIELDNAMES; agent tools read both csvs identically, so they must not drift.
    _stub_pipeline(monkeypatch, [])
    out = tmp_path / "jobs.csv"
    scrape_jobs_to_csv("https://www.stepstone.de/jobs?q=x", str(out))
    _, fieldnames = read_csv(out)
    assert fieldnames == CSV_FIELDNAMES
