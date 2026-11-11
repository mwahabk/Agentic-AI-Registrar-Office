"""Knowledge retrieval (RAG) over the registrar's documents in knowledge/.

Pipeline (Chapter 14): load documents -> chunk by section -> embed chunks -> index,
then at question time retrieve with BM25 (keyword), vector search (semantic), or a
hybrid of both (reciprocal rank fusion). Each chunk carries metadata (document,
section, effective date, current/superseded) so the agent can cite its sources and
check that a rule is still in force.

Embeddings come from a small local model (model2vec "potion-retrieval-32M", a static
embedding model tuned for retrieval, downloaded once from Hugging Face, about 120 MB). If it can't be loaded, retrieval falls back to
BM25 keyword search only, so the system keeps working offline.
"""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
INDEX_FILE = Path(__file__).resolve().parent.parent / "data" / "rag_index.npz"
EMBEDDING_MODEL = "minishlab/potion-retrieval-32M"
MAX_CHUNK_CHARS = 1200   # longer sections are split by paragraph
TOP_K = 4                # chunks returned per search
MIN_SIMILARITY = 0.22    # best cosine similarity below this, and no strong keyword match = nothing relevant
RRF_K = 60               # reciprocal rank fusion constant

STOPWORDS = set("""a an and are as at be by can do does for from has have how i if in is it its my of on or
so that the their them there they this to was what when where which who will with you your me we our not
no any all about into than then also only after before more most may must should would could""".split())


@dataclass
class Chunk:
    id: str
    doc: str          # document title
    section: str
    text: str
    effective: str
    status: str       # "current" or "superseded"
    superseded_by: str
    source_file: str

    def citation(self) -> str:
        return f"{self.doc} > {self.section}"


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


# ---- ingestion: load and chunk ------------------------------------------------------------------

def _front_matter(raw: str) -> tuple[dict, str]:
    meta = {}
    if raw.startswith("---"):
        head, _, raw = raw[3:].partition("\n---")
        for line in head.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, raw


