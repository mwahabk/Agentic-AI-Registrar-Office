"""Registrar business logic with role-based access control.

Every public method checks permissions against the logged-in user in code,
so the agents cannot leak or change data the user is not entitled to,
whatever the prompt says.
"""

import sqlite3
from dataclasses import dataclass

from .db import (CORE_COURSES, CURRENT_SEMESTER, DEGREE_CREDITS, GRADE_POINTS,
                 MAX_CREDITS_PER_SEMESTER, PASSING_FOR_PREREQ, REGISTRATION_SEMESTER, SEMESTERS)

ROLES = ("student", "professor", "advisor", "admin")


class PermissionDenied(Exception):
    pass


class RegistrarError(Exception):
    pass


@dataclass(frozen=True)
class User:
    role: str
    id: str | None = None  # student or professor ID; None for admin
    name: str = ""


def _gpa(rows) -> float | None:
    graded = [(GRADE_POINTS[r["grade"]], r["credits"]) for r in rows if r["grade"] is not None]
    total = sum(c for _, c in graded)
    return round(sum(p * c for p, c in graded) / total, 2) if total else None


class Registrar:
    def __init__(self, conn: sqlite3.Connection, user: User):
        self.conn = conn
        self.user = user

    # ---- authentication / authorization ---------------------------------------
    @staticmethod
    def login(conn: sqlite3.Connection, role: str, user_id: str | None) -> User:
        if role not in ROLES:
            raise PermissionDenied(f"Unknown role {role!r}")
        if role == "admin":
            return User("admin", None, "Administrator")
        table = "students" if role == "student" else "professors"
        row = conn.execute(f"SELECT name FROM {table} WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise PermissionDenied(f"No {role} with ID {user_id}")
        if role == "advisor" and not conn.execute(
                "SELECT 1 FROM students WHERE advisor_id = ?", (user_id,)).fetchone():
            raise PermissionDenied(f"{user_id} has no advisees")
        return User(role, user_id, row["name"])

    def _require(self, *roles: str) -> None:
        if self.user.role not in roles:
            raise PermissionDenied(f"This action is not available to the {self.user.role} role.")

    def _check_student_access(self, student_id: str) -> None:
        u = self.user
        if u.role == "admin" or (u.role == "student" and u.id == student_id):
            pass
        elif u.role == "advisor" and self.conn.execute(
                "SELECT 1 FROM students WHERE id = ? AND advisor_id = ?", (student_id, u.id)).fetchone():
            pass
        else:
            raise PermissionDenied(f"You are not allowed to access records of student {student_id}.")
        if not self.conn.execute("SELECT 1 FROM students WHERE id = ?", (student_id,)).fetchone():
            raise RegistrarError(f"No student with ID {student_id}")

    def _check_section_access(self, course: str, semester: str) -> sqlite3.Row:
        sec = self.conn.execute("SELECT * FROM schedule WHERE course = ? AND semester = ?",
                                (course, semester)).fetchone()
        if sec is None:
            raise RegistrarError(f"{course} is not offered in {semester}")
        if self.user.role == "admin":
            return sec
        if self.user.role in ("professor", "advisor") and sec["professor_id"] == self.user.id:
            return sec
        raise PermissionDenied(f"You do not teach {course} in {semester}.")

    def _default_student(self, student_id: str | None) -> str:
        if self.user.role == "student":
            return self.user.id  # students always act on themselves
        if not student_id:
            raise RegistrarError("student_id is required")
        return student_id

    # ---- public catalog (all roles) -------------------------------------------
    def list_courses(self, query: str = "") -> list[dict]:
        q = f"%{query}%"
        rows = self.conn.execute(
            "SELECT * FROM courses WHERE code LIKE ? OR title LIKE ? ORDER BY code", (q, q))
        return [dict(r) for r in rows]

    def course_schedule(self, semester: str, course: str | None = None) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.course, c.title, c.credits, s.semester, p.name AS professor, s.days, s.time,
                      s.room, s.capacity,
                      (SELECT COUNT(*) FROM grades g WHERE g.course = s.course
                         AND g.semester = s.semester) AS enrolled
               FROM schedule s JOIN courses c ON c.code = s.course
               JOIN professors p ON p.id = s.professor_id
               WHERE s.semester = ? AND (? IS NULL OR s.course = ?) ORDER BY s.course""",
            (semester, course, course))
        return [dict(r) for r in rows]

    # ---- student records -------------------------------------------------------
    def _student_rows(self, student_id: str):
        return self.conn.execute(
            """SELECT g.course, c.title, c.credits, g.semester, g.grade
               FROM grades g JOIN courses c ON c.code = g.course WHERE g.student_id = ?""",
            (student_id,)).fetchall()

    def transcript(self, student_id: str | None = None) -> dict:
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        student = dict(self.conn.execute(
            """SELECT s.id, s.name, s.major, s.entry_semester, p.name AS advisor
               FROM students s LEFT JOIN professors p ON p.id = s.advisor_id WHERE s.id = ?""",
            (sid,)).fetchone())
        rows = self._student_rows(sid)
        terms = []
        for sem in SEMESTERS:
            term = [r for r in rows if r["semester"] == sem]
            if term:
                terms.append({"semester": sem, "term_gpa": _gpa(term),
                              "courses": [{"course": r["course"], "title": r["title"],
                                           "credits": r["credits"],
                                           "grade": r["grade"] or "IP (in progress)"} for r in term]})
        earned = sum(r["credits"] for r in rows if r["grade"] and GRADE_POINTS[r["grade"]] > 0)
        return {**student, "cumulative_gpa": _gpa(rows), "credits_earned": earned, "terms": terms}

    def student_schedule(self, student_id: str | None = None, semester: str = CURRENT_SEMESTER) -> list[dict]:
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        rows = self.conn.execute(
            """SELECT s.course, c.title, c.credits, p.name AS professor, s.days, s.time, s.room
               FROM grades g JOIN schedule s ON s.course = g.course AND s.semester = g.semester
               JOIN courses c ON c.code = g.course JOIN professors p ON p.id = s.professor_id
               WHERE g.student_id = ? AND g.semester = ? ORDER BY s.days, s.time""", (sid, semester))
        return [dict(r) for r in rows]

    def degree_progress(self, student_id: str | None = None) -> dict:
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        rows = self._student_rows(sid)
        passed = {r["course"] for r in rows if r["grade"] and GRADE_POINTS[r["grade"]] >= PASSING_FOR_PREREQ}
        in_progress = {r["course"] for r in rows if r["grade"] is None}
        earned = sum(r["credits"] for r in rows if r["grade"] and GRADE_POINTS[r["grade"]] > 0)
        ip_credits = sum(r["credits"] for r in rows if r["grade"] is None)
        gpa = _gpa(rows)
        done_or_ip = passed | in_progress
        eligible = []
        for c in self.conn.execute("SELECT * FROM courses ORDER BY code"):
            prereqs = [p for p in c["prerequisites"].split(",") if p]
            if c["code"] not in done_or_ip and all(p in done_or_ip for p in prereqs):
                eligible.append({"course": c["code"], "title": c["title"], "credits": c["credits"],
                                 "core": c["code"] in CORE_COURSES})
        return {
            "student_id": sid, "cumulative_gpa": gpa,
            "credits_earned": earned, "credits_in_progress": ip_credits,
            "credits_required": DEGREE_CREDITS,
            "core_remaining": [c for c in CORE_COURSES if c not in done_or_ip],
            "standing": _standing(gpa),
            "on_track_to_graduate_after_current_term": _will_graduate(earned + ip_credits, gpa, done_or_ip),
            "eligible_next_courses": eligible,
            "note": "Prerequisites require C- or better; in-progress courses are assumed passed.",
        }

    # ---- registration --------------------------------------------------------------
    def register(self, course: str, semester: str = REGISTRATION_SEMESTER, student_id: str | None = None) -> str:
        self._require("student", "admin")
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        if semester != REGISTRATION_SEMESTER:
            raise RegistrarError(f"Registration is only open for {REGISTRATION_SEMESTER}.")
        sec = self.conn.execute(
            """SELECT s.*, c.credits, c.prerequisites FROM schedule s JOIN courses c ON c.code = s.course
               WHERE s.course = ? AND s.semester = ?""", (course, semester)).fetchone()
        if sec is None:
            raise RegistrarError(f"{course} is not offered in {semester}.")
        rows = self._student_rows(sid)
        if any(r["course"] == course and r["semester"] == semester for r in rows):
            raise RegistrarError(f"Already registered for {course} in {semester}.")
        passed = {r["course"] for r in rows if r["grade"] and GRADE_POINTS[r["grade"]] >= PASSING_FOR_PREREQ}
        if course in passed:
            raise RegistrarError(f"{course} has already been completed.")
        satisfied = passed | {r["course"] for r in rows if r["grade"] is None}
        missing = [p for p in sec["prerequisites"].split(",") if p and p not in satisfied]
        if missing:
            raise RegistrarError(f"Missing prerequisites for {course}: {', '.join(missing)}")
        enrolled = self.conn.execute("SELECT COUNT(*) FROM grades WHERE course = ? AND semester = ?",
                                     (course, semester)).fetchone()[0]
        if enrolled >= sec["capacity"]:
            raise RegistrarError(f"{course} is full ({enrolled}/{sec['capacity']}).")
        term = [r for r in rows if r["semester"] == semester]
        load = sum(r["credits"] for r in term) + sec["credits"]
        if load > MAX_CREDITS_PER_SEMESTER:
            raise RegistrarError(f"Credit limit exceeded: {load} > {MAX_CREDITS_PER_SEMESTER}.")
        for r in self.conn.execute(
                """SELECT s.course, s.days, s.time FROM grades g JOIN schedule s
                   ON s.course = g.course AND s.semester = g.semester
                   WHERE g.student_id = ? AND g.semester = ?""", (sid, semester)):
            if r["days"] == sec["days"] and r["time"] == sec["time"]:
                raise RegistrarError(f"Time conflict with {r['course']} ({r['days']} {r['time']}).")
        self.conn.execute("INSERT INTO grades VALUES (?,?,?,NULL)", (sid, course, semester))
        self.conn.commit()
        return f"Registered {sid} for {course} in {semester}. Term load: {load} credits."

    def drop(self, course: str, semester: str = REGISTRATION_SEMESTER, student_id: str | None = None) -> str:
        self._require("student", "admin")
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        if semester not in (REGISTRATION_SEMESTER, CURRENT_SEMESTER):
            raise RegistrarError("Only current or upcoming-semester courses can be dropped.")
        cur = self.conn.execute(
            "DELETE FROM grades WHERE student_id = ? AND course = ? AND semester = ? AND grade IS NULL",
            (sid, course, semester))
        self.conn.commit()
        if cur.rowcount == 0:
            raise RegistrarError(f"{sid} is not enrolled in {course} for {semester}.")
        return f"Dropped {course} ({semester}) for {sid}."

    # ---- professors ---------------------------------------------------------------
    def professor_schedule(self, semester: str = CURRENT_SEMESTER, professor_id: str | None = None) -> list[dict]:
        self._require("professor", "advisor", "admin")
        pid = self.user.id if self.user.role != "admin" else professor_id
        if not pid:
            raise RegistrarError("professor_id is required")
        rows = self.conn.execute(
            """SELECT s.course, c.title, c.credits, s.days, s.time, s.room, s.capacity,
                      (SELECT COUNT(*) FROM grades g WHERE g.course = s.course
                         AND g.semester = s.semester) AS enrolled
               FROM schedule s JOIN courses c ON c.code = s.course
               WHERE s.professor_id = ? AND s.semester = ? ORDER BY s.course""", (pid, semester))
        return [dict(r) for r in rows]

    def class_roster(self, course: str, semester: str = CURRENT_SEMESTER) -> list[dict]:
        self._require("professor", "advisor", "admin")
        self._check_section_access(course, semester)
        rows = self.conn.execute(
            """SELECT st.id, st.name, g.grade FROM grades g JOIN students st ON st.id = g.student_id
               WHERE g.course = ? AND g.semester = ? ORDER BY st.name""", (course, semester))
        return [dict(r) for r in rows]

    def submit_grade(self, student_id: str, course: str, grade: str, semester: str = CURRENT_SEMESTER) -> str:
        self._require("professor", "advisor")
        self._check_section_access(course, semester)
        if semester != CURRENT_SEMESTER:
            raise RegistrarError(f"Grades can only be submitted for {CURRENT_SEMESTER}.")
        if grade not in GRADE_POINTS:
            raise RegistrarError(f"Invalid grade {grade!r}. Valid: {', '.join(GRADE_POINTS)}")
        cur = self.conn.execute(
            "UPDATE grades SET grade = ? WHERE student_id = ? AND course = ? AND semester = ?",
            (grade, student_id, course, semester))
        self.conn.commit()
        if cur.rowcount == 0:
            raise RegistrarError(f"{student_id} is not enrolled in {course} for {semester}.")
        return f"Recorded {grade} for {student_id} in {course} ({semester})."

    # ---- statistics ------------------------------------------------------------------
    def course_grade_stats(self, course: str, semester: str | None = None) -> dict:
        self._require("professor", "advisor", "admin")
        if self.user.role != "admin":
            # professors may only see stats for courses they taught
            taught = self.conn.execute(
                "SELECT 1 FROM schedule WHERE course = ? AND professor_id = ? AND (? IS NULL OR semester = ?)",
                (course, self.user.id, semester, semester)).fetchone()
            if not taught:
                raise PermissionDenied(f"You did not teach {course}"
                                       + (f" in {semester}." if semester else "."))
        query = """SELECT g.grade, c.credits FROM grades g JOIN courses c ON c.code = g.course
                   WHERE g.course = ? AND g.grade IS NOT NULL AND (? IS NULL OR g.semester = ?)"""
        params = [course, semester, semester]
        if self.user.role != "admin":
            query += " AND g.semester IN (SELECT semester FROM schedule WHERE course = ? AND professor_id = ?)"
            params += [course, self.user.id]
        return _distribution(self.conn.execute(query, params).fetchall(), course=course, semester=semester or "all")

    def grade_distribution_report(self, course_prefix: str = "", semester: str | None = None) -> dict:
        """Statistics for a group of courses (e.g. all CS courses)."""
        self._require("admin")
        rows = self.conn.execute(
            """SELECT g.course, g.grade, c.credits FROM grades g JOIN courses c ON c.code = g.course
               WHERE g.grade IS NOT NULL AND g.course LIKE ? AND (? IS NULL OR g.semester = ?)""",
            (f"{course_prefix}%", semester, semester)).fetchall()
        per_course = {}
        for code in sorted({r["course"] for r in rows}):
            per_course[code] = _distribution([r for r in rows if r["course"] == code])
        overall = _distribution(rows)
        return {
            "group": course_prefix or "all courses", "semester": semester or "all",
            "overall": overall, "per_course": per_course,
            "flag_thresholds": {"high_failure": "F-rate >= 15%", "high_A": "A/A- rate >= 40%"},
            "high_failure_courses": [c for c, d in per_course.items() if d["fail_rate_pct"] >= 15],
            "high_A_courses": [c for c, d in per_course.items() if d["A_rate_pct"] >= 40],
        }

    def student_standing_report(self, category: str) -> list[dict]:
        """Groups of students: graduating, at_risk (GPA < 2.0), probation_watch (2.0-2.3), honors (>= 3.5)."""
        self._require("admin")
        valid = ("graduating", "at_risk", "probation_watch", "honors")
        if category not in valid:
            raise RegistrarError(f"category must be one of {valid}")
        out = []
        for s in self.conn.execute("SELECT id, name FROM students ORDER BY id").fetchall():
            rows = self._student_rows(s["id"])
            gpa = _gpa(rows)
            earned = sum(r["credits"] for r in rows if r["grade"] and GRADE_POINTS[r["grade"]] > 0)
            ip = sum(r["credits"] for r in rows if r["grade"] is None)
            done = {r["course"] for r in rows if r["grade"] is None
                    or GRADE_POINTS[r["grade"]] >= PASSING_FOR_PREREQ}
            fails = sum(1 for r in rows if r["grade"] == "F")
            match = {"graduating": _will_graduate(earned + ip, gpa, done),
                     "at_risk": gpa is not None and gpa < 2.0,
                     "probation_watch": gpa is not None and 2.0 <= gpa < 2.3,
                     "honors": gpa is not None and gpa >= 3.5}[category]
            if match:
                out.append({"id": s["id"], "name": s["name"], "gpa": gpa, "credits_earned": earned,
                            "credits_in_progress": ip, "f_grades": fails})
        return out

    # ---- advising -----------------------------------------------------------------------
    def list_advisees(self) -> list[dict]:
        self._require("advisor")
        rows = self.conn.execute(
            "SELECT id, name, entry_semester FROM students WHERE advisor_id = ? ORDER BY name",
            (self.user.id,)).fetchall()
        return [{**dict(r), "gpa": _gpa(self._student_rows(r["id"])),
                 "standing": _standing(_gpa(self._student_rows(r["id"])))} for r in rows]

    def find_students(self, query: str) -> list[dict]:
        self._require("admin", "advisor")
        q = f"%{query}%"
        sql = "SELECT id, name, major, advisor_id FROM students WHERE (id LIKE ? OR name LIKE ?)"
        params: list = [q, q]
        if self.user.role == "advisor":
            sql += " AND advisor_id = ?"
            params.append(self.user.id)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id LIMIT 25", params)]


def _standing(gpa: float | None) -> str:
    if gpa is None:
        return "no grades yet"
    if gpa < 2.0:
        return "at risk of dismissal (GPA below 2.0)"
    if gpa < 2.3:
        return "probation watch"
    if gpa >= 3.5:
        return "honors"
    return "good standing"


def _will_graduate(credits: int, gpa: float | None, done: set[str]) -> bool:
    return credits >= DEGREE_CREDITS and gpa is not None and gpa >= 2.0 and all(c in done for c in CORE_COURSES)


def _distribution(rows, **extra) -> dict:
    counts = {g: 0 for g in GRADE_POINTS}
    for r in rows:
        counts[r["grade"]] += 1
    n = len(rows)
    a = counts["A"] + counts["A-"]
    return {**extra, "graded": n, "average_gpa": _gpa(rows), "counts": {g: c for g, c in counts.items() if c},
            "A_rate_pct": round(100 * a / n, 1) if n else 0.0,
            "fail_rate_pct": round(100 * counts["F"] / n, 1) if n else 0.0}
