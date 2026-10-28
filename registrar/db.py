"""SQLite database for the registrar: the five tables from the project brief.

Students(ID, Name)  Professors(ID, Name)  Courses(Course, Title, Credits)
Schedule(Course, Semester, Professor)  Grades(Student, Course, Semester, Grade)

A few extra columns make the requested features possible (major/advisor for
advising, prerequisites for registration, capacity/time for schedules).
A Grades row with grade NULL means "enrolled, no grade yet" - that is how
registration is stored.
"""

import random
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "registrar.db"

SEMESTERS = ["Fall 2024", "Spring 2025", "Fall 2025", "Spring 2026", "Fall 2026", "Spring 2027"]
CURRENT_SEMESTER = "Fall 2026"        # in progress: enrolled, grades not posted
REGISTRATION_SEMESTER = "Spring 2027"  # open for registration
COMPLETED_SEMESTERS = SEMESTERS[:4]

GRADE_POINTS = {"A": 4.0, "A-": 3.7, "B+": 3.3, "B": 3.0, "B-": 2.7, "C+": 2.3,
                "C": 2.0, "C-": 1.7, "D+": 1.3, "D": 1.0, "F": 0.0}
PASSING_FOR_PREREQ = 1.7  # C- or better satisfies a prerequisite
MAX_CREDITS_PER_SEMESTER = 18
DEGREE_CREDITS = 42
CORE_COURSES = ["CS101", "CS102", "CS201", "CS220", "CS400", "MATH150", "MATH160", "ENG101"]

SCHEMA = """
CREATE TABLE students (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, major TEXT NOT NULL,
    advisor_id TEXT REFERENCES professors(id), entry_semester TEXT NOT NULL);
CREATE TABLE professors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL);
CREATE TABLE courses (
    code TEXT PRIMARY KEY, title TEXT NOT NULL, credits INTEGER NOT NULL,
    prerequisites TEXT NOT NULL DEFAULT '');  -- comma-separated course codes
CREATE TABLE schedule (
    course TEXT NOT NULL REFERENCES courses(code), semester TEXT NOT NULL,
    professor_id TEXT NOT NULL REFERENCES professors(id),
    days TEXT NOT NULL, time TEXT NOT NULL, room TEXT NOT NULL, capacity INTEGER NOT NULL,
    PRIMARY KEY (course, semester));
CREATE TABLE grades (
    student_id TEXT NOT NULL REFERENCES students(id), course TEXT NOT NULL,
    semester TEXT NOT NULL, grade TEXT,  -- NULL = enrolled, not graded yet
    PRIMARY KEY (student_id, course, semester),
    FOREIGN KEY (course, semester) REFERENCES schedule(course, semester));
"""

PROFESSORS = [
    ("P01", "Dr. Alan Turing", "CS"), ("P02", "Dr. Grace Hopper", "CS"),
    ("P03", "Dr. Barbara Liskov", "CS"), ("P04", "Dr. Emmy Noether", "MATH"),
    ("P05", "Dr. John Nash", "MATH"), ("P06", "Dr. Toni Morrison", "ENG"),
    ("P07", "Dr. Richard Feynman", "PHYS"),
]

# code, title, credits, prerequisites, professor
COURSES = [
    ("CS101", "Introduction to Programming", 3, "", "P02"),
    ("CS102", "Data Structures", 3, "CS101", "P02"),
    ("CS201", "Algorithms", 3, "CS102,MATH150", "P01"),
    ("CS210", "Computer Systems", 3, "CS102", "P03"),
    ("CS220", "Databases", 3, "CS102", "P03"),
    ("CS301", "Operating Systems", 3, "CS210", "P03"),
    ("CS310", "Artificial Intelligence", 3, "CS201", "P01"),
    ("CS320", "Machine Learning", 3, "CS310,MATH250", "P01"),
    ("CS400", "Senior Capstone", 3, "CS201,CS220", "P02"),
    ("MATH150", "Discrete Mathematics", 3, "", "P04"),
    ("MATH160", "Calculus I", 4, "", "P05"),
    ("MATH250", "Linear Algebra", 3, "MATH160", "P04"),
    ("MATH260", "Probability and Statistics", 3, "MATH160", "P05"),
    ("ENG101", "Academic Writing", 3, "", "P06"),
    ("ENG201", "Technical Writing", 3, "ENG101", "P06"),
    ("PHYS101", "Physics I", 4, "MATH160", "P07"),
]

