import csv

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
