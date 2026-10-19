"""Document precedence rules (Section 5 of TASK.pdf).

Determines which documents apply to a given employee on a given date,
and resolves conflicts using authority → scope → recency ordering.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Authority ranking: lower number = higher authority
# policy, addendum, memo, handbook, calendar outrank faq and wiki
AUTHORITY_RANK = {
    "policy": 1,
    "addendum": 1,
    "memo": 1,
    "handbook": 1,
    "calendar": 1,
    "faq": 2,
    "wiki": 2,
}

# Access level hierarchy
ACCESS_LEVELS = {
    "employee": {"all"},
    "manager": {"all", "managers"},
    "hr": {"all", "managers", "hr"},
}


def get_accessible_levels(role: str) -> set[str]:
    """Get the set of access_level values readable by a given role."""
    return ACCESS_LEVELS.get(role, {"all"})


# ── Deterministic precedence model (Section 5.1) ─────────────────────────────


def authority_rank(doc_meta: dict) -> int:
    """1 for policy/addendum/memo/handbook/calendar, 2 for faq/wiki (lower wins)."""
    return AUTHORITY_RANK.get(doc_meta.get("doc_type", "wiki"), 2)


def scope_rank(doc_meta: dict) -> int:
    """0 for documents scoped to specific regions, 1 for GLOBAL (lower wins)."""
    return 1 if "GLOBAL" in doc_meta.get("regions", ["GLOBAL"]) else 0


def precedence_key(doc_meta: dict) -> tuple:
    """Sort key: smaller key = higher precedence (authority, scope, recency)."""
    effective = date.fromisoformat(doc_meta.get("effective_date", "1900-01-01"))
    return (authority_rank(doc_meta), scope_rank(doc_meta), -effective.toordinal())


def compare_precedence(a: dict, b: dict) -> tuple[int, Optional[str]]:
    """Compare two documents under Section 5.1.

    Returns:
        (result, deciding_test) where result is -1 if ``a`` wins, 1 if ``b``
        wins and 0 if no test decides (an unresolved conflict). deciding_test
        is "authority", "scope", "recency" or None.
    """
    tests = (
        ("authority", authority_rank(a), authority_rank(b)),
        ("scope", scope_rank(a), scope_rank(b)),
        ("recency", -date.fromisoformat(a["effective_date"]).toordinal(),
         -date.fromisoformat(b["effective_date"]).toordinal()),
    )
    for name, ka, kb in tests:
        if ka != kb:
            return (-1 if ka < kb else 1), name
    return 0, None


def resolve_winners(doc_metas: list[dict]) -> list[dict]:
    """Return the winning document(s) among documents stating the same rule.

    More than one document is returned only when the tests cannot decide
    (an unresolved conflict that must be reported, not silently picked).
    """
    if not doc_metas:
        return []
    best = min(precedence_key(m) for m in doc_metas)
    return [m for m in doc_metas if precedence_key(m) == best]


def upcoming_outranked_by(upcoming_meta: dict, current_metas: list[dict]) -> list[str]:
    """IDs of current documents that would still beat an upcoming document.

    On its effective date the upcoming document is the most recent, so it can
    only lose on authority or scope. If this list is empty for the documents
    that state a rule, the upcoming document would win and should be mentioned.
    """
    beaten_by = []
    for cur in current_metas:
        result, test = compare_precedence(cur, upcoming_meta)
        if result == -1 and test in ("authority", "scope"):
            beaten_by.append(cur["doc_id"])
    return beaten_by


def is_document_eligible(
    doc_meta: dict,
    employee_role: str,
    employee_region: str,
    reference_date: date,
) -> bool:
    """Check if a document is eligible for a given employee on a given date.

    A document is eligible if ALL of these are true:
    1. The requester's role can read its access_level
    2. Its regions include the requester's region, or GLOBAL
    3. Its effective_date is on or before the reference date
    """
    # 1. Access level check
    accessible = get_accessible_levels(employee_role)
    if doc_meta["access_level"] not in accessible:
        return False

    # 2. Region check
    doc_regions = doc_meta.get("regions", [])
    if "GLOBAL" not in doc_regions and employee_region not in doc_regions:
        return False

    # 3. Effective date check
    effective = date.fromisoformat(doc_meta["effective_date"])
    if effective > reference_date:
        return False

    return True


def is_document_upcoming(
    doc_meta: dict,
    employee_role: str,
    employee_region: str,
    reference_date: date,
) -> bool:
    """Check if a document is an upcoming change (effective_date > reference_date).

    The document must be readable by the requester and for their region.
    """
    accessible = get_accessible_levels(employee_role)
    if doc_meta["access_level"] not in accessible:
        return False

    doc_regions = doc_meta.get("regions", [])
    if "GLOBAL" not in doc_regions and employee_region not in doc_regions:
        return False

    effective = date.fromisoformat(doc_meta["effective_date"])
    return effective > reference_date


def get_allowed_doc_ids(
    all_doc_metadata: dict[str, dict],
    employee_role: str,
    employee_region: str,
    reference_date: date,
) -> tuple[set[str], set[str]]:
    """Get the set of eligible and upcoming doc_ids for an employee.

    Returns:
        Tuple of (eligible_doc_ids, upcoming_doc_ids)
    """
    eligible = set()
    upcoming = set()

    for doc_id, meta in all_doc_metadata.items():
        if is_document_eligible(meta, employee_role, employee_region, reference_date):
            eligible.add(doc_id)
        elif is_document_upcoming(
            meta, employee_role, employee_region, reference_date
        ):
            upcoming.add(doc_id)

    return eligible, upcoming


def resolve_precedence(
    chunks_with_scores: list[tuple],
    all_doc_metadata: dict[str, dict],
    employee_region: str,
) -> list[tuple]:
    """Apply precedence rules to rerank retrieved chunks.

    When multiple documents address the same topic, apply:
    1. Authority: policy/addendum/memo/handbook/calendar > faq/wiki
    2. Scope: region-specific > GLOBAL
    3. Recency: later effective_date > earlier

    This function reranks chunks by boosting scores of higher-precedence documents.

    Args:
        chunks_with_scores: List of (chunk, score) tuples
        all_doc_metadata: Document metadata dict
        employee_region: Employee's region

    Returns:
        Reranked list of (chunk, score) tuples
    """
    if not chunks_with_scores:
        return []

    reranked = []
    for chunk, score in chunks_with_scores:
        meta = all_doc_metadata.get(chunk.doc_id, {})

        # Calculate precedence boost
        boost = 0.0

        # Authority boost (higher authority docs get more boost)
        doc_type = meta.get("doc_type", "wiki")
        authority = AUTHORITY_RANK.get(doc_type, 2)
        if authority == 1:
            boost += 0.05  # High authority docs get a small boost

        # Scope boost (region-specific docs outrank GLOBAL)
        regions = meta.get("regions", ["GLOBAL"])
        if employee_region in regions and "GLOBAL" not in regions:
            boost += 0.05  # Region-specific gets priority

        # Recency boost (newer docs get a small boost)
        effective_date = meta.get("effective_date", "2020-01-01")
        # Normalize to a 0-0.02 range (2024 = 0, 2027 = 0.02)
        try:
            year = int(effective_date[:4])
            recency = min(0.02, max(0.0, (year - 2024) * 0.0067))
            boost += recency
        except (ValueError, IndexError):
            pass

        reranked.append((chunk, score + boost))

    # Sort by adjusted score (descending)
    reranked.sort(key=lambda x: x[1], reverse=True)

    return reranked


def detect_conflicts(
    chunks: list,
    all_doc_metadata: dict[str, dict],
) -> list[dict]:
    """Detect conflicting information across documents.

    Returns a list of potential conflicts for the LLM to handle.
    """
    # Group chunks by their source doc_id
    doc_chunks = {}
    for chunk in chunks:
        doc_id = chunk.doc_id
        if doc_id not in doc_chunks:
            doc_chunks[doc_id] = {
                "meta": all_doc_metadata.get(doc_id, {}),
                "chunks": [],
            }
        doc_chunks[doc_id]["chunks"].append(chunk)

    conflicts = []

    # Check for same-topic documents with different effective dates or types
    doc_ids = list(doc_chunks.keys())
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            meta_i = doc_chunks[doc_ids[i]]["meta"]
            meta_j = doc_chunks[doc_ids[j]]["meta"]

            # If both docs are the same type (e.g., both policies), they might conflict
            if meta_i.get("doc_type") == meta_j.get("doc_type"):
                # Check if they overlap in scope
                regions_i = set(meta_i.get("regions", []))
                regions_j = set(meta_j.get("regions", []))

                if regions_i & regions_j or "GLOBAL" in regions_i or "GLOBAL" in regions_j:
                    conflicts.append({
                        "doc_a": doc_ids[i],
                        "doc_b": doc_ids[j],
                        "reason": "same_type_overlapping_scope",
                        "meta_a": meta_i,
                        "meta_b": meta_j,
                    })

    return conflicts
