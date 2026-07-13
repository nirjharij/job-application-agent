import asyncio
import csv
import os

from dotenv import load_dotenv
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langchain.messages import HumanMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from job_scraper import scrape_jobs_to_csv
from linkedin_scraper import scrape_linkedin_jobs_to_csv

load_dotenv()

MODEL = "gpt-5-nano"


# --- State (ported as-is; already includes the `platform` field fix) ---

class JobApplicationAgentState(AgentState):
    pathToJobsCsv: str
    platform: str


class JobSearchAgentState(AgentState):
    pathToJobsCsv: str
    platform: str


class ResumeHandlerAgentState(AgentState):
    pathToJobsCsv: str
    pdfBase64: str
    tailoredResumeFiles: list


# --- Job search tools (ported; scrape_jobs_to_csv call adapted from max_jobs= to app's limit=) ---

@tool
async def job_finder(runtime: ToolRuntime, platform: str, role: str, city: str, radius: int = 30) -> str:
    """Find jobs based on platform the user wants to search jobs on and create a csv file with list of jobs to apply to"""
    csv_filename = "job_details.csv"
    csv_path = os.path.join(os.getcwd(), csv_filename)

    if platform.lower() == "stepstone":
        scrape_jobs_to_csv(
            f"https://www.stepstone.de/work/{role.strip()}/in-{city.lower()}?radius={radius}",
            csv_filename,
            limit=3,  # app/job_scraper.py's scrape_jobs_to_csv takes `limit`, not `max_jobs`
        )
    else:
        await scrape_linkedin_jobs_to_csv(role, city, csv_path, limit=3)

    return Command(update={
        "pathToJobsCsv": csv_path,
        "messages": [ToolMessage(
            f"Successfully updated csv file at path: {csv_path}. Next step is to display all the jobs",
            tool_call_id=runtime.tool_call_id,
        )]})


@tool
def display_jobs(runtime: ToolRuntime) -> str:
    """ Display the title, company, and location of every job found so far """
    csv_path = runtime.state.get("pathToJobsCsv")
    if not csv_path:
        return "No CSV file path key found"

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    columns = ["title", "company", "location"]
    widths = {col: max(len(col), *(len(row[col]) for row in rows)) for col in columns}

    def format_row(values):
        return " | ".join(str(v).ljust(widths[col]) for col, v in zip(columns, values))

    lines = [format_row(columns), "-+-".join("-" * widths[col] for col in columns)]
    lines.extend(format_row([row[col] for col in columns]) for row in rows)

    return "\n".join(lines)


@tool
def apply_jobs(runtime: ToolRuntime) -> str:
    """ Apply to jobs listed in csv post confirmation on all the jobs from human"""
    return ToolMessage("Applied to all the jobs", tool_call_id=runtime.tool_call_id)


# --- Resume tools ---
# NOTE (adapted per confirmed decision): `apply` defaults to False, not True — resume_corrections_and_download
# is gated by HumanInTheLoopMiddleware (see resume_handler_agent below), pausing before it runs at all. While
# paused, the Streamlit UI lets the user flip each row's `apply` True/False directly in the csv (no agent
# involvement); a single "Continue" action then always resumes with {"type": "approve"} regardless of what
# was picked — the interrupt exists so the human can review suggestions before generation, not to gate
# individual rows through the HITL decision itself. resume_corrections_and_download then processes whichever
# rows are apply=True at that point, exactly as originally written.

