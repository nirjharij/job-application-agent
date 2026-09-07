"""One-time setup: seed a Playwright storage_state file from a manually-copied LinkedIn `li_at`
cookie, so apply_jobs_agent's automated browser opens LinkedIn already signed in.

    uv run --project .. python -m utilities.linkedin_session

Re-run it whenever the cookie stops working (password change, LinkedIn's "unusual activity"
check, or natural ~1yr expiry).
"""

import json
import time
from pathlib import Path

STORAGE_STATE_PATH = Path(__file__).resolve().parent.parent / "linkedin_storage_state.json"

_ONE_YEAR_SECONDS = 365 * 24 * 60 * 60


def write_storage_state(li_at: str) -> None:
    storage_state = {
        "cookies": [
            {
                "name": "li_at",
                "value": li_at,
                "domain": ".linkedin.com",
                "path": "/",
                "expires": time.time() + _ONE_YEAR_SECONDS,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ],
        "origins": [],
    }
    STORAGE_STATE_PATH.write_text(json.dumps(storage_state, indent=2))


if __name__ == "__main__":
    li_at = input("Paste your LinkedIn `li_at` cookie value: ").strip()
    write_storage_state(li_at)
    print(f"Wrote {STORAGE_STATE_PATH}")
