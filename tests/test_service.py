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
        Registrar.login(conn, "advisor", "P06")  # English professor advises no one


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
