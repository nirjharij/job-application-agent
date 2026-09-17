"""Tests for job_finder.

These assert the behavior job_finder is meant to have. They currently fail: config.OUTPUT_PATH is
`os.makedirs(...)`, which returns None, so `os.path.join(OUTPUT_PATH, CSV_FILENAME)` raises
TypeError on every call. Fixing config.py to `OUTPUT_PATH = OUTPUT_DIRECTORY` turns them green.
"""

import os

import pytest

from tools import job_search_agent_tools
from tools.job_search_agent_tools import job_finder

from conftest import FakeRuntime


@pytest.fixture
def scrapers(monkeypatch):
    calls = {"stepstone": [], "linkedin": []}

    def fake_stepstone(search_url, output_csv, **kwargs):
        calls["stepstone"].append((search_url, output_csv, kwargs))

    async def fake_linkedin(role, city, output_csv, **kwargs):
        calls["linkedin"].append((role, city, output_csv, kwargs))

    monkeypatch.setattr(job_search_agent_tools, "scrape_jobs_to_csv", fake_stepstone)
    monkeypatch.setattr(job_search_agent_tools, "scrape_linkedin_jobs_to_csv", fake_linkedin)
    return calls


async def test_stepstone_search_url_is_built_from_role_and_city(scrapers):
    await job_finder.coroutine(
        runtime=FakeRuntime(state={"numJobs": 3}),
        platform="stepstone",
        role="  backend-engineer  ",
        city="Berlin",
        radius=50,
    )

    search_url, _, _ = scrapers["stepstone"][0]
    # role is .strip()ed but city is .lower()ed — the asymmetry is deliberate.
    assert search_url == "https://www.stepstone.de/work/backend-engineer/in-berlin?radius=50"


async def test_stepstone_radius_defaults_to_30(scrapers):
    await job_finder.coroutine(
        runtime=FakeRuntime(), platform="stepstone", role="dev", city="berlin"
    )
    assert scrapers["stepstone"][0][0].endswith("?radius=30")


@pytest.mark.parametrize("platform", ["STEPSTONE", "StepStone"])
async def test_platform_match_is_case_insensitive(scrapers, platform):
    await job_finder.coroutine(runtime=FakeRuntime(), platform=platform, role="dev", city="berlin")
    assert scrapers["stepstone"] and not scrapers["linkedin"]


@pytest.mark.parametrize("platform", ["linkedin", "LinkedIn", "anything-else"])
async def test_every_other_platform_routes_to_linkedin(scrapers, platform):
    await job_finder.coroutine(runtime=FakeRuntime(), platform=platform, role="dev", city="Berlin")

    (role, city, _, _), = scrapers["linkedin"]
    assert not scrapers["stepstone"]
    # The LinkedIn path passes role/city through untouched, unlike the StepStone url builder.
    assert (role, city) == ("dev", "Berlin")


@pytest.mark.parametrize("state,expected", [({"numJobs": 7}, 7), ({}, 3), ({"numJobs": 0}, 3)])
async def test_num_jobs_comes_from_state_and_falls_back_to_three(scrapers, state, expected):
    await job_finder.coroutine(
        runtime=FakeRuntime(state=state), platform="stepstone", role="dev", city="berlin"
    )
    assert scrapers["stepstone"][0][2]["limit"] == expected


async def test_csv_lands_in_the_configured_output_directory(scrapers):
    result = await job_finder.coroutine(
        runtime=FakeRuntime(), platform="stepstone", role="dev", city="berlin"
    )

    expected = os.path.join(job_search_agent_tools.OUTPUT_PATH, job_search_agent_tools.CSV_FILENAME)
    assert result.update["pathToJobsCsv"] == expected
    assert scrapers["stepstone"][0][1] == expected


async def test_returns_a_command_updating_state_and_messages(scrapers):
    runtime = FakeRuntime(tool_call_id="call-xyz")
    result = await job_finder.coroutine(
        runtime=runtime, platform="stepstone", role="dev", city="berlin"
    )

    (message,) = result.update["messages"]
    assert message.tool_call_id == "call-xyz"
    assert result.update["pathToJobsCsv"] in message.content
