import asyncio
import base64
import csv
import os
import time
import traceback
import uuid

import streamlit as st
from langchain.messages import HumanMessage
from langgraph.types import Command

from agent import build_agent
from config import OUTPUT_DIRECTORY
from tools.apply_jobs_agent_tools import pending_tabs_open
from utilities.jobs_csv import update_job_row
from utilities.validation import _validate_prompt, _validate_resume

st.set_page_config(page_title="Job Application Agent", layout="wide")


@st.cache_resource
def get_event_loop():
    # A single persistent loop for the whole process, reused by every run() call below (including
    # the one that builds the agents) — asyncio.run() creates *and closes* a fresh loop each call,
    # which would leave apply_jobs_agent_tools.py's shared Playwright browser connection (opened
    # inside whichever loop built it) unusable from every subsequent call on a different loop, since
    # asyncio transports can't be used across event loops. See CLAUDE.md's "Applying to jobs" section
    # for the related must-use-async-API note this pitfall is adjacent to.
    return asyncio.new_event_loop()


def run(coro):
    try:
        return get_event_loop().run_until_complete(coro)
    except BaseException:
        with open("error.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 80}\n{time.strftime('%Y-%m-%d %H:%M:%S')} phase={st.session_state.get('phase')}\n")
            traceback.print_exc(file=f)
        raise


@st.cache_resource
def get_agents():
    return run(build_agent())


agents = get_agents()
main_agent = agents["main_agent"]

if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
if "phase" not in st.session_state:
    # idle -> starting -> reviewing -> applying_wait -> resuming -> done
    # (resuming is shared: it's used both after "Continue" in reviewing, and after every
    # application tab is closed in applying_wait, since both are just resuming main_agent's
    # own interrupted thread)
    st.session_state.phase = "idle"
if "main_response" not in st.session_state:
    st.session_state.main_response = None
if "pending_inputs" not in st.session_state:
    st.session_state.pending_inputs = None


def reset_session():
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.phase = "idle"
    st.session_state.main_response = None
    st.session_state.pending_inputs = None


def advance_phase(response) -> str:
    """Decide the next phase from main_agent's own response. Since resume_handler_agent and
    apply_jobs_agent now run nested under main_agent's own checkpointer/thread (see agent.py),
    their HumanInTheLoopMiddleware interrupts surface directly as main_agent's own __interrupt__
    instead of a subagent-local one — so this is the single place that inspects it, for both the
    initial call and every resume."""
    if not response.get("pathToJobsCsv"):
        # No jobs csv produced at all (e.g. the model never called the search tool, or the search
        # itself failed) — nothing to review or apply to yet.
        return "search_failed"
    interrupts = response.get("__interrupt__")
    if interrupts:
        action_name = interrupts[0].value["action_requests"][0]["name"]
        if action_name == "resume_corrections_and_download":
            return "reviewing"
        if action_name == "post_job_apply":
            return "applying_wait"
    return "done"


config = {"configurable": {"thread_id": st.session_state.thread_id}}

with st.sidebar:
    st.header("Job Application Agent")
    st.caption(
        "Upload a resume and describe what you're looking for in one message (role, location, "
        "and platform if you have one), and it will find jobs and suggest resume tailoring for "
        "each one."
    )
    if st.button("Start new session"):
        reset_session()
        st.rerun()

st.title("Job Application Agent")

# Phases that involve a long-running agent call are handled up front, before any
# interactive widget is rendered — this ensures the UI reflects the "busy" state
# (no clickable submit/approve buttons) on the very next rerun, closing the window
# for a duplicate click to fire a second overlapping call on the same thread.

if st.session_state.phase == "starting":
    inputs = st.session_state.pending_inputs
    st.info("Searching for jobs and analyzing your resume against them...")
    with st.spinner("Working..."):
        response = run(main_agent.ainvoke(
            {
                "messages": [HumanMessage(content=inputs["userPrompt"])],
                "pdfBase64": inputs["pdfBase64"],
            },
            config=config,
        ))
    st.session_state.main_response = response
    st.session_state.pending_inputs = None
    st.session_state.phase = advance_phase(response)
    st.rerun()

