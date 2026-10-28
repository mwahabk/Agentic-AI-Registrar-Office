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

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python main.py                                    # interactive login
.venv/bin/python main.py --role student --id S1001
.venv/bin/python main.py --role advisor --id P01
.venv/bin/python main.py --role admin -q "Who is on the brink of dismissal?"
.venv/bin/python main.py --role professor --id P02 -v       # -v shows every tool call
.venv/bin/python -m pytest -q                               # tests (no API calls)
```

### Web chat

```bash
.venv/bin/uvicorn web.app:app --port 8000      # then open http://localhost:8000
```

Sign in with a role and ID. The page shows which specialist agents handle each request (tick **tool calls** to see every tool call), renders tables, and suggests questions for each role.

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
