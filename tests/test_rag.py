"""Tests for knowledge retrieval (RAG). No Claude calls."""

import re

import pytest

import rag_eval
from registrar import db
from registrar.rag import KnowledgeBase, format_results, load_chunks


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase(index_file=None)


def test_documents_are_chunked_by_section_with_metadata():
    chunks = load_chunks()
    assert len(chunks) >= 40
    assert all(c.doc and c.section and c.text and c.status in ("current", "superseded") for c in chunks)
    old = [c for c in chunks if c.status == "superseded"]
    assert old and all(c.superseded_by == "Registrar Handbook 2026-2027" for c in old)


def test_catalog_matches_the_database():
    """Every course in the database is described in the catalog with the same credits, prerequisites and instructor."""
    catalog = {c.section.split()[0]: c for c in load_chunks() if c.doc.startswith("Course Catalog")}
    professors = {p[0]: p[1] for p in db.PROFESSORS}
    assert set(catalog) == {c[0] for c in db.COURSES}
    for code, title, credits, prereqs, prof in db.COURSES:
        chunk = catalog[code]
        assert f"({credits} credits)" in chunk.section and title in chunk.section
        listed = re.search(r"Prerequisites: ([^.]*)\.", chunk.text).group(1)
        assert sorted(x.strip() for x in listed.split(",") if x.strip() != "none") == sorted(p for p in prereqs.split(",") if p)
        assert professors[prof] in chunk.text


def test_current_handbook_states_the_rules_the_code_enforces():
    text = " ".join(c.text for c in load_chunks() if c.doc == "Registrar Handbook 2026-2027")
    assert f"at most {db.MAX_CREDITS_PER_SEMESTER} credits" in text
    assert f"at most {db.PROBATION_MAX_CREDITS} credits" in text
    assert f"needs {db.DEGREE_CREDITS} credits" in text
    assert "C- or better" in text and "in progress do not count" in text.replace("still in progress", "in progress")
    assert all(code in text for code in db.CORE_COURSES)


def test_retrieval_quality(kb):
    """Hybrid search finds the right section in the top 3 for at least 80% of the evaluation questions."""
    assert rag_eval.evaluate(kb, "hybrid")["recall@3"] >= 0.8
    assert rag_eval.evaluate(kb, "keyword")["recall@3"] >= 0.8


def test_semantic_search_matches_meaning_not_words(kb):
    if kb.mode != "hybrid":
        pytest.skip("embedding model not available")
    top = kb.search("protecting computers from hackers", mode="vector")[0]
    assert top["citation"].endswith("CS240 Computer Security (3 credits)")


def test_knowledge_gap_is_detected(kb):
    rejected, false_gaps = rag_eval.gap_check(kb)
    assert false_gaps == 0
    if kb.mode == "hybrid":
        assert rejected >= 3
    assert format_results("parking permits", kb.search("How much does a parking permit cost?"), kb) \
        .startswith("NO RELEVANT DOCUMENTS") or kb.mode != "hybrid"


def test_superseded_passages_are_labelled(kb):
    results = kb.search("probation credit limit 16 credits", top_k=8)
    old = [r for r in results if r["status"] == "superseded"]
    assert old, "the 2024 rule should be retrieved so the agent can reconcile it"
    out = format_results("probation credit limit", results, kb)
    assert "SUPERSEDED (replaced by Registrar Handbook 2026-2027)" in out and "CURRENT" in out


def test_keyword_fallback_without_embeddings():
    kb = KnowledgeBase(index_file=None, use_embeddings=False)
    assert kb.mode == "keyword"
    assert kb.search("last day to drop")[0]["citation"].endswith("Spring 2027 calendar and deadlines")
