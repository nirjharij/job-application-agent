import csv
import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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


def get_job_links(search_url: str, max_jobs: int = 3) -> list[str]:
    """Collect absolute job detail URLs from one or more search result pages."""
    links: list[str] = []
    jobs_count = 0 
    for page in range(1, 11):
        if jobs_count == max_jobs:
                break
        paged_url = search_url if page == 1 else f"{search_url}&page={page}"
        soup = BeautifulSoup(_get(paged_url).text, "html.parser")
        job_items = soup.find_all(attrs={"data-at": "job-item"})
        if not job_items:
            break
        # Adding this for testing with only 2 jobs at the time    
        for item in job_items:
            if jobs_count == max_jobs:
                break
            title_el = item.find(attrs={"data-at": "job-item-title"})
            if title_el and title_el.get("href"):
                links.append(urljoin(BASE_URL, title_el["href"]))
            jobs_count += 1
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


def scrape_jobs_to_csv(search_url: str, output_csv: str, max_jobs: int = 3) -> None:
    links = get_job_links(search_url, max_jobs=max_jobs)
    # print(f"Found {len(links)} job links")

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
            try:
                job = parse_job_posting(link)
                if job:
                    writer.writerow(job)
                    print(f"[{i}/{len(links)}] scraped: {job.get('title')}")
            except requests.RequestException as e:
                print(f"[{i}/{len(links)}] failed: {link} ({e})")
            time.sleep(1)


if __name__ == "__main__":
    SEARCH_URL = "https://www.stepstone.de/work/software-development-engineer/in-berlin?radius=30"
    scrape_jobs_to_csv(SEARCH_URL, "stepstone_jobs.csv", max_jobs=3)
