# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

- **`app/`** — the finished, working application. A Streamlit UI (`streamlit_app.py`) backed by
  `agent.py`, a multi-agent system built with LangChain/LangGraph: a `main_agent` that delegates to
  `job_search_agent`, `resume_handler_agent`, and `apply_jobs_agent` subagents. The Streamlit UI drives
  the full pipeline end to end: search → resume tailoring (with human review) → filling out
  applications (with a human review-and-close-tabs step) → done — see "Streamlit app" below.
  - `agent.py` only defines the `AgentState` subclasses and builds the four agents — prompts and
    tools live elsewhere so agent wiring stays readable on its own.
  - `prompts/` — one file per agent's system prompt (`main_agent_prompt.py`,
    `job_search_agent_prompt.py`, `resume_handler_agent_prompt.py`, `apply_jobs_agent_prompt.py`).
  - `tools/` — one file per subagent's toolset (`job_search_agent_tools.py`,
    `resume_handler_agent_tools.py`, `apply_jobs_agent_tools.py`); `call_job_search_agent` stays
    defined inline in `agent.py` since it closes over the `job_search_agent` graph built in the
    same function.
  - `utilities/` — the scrapers (`job_scraper.py` for StepStone, `linkedin_scraper.py` for LinkedIn)
    and `jobs_db.py` (the Postgres `jobs_applied` dedup table helpers).
  - `ui/browser.py` — a Playwright helper that opens a job's URL in a real browser window; no longer
    called from anywhere (superseded by `apply_jobs_agent`'s bulk-apply flow), left in place as dead
    code rather than deleted along with the feature that used it.
  - `config.py` — just the shared `MODEL` constant, split out so both `agent.py` and the tools
    modules can import it without a circular import.
- **`notebooks/`** — the code for building the agent from scratch, in steps, with duplicated copies
  of `job_scraper.py`/`linkedin_scraper.py`. This shows how the agent was built and why; it is not
  the thing to modify when fixing/extending app behavior — `
- app/` is.

There is no test suite, linter, or build step configured in this repo.

## Setup & running

Dependency management is via `uv` (see `pyproject.toml` / `uv.lock`); `requirements.txt` also exists
but `uv` is the primary workflow.

```bash
uv sync                                    # install dependencies
```

A `.env` file in the project root must define at least `OPENAI_API_KEY` (`LANGSMITH_*` vars are
optional, for tracing). `JOBS_DB_URI` (defaults to
`postgresql://postgres:postgres@localhost:5432/postgres?sslmode=disable` if unset — see `app/utilities/jobs_db.py`)
points at a running Postgres instance used both for `main_agent`'s long-term memory store and the
`jobs_applied` dedup table; nothing else in the app touches Postgres.

Run the app (from the `app/` directory):
```bash
cd app
uv run --project .. streamlit run streamlit_app.py
```
Starts at `http://localhost:8501`. First load takes ~15-20s (starts the LinkedIn MCP server and
builds the agents).

Run the notebooks:
```bash
uv sync
uv run jupyter lab
```

To search jobs on LinkedIn (rather than just StepStone), a one-time interactive login is required so
the LinkedIn MCP server has a session to reuse:
```bash
uvx mcp-server-linkedin@latest --login
```

To let `apply_jobs_agent`'s Playwright browser open LinkedIn job pages already signed in (rather
than hitting LinkedIn's sign-in wall on every run), a one-time setup seeds a storage_state file from
a manually-copied `li_at` cookie — see `app/utilities/linkedin_session.py` for why (in short: signing
into LinkedIn via "Sign in with Google" inside the automated browser gets blocked by Google itself,
not LinkedIn, so the login has to happen in a normal browser instead):
1. Log into LinkedIn normally in your everyday Chrome.
2. DevTools → Application → Cookies → `https://www.linkedin.com` → copy the `li_at` value.
3. From `app/`: `uv run --project .. python -m utilities.linkedin_session`, paste the value when prompted.

This writes `app/linkedin_storage_state.json` (gitignored, same sensitivity tier as `.env`). Redo it
whenever the cookie stops working (password change, LinkedIn's "unusual activity" check, or its
natural ~1yr expiry).

## Architecture

### Agent graph (`app/agent.py`)

Four `create_agent` (LangGraph) agents. Only `main_agent` has a real checkpointer
(`InMemorySaver`) — `job_search_agent`, `resume_handler_agent`, and `apply_jobs_agent` are compiled
with `checkpointer=True`, LangGraph's "nested-only" sentinel (raises if one is ever invoked as a
root graph). All three are only ever invoked through `main_agent`'s own `call_*_agent` tools, each
passing `config=runtime.config` (main_agent's own ambient config, not a hand-built dict) rather than
a fresh `{"configurable": {"thread_id": ...}}`. That one detail is what makes this a genuine nested
LangGraph run rather than an unrelated top-level one: `runtime.config` already carries LangGraph's
internal `CONFIG_KEY_TASK_ID`/`CONFIG_KEY_CHECKPOINTER` markers (stamped on automatically since the
tool itself is executing as part of `main_agent`'s own Pregel run), so the subagent's own
`interrupt()` call is *not* caught and converted locally — it propagates straight out through the
`await subagent.ainvoke(...)` call, through the tool, through `main_agent`'s own `ToolNode` (which
explicitly re-raises `GraphInterrupt`/`GraphBubbleUp` rather than swallowing it), and is only caught
by `main_agent`'s own (non-nested) run — meaning `main_agent.ainvoke()` itself returns `__interrupt__`
whenever a nested subagent pauses. Resuming is symmetric: `main_agent.ainvoke(Command(resume=...),
config=main_config)` replays into the same nested tool call and finishes the paused subagent using
checkpoints now stored under `main_agent`'s own checkpointer, and `main_agent`'s own reasoning
continues naturally afterward in the same call (e.g. deciding to call `call_apply_jobs_agent` right
after being resumed) — no relay message needed. See "Streamlit app" below for how this drives the UI.

- **`main_agent`** (`JobApplicationAgentState`) — user-facing entry point, and the *only* way any
  subagent gets invoked at all (the Streamlit app never calls `job_search_agent`/
  `resume_handler_agent`/`apply_jobs_agent` itself — see "Streamlit app" below). Tools:
  `call_job_search_agent` (wraps `job_search_agent`), `call_resume_handler_agent` (wraps
  `resume_handler_agent`, invoked automatically right after search succeeds), and
  `call_apply_jobs_agent` (wraps `apply_jobs_agent`, invoked once tailoring is complete — see
  `prompts/main_agent_prompt.py`). It never searches, analyzes resumes, fills forms, or fabricates
  job details itself; it just relays each subagent's output — and since nested interrupts pause
  `main_agent`'s own run (see above), there's no separate "pending" flag to track: Streamlit reads
  `main_agent`'s own `__interrupt__` directly. Also carries a `PostgresStore` (see "Long-term memory"
  below) as long-term memory, entered once in `build_agent()` and never explicitly closed — it needs
  to outlive `build_agent()` for the life of the Streamlit process, same as `main_agent`'s
  `InMemorySaver`.
