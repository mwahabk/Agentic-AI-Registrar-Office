# Agentic AI Registrar Office

A multi-agent AI system that works like a university registrar office. It's built with the Claude Agent SDK (Python).

## Architecture

```
User (student / professor / advisor / admin) ── login (role + ID)
        │
        ▼
Registrar coordinator agent ── routes requests, runs specialists in parallel, merges answers
   ├── Records agent       transcripts, GPA, grade submission
   ├── Registration agent  catalog, schedules, rosters, register/drop
   ├── Analytics agent     course-group grade stats, student groups
   └── Advising agent      degree audit, next-course recommendations
        │ (tools, filtered by role)
        ▼
Service layer (registrar/service.py) ── role-based access control and business rules
        │
        ▼
SQLite: Students · Grades · Schedule · Courses · Professors
```

* The **coordinator has no data tools**. It has to delegate to a specialist.
* Each specialist only gets the tools for its own area, filtered by the user's role. For example, students get no Analytics agent.
* **Access control is enforced in code**, not just in prompts:
  - Students can only see and change their own records.
  - Professors can only see rosters and statistics for, and grade, their own sections.
  - Advisors can only see their own advisees.
  - Admins see everything.
* The service layer enforces the business rules: prerequisites (C- or better), seat capacity, time conflicts, an 18-credit limit, the registration window, and valid grades.

## What each user can do (from the project brief)

| Role | Examples |
|---|---|
| Student | "Show my transcript", "What's my schedule?", "Register me for CS201", "Am I on track to graduate?" |
| Professor | "What am I teaching?", "CS201 roster", "Grade stats for CS310", "Give S1010 a B+ in CS201" |
| Administrator | "Grade distribution for CS courses", "Courses with lots of Fs", "Who will graduate?", "Students on the brink of dismissal" |
| Advisor | "List my advisees", "Which advisees are at risk?", "What should S1005 take next semester?" |

## Installation

### 1. Requirements

* **Python 3.10 or newer.** On Windows: `winget install -e --id Python.Python.3.12`, or the installer from python.org (tick **"Add python.exe to PATH"**). Open a new terminal afterwards and check with `python --version`.
* **A way to call Claude**, either one of these:
  - **Claude Pro/Max subscription (no extra cost, personal use on your own computer):** install Claude Code and sign in once. Usage counts against your plan's limits. Anthropic recommends an API key for agents you share with others.
    - Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`
    - macOS / Linux: `curl -fsSL https://claude.ai/install.sh | bash`

    Then run `claude`, choose **"Claude account with subscription"**, sign in in the browser, and type `/exit`. Do **not** set `ANTHROPIC_API_KEY`, or usage is billed to the API instead.
  - **API key (pay as you go):** create a key at console.anthropic.com and set `ANTHROPIC_API_KEY` (see below).

### 2. Set up the project (once)

**Windows (Command Prompt)**

```bat
cd /d "path\to\my-agent"
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**macOS / Linux**

```bash
cd path/to/my-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

This installs both modes: the CLI (`claude-agent-sdk`) and the web chat (`fastapi`, `uvicorn`, `websockets`).

Only if you use an API key instead of a subscription, set it in each new terminal:

```bat
set ANTHROPIC_API_KEY=sk-ant-...        (Windows)
export ANTHROPIC_API_KEY=sk-ant-...     (macOS / Linux)
```

### 3. Model

The project uses **Claude Haiku 5.5**, the lightest model, to save usage. To get more polished answers (for example for a final demo), change one line in `registrar/agents.py`:

```python
MODEL = "claude-haiku-5-5"   # or "claude-sonnet-5-5" / "claude-opus-5-5"
```

## Run it

Run these from the project folder. On macOS / Linux, use `.venv/bin/` where the commands below say `.venv\Scripts\`.

### CLI mode (terminal)

```bat
.venv\Scripts\python main.py
```

It asks for a role and an ID. Type `quit` to exit. More options:

```bat
.venv\Scripts\python main.py --role student --id S1001
.venv\Scripts\python main.py --role advisor --id P01
.venv\Scripts\python main.py --role admin -q "Who is on the brink of dismissal?"
.venv\Scripts\python main.py --role professor --id P02 -v
```

* `-q "..."` asks one question and exits.
* `-v` shows every tool call.

### Web mode (browser chat)

```bat
.venv\Scripts\uvicorn web.app:app --port 8000
```

When it shows `Uvicorn running on http://127.0.0.1:8000`, open **http://localhost:8000**. Sign in with a role and ID. The page shows which specialist agents handle each request (tick **tool calls** to see every tool call), renders tables, and suggests questions for each role. Keep the terminal open while you use it, and press **Ctrl+C** to stop. After restarting the server, refresh the page and sign in again.

### Sample logins

| Role | ID |
|---|---|
| Student | `S1001` … `S1060` |
| Professor | `P01` … `P07` |
| Advisor | `P01`, `P02`, `P03` (the professors who have advisees) |
| Administrator | no ID |

### Tests (no Claude calls, free)

```bat
.venv\Scripts\python -m pytest -q
```

### Troubleshooting

| Problem | Fix |
|---|---|
| `Python was not found` | Install Python (step 1). If it still opens the Microsoft Store, turn off **Settings → Apps → Advanced app settings → App execution aliases → python.exe / python3.exe**. |
| `The system cannot find the path specified` for `.venv\...` | You're not in the project folder, or step 2 didn't create `.venv`. Run `cd` into the folder and redo step 2. |
| `No module named 'claude_agent_sdk'` | Packages aren't installed. Run `.venv\Scripts\python -m pip install -r requirements.txt`. |
| Web page loads but messages don't send, and the server log says `No supported WebSocket library detected` | Run `.venv\Scripts\python -m pip install websockets`, restart the server, then sign in again. |
| Error about usage limits | Your plan's limit was reached. Wait for it to reset, or switch to a lighter model (step 3). |

Architecture diagram: `docs/architecture.html`

On first run, the database is created at `data/registrar.db`. It's seeded with sample data: 60 students, 7 professors, 16 courses, 6 semesters, and an advisor for each student. Delete the file to reset it.
Fall 2026 is in progress, and registration is open for Spring 2027.

## Files

* `registrar/db.py`: schema, policies, and the sample-data generator
* `registrar/service.py`: business logic and permission checks
* `registrar/agents.py`: tool definitions, specialist subagents, and the coordinator
* `main.py`: CLI
* `web/app.py`, `web/static/index.html`: web chat (FastAPI + WebSocket)
* `docs/architecture.html`: architecture diagram for the write-up
* `tests/test_service.py`: tests for access control and business rules
