import csv
import os

from utilities.jobs_db import mark_job_applied

JOB_APPLICATION_STATUS_APPLYING = "applying"
JOB_APPLICATION_STATUS_APPLIED = "applied"


def read_job_rows(csv_path: str) -> list[dict]:
    """Read every row of the jobs csv as plain dicts."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def update_job_row(csv_path: str, job_url: str, **updates) -> None:
    """Persist arbitrary per-job fields (apply, tailored_resume_path, job_application_status, ...)
    back into the jobs csv, adding new columns as needed."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames)
        for key in updates:
            if key not in fieldnames:
                fieldnames.append(key)

    for row in rows:
        for key in updates:
            row.setdefault(key, "")
        if row["url"] == job_url:
            row.update(updates)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def recover_and_reset_jobs_csv(csv_path: str) -> None:
    """One-time startup recovery: a previous process run may have crashed while a job's browser
    fill was in progress, leaving its row's job_application_status stuck at "applying" even though
    Postgres never heard about it — finalize any such leftovers there, then remove the stale csv
    entirely. Every search overwrites this same fixed path from scratch (never appends to it), so
    there is nothing in it worth keeping once its jobs are accounted for.

    Call this once per process start (e.g. from build_agent()), not on every new session — the csv
    only ever reflects the single most recent search anyway."""
    if not os.path.exists(csv_path):
        return
    for row in read_job_rows(csv_path):
        if row.get("job_application_status") == JOB_APPLICATION_STATUS_APPLYING:
            mark_job_applied(row["url"])
    os.remove(csv_path)
