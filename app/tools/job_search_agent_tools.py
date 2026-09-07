import os

from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from utilities.job_scraper import scrape_jobs_to_csv
from utilities.linkedin_scraper import scrape_linkedin_jobs_to_csv


@tool
async def job_finder(runtime: ToolRuntime, platform: str, role: str, city: str, radius: int = 30) -> str:
    """Find jobs based on platform the user wants to search jobs on and create a csv file with list of jobs to apply to"""
    csv_filename = "job_details.csv"
    csv_path = os.path.join(os.getcwd(), csv_filename)
    limit = runtime.state.get("numJobs") or 3

    if platform.lower() == "stepstone":
        scrape_jobs_to_csv(
            f"https://www.stepstone.de/work/{role.strip()}/in-{city.lower()}?radius={radius}",
            csv_filename,
            limit=limit,  # utilities/job_scraper.py's scrape_jobs_to_csv takes `limit`, not `max_jobs`
        )
    else:
        await scrape_linkedin_jobs_to_csv(role, city, csv_path, limit=limit)

    return Command(update={
        "pathToJobsCsv": csv_path,
        "messages": [ToolMessage(
            f"Successfully updated csv file at path: {csv_path}. Next step is to analyze resume",
            tool_call_id=runtime.tool_call_id,
        )]})
