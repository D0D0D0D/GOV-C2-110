"""GOV-C2-110 — inner domain workflow nodes.

Stage 1 / 2 / 4 and the ingest & feedback nodes are fully deterministic.
Stage 3 is the only node that calls an LLM, and even there the model may only
select identifiers that Stage 1 minted.

Design: docs/02_design.md "内側 domain workflow graph".
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import contracts, service
from src.services.egress import is_valid_identifier, sanitize_audit_payload
from src.services.llm_client import draft_evidence_brief, empty_brief


def _audit(event: str, payload: dict, state: dict) -> None:
    """Emit a domain audit event with the payload reduced to the allowlist.

    Logs are an egress channel: emitting raw input, drafts, quoted spans or
    exception text here would leak conclusions even when the response is clean.
    """
    emit_trace_event(event, sanitize_audit_payload(payload), state)


def _passthrough_error(state: dict) -> dict | None:
    """Propagate an upstream error without doing any further work."""
    if state.get("status") == AgentStatus.ERROR.value:
        return {"status": AgentStatus.ERROR.value}
    return None


class _DomainNode(FunctionNode):
    """Common base: every node in this template handles municipal case data."""

    # S-1: municipal contract records are internal business data, so no node
    # here may run for an anonymous caller. Declared explicitly on every
    # subclass path (implicit ANONYMOUS inheritance is not acceptable).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL


def _unpack_inner_input(state: dict) -> dict:
    """Read the map the parent GraphNode handed down.

    ``GraphNode.extract_input()`` serialises the minimal map into the inner
    graph's ``user_input`` string, so the inner nodes must decode it rather
    than expecting ``mode`` / ``case_ref`` to already be state fields.
    """
    if state.get("validated_input"):
        return {
            "mode": state.get("mode") or contracts.MODE_INVOKE,
            "case_ref": state.get("case_ref", ""),
            "validated_input": state.get("validated_input") or {},
        }
    raw = state.get("user_input")
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            return {
                "mode": payload.get("mode") or contracts.MODE_INVOKE,
                "case_ref": payload.get("case_ref", ""),
                "validated_input": payload.get("validated_input") or {},
            }
    return {"mode": contracts.MODE_INVOKE, "case_ref": "", "validated_input": {}}


class UnpackInputNode(_DomainNode):
    """Decode the parent's serialised map into inner state fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early
        unpacked = _unpack_inner_input(state)
        return {
            "mode": unpacked["mode"],
            "case_ref": unpacked["case_ref"],
            "validated_input": unpacked["validated_input"],
            "requires_human_review": False,
            "degradation": {},
            "status": AgentStatus.SUCCESS.value,
        }


class DeterministicCheckNode(_DomainNode):
    """Stage 1 — deterministic checks, source minting and version reconciliation.

    Draws no semantic conclusion. When versions do not reconcile it stops the
    semantic pipeline entirely rather than reasoning over a baseline it cannot
    trust.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early

        validated = state.get("validated_input") or _unpack_inner_input(state)["validated_input"]
        evidence = validated.get("evidence") or []
        baseline = self._config.get("baseline") or {}

        registry = service.build_source_registry(evidence)
        version_check = service.check_versions(validated.get("versions") or {}, baseline)
        authority_match = service.match_authority(
            validated.get("authority") or {},
            self._config.get("authority_matrix") or {},
        )
        documents = service.check_required_documents(
            evidence, self._config.get("required_doc_types") or []
        )

        _audit(
            "cv_stage1_checked",
            {
                "case_ref": state.get("case_ref", ""),
                "count": len(registry),
                "status": "ok" if version_check["ok"] else "version_mismatch",
            },
            state,
        )

        update: dict[str, Any] = {
            "source_registry": registry,
            "version_check": version_check,
            "authority_match": authority_match,
            "docs_present": documents,
            "format_check": {"checked": True},
            "gaps": [],
            "status": AgentStatus.SUCCESS.value,
        }

        # Version reconciliation failed -> fail closed before any semantic work.
        if not version_check["ok"]:
            gap_codes = ["version_mismatch"]
            unknown_codes = (
                ["version_undeclared"]
                if any(m.endswith(":undeclared") for m in version_check["mismatches"])
                else []
            )
            update.update({
                "requires_human_review": True,
                "degradation": {
                    "review_reason_code": "version_mismatch",
                    "gap_codes": gap_codes,
                    "unknown_codes": unknown_codes,
                },
            })
        return update


class RecallNode(_DomainNode):
    """Stage 2 — recall approved perspectives through the retrieval-safe projection."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None, kb: Any = None) -> None:
        super().__init__()
        self._config = config or {}
        self._kb = kb

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early
        if state.get("requires_human_review"):
            return {"status": AgentStatus.SUCCESS.value}

        top_k = int(self._config.get("top_k", 8))
        raw: list[dict] = []
        if self._kb is not None:
            try:
                raw = self._kb.search(query=state.get("case_ref", ""), top_k=top_k) or []
            except Exception:
                # A retrieval failure is NOT "no approved perspectives": treating
                # it as an empty result would silently release a brief that was
                # never checked against the customer's approved perspectives.
                _audit("cv_recall_failed", {"status": "kb_unavailable"}, state)
                return {
                    "recalled_perspectives": [],
                    "prior_ids": [],
                    "requires_human_review": True,
                    "degradation": {
                        "review_reason_code": "required_evidence_absent",
                        "gap_codes": [],
                        "unknown_codes": ["perspective_not_recallable"],
                    },
                    "status": AgentStatus.SUCCESS.value,
                }
        else:
            raw = list(self._config.get("perspectives") or [])[:top_k]

        # Project before anything reaches an LLM context: past decisions and
        # adjudicative outcomes must never become retrievable model input.
        projected = [service.project_perspective_record(r) for r in raw]
        projected = [p for p in projected if p.get("perspective_id")]

        _audit(
            "cv_perspectives_recalled",
            {"case_ref": state.get("case_ref", ""), "count": len(projected)},
            state,
        )
        return {
            "recalled_perspectives": projected,
            "prior_ids": [p["perspective_id"] for p in projected],
            "status": AgentStatus.SUCCESS.value,
        }


