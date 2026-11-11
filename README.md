# Agentic AI Registrar Office

A multi-agent AI system that works like a university registrar office. It's built with the Claude Agent SDK (Python).

**Quick links:**
* **[How to install and run](#installation)**: set up once, then run the web app or the terminal version
* **[How to test it (TESTING.md)](TESTING.md)**: step-by-step tests in the web app, with expected results
* **[RAG report (PDF)](docs/RAG_Report.pdf)**: how knowledge retrieval is used in the system
* **[Architecture (PDF)](docs/architecture.pdf)**: system diagram, agents, enrollment chain, RAG, access rules and data model

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
* Registration locks the database while it checks seats and saves, so two students can never both take the last seat (see `demo_last_seat.py`).
* The service layer enforces the business rules, based on NMSU's catalog and common university rules:
  - Prerequisites must be completed with C- or better; courses in progress don't count.
  - Seat capacity, and time conflicts (including partly overlapping times).
  - At most 18 credits per semester; more is an overload that needs written permission.
  - Students on academic probation (GPA below 2.0) may take at most 13 credits (NMSU's Probation I limit).
  - Only courses with a D or F can be repeated; a repeated course counts once in GPA and credits, with its latest grade.
  - A student can't register for a course they're taking this semester.
  - Registration is only open for the upcoming semester; grades can only be entered for the current one, by the course's professor.

### Agents working together: enrollment

An enrollment request ("My ID is S1001, enroll me in CS310") runs as a chain. Each specialist does one step, and the Registrar passes its findings to the next agent:

1. **Records** checks the prerequisites (`check_prerequisites`).
2. **Registration** gets those findings and checks seats, time clashes and the credit limit (`check_enrollment`).
3. **Advising** gets both findings and checks how the course fits the degree (`degree_progress`).
4. **Registration** enrolls the student (`register_course`). The service layer runs every check again in code before saving.

If a step fails, the chain stops there and the reply explains why. The reply ends with a "How we checked" list, and the web page and terminal show what each agent received from the earlier ones.

### Knowledge retrieval (RAG)

Questions about rules, deadlines, procedures and course content are answered from the registrar's documents in `knowledge/` (2026-2027 handbook, a superseded 2024-2025 handbook, the course catalog and an advising guide):

* **Ingestion:** each document section becomes a chunk with metadata (document, section, effective date, current/superseded). Chunks are embedded with a small local model (`potion-retrieval-32M`, downloaded once, about 130 MB, no API key) and cached in `data/rag_index.npz`.
* **Retrieval:** hybrid search: BM25 keyword ranking plus embedding similarity, combined with reciprocal rank fusion. If nothing relevant is found, the tool says so (knowledge-gap check). Without the model (offline), it falls back to keyword search.
* **Policy agent (Agentic RAG):** answers only from retrieved passages, cites them as [document > section], prefers current rules over superseded ones, searches again with new wording when results are poor, and never guesses when the documents don't cover a question. The Advising agent uses the same search, and a refused enrollment step is explained with the handbook rule behind it.
* **Visible in the demo:** the web page and terminal show each search and which documents it retrieved.
* **Evaluation:** `python rag_eval.py` compares keyword, vector and hybrid retrieval on 30 test questions (hybrid: 87% of answers in the top 3). Full write-up: `docs/RAG_Report.pdf`.

### Sample data

The Computer Science courses are taught by real NMSU Computer Science faculty, matched to courses by research area (for example Dr. Christian Wayllace teaches Artificial Intelligence and Dr. Huiping Cao teaches Databases). Their course assignments, meeting times, students and grades are fictional sample data. The math, English and physics instructors are made-up names.

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

This installs both modes: the CLI (`claude-agent-sdk`) and the web chat (`fastapi`, `uvicorn`, `websockets`), plus `model2vec` for knowledge retrieval. The first question that searches the documents downloads the embedding model (about 130 MB) once.

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

To test every feature step by step in the web app (with the expected result for each question), follow **[TESTING.md](TESTING.md)**.

### Sample logins

| Role | ID |
|---|---|
| Student | `S1001` … `S1060` |
| Professor | `P01` … `P13` (P01–P09 are Computer Science faculty) |
| Advisor | `P01` … `P09` (every CS faculty member advises students) |
| Administrator | no ID |

### Demo: two students, one seat

```bat
.venv\Scripts\python demo_last_seat.py
```

Shows what happens when two students try to take the **last seat** in a course at the same moment. Part 1 uses the old logic without a lock, and the course ends up overbooked. Part 2 uses the real registration code, which locks the database while it checks seats: one student gets the seat and the other is told the course is full. It runs on a temporary copy of the database and makes no Claude calls, so it's free and safe to run any time.

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

Architecture: `docs/architecture.pdf`

On first run, the database is created at `data/registrar.db`. It's seeded with sample data: 60 students, 13 professors, 22 courses (16 core and general, 6 CS electives), 6 semesters, and an advisor for each student. Delete the file to reset it.
Fall 2026 is in progress, and registration is open for Spring 2027.

## Files

* `registrar/db.py`: schema, policies, and the sample-data generator
* `registrar/service.py`: business logic and permission checks
* `registrar/agents.py`: tool definitions, specialist subagents, and the coordinator
* `registrar/rag.py`: knowledge retrieval (chunking, embeddings, BM25, hybrid search)
* `knowledge/`: the registrar's documents used for RAG
* `rag_eval.py`: retrieval evaluation (keyword vs vector vs hybrid)
* `docs/RAG_Report.pdf` (and `.html`): report on how RAG is used
* `main.py`: CLI
* `demo_last_seat.py`: demo of two students racing for the last seat
* `TESTING.md`: step-by-step test guide with expected results
* `web/app.py`, `web/static/index.html`: web chat (FastAPI + WebSocket)
* `docs/architecture.pdf` (and `.html`): architecture diagram and design for the write-up
* `tests/test_service.py`: tests for access control and business rules
