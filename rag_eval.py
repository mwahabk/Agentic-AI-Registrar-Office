"""Evaluate retrieval quality: keyword (BM25) vs vector (embeddings) vs hybrid.

    python rag_eval.py

Each question is phrased the way a student would ask it, mostly without the handbook's
own wording, and lists the section(s) that contain the answer. Reports Recall@1, Recall@3
and MRR (mean reciprocal rank) for each retrieval mode, plus how well the knowledge-gap
check rejects off-topic questions. No Claude calls; free to run.
"""

from registrar.rag import KnowledgeBase

# (question, expected sections: any of these counts as a hit)
EVAL_SET = [
    ("Can I sign up for more than 18 credits?", ["Credit load and overloads"]),
    ("can I take more classes than normal this semester", ["Credit load and overloads"]),
    ("My GPA is 1.8, how many credits am I allowed?", ["Academic standing and probation", "Students on probation or at risk"]),
    ("what happens if my grades are really bad", ["Academic standing and probation", "Students on probation or at risk"]),
    ("I failed a class, can I retake it?", ["Repeating a course"]),
    ("If I take a course again, which grade counts?", ["Repeating a course"]),
    ("Does a D count as passing a prerequisite?", ["Prerequisites"]),
    ("I'm taking Computer Systems now, can I sign up for Operating Systems next term?", ["Prerequisites"]),
    ("When is the last day to drop a class?", ["Spring 2027 calendar and deadlines"]),
    ("When does the spring semester start?", ["Spring 2027 calendar and deadlines"]),
    ("When are fall grades due?", ["Spring 2027 calendar and deadlines"]),
    ("What do I need to finish my degree?", ["Graduation requirements", "Planning to graduate"]),
    ("Can my classmate see my grades?", ["Records privacy (FERPA)"]),
    ("What happens if two of my classes are at the same time?", ["Time conflicts and seat limits"]),
    ("The class I want has no seats left", ["Time conflicts and seat limits", "Planning to graduate"]),
    ("How is my GPA calculated?", ["Grading scale and GPA"]),
    ("Which course teaches protecting computers from hackers?", ["CS240 Computer Security (3 credits)"]),
    ("I want to learn how to build websites", ["CS150 Web Programming (3 credits)"]),
    ("Is there a class about DNA and genes?", ["CS340 Bioinformatics (3 credits)"]),
    ("course on how the internet works", ["CS330 Computer Networks (3 credits)"]),
    ("I want to work in teams on big programs", ["CS250 Software Engineering (3 credits)", "CS400 Senior Capstone (3 credits)"]),
    ("Which electives fit someone interested in AI?", ["Choosing electives by interest", "CS310 Artificial Intelligence (3 credits)"]),
    ("What should I take in my first year?", ["Recommended course sequence"]),
    ("Can someone waive a prerequisite for me?", ["Prerequisites", "When to contact the Registrar office in person"]),
    # exact terms and course codes, where keyword matching should help
    ("What are the prerequisites for CS320?", ["CS320 Machine Learning (3 credits)"]),
    ("Who teaches CS260?", ["CS260 Data Mining (3 credits)"]),
    ("FERPA", ["Records privacy (FERPA)"]),
    ("MATH250 description", ["MATH250 Linear Algebra (3 credits)"]),
    ("academic suspension and re-admission", ["Academic standing and probation"]),
    ("Is ENG201 a core course?", ["ENG201 Technical Writing (3 credits)", "Graduation requirements"]),
]

# questions the knowledge base does not cover: the gap check should reject them
OFF_TOPIC = [
    "How much does a parking permit cost?",
    "What is the cafeteria menu today?",
    "Who won the football game last weekend?",
    "How do I reset my email password?",
    "Where can I rent an apartment near campus?",
]


def evaluate(kb: KnowledgeBase, mode: str, k: int = 3) -> dict:
    hits1 = hitsk = rr = 0.0
    for question, expected in EVAL_SET:
        sections = [r["citation"].split(" > ", 1)[1] for r in kb.search(question, top_k=10, mode=mode)]
        rank = next((i for i, s in enumerate(sections) if s in expected), None)
        hits1 += rank == 0
        hitsk += rank is not None and rank < k
        rr += 1 / (rank + 1) if rank is not None else 0
    n = len(EVAL_SET)
    return {"mode": mode, "recall@1": hits1 / n, f"recall@{k}": hitsk / n, "mrr": rr / n}


def misses(kb: KnowledgeBase, mode: str, k: int = 3) -> list[str]:
    out = []
    for question, expected in EVAL_SET:
        top = [r["citation"].split(" > ", 1)[1] for r in kb.search(question, top_k=k, mode=mode)]
        if not any(s in expected for s in top):
            out.append(f"{question!r} -> got {top[0]!r}")
    return out


def gap_check(kb: KnowledgeBase) -> tuple[int, int]:
    """(off-topic questions correctly rejected, on-topic questions wrongly rejected)"""
    rejected = sum(not kb.relevant(kb.search(q)) for q in OFF_TOPIC)
    false_gaps = sum(not kb.relevant(kb.search(q)) for q, _ in EVAL_SET)
    return rejected, false_gaps


if __name__ == "__main__":
    kb = KnowledgeBase()
    print(f"{len(kb.chunks)} chunks, {len(EVAL_SET)} test questions\n")
    print(f"{'mode':<8} {'recall@1':>9} {'recall@3':>9} {'MRR':>6}")
    for mode in ("keyword", "vector", "hybrid"):
        r = evaluate(kb, mode)
        print(f"{mode:<8} {r['recall@1']:>9.0%} {r['recall@3']:>9.0%} {r['mrr']:>6.2f}")
    print("\nHybrid misses (expected section not in top 3):")
    for m in misses(kb, "hybrid"):
        print("  " + m)
    rejected, false_gaps = gap_check(kb)
    print(f"\nKnowledge-gap check: rejected {rejected}/{len(OFF_TOPIC)} off-topic questions, "
          f"wrongly rejected {false_gaps}/{len(EVAL_SET)} answerable ones")