def load_chunks(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[Chunk]:
    """One chunk per '## ' section; sections longer than MAX_CHUNK_CHARS are split by paragraph."""
    chunks = []
    for path in sorted(knowledge_dir.glob("*.md")):
        meta, body = _front_matter(path.read_text(encoding="utf-8"))
        for part in re.split(r"\n(?=## )", body):
            part = part.strip()
            if not part.startswith("## "):
                continue
            title, _, text = part[3:].partition("\n")
            pieces, current = [], ""
            for para in text.strip().split("\n\n"):
                if current and len(current) + len(para) > MAX_CHUNK_CHARS:
                    pieces.append(current)
                    current = ""
                current = f"{current}\n\n{para}".strip()
            pieces.append(current)
            for n, piece in enumerate(pieces, 1):
                section = title.strip() + (f" (part {n})" if len(pieces) > 1 else "")
                chunks.append(Chunk(
                    id=f"{path.stem}#{len(chunks)}", doc=meta.get("title", path.stem), section=section,
                    text=piece, effective=meta.get("effective", ""), status=meta.get("status", "current"),
                    superseded_by=meta.get("superseded_by", ""), source_file=path.name))
    return chunks


# ---- retrieval ----------------------------------------------------------------------------------

class BM25:
    """Okapi BM25 keyword ranking (k1=1.5, b=0.75)."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.avg_len = sum(map(len, docs)) / max(len(docs), 1)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.docs))
        for i, (tf, doc) in enumerate(zip(self.tf, self.docs)):
            norm = self.k1 * (1 - self.b + self.b * len(doc) / self.avg_len)
            out[i] = sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in query if t in tf)
        return out


class KnowledgeBase:
    def __init__(self, knowledge_dir: Path = KNOWLEDGE_DIR, index_file: Path | None = INDEX_FILE,
                 use_embeddings: bool = True):
        self.chunks = load_chunks(knowledge_dir)
        # the section title is part of what gets matched, as a header would be in a real document
        self._texts = [f"{c.doc}. {c.section}. {c.text}" for c in self.chunks]
        self.bm25 = BM25([tokenize(t) for t in self._texts])
        self.model = None
        self.vectors = None
        if use_embeddings:
            self._load_embeddings(index_file)

    # embeddings are cached on disk and rebuilt only when the documents change
    def _load_embeddings(self, index_file: Path | None) -> None:
        try:
            from model2vec import StaticModel
            self.model = StaticModel.from_pretrained(EMBEDDING_MODEL)
        except Exception as e:  # no network on first run, package missing, ...
            print(f"[rag] embedding model unavailable ({type(e).__name__}); using keyword search only")
            return
        # rebuild when the documents or the embedding model change
        digest = hashlib.sha256("\n".join([EMBEDDING_MODEL, *self._texts]).encode()).hexdigest()
        if index_file and index_file.exists():
            cached = np.load(index_file)
            if str(cached["digest"]) == digest:
                self.vectors = cached["vectors"]
                return
        self.vectors = self._embed(self._texts)
        if index_file:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            np.savez(index_file, vectors=self.vectors, digest=digest)

    def _embed(self, texts: list[str]) -> np.ndarray:
        v = np.asarray(self.model.encode(texts), dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)

    @property
    def mode(self) -> str:
        return "hybrid" if self.vectors is not None else "keyword"

    def search(self, query: str, top_k: int = TOP_K, mode: str | None = None) -> list[dict]:
        """Rank chunks for a query. mode: 'hybrid' (default), 'vector' or 'keyword'."""
        mode = mode or self.mode
        if mode != "keyword" and self.vectors is None:
            mode = "keyword"
        kw = self.bm25.scores(tokenize(query))
        sim = (self.vectors @ self._embed([query])[0]) if self.vectors is not None else np.zeros(len(self.chunks))
        if mode == "keyword":
            order, fused = np.argsort(-kw), kw
        elif mode == "vector":
            order, fused = np.argsort(-sim), sim
        else:  # reciprocal rank fusion of both rankings
            rank_kw = np.empty(len(kw), int)
            rank_kw[np.argsort(-kw)] = np.arange(len(kw))
            rank_vec = np.empty(len(sim), int)
            rank_vec[np.argsort(-sim)] = np.arange(len(sim))
            fused = 1 / (RRF_K + rank_vec + 1) + np.where(kw > 0, 1 / (RRF_K + rank_kw + 1), 0)
            order = np.argsort(-fused)
        results = []
        for i in order[:top_k]:
            c = self.chunks[i]
            results.append({"citation": c.citation(), "text": c.text, "status": c.status,
                            "effective": c.effective, "superseded_by": c.superseded_by,
                            "keyword_score": round(float(kw[i]), 2), "semantic_similarity": round(float(sim[i]), 2),
                            "chunk_id": c.id})
        return results

    def relevant(self, results: list[dict]) -> bool:
        """Knowledge-gap check: is anything retrieved actually about the question?"""
        if not results:
            return False
        if self.vectors is None:
            return max(r["keyword_score"] for r in results) > 1.0
        return max(r["semantic_similarity"] for r in results) >= MIN_SIMILARITY or \
            max(r["keyword_score"] for r in results) > 3.0  # thresholds tuned with rag_eval.py


_KB: KnowledgeBase | None = None


def knowledge_base() -> KnowledgeBase:
    """Shared index, built once per process."""
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def format_results(query: str, results: list[dict], kb: KnowledgeBase) -> str:
    """Tool output for the agents: sources first, then the retrieved text with its metadata."""
    if not kb.relevant(results):
        return (f"NO RELEVANT DOCUMENTS for: {query!r}. The registrar knowledge base does not cover this. "
                "Say so, and suggest contacting the Registrar office; do not guess.")
    lines = [f"Retrieved {len(results)} chunks ({kb.mode} search) for: {query!r}",
             "SOURCES: " + " | ".join(f"[{n}] {r['citation']}" for n, r in enumerate(results, 1)), ""]
    for n, r in enumerate(results, 1):
        status = r["status"].upper() + (f" (replaced by {r['superseded_by']})" if r["superseded_by"] else "")
        lines.append(f"[{n}] {r['citation']} | effective {r['effective']} | {status} | "
                     f"semantic {r['semantic_similarity']}, keyword {r['keyword_score']}")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines)
