"""Tests for the registrar data layer and access control (no API calls)."""

import pytest

from registrar.agents import build_options, build_tools
from registrar.db import CURRENT_SEMESTER, REGISTRATION_SEMESTER, connect
from registrar.service import PermissionDenied, Registrar, RegistrarError, User


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


def as_(conn, role, uid=None):
    return Registrar(conn, Registrar.login(conn, role, uid))


def advisor_and_advisee(conn):
    row = conn.execute("SELECT id, advisor_id FROM students LIMIT 1").fetchone()
    return row["advisor_id"], row["id"]


def test_seed_has_five_core_tables(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"students", "grades", "schedule", "courses", "professors"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 60


def test_student_sees_only_own_transcript(conn):
    s = as_(conn, "student", "S1001")
    assert s.transcript()["id"] == "S1001"
    assert s.transcript("S1002")["id"] == "S1001"  # student_id is ignored for students
    with pytest.raises(PermissionDenied):
        s.grade_distribution_report("CS")


def test_advisor_scoped_to_advisees(conn):
    adv_id, advisee = advisor_and_advisee(conn)
    a = as_(conn, "advisor", adv_id)
    assert a.degree_progress(advisee)["student_id"] == advisee
    other = conn.execute("SELECT id FROM students WHERE advisor_id != ?", (adv_id,)).fetchone()["id"]
    with pytest.raises(PermissionDenied):
        a.transcript(other)


def test_professor_roster_and_grades_only_own_sections(conn):
    p = as_(conn, "professor", "P02")  # teaches CS101
    assert isinstance(p.class_roster("CS101", CURRENT_SEMESTER), list)
    with pytest.raises(PermissionDenied):
        p.class_roster("CS201", CURRENT_SEMESTER)  # taught by P01
    enrolled = conn.execute("SELECT student_id FROM grades WHERE course='CS101' AND semester=? "
                            "AND grade IS NULL", (CURRENT_SEMESTER,)).fetchone()
    if enrolled:
        assert "Recorded" in p.submit_grade(enrolled[0], "CS101", "B+")
    with pytest.raises(RegistrarError):
        p.submit_grade("S1001", "CS101", "Z")


def test_registration_rules(conn):
    s = as_(conn, "student", "S1001")
    done = {r["course"] for r in s._student_rows("S1001")}
    eligible = [c["course"] for c in s.degree_progress()["eligible_next_courses"]]
    if eligible:
        assert "Registered" in s.register(eligible[0])
        with pytest.raises(RegistrarError, match="Already registered"):
            s.register(eligible[0])
        assert "Dropped" in s.drop(eligible[0])
    with pytest.raises(RegistrarError, match="only open"):
        s.register("CS101", "Fall 2025")
    blocked = next((c for c in ("CS320", "CS400", "CS310") if c not in done and c not in eligible), None)
    if blocked:
        with pytest.raises(RegistrarError, match="prerequisites"):
            s.register(blocked, REGISTRATION_SEMESTER)


def test_admin_reports(conn):
    a = as_(conn, "admin")
    rep = a.grade_distribution_report("CS")
    assert rep["overall"]["graded"] > 0 and all(c.startswith("CS") for c in rep["per_course"])
    at_risk = a.student_standing_report("at_risk")
    assert all(s["gpa"] < 2.0 for s in at_risk)
    assert a.student_standing_report("graduating")  # seed data includes near-graduates


def test_login_rejects_unknown_ids(conn):
    with pytest.raises(PermissionDenied):
        Registrar.login(conn, "student", "S9999")
    with pytest.raises(PermissionDenied):
        Registrar.login(conn, "advisor", "P12")  # English professor advises no one


def test_tools_filtered_by_role(conn):
    names = lambda role, uid=None: {t.name for t in build_tools(as_(conn, role, uid))}  # noqa: E731
    assert "student_standing_report" not in names("student", "S1001")
    assert "submit_grade" in names("professor", "P01")
    assert "student_standing_report" in names("admin")
    opts = build_options(conn, Registrar.login(conn, "student", "S1001"))
    assert "analytics" not in opts.agents  # students get no analytics specialist


async def test_tool_returns_error_on_denied(conn):
    tools = {t.name: t for t in build_tools(as_(conn, "professor", "P02"))}
    res = await tools["class_roster"].handler({"course": "CS201"})
    assert res["is_error"] and "PermissionDenied" in res["content"][0]["text"]


def test_in_progress_course_does_not_satisfy_prerequisite(conn):
    """A course still in progress (no grade yet) must not count as a completed prerequisite."""
    row = conn.execute(
        """SELECT g.student_id, c.code FROM grades g JOIN courses c
           ON (',' || c.prerequisites || ',') LIKE ('%,' || g.course || ',%')
           WHERE g.semester = ? AND g.grade IS NULL LIMIT 1""", (CURRENT_SEMESTER,)).fetchone()
    assert row, "seed data should have a student taking a prerequisite this semester"
    s = as_(conn, "student", row["student_id"])
    with pytest.raises(RegistrarError, match="in progress"):
        s.register(row["code"])
    assert row["code"] not in [c["course"] for c in s.degree_progress()["eligible_next_courses"]]


def test_overlap_detection():
    from registrar.service import _overlaps
    assert _overlaps("MWF", "09:00-09:50", "MW", "09:30-10:45")      # shared days, partly overlapping
    assert not _overlaps("MWF", "09:00-09:50", "MWF", "10:00-10:50")  # back to back
    assert not _overlaps("MWF", "09:00-09:50", "TTh", "09:00-10:15")  # different days
    assert _overlaps("TTh", "11:00-12:15", "Th", "12:00-13:00")       # Thursday only


def test_partial_time_overlap_blocks_enrollment(conn):
    """A section that only partly overlaps a course already on the schedule must be reported as a clash."""
    a = Registrar(conn, User("admin"))
    for (sid,) in conn.execute("SELECT id FROM students").fetchall():
        eligible = [e["course"] for e in a.degree_progress(sid)["eligible_next_courses"]]
        if len(eligible) >= 2:
            break
    first, second = eligible[:2]
    a.register(first, student_id=sid)
    sec = conn.execute("SELECT days, time FROM schedule WHERE course = ? AND semester = ?",
                       (first, REGISTRATION_SEMESTER)).fetchone()
    start_h = int(sec["time"][:2])
    shifted = f"{start_h:02d}:20-{start_h + 1:02d}:35"  # starts 20 minutes into the first course
    conn.execute("UPDATE schedule SET days = ?, time = ? WHERE course = ? AND semester = ?",
                 (sec["days"], shifted, second, REGISTRATION_SEMESTER))
    chk = a.check_enrollment(second, student_id=sid)
    assert not chk["can_enroll"] and first in chk["time_clashes"][0]
    with pytest.raises(RegistrarError, match="Time conflict"):
        a.register(second, student_id=sid)


def test_cs_courses_taught_by_cs_faculty(conn):
    rows = conn.execute("""SELECT s.course, p.department FROM schedule s JOIN professors p
                           ON p.id = s.professor_id WHERE s.course LIKE 'CS%'""").fetchall()
    assert rows and all(r["department"] == "CS" for r in rows)
    advisors = {r[0] for r in conn.execute("SELECT DISTINCT advisor_id FROM students")}
    cs = {r[0] for r in conn.execute("SELECT id FROM professors WHERE department = 'CS'")}
    assert advisors <= cs


def test_sample_data_leaves_students_courses_to_register_for(conn):
    """The demo needs most students to have something to enroll in next semester, and no double-booked teachers."""
    a = Registrar(conn, User("admin"))
    counts = [len(a.degree_progress(sid)["eligible_next_courses"])
              for (sid,) in conn.execute("SELECT id FROM students").fetchall()]
    assert sum(1 for n in counts if n >= 2) >= 0.75 * len(counts)
    assert not conn.execute("""SELECT 1 FROM schedule GROUP BY semester, professor_id, days, time
                               HAVING COUNT(*) > 1""").fetchone()


def test_two_students_cannot_both_take_the_last_seat(tmp_path):
    """Two simultaneous registrations for the last seat: exactly one succeeds, the course is never overbooked."""
    import demo_last_seat as demo
    db_path = tmp_path / "race.db"
    conn = connect(db_path)
    course, students = demo.pick_course_and_students(conn)
    demo.course = course
    demo.leave_one_seat(conn, course, students)
    Registrar.check_enrollment, original = demo.slow_check, Registrar.check_enrollment
    try:
        results = demo.race(db_path, course, students, demo.with_lock)
    finally:
        Registrar.check_enrollment = original
    assert sum(r.startswith("GOT THE SEAT") for r in results.values()) == 1
    taken, cap = demo.seats(conn, course)
    assert taken == cap


# ---- registration rules, each checked with a test student built for it ----------------------------

def make_student(conn, sid, history, spring=()):
    """A test student with an exact course history: (course, semester, grade or None for in progress).
    `spring` lists courses already on their Spring 2027 schedule (added directly, to set up a credit load)."""
    conn.execute("INSERT INTO students VALUES (?,?,?,?,?)", (sid, "Test Student", "Computer Science", "P01", "Fall 2024"))
    for course, semester, grade in history:
        conn.execute("INSERT INTO grades VALUES (?,?,?,?)", (sid, course, semester, grade))
    for course in spring:
        conn.execute("INSERT INTO grades VALUES (?,?,?,NULL)", (sid, course, REGISTRATION_SEMESTER))
    conn.commit()
    return Registrar(conn, User("admin"))


def test_prerequisite_needs_c_minus_or_better(conn):
    a = make_student(conn, "S9001", [("CS101", "Fall 2025", "D")])
    with pytest.raises(RegistrarError, match="below C-"):
        a.register("CS102", student_id="S9001")
    a = make_student(conn, "S9002", [("CS101", "Fall 2025", "C-")])
    assert "Registered" in a.register("CS102", student_id="S9002")


def test_repeated_course_counts_once_with_latest_grade(conn):
    a = make_student(conn, "S9003", [("CS101", "Fall 2024", "D"), ("CS101", "Spring 2025", "B"),
                                     ("ENG101", "Fall 2024", "A")])
    t = a.transcript("S9003")
    assert t["credits_earned"] == 6          # CS101 counted once, not twice
    assert t["cumulative_gpa"] == 3.5        # (B 3.0 x 3 + A 4.0 x 3) / 6; the earlier D is left out
    first_try = t["terms"][0]["courses"]
    assert any("not counted" in c["grade"] for c in first_try if c["course"] == "CS101")


def test_only_courses_with_d_or_f_can_be_repeated(conn):
    a = make_student(conn, "S9004", [("CS101", "Fall 2025", "C")])
    with pytest.raises(RegistrarError, match="already completed"):
        a.register("CS101", student_id="S9004")
    a = make_student(conn, "S9005", [("CS101", "Fall 2025", "F"), ("ENG101", "Fall 2025", "A")])
    assert "Registered" in a.register("CS101", student_id="S9005")


def test_cannot_register_for_a_course_being_taken_now(conn):
    a = make_student(conn, "S9006", [("CS101", CURRENT_SEMESTER, None)])
    with pytest.raises(RegistrarError, match="currently taking"):
        a.register("CS101", student_id="S9006")


def test_probation_limits_registration_to_13_credits(conn):
    # GPA 1.0 (one D) = academic probation; 11 credits already on the Spring schedule, adding 3 makes 14
    a = make_student(conn, "S9007", [("ENG101", "Fall 2025", "D")], spring=["MATH150", "MATH160", "PHYS101"])
    with pytest.raises(RegistrarError, match="academic probation"):
        a.register("CS101", student_id="S9007")
    # the same 14 credits are fine for a student in good standing
    a = make_student(conn, "S9008", [("ENG101", "Fall 2025", "A")], spring=["MATH150", "MATH160", "PHYS101"])
    problems = a.check_enrollment("CS101", student_id="S9008")["problems"]
    assert not any("probation" in p or "Credit limit" in p for p in (problems if problems != "none" else []))


def test_more_than_18_credits_is_an_overload(conn):
    a = make_student(conn, "S9009", [("ENG101", "Fall 2025", "A")],
                     spring=["MATH150", "MATH160", "PHYS101", "CS102", "CS201"])  # 17 credits
    with pytest.raises(RegistrarError, match="overload"):
        a.register("CS101", student_id="S9009")


def test_full_course_is_refused(conn):
    a = make_student(conn, "S9010", [])
    taken = conn.execute("SELECT COUNT(*) FROM grades WHERE course='CS101' AND semester=?",
                         (REGISTRATION_SEMESTER,)).fetchone()[0]
    conn.execute("UPDATE schedule SET capacity=? WHERE course='CS101' AND semester=?", (taken, REGISTRATION_SEMESTER))
    with pytest.raises(RegistrarError, match="is full"):
        a.register("CS101", student_id="S9010")


def test_drop_rules(conn):
    a = make_student(conn, "S9011", [("ENG101", "Fall 2025", "A")])
    a.register("CS101", student_id="S9011")
    assert "Dropped" in a.drop("CS101", student_id="S9011")
    with pytest.raises(RegistrarError, match="not enrolled"):
        a.drop("CS101", student_id="S9011")
    with pytest.raises(RegistrarError, match="Only current or upcoming"):
        a.drop("ENG101", "Fall 2025", student_id="S9011")  # a finished, graded course


def test_only_students_and_admins_can_register(conn):
    advisor_id, advisee = advisor_and_advisee(conn)
    with pytest.raises(PermissionDenied):
        as_(conn, "advisor", advisor_id).register("CS101", student_id=advisee)
    with pytest.raises(PermissionDenied):
        as_(conn, "professor", "P02").register("CS101", student_id="S1001")


def test_grade_submission_rules(conn):
    p = as_(conn, "professor", "P02")  # teaches CS101
    make_student(conn, "S9012", [("CS101", CURRENT_SEMESTER, None)])
    assert "Recorded" in p.submit_grade("S9012", "CS101", "A-")
    with pytest.raises(RegistrarError, match="not enrolled"):
        p.submit_grade("S1001", "CS101", "B")  # S1001 isn't in CS101 this semester
    with pytest.raises(RegistrarError):
        p.submit_grade("S9012", "CS101", "B", semester=REGISTRATION_SEMESTER)  # next semester hasn't happened
