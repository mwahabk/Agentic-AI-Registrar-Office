"""Multi-agent setup: a Registrar coordinator delegating to specialist subagents.

            ┌──────────────── Registrar (coordinator) ────────────────┐
            │  routes each request, combines answers                  │
            └───┬──────────────┬───────────────┬───────────────┬──────┘
           Records       Registration      Analytics        Advising
       (transcripts,    (schedules, add/  (grade stats,    (degree progress,
        grades)          drop, rosters)    student groups)  course planning)

Each subagent only gets the tools of its specialty, and the tool set is
further filtered by the logged-in user's role. Permission checks inside
Registrar (service.py) are the final authority.
"""

import json
import sqlite3
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, create_sdk_mcp_server, tool

from .db import CURRENT_SEMESTER, DEGREE_CREDITS, MAX_CREDITS_PER_SEMESTER, REGISTRATION_SEMESTER, SEMESTERS
from .service import PermissionDenied, Registrar, RegistrarError, User

SERVER = "registrar"
# Lightest model, to save Pro-plan usage. For the best answers (e.g. a final demo),
# switch to "claude-sonnet-5-5" or "claude-opus-5-5".
MODEL = "claude-haiku-5-5"


def _schema(props: dict[str, tuple[str, str]], required: list[str]) -> dict:
    """props: name -> (json type, description)."""
    return {"type": "object",
            "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            "required": required}


SEM = ("string", f"Semester, one of {SEMESTERS}")
SID = ("string", "Student ID like S1001 (ignored for students: they always act on themselves)")

# name -> (description, input schema, roles allowed, Registrar method, specialist)
TOOL_SPECS: dict[str, tuple[str, dict, set[str], str, str]] = {
    "list_courses": ("Search the course catalog (code, title, credits, prerequisites).",
                     _schema({"query": ("string", "Text in code or title; empty for all")}, []),
                     {"student", "professor", "advisor", "admin"}, "list_courses", "registration"),
    "course_schedule": ("Course offerings for a semester: professor, days/time, room, seats.",
                        _schema({"semester": SEM, "course": ("string", "Optional course code")}, ["semester"]),
                        {"student", "professor", "advisor", "admin"}, "course_schedule", "registration"),
    "student_schedule": ("A student's class schedule for a semester.",
                         _schema({"student_id": SID, "semester": SEM}, []),
                         {"student", "advisor", "admin"}, "student_schedule", "registration"),
    "check_enrollment": (f"Check, without registering, whether a student can enroll in a course in a semester "
                         f"(default {REGISTRATION_SEMESTER}): open seats, time clashes with their other courses "
                         "(overlapping times count), and the credit limit.",
                         _schema({"course": ("string", "Course code"), "semester": SEM, "student_id": SID},
                                 ["course"]),
                         {"student", "advisor", "admin"}, "check_enrollment", "registration"),
    "register_course": (f"Register a student for a course (only {REGISTRATION_SEMESTER} is open). "
                        "Checks prerequisites, capacity, time conflicts and credit limit.",
                        _schema({"course": ("string", "Course code"), "semester": SEM, "student_id": SID},
                                ["course"]),
                        {"student", "admin"}, "register", "registration"),
    "drop_course": ("Drop a course from the current or upcoming semester.",
                    _schema({"course": ("string", "Course code"), "semester": SEM, "student_id": SID},
                            ["course"]),
                    {"student", "admin"}, "drop", "registration"),
    "professor_schedule": ("Courses a professor teaches in a semester, with enrollment counts.",
                           _schema({"semester": SEM, "professor_id": ("string", "Admins only, e.g. P01")}, []),
                           {"professor", "advisor", "admin"}, "professor_schedule", "registration"),
    "class_roster": ("Students enrolled in a course section (professors: own sections only).",
                     _schema({"course": ("string", "Course code"), "semester": SEM}, ["course"]),
                     {"professor", "advisor", "admin"}, "class_roster", "registration"),
    "check_prerequisites": ("Check whether a student has completed every prerequisite of a course with C- or "
                            "better, and whether they already completed the course. Shows each prerequisite's status.",
                            _schema({"course": ("string", "Course code"), "student_id": SID}, ["course"]),
                            {"student", "advisor", "admin"}, "check_prerequisites", "records"),
    "get_transcript": ("Full transcript: grades per term, term and cumulative GPA, credits.",
                       _schema({"student_id": SID}, []),
                       {"student", "advisor", "admin"}, "transcript", "records"),
    "submit_grade": (f"Record a final grade for a student in a course you teach ({CURRENT_SEMESTER}).",
                     _schema({"student_id": ("string", "Student ID"), "course": ("string", "Course code"),
                              "grade": ("string", "Letter grade, e.g. A, B+, F"), "semester": SEM},
                             ["student_id", "course", "grade"]),
                     {"professor", "advisor"}, "submit_grade", "records"),
    "find_students": ("Look up students by ID or name.",
                      _schema({"query": ("string", "ID or part of a name")}, ["query"]),
                      {"advisor", "admin"}, "find_students", "records"),
    "course_grade_stats": ("Grade distribution, average GPA, A-rate and fail-rate for one course.",
                           _schema({"course": ("string", "Course code"),
                                    "semester": ("string", "Optional semester; omit for all")}, ["course"]),
                           {"professor", "advisor", "admin"}, "course_grade_stats", "analytics"),
    "grade_distribution_report": ("Statistics for a group of courses (e.g. prefix 'CS', 'MATH'): "
                                  "per-course distributions, courses with many Fs or many As.",
                                  _schema({"course_prefix": ("string", "e.g. CS, MATH; empty = all"),
                                           "semester": ("string", "Optional semester")}, []),
                                  {"admin"}, "grade_distribution_report", "analytics"),
    "student_standing_report": ("Groups of students: 'graduating', 'at_risk' (GPA<2.0, brink of dismissal), "
                                "'probation_watch' (2.0-2.3), 'honors' (>=3.5).",
                                _schema({"category": ("string", "graduating | at_risk | probation_watch | honors")},
                                        ["category"]),
                                {"admin"}, "student_standing_report", "analytics"),
    "degree_progress": ("Degree audit: credits, GPA, standing, core courses remaining, "
                        "courses the student is eligible to take next.",
                        _schema({"student_id": SID}, []),
                        {"student", "advisor", "admin"}, "degree_progress", "advising"),
    "search_knowledge": ("Search the registrar's documents: the 2026-2027 handbook (rules, deadlines, credit limits, "
                         "probation, repeats, graduation, privacy), the course catalog with course descriptions, "
                         "and the advising guide. Returns the most relevant passages with their source, effective "
                         "date and whether they are current or superseded. Use plain questions or key terms; search "
                         "again with different wording if the results don't answer the question.",
                         _schema({"query": ("string", "What to look up, e.g. 'credit limit on probation'"),
                                  "top_k": ("integer", "How many passages to return (default 4, max 8)")},
                                 ["query"]),
                         {"student", "professor", "advisor", "admin"}, "search_knowledge", "policy"),
    "list_advisees": ("List the advisor's advisees with GPA and standing.", _schema({}, []),
                      {"advisor"}, "list_advisees", "advising"),
}

# Advising also needs to read transcripts and the catalog/schedule.
SHARED = {"advising": ["get_transcript", "list_courses", "course_schedule", "find_students", "search_knowledge"],
          "analytics": ["find_students"]}

SPECIALISTS = {
    "records": "Records Agent. Handles transcripts, GPA, prerequisite checks, grade lookups and grade submission by professors.",
    "registration": "Registration Agent. Handles course catalog, semester schedules, class rosters, "
                    "and registering/dropping courses.",
    "analytics": "Analytics Agent. Produces statistics: grade distributions for a course or group of courses, "
                 "and university-wide student groups (graduating, at risk of dismissal, probation, honors). "
                 "For an advisor's own students, use the Advising Agent instead.",
    "advising": "Advising Agent. Lists an advisor's advisees with GPA and standing (including who is at risk), "
                "audits degree progress and recommends which courses to take next.",
    "policy": "Policy Agent. Answers questions about registrar rules, deadlines, procedures and what courses "
              "cover, by retrieving passages from the handbook, course catalog and advising guide (RAG), "
              "with citations.",
}

# Agentic RAG (Chapter 14): the Policy agent doesn't just pass retrieved text along. It judges it.
POLICY_AGENT_RULES = """
How to answer (retrieval-augmented generation):
1. Search with search_knowledge before answering. For a question with several parts, search each part separately.
2. Judge the results. If none of them actually answers the question, search once more with different wording
   (for example 'repeat' instead of 'retake', or the course code instead of its topic).
3. Check every passage's status. A SUPERSEDED passage no longer applies. If it disagrees with a CURRENT passage,
   follow the current one and say briefly that the older rule was replaced.
4. Answer only from the retrieved passages. Cite each fact with its source in square brackets, for example
   [Registrar Handbook 2026-2027 > Credit load and overloads].
5. If the search says NO RELEVANT DOCUMENTS, or the passages don't cover the question, say the registrar's
   documents don't cover it and suggest contacting the Registrar office. Never fill the gap from memory.
6. You explain rules; you never apply them to a student's record or change anything. Record checks and
   registration belong to the other agents."""

POLICY = (f"Policies: current semester is {CURRENT_SEMESTER} (in progress); registration is open for "
          f"{REGISTRATION_SEMESTER}. Degree requires {DEGREE_CREDITS} credits, GPA >= 2.0 and all core courses. "
          f"Max {MAX_CREDITS_PER_SEMESTER} credits per semester. Prerequisites must be completed with C- or better; in-progress courses do not count. "
          "GPA below 2.0 = academic probation and at risk of dismissal, limited to 13 credits per semester; "
          "GPA 2.0 to 2.3 = probation watch; 3.5 or higher = honors. More than 18 credits is an overload that "
          "needs written permission. Only courses with a D or F can be repeated, and a repeated course counts "
          "once in GPA and credits, with its latest grade. A student can't register for a course they are taking now. "
          "Every passed course, core or elective, counts toward the credit total.")


# Enrollment is the showcase of agents working together: each specialist does one step and its
# findings are handed to the next one, so later agents build on what earlier ones found.
ENROLLMENT_WORKFLOW = """

ENROLLMENT WORKFLOW. When the user asks to enroll a student in a course, the specialists work as a chain.
Call them one at a time, in this order, and start every prompt after the first with a line
"Findings so far:" followed by a short summary of what the earlier agents found, so each agent builds on
the others' work. Describe each delegation as "Step N: <what this agent checks>".
  Step 1. records: run check_prerequisites for the course. If the course was already completed or a
          prerequisite isn't satisfied, stop here and explain which one and why.
  Step 2. registration: with the Records findings, run check_enrollment (open seats, time clashes, credit
          limit). If there is a problem, stop and suggest alternatives (another course, or what to drop).
  Step 3. advising: with the findings so far, run degree_progress and say whether the course counts toward
          the degree and how many credits remain afterwards.
  Step 4. registration: if all checks passed and the user clearly asked to enroll, register_course.
          Otherwise ask the user to confirm first.
If a step stops the chain, also ask the policy agent to find the handbook rule behind the refusal, and quote
it with its [source] citation, so the student sees the official rule and not only the error.
End the reply with a short "How we checked" list: one line per agent saying what it found."""


def build_tools(registrar: Registrar):
    """Create MCP tools bound to this user's Registrar session, filtered by role."""
    tools = []
    for name, (desc, schema, roles, method, _) in TOOL_SPECS.items():
        if registrar.user.role not in roles:
            continue

        def make(method=method):
            async def handler(args: dict[str, Any]) -> dict:
                try:
                    result = getattr(registrar, method)(**{k: v for k, v in args.items() if v not in (None, "")})
                except (PermissionDenied, RegistrarError) as e:
                    return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "is_error": True}
                text = result if isinstance(result, str) else json.dumps(result, indent=1)
                return {"content": [{"type": "text", "text": text}]}
            return handler

        tools.append(tool(name, desc, schema)(make()))
    return tools


