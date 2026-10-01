"""Tests for job_finder (LinkedIn-only search)."""

import os

import pytest

from tools import job_search_agent_tools
from tools.job_search_agent_tools import job_finder

from conftest import FakeRuntime


@pytest.fixture
def scraper(monkeypatch):
    calls = []

    async def fake_linkedin(role, city, output_csv, **kwargs):
        calls.append((role, city, output_csv, kwargs))

    monkeypatch.setattr(job_search_agent_tools, "scrape_linkedin_jobs_to_csv", fake_linkedin)
    return calls


async def test_role_and_city_are_passed_through_untouched(scraper):
    await job_finder.coroutine(runtime=FakeRuntime(), role="dev", city="Berlin")

    (role, city, _, _), = scraper
    assert (role, city) == ("dev", "Berlin")


@pytest.mark.parametrize("num_jobs,expected", [(5, 5), (0, 1), (-3, 1), (15, 10), (3, 3)])
async def test_num_jobs_is_clamped_between_one_and_ten(scraper, num_jobs, expected):
    await job_finder.coroutine(runtime=FakeRuntime(), role="dev", city="berlin", num_jobs=num_jobs)
    assert scraper[0][3]["limit"] == expected


async def test_num_jobs_defaults_to_three(scraper):
    await job_finder.coroutine(runtime=FakeRuntime(), role="dev", city="berlin")
    assert scraper[0][3]["limit"] == 3


async def test_csv_lands_in_the_configured_output_directory(scraper):
    result = await job_finder.coroutine(runtime=FakeRuntime(), role="dev", city="berlin")

    expected = os.path.join(job_search_agent_tools.OUTPUT_DIRECTORY, job_search_agent_tools.CSV_FILENAME)
    assert result.update["pathToJobsCsv"] == expected
    assert scraper[0][2] == expected


async def test_returns_a_command_updating_state_and_messages(scraper):
    runtime = FakeRuntime(tool_call_id="call-xyz")
    result = await job_finder.coroutine(runtime=runtime, role="dev", city="berlin")

    (message,) = result.update["messages"]
    assert message.tool_call_id == "call-xyz"
    assert result.update["pathToJobsCsv"] in message.content


async def test_search_failure_clears_the_csv_path_and_reports_the_error(monkeypatch):
    async def fake_linkedin(role, city, output_csv, **kwargs):
        raise job_search_agent_tools.LinkedInSearchEmptyError("no jobs found")

    monkeypatch.setattr(job_search_agent_tools, "scrape_linkedin_jobs_to_csv", fake_linkedin)

    runtime = FakeRuntime(tool_call_id="call-xyz")
    result = await job_finder.coroutine(runtime=runtime, role="dev", city="berlin")

    assert result.update["pathToJobsCsv"] is None
    (message,) = result.update["messages"]
    assert "no jobs found" in message.content
