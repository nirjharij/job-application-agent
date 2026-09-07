from dotenv import load_dotenv
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.messages import HumanMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.postgres import PostgresStore
from langgraph.types import Command

from config import MODEL
from dataclasses import dataclass
from utilities.jobs_db import DB_URI
from prompts.apply_jobs_agent_prompt import APPLY_JOBS_AGENT_SYSTEM_PROMPT
from prompts.job_search_agent_prompt import JOB_SEARCH_AGENT_SYSTEM_PROMPT
from prompts.main_agent_prompt import MAIN_AGENT_SYSTEM_PROMPT
from prompts.resume_handler_agent_prompt import RESUME_HANDLER_AGENT_SYSTEM_PROMPT
from tools.apply_jobs_agent_tools import (
    get_apply_jobs_mcp_tools,
    post_job_apply,
    start_applying,
)
from tools.job_search_agent_tools import job_finder
from tools.resume_handler_agent_tools import (
    analyze_resume_and_make_suggestions,
    resume_corrections_and_download,
)

load_dotenv()

@dataclass
class JobSearchContext:
    platform: str
    numJobs: int
    role: str
    city: str

@dataclass
class JobApplyContext:
    applicantProfile: dict

class JobApplicationAgentState(AgentState):
    pathToJobsCsv: str
    pdfBase64: str
    applicantProfile: dict

class JobSearchAgentState(AgentState):
    pathToJobsCsv: str
    platform: str
    numJobs: int

class ResumeHandlerAgentState(AgentState):
    pathToJobsCsv: str
    pdfBase64: str
    tailoredResumeFiles: list


class ApplyJobsAgentState(AgentState):
    pathToJobsCsv: str


_store_cm = None  # holds a reference to the entered context manager below so it isn't garbage
                  # collected (which would close its connection) once build_agent() returns.


async def build_agent():
    """Build the full 3-agent system: main_agent delegates to job_search_agent and resume_handler_agent
    and apply_jobs_agent"""

    # Long-term memory store for main_agent, backed by Postgres. from_conn_string is a context
    # manager; entered manually (never exited) so the connection outlives build_agent() for the
    # lifetime of the app, same as the InMemorySaver checkpointers below. The module-level _store_cm
    # keeps it referenced — otherwise the context manager (and its connection) would be garbage
    # collected immediately, since only the store it yields, not the CM itself, is used below.
    global _store_cm
    _store_cm = PostgresStore.from_conn_string(DB_URI)
    store = _store_cm.__enter__()
    store.setup()

    job_search_agent = create_agent(
        model=MODEL,
        system_prompt=JOB_SEARCH_AGENT_SYSTEM_PROMPT,
        tools=[job_finder],
        context_schema=JobSearchContext,
        state_schema=JobSearchAgentState,
        checkpointer=True,
    )

    # Warms up the shared browser subprocess at startup rather than on the first real apply — its
    # tools aren't spliced in here since start_applying (see apply_jobs_agent_tools.py) drives them
    # itself, one fresh tool-calling loop per job, instead of apply_jobs_agent's own turns.
    await get_apply_jobs_mcp_tools()
    apply_jobs_agent = create_agent(
        model=MODEL,
        system_prompt=APPLY_JOBS_AGENT_SYSTEM_PROMPT,
        tools=[
            start_applying,
            post_job_apply,
        ],
        context_schema=JobApplyContext,
        state_schema=ApplyJobsAgentState,
        checkpointer=True,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"post_job_apply": True}
            ),
        ],
    )

    resume_handler_agent = create_agent(
        model=MODEL,
        system_prompt=RESUME_HANDLER_AGENT_SYSTEM_PROMPT,
        tools=[analyze_resume_and_make_suggestions, resume_corrections_and_download],
        state_schema=ResumeHandlerAgentState,
        checkpointer=True,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"resume_corrections_and_download": True}
            ),
        ],
    )

    @tool
    async def call_job_search_agent(platform, num_jobs, role, city, runtime: ToolRuntime) -> str:
        """Call the job search subagent to find jobs"""
        response = await job_search_agent.ainvoke(
            {
                "messages": [HumanMessage(content=f"Find {num_jobs} job(s) for {role} in {city} on {platform}")]
            },
            context=JobSearchContext(platform=platform, numJobs=num_jobs, role=role, city=city),
            config=runtime.config,
        )
        return Command(update={
            "pathToJobsCsv": response.get("pathToJobsCsv"),
            "messages": [ToolMessage(response["messages"][-1].content, tool_call_id=runtime.tool_call_id)],
        })

    @tool
    async def call_resume_handler_agent(runtime: ToolRuntime) -> str:
        """Call the resume handler subagent to analyze the resume against the jobs found so far and \
prepare tailored versions."""
        response = await resume_handler_agent.ainvoke(
            {
                "messages": [HumanMessage(
                    content="Analyze my resume against the job descriptions, then prepare tailored versions."
                )],
                "pdfBase64": runtime.state.get("pdfBase64"),
                "pathToJobsCsv": runtime.state.get("pathToJobsCsv"),
            },
            config=runtime.config,
        )
        return Command(update={
            "pathToJobsCsv": response.get("pathToJobsCsv"),
            "messages": [ToolMessage(response["messages"][-1].content, tool_call_id=runtime.tool_call_id)],
        })

    @tool
    async def call_apply_jobs_agent(runtime: ToolRuntime) -> str:
        """Call the apply-jobs subagent to fill out applications for the jobs once tailoring is complete."""
        # Same as call_resume_handler_agent above: apply_jobs_agent's own post_job_apply interrupt
        # propagates straight out of this await and pauses main_agent's own run instead — nothing
        # below this line runs until apply_jobs_agent has genuinely finished.
        response = await apply_jobs_agent.ainvoke(
            {
                "messages": [HumanMessage(content="Apply to the jobs in the jobs csv.")],
                "pathToJobsCsv": runtime.state.get("pathToJobsCsv")
            },
            context=JobApplyContext(applicantProfile=runtime.state.get("applicantProfile")),
            config=runtime.config,
        )
        return Command(update={
            "messages": [ToolMessage(response["messages"][-1].content, tool_call_id=runtime.tool_call_id)],
        })

    main_agent = create_agent(
        model=MODEL,
        state_schema=JobApplicationAgentState,
        tools=[call_job_search_agent, call_resume_handler_agent, call_apply_jobs_agent],
        system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
        store=store,
    )

    return {
        "main_agent": main_agent,
    }