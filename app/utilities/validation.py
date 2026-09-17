import re
from urllib.parse import urlparse

# Unicode letters/digits/spaces plus common punctuation found in job titles/locations
# (e.g. "Berlin, Germany", "R&D", "Software Engineer (m/w/d)", "O'Fallon").
_JOB_TEXT_ALLOWED = re.compile(r"^[\w\s\-,.&/'()]+$", re.UNICODE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_RESUME_SIZE_BYTES = 8 * 1024 * 1024  # 8 MB — generous for a text resume, bounds base64/LLM-state blowup


def _validate_resume(resume_file) -> str | None:
    if resume_file is None:
        return "Please upload a resume PDF."
    if resume_file.size > MAX_RESUME_SIZE_BYTES:
        return f"Resume file is too large (max {MAX_RESUME_SIZE_BYTES // (1024 * 1024)} MB)."
    header = resume_file.read(5)
    resume_file.seek(0)
    if header != b"%PDF-":
        return "That file doesn't look like a valid PDF."
    return None


def _validate_job_text(value: str, label: str, max_len: int = 100) -> str | None:
    value = (value or "").strip()
    if not value:
        return f"{label} is required."
    if len(value) > max_len:
        return f"{label} must be at most {max_len} characters."
    if not _JOB_TEXT_ALLOWED.match(value):
        return f"{label} contains characters that aren't allowed."
    return None


def _validate_name(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return "Full name is required."
    if len(value) > 100:
        return "Full name must be at most 100 characters."
    return None


def _validate_email(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return "Email is required."
    if not _EMAIL_RE.match(value):
        return "Please enter a valid email address."
    return None


def _validate_linkedin_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    parts = urlparse(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "LinkedIn URL must be a valid http(s) URL."
    return None


def _validate_optional_text(value: str, max_len: int = 200) -> str | None:
    value = (value or "").strip()
    if len(value) > max_len:
        return f"Must be at most {max_len} characters."
    return None
