"""Retriever module — searches the vector store with access control and precedence filtering.

Guarantees enforced here (before anything reaches the LLM):
* Only documents the requester may use are searched (role access level,
  region, effective date) — restricted documents are never retrieved.
* Upcoming documents (effective after the reference date) are searched
  separately and only if readable and for the requester's region.
* All passages, including upcoming ones, fit in ``MAX_CORPUS_TOKENS``
  (cl100k_base), counting the label line each passage carries in the prompt.
* Instruction-like text inside documents is neutralised (Section 7.5).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.config import get_settings
from app.data_access.employee_repo import Employee
from app.ingestion.chunker import count_tokens, truncate_to_tokens
from app.retrieval.embeddings import embed_query
from app.retrieval.precedence import (
    get_allowed_doc_ids,
    precedence_key,
    resolve_precedence,
    upcoming_outranked_by,
)
from app.retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Tokens reserved per passage for the label line added in the prompt.
PASSAGE_LABEL_OVERHEAD = 45

# Sentences inside documents that address an AI system are information only;
# they are removed from the context so they cannot act as instructions.
_INJECTION_SENTENCE = re.compile(
    r"[^.\n]*\b("
    r"(note|message|instruction|instructions|reminder)s?\s+(for|to)\s+(the\s+|any\s+)?"
    r"(ai|a\.i\.|assistant|chatbot|bot|llm|language model|model)"
    r"|ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules)"
    r"|(ai|virtual)\s+assistants?\s+(reading|processing|must|should)"
    r"|system\s+prompt"
    r"|you\s+are\s+now\s+(an?\s+)?(ai|assistant|unrestricted)"
    r")\b[^.\n]*(\.|$)",
    re.IGNORECASE,
)
INJECTION_PLACEHOLDER = "[instruction-like text removed]"


def neutralize_injections(text: str) -> tuple[str, int]:
    """Remove sentences that try to instruct an AI. Returns (text, removed_count)."""
    cleaned, n = _INJECTION_SENTENCE.subn(INJECTION_PLACEHOLDER, text)
    return cleaned, n


@dataclass
class Passage:
    """A retrieved passage ready to be shown to the LLM."""

    text: str
    doc_id: str
    locator: str
    score: float
    token_count: int
    metadata: dict = field(default_factory=dict)
    outranked_by: list = field(default_factory=list)  # upcoming passages only

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "doc_id": self.doc_id,
            "locator": self.locator,
            "metadata": self.metadata,
            "score": self.score,
            "token_count": self.token_count,
        }


@dataclass
class RetrievalResult:
    """Passages for one LLM call, all within the corpus-token budget."""

    passages: list[Passage]
    upcoming: list[Passage]
    reference_date: date
    budget_tokens: int

    @property
    def corpus_tokens(self) -> int:
        return sum(p.token_count for p in self.passages + self.upcoming)

    @property
    def top_score(self) -> float:
        return max((p.score for p in self.passages), default=0.0)


class Retriever:
    """Retrieves relevant document chunks with access control and token budget enforcement."""

    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store
        self.settings = get_settings()

    def retrieve_context(
        self,
        query: str,
        employee: Employee,
        reference_date: Optional[date] = None,
        max_tokens: Optional[int] = None,
    ) -> RetrievalResult:
        """Retrieve eligible passages and relevant upcoming changes.

        CRITICAL: Documents the requester cannot read are NEVER retrieved.
        This is enforced BEFORE any data reaches the LLM.
        """
        reference_date = reference_date or self.settings.today
        max_tokens = min(max_tokens or self.settings.max_corpus_tokens,
                         self.settings.max_corpus_tokens)
        doc_meta = self.vector_store.doc_metadata

        # Step 1: Determine which documents this employee can access
        eligible_ids, upcoming_ids = get_allowed_doc_ids(
            all_doc_metadata=doc_meta,
            employee_role=employee.role,
            employee_region=employee.region,
            reference_date=reference_date,
        )
        logger.info(
            "Employee %s (role=%s, region=%s, ref=%s): %d eligible docs, %d upcoming",
            employee.employee_id, employee.role, employee.region,
            reference_date, len(eligible_ids), len(upcoming_ids),
        )
        empty = RetrievalResult([], [], reference_date, max_tokens)
        if not eligible_ids:
            return empty

        # Step 2: Embed the query
        query_embedding = embed_query(query)

        # Step 3: Search vector store restricted to eligible documents
        raw_results = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=self.settings.retrieval_top_k,
            doc_id_filter=eligible_ids,
        )
        min_score = self.settings.retrieval_min_score
        raw_results = [(c, s) for c, s in raw_results if s >= min_score]
        if not raw_results:
            logger.info("No sufficiently relevant passages (min_score=%.2f)", min_score)
            return empty

        # Step 4: Upcoming changes (budgeted first, from a reserved share)
        upcoming: list[Passage] = []
        upcoming_tokens = 0
        if upcoming_ids:
            reserve = min(self.settings.upcoming_token_reserve, max_tokens // 4)
            up_raw = self.vector_store.search(
                query_embedding=query_embedding, top_k=5, doc_id_filter=upcoming_ids
            )
            for chunk, score in up_raw:
                if score < max(min_score, 0.35) or len(upcoming) >= 2:
                    continue
                passage = self._to_passage(chunk, score)
                cost = passage.token_count + PASSAGE_LABEL_OVERHEAD
                if upcoming_tokens + cost > reserve:
                    continue
                upcoming.append(passage)
                upcoming_tokens += cost

        # Step 5: Rerank eligible passages (relevance + mild precedence boost)
        reranked = resolve_precedence(
            chunks_with_scores=raw_results,
            all_doc_metadata=doc_meta,
            employee_region=employee.region,
        )

        # Step 6: Enforce the token budget and per-document cap
        budget = max_tokens - upcoming_tokens
        selected: list[Passage] = []
        used = 0
        per_doc: dict[str, int] = {}
        for chunk, score in reranked:
            if len(selected) >= self.settings.retrieval_final_k:
                break
            if per_doc.get(chunk.doc_id, 0) >= self.settings.max_chunks_per_doc:
                continue
            passage = self._to_passage(chunk, score)
            cost = passage.token_count + PASSAGE_LABEL_OVERHEAD
            if used + cost > budget:
                remaining = budget - used - PASSAGE_LABEL_OVERHEAD
                if remaining > 80 and not selected:
                    passage.text = truncate_to_tokens(passage.text, remaining)
                    passage.token_count = count_tokens(passage.text)
                    cost = passage.token_count + PASSAGE_LABEL_OVERHEAD
                else:
                    continue
            selected.append(passage)
            used += cost
            per_doc[chunk.doc_id] = per_doc.get(chunk.doc_id, 0) + 1

        # Upcoming: which selected (current) documents would still beat them
        current_metas = [doc_meta[p.doc_id] for p in selected if p.doc_id in doc_meta]
        unique_current = {m["doc_id"]: m for m in current_metas}.values()
        for up in upcoming:
            up.outranked_by = upcoming_outranked_by(doc_meta[up.doc_id], list(unique_current))

        # Present passages in precedence order (highest authority first)
        selected.sort(key=lambda p: (precedence_key(doc_meta[p.doc_id]), -p.score))

        result = RetrievalResult(selected, upcoming, reference_date, max_tokens)
        assert result.corpus_tokens + PASSAGE_LABEL_OVERHEAD * (
            len(selected) + len(upcoming)
        ) <= max_tokens, "corpus token budget exceeded"
        logger.info(
            "Retrieved %d passages + %d upcoming (%d corpus tokens, budget %d)",
            len(selected), len(upcoming), result.corpus_tokens, max_tokens,
        )
        return result

    def retrieve(
        self,
        query: str,
        employee: Employee,
        reference_date: Optional[date] = None,
        max_tokens: Optional[int] = None,
    ) -> list[dict]:
        """Retrieve relevant chunks for a query, filtered by access control.

        Backwards-compatible wrapper around :meth:`retrieve_context`.

        Returns:
            List of dicts with keys: text, doc_id, locator, metadata, score
        """
        result = self.retrieve_context(query, employee, reference_date, max_tokens)
        return [p.as_dict() for p in result.passages]

    def _to_passage(self, chunk, score: float) -> Passage:
        text, removed = neutralize_injections(chunk.text)
        if removed:
            logger.warning(
                "Neutralised %d instruction-like sentence(s) in %s", removed, chunk.doc_id
            )
        return Passage(
            text=text,
            doc_id=chunk.doc_id,
            locator=chunk.locator,
            score=float(score),
            token_count=count_tokens(text),
            metadata=self.vector_store.doc_metadata.get(chunk.doc_id, {}),
        )
