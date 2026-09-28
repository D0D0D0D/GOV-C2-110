"""GOV-C2-110 — closed contracts for output, degradation and errors.

This module is the single source of truth for *what may leave the agent*.
Everything here is a closed enumeration or a closed schema; the S-3 gate and
the egress sanitizer validate against these constants rather than against
hand-written prose.

Why closed rather than a forbidden-phrase denylist (docs/02_design.md
"S-3 設計方針"): a denylist of assertive phrases is defeated by paraphrase
("約款上問題ないので変更できます") and produces false assurance. Non-conclusiveness
is guaranteed structurally instead:

  1. no conclusion field exists in the output schema at all;
  2. no field the LLM populates accepts free text — only ids, closed codes and
     verbatim quotes;
  3. quotable spans are restricted by ``span_kind`` (a verbatim quote proves
     provenance, not non-conclusiveness — the source itself may contain a legal
     conclusion).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Identifier grammar
# ---------------------------------------------------------------------------
# Every id that leaves the agent must be an OPAQUE HANDLE, not free text.
# Without this, a conclusion rides out inside a field that is nominally "an
# id": e.g. fact_id="承認すべきである" or perspective_id="本変更は適法" satisfies a
# key-only allowlist. Restricting the grammar to ASCII handles makes any
# Japanese prose structurally impossible in an id position.
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
MAX_ID_LENGTH = 64

MODE_INGEST = "ingest"
MODE_INVOKE = "invoke"
MODE_FEEDBACK = "feedback"
ALLOWED_MODES: frozenset[str] = frozenset({MODE_INGEST, MODE_INVOKE, MODE_FEEDBACK})

# ---------------------------------------------------------------------------
# span_kind allowlist
# ---------------------------------------------------------------------------
# A verbatim quote proves WHERE text came from, not that the text is
# non-conclusive. A supplied record ("past decision", "counsel's opinion") may
# itself say "this variation is permissible under the clause"; quoting it
# verbatim would emit a prohibited conclusion. So each span carries a
# deterministic ``span_kind`` and only factual kinds may be quoted.
#
# The classification is deterministic (never LLM-assigned): letting a model
# label spans would create a path where an `adjudicative` span is mislabelled
# `observed_condition` and passes.

SPAN_KIND_QUANTITY = "quantity"
SPAN_KIND_DATE = "date"
SPAN_KIND_IDENTIFIER = "identifier"
SPAN_KIND_OBSERVED_CONDITION = "observed_condition"
SPAN_KIND_PROCEDURAL_RECORD = "procedural_record"
SPAN_KIND_ADJUDICATIVE = "adjudicative"
SPAN_KIND_OPINION = "opinion"
SPAN_KIND_UNCLASSIFIED = "unclassified"

QUOTABLE_SPAN_KINDS: frozenset[str] = frozenset({
    SPAN_KIND_QUANTITY,
    SPAN_KIND_DATE,
    SPAN_KIND_IDENTIFIER,
    SPAN_KIND_OBSERVED_CONDITION,
    SPAN_KIND_PROCEDURAL_RECORD,
})

NON_QUOTABLE_SPAN_KINDS: frozenset[str] = frozenset({
    SPAN_KIND_ADJUDICATIVE,
    SPAN_KIND_OPINION,
    SPAN_KIND_UNCLASSIFIED,  # fail-closed default
})

ALL_SPAN_KINDS: frozenset[str] = QUOTABLE_SPAN_KINDS | NON_QUOTABLE_SPAN_KINDS

# ---------------------------------------------------------------------------
# evidence_brief — closed output contract
# ---------------------------------------------------------------------------
# Top-level keys are fixed. Conclusion-bearing fields
# (clause_permissibility_verdict / legality_verdict / material_change_verdict /
# competition_impact_verdict / approval_recommendation /
# estimate_validity_verdict) are absent by construction, so even if a model
# emits one the recursive allowlist rejects it.

BRIEF_FIELDS: frozenset[str] = frozenset({
    "perspectives",
    "stated_facts",
    "evidence_sources",
    "gaps",
    "contradictions",
    "verification_questions",
    "unknowns",
})

# Per-element key allowlists. Every element is a closed dict: no element in the
# brief has a free-text field except ``verbatim_quote``, which must match a
# registry span exactly AND come from a quotable span_kind.
BRIEF_ELEMENT_FIELDS: dict[str, frozenset[str]] = {
    "perspectives": frozenset({"perspective_id", "source_id"}),
    "stated_facts": frozenset({
        "fact_id", "perspective_id", "source_id", "span_ref", "verbatim_quote",
    }),
    "evidence_sources": frozenset({"source_id", "doc_ref", "provenance"}),
    "gaps": frozenset({"perspective_id", "gap_code"}),
    "contradictions": frozenset({
        "perspective_id", "source_id_a", "source_id_b", "contradiction_code",
    }),
    "verification_questions": frozenset({
        "question_template_id", "perspective_id", "source_id",
    }),
    "unknowns": frozenset({"perspective_id", "unknown_code"}),
}

# Fields inside an element that may be omitted.
BRIEF_OPTIONAL_ELEMENT_FIELDS: dict[str, frozenset[str]] = {
    "verification_questions": frozenset({"source_id"}),
}

# ---------------------------------------------------------------------------
# Closed code enumerations
# ---------------------------------------------------------------------------
# Naming a field "typed code" is not enough: without an enumeration a
# ``review_reason_code`` could itself carry free text such as
# "約款上許容されるため承認可" and the degradation path becomes the leak.

GAP_CODES: frozenset[str] = frozenset({
    "evidence_absent",
    "evidence_unreadable",
    "version_mismatch",
    "authority_unmatched",
    "estimate_basis_absent",
    "schedule_impact_absent",
    "evidence_not_quotable",  # only non-quotable spans supported this perspective
})

CONTRADICTION_CODES: frozenset[str] = frozenset({
    "amount_mismatch",
    "date_mismatch",
    "duration_mismatch",
    "scope_mismatch",
    "authority_mismatch",
})

UNKNOWN_CODES: frozenset[str] = frozenset({
    "perspective_not_recallable",
    "source_unresolvable",
    "version_undeclared",
})

REVIEW_REASON_CODES: frozenset[str] = frozenset({
    "version_mismatch",
    "citation_missing",
    "citation_span_mismatch",
    "citation_not_quotable",
    "confidence_below_threshold",
    "authority_unmatched",
    "required_evidence_absent",
    "llm_not_configured",
    "llm_call_failed",
    "llm_response_unparsable",
    "mapping_llm_empty_response",
    "mapping_llm_schema_violation",
})

# Degradation envelope — the only payload returned when the brief is withheld.
DEGRADATION_FIELDS: frozenset[str] = frozenset({
    "review_reason_code", "gap_codes", "unknown_codes",
})

# Ingest/feedback rejection reasons — closed so the summary cannot echo prose.
REJECT_REASONS: frozenset[str] = frozenset({
    "missing_perspective_id",
    "malformed_perspective_id",
    "unknown_perspective_code",
    "write_failed",
})

# Error envelope — no exception text, stack trace, state or input echo.
ERROR_CODES: frozenset[str] = frozenset({
    "scope_unresolved",
    "input_rejected",
    "upstream_failure",
    "internal_error",
})

# ---------------------------------------------------------------------------
# Retrieval-safe KB projection
# ---------------------------------------------------------------------------
# Only these keys may be projected into the searchable knowledge base. Past
# decisions, adjudicative outcomes and raw human feedback stay in the audit
# tables and are never retrievable into an LLM context (design risk #2:
# indirect conclusion leakage).

PERSPECTIVE_RECORD_FIELDS: frozenset[str] = frozenset({
    "perspective_id",
    "perspective_code",
    "required_fact_codes",
    "question_template_ids",
    "source_id",
    "provenance",
    "approved_by_role",
    "approved_at",
    "record_version",
})

PERSPECTIVE_CODES: frozenset[str] = frozenset({
    "change_reason_basis",
    "estimate_basis",
    "schedule_impact_basis",
    "authority_conformance",
})

# ---------------------------------------------------------------------------
# Audit payload allowlist (S-4)
# ---------------------------------------------------------------------------
# Logs are an egress channel too. Only ids, counts, codes and status may be
# emitted — never raw input, drafts, quoted spans, feedback text or exception
# messages.

AUDIT_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "case_ref", "mode", "perspective_id", "source_id", "status",
    "review_reason_code", "gap_code", "unknown_code", "error_code",
    "count", "elapsed_ms", "span_kind", "record_version",
})
