"""GOV-C2-110 — state schema.

ADR-005: State must be a flat TypedDict. LangGraph checkpoints use msgpack
serialization, so only plain serializable fields are allowed. Never store
credentials, InvocationContext, Pydantic models or dataclasses here.

Design: docs/02_design.md "State Definition".
"""

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer backbone state.

    Shared fields (user_input, status, session_id, node_history, error_log,
    hitl_*, ...) are inherited from AgentState. Only agent-specific fields
    are declared below.

    Raw evidence never crosses this boundary: only reference identifiers
    (``case_ref``, ``source_id``) and the minimised ``validated_input`` are
    stored, so no raw case body is persisted into a checkpoint.
    """

    # --- routing / scope ------------------------------------------------
    mode: str
    """Execution mode: "ingest" | "invoke" | "feedback"."""

    case_ref: str
    """Reference id of the contract-variation case (never the raw case body)."""

    validated_input: dict
    """S-2 cleared, minimised input (rationale text, evidence refs, versions)."""

    # --- Stage 1 (deterministic) ----------------------------------------
    version_check: dict
    """{"ok": bool, "mismatches": [str]} for contract / authority-matrix versions."""

    source_registry: dict
    """Deterministic source handle table.

    ``{source_id: {"doc_ref", "span_ref", "span_kind", "text", "provenance"}}``

    ``span_kind`` decides whether a span may be quoted verbatim
    (docs/02_design.md "span kind allowlist"). The LLM may only *select*
    existing handles; it can never mint a source_id.
    """

    docs_present: dict
    format_check: dict
    authority_match: dict

    # --- Stage 2 --------------------------------------------------------
    recalled_perspectives: list
    """Approved, source-linked fact-check perspectives (retrieval-safe projection)."""

    prior_ids: list

    # --- Stage 3 / 4 ----------------------------------------------------
    evidence_brief: dict
    """Decision-brief draft. Closed allowlist — see docs/02_design.md."""

    mapping_degradation_reason: str
    """Closed reason code when Stage 3 falls back instead of accepting the LLM reply."""

    evidence_map: dict
    """claim -> source mapping, built deterministically from source_registry."""

    gaps: list

    requires_human_review: bool
    """True => evidence_brief is withheld and ``degradation`` carries the reason."""

    degradation: dict
    """Degradation envelope, set only when requires_human_review is True.

    ``{"review_reason_code": str, "gap_codes": [str], "unknown_codes": [str]}``
    Every value comes from a closed enumeration (src/services/contracts.py).
    """

    # --- ingest / feedback ----------------------------------------------
    ingest_result: dict
    feedback_result: dict
