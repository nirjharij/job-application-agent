from types import SimpleNamespace

import pytest

from tools import apply_jobs_agent_tools as aj
from tools.apply_jobs_agent_tools import (
    _MAX_STEPS_PER_JOB,
    _fill_one_job,
    pending_tabs_open,
    post_job_apply,
    start_applying,
)

from conftest import CSV_FIELDNAMES, FakeProfileContext, FakeResponse, FakeRuntime, job_row, write_csv

JOB = {"url": "https://x/jobs/view/1/", "description": "Python role", "tailored_resume_path": "/r.pdf"}


class FakeTool:
    def __init__(self, name, result="ok"):
        self.name = name
        self.result = result
        self.calls = []

    async def ainvoke(self, call):
        self.calls.append(call)
        return SimpleNamespace(content=self.result, tool_call_id=call["id"])


# --- _fill_one_job --------------------------------------------------------------------------


async def test_fill_one_job_returns_the_models_summary(fake_llm):
    llm = fake_llm("Filled in the application.")
    assert await _fill_one_job(llm, {}, JOB, "- name: Jane") == "Filled in the application."


async def test_fill_one_job_prompt_carries_the_job_and_the_applicant(fake_llm):
    llm = fake_llm("done")
    await _fill_one_job(llm, {}, JOB, "- name: Jane\n- email: jane@example.com")

    human = llm.calls[0][1].content
    assert "URL: https://x/jobs/view/1/" in human
    assert "Description: Python role" in human
    assert "Tailored resume file: /r.pdf" in human
    assert "- email: jane@example.com" in human


async def test_fill_one_job_reports_a_missing_tailored_resume_as_none(fake_llm):
    llm = fake_llm("done")
    await _fill_one_job(llm, {}, {"url": "https://x/1", "description": ""}, "(none provided)")
    assert "Tailored resume file: none" in llm.calls[0][1].content


async def test_fill_one_job_dispatches_tool_calls_and_feeds_results_back(fake_llm):
    navigate = FakeTool("browser_navigate")
    llm = fake_llm(
        FakeResponse("", [{"name": "browser_navigate", "id": "t1", "args": {}}]),
        FakeResponse("Submitted the form."),
    )

    result = await _fill_one_job(llm, {"browser_navigate": navigate}, JOB, "- name: Jane")

    assert result == "Submitted the form."
    assert navigate.calls == [{"name": "browser_navigate", "id": "t1", "args": {}}]
    # The tool result is appended to the history the second model call sees.
    assert len(llm.calls[1]) == 4


async def test_fill_one_job_reports_an_unknown_tool_without_stopping(fake_llm):
    llm = fake_llm(
        FakeResponse("", [{"name": "nope", "id": "t1", "args": {}}]),
        FakeResponse("Recovered."),
    )

    assert await _fill_one_job(llm, {}, JOB, "-") == "Recovered."
    assert llm.calls[1][-1].content == "Unknown tool 'nope'."


async def test_fill_one_job_gives_up_after_the_step_cap(fake_llm):
    llm = fake_llm(FakeResponse("", [{"name": "nope", "id": "t1", "args": {}}]))

    assert await _fill_one_job(llm, {}, JOB, "-") == "Gave up after too many steps without finishing."
    assert len(llm.calls) == _MAX_STEPS_PER_JOB


# --- start_applying -------------------------------------------------------------------------


async def test_start_applying_without_a_csv_path():
    result = await start_applying.coroutine(runtime=FakeRuntime(state={}))
    assert result.content == "No jobs csv path found in state."


async def test_start_applying_with_an_empty_csv(tmp_path):
    path = write_csv(tmp_path / "jobs.csv", [], CSV_FIELDNAMES)
    result = await start_applying.coroutine(runtime=FakeRuntime(state={"pathToJobsCsv": path}))
    assert result.content == "No jobs found in the csv — nothing to apply to."


@pytest.fixture
def apply_env(monkeypatch, fake_llm):
    """Stubs the two things start_applying reaches for after its guards: MCP tools and the model."""

    def _setup(fill_results=None):
        llm = fake_llm("done")
        monkeypatch.setattr(aj, "init_chat_model", lambda model: llm)

        async def fake_tools():
            return [FakeTool("browser_navigate")]

        monkeypatch.setattr(aj, "get_apply_jobs_mcp_tools", fake_tools)

        seen = []

        async def fake_fill(llm_with_tools, tools_by_name, job, profile_lines):
            seen.append((job, profile_lines))
            outcome = (fill_results or {}).get(job["url"], "Filled it in.")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(aj, "_fill_one_job", fake_fill)
        return seen

    return _setup


async def test_start_applying_processes_every_row_sequentially(jobs_csv, apply_env):
    seen = apply_env()
    runtime = FakeRuntime(
        state={"pathToJobsCsv": jobs_csv}, context=FakeProfileContext({"name": "Jane"})
    )

    result = await start_applying.coroutine(runtime=runtime)

    assert [job["url"] for job, _ in seen] == [
        "https://www.linkedin.com/jobs/view/111/",
        "https://www.linkedin.com/jobs/view/222/",
    ]
    assert "Processed 2 job(s):" in result.content
    assert aj._pending_jobs and len(aj._pending_jobs) == 2


