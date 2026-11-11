"""Registrar business logic with role-based access control.

Every public method checks permissions against the logged-in user in code,
so the agents cannot leak or change data the user is not entitled to,
whatever the prompt says.
"""

import re
import sqlite3
from dataclasses import dataclass

from .db import (CORE_COURSES, CURRENT_SEMESTER, DEGREE_CREDITS, GRADE_POINTS,
                 MAX_CREDITS_PER_SEMESTER, PASSING_FOR_PREREQ, PROBATION_MAX_CREDITS, REGISTRATION_SEMESTER, SEMESTERS)

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


def _latest_attempts(rows) -> list:
    """Most recent graded attempt of each course: a repeated course counts once, with its latest grade."""
    order = {sem: i for i, sem in enumerate(SEMESTERS)}
    latest = {}
    for r in rows:
        if r["grade"] is not None and (r["course"] not in latest
                                       or order[r["semester"]] > order[latest[r["course"]]["semester"]]):
            latest[r["course"]] = r
    return list(latest.values())


def _cumulative(rows) -> tuple[float | None, int]:
    """Cumulative GPA and credits earned, counting each repeated course once (latest grade)."""
    latest = _latest_attempts(rows)
    return _gpa(latest), sum(r["credits"] for r in latest if GRADE_POINTS[r["grade"]] > 0)


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
        counted = {(r["course"], r["semester"]) for r in _latest_attempts(rows)}
        terms = []
        for sem in SEMESTERS:
            term = [r for r in rows if r["semester"] == sem]
            if term:
                terms.append({"semester": sem, "term_gpa": _gpa(term),
                              "courses": [{"course": r["course"], "title": r["title"],
                                           "credits": r["credits"],
                                           "grade": _grade_label(r, counted)} for r in term]})
        gpa, earned = _cumulative(rows)
        return {**student, "cumulative_gpa": gpa, "credits_earned": earned, "terms": terms,
                "note": "A repeated course counts once in the GPA and credits, with its most recent grade."}

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
        gpa, earned = _cumulative(rows)
        ip_credits = sum(r["credits"] for r in rows if r["grade"] is None)
        done_or_ip = passed | in_progress
        eligible = []
        for c in self.conn.execute("SELECT * FROM courses ORDER BY code"):
            prereqs = [p for p in c["prerequisites"].split(",") if p]
            # prerequisites must be completed (graded C- or better); in-progress courses don't count
            if c["code"] not in done_or_ip and all(p in passed for p in prereqs):
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
            "note": "Prerequisites must be completed with C- or better; in-progress courses do not count yet.",
        }

    # ---- registration --------------------------------------------------------------
    def check_prerequisites(self, course: str, student_id: str | None = None) -> dict:
        """Records step of enrollment: has the student completed every prerequisite (C- or better)?"""
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        c = self.conn.execute("SELECT * FROM courses WHERE code = ?", (course,)).fetchone()
        if c is None:
            raise RegistrarError(f"No course {course} in the catalog.")
        rows = self._student_rows(sid)
        in_progress = {r["course"] for r in rows if r["grade"] is None}
        best: dict[str, str | None] = {}
        for r in rows:  # best grade per course; None (in progress) only if never graded
            g = r["grade"]
            if r["course"] not in best or (g and (best[r["course"]] is None
                                                 or GRADE_POINTS[g] > GRADE_POINTS[best[r["course"]]])):
                best[r["course"]] = g

        def status(code: str) -> tuple[str, bool]:
            if code not in best:
                return "not taken", False
            g = best[code]
            if g is None:
                return "in progress, not completed yet", False
            ok = GRADE_POINTS[g] >= PASSING_FOR_PREREQ
            if not ok and code in in_progress:
                return f"in progress, not completed yet (retaking; earlier grade {g})", False
            return f"completed with {g}" + ("" if ok else " (below C-)"), ok

        prereqs = []
        for code in [x for x in c["prerequisites"].split(",") if x]:
            text, ok = status(code)
            prereqs.append({"course": code, "status": text, "satisfied": ok})
        done_text, done = status(course)
        return {"student_id": sid, "course": course, "title": c["title"],
                "already_completed": done, "course_status": done_text,
                "currently_taking": any(r["course"] == course and r["semester"] == CURRENT_SEMESTER
                                        and r["grade"] is None for r in rows),
                "prerequisites": prereqs or "none",
                "all_prerequisites_satisfied": all(x["satisfied"] for x in prereqs)}

    def check_enrollment(self, course: str, semester: str = REGISTRATION_SEMESTER,
                         student_id: str | None = None) -> dict:
        """Registration step of enrollment: open seats, time clashes and credit load, without registering."""
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        sec = self.conn.execute(
            """SELECT s.*, c.credits, p.name AS professor FROM schedule s JOIN courses c ON c.code = s.course
               JOIN professors p ON p.id = s.professor_id WHERE s.course = ? AND s.semester = ?""",
            (course, semester)).fetchone()
        if sec is None:
            raise RegistrarError(f"{course} is not offered in {semester}.")
        enrolled = self.conn.execute("SELECT COUNT(*) FROM grades WHERE course = ? AND semester = ?",
                                     (course, semester)).fetchone()[0]
        term = self.conn.execute(
            """SELECT s.course, s.days, s.time, c.credits FROM grades g
               JOIN schedule s ON s.course = g.course AND s.semester = g.semester
               JOIN courses c ON c.code = g.course WHERE g.student_id = ? AND g.semester = ?""",
            (sid, semester)).fetchall()
        already = any(r["course"] == course for r in term)
        clashes = [f"{r['course']} ({r['days']} {r['time']})" for r in term
                   if r["course"] != course and _overlaps(r["days"], r["time"], sec["days"], sec["time"])]
        load = sum(r["credits"] for r in term) + (0 if already else sec["credits"])
        problems = []
        if semester != REGISTRATION_SEMESTER:
            problems.append(f"Registration is only open for {REGISTRATION_SEMESTER}.")
        if already:
            problems.append(f"Already registered for {course} in {semester}.")
        if enrolled >= sec["capacity"] and not already:
            problems.append(f"{course} is full ({enrolled}/{sec['capacity']}).")
        if clashes:
            problems.append(f"Time conflict with {', '.join(clashes)}.")
        gpa, _ = _cumulative(self._student_rows(sid))
        if gpa is not None and gpa < 2.0 and load > PROBATION_MAX_CREDITS:
            problems.append(f"Students on academic probation (GPA {gpa} is below 2.0) may take at most "
                            f"{PROBATION_MAX_CREDITS} credits per semester; this would make {load}.")
        elif load > MAX_CREDITS_PER_SEMESTER:
            problems.append(f"Credit limit exceeded: {load} > {MAX_CREDITS_PER_SEMESTER}. More than "
                            f"{MAX_CREDITS_PER_SEMESTER} credits is an overload and needs written permission.")
        return {"student_id": sid, "course": course, "semester": semester, "professor": sec["professor"],
                "meets": f"{sec['days']} {sec['time']}, {sec['room']}",
                "seats": f"{enrolled}/{sec['capacity']} taken", "time_clashes": clashes or "none",
                "term_credits_after_adding": load, "problems": problems or "none", "can_enroll": not problems}

    def register(self, course: str, semester: str = REGISTRATION_SEMESTER, student_id: str | None = None) -> str:
        """Final step: re-runs both checks in code, then enrolls. The agents' checks are never trusted alone."""
        self._require("student", "admin")
        sid = self._default_student(student_id)
        self._check_student_access(sid)
        if semester != REGISTRATION_SEMESTER:
            raise RegistrarError(f"Registration is only open for {REGISTRATION_SEMESTER}.")
        if self.conn.in_transaction:
            self.conn.commit()
        # Take the database's write lock BEFORE counting seats. Two students can then never both see the
        # last seat as free: the second registration waits here until the first is saved, then sees a full course.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            pre = self.check_prerequisites(course, sid)
            if pre["already_completed"]:
                raise RegistrarError(f"{course} was already completed ({pre['course_status']}). Only courses with "
                                     "a grade below C- (D or F) can be repeated.")
            if pre["currently_taking"]:
                raise RegistrarError(f"{sid} is currently taking {course} this semester and can't register "
                                     "for it again until it is graded.")
            if not pre["all_prerequisites_satisfied"]:
                details = [f"{x['course']} ({x['status']})" for x in pre["prerequisites"] if not x["satisfied"]]
                raise RegistrarError(f"Missing prerequisites for {course}: {', '.join(details)}. "
                                     "Prerequisites must be completed with C- or better.")
            chk = self.check_enrollment(course, semester, sid)
            if not chk["can_enroll"]:
                raise RegistrarError(" ".join(chk["problems"]))
            self.conn.execute("INSERT INTO grades VALUES (?,?,?,NULL)", (sid, course, semester))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return (f"Registered {sid} for {course} in {semester} ({chk['meets']}, {chk['professor']}). "
                f"Term load: {chk['term_credits_after_adding']} credits.")

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
            gpa, earned = _cumulative(rows)
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
        out = []
        for r in rows:
            gpa, _ = _cumulative(self._student_rows(r["id"]))
            out.append({**dict(r), "gpa": gpa, "standing": _standing(gpa)})
        return out

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
        return (f"academic probation, at risk of dismissal (GPA below 2.0; "
                f"limited to {PROBATION_MAX_CREDITS} credits per semester)")
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


_DAY = re.compile(r"Th|M|T|W|F")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _overlaps(days_a: str, time_a: str, days_b: str, time_b: str) -> bool:
    """True if two meetings share a day and their time ranges overlap (e.g. MWF 09:00-09:50 vs MW 09:30-10:45)."""
    if not set(_DAY.findall(days_a)) & set(_DAY.findall(days_b)):
        return False
    a0, a1 = (_minutes(t) for t in time_a.replace("\u2013", "-").split("-"))
    b0, b1 = (_minutes(t) for t in time_b.replace("\u2013", "-").split("-"))
    return a0 < b1 and b0 < a1


def _grade_label(r, counted: set) -> str:
    if r["grade"] is None:
        return "IP (in progress)"
    if (r["course"], r["semester"]) not in counted:
        return f"{r['grade']} (repeated later; not counted)"
    return r["grade"]
