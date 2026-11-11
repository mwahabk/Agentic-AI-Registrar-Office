# How to test the Agentic AI Registrar Office

Run every command from the project folder (the one with `main.py`). The commands are for Windows; on macOS/Linux use `.venv/bin/` where they say `.venv\Scripts\`.

Parts 1 and 2 are free: they make no Claude calls. Part 3 runs the agents in the web app and uses your Claude plan (with Claude Haiku 5.5, each question costs very little).

The AI's wording changes from run to run. The **"Check"** column lists the facts and behavior that should always be there.

## 0. Before you start

1. Set up once (see the README): `python -m venv .venv`, then `.venv\Scripts\python -m pip install -r requirements.txt`, and sign in to Claude Code (`claude`) or set `ANTHROPIC_API_KEY`.
2. Start from fresh sample data: **delete `data\registrar.db`** if it exists. It is rebuilt on the next run, and the examples below assume the fresh data.
3. The first question that searches the documents downloads the embedding model (about 130 MB, once), so it can take a little longer.

## 1. Automated tests (free, about 3 seconds)

```bat
.venv\Scripts\python -m pytest -q
```

**Expected:** `33 passed`. They cover:

| Area | What is checked |
|---|---|
| Access rules | Students see only their own records; advisors only their advisees; professors only their own sections; advisors and professors can't register students |
| Registration rules | Prerequisites need C- or better and in-progress courses don't count; time clashes, including partial overlaps; full courses; 18-credit limit (overload); 13-credit limit on probation; only D/F courses can be repeated; can't register for a course being taken now; registration window; drops; grade entry |
| GPA | A repeated course counts once, with its latest grade |
| Concurrency | Two students racing for the last seat: exactly one gets it |
| Sample data | CS courses taught by CS faculty; no professor double-booked; most students have courses to choose |
| RAG | Chunking and metadata; catalog matches the database; handbook states the same limits the code enforces; retrieval quality; semantic matching; knowledge-gap check; superseded rules labelled; keyword fallback without the model |

## 2. Free demos

**Retrieval evaluation (RAG):**
```bat
.venv\Scripts\python rag_eval.py
```
**Expected:** a table where hybrid search finds the answer in the top 3 for 87% of 30 questions, and "rejected 4/5 off-topic questions, wrongly rejected 0/30".

**Two students, one seat:**
```bat
.venv\Scripts\python demo_last_seat.py
```
**Expected:** Part 1 (no lock) ends `OVERBOOKED`; Part 2 (the real code) gives the seat to one student, refuses the other with "is full", and ends `correct`.

## 3. Agent tests in the web app (uses Claude)

### Start the web app

```bat
.venv\Scripts\uvicorn web.app:app --port 8000
```

Wait for `Uvicorn running on http://127.0.0.1:8000`, then open **http://localhost:8000** in your browser. Keep this terminal open the whole time; press **Ctrl+C** in it to stop the server at the end.

### How to run each test

1. **Sign in:** on the sign-in screen, pick the **Role** from the list and type the **ID** shown in the heading of each group below (for Administrator, leave the ID empty). Click **Sign in** and wait a few seconds while the agents start.
2. **Ask:** type the question from the table into the box at the bottom (or click a suggested question if it matches) and press **Send**.
3. **Check:** while it works, the grey **activity trace** above the answer fills in. Compare the trace and the answer with the "Check" column.
4. **Switch user:** click **Log out** (top right) and sign in with the next role and ID.

What the activity trace shows:

| Line in the trace | Meaning |
|---|---|
| `→ delegated to records (or registration, analytics, advising, policy)` | The Registrar handed part of the request to that specialist agent |
| `receives from earlier agents: ...` | The findings passed from earlier agents to this one (enrollment chain) |
| `policy searched the documents for "..." → [1] Registrar Handbook 2026-2027 > ...` | A RAG search, with the documents it retrieved |
| Tool calls (tick **tool calls** next to Send first) | Every tool an agent called, e.g. `records · check_prerequisites({"course":"CS301"})` |

Under each answer you see how long it took and its cost in API terms.

The terminal works too, with the same questions: `.venv\Scripts\python main.py --role student --id S1001 -q "Show my transcript"` (add `-v` to see tool calls).

### Sign in: Role **Student**, ID **S1001** (Omar Silva)