class MappingNode(_DomainNode):
    """Stage 3 — the only LLM node: map free-text rationale onto approved perspectives."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None, llm: Any = None) -> None:
        super().__init__()
        self._config = config or {}
        self._llm = llm

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early
        if state.get("requires_human_review"):
            return {"status": AgentStatus.SUCCESS.value}

        validated = state.get("validated_input") or {}
        brief, degradation_reason = draft_evidence_brief(
            self._llm,
            str(validated.get("variation_rationale") or ""),
            state.get("recalled_perspectives") or [],
            state.get("source_registry") or {},
        )

        _audit(
            "cv_brief_drafted",
            {
                "case_ref": state.get("case_ref", ""),
                "count": len(brief.get("stated_facts") or []),
                "status": degradation_reason or "ok",
            },
            state,
        )
        if degradation_reason:
            return {
                "evidence_brief": {},
                "mapping_degradation_reason": degradation_reason,
                "requires_human_review": True,
                "degradation": {
                    "review_reason_code": degradation_reason,
                    "gap_codes": [],
                    "unknown_codes": [],
                },
                "status": AgentStatus.SUCCESS.value,
            }
        return {"evidence_brief": brief, "status": AgentStatus.SUCCESS.value}


class EvidenceVerifyNode(_DomainNode):
    """Stage 4 — verify every citation, then either release or fail closed.

    There is no in-graph suspend: on any verification failure the brief is
    withheld and a typed degradation envelope is returned, so the caller fixes
    the input and re-invokes.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early
        if state.get("requires_human_review"):
            # Stage 1 already failed closed; keep its envelope.
            return {
                "evidence_brief": {},
                "requires_human_review": True,
                "degradation": state.get("degradation") or {},
                "status": AgentStatus.SUCCESS.value,
            }

        brief = state.get("evidence_brief") or empty_brief()
        registry = state.get("source_registry") or {}
        recalled = state.get("recalled_perspectives") or []

        violations = service.verify_citations(brief, registry)
        gaps = service.coverage_gaps(brief, recalled)
        evidence_map = service.build_evidence_map(brief, registry)

        review_reason = ""
        extra_gap_codes: set[str] = set()

        if violations:
            # Report the most specific violation first.
            for code in ("citation_missing", "citation_span_mismatch", "citation_not_quotable"):
                if code in violations:
                    review_reason = code
                    break
        elif not brief.get("perspectives") and recalled:
            review_reason = "required_evidence_absent"

        # Stage 1 computed these; releasing a brief while they fail would make
        # the checks decorative (they were previously calculated and ignored).
        if not review_reason:
            missing_docs = (state.get("docs_present") or {}).get("missing") or []
            if missing_docs:
                review_reason = "required_evidence_absent"
                extra_gap_codes.add("evidence_absent")

        if not review_reason:
            authority = state.get("authority_match") or {}
            # Absent authority data is indeterminable, not "fine".
            if not authority.get("matched", False):
                review_reason = "authority_unmatched"
                extra_gap_codes.add("authority_unmatched")

        if not review_reason:
            # Deterministic coverage confidence: what share of the recalled
            # perspectives the brief actually evidences. No LLM self-scoring.
            threshold = float(self._config.get("evidence_confidence_threshold", 0.75))
            if recalled:
                covered = len(brief.get("perspectives") or [])
                confidence = covered / len(recalled)
                if confidence < threshold:
                    review_reason = "confidence_below_threshold"

        if review_reason:
            gap_codes = sorted(
                {g["gap_code"] for g in gaps}
                | extra_gap_codes
                | ({"evidence_not_quotable"} if review_reason == "citation_not_quotable" else set())
            )
            _audit(
                "cv_evidence_rejected",
                {
                    "case_ref": state.get("case_ref", ""),
                    "review_reason_code": review_reason,
                    "count": len(violations),
                },
                state,
            )
            return {
                "evidence_brief": {},
                "evidence_map": {},
                "gaps": gaps,
                "requires_human_review": True,
                "degradation": {
                    "review_reason_code": review_reason,
                    "gap_codes": gap_codes,
                    "unknown_codes": [],
                },
                "status": AgentStatus.SUCCESS.value,
            }

        _audit(
            "cv_evidence_verified",
            {"case_ref": state.get("case_ref", ""), "count": len(evidence_map)},
            state,
        )
        # Coverage gaps are informative, not a failure: they are what the
        # drafter must fill before escalating for approval.
        merged = dict(brief)
        merged["gaps"] = list(brief.get("gaps") or []) + gaps
        return {
            "evidence_brief": merged,
            "evidence_map": evidence_map,
            "gaps": gaps,
            "requires_human_review": False,
            "status": AgentStatus.SUCCESS.value,
        }


