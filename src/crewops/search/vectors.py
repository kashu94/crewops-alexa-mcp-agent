"""Semantic search over rules, example questions and past scenarios.

Three S3 Vectors indexes, each small and fixed:

* ``rules``: the seven legality rules, for a legality question that names no rule id
  ("how much rest does a pilot need?").
* ``intents``: the gold example questions, with ids masked. The router's fallback: a
  question phrased in a way no routing rule anticipated is matched to the closest example
  question, whose intent the router knows. Never its ids or its answer.
* ``scenarios``: past disruption scenarios, to classify what *kind* of disruption a free-text
  description is. Only the kind is returned, never that scenario's answer: its crew and
  costs were right for its own moment, and a live answer must always be computed fresh.

Each document is embedded with Amazon Titan Text Embeddings V2 and stored with its text as
metadata. A query fetches every document of the index with its semantic distance, scores
the same documents by keywords (BM25), and blends the two (see ``hybrid``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crewops.search.hybrid import blend, bm25

RULES, INTENTS, SCENARIOS = "rules", "intents", "scenarios"

ID_PATTERNS = (
    (re.compile(r"\bDX\d{2,4}-\d{4}-\d{2}-\d{2}\b", re.I), "<flight>"),
    (re.compile(r"\bDX\d{2,4}\b", re.I), "<flight>"),
    (re.compile(r"\bC-\d{4}\b", re.I), "<crew>"),
    (re.compile(r"\bP-\d{4}\b", re.I), "<pairing>"),
    (re.compile(r"\bVT-[A-Z]{3}\b", re.I), "<aircraft>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
)
"""Specific ids replaced by placeholders in example questions and in queries against them.
An example question is about a *kind* of request, not about C-3310 or P-2291: masking keeps
ids from dominating the keyword score and keeps one question's answer (a crew id it names)
from surfacing in another's search."""


def mask_ids(text: str) -> str:
    for pattern, placeholder in ID_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


INDEXES = (RULES, INTENTS, SCENARIOS)
QUERY_LIMIT = 100
"""Neighbours requested per query, and the most documents an index may hold. Responses come
in pages of at most 100; keeping every index within one page means a query sees all of its
documents, so keyword scores are computed over the whole index. ``put`` enforces it."""
TEXT_KEY = "text"
"""Metadata key holding the embedded text. Declared non-filterable on each index, so it
can be long and does not count against the filterable-metadata size limit."""

SCENARIO_KIND = {
    "SICK_CREW": "crew_unavailable",
    "MULTI_SICK": "crew_unavailable",
    "STATION_CLOSURE": "station_disruption",
    "CERT_EXPIRY": "certification_lapse",
    "DELAY": "flight_delay",
}
"""Scenario event types mapped to report types. A delay that names an aircraft rather than
a flight is an aircraft fault."""


@dataclass(frozen=True, slots=True)
class Document:
    key: str
    text: str
    """What is embedded and keyword-matched."""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    """Returned with a hit. Never contains an answer key."""


def rule_documents(rules_doc: Mapping[str, Any]) -> list[Document]:
    return [
        Document(r["rule_id"], f"{r['rule_id']}: {r['text']}", {"rule_id": r["rule_id"]})
        for r in rules_doc["rules"]
    ]


def intent_documents(questions: Sequence[Mapping[str, Any]]) -> list[Document]:
    """Example questions with their ids masked. A hit names the closest example question and
    its tier; the router maps that question to an intent. Expected answers are never stored."""
    return [
        Document(
            q["question_id"],
            mask_ids(q["prompt"]),
            {"question_id": q["question_id"], "tier": int(q["tier"])},
        )
        for q in questions
    ]