@tool
async def analyze_resume_and_make_suggestions(runtime: ToolRuntime) -> str:
    """Fetch job description and add changes to resume(passed as pdf) by tailoring it to each job description and save it to csv for human approval"""
    csv_path = runtime.state.get("pathToJobsCsv")
    pdf_base64 = runtime.state.get("pdfBase64")
    if not csv_path or not pdf_base64:
        return "No CSV file path key found"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames + ["resume_corrections"] + ["apply"]

    llm = init_chat_model(MODEL)

    async def get_suggestions(row):
        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        "You are a resume coach. Compare the attached resume to the job descriptions "
                        "and suggest specific, concise corrections or additions to better match the job. "
                        "Match their language, mirror their priorities, and keep it to one page. "
                        "Don’t make anything up — only use what’s already in my resume, just position it better."
                        "Return only a short bulleted list of suggestions.\n\n"
                        f"Job Description:\n{row['description']}\n"
                    ),
                },
                {
                    "type": "file",
                    "mime_type": "application/pdf",
                    "base64": pdf_base64,
                },
            ]
        )
        response = await llm.ainvoke([message])
        row["resume_corrections"] = response.content.replace("\n", " | ")
        row["apply"] = False

    await asyncio.gather(*(get_suggestions(row) for row in rows))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return ToolMessage(
        f"Tailored resume suggestions for all jobs and saved to csv {csv_path}. "
        f"Next step: wait for the user to approve individual jobs before generating tailored files.",
        tool_call_id=runtime.tool_call_id,
    )


