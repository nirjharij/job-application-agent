import os

from langchain.chat_models import init_chat_model

from utilities.linkedin.linkedin_session import STORAGE_STATE_PATH

CSV_FILENAME = "job_details.csv"
MODEL = "gpt-5-nano"

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