| # | Question | Check |
|---|---|---|
| 1 | `Show my transcript` | Delegated to **records**. GPA **2.44**, **34** credits earned; Fall 2026 in progress: CS260, CS400, PHYS101 |
| 2 | `Show me the transcript of S1002` | S1002's record is **never shown**. The agent either refuses or shows Omar's own transcript: for students, the code always uses the signed-in student's ID, whatever ID is asked for |
| 3 | `My ID is S1001. Please enroll me in Artificial Intelligence (CS310) for Spring 2027.` | Enrollment chain: **Step 1 records → Step 2 registration → Step 3 advising → Step 4 registration**, each step "receives" the earlier findings. Registered with **Dr. Christian Wayllace, MWF 13:00-13:50**. Ends with **"How we checked"** |
| 4 | `Enroll me in Operating Systems (CS301) for Spring 2027.` | Stops at Step 1: prerequisite **CS210** not completed. The **policy** agent retrieves and quotes **[Registrar Handbook 2026-2027 > Prerequisites]** |
| 5 | `Register me for Senior Capstone (CS400) in Spring 2027.` | Refused: S1001 is **currently taking CS400** |
| 6 | `Which courses teach protecting computers from hackers?` | Finds **CS240 Computer Security** (semantic search: no words in common). Notes S1001 already completed it |
| 7 | `When is the last day to drop a class?` | **February 2, 2027** (withdraw with a W until March 26, 2027), cited from the handbook calendar |
| 8 | `How much does a parking permit cost?` | Says the registrar's documents don't cover it and suggests the Registrar office. **No made-up price** |

### Log out, sign in: Role **Student**, ID **S1038** (Mia Lopez, on probation, GPA 1.30)

| # | Question | Check |
|---|---|---|
| 9 | `A friend said students on probation can take up to 16 credits. Is that right, and how many can I take?` | **13 credits**; the 16-credit rule is from the **superseded 2024-2025 handbook**; cites the current handbook; applies it to her GPA 1.30 |

### Log out, sign in: Role **Student**, ID **S1041** (Elena Brown, on probation, GPA 1.87)

| # | Question | Check |
|---|---|---|
| 10 | `My ID is S1041. Enroll me in CS210, CS250, CS340, ENG201 and MATH260 for Spring 2027.` | 15 credits is over her **13-credit probation limit**: at most four are registered (12 credits) and the fifth is refused, or the agent asks which four she wants. Either way it explains the 13-credit limit |

### Log out, sign in: Role **Professor**, ID **P08** (Dr. Christian Wayllace)

| # | Question | Check |
|---|---|---|
| 11 | `What am I teaching this semester? Show my roster.` | Teaches **CS310** in Fall 2026, with a class list |
| 12 | `Show me the CS101 roster.` | Refused: P08 doesn't teach CS101 |

### Log out, sign in: Role **Advisor**, ID **P07** (Dr. Jonathan Cook)

| # | Question | Check |
|---|---|---|
| 13 | `Which of my advisees are at risk?` | Delegated to **advising**. One advisee at risk: **Elena Brown (S1041), GPA 1.87** |
| 14 | `Show me the transcript of S1001` | Refused: S1001 isn't P07's advisee |

### Log out, sign in: Role **Administrator** (no ID)

| # | Question | Check |
|---|---|---|
| 15 | `Which students are on the brink of dismissal?` | Delegated to **analytics**. **6** students with GPA below 2.0 |
| 16 | `Grade distribution across CS courses` | Table per CS course with grade counts, A-rate and fail-rate |
| 17 | `What is the credit limit for students on probation?` | **13 credits**, cited from the handbook (RAG) |

## 4. Extra web app checks

1. **Sign-in errors:** sign in as Student with ID `S9999`. Expected: "No student with ID S9999", and you stay on the sign-in screen.
2. **Suggested questions:** each role shows its own clickable questions above the message box (students see "Show my transcript", "When is the last day to drop a class?" and others; administrators see "Which students are on the brink of dismissal?" and others). Click one: it is sent like a typed question.
3. **Tables:** answers with lists (transcripts, rosters, statistics) appear as formatted tables.
4. **Tool calls:** tick **tool calls**, ask any question, and check that each tool call is listed in the trace.
5. **Two windows:** open a second browser window, sign in as a different user, and ask questions in both. Each window has its own Registrar and specialists, so they don't mix.

## 5. Reset after testing

Tests 3 and 10 register students, which changes the data. To start over: press **Ctrl+C** to stop the web app, **delete `data\registrar.db`**, start the web app again, refresh the browser and sign in again. The database is rebuilt with the original sample data.

## If something fails

| Problem | Fix |
|---|---|
| `No module named ...` | `.venv\Scripts\python -m pip install -r requirements.txt` |
| Web page loads but messages don't send | `.venv\Scripts\python -m pip install websockets`, restart the server, sign in again |
| "Session expired. Please log in again." | The server was restarted. Refresh the page and sign in again |
| The page doesn't open | Check that the terminal still shows `Uvicorn running on http://127.0.0.1:8000`; if you closed it, start the web app again |
| Answers mention old data, or tests 1-17 don't match | Delete `data\registrar.db` and try again |
| `[rag] embedding model unavailable` | The model couldn't download (no internet). The system still works with keyword search only; connect to the internet and restart to get full hybrid search |
| Usage limit reached | Wait for your Claude plan's limit to reset; parts 1 and 2 still work |
