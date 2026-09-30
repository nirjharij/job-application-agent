import os
from pathlib import Path

from langchain.chat_models import init_chat_model

CSV_FILENAME = "job_details.csv"
MODEL = "gpt-5-nano"

# Every documented way of running this app (`cd app && uv run --project .. streamlit run
# streamlit_app.py`, and utilities/linkedin/linkedin_session.py's own one-time setup script) runs
# from `app/`, so os.getcwd() reliably resolves here — same convention OUTPUT_DIRECTORY below uses.
LINKEDIN_STORAGE_STATE_PATH = Path(os.getcwd()) / "linkedin_storage_state.json"

PLAYWRIGHT_MCP_CONFIG = {
    "playwright": {
        "transport": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@playwright/mcp@latest",
            "--isolated",
            "--storage-state",
            str(LINKEDIN_STORAGE_STATE_PATH),
            "--allow-unrestricted-file-access",
        ],
    }
}

LINKEDIN_MCP_CONFIG = {
    "mcp-server-linkedin": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-linkedin@latest"],
        "env": {"UV_HTTP_TIMEOUT": "300"},
    }
}

# Every LLM call in the app (agent reasoning via create_agent, and the direct init_chat_model
# calls inside apply_jobs_agent_tools.py/resume_handler_agent_tools.py) goes through get_llm()
# below so all of them share the same timeout/retry policy. max_retries is handled by the
# underlying provider client (langchain-openai/openai SDK), which retries with its own
# exponential backoff — no separate backoff loop needed here.
LLM_TIMEOUT_SECONDS = 60
LLM_MAX_RETRIES = 3


def get_llm():
    """Build a chat model instance with the shared timeout/retry policy applied. Called lazily
    (never at import time) since it needs the API key that load_dotenv() loads from .env, and
    every caller of this module runs load_dotenv() before actually building an agent."""
    return init_chat_model(MODEL, timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)


OUTPUT_DIRECTORY = os.path.join(os.getcwd(), "output")
os.makedirs(OUTPUT_DIRECTORY, exist_ok=True)
