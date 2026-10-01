import json
import time

from utilities.linkedin import linkedin_session
from utilities.linkedin.linkedin_session import _ONE_YEAR_SECONDS, write_storage_state


def test_write_storage_state_writes_a_playwright_cookie_jar(tmp_path, monkeypatch):
    # Redirecting the path matters: the real target is the gitignored, hand-seeded
    # app/linkedin_storage_state.json and a test must never clobber it.
    target = tmp_path / "linkedin_storage_state.json"
    monkeypatch.setattr(linkedin_session, "LINKEDIN_STORAGE_STATE_PATH", target)

    before = time.time()
    write_storage_state("AQEDA-fake-cookie", '"ajax:1234"')
    state = json.loads(target.read_text())

    assert state["origins"] == []
    li_at, jsessionid = state["cookies"]

    assert li_at["name"] == "li_at"
    assert li_at["value"] == "AQEDA-fake-cookie"
    assert li_at["domain"] == ".linkedin.com"
    assert li_at["path"] == "/"
    assert li_at["httpOnly"] is True
    assert li_at["secure"] is True
    assert li_at["sameSite"] == "None"
    assert before + _ONE_YEAR_SECONDS <= li_at["expires"] <= time.time() + _ONE_YEAR_SECONDS

    assert jsessionid["name"] == "JSESSIONID"
    assert jsessionid["value"] == '"ajax:1234"'
    assert jsessionid["domain"] == ".linkedin.com"
    assert jsessionid["path"] == "/"
    assert jsessionid["httpOnly"] is False
    assert jsessionid["secure"] is True
    assert jsessionid["sameSite"] == "None"
    assert before + _ONE_YEAR_SECONDS <= jsessionid["expires"] <= time.time() + _ONE_YEAR_SECONDS


def test_write_storage_state_overwrites_an_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "linkedin_storage_state.json"
    monkeypatch.setattr(linkedin_session, "LINKEDIN_STORAGE_STATE_PATH", target)

    write_storage_state("old", '"old-session"')
    write_storage_state("new", '"new-session"')

    cookies = json.loads(target.read_text())["cookies"]
    assert [c["value"] for c in cookies] == ["new", '"new-session"']


def test_storage_state_path_sits_next_to_the_app_package():
    assert linkedin_session.LINKEDIN_STORAGE_STATE_PATH.name == "linkedin_storage_state.json"
    assert linkedin_session.LINKEDIN_STORAGE_STATE_PATH.parent.name == "app"