async def generate_tailored_resume_for_row(pdf_base64: str, row: dict) -> str:
    """Generate one tailored resume file for a single job row (never overwrites the original resume).

    Extracted from resume_corrections_and_download so it can be called for a single approved job
    (from the Streamlit Approve button) as well as in a full-csv batch (from the tool below).
    """
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "Rewrite the attached resume, applying the suggested corrections below. "
                    "Do not invent new experience or skills — only reorganize, rephrase, and "
                    "emphasize what is already in the original resume, per the suggestions. "
                    "Output the complete rewritten resume as plain text, ready to save as a file. "
                    "Do not include any commentary before or after the resume text.\n\n"
                    f"Suggested changes:\n{row['resume_corrections']}\n"
                ),
            },
            {
                "type": "file",
                "mime_type": "application/pdf",
                "base64": pdf_base64,
            },
        ]
    )
    llm = init_chat_model(MODEL)
    response = await llm.ainvoke([message])

    filename = f"{row['company']}_{row['title']}_tailored_resume.txt".replace(" ", "_").replace("/", "-")
    filepath = os.path.join(os.getcwd(), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(response.content)
    return filepath


@tool
async def resume_corrections_and_download(runtime: ToolRuntime) -> str:
    """Apply the suggested resume corrections and save each tailored resume as a new file \
        (never overwriting the original), one per job marked apply=True in the jobs csv."""
    csv_path = runtime.state.get("pathToJobsCsv")
    pdf_base64 = runtime.state.get("pdfBase64")
    if not csv_path or not pdf_base64:
        return "No CSV file path key found"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames if "tailored_resume_path" in reader.fieldnames else [*reader.fieldnames, "tailored_resume_path"]

    saved_files = []

    async def apply_suggestions(row):
        row.setdefault("tailored_resume_path", "")
        if row.get("apply", "").strip().lower() != "true":
            return
        filepath = await generate_tailored_resume_for_row(pdf_base64, row)
        row["tailored_resume_path"] = filepath
        saved_files.append(filepath)

    await asyncio.gather(*(apply_suggestions(row) for row in rows))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return Command(update={
        "tailoredResumeFiles": saved_files,
        "messages": [ToolMessage(
            f"Saved {len(saved_files)} tailored resume file(s): {', '.join(saved_files)}.",
            tool_call_id=runtime.tool_call_id,
        )],
    })


# --- System prompts ---

MAIN_AGENT_SYSTEM_PROMPT = """You are a job search assistant. You do not search for jobs yourself — you \
delegate to a specialist subagent and relay its results to the user.

## Subagents

- `call_job_search_agent`: delegates to a subagent that searches for jobs (on whichever platform the user \
selected — this is handled automatically, you don't need to specify it) and can apply to jobs once approved. \
Use this whenever the user wants to find or apply to jobs.

## Rules

1. Call `call_job_search_agent` when the user wants jobs found.
2. Never fabricate job details yourself. Only relay what the subagent actually returned.
3. Summarize concrete results (job titles/companies) rather than a generic confirmation.

Note: resume analysis and tailoring are handled by a separate resume_handler_agent, invoked directly by \
the application (not through you) once jobs have been found.
"""

JOB_SEARCH_AGENT_SYSTEM_PROMPT = """You search for jobs on the platform, role, and location you're given, \
and can apply to jobs once approved.

## Tools

- `job_finder`: search for jobs and save the results to a csv. This works for both StepStone and LinkedIn \
— pass the exact platform you were given and it handles everything internally (scraping StepStone \
directly for "stepstone", or using LinkedIn's own API under the hood for "linkedin"). This one call is \
the entire search step for either platform; there is nothing else you need to do to search.
- `display_jobs`: show the jobs found so far.
- `apply_jobs`: apply to the jobs marked for application in the jobs csv. Call it directly when asked to \
apply — the system automatically pauses for human approval the moment you call it. Do not ask for \
confirmation in chat first; that skips the approval mechanism entirely.

## Rules

1. Always call `job_finder` first, then `display_jobs`, each as its own step.
2. Pass the exact platform you were given to `job_finder` — do not guess or default it, and do not alter \
its casing or spelling.
3. Never fabricate job listings or details — only report what `job_finder`/`display_jobs` actually returned.
"""

RESUME_HANDLER_AGENT_SYSTEM_PROMPT = """You analyze a resume against job descriptions and produce tailored \
versions, without ever modifying the original resume.

## Tools

- `analyze_resume_and_make_suggestions`: compare the resume to each job description found so far and save \
suggested corrections to the jobs csv.
- `resume_corrections_and_download`: apply the saved suggestions to a *new* file per job marked \
apply=True in the csv — never overwrite the original resume.

## Rules

1. Call `analyze_resume_and_make_suggestions` first, then `resume_corrections_and_download`, each as its \
own step, without pausing to ask if you should proceed.
2. Call `resume_corrections_and_download` directly — the system automatically pauses for human review the \
moment you call it. Do not ask for confirmation in chat first; that skips the review mechanism entirely.
3. Only apply suggestions to jobs explicitly marked apply=true in the csv — never assume a job should be \
included.
4. Never invent resume content — only reorganize and rephrase what's already in the original resume, per \
the generated suggestions.
"""


async def build_agent():
    """Build the full 3-agent system: main_agent delegates to job_search_agent and resume_handler_agent."""

    job_search_agent = create_agent(
        model=MODEL,
        system_prompt=JOB_SEARCH_AGENT_SYSTEM_PROMPT,
        tools=[job_finder, display_jobs, apply_jobs],
        state_schema=JobSearchAgentState,
        checkpointer=InMemorySaver(),
    )

    resume_handler_agent = create_agent(
        model=MODEL,
        system_prompt=RESUME_HANDLER_AGENT_SYSTEM_PROMPT,
        tools=[analyze_resume_and_make_suggestions, resume_corrections_and_download],
        state_schema=ResumeHandlerAgentState,
        checkpointer=InMemorySaver(),
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"resume_corrections_and_download": True}
            ),
        ],
    )

    @tool
    async def call_job_search_agent(role: str, city: str, runtime: ToolRuntime) -> str:
        """Call the job search subagent to find jobs and optionally apply to them."""
        # platform intentionally comes from state (set by the UI), not a model-supplied argument —
        # relying on the LLM to correctly re-extract "linkedin"/"stepstone" from prose was the root
        # cause of job_search_agent silently getting the wrong tools from dynamic_tool_call.
        platform = runtime.state.get("platform") or "linkedin"
        config = {"configurable": {"thread_id": f"{runtime.config['configurable']['thread_id']}-job-search"}}
        response = await job_search_agent.ainvoke(
            {"messages": [HumanMessage(content=f"Find job for {role} in {city} on {platform}")]},
            config=config,
        )
        return Command(update={
            "pathToJobsCsv": response.get("pathToJobsCsv"),
            "messages": [ToolMessage(response["messages"][-1].content, tool_call_id=runtime.tool_call_id)],
        })

    main_agent = create_agent(
        model=MODEL,
        state_schema=JobApplicationAgentState,
        tools=[call_job_search_agent],
        system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )

    # resume_handler_agent is invoked directly by the Streamlit app (not through main_agent) so that its
    # HumanInTheLoopMiddleware interrupt on resume_corrections_and_download surfaces straight to the UI,
    # instead of needing to propagate through a second, nested agent-as-tool boundary.
    return {"main_agent": main_agent, "resume_handler_agent": resume_handler_agent}
