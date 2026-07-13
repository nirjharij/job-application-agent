import asyncio
import base64
import csv
import os
import uuid

import streamlit as st
from langchain.messages import HumanMessage
from langgraph.types import Command

from agent import build_agent
from browser import open_job_in_browser

st.set_page_config(page_title="Job Application Agent", layout="wide")


@st.cache_resource
def get_agents():
    return asyncio.run(build_agent())


agents = get_agents()
main_agent = agents["main_agent"]
resume_handler_agent = agents["resume_handler_agent"]

if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
if "phase" not in st.session_state:
    st.session_state.phase = "idle"  # idle -> searching -> analyzing -> reviewing -> resuming -> done
if "job_response" not in st.session_state:
    st.session_state.job_response = None
if "resume_response" not in st.session_state:
    st.session_state.resume_response = None
if "pending_inputs" not in st.session_state:
    st.session_state.pending_inputs = None
if "opening_job" not in st.session_state:
    st.session_state.opening_job = None


def run(coro):
    return asyncio.run(coro)


def reset_session():
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.phase = "idle"
    st.session_state.job_response = None
    st.session_state.resume_response = None
    st.session_state.pending_inputs = None
    st.session_state.opening_job = None


def update_job_row(csv_path: str, job_url: str, **updates) -> None:
    """Persist arbitrary per-job fields (decision, apply, tailored_resume_path, ...) back into the jobs csv."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames)
        for key in updates:
            if key not in fieldnames:
                fieldnames.append(key)

    for row in rows:
        for key in updates:
            row.setdefault(key, "")
        if row["url"] == job_url:
            row.update(updates)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


main_config = {"configurable": {"thread_id": st.session_state.thread_id}}
resume_config = {"configurable": {"thread_id": f"{st.session_state.thread_id}-resume-handler"}}

with st.sidebar:
    st.header("Job Application Agent")
    st.caption(
        "Upload a resume, tell the agent what role, location, and platform to search, "
        "and it will find jobs and suggest resume tailoring for each one."
    )
    if st.button("Start new session"):
        reset_session()
        st.rerun()

st.title("Job Application Agent")

# Phases that involve a long-running agent call are handled up front, before any
# interactive widget is rendered — this ensures the UI reflects the "busy" state
# (no clickable submit/approve buttons) on the very next rerun, closing the window
# for a duplicate click to fire a second overlapping call on the same thread.

if st.session_state.opening_job:
    job = st.session_state.opening_job
    st.info(f"Opening {job['url']} in a browser window. Close that window when you're done to return here.")
    with st.spinner("Waiting for you to finish in the browser..."):
        open_job_in_browser(job["url"])
    update_job_row(job["csv_path"], job["url"], decision="applied")
    st.session_state.opening_job = None
    st.rerun()

elif st.session_state.phase == "searching":
    inputs = st.session_state.pending_inputs
    st.info(f"Searching {inputs['platform']} for jobs...")
    with st.spinner("Working..."):
        response = run(main_agent.ainvoke(
            {
                "messages": [HumanMessage(
                    content=f"Find job for {inputs['jobRole']} in {inputs['jobLocation']} on {inputs['platform']}"
                )],
                "platform": inputs["platform"],
            },
            config=main_config,
        ))
    st.session_state.job_response = response
    if response.get("pathToJobsCsv"):
        st.session_state.phase = "analyzing"
    else:
        # Job search didn't actually produce a jobs csv (e.g. the model never called the search
        # tool, or the search itself failed) — don't proceed into resume analysis with no csv path,
        # that would crash the resume_handler_agent instead of surfacing a clear error here.
        st.session_state.pending_inputs = None
        st.session_state.phase = "search_failed"
    st.rerun()

elif st.session_state.phase == "search_failed":
    st.error(
        "Job search didn't produce any results to analyze. This can happen if the agent couldn't "
        "find matching jobs, or the search itself failed. See the message below for details, then "
        "try again with a new search."
    )
    st.write(st.session_state.job_response["messages"][-1].content)
    st.button("Start a new search", on_click=reset_session)

elif st.session_state.phase == "analyzing":
    inputs = st.session_state.pending_inputs
    csv_path = st.session_state.job_response.get("pathToJobsCsv")
    st.info("Analyzing your resume against the job descriptions...")
    with st.spinner("Working..."):
        response = run(resume_handler_agent.ainvoke(
            {
                "messages": [HumanMessage(content="Analyze my resume against the job descriptions, then prepare tailored versions.")],
                "pdfBase64": inputs["pdfBase64"],
                "pathToJobsCsv": csv_path,
            },
            config=resume_config,
        ))
    st.session_state.resume_response = response
    st.session_state.pending_inputs = None
    st.session_state.phase = "reviewing" if response.get("__interrupt__") else "done"
    st.rerun()

elif st.session_state.phase == "resuming":
    st.info("Continuing...")
    with st.spinner("Working..."):
        response = run(resume_handler_agent.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=resume_config,
        ))
    st.session_state.resume_response = response
    st.session_state.phase = "reviewing" if response.get("__interrupt__") else "done"
    st.rerun()

elif st.session_state.phase == "idle":
    with st.form("search_form"):
        resume_file = st.file_uploader("Resume (PDF)", type=["pdf"])
        col1, col2, col3 = st.columns(3)
        with col1:
            job_role = st.text_input("Job role", placeholder="Software Development Engineer")
        with col2:
            job_location = st.text_input("Location", placeholder="Berlin")
        with col3:
            platform = st.selectbox("Platform", options=["linkedin", "stepstone"], index=0)
        st.caption("Results are capped at 3 jobs to keep scraping time and LLM costs down for this demo.")
        submitted = st.form_submit_button("Find jobs & analyze resume")

    if submitted:
        if not resume_file or not job_role or not job_location:
            st.error("Please provide a resume PDF, job role, and location.")
        else:
            st.session_state.pending_inputs = {
                "jobRole": job_role,
                "jobLocation": job_location,
                "platform": platform,
                "pdfBase64": base64.b64encode(resume_file.read()).decode("utf-8"),
            }
            st.session_state.phase = "searching"
            st.rerun()

if st.session_state.phase in ("reviewing", "done") and st.session_state.job_response:
    job_response = st.session_state.job_response
    resume_response = st.session_state.resume_response

    st.subheader("Jobs found")
    csv_path = job_response.get("pathToJobsCsv")
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
                apply_flag = str(r.get("apply", "")).strip().lower() == "true"
                status_badge = " ✅ Marked for tailoring" if apply_flag else ""
                job_key = r["url"]
                with st.expander(f"{r['title']} — {r['company']}{status_badge}", expanded=True):
                    for suggestion in r["resume_corrections"].split(" | "):
                        if suggestion.strip():
                            st.markdown(f"- {suggestion.strip()}")

                    if st.session_state.phase == "reviewing":
                        approve_col, reject_col = st.columns(2)
                        if approve_col.button("Approve", key=f"approve_{job_key}", type="primary" if not apply_flag else "secondary"):
                            update_job_row(csv_path, job_key, apply=True)
                            st.rerun()
                        if reject_col.button("Reject", key=f"reject_{job_key}"):
                            update_job_row(csv_path, job_key, apply=False)
                            st.rerun()

                    resume_path = r.get("tailored_resume_path")
                    if resume_path and os.path.exists(resume_path):
                        decision = r.get("decision") or ""
                        apply_open_col, download_col = st.columns(2)
                        if apply_open_col.button("Apply", key=f"apply_{job_key}", disabled=(decision == "applied")):
                            st.session_state.opening_job = {"url": job_key, "csv_path": csv_path}
                            st.rerun()
                        with open(resume_path, "rb") as rf:
                            download_col.download_button(
                                "Download tailored resume",
                                data=rf.read(),
                                file_name=os.path.basename(resume_path),
                                key=f"download_{job_key}",
                            )

            if st.session_state.phase == "reviewing":
                st.divider()
                if st.button("Continue", type="primary"):
                    st.session_state.phase = "resuming"
                    st.rerun()

    if st.session_state.phase == "done":
        st.subheader("Agent summary")
        st.write(resume_response["messages"][-1].content)
        st.button("Start a new search", on_click=reset_session)
