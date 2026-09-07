import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg

DB_URI = os.environ.get(
    "JOBS_DB_URI", "postgresql://postgres:postgres@localhost:5432/postgres?sslmode=disable"
)


def _normalize_url(url: str) -> str:
    """Strip query string, fragment, and trailing slash so tracking params (e.g. LinkedIn's
    search-result urls carry a `?trk=...` that a later search for the same job may omit or vary)
    don't make the same job hash to a different id."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def job_id_from_url(url: str) -> str:
    """Deterministic UUID for a job url, so the same url always maps to the same id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _normalize_url(url)))


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs_applied (
            id UUID PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def is_job_applied(url: str) -> bool:
    """Check whether a job has already been applied to, by its url."""
    with psycopg.connect(DB_URI) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT 1 FROM jobs_applied WHERE id = %s", (job_id_from_url(url),)
        ).fetchone()
        return row is not None


def mark_job_applied(url: str) -> None:
    """Record that a job has been applied to, keyed by a uuid derived from its url."""
    with psycopg.connect(DB_URI) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO jobs_applied (id, url) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (job_id_from_url(url), url),
        )
        conn.commit()