elif st.session_state.phase == "search_failed":
    st.error(
        "Job search didn't produce any results to analyze. This can happen if the agent couldn't "
        "find matching jobs, or the search itself failed. See the message below for details, then "
        "try again with a new search."
    )
    st.write(st.session_state.main_response["messages"][-1].content)
    st.button("Start a new search", on_click=reset_session)

elif st.session_state.phase == "resuming":
    st.info("Continuing...")
    with st.spinner("Working..."):
        response = run(main_agent.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
        ))
    st.session_state.main_response = response
    st.session_state.phase = advance_phase(response)
    st.rerun()

elif st.session_state.phase == "applying_wait":
    if run(pending_tabs_open()):
        st.info("Applications are open in the browser — review, submit, and close each tab.")
        st.caption("This page will keep checking until every tab is closed.")
        time.sleep(2)
        st.rerun()
    else:
        st.session_state.phase = "resuming"
        st.rerun()

elif st.session_state.phase == "idle":
    with st.form("search_form"):
        resume_file = st.file_uploader("Resume (PDF)", type=["pdf"])
        user_prompt = st.text_area(
            "What are you looking for?",
            placeholder="I am looking for a SDE role in Noida which is part time or contract based",
            height=100,
        )

        submitted = st.form_submit_button("Find jobs & analyze resume")

    if submitted:
        validation_error = _validate_resume(resume_file) or _validate_prompt(user_prompt)
        if validation_error:
            st.error(validation_error)
        else:
            resume_bytes = resume_file.getvalue()
            with open(os.path.join(OUTPUT_DIRECTORY, "resume.pdf"), "wb") as f:
                f.write(resume_bytes)
            st.session_state.pending_inputs = {
                "userPrompt": user_prompt.strip(),
                "pdfBase64": base64.b64encode(resume_bytes).decode("utf-8"),
            }
            st.session_state.phase = "starting"
            st.rerun()

if st.session_state.phase in ("reviewing", "done") and st.session_state.main_response:
    main_response = st.session_state.main_response

    st.subheader("Jobs found")
    csv_path = main_response.get("pathToJobsCsv")
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        st.info("No jobs were found for that role and location.")
    else:
        st.dataframe(
            [
                {"Title": r["title"], "Company": r["company"], "Location": r["location"], "URL": r["url"]}
                for r in rows
            ],
            width="stretch",
        )

        if st.session_state.phase == "done":
            st.subheader("Agent summary")
            st.write(main_response["messages"][-1].content)
            st.button("Start a new search", on_click=reset_session)
        else:
            tailored = [r for r in rows if r.get("resume_corrections")]
            if tailored:
                st.subheader("Resume tailoring suggestions")
                if st.session_state.phase == "reviewing":
                    st.warning(
                        "The agent is paused, waiting for you to review these suggestions. Approve/Reject "
                        "below just mark which jobs to generate a tailored resume for — click Continue when "
                        "you're done reviewing to let the agent proceed."
                    )

                for r in tailored:
                    apply_flag = str(r.get("apply_resume_corrections", "")).strip().lower() == "true"
                    status_badge = " ✅ Marked for tailoring" if apply_flag else ""
                    job_key = r["url"]
                    with st.expander(f"{r['title']} — {r['company']}{status_badge}", expanded=True):
                        for suggestion in r["resume_corrections"].split(" | "):
                            if suggestion.strip():
                                st.markdown(f"- {suggestion.strip()}")

                        if st.session_state.phase == "reviewing":
                            approve_col, reject_col = st.columns(2)
                            if approve_col.button("Approve", key=f"approve_{job_key}", type="primary" if apply_flag else "secondary"):
                                update_job_row(csv_path, job_key, apply_resume_corrections=True)
                                st.rerun()
                            if reject_col.button("Reject", key=f"reject_{job_key}", type="secondary" if apply_flag else "primary"):
                                update_job_row(csv_path, job_key, apply_resume_corrections=False)
                                st.rerun()

                        resume_path = r.get("tailored_resume_path")
                        if resume_path and os.path.exists(resume_path):
                            with open(resume_path, "rb") as rf:
                                st.download_button(
                                    "Download tailored resume",
                                    data=rf.read(),
                                    file_name=os.path.basename(resume_path),
                                    key=f"download_{job_key}",
                                )

                st.divider()
                if st.button("Continue", type="primary"):
                    st.session_state.phase = "resuming"
                    st.rerun()