class IngestNode(_DomainNode):
    """Accumulate approved baselines, clauses, authority matrix and perspectives."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None, kb: Any = None) -> None:
        super().__init__()
        self._config = config or {}
        self._kb = kb

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early

        # Reporting written_ids without a durable store would tell the caller
        # their records were accepted when nothing was persisted.
        if self._kb is None:
            _audit("cv_ingest_rejected", {"status": "kb_unavailable"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ingest requires a provisioned knowledge base"],
            }

        records = (state.get("validated_input") or {}).get("records") or []
        written: list[str] = []
        rejected: list[dict] = []

        for record in records:
            projected = service.project_perspective_record(record)
            perspective_id = projected.get("perspective_id")
            code = projected.get("perspective_code")
            if not perspective_id:
                rejected.append({"reason": "missing_perspective_id"})
                continue
            # Ids are echoed back in the summary, so a caller-supplied prose id
            # ("本変更は適法") would otherwise become an egress channel.
            if not is_valid_identifier(perspective_id):
                rejected.append({"reason": "malformed_perspective_id"})
                continue
            if code not in contracts.PERSPECTIVE_CODES:
                rejected.append({"reason": "unknown_perspective_code"})
                continue
            if self._kb is not None:
                try:
                    self._kb.write(projected)
                except Exception:
                    rejected.append({"reason": "write_failed"})
                    continue
            written.append(str(perspective_id))

        _audit(
            "cv_records_ingested",
            {"mode": contracts.MODE_INGEST, "count": len(written)},
            state,
        )
        return {
            "ingest_result": {"written_ids": written, "rejected": rejected},
            "status": AgentStatus.SUCCESS.value,
        }


class FeedbackNode(_DomainNode):
    """Record human-confirmed perspectives with explicit approval and an audit event.

    This is where human involvement lands: the agent has no in-graph HITL, so
    corrections arrive asynchronously through this mode.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None, kb: Any = None) -> None:
        super().__init__()
        self._config = config or {}
        self._kb = kb

    def execute(self, state: dict) -> dict:
        early = _passthrough_error(state)
        if early:
            return early

        validated = state.get("validated_input") or {}

        # Approver authority comes from the verified context, never from a
        # self-asserted field in the request body. An incomplete context (e.g.
        # a framework conformance probe) must fail closed with a normal error
        # delta rather than raising out of execute().
        try:
            ctx = InvocationContext.from_state(state)
            approver_role = getattr(ctx, "caller_id", "") or ""
        except Exception:
            approver_role = ""
        if not approver_role:
            _audit("cv_feedback_rejected", {"status": "approver_unresolved"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["feedback rejected: approver could not be resolved from context"],
            }

        if self._kb is None:
            _audit("cv_feedback_rejected", {"status": "kb_unavailable"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["feedback requires a provisioned knowledge base"],
            }

        accepted: list[str] = []
        quarantined = 0
        for record in (validated.get("confirmed_perspectives") or []):
            projected = service.project_perspective_record(record)
            if not projected.get("perspective_id"):
                quarantined += 1
                continue
            if not is_valid_identifier(projected["perspective_id"]):
                quarantined += 1
                continue
            if projected.get("perspective_code") not in contracts.PERSPECTIVE_CODES:
                quarantined += 1
                continue
            projected["approved_by_role"] = approver_role
            if self._kb is not None:
                try:
                    self._kb.write(projected)
                except Exception:
                    quarantined += 1
                    continue
            accepted.append(str(projected["perspective_id"]))

        _audit(
            "cv_feedback_recorded",
            {"mode": contracts.MODE_FEEDBACK, "count": len(accepted)},
            state,
        )
        # Free-text commentary is deliberately NOT projected into the KB — it
        # stays in the audit trail so it can never re-enter an LLM context.
        return {
            "feedback_result": {
                "accepted_ids": accepted,
                "quarantined": quarantined,
            },
            "status": AgentStatus.SUCCESS.value,
        }
