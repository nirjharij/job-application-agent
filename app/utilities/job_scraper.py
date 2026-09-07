import csv
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from utilities.jobs_db import is_job_applied

BASE_URL = "https://www.stepstone.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def _get(url: str, timeout: int = 15) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def get_job_links(search_url: str, max_pages: int = 1, limit: int | None = None) -> list[str]:
    """Collect absolute job detail URLs from one or more search result pages."""
    links: list[str] = []
    for page in range(1, max_pages + 1):
        paged_url = search_url if page == 1 else f"{search_url}&page={page}"
        soup = BeautifulSoup(_get(paged_url).text, "html.parser")
        job_items = soup.find_all(attrs={"data-at": "job-item"})
        if not job_items:
            break
        for item in job_items:
            title_el = item.find(attrs={"data-at": "job-item-title"})
            if title_el and title_el.get("href"):
                links.append(urljoin(BASE_URL, title_el["href"]))
            if limit is not None and len(links) >= limit:
                return links
        time.sleep(1)
    return links


def parse_job_posting(url: str) -> dict:
    """Fetch a job detail page and extract structured JobPosting data."""
    soup = BeautifulSoup(_get(url).text, "html.parser")
    script = soup.find("script", type="application/ld+json", string=re.compile("JobPosting"))
    if not script:
        return {}

    data = json.loads(script.string)
    address = data.get("jobLocation", {}).get("address", {})
    salary = data.get("baseSalary", {}).get("value", {}) if data.get("baseSalary") else {}
    description_html = data.get("description", "")
    description_text = BeautifulSoup(description_html, "html.parser").get_text(
        separator="\n", strip=True
    )

    return {
        "title": data.get("title"),
        "company": data.get("hiringOrganization", {}).get("name"),
        "location": ", ".join(
            filter(None, [address.get("addressLocality"), address.get("addressRegion")])
        ),
        "employment_type": data.get("employmentType"),
        "date_posted": data.get("datePosted"),
        "salary_min": salary.get("minValue"),
        "salary_max": salary.get("maxValue"),
        "salary_currency": data.get("baseSalary", {}).get("currency"),
        "description": description_text,
        "url": url,
    }


def scrape_jobs_to_csv(search_url: str, output_csv: str, max_pages: int = 1, limit: int | None = None) -> None:
    links = get_job_links(search_url, max_pages=max_pages, limit=limit)

    fieldnames = [
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

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, link in enumerate(links, start=1):
            if is_job_applied(link):
                continue
            try:
                job = parse_job_posting(link)
                if job:
                    writer.writerow(job)
            except requests.RequestException:
                pass
            time.sleep(1)
