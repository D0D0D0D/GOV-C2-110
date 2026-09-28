"""GOV-C2-110 — single egress choke point.

Everything that leaves the agent passes through :func:`sanitize_for_egress`.

The S-3 gate only runs in the outer ``post_process`` node, so without this
module the degradation return and the error envelope would reach the caller
without structural checking — and an unverified Stage 3 draft or a raw
exception message could carry a prohibited legal conclusion out with it.

Validation is *fail-closed*: anything not explicitly allowed is rejected.
"""

from __future__ import annotations

import re
from typing import Any

from src.services import contracts

_ID_RE = re.compile(contracts.ID_PATTERN)

# Fields whose value must be an opaque handle, never free text. Without this a
# conclusion rides out inside a field that is nominally "an id"
# (fact_id="承認すべきである" passes a key-only allowlist).
_ID_FIELDS = frozenset({
    "fact_id", "perspective_id", "source_id", "source_id_a", "source_id_b",
    "question_template_id", "span_ref", "doc_ref", "gap_code",
    "contradiction_code", "unknown_code", "review_reason_code",
    "perspective_code", "record_version", "approved_at", "approved_by_role",
})

# Provenance is metadata, not prose: only these keys, each an opaque handle.
_PROVENANCE_FIELDS = frozenset({
    "doc_ref", "contract_version", "authority_matrix_version", "effective_date",
})


def is_valid_identifier(value: Any) -> bool:
    """True when ``value`` is an opaque handle (no prose)."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _check_id(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        _reject(
            f"{where}: {value!r} is not an opaque identifier "
            f"(must match {contracts.ID_PATTERN})"
        )


def _check_provenance(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        _reject(f"{where}: provenance must be an object")
    unknown = set(value) - _PROVENANCE_FIELDS
    if unknown:
        _reject(f"{where}: provenance carries unexpected field(s) {sorted(unknown)}")
    for key, item in value.items():
        _check_id(item, f"{where}.provenance.{key}")

# Kinds of payload that may be emitted.
KIND_BRIEF = "brief"
KIND_DEGRADATION = "degradation"
KIND_ERROR = "error"
KIND_INGEST = "ingest"
KIND_FEEDBACK = "feedback"


class EgressViolation(ValueError):
    """Raised when a payload does not satisfy its closed contract."""


def _reject(message: str) -> None:
    raise EgressViolation(f"egress: {message}")


def _require_plain(value: Any, where: str) -> None:
    """Only JSON-serialisable primitives/containers may leave the agent."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return
    if isinstance(value, list):
        for item in value:
            _require_plain(item, where)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _reject(f"{where}: non-string key {key!r}")
            _require_plain(item, f"{where}.{key}")
        return
    _reject(f"{where}: non-serialisable value of type {type(value).__name__}")


def _check_element(section: str, element: Any, index: int) -> None:
    if not isinstance(element, dict):
        _reject(f"{section}[{index}] must be an object")
    allowed = contracts.BRIEF_ELEMENT_FIELDS[section]
    optional = contracts.BRIEF_OPTIONAL_ELEMENT_FIELDS.get(section, frozenset())
    keys = set(element.keys())
    unknown = keys - allowed
    if unknown:
        # This is where a conclusion field (e.g. legality_verdict) dies even if
        # a model invents one: it is simply not in the allowlist.
        _reject(f"{section}[{index}]: unexpected field(s) {sorted(unknown)}")
    missing = (allowed - optional) - keys
    if missing:
        _reject(f"{section}[{index}]: missing field(s) {sorted(missing)}")

    # Every id/code field must be an opaque handle, never prose. This is what
    # stops a conclusion riding out inside fact_id / span_ref / doc_ref.
    for key, value in element.items():
        if key in _ID_FIELDS:
            _check_id(value, f"{section}[{index}].{key}")
        elif key == "provenance":
            _check_provenance(value, f"{section}[{index}]")
        elif key != "verbatim_quote":
            # verbatim_quote is the sole free-text field; it is separately
            # constrained by exact registry match + span_kind allowlist.
            _reject(f"{section}[{index}].{key}: unconstrained free-text field")

    # Closed code enumerations — a free-text "code" would be a leak path.
    if section == "gaps" and element["gap_code"] not in contracts.GAP_CODES:
        _reject(f"gaps[{index}]: gap_code {element['gap_code']!r} is not an allowed code")
    if (
        section == "contradictions"
        and element["contradiction_code"] not in contracts.CONTRADICTION_CODES
    ):
        _reject(
            f"contradictions[{index}]: contradiction_code "
            f"{element['contradiction_code']!r} is not an allowed code"
        )
    if section == "unknowns" and element["unknown_code"] not in contracts.UNKNOWN_CODES:
        _reject(
            f"unknowns[{index}]: unknown_code {element['unknown_code']!r} is not an allowed code"
        )


def validate_brief(brief: Any) -> dict:
    """Validate ``evidence_brief`` against the closed output contract.

    Recursive: ``additionalProperties: false`` is enforced at every level, not
    just on the top-level keys. A key-only allowlist would still let a model
    smuggle a conclusion into the free text of an allowed field.
    """
    if not isinstance(brief, dict):
        _reject("evidence_brief must be an object")
    keys = set(brief.keys())
    unknown = keys - contracts.BRIEF_FIELDS
    if unknown:
        _reject(f"evidence_brief: unexpected field(s) {sorted(unknown)}")
    missing = contracts.BRIEF_FIELDS - keys
    if missing:
        _reject(f"evidence_brief: missing field(s) {sorted(missing)}")

    for section in contracts.BRIEF_FIELDS:
        value = brief[section]
        if not isinstance(value, list):
            _reject(f"evidence_brief.{section} must be a list")
        for index, element in enumerate(value):
            _check_element(section, element, index)

    _require_plain(brief, "evidence_brief")
    return brief


