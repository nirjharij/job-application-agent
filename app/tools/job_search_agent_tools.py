import logging
import os

from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command
from config import OUTPUT_DIRECTORY, CSV_FILENAME
from utilities.stepstone.stepstone_scraper import scrape_jobs_to_csv
from utilities.linkedin.linkedin_scraper import LinkedInSearchEmptyError, scrape_linkedin_jobs_to_csv
from utilities.mcp_tools import MCPConnectionError
from utilities.stepstone.custom_exception import StepStoneJobParsingError

logger = logging.getLogger(__name__)

@tool
async def job_finder(runtime: ToolRuntime, platform: str, role: str, city: str, radius: int = 30) -> str:
    """Find jobs based on platform the user wants to search jobs on and create a csv file with list of jobs to apply to"""

    csv_path = os.path.join(OUTPUT_DIRECTORY, CSV_FILENAME)
    limit = runtime.state.get("numJobs") or 3

    if platform.lower() == "stepstone":
        try:
            scrape_jobs_to_csv(
                f"https://www.stepstone.de/work/{role.strip()}/in-{city.lower()}?radius={radius}",
                csv_path,
                limit=limit,
            )
        except Exception as e:
            logger.exception("Failed to scrape jobs from StepStone for role=%s, location=%s", role, city)
            return Command(update={
                "pathToJobsCsv": None,
                "messages": [ToolMessage(
                    f"StepStone search failed for role={role!r} city={city!r}: {e}",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

    else:
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
            f"Successfully updated csv file at path: {csv_path}. Next step is to analyze resume",
            tool_call_id=runtime.tool_call_id,
        )]})
