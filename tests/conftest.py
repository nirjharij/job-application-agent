"""Shared fixtures.

Two conventions used throughout the suite:

* Tools decorated with `@tool` are exercised through `.coroutine` (the undecorated function)
  rather than `.ainvoke({...})`, because LangChain injects the real `ToolRuntime` and we want
  to hand in our own.
* Collaborators are patched at the *import site* (`linkedin_scraper.is_job_applied`), never at the
  definition site (`jobs_db.is_job_applied`), since every module does `from x import y`.
"""

import csv
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

CSV_FIELDNAMES = [
    "title",
    "company",
    "location",
    "employment_type",
    "date_posted",
    "salary_min",
    "salary_max",
    "salary_currency",
    "description",
    "url",
]


@dataclass
class FakeRuntime:
    """Stand-in for langchain.tools.ToolRuntime."""

    state: dict = field(default_factory=dict)
    context: Any = None
    tool_call_id: str = "call-1"
    config: dict = field(default_factory=dict)


@dataclass
class FakeProfileContext:
    """Stand-in for agent.JobApplyContext."""

    applicantProfile: dict | None = None


class FakeResponse:
    """An LLM response: `.content` plus the `.tool_calls` the fill loop branches on."""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    """Replaces init_chat_model(...). Replays `responses`, repeating the last one forever so a
    bounded-retry loop can be driven without building a 40-element list."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        # Snapshot: _fill_one_job mutates one list in place, so storing the reference would make
        # every recorded call alias the final history.
        self.calls.append(list(messages))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@pytest.fixture
def fake_llm():
    def _make(*responses):
        return FakeLLM([r if isinstance(r, FakeResponse) else FakeResponse(r) for r in responses])

    return _make


def write_csv(path, rows, fieldnames=None):
    fieldnames = fieldnames or (list(rows[0]) if rows else CSV_FIELDNAMES)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def job_row(**overrides):
    row = dict.fromkeys(CSV_FIELDNAMES, "")
    row.update(
        title="Backend Engineer",
        company="Acme",
        location="Berlin",
        description="We need Python.",
        url="https://www.linkedin.com/jobs/view/111/",
    )
    row.update(overrides)
    return row


@pytest.fixture
def jobs_csv(tmp_path):
    """A two-row jobs csv in the canonical scraper schema."""
    rows = [
        job_row(),
        job_row(title="Data Engineer", company="Globex", url="https://www.linkedin.com/jobs/view/222/"),
    ]
    return write_csv(tmp_path / "job_details.csv", rows, CSV_FIELDNAMES)


@pytest.fixture(autouse=True)
def reset_module_state():
    """These modules keep process-lifetime globals that would otherwise leak between tests."""
    from utilities import mcp_tools

    yield
    mcp_tools._mcp_session = None
    mcp_tools._mcp_session_cm = None
    mcp_tools._linkedin_client = None
    mcp_tools._linkedin_tools = None


# --- streamlit_app import shim -------------------------------------------------------------
# streamlit_app.py is a script: importing it runs st.set_page_config() and builds the real agents
# (Postgres + LLM) at module scope. Stubbing `streamlit` and `agent` lets the two pure helpers
# (advance_phase, update_job_row) be imported without touching either.


class _StreamlitStub(types.ModuleType):
    class _SessionState(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, name, value):
            self[name] = value

    class _Widget:
        """Callable, context-manager, and falsy — so `if st.button(...)` never fires."""

        def __call__(self, *args, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __bool__(self):
            return False

    def __init__(self):
        super().__init__("streamlit")
        self.session_state = self._SessionState()

    def cache_resource(self, func):
        # Must be an identity decorator: a MagicMock here would replace get_agents/get_event_loop
        # with mocks and the module's own `agents = get_agents()` line would stop meaning anything.
        return func

    def columns(self, spec):
        n = spec if isinstance(spec, int) else len(spec)
        return tuple(self._Widget() for _ in range(n))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._Widget()


@pytest.fixture
def streamlit_app(monkeypatch):
    agent_stub = types.ModuleType("agent")

    async def build_agent():
        return {"main_agent": object()}

    agent_stub.build_agent = build_agent

    monkeypatch.setitem(sys.modules, "streamlit", _StreamlitStub())
    monkeypatch.setitem(sys.modules, "agent", agent_stub)
    monkeypatch.delitem(sys.modules, "streamlit_app", raising=False)

    import streamlit_app as module

    yield module

    sys.modules.pop("streamlit_app", None)
