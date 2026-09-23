import logging
import re

from langchain.messages import HumanMessage, SystemMessage, ToolMessage
from langchain.tools import ToolRuntime, tool

from config import get_llm
from prompts.job_filler_agent_prompt import JOB_FILLER_AGENT_SYSTEM_PROMPT
from utilities.applicant_profile import get_or_extract_applicant_profile
from utilities.jobs_csv import (
    JOB_APPLICATION_STATUS_APPLIED,
    JOB_APPLICATION_STATUS_APPLYING,
    read_job_rows,
    update_job_row,
)
from utilities.jobs_db import mark_job_applied
from utilities.mcp_tools import MCPConnectionError, get_apply_jobs_mcp_tools, get_current_apply_jobs_session

# Populated by start_applying, consumed by post_job_apply — within the same apply_jobs_agent run, so
# module-level state is enough to hand off between them.
_pending_jobs: list[dict] = []

# Bounds each job's tool-calling loop below so a confused model can't run forever on one job.
_MAX_STEPS_PER_JOB = 40

logger = logging.getLogger(__name__)

def _mark_applied(csv_path: str | None, url: str) -> None:
    """Mark a job applied in Postgres (the real dedup source of truth) and, if we know the csv
    path, flip its job_application_status to "applied" too — this second write is what lets a
    restarted run detect and finish a job that was left mid-"applying" by a crash."""
    mark_job_applied(url)
    if csv_path:
        update_job_row(csv_path, url, job_application_status=JOB_APPLICATION_STATUS_APPLIED)

async def _fill_one_job(llm_with_tools, tools_by_name: dict, job: dict, profile_lines: str) -> str:
    """Drive a single job application to completion via a plain bind_tools() + loop"""
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
            try:
                result = await tool_fn.ainvoke(call)
            except Exception as e:
                logger.exception("Tool '%s' failed while filling job %s", call["name"], job.get("url"))
                messages.append(ToolMessage(content=f"Tool '{call['name']}' failed: {e}", tool_call_id=call["id"]))
                continue
            messages.append(result)
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

    rows = read_job_rows(csv_path)

    _pending_jobs = rows

    if not _pending_jobs:
        return ToolMessage("No jobs found in the csv — nothing to apply to.", tool_call_id=runtime.tool_call_id)

    pdf_base64 = runtime.state.get("pdfBase64")
    profile = await get_or_extract_applicant_profile(pdf_base64) if pdf_base64 else {}
    profile_lines = "\n".join(f"- {k}: {v}" for k, v in profile.items() if v) or "(none provided)"

    try:
        tools = await get_apply_jobs_mcp_tools()
    except MCPConnectionError as e:
        return ToolMessage(f"Could not start applying: {e}", tool_call_id=runtime.tool_call_id)
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = get_llm().bind_tools(tools)

    # Sequential, not asyncio.gather: every job shares the same underlying browser, which only ever
    # has one "current" tab — running jobs concurrently would race on that shared focus state (unlike
    # resume_handler_agent_tools.py's parallel LLM calls, which touch no shared browser state).
    results = []
    for job in _pending_jobs:
        update_job_row(csv_path, job["url"], job_application_status=JOB_APPLICATION_STATUS_APPLYING)
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
    try:
        result = await session.call_tool("browser_tabs", {"action": "list"})
        text = result.content[0].text if result.content else ""
    except Exception:
        logger.exception("Failed to check open tabs via MCP — assuming tabs are still open")
        return True
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

    csv_path = runtime.state.get("pathToJobsCsv")
    for job in jobs:
        _mark_applied(csv_path, job["url"])

    return ToolMessage(
        f"{len(jobs)} application(s) marked as applied.",
        tool_call_id=runtime.tool_call_id,
    )