def build_options(conn: sqlite3.Connection, user: User) -> ClaudeAgentOptions:
    registrar = Registrar(conn, user)
    tools = build_tools(registrar)
    available = {t.name for t in tools}
    full = lambda n: f"mcp__{SERVER}__{n}"  # noqa: E731

    agents = {}
    for spec, desc in SPECIALISTS.items():
        names = [n for n, s in TOOL_SPECS.items() if s[4] == spec] + SHARED.get(spec, [])
        names = [n for n in dict.fromkeys(names) if n in available]
        if not names:
            continue  # this role has nothing to do with this specialist
        agents[spec] = AgentDefinition(
            description=desc,
            prompt=(f"You are the {desc} You work for a university registrar office, serving "
                    f"{_who(user)}. Use your tools to get facts; never invent records. If a tool "
                    "returns PermissionDenied, explain that the user is not authorized; do not try "
                    f"to work around it. Return a concise, well-organized answer.\n{POLICY}"
                    + (POLICY_AGENT_RULES if spec == "policy" else "")),
            tools=[full(n) for n in names],
            model="inherit",
            background=False,  # coordinator must wait for the specialist's answer
        )

    system_prompt = (
        f"You are the Registrar, the front desk of a university registrar office. You are serving "
        f"{_who(user)}.\nYou coordinate specialist agents: "
        + "; ".join(f"'{k}' ({SPECIALISTS[k]})" for k in agents)
        + ".\nDelegate every data question or action to the right specialist with the Agent tool; "
        "for requests spanning several areas, consult several specialists (in parallel when "
        "independent) and merge their answers. Always wait for every specialist's result and give the full answer in the "
        "same reply; never run specialists in the background or promise to report back later. Never make up data, and don't answer questions about records, schedules, eligibility or "
        "whether an action is allowed from memory or earlier replies: ask a specialist each time, because data "
        "changes and only the tools apply the rules. Before registering, dropping "
        "or submitting grades, confirm the exact action with the user unless they already clearly "
        f"asked for it. Keep answers clear and brief; use tables for lists.\n{POLICY}"
        "\nQuestions about rules, deadlines, procedures, privacy or what a course covers go to the 'policy' "
        "agent, which looks them up in the registrar's documents. Keep its [source] citations in your answer."
    )
    if {"records", "registration", "advising"} <= agents.keys():
        system_prompt += ENROLLMENT_WORKFLOW
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=system_prompt,
        mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, tools=tools)},
        agents=agents,
        tools=["Agent"],  # coordinator has no data tools itself: it must delegate
        allowed_tools=["Agent"] + [full(n) for n in available],
        max_turns=30,
        # specialists must finish before the coordinator replies, not run in the background
        env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
        # don't load the user's own Claude Code settings, MCP servers or CLAUDE.md into the agents
        setting_sources=[],
        strict_mcp_config=True,
    )


def _who(user: User) -> str:
    if user.role == "admin":
        return "an administrator (full access, statistics)"
    return f"{user.name} ({user.role}, ID {user.id})"


def handoff_note(prompt: str) -> str:
    """The findings the coordinator passes from earlier agents to this one, if any."""
    marker = "Findings so far:"
    if marker not in prompt:
        return ""
    note = " ".join(prompt.split(marker, 1)[1].split())
    return note[:280] + ("..." if len(note) > 280 else "")


def retrieval_sources(content) -> str:
    """The SOURCES line of a search_knowledge result (or the knowledge-gap notice), for display."""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    for line in (content or "").splitlines():
        if line.startswith("SOURCES: ") or line.startswith("NO RELEVANT DOCUMENTS"):
            return line.removeprefix("SOURCES: ")
    return ""
