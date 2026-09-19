class StepStoneJobParsingError(RuntimeError):
    """Raised when LinkedIn's search_jobs MCP tool returns no job_ids at all for a search
    (as opposed to a search that found jobs but all of them were already applied to)."""
