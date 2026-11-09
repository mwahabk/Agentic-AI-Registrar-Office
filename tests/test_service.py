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
