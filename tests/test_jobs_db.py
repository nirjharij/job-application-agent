import uuid
from unittest.mock import MagicMock

import pytest

from utilities.jobs_db import _normalize_url, is_job_applied, job_id_from_url, mark_job_applied
from utilities import jobs_db

BASE = "https://www.linkedin.com/jobs/view/123"


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{BASE}/", BASE),
        (f"{BASE}?trk=public_jobs", BASE),
        (f"{BASE}/?trk=public_jobs&refId=x", BASE),
        (f"{BASE}#applicant-info", BASE),
        (f"{BASE}/?a=1#frag", BASE),
        (BASE, BASE),
    ],
)
def test_normalize_url_strips_query_fragment_and_trailing_slash(url, expected):
    assert _normalize_url(url) == expected


def test_normalize_url_strips_every_trailing_slash():
    # rstrip("/") is greedy, so a bare host collapses to an empty path rather than "/".
    assert _normalize_url("https://example.com///") == "https://example.com"


def test_job_id_is_deterministic():
    assert job_id_from_url(BASE) == job_id_from_url(BASE)


def test_tracking_params_and_trailing_slash_map_to_one_id():
    ids = {
        job_id_from_url(f"{BASE}/"),
        job_id_from_url(BASE),
        job_id_from_url(f"{BASE}/?trk=abc"),
        job_id_from_url(f"{BASE}#section"),
    }
    assert len(ids) == 1


def test_job_id_matches_uuid5_of_the_normalized_url():
    # Pinned so a change to _normalize_url is caught instead of silently re-hashing every job
    # already recorded in jobs_applied.
    assert job_id_from_url(f"{BASE}/?trk=abc") == str(uuid.uuid5(uuid.NAMESPACE_URL, BASE))


def test_different_jobs_get_different_ids():
    assert job_id_from_url(BASE) != job_id_from_url("https://www.linkedin.com/jobs/view/124")


@pytest.mark.parametrize(
    "other",
    [
        "http://www.linkedin.com/jobs/view/123",  # scheme
        "https://linkedin.com/jobs/view/123",  # www. prefix
        "https://www.LinkedIn.com/jobs/view/123",  # host case
    ],
)
def test_only_query_and_fragment_are_normalized(other):
    # Documents what is deliberately *not* collapsed — these are distinct ids today.
    assert job_id_from_url(other) != job_id_from_url(BASE)


def _fake_connect(monkeypatch, fetchone_result=None):
    """psycopg.connect is used as a `with` block, so the mock needs __enter__/__exit__."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = fetchone_result
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    monkeypatch.setattr(jobs_db.psycopg, "connect", MagicMock(return_value=ctx))
    return conn


def test_is_job_applied_true_when_a_row_comes_back(monkeypatch):
    _fake_connect(monkeypatch, fetchone_result=(1,))
    assert is_job_applied(BASE) is True


def test_is_job_applied_false_when_no_row(monkeypatch):
    _fake_connect(monkeypatch, fetchone_result=None)
    assert is_job_applied(BASE) is False


def test_is_job_applied_queries_by_derived_id_not_raw_url(monkeypatch):
    conn = _fake_connect(monkeypatch, fetchone_result=None)
    is_job_applied(f"{BASE}/?trk=abc")

    sql, params = conn.execute.call_args_list[-1].args
    assert "SELECT 1 FROM jobs_applied WHERE id" in sql
    assert params == (job_id_from_url(BASE),)


def test_mark_job_applied_upserts_and_commits(monkeypatch):
    conn = _fake_connect(monkeypatch)
    mark_job_applied(BASE)

    sql, params = conn.execute.call_args_list[-1].args
    assert "INSERT INTO jobs_applied" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql
    # The id is normalized, but the raw url is stored verbatim for readability.
    assert params == (job_id_from_url(BASE), BASE)
    conn.commit.assert_called_once()


def test_both_helpers_create_the_table_first(monkeypatch):
    conn = _fake_connect(monkeypatch, fetchone_result=None)
    is_job_applied(BASE)
    assert "CREATE TABLE IF NOT EXISTS jobs_applied" in conn.execute.call_args_list[0].args[0]
