"""GOV-C2-110 — pre_process: input control, mode routing, S-2 gate."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import contracts
from src.services.egress import is_valid_identifier, sanitize_audit_payload

# --- Input format gate ------------------------------------------------------
# Only lightweight text formats are accepted. PDF/Excel are rejected by magic
# bytes: adding parsers would pull in OCR failure modes and dependency weight
# for no gain here.
_REJECTED_MAGIC = {
    b"%PDF": "pdf",
    b"PK\x03\x04": "zip_or_xlsx",
    b"\xd0\xcf\x11\xe0": "legacy_office",
}

# --- IMP-76 regulated data (GOV is in scope) --------------------------------
# My Number identifiers, government classification markers and APPI art.17
# sensitive categories must be detected at the input boundary.
_MY_NUMBER_RE = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_CLASSIFICATION_MARKERS = ("極秘", "機密", "秘密指定", "取扱注意", "部外秘")
_SENSITIVE_MARKERS = ("病歴", "犯罪歴", "前科", "信条", "社会的身分", "人種", "障害者手帳")

_MAX_INPUT_CHARS = 200_000


class PreProcessNode(FunctionNode):
    """Resolve scope, validate input, decide the mode, mint the case reference."""

    # S-1: municipal contract records are internal business data.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 domain checks.

        Reads ONLY the raw ``user_input`` / ``input_context``. It must not
        depend on anything ``execute()`` writes (mode, case_ref, ...), because
        the gate runs before ``execute()`` — a gate that branches on
        execute-written state fails open.
        """
        raw = state.get("user_input", "")
        if isinstance(raw, (bytes, bytearray)):
            for magic, label in _REJECTED_MAGIC.items():
                if bytes(raw).startswith(magic):
                    raise ValueError(
                        f"S-2: binary input format '{label}' is not accepted; "
                        "supply JSON/CSV/plain text"
                    )
            raise ValueError("S-2: binary input is not accepted")

        if isinstance(raw, str):
            if len(raw) > _MAX_INPUT_CHARS:
                raise ValueError("S-2: input exceeds the permitted size")
            for magic, label in _REJECTED_MAGIC.items():
                if raw.startswith(magic.decode("latin-1")):
                    raise ValueError(
                        f"S-2: binary input format '{label}' is not accepted; "
                        "supply JSON/CSV/plain text"
                    )
            if _MY_NUMBER_RE.search(raw):
                raise ValueError(
                    "S-2: a My Number style identifier was detected in the input "
                    "(IMP-76 regulated data); remove it before submitting"
                )
            for marker in _CLASSIFICATION_MARKERS:
                if marker in raw:
                    raise ValueError(
                        f"S-2: government classification marker '{marker}' detected "
                        "(IMP-76 regulated data)"
                    )
            for marker in _SENSITIVE_MARKERS:
                if marker in raw:
                    raise ValueError(
                        f"S-2: APPI art.17 sensitive category '{marker}' detected "
                        "(IMP-76 regulated data)"
                    )

            # Mode allowlist on the RAW value, before execute() interprets it.
            mode = self._raw_mode(raw, state)
            if mode is not None and mode not in contracts.ALLOWED_MODES:
                raise ValueError(f"S-2: mode '{mode}' is not an allowed mode")

        return state

    @staticmethod
    def _raw_mode(raw: str, state: dict) -> str | None:
        context = state.get("input_context")
        if isinstance(context, dict) and context.get("mode") is not None:
            return str(context["mode"])
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(payload, dict) and payload.get("mode") is not None:
            return str(payload["mode"])
        return None

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        if not isinstance(raw, str) or not raw.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        try:
            payload: Any = json.loads(raw)
        except (ValueError, TypeError):
            payload = {"variation_rationale": raw}
        if not isinstance(payload, dict):
            payload = {"variation_rationale": str(payload)}

        context = state.get("input_context") or {}
        mode = str(context.get("mode") or payload.get("mode") or contracts.MODE_INVOKE)
        if mode not in contracts.ALLOWED_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unsupported mode '{mode}'"],
            }

        case_ref = str(payload.get("case_ref") or context.get("case_ref") or "")
        if mode in (contracts.MODE_INVOKE, contracts.MODE_FEEDBACK) and not case_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: case_ref is required for this mode"],
            }
        # case_ref is echoed into audit events, so it must be an opaque handle:
        # a caller-supplied "本変更は適法" would otherwise land in the log.
        if case_ref and not is_valid_identifier(case_ref):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: case_ref must be an opaque identifier"],
            }

        validated = {
            "mode": mode,
            "variation_rationale": str(payload.get("variation_rationale") or ""),
            "evidence": payload.get("evidence") or [],
            "versions": payload.get("versions") or {},
            "authority": payload.get("authority") or {},
            "records": payload.get("records") or [],
            "confirmed_perspectives": payload.get("confirmed_perspectives") or [],
        }

        emit_trace_event(
            "cv_input_validated",
            sanitize_audit_payload({"mode": mode, "case_ref": case_ref}),
            state,
        )
        return {
            "mode": mode,
            "case_ref": case_ref,
            "validated_input": validated,
            "requires_human_review": False,
            "degradation": {},
            "status": AgentStatus.SUCCESS.value,
        }
