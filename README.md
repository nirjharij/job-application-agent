# Job Application Agent

An AI agent that searches for jobs, analyzes a resume against the job
descriptions it finds, and produces tailored versions of the resume for the jobs you approve.

## Project structure

- **`app/`** — the frontend application. A Streamlit UI backed by `agent.py` (the multi-agent
  system: a main agent that delegates to a job-search subagent and a resume-handling subagent),
  This is the finished, working version of the agent — run this if you just want to use the tool.
- **`notebooks/`** — the code for building the agent from scratch, in steps. 
  Read these if you want to see how the agent was built and why, rather than just run the finished result.

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

1. Install dependencies:
   ```bash
   uv sync
   ```
2. Create a `.env` file in this directory (`job_application_agent/`) with at least:
   ```
   OPENAI_API_KEY=your-key-here
   ```
   (`LANGSMITH_*` variables are optional and only needed if you want tracing.)
3. If you want to search jobs on LinkedIn (rather than just StepStone), log in once so the LinkedIn
   MCP server has a session to reuse:
   ```bash
   uvx mcp-server-linkedin@latest --login
   ```

## Running the notebooks

From this directory:
```bash
uv sync
uv run jupyter lab
```

## Running the app

From this directory:
```bash
cd app
uv run --project .. streamlit run streamlit_app.py
```
This starts the app at `http://localhost:8501`. The first load takes ~15-20 seconds while it starts
the LinkedIn MCP server and builds the agents. Upload a resume (PDF), enter a job role, location, and
platform, and click "Find jobs & analyze resume" to start.
