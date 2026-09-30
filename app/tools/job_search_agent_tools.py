import logging
import os

from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command
from config import OUTPUT_DIRECTORY, CSV_FILENAME
from utilities.linkedin.linkedin_scraper import LinkedInSearchEmptyError, scrape_linkedin_jobs_to_csv
from utilities.mcp_tools import MCPConnectionError

logger = logging.getLogger(__name__)

@tool
async def job_finder(runtime: ToolRuntime, role: str, city: str, num_jobs: int = 3) -> str:
    """Find jobs on LinkedIn for the given role and city, and create a csv file with the list of jobs to apply to"""

    csv_path = os.path.join(OUTPUT_DIRECTORY, CSV_FILENAME)
    limit = max(1, min(int(num_jobs), 10))

    try:
        await scrape_linkedin_jobs_to_csv(role, city, csv_path, limit=limit)
    except (LinkedInSearchEmptyError, MCPConnectionError) as e:
        logger.exception("Failed to scrape job from LinkedIn for role=%s, location=%s", role, city)
        return Command(update={
            "pathToJobsCsv": None,
            "messages": [ToolMessage(f"LinkedIn search failed for role={role!r} location={city!r}: {e}",
                                     tool_call_id=runtime.tool_call_id)],
        })

    return Command(update={
        "pathToJobsCsv": csv_path,
        "messages": [ToolMessage(
            f"Successfully updated csv file at path: {csv_path}.",
            tool_call_id=runtime.tool_call_id,
        )]})
