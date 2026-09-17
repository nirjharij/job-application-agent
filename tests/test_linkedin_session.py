import json
import time

from utilities import linkedin_session
from utilities.linkedin_session import _ONE_YEAR_SECONDS, write_storage_state


def test_write_storage_state_writes_a_playwright_cookie_jar(tmp_path, monkeypatch):
    # Redirecting the path matters: the real target is the gitignored, hand-seeded
    # app/linkedin_storage_state.json and a test must never clobber it.
    target = tmp_path / "linkedin_storage_state.json"
    monkeypatch.setattr(linkedin_session, "STORAGE_STATE_PATH", target)

    before = time.time()
    write_storage_state("AQEDA-fake-cookie")
    state = json.loads(target.read_text())

    assert state["origins"] == []
    (cookie,) = state["cookies"]
    assert cookie["name"] == "li_at"
    assert cookie["value"] == "AQEDA-fake-cookie"
    assert cookie["domain"] == ".linkedin.com"
    assert cookie["path"] == "/"
    assert cookie["httpOnly"] is True
    assert cookie["secure"] is True
    assert cookie["sameSite"] == "None"
    assert before + _ONE_YEAR_SECONDS <= cookie["expires"] <= time.time() + _ONE_YEAR_SECONDS


def test_write_storage_state_overwrites_an_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "linkedin_storage_state.json"
    monkeypatch.setattr(linkedin_session, "STORAGE_STATE_PATH", target)

    write_storage_state("old")
    write_storage_state("new")

    cookies = json.loads(target.read_text())["cookies"]
    assert [c["value"] for c in cookies] == ["new"]


def test_storage_state_path_sits_next_to_the_app_package():
    assert linkedin_session.STORAGE_STATE_PATH.name == "linkedin_storage_state.json"
    assert linkedin_session.STORAGE_STATE_PATH.parent.name == "app"
