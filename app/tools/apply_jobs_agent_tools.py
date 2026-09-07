import csv
import re

from langchain.chat_models import init_chat_model
from langchain.messages import HumanMessage, SystemMessage, ToolMessage
from langchain.tools import ToolRuntime, tool

from config import MODEL
from prompts.job_filler_agent_prompt import JOB_FILLER_AGENT_SYSTEM_PROMPT
from utilities.jobs_db import mark_job_applied
from utilities.mcp_tools import get_apply_jobs_mcp_tools, get_current_apply_jobs_session

# Populated by start_applying, consumed by post_job_apply — within the same apply_jobs_agent run, so
# module-level state is enough to hand off between them.
_pending_jobs: list[dict] = []

# Bounds each job's tool-calling loop below so a confused model can't run forever on one job.
_MAX_STEPS_PER_JOB = 40


async def _fill_one_job(llm_with_tools, tools_by_name: dict, job: dict, profile_lines: str) -> str:
    """Drive a single job application to completion via a plain bind_tools() + loop — deliberately
    not a nested LangGraph create_agent: this never needs interrupting/resuming (the only interrupt
    anywhere in this system is post_job_apply, in the outer apply_jobs_agent), so a bare tool-calling
    loop is simpler and avoids nesting LangGraph three levels deep for no benefit. Mirrors
    resume_handler_agent_tools.py's direct init_chat_model use rather than building another agent
    object — the difference here is just that this task needs several rounds of tool feedback
    (open tab, see what's there, click, see what changed, fill...), not one single completion.

    Each call gets a brand-new, job-scoped message history — nothing from any other job is ever in
    context — which is what actually fixes the earlier tab-reopening bug: there is no multi-job
    conversation left to lose track of.
    """
    messages = [
        SystemMessage(content=JOB_FILLER_AGENT_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"URL: {job['url']}\n"
            f"Description: {job.get('description', '')}\n"
            f"Tailored resume file: {job.get('tailored_resume_path') or 'none'}\n\n"
            f"Applicant details:\n{profile_lines}"
        )),
    ]
    for _ in range(_MAX_STEPS_PER_JOB):
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)
        if not response.tool_calls:
            return str(response.content)
        for call in response.tool_calls:
            tool_fn = tools_by_name.get(call["name"])
            if tool_fn is None:
                messages.append(ToolMessage(content=f"Unknown tool '{call['name']}'.", tool_call_id=call["id"]))
                continue
            messages.append(await tool_fn.ainvoke(call))
    return "Gave up after too many steps without finishing."


@tool
async def start_applying(runtime: ToolRuntime) -> str:
    """Fill out every job application in the jobs csv, one at a time, each in its own fresh \
    conversation so nothing from one job carries over to the next. Call this once, exactly once, \
    before touching the browser."""
    global _pending_jobs
    csv_path = runtime.state.get("pathToJobsCsv")
    if not csv_path:
        return ToolMessage("No jobs csv path found in state.", tool_call_id=runtime.tool_call_id)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    _pending_jobs = rows

    if not _pending_jobs:
        return ToolMessage("No jobs found in the csv — nothing to apply to.", tool_call_id=runtime.tool_call_id)

    profile = runtime.context.applicantProfile or {}
    profile_lines = "\n".join(f"- {k}: {v}" for k, v in profile.items() if v) or "(none provided)"

    tools = await get_apply_jobs_mcp_tools()
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = init_chat_model(MODEL).bind_tools(tools)

    # Sequential, not asyncio.gather: every job shares the same underlying browser, which only ever
    # has one "current" tab — running jobs concurrently would race on that shared focus state (unlike
    # resume_handler_agent_tools.py's parallel LLM calls, which touch no shared browser state).
    results = []
    for job in _pending_jobs:
        try:
            summary = await _fill_one_job(llm_with_tools, tools_by_name, job, profile_lines)
        except Exception as e:
            summary = f"Failed: {e}"
        results.append(f"- {job['url']}: {summary}")

    return ToolMessage(
        f"Processed {len(_pending_jobs)} job(s):\n" + "\n".join(results) +
        "\n\nNow wait for the user to verify before submitting.",
        tool_call_id=runtime.tool_call_id,
    )


async def pending_tabs_open() -> bool:
    """True if any tab this run opened is still open. Polled by the Streamlit UI to decide when to \
    resume apply_jobs_agent's post_job_apply interrupt — the actual wait for the human to review, \
    submit, and close each tab happens there, not inside this module."""
    session = get_current_apply_jobs_session()
    if session is None:
        return False
    result = await session.call_tool("browser_tabs", {"action": "list"})
    text = result.content[0].text if result.content else ""
    urls = re.findall(r"\]\((\S+)\)", text)
    return any(url != "about:blank" for url in urls)


@tool
async def post_job_apply(runtime: ToolRuntime) -> str:
    """Mark every application opened during this run as applied. Call this once, immediately after \
    every job has been filled in — the system automatically pauses for human approval the moment \
    you call it, and only resumes once the human has reviewed, submitted, and closed every tab, so \
    there is nothing to confirm in chat first either before or after calling it."""
    global _pending_jobs
    jobs = _pending_jobs
    _pending_jobs = []

    if not jobs:
        return ToolMessage("No pending applications to mark as applied.", tool_call_id=runtime.tool_call_id)

    for job in jobs:
        mark_job_applied(job["url"])

    return ToolMessage(
        f"{len(jobs)} application(s) marked as applied.",
        tool_call_id=runtime.tool_call_id,
    )
