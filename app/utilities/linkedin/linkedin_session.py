"""One-time setup: seed a Playwright storage_state file from manually-copied LinkedIn `li_at` and
`JSESSIONID` cookies, so apply_jobs_agent's automated browser opens LinkedIn already signed in.

    uv run --project .. python -m utilities.linkedin.linkedin_session

Re-run it whenever the cookies stop working (password change, LinkedIn's "unusual activity" check,
natural ~1yr expiry, or an `ERR_TOO_MANY_REDIRECTS` loop on linkedin.com — that specific error means
`li_at` alone wasn't accepted; LinkedIn also uses `JSESSIONID` as a CSRF token paired with it, so
both have to be present and matching or the server keeps bouncing the request back to re-auth).
"""

import json
import time

from config import LINKEDIN_STORAGE_STATE_PATH

_ONE_YEAR_SECONDS = 365 * 24 * 60 * 60


def write_storage_state(li_at: str, jsessionid: str) -> None:
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
            },
            {
                # Not httpOnly: LinkedIn's own frontend JS reads this cookie to set the
                # `csrf-token` request header on every call, so Playwright must expose it the
                # same way a real browser would.
                "name": "JSESSIONID",
                "value": jsessionid,
                "domain": ".linkedin.com",
                "path": "/",
                "expires": time.time() + _ONE_YEAR_SECONDS,
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
        ],
        "origins": [],
    }
    LINKEDIN_STORAGE_STATE_PATH.write_text(json.dumps(storage_state, indent=2))


if __name__ == "__main__":
    li_at = input("Paste your LinkedIn `li_at` cookie value: ").strip()
    jsessionid = input(
        "Paste your LinkedIn `JSESSIONID` cookie value (include the surrounding \" quotes "
        "exactly as DevTools shows them): "
    ).strip()
    write_storage_state(li_at, jsessionid)
    print(f"Wrote {LINKEDIN_STORAGE_STATE_PATH}")
