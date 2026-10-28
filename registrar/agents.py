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
    "list_advisees": ("List the advisor's advisees with GPA and standing.", _schema({}, []),
                      {"advisor"}, "list_advisees", "advising"),
}

# Advising also needs to read transcripts and the catalog/schedule.
SHARED = {"advising": ["get_transcript", "list_courses", "course_schedule", "find_students"],
          "analytics": ["find_students"]}

SPECIALISTS = {
    "records": "Records Agent. Handles transcripts, GPA, grade lookups and grade submission by professors.",
    "registration": "Registration Agent. Handles course catalog, semester schedules, class rosters, "
                    "and registering/dropping courses.",
    "analytics": "Analytics Agent. Produces statistics: grade distributions for a course or group of courses, "
                 "and student groups (graduating, at risk of dismissal, probation, honors).",
    "advising": "Advising Agent. Audits degree progress and recommends which courses to take next.",
}

POLICY = (f"Policies: current semester is {CURRENT_SEMESTER} (in progress); registration is open for "
          f"{REGISTRATION_SEMESTER}. Degree requires {DEGREE_CREDITS} credits, GPA >= 2.0 and all core courses. "
          f"Max {MAX_CREDITS_PER_SEMESTER} credits per semester. Prerequisites need C- or better. "
          "GPA below 2.0 = at risk of dismissal.")


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
                    f"to work around it. Return a concise, well-organized answer.\n{POLICY}"),
            tools=[full(n) for n in names],
            model="inherit",
        )

    system_prompt = (
        f"You are the Registrar, the front desk of a university registrar office. You are serving "
        f"{_who(user)}.\nYou coordinate specialist agents: "
        + "; ".join(f"'{k}' ({SPECIALISTS[k]})" for k in agents)
        + ".\nDelegate every data question or action to the right specialist with the Agent tool; "
        "for requests spanning several areas, consult several specialists (in parallel when "
        "independent) and merge their answers. Never make up data. Before registering, dropping "
        "or submitting grades, confirm the exact action with the user unless they already clearly "
        f"asked for it. Keep answers clear and brief; use tables for lists.\n{POLICY}"
    )
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=system_prompt,
        mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, tools=tools)},
        agents=agents,
        tools=["Agent"],  # coordinator has no data tools itself: it must delegate
        allowed_tools=["Agent"] + [full(n) for n in available],
        max_turns=30,
    )


def _who(user: User) -> str:
    if user.role == "admin":
        return "an administrator (full access, statistics)"
    return f"{user.name} ({user.role}, ID {user.id})"
