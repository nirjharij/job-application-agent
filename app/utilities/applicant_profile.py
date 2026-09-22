import hashlib
import json
import logging

import psycopg
from langchain.messages import HumanMessage
from pydantic import BaseModel, Field

from config import get_llm
from utilities.jobs_db import _connect

logger = logging.getLogger(__name__)


class ApplicantProfile(BaseModel):
    name: str = Field(default="", description="Full name")
    email: str = Field(default="", description="Email address")
    phone: str = Field(default="", description="Phone number")
    university: str = Field(default="", description="University or degree-granting institution")
    experience_summary: str = Field(
        default="", description="Brief summary of work experience: roles, companies, years"
    )
    linkedin_url: str = Field(default="", description="LinkedIn profile URL, if present")
    location: str = Field(default="", description="City/location, if present")


def _resume_hash(pdf_base64: str) -> str:
    """Deterministic id for a resume's content, so the same resume always maps to the same
    cached profile row."""
    return hashlib.sha256(pdf_base64.encode("utf-8")).hexdigest()


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applicant_profiles (
            resume_hash TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def get_cached_applicant_profile(pdf_base64: str) -> dict | None:
    """Look up a previously-extracted applicant profile for this exact resume, by content hash.

    Fails open (returns None, i.e. "not cached") on any database error, so a broken Postgres
    instance forces re-extraction rather than blocking the whole apply run."""
    try:
        with _connect() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT profile FROM applicant_profiles WHERE resume_hash = %s",
                (_resume_hash(pdf_base64),),
            ).fetchone()
            return json.loads(row[0]) if row else None
    except psycopg.Error:
        logger.exception("Jobs database error while reading cached applicant profile")
        return None


def save_applicant_profile(pdf_base64: str, profile: dict) -> None:
    """Cache an extracted applicant profile, keyed by the resume's content hash."""
    try:
        with _connect() as conn:
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO applicant_profiles (resume_hash, profile) VALUES (%s, %s)
                ON CONFLICT (resume_hash) DO UPDATE SET profile = EXCLUDED.profile
                """,
                (_resume_hash(pdf_base64), json.dumps(profile)),
            )
            conn.commit()
    except psycopg.Error:
        logger.exception("Jobs database error while saving applicant profile")


async def _extract_applicant_profile(pdf_base64: str) -> dict:
    """Ask the LLM to read the resume once and pull out the applicant's contact/background
    details, via structured output rather than hand-parsed prose."""
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "Extract the applicant's contact and background details from the attached "
                    "resume. Leave any field blank if it isn't present — never invent information."
                ),
            },
            {
                "type": "file",
                "mime_type": "application/pdf",
                "base64": pdf_base64,
            },
        ]
    )
    llm = get_llm().with_structured_output(ApplicantProfile)
    result = await llm.ainvoke([message])
    return result.model_dump()


async def get_or_extract_applicant_profile(pdf_base64: str) -> dict:
    """Return this resume's applicant profile — from Postgres if we've already extracted it for
    this exact resume before, else by asking the LLM to read the resume once and caching the
    result for every future call with the same resume."""
    cached = get_cached_applicant_profile(pdf_base64)
    if cached is not None:
        return cached

    try:
        profile = await _extract_applicant_profile(pdf_base64)
    except Exception:
        # Not cached on failure — a later retry (e.g. next apply run) should get another shot at
        # extraction rather than being permanently stuck with an empty profile.
        logger.exception("Failed to extract applicant profile from resume")
        return {}

    save_applicant_profile(pdf_base64, profile)
    return profile
