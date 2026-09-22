import asyncio
import logging

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from config import LINKEDIN_MCP_CONFIG, PLAYWRIGHT_MCP_CONFIG

logger = logging.getLogger(__name__)

class MCPConnectionError(RuntimeError):
    """Raised when establishing an MCP server connection (subprocess launch + handshake) fails —
    e.g. the server command isn't installed, its startup path is wrong, or it crashes before the
    initial handshake completes."""

# Bounds each MCP tool call (stdio subprocess round-trip) so a stalled MCP server process can't
# hang the caller indefinitely.
MCP_TOOL_TIMEOUT_SECONDS = 30
MCP_TOOL_MAX_ATTEMPTS = 3
MCP_TOOL_RETRY_BACKOFF_SECONDS = 2

_linkedin_client = None
_linkedin_tools = None
# needed so that it is not garbage collected
_mcp_session_cm = None
_mcp_session = None

async def _call_mcp_tool(tool, args: dict, *, attempts: int = MCP_TOOL_MAX_ATTEMPTS):
    """Call an MCP tool's ainvoke, retrying with a short linear backoff on timeout or any other
    error (a dropped stdio pipe, a transient upstream 5xx, ...) before giving up. Each attempt is
    itself bounded by MCP_TOOL_TIMEOUT_SECONDS so a hung attempt doesn't just eat the whole budget."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(tool.ainvoke(args), timeout=MCP_TOOL_TIMEOUT_SECONDS)
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "MCP tool %s call failed (attempt %d/%d): %s — retrying",
                tool.name, attempt, attempts, exc,
            )
            await asyncio.sleep(MCP_TOOL_RETRY_BACKOFF_SECONDS * attempt)

async def get_apply_jobs_mcp_tools() -> list:
    """Launch the shared Playwright MCP session (first call only) and return its tools, minus
    browser_close (a full browser-process teardown tool — an accidental call would kill the shared,
    whole-process-lifetime browser the same way an accidental _browser.close() would with raw
    Playwright).
    """
    global _mcp_session_cm, _mcp_session
    if _mcp_session is None:
        try:
            client = MultiServerMCPClient(PLAYWRIGHT_MCP_CONFIG)
            _mcp_session_cm = client.session("playwright")
            _mcp_session = await _mcp_session_cm.__aenter__()
        except Exception as exc:
            # Reset both so the next call starts a fresh client/session instead of getting stuck
            # on a half-initialized one (_mcp_session_cm set but _mcp_session never assigned).
            _mcp_session_cm = None
            _mcp_session = None
            logger.exception("Failed to establish the Playwright MCP session")
            raise MCPConnectionError(f"Could not connect to the Playwright MCP server: {exc}") from exc
    tools = await load_mcp_tools(_mcp_session)
    return [t for t in tools if t.name != "browser_close"]

def get_current_apply_jobs_session():
    """The shared Playwright MCP session if it's been launched already, else None — a bare read,
    never triggers get_apply_jobs_mcp_tools()'s lazy launch. Used by pending_tabs_open() to poll
    open tabs without forcing the shared browser to start just to answer "is anything open"."""
    return _mcp_session

async def get_linkedin_mcp_tools() -> list:
    """Fetch the LinkedIn MCP server's tools once (first call only) and cache them, so every scrape
    after the first reuses the same client instead of spinning up a new mcp-server-linkedin
    subprocess."""
    global _linkedin_client, _linkedin_tools
    if _linkedin_client is None:
        _linkedin_client = MultiServerMCPClient(LINKEDIN_MCP_CONFIG)
    if _linkedin_tools is None:
        try:
            _linkedin_tools = await _linkedin_client.get_tools(server_name="mcp-server-linkedin")
        except Exception as exc:
            # Reset the client too, not just the tools cache, so a retry launches a fresh
            # mcp-server-linkedin subprocess instead of reusing one that failed to connect.
            _linkedin_client = None
            _linkedin_tools = None
            logger.exception("Failed to establish the LinkedIn MCP connection")
            raise MCPConnectionError(f"Could not connect to the LinkedIn MCP server: {exc}") from exc
    return _linkedin_tools