async def test_start_applying_formats_the_applicant_profile(jobs_csv, apply_env):
    seen = apply_env()
    profile = {"name": "Jane", "email": "jane@example.com", "phone": "", "notice_period": None}
    runtime = FakeRuntime(state={"pathToJobsCsv": jobs_csv}, context=FakeProfileContext(profile))

    await start_applying.coroutine(runtime=runtime)

    _, profile_lines = seen[0]
    assert profile_lines == "- name: Jane\n- email: jane@example.com"  # blank fields are dropped


@pytest.mark.parametrize("profile", [{}, None])
async def test_start_applying_falls_back_when_no_profile_was_collected(jobs_csv, apply_env, profile):
    seen = apply_env()
    runtime = FakeRuntime(state={"pathToJobsCsv": jobs_csv}, context=FakeProfileContext(profile))

    await start_applying.coroutine(runtime=runtime)
    assert seen[0][1] == "(none provided)"


async def test_start_applying_reports_a_failed_job_without_aborting_the_rest(jobs_csv, apply_env):
    seen = apply_env({"https://www.linkedin.com/jobs/view/111/": RuntimeError("tab crashed")})
    runtime = FakeRuntime(state={"pathToJobsCsv": jobs_csv}, context=FakeProfileContext({}))

    result = await start_applying.coroutine(runtime=runtime)

    assert len(seen) == 2  # the second job still ran
    assert "- https://www.linkedin.com/jobs/view/111/: Failed: tab crashed" in result.content
    assert "- https://www.linkedin.com/jobs/view/222/: Filled it in." in result.content


async def test_start_applying_binds_the_mcp_tools_to_the_model(jobs_csv, apply_env, monkeypatch):
    apply_env()
    runtime = FakeRuntime(state={"pathToJobsCsv": jobs_csv}, context=FakeProfileContext({}))
    await start_applying.coroutine(runtime=runtime)

    assert [t.name for t in aj.init_chat_model("m").bound_tools] == ["browser_navigate"]


# --- pending_tabs_open ----------------------------------------------------------------------


async def test_pending_tabs_open_is_false_before_the_browser_has_launched(monkeypatch):
    # A bare read: polling must never trigger the lazy MCP launch just to answer the question.
    monkeypatch.setattr(aj, "get_current_apply_jobs_session", lambda: None)
    assert await pending_tabs_open() is False


def _session(monkeypatch, text, has_content=True):
    content = [SimpleNamespace(text=text)] if has_content else []

    class Session:
        async def call_tool(self, name, args):
            assert (name, args) == ("browser_tabs", {"action": "list"})
            return SimpleNamespace(content=content)

    monkeypatch.setattr(aj, "get_current_apply_jobs_session", lambda: Session())


@pytest.mark.parametrize(
    "tab_list,expected",
    [
        ("- 0: [Backend Engineer](https://x/jobs/view/1/)", True),
        ("- 0: [New Tab](about:blank)", False),
        ("- 0: [New Tab](about:blank)\n- 1: [New Tab](about:blank)", False),
        ("- 0: [New Tab](about:blank)\n- 1: [Job](https://x/jobs/view/1/)", True),
        ("no tabs", False),
    ],
)
async def test_pending_tabs_open_reads_the_tab_list(monkeypatch, tab_list, expected):
    _session(monkeypatch, tab_list)
    assert await pending_tabs_open() is expected


async def test_pending_tabs_open_is_false_when_the_tool_returns_nothing(monkeypatch):
    _session(monkeypatch, "", has_content=False)
    assert await pending_tabs_open() is False


async def test_a_titled_markdown_link_matches_nothing(monkeypatch):
    # The regex captures \S+, which cannot span the space before a link title, so a titled link
    # yields no url at all. Pinned because it makes a genuinely open tab look closed and would
    # let the UI resume the interrupt early.
    _session(monkeypatch, '- 0: [Job](https://x/jobs/view/1/ "Backend Engineer")')
    assert await pending_tabs_open() is False


# --- post_job_apply -------------------------------------------------------------------------


@pytest.fixture
def marked(monkeypatch):
    urls = []
    monkeypatch.setattr(aj, "mark_job_applied", urls.append)
    return urls


async def test_post_job_apply_with_nothing_pending(marked):
    aj._pending_jobs = []
    result = await post_job_apply.coroutine(runtime=FakeRuntime())

    assert result.content == "No pending applications to mark as applied."
    assert marked == []


async def test_post_job_apply_marks_every_pending_job_in_order(marked):
    aj._pending_jobs = [job_row(url="https://x/1"), job_row(url="https://x/2")]
    result = await post_job_apply.coroutine(runtime=FakeRuntime(tool_call_id="c9"))

    assert marked == ["https://x/1", "https://x/2"]
    assert result.content == "2 application(s) marked as applied."
    assert result.tool_call_id == "c9"


async def test_post_job_apply_drains_the_pending_list(marked):
    aj._pending_jobs = [job_row(url="https://x/1")]

    await post_job_apply.coroutine(runtime=FakeRuntime())
    assert aj._pending_jobs == []

    # A second call is a no-op rather than a duplicate round of DB writes.
    second = await post_job_apply.coroutine(runtime=FakeRuntime())
    assert second.content == "No pending applications to mark as applied."
    assert marked == ["https://x/1"]