FIRST = ["Ava", "Liam", "Noah", "Emma", "Olivia", "Mason", "Sophia", "Lucas", "Mia", "Ethan",
         "Amir", "Fatima", "Wei", "Priya", "Diego", "Yuki", "Omar", "Zara", "Kofi", "Elena"]
LAST = ["Smith", "Khan", "Garcia", "Chen", "Patel", "Nguyen", "Okafor", "Silva", "Kim", "Ali",
        "Brown", "Haddad", "Rossi", "Tanaka", "Lopez", "Ivanova", "Mensah", "Park", "Wahab", "Cohen"]
SLOTS = [("MWF", "09:00-09:50"), ("MWF", "10:00-10:50"), ("MWF", "11:00-11:50"), ("MWF", "13:00-13:50"),
         ("TTh", "09:30-10:45"), ("TTh", "11:00-12:15"), ("TTh", "14:00-15:15"), ("TTh", "15:30-16:45")]


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    fresh = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        seed(conn)
    return conn


def _grade_for(ability: float, rng: random.Random) -> str:
    score = ability + rng.gauss(0, 0.6)
    for cutoff, grade in [(3.85, "A"), (3.5, "A-"), (3.2, "B+"), (2.85, "B"), (2.5, "B-"),
                          (2.15, "C+"), (1.85, "C"), (1.5, "C-"), (1.15, "D+"), (0.8, "D")]:
        if score >= cutoff:
            return grade
    return "F"


def seed(conn: sqlite3.Connection, n_students: int = 60, seed_value: int = 42) -> None:
    rng = random.Random(seed_value)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO professors VALUES (?,?,?)", PROFESSORS)
    conn.executemany("INSERT INTO courses VALUES (?,?,?,?)", [c[:4] for c in COURSES])
    prereqs = {c[0]: [p for p in c[3].split(",") if p] for c in COURSES}
    credits = {c[0]: c[2] for c in COURSES}

    for sem in SEMESTERS:
        for i, (code, *_rest, prof) in enumerate(COURSES):
            days, time = SLOTS[(i + SEMESTERS.index(sem)) % len(SLOTS)]
            conn.execute("INSERT INTO schedule VALUES (?,?,?,?,?,?,?)",
                         (code, sem, prof, days, time, f"Hall {100 + i}", 35))

    cs_advisors = [p[0] for p in PROFESSORS if p[2] == "CS"]
    names = rng.sample([f"{f} {l}" for f in FIRST for l in LAST], n_students)
    enrolled_count: dict[tuple[str, str], int] = {}
    for n in range(n_students):
        sid = f"S{1001 + n}"
        entry = rng.choice(COMPLETED_SEMESTERS)
        # ability: most students do fine, ~15% struggle (feeds the at-risk report)
        ability = rng.uniform(0.6, 1.9) if rng.random() < 0.15 else rng.uniform(2.3, 4.0)
        conn.execute("INSERT INTO students VALUES (?,?,?,?,?)",
                     (sid, names[n], "Computer Science", rng.choice(cs_advisors), entry))

        passed: set[str] = set()
        for sem in SEMESTERS[SEMESTERS.index(entry):SEMESTERS.index(REGISTRATION_SEMESTER)]:
            eligible = [c for c in credits if c not in passed and all(p in passed for p in prereqs[c])]
            rng.shuffle(eligible)
            # prefer core courses first, like a real student
            eligible.sort(key=lambda c: c not in CORE_COURSES)
            for code in eligible[:rng.choice([3, 4, 4, 5])]:
                enrolled_count[(code, sem)] = enrolled_count.get((code, sem), 0) + 1
                grade = None if sem == CURRENT_SEMESTER else _grade_for(ability, rng)
                conn.execute("INSERT INTO grades VALUES (?,?,?,?)", (sid, code, sem, grade))
                if grade and GRADE_POINTS[grade] >= PASSING_FOR_PREREQ:
                    passed.add(code)
                elif sem == CURRENT_SEMESTER:
                    passed.add(code)  # assume in-progress courses will pass, for planning
    conn.commit()