def scenario_documents(scenarios: Sequence[Mapping[str, Any]]) -> list[Document]:
    docs = []
    for s in scenarios:
        event = s["event"]
        kind = SCENARIO_KIND.get(event["type"], "unknown")
        if event["type"] == "DELAY" and "aircraft" in event:
            kind = "aircraft_technical"
        text = f"{s['title']}. {event.get('narrative', '')}".strip()
        docs.append(
            Document(
                s["scenario_id"],
                text,
                {
                    "scenario_id": s["scenario_id"],
                    "report_type": kind,
                    "difficulty": s["difficulty"],
                },
            )
        )
    return docs


def load_documents(seed_dir: Path, eval_dir: Path) -> dict[str, list[Document]]:
    """All three corpora from the repository's data files."""
    read = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
    return {
        RULES: rule_documents(read(seed_dir / "rules.json")),
        INTENTS: intent_documents(read(eval_dir / "questions.json")),
        SCENARIOS: scenario_documents(read(eval_dir / "scenarios.json")),
    }


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

Embed = Callable[[str], list[float]]


def titan_embedder(bedrock_runtime: Any, model_id: str, dimensions: int) -> Embed:
    """Embed one text with Titan Text Embeddings V2 (unit-length vectors, for cosine)."""

    def embed(text: str) -> list[float]:
        response = bedrock_runtime.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True}),
        )
        return json.loads(response["body"].read())["embedding"]

    return embed


# --------------------------------------------------------------------------
# Index and query
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hit:
    key: str
    score: float
    """Blended 0-1 score, relative to the other documents of the same index."""
    semantic: float
    """Absolute cosine similarity, -1 to 1. Use it for a floor: the blended score is
    relative, so the best document always scores high even when nothing really matches."""
    keyword: float
    metadata: Mapping[str, Any]


class VectorSearch:
    def __init__(self, s3vectors: Any, bucket: str, embed: Embed) -> None:
        self.s3vectors = s3vectors
        self.bucket = bucket
        self.embed = embed

    def put(self, index: str, documents: Sequence[Document]) -> int:
        """Embed and store documents; the same key replaces the stored vector."""
        if len(documents) > QUERY_LIMIT:
            raise ValueError(
                f"{index}: {len(documents)} documents; at most {QUERY_LIMIT} keep every query "
                "within one result page"
            )
        vectors = [
            {
                "key": d.key,
                "data": {"float32": self.embed(d.text)},
                "metadata": {**d.metadata, TEXT_KEY: d.text},
            }
            for d in documents
        ]
        for start in range(0, len(vectors), 100):
            self.s3vectors.put_vectors(
                vectorBucketName=self.bucket,
                indexName=index,
                vectors=vectors[start : start + 100],
            )
        return len(vectors)

    def search(self, index: str, query: str, top_k: int = 3, alpha: float = 0.5) -> list[Hit]:
        """The ``top_k`` documents of ``index`` best matching ``query``, best first."""
        if index == INTENTS:
            query = mask_ids(query)
        response = self.s3vectors.query_vectors(
            vectorBucketName=self.bucket,
            indexName=index,
            queryVector={"float32": self.embed(query)},
            topK=QUERY_LIMIT,
            returnMetadata=True,
            returnDistance=True,
        )
        found = response.get("vectors", [])
        if not found:
            return []
        texts = [v.get("metadata", {}).get(TEXT_KEY, "") for v in found]
        # Cosine distance is 1 - similarity.
        semantic = [1.0 - float(v.get("distance", 1.0)) for v in found]
        keyword = bm25(query, texts)
        blended = blend(keyword, semantic, alpha)
        hits = [
            Hit(
                key=v["key"],
                score=round(blended[i], 3),
                semantic=round(semantic[i], 3),
                keyword=round(keyword[i], 3),
                metadata={k: val for k, val in v.get("metadata", {}).items() if k != TEXT_KEY}
                | {TEXT_KEY: texts[i]},
            )
            for i, v in enumerate(found)
        ]
        hits.sort(key=lambda h: (-h.score, -h.semantic, h.key))
        return hits[:top_k]
