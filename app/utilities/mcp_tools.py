from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from utilities.linkedin_session import STORAGE_STATE_PATH

PLAYWRIGHT_MCP_CONFIG = {
    "playwright": {
        "transport": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@playwright/mcp@latest",
            "--isolated",
            "--storage-state",
            str(STORAGE_STATE_PATH),
            "--allow-unrestricted-file-access",
        ],
    }
}

# needed so that it is not garbage collected
_mcp_session_cm = None
_mcp_session = None


async def get_apply_jobs_mcp_tools() -> list:
    """Launch the shared Playwright MCP session (first call only) and return its tools, minus
    browser_close (a full browser-process teardown tool — an accidental call would kill the shared,
    whole-process-lifetime browser the same way an accidental _browser.close() would with raw
    Playwright).
    """
    global _mcp_session_cm, _mcp_session
    if _mcp_session is None:
        client = MultiServerMCPClient(PLAYWRIGHT_MCP_CONFIG)
        _mcp_session_cm = client.session("playwright")
        _mcp_session = await _mcp_session_cm.__aenter__()
    tools = await load_mcp_tools(_mcp_session)
    return [t for t in tools if t.name != "browser_close"]


def get_current_apply_jobs_session():
    """The shared Playwright MCP session if it's been launched already, else None — a bare read,
    never triggers get_apply_jobs_mcp_tools()'s lazy launch. Used by pending_tabs_open() to poll
    open tabs without forcing the shared browser to start just to answer "is anything open"."""
    return _mcp_session


LINKEDIN_MCP_CONFIG = {
    "mcp-server-linkedin": {
        "transport": "stdio",
        "command": "uv",
        "args": [
            "run",
            "--directory",
            "/Users/nirjharijankar/projects/linkedin-mcp-server-pr727",
            "-m",
            "linkedin_mcp_server",
        ],
        "env": {"UV_HTTP_TIMEOUT": "300"},
    }
}


_linkedin_client = None
_linkedin_tools = None


async def get_linkedin_mcp_tools() -> list:
    """Fetch the LinkedIn MCP server's tools once (first call only) and cache them, so every scrape
    after the first reuses the same client instead of spinning up a new mcp-server-linkedin
    subprocess."""
    global _linkedin_client, _linkedin_tools
    if _linkedin_client is None:
        _linkedin_client = MultiServerMCPClient(LINKEDIN_MCP_CONFIG)
    if _linkedin_tools is None:
        _linkedin_tools = await _linkedin_client.get_tools(server_name="mcp-server-linkedin")
    return _linkedin_tools
