import csv
import json
import logging

from utilities.jobs_db import is_job_applied
from utilities.mcp_tools import MCP_TOOL_MAX_ATTEMPTS, _call_mcp_tool, get_linkedin_mcp_tools
from utilities.linkedin.custom_exception import LinkedInSearchEmptyError

logger = logging.getLogger(__name__)

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
    async def scrape_linkedin_jobs_to_csv(self, role: str, location: str, output_csv: str, limit: int = 3) -> None:
        """Search LinkedIn for jobs (via the authenticated mcp-server-linkedin MCP server) and save to csv.

        Requires a logged-in LinkedIn session — run `uvx mcp-server-linkedin@latest --login` once beforehand.
        """

        tools_by_name = {t.name: t for t in await get_linkedin_mcp_tools()}
        search_jobs = tools_by_name.get("search_jobs")
        get_job_details = tools_by_name.get("get_job_details")

        try:
            search_result = self._extract_json(
                await _call_mcp_tool(search_jobs, {"keywords": role, "location": location, "max_pages": 1})
            )
        except Exception as exc:
            logger.error(
                "LinkedIn search_jobs MCP call failed after %d attempts: role=%r location=%r error=%s",
                MCP_TOOL_MAX_ATTEMPTS, role, location, exc,
            )
            raise LinkedInSearchEmptyError(
                f"LinkedIn search for role={role!r} location={location!r} failed: {exc}"
            ) from exc
        logger.info("Jobs Found for role=%s & location=%s are: %s", role, location, str(search_result))

        job_ids = search_result.get("job_ids", [])

        logger.info("Job Ids for role=%s & location=%s are: %s", role, location, str(job_ids))

        if job_ids == []:
            reason = "search_jobs returned no job_ids"
            logger.error("LinkedIn search failed: role=%r location=%r reason=%s", role, location, reason)
            raise LinkedInSearchEmptyError(
                f"LinkedIn search for role={role!r} location={location!r} returned no jobs ({reason})."
            )

        job_ids = job_ids[:limit]

        try:
            titles_by_id = {
                ref["url"].strip("/").split("/")[-1]: ref["text"]
                for ref in search_result.get("references", {}).get("search_results", [])
                if ref.get("kind") == "job"
            }
        except KeyError as e:
            logger.exception("During parsing Job search results, could not find key: %s", e)
            titles_by_id = {}

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
                    details = self._extract_json(
                        await _call_mcp_tool(get_job_details, {"job_id": job_id})
                    )
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
        """ MCP tool results come back as a list of content blocks; the payload is JSON text in the first one."""
        return json.loads(mcp_result[0]["text"])

    def _parse_job_posting_text(self, text: str) -> tuple[str, str, str]:
        """ Best-effort split of LinkedIn's unstructured job posting text into (company, location, description).

        LinkedIn exposes no structured job schema, so this relies on the page's typical text layout: \
        company name on the first line, then title, then a
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
        """ Consolidate raw search_jobs/get_job_details ToolMessage results already present in the \
            conversation into a jobs csv, matching the standard jobs csv schema.

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
# new mcp-server-linkedin subprocess per call — see get_linkedin_mcp_tools.
_scraper = LinkedInJobScraper()


async def scrape_linkedin_jobs_to_csv(role: str, location: str, output_csv: str, limit: int = 3) -> None:
    await _scraper.scrape_linkedin_jobs_to_csv(role, location, output_csv, limit=limit)