- **`job_search_agent`** (`JobSearchAgentState`) — `main_agent`'s subagent for the search step only.
  Tools: `job_finder` (scrapes StepStone directly or LinkedIn via MCP, writes `job_details.csv`,
  returns a `Command` updating `pathToJobsCsv`).
- **`resume_handler_agent`** (`ResumeHandlerAgentState`) — entered only via `main_agent`'s
  `call_resume_handler_agent`. Tools: `analyze_resume_and_make_suggestions` (compares the resume PDF
  to each job description, writes suggestions + `apply=False` back into the jobs csv),
  `resume_corrections_and_download` (generates one tailored resume `.txt` per row with `apply=True`,
  never overwriting the original resume) — its `HumanInTheLoopMiddleware` interrupt is configured on
  this tool, and per the nested-propagation design above, pausing here pauses `main_agent`'s own run.
- **`apply_jobs_agent`** (`ApplyJobsAgentState`, `JobApplyContext`) — entered only via `main_agent`'s
  `call_apply_jobs_agent`; its `HumanInTheLoopMiddleware` interrupt is configured on `post_job_apply`,
  not `start_applying` — every job gets opened and filled in automatically with no approval gate
  first (filling a form never submits anything, so there's nothing to approve), and the pause instead
  happens right before jobs get marked applied. See "Applying to jobs" below for its tools.

Key state-passing conventions:
- `platform` ("linkedin" or "stepstone") and `numJobs` are set by the Streamlit UI into `main_agent`
  state and read from state inside `call_job_search_agent` — never re-extracted from the LLM's own
  prose, because that previously caused the subagent to silently get the wrong tools (and the same
  exact-value risk applies to an exact job count).
- `pathToJobsCsv` is the single source of truth for where the jobs csv lives; it flows from
  `job_finder` → `main_agent` state → the Streamlit session → `resume_handler_agent` state, and
  `call_apply_jobs_agent` reads it back out of `main_agent`'s own state (`runtime.state.get
  ("pathToJobsCsv")`, unchanged since the original search) to pass into `apply_jobs_agent`.
- `applicantProfile` (name/email/phone/etc., collected once in the Streamlit form alongside the
  resume) flows the same way as `pathToJobsCsv`: the Streamlit app puts it into `main_agent` state on
  the initial call, `call_apply_jobs_agent` reads it back out of `main_agent`'s own state, and passes
  it into `apply_jobs_agent` as `JobApplyContext` — not state, since it's static for the whole
  `apply_jobs_agent` run. `start_applying` (`tools/apply_jobs_agent_tools.py`) reads
  `runtime.context.applicantProfile` and surfaces it in its return message, which is what
  `apply_jobs_agent` actually fills forms with, alongside each job's `tailored_resume_path`.
- The jobs csv itself accumulates columns as the pipeline progresses: scraper output
  (`title`/`company`/`location`/.../`description`/`url`) → `+resume_corrections`, `+apply` (from
  analysis) → `+tailored_resume_path` (from download). Whether a job has actually been applied to
  lives in Postgres (`jobs_applied`), not the csv — see "Long-term memory & dedup" below.
- The Streamlit "Approve"/"Reject" buttons during the `reviewing` phase do not go through the
  human-in-the-loop `Command(resume=...)` decision at all — they just flip `apply` True/False
  directly in the csv via `update_job_row`. The single "Continue" button always resumes the
  interrupted graph with `{"type": "approve"}` regardless of which rows were picked; the interrupt
  exists purely to pause before generation so the human can review suggestions, not to gate
  individual rows through the HITL mechanism itself.

### Long-term memory & dedup (`app/utilities/jobs_db.py`)

A single Postgres instance (`JOBS_DB_URI`) serves two unrelated purposes:
- **`main_agent`'s long-term memory** — a `langgraph.store.postgres.PostgresStore`, passed as
  `store=` to `main_agent`'s `create_agent(...)` call. `PostgresStore.from_conn_string(...)` is a
  context manager; `build_agent()` enters it manually (`.__enter__()`) and never exits, so the pool
  survives for the process lifetime rather than closing the moment `build_agent()` returns.
- **`jobs_applied` dedup table** — a plain table (`id UUID PRIMARY KEY, url TEXT UNIQUE, applied_at`)
  managed by hand-written SQL in `jobs_db.py`, unrelated to the store's own internal tables. `id` is
  `uuid.uuid5(uuid.NAMESPACE_URL, url)` — deterministic, so the same job url always maps to the same
  row without needing a prior lookup to get an id. `mark_job_applied(url)` is called from
  `post_job_apply` (`tools/apply_jobs_agent_tools.py`), once per job that was filled in, after the
  Streamlit app has confirmed every opened tab is closed and resumed `main_agent`'s interrupt
  (replacing the old `decision="applied"` csv column); `is_job_applied(url)` gates both scrapers
  (`job_scraper.py`, `linkedin_scraper.py`) so already-applied jobs are skipped before being written to
  a new search's csv.

### Streamlit app (`app/streamlit_app.py`)

A phase-based state machine driven by `st.session_state.phase`:
`idle → starting → reviewing → applying_wait → resuming → done` (plus a `search_failed` branch).
Everything runs on a single LangGraph thread (`st.session_state.thread_id`) against `main_agent`
only — there is no separate thread or direct `.ainvoke()` call for `resume_handler_agent`/
`apply_jobs_agent` anymore, since their interrupts now surface as `main_agent`'s own `__interrupt__`
(see "Agent graph" above).

`starting` makes the initial `main_agent.ainvoke({...})` call (search + resume analysis, since
`main_agent` chains into `call_resume_handler_agent` itself in the same turn). `resuming` makes every
subsequent call, always `main_agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}),
config=main_config)` — used both after "Continue" in `reviewing` and after every tab closes in
`applying_wait`. Both phases funnel their response through a shared `advance_phase()` helper that:
- returns `search_failed` if `pathToJobsCsv` is still missing,
- else, if `response.get("__interrupt__")` is set, inspects
  `response["__interrupt__"][0].value["action_requests"][0]["name"]` (the `HITLRequest` shape
  `HumanInTheLoopMiddleware` raises) to tell the two possible pauses apart —
  `"resume_corrections_and_download"` → `reviewing`, `"post_job_apply"` → `applying_wait`,
- else → `done`.

`applying_wait` is a pure poll loop, independent of any agent call: while `pending_tabs_open()`
(`tools/apply_jobs_agent_tools.py`) reports any job tab still open, it shows a waiting message,
sleeps briefly, and reruns; once every tab is closed, it just sets phase to `resuming` — the actual
resume (which lets `post_job_apply` run and mark the jobs applied) happens there, uniformly with
every other resume.

Phases that call a long-running agent (`starting`, `resuming`) are handled at the very top of the
script, before any interactive widget renders — this ensures the "busy" UI state (no clickable
buttons) is shown on the very next rerun, closing the window for a duplicate click to fire a second
overlapping call on the same LangGraph thread. `applying_wait` follows the same convention.

### Scrapers

- **`app/utilities/job_scraper.py`** — StepStone: plain `requests` + BeautifulSoup, parses the
  `JobPosting` JSON-LD block embedded in each job detail page. Synchronous, rate-limited with
  `time.sleep(1)` between requests.
- **`app/utilities/linkedin_scraper.py`** — LinkedIn: goes through the `mcp-server-linkedin` MCP server
  (`langchain_mcp_adapters.MultiServerMCPClient`, stdio transport) rather than scraping HTML
  directly, since LinkedIn exposes no structured job schema. `_parse_job_posting_text` is a
  best-effort heuristic split of unstructured MCP text output into company/location/description
  (company = first line, location = first line containing `" · "`, description = text after "About
  the job"). `linkedin_tool_messages_to_csv` is an alternate path for consolidating results when the
  LLM calls the LinkedIn MCP tools directly via dynamic tool selection, instead of going through
  `scrape_linkedin_jobs_to_csv`.

Both scrapers normalize to the same csv schema (`title, company, location, employment_type,
date_posted, salary_min, salary_max, salary_currency, description, url`) so `agent.py`'s tools work
identically regardless of platform.

### Applying to jobs (`app/tools/apply_jobs_agent_tools.py`)

`apply_jobs_agent`'s tools are the stock `langchain_community` `PlayWrightBrowserToolkit` tools
(`navigate_browser`, `click_element`, `extract_text`, `extract_hyperlinks`, `get_elements`,
`previous_webpage`, `current_webpage`) plus custom ones defined here (`start_applying`,
`open_new_tab`, `fill_element`, `upload_file`, `post_job_apply`) — unlike the earlier
`browser-use`-based design, there's no separate LLM loop hidden inside a library call; the agent's
own `create_agent` tool-calling loop *is* what decides each click/fill/navigate, one small step at a
time, driven by `prompts/apply_jobs_agent_prompt.py`.

- All tools share one real, visible Playwright browser (`_browser` at module level), built once by
  `build_playwright_tools()` from `agent.py`'s `build_agent()` and kept alive for the app's entire
  process lifetime — no lazy create/teardown/recreate cycle between apply runs, deliberately simpler
  than the churn that caused problems in the `browser-use` version. Trade-off: if the human fully
  quits the browser app itself (not just closes individual job tabs), the Streamlit process needs
  restarting too, since there's no recovery path for a dead shared browser.
- **Must use `playwright.async_api` directly** (`async_playwright().start()` +
  `browser.chromium.launch(...)`), not `langchain_community`'s `create_async_playwright_browser()`
  helper or Playwright's sync API — both of those either wrap a `run_until_complete()` call or are
  Playwright's sync API itself, and neither can run inside a thread that already has an asyncio event
  loop running in it, which every call site here does. This bit during development: the failure
  doesn't surface at browser-launch time, only at the next unrelated event-loop call.
- **`streamlit_app.py` runs everything on one persistent event loop, not a fresh `asyncio.run()` per
  call.** `asyncio.run()` creates a new loop and closes it when the coroutine returns; since the
  shared Playwright browser above is opened once (inside whichever call runs `build_agent()`) and
  reused for the app's entire lifetime, calling it from a *different*, later `asyncio.run()` loop
  doesn't work — asyncio transports can't be used across event loops, and using it that way manifests
  as an immediate "transport closed" error (or a hang, depending on the runtime) the first time a
  tool actually touches `_browser`, not at build or launch time. `streamlit_app.py` avoids this with
  `get_event_loop()` (an `@st.cache_resource`-cached `asyncio.new_event_loop()`) and a `run(coro)`
  helper that calls `get_event_loop().run_until_complete(coro)` — used for every agent call in the
  file, including the one inside `get_agents()` that runs `build_agent()` itself, so the browser and
  every later call to use it share the same loop for the process's whole lifetime.
- The stock `NavigateTool` always navigates the *current* page (`context.pages[-1]`), reusing
  whatever tab is already open rather than creating a new one — so `open_new_tab` (which does
  `context.new_page()` itself) is what `apply_jobs_agent_prompt.py` tells the agent to use once per
  job, keeping every previous job's filled-out tab open while a new one becomes current.
- `start_applying` reads every row in the jobs csv (deliberately unfiltered — whether a job's resume
  was tailored/approved is an independent decision from whether to apply to it) and returns their
  url/description, plus the applicant's own details from `runtime.context.applicantProfile`
  (`JobApplyContext`), as its tool output — it is *not* gated, so the whole fill-in loop for every job
  runs automatically once it's called.
- `post_job_apply` is the tool `HumanInTheLoopMiddleware` gates (`interrupt_on={"post_job_apply": True}`
  in `agent.py`) — the pause happens right before it runs, after every job has already been opened and
  filled in, not before any browser action (see `apply_jobs_agent` in "Agent graph" above for why
  filling isn't gated). Unlike the tool it replaced (`verify_and_submit`), it does **not** block or
  poll itself — it just marks every pending job applied via `mark_job_applied` and returns immediately.
  The actual "wait for the human to review, submit, and close every tab" logic now lives in
  Streamlit's `applying_wait` phase (see "Streamlit app" above), which polls `pending_tabs_open()` and
  only moves on to resuming `main_agent` once every tab is confirmed closed — moving that wait out of
  the tool is what makes the interrupt actually resumable instead of hanging forever. Jobs
  pending application are tracked in a module-level list (`_pending_jobs`/`_opened_pages`) between
  `start_applying`/`open_new_tab` and `post_job_apply`/`pending_tabs_open`, for the same reason the
  browser itself is module-level.
- `extract_text` (one of the stock toolkit tools) needs `lxml` for its `BeautifulSoup` parser —
  previously only present transitively via the now-removed `browser-use` dependency, so it's now
  listed explicitly in `pyproject.toml`. Missing it fails at first use of `extract_text`, not at
  browser-launch time, which made it easy to miss during testing.

### Model

All LLM calls (agents and direct `init_chat_model` calls inside tools) use `MODEL = "gpt-5-nano"`,
defined once in `app/config.py`. The number of jobs per search is user-configurable from the Streamlit
form (1-10, default 3) — it flows through state as `numJobs` (`JobApplicationAgentState` →
`JobSearchAgentState` → read directly in `job_finder`, the same state-passing pattern used for
`platform`) rather than as a model-supplied tool argument, to bound scraping time/LLM cost reliably.
`apply_jobs_agent` uses `config.MODEL` like every other agent here — no exception, since it's a
regular `create_agent` graph rather than a separate library's own agent abstraction.
