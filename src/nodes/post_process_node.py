"""GOV-C2-110 — post_process: output assembly and the S-3 gate."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.services import contracts, egress
from src.services.egress import sanitize_audit_payload


class PostProcessNode(FunctionNode):
    """Assemble ``result`` and enforce the S-3 output contract.

    Branch order matters (docs/02_design.md): an upstream error passes through
    first, then the degradation path, and only then the normal brief. Checking
    the brief first would turn a deliberate withhold into a missing-field error
    — or tempt an implementation into returning the unverified draft anyway.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain checks — allowlist only, no assertive-phrase denylist.

        A denylist of phrases is defeated by paraphrase and yields false
        assurance. Non-conclusiveness is enforced structurally: the payload
        must match a closed schema whose conclusion fields simply do not
        exist, and every code must come from a closed enumeration.
        """
        payload = result.get("result")
        if payload is None:
            return result
        if not isinstance(payload, dict):
            raise ValueError("S-3: result must be an object")

        kind = payload.get("kind")
        if kind == egress.KIND_BRIEF:
            egress.validate_brief(payload.get("evidence_brief"))
        elif kind == egress.KIND_DEGRADATION:
            egress.validate_degradation(payload.get("degradation"))
        elif kind in (egress.KIND_INGEST, egress.KIND_FEEDBACK):
            egress.sanitize_for_egress(payload.get("summary"), kind)
        else:
            raise ValueError(f"S-3: unknown result kind {kind!r}")

        # Residual PII sweep over every nested string. The framework default
        # only scans top-level strings, so nested brief content would slip past.
        self._scan_pii(payload, "result")
        return result

    def _scan_pii(self, value: object, where: str) -> None:
        if isinstance(value, str):
            findings = detect_pii(value)
            if findings:
                raise ValueError(
                    f"S-3: PII pattern '{findings[0].get('type', 'unknown')}' detected in {where}"
                )
            return
        if isinstance(value, dict):
            for key, item in value.items():
                self._scan_pii(item, f"{where}.{key}")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                self._scan_pii(item, f"{where}[{index}]")

    def execute(self, state: dict) -> dict:
        # 1. upstream error — pass through untouched
        if state.get("status") == AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value}

        mode = state.get("mode") or contracts.MODE_INVOKE

        # 2. degradation — withhold the brief, return typed codes only
        if state.get("requires_human_review"):
            envelope = state.get("degradation") or {}
            emit_trace_event(
                "cv_degraded_response",
                sanitize_audit_payload({
                    "case_ref": state.get("case_ref", ""),
                    "review_reason_code": envelope.get("review_reason_code", ""),
                }),
                state,
            )
            return {
                "result": {
                    "kind": egress.KIND_DEGRADATION,
                    "requires_human_review": True,
                    "degradation": envelope,
                },
                "formatted_output": "",
                "status": AgentStatus.SUCCESS.value,
            }

        # 3/4. mode-specific normal paths
        if mode == contracts.MODE_INGEST:
            summary = state.get("ingest_result") or {}
            payload = {"kind": egress.KIND_INGEST, "summary": summary}
        elif mode == contracts.MODE_FEEDBACK:
            summary = state.get("feedback_result") or {}
            payload = {"kind": egress.KIND_FEEDBACK, "summary": summary}
        else:
            brief = state.get("evidence_brief") or {}
            if not brief:
                # Defence in depth: an absent brief on the normal path means an
                # upstream stage withheld it without setting the flag. Fail
                # closed with a typed envelope rather than emitting an empty or
                # malformed brief.
                emit_trace_event(
                    "cv_brief_absent",
                    sanitize_audit_payload({
                        "case_ref": state.get("case_ref", ""),
                        "review_reason_code": "required_evidence_absent",
                    }),
                    state,
                )
                return {
                    "result": {
                        "kind": egress.KIND_DEGRADATION,
                        "requires_human_review": True,
                        "degradation": {
                            "review_reason_code": "required_evidence_absent",
                            "gap_codes": [],
                            "unknown_codes": [],
                        },
                    },
                    "formatted_output": "",
                    "status": AgentStatus.SUCCESS.value,
                }
            payload = {
                "kind": egress.KIND_BRIEF,
                "evidence_brief": brief,
                "evidence_map": state.get("evidence_map") or {},
                "requires_human_review": False,
            }

        emit_trace_event(
            "cv_response_assembled",
            sanitize_audit_payload({"case_ref": state.get("case_ref", ""), "mode": mode}),
            state,
        )
        return {
            "result": payload,
            "formatted_output": "",
            "status": AgentStatus.SUCCESS.value,
        }