def validate_degradation(envelope: Any) -> dict:
    """Validate the degradation envelope returned instead of a brief."""
    if not isinstance(envelope, dict):
        _reject("degradation must be an object")
    keys = set(envelope.keys())
    unknown = keys - contracts.DEGRADATION_FIELDS
    if unknown:
        _reject(f"degradation: unexpected field(s) {sorted(unknown)}")
    missing = contracts.DEGRADATION_FIELDS - keys
    if missing:
        _reject(f"degradation: missing field(s) {sorted(missing)}")

    reason = envelope["review_reason_code"]
    if reason not in contracts.REVIEW_REASON_CODES:
        _reject(f"degradation: review_reason_code {reason!r} is not an allowed code")

    for field, allowed in (
        ("gap_codes", contracts.GAP_CODES),
        ("unknown_codes", contracts.UNKNOWN_CODES),
    ):
        codes = envelope[field]
        if not isinstance(codes, list):
            _reject(f"degradation.{field} must be a list")
        for code in codes:
            if code not in allowed:
                _reject(f"degradation.{field}: {code!r} is not an allowed code")

    _require_plain(envelope, "degradation")
    return envelope


_SUMMARY_FIELDS = {
    KIND_INGEST: frozenset({"written_ids", "rejected"}),
    KIND_FEEDBACK: frozenset({"accepted_ids", "quarantined"}),
}


def validate_mode_summary(summary: Any, kind: str) -> dict:
    """Validate an ingest/feedback summary against its closed shape.

    Accepting "any JSON object" here made these two modes an open egress
    channel: the nodes echo caller-supplied ids back, so a submitted
    ``perspective_id`` of "本変更は適法" would be returned verbatim.
    """
    if not isinstance(summary, dict):
        _reject(f"{kind} summary must be an object")
    allowed = _SUMMARY_FIELDS[kind]
    unknown = set(summary) - allowed
    if unknown:
        _reject(f"{kind} summary: unexpected field(s) {sorted(unknown)}")

    for field in ("written_ids", "accepted_ids"):
        if field in summary:
            ids = summary[field]
            if not isinstance(ids, list):
                _reject(f"{kind} summary.{field} must be a list")
            for value in ids:
                _check_id(value, f"{kind} summary.{field}")

    if "quarantined" in summary:
        value = summary["quarantined"]
        if not isinstance(value, int) or isinstance(value, bool):
            _reject(f"{kind} summary.quarantined must be an integer")

    if "rejected" in summary:
        rejected = summary["rejected"]
        if not isinstance(rejected, list):
            _reject(f"{kind} summary.rejected must be a list")
        for item in rejected:
            if not isinstance(item, dict) or set(item) - {"reason"}:
                _reject(f"{kind} summary.rejected entries must be " + "{reason} only")
            if item.get("reason") not in contracts.REJECT_REASONS:
                _reject(f"{kind} summary.rejected: {item.get('reason')!r} is not an allowed reason")

    _require_plain(summary, kind)
    return summary


def build_error(error_code: str) -> dict:
    """Build the only error shape the agent ever returns.

    Deliberately carries no exception message, stack trace, state or input
    echo — those are the classic channels through which a raw upstream failure
    leaks case content.
    """
    if error_code not in contracts.ERROR_CODES:
        error_code = "internal_error"
    return {"status": "error", "error_code": error_code}


def sanitize_audit_payload(payload: dict) -> dict:
    """Drop anything not on the S-4 audit allowlist.

    Logs are an egress channel: emitting ``validated_input``, a draft, a quoted
    span, feedback text or an exception message would leak conclusions even
    when the response itself is clean.
    """
    if not isinstance(payload, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in contracts.AUDIT_PAYLOAD_FIELDS:
            continue
        if key in ("count", "elapsed_ms", "record_version"):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
            continue
        # Key-name filtering alone is not enough: a caller-supplied case_ref of
        # "本変更は適法" would otherwise be written straight into the audit log.
        if isinstance(value, str) and _ID_RE.match(value):
            clean[key] = value
    _require_plain(clean, "audit")
    return clean


def sanitize_for_egress(payload: Any, kind: str) -> dict:
    """Validate ``payload`` for the given egress ``kind``.

    Raises :class:`EgressViolation` when the payload violates its contract, so
    a malformed payload fails closed instead of being emitted.
    """
    if kind == KIND_BRIEF:
        return validate_brief(payload)
    if kind == KIND_DEGRADATION:
        return validate_degradation(payload)
    if kind == KIND_ERROR:
        code = payload.get("error_code") if isinstance(payload, dict) else None
        return build_error(code if isinstance(code, str) else "internal_error")
    if kind in (KIND_INGEST, KIND_FEEDBACK):
        return validate_mode_summary(payload, kind)
    _reject(f"unknown egress kind {kind!r}")
    raise AssertionError("unreachable")  # pragma: no cover
