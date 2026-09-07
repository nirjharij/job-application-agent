import csv
import json

from langchain_mcp_adapters.client import MultiServerMCPClient

from utilities.jobs_db import is_job_applied

LINKEDIN_MCP_CONFIG = {
    "mcp-server-linkedin": {
        "transport": "stdio",
        "command": "uv",
        "args": [
            "run",
            "--directory",
            "/Users/nirjharijankar/projects/linkedin-mcp-server-pr727",
            "-m",
            "linkedin_mcp_server",
        ],
        "env": {"UV_HTTP_TIMEOUT": "300"},
    }
}

CSV_FIELDNAMES = [
    "title",
    "company",
    "location",
    "employment_type",
    "date_posted",
    "salary_min",
    "salary_max",
    "salary_currency",
    "description",
    "url",
]

class LinkedInJobScraper:
    def __init__(self):
        self.client = MultiServerMCPClient(LINKEDIN_MCP_CONFIG)
        self.tools = None

    async def _get_tools(self):
        """Fetch tools from the MCP server on first use only, so every scrape after the first reuses
        the same client/session instead of spinning up a new mcp-server-linkedin subprocess."""
        if self.tools is None:
            self.tools = await self.client.get_tools(server_name="mcp-server-linkedin")
        return self.tools

    async def scrape_linkedin_jobs_to_csv(self, role: str, location: str, output_csv: str, limit: int = 3) -> None:
        """Search LinkedIn for jobs (via the authenticated mcp-server-linkedin MCP server) and save to csv.

        Requires a logged-in LinkedIn session — run `uvx mcp-server-linkedin@latest --login` once beforehand.
        """

        tools_by_name = {t.name: t for t in await self._get_tools()}
        search_jobs = tools_by_name.get("search_jobs")
        get_job_details = tools_by_name.get("get_job_details")

        search_result = self._extract_json(
            await search_jobs.ainvoke({"keywords": role, "location": location, "max_pages": 1})
        )
        print("Search Result: " + str(search_result))
        job_ids = search_result.get("job_ids", [])[:limit]
        titles_by_id = {
            ref["url"].strip("/").split("/")[-1]: ref["text"]
            for ref in search_result.get("references", {}).get("search_results", [])
            if ref.get("kind") == "job"
        }

        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            for job_id in job_ids:
                # search_jobs' ref["url"] is a relative path (e.g. "/jobs/view/123/"), not the absolute
                # url get_job_details/the csv/jobs_applied use — build the canonical absolute form
                # directly from job_id instead, so the dedup check actually matches applied jobs.
                fallback_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
                if is_job_applied(fallback_url):
                    continue

                try:
                    details = self._extract_json(await get_job_details.ainvoke({"job_id": job_id}))
                except Exception:
                    continue

                raw_text = details.get("sections", {}).get("job_posting", "")
                company, parsed_location, description = self._parse_job_posting_text(raw_text)

                writer.writerow({
                    "title": titles_by_id.get(job_id, ""),
                    "company": company,
                    "location": parsed_location or location,
                    "employment_type": "",
                    "date_posted": "",
                    "salary_min": "",
                    "salary_max": "",
                    "salary_currency": "",
                    "description": description,
                    "url": details.get("url", fallback_url),
                })

    def _extract_json(self, mcp_result) -> dict:
        """MCP tool results come back as a list of content blocks; the payload is JSON text in the first one."""
        return json.loads(mcp_result[0]["text"])


    def _parse_job_posting_text(self, text: str) -> tuple[str, str, str]:
        """Best-effort split of LinkedIn's unstructured job posting text into (company, location, description).

        LinkedIn exposes no structured job schema (unlike StepStone's JobPosting JSON-LD), so this relies on
        the page's typical text layout: company name on the first line, then title, then a
        "location · posted-time · applicants" line, then the full description after "About the job".
        """
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        company = lines[0] if lines else ""

        location = ""
        for line in lines[:6]:
            if " · " in line:
                location = line.split(" · ")[0].strip()
                break

        description = text.split("About the job", 1)[1].strip() if "About the job" in text else text
        return company, location, description


    def linkedin_tool_messages_to_csv(self, messages, output_csv: str, location: str = "") -> int:
        """Consolidate raw search_jobs/get_job_details ToolMessage results already present in the \
    conversation into a jobs csv, matching the same schema as the StepStone scraper.

        Use this when the LLM called LinkedIn's MCP tools directly (via dynamic tool selection) \
    instead of going through scrape_linkedin_jobs_to_csv. Returns the number of rows written.
        """
        titles_by_id: dict[str, str] = {}
        job_details: dict[str, dict] = {}

        for message in messages:
            name = getattr(message, "name", None)
            if name == "search_jobs" and isinstance(message.content, list):
                try:
                    search_result = self._extract_json(message.content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                for ref in search_result.get("references", {}).get("search_results", []):
                    if ref.get("kind") == "job":
                        job_id = ref["url"].strip("/").split("/")[-1]
                        titles_by_id[job_id] = ref["text"]

            elif name == "get_job_details" and isinstance(message.content, list):
                try:
                    details = self._extract_json(message.content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                url = details.get("url", "")
                job_id = url.strip("/").split("/")[-1] if url else None
                if job_id:
                    job_details[job_id] = details

        rows = []
        for job_id, details in job_details.items():
            url = details.get("url", f"https://www.linkedin.com/jobs/view/{job_id}/")
            if is_job_applied(url):
                continue
            raw_text = details.get("sections", {}).get("job_posting", "")
            company, parsed_location, description = self._parse_job_posting_text(raw_text)
            rows.append({
                "title": titles_by_id.get(job_id, ""),
                "company": company,
                "location": parsed_location or location,
                "employment_type": "",
                "date_posted": "",
                "salary_min": "",
                "salary_max": "",
                "salary_currency": "",
                "description": description,
                "url": url,
            })

        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

        return len(rows)


# Single shared instance so every scrape reuses the same MCP client/session instead of spawning a
# new mcp-server-linkedin subprocess per call — see LinkedInJobScraper._get_tools.
_scraper = LinkedInJobScraper()


async def scrape_linkedin_jobs_to_csv(role: str, location: str, output_csv: str, limit: int = 3) -> None:
    await _scraper.scrape_linkedin_jobs_to_csv(role, location, output_csv, limit=limit)
