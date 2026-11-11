"""Demo: two students try to take the LAST seat in a course at the same moment.

    python demo_last_seat.py

Part 1 shows what happens without a lock (how registration worked before): both students
pass the seat check before either is saved, and the course ends up over capacity.
Part 2 uses the real registration code, which locks the database while it checks seats:
one student gets the seat and the other is told the course is full.

It works on a temporary copy of the sample database and never touches data/registrar.db.
No Claude calls are made, so it is free to run.
"""

import tempfile
import threading
import time
from pathlib import Path

from registrar.db import REGISTRATION_SEMESTER, connect
from registrar.service import Registrar, RegistrarError, User

DELAY = 0.5  # seconds between checking seats and saving, so both students check at the same moment
_original_check = Registrar.check_enrollment


def slow_check(self, *args, **kwargs):
    result = _original_check(self, *args, **kwargs)
    time.sleep(DELAY)  # simulate a busy server: the gap where a second request can slip in
    return result


def pick_course_and_students(conn):
    """A course with open seats and two students who are both allowed to take it."""
    admin = Registrar(conn, User("admin"))
    by_course: dict[str, list[str]] = {}
    for (sid,) in conn.execute("SELECT id FROM students ORDER BY id").fetchall():
        for e in admin.degree_progress(sid)["eligible_next_courses"]:
            if admin.check_enrollment(e["course"], student_id=sid)["can_enroll"]:
                by_course.setdefault(e["course"], []).append(sid)
    # prefer a CS course with the most qualifying students
    course, students = max(by_course.items(), key=lambda kv: (kv[0].startswith("CS") and len(kv[1]) >= 2, len(kv[1])))
    return course, students[:2]


def seats(conn, course):
    taken = conn.execute("SELECT COUNT(*) FROM grades WHERE course = ? AND semester = ?",
                         (course, REGISTRATION_SEMESTER)).fetchone()[0]
    cap = conn.execute("SELECT capacity FROM schedule WHERE course = ? AND semester = ?",
                       (course, REGISTRATION_SEMESTER)).fetchone()[0]
    return taken, cap


def race(db_path, course, students, attempt):
    """Run `attempt(registrar, student_id)` for both students at exactly the same moment."""
    start = threading.Barrier(len(students))
    results = {}

    def worker(sid):
        conn = connect(db_path)  # each student has their own connection, like separate sessions
        reg = Registrar(conn, User("admin"))
        start.wait()
        try:
            results[sid] = "GOT THE SEAT: " + attempt(reg, sid)
        except RegistrarError as e:
            results[sid] = "refused: " + str(e)
        conn.close()

    threads = [threading.Thread(target=worker, args=(s,)) for s in students]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def without_lock(reg, sid):
    """The old logic: check seats, then save, with nothing stopping another request in between."""
    chk = reg.check_enrollment(course, student_id=sid)
    if not chk["can_enroll"]:
        raise RegistrarError(" ".join(chk["problems"]))
    reg.conn.execute("INSERT INTO grades VALUES (?,?,?,NULL)", (sid, course, REGISTRATION_SEMESTER))
    reg.conn.commit()
    return f"registered {sid} for {course}"


def with_lock(reg, sid):
    return reg.register(course, student_id=sid)  # the real registration code


def leave_one_seat(conn, course, students):
    conn.execute("DELETE FROM grades WHERE course = ? AND semester = ? AND student_id IN (?, ?)",
                 (course, REGISTRATION_SEMESTER, *students))
    taken, _ = seats(conn, course)
    conn.execute("UPDATE schedule SET capacity = ? WHERE course = ? AND semester = ?",
                 (taken + 1, course, REGISTRATION_SEMESTER))
    conn.commit()


def report(conn, results):
    for sid, outcome in results.items():
        print(f"  {sid}: {outcome}")
    taken, cap = seats(conn, course)
    verdict = "OVERBOOKED" if taken > cap else "correct"
    print(f"  Seats taken: {taken} of {cap}  ->  {verdict}\n")


if __name__ == "__main__":
    Registrar.check_enrollment = slow_check
    db_path = Path(tempfile.mkdtemp()) / "demo.db"
    conn = connect(db_path)
    course, students = pick_course_and_students(conn)
    print(f"Course {course}, {REGISTRATION_SEMESTER}. Students {students[0]} and {students[1]} both qualify.")
    print("The course is set to have exactly ONE seat left, and both students ask for it at the same moment.\n")

    leave_one_seat(conn, course, students)
    print("PART 1: without a lock (how registration worked before)")
    report(conn, race(db_path, course, students, without_lock))

    leave_one_seat(conn, course, students)
    print("PART 2: with the fix (the real registration code)")
    report(conn, race(db_path, course, students, with_lock))
