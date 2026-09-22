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


def _validate_prompt(value: str, max_len: int = 1000) -> str | None:
    value = (value or "").strip()
    if not value:
        return "Please describe what kind of job you're looking for."
    if len(value) > max_len:
        return f"Please keep your description to at most {max_len} characters."
    return None
