"""GOV-C2-110 — deterministic domain service.

Everything in this module is deterministic: source-handle minting, span-kind
classification, version reconciliation, authority-matrix matching and
claim->source verification. No LLM is involved here.

That split is the point of the design: the LLM only *maps* free-text variation
rationale onto approved fact-check perspectives, while every factual string,
identifier and code is produced or checked by the code below.
"""

from __future__ import annotations

import re
import unicodedata

from src.services import contracts

# Zero-width and formatting characters that could break a keyword apart.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD]
)


def _normalise(text: str) -> str:
    """NFKC-fold, strip invisible characters and drop whitespace.

    Keyword checks run on this form so that "許 容", "許​容" and "許容" are
    all caught. Regex checks still run on the original text, where spacing is
    meaningful (e.g. "30 日").
    """
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return "".join(folded.split())

# ---------------------------------------------------------------------------
# span_kind classification (deterministic — never LLM-assigned)
# ---------------------------------------------------------------------------
# Classification is intentionally conservative: anything that is not clearly a
# factual span falls through to ``unclassified``, which is NOT quotable. The
# allowlist plus this fail-closed default is what stops a source document's own
# legal conclusion from being quoted verbatim into the brief.

# Markers of adjudicative / evaluative text. These are used to *withhold*
# quoting (fail-closed), never to bless text as safe, so a miss degrades to
# `unclassified` rather than to an emitted conclusion.
_ADJUDICATIVE_MARKERS = (
    "許容", "不許容", "適法", "違法", "合法", "実質的変更", "競争性",
    "承認が相当", "承認相当", "承認して差し支え", "差し支えない", "認められる",
    "妥当と判断", "問題ない", "可とする", "不可とする",
)
_OPINION_MARKERS = (
    "と解される", "と考えられる", "意見", "見解", "評価する", "判断する",
    "所見", "答申", "助言",
)

_QUANTITY_RE = re.compile(
    # A currency symbol before the digits is enough (¥3,200,000), and so is a
    # unit after them (30 日). Requiring the trailing unit alone missed the
    # most common estimate form in Japanese municipal documents.
    r"[¥￥]\s*\d[\d,]*"
    r"|\d[\d,]*\s*(?:円|万円|千円|日間|日|箇月|ヶ月|か月|%|％|m3|m2|m|ｍ|件|本|基|式)"
)
_DATE_RE = re.compile(
    r"(令和|平成|昭和)\s*\d+\s*年|\d{4}\s*[/年-]\s*\d{1,2}\s*[/月-]\s*\d{1,2}"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+[-－][A-Za-z0-9぀-ヿ一-鿿]+[-－][A-Za-z0-9]+")
_PROCEDURAL_MARKERS = ("協議", "立会", "指示", "通知", "届出", "報告", "決裁", "起案", "受領")
_OBSERVED_MARKERS = ("発見", "確認された", "判明", "出土", "検出", "現地", "掘削")

# Evaluative sentence shapes that carry a judgement without using any of the
# adjudicative keywords above — e.g. "契約変更に支障はない" (a conclusion) shares
# the word 支障 with genuinely factual text ("埋設管が支障となる").
#
# Chasing individual words is a losing game, so this is a STRUCTURAL guard: any
# span matching an evaluative shape is demoted to `unclassified` (not quotable)
# even if a factual marker also matched. Missing a phrasing costs a false
# `unclassified` (the fact is reported as a gap), never a leaked conclusion.
_EVALUATIVE_SHAPES = (
    "はない", "ではない", "ありません", "当たらない", "あたらない",
    "できる", "できます", "してよい", "して良い", "が相当", "が適当",
    "正当", "妥当", "適切である", "支障はな", "問題はな", "問題な",
    "範囲内", "認容", "許され", "ゆるされ",
)


# A quotable span must be one atomic fact. Sentence terminators, conjunctions
# and excessive length all indicate prose that may carry a judgement alongside
# the factual token.
_CLAUSE_BREAKS = ("。", "．", "\n", "、また", "したがって", "よって", "ゆえに", "ため、")
_MAX_QUOTABLE_CHARS = 120


def _is_multi_clause(text: str) -> bool:
    """True when the span is prose rather than a single atomic fact."""
    stripped = text.strip()
    if len(stripped) > _MAX_QUOTABLE_CHARS:
        return True
    # A single trailing terminator is fine; an internal one means >1 clause.
    body = stripped.rstrip("。．")
    return any(marker in body for marker in _CLAUSE_BREAKS)


def classify_span_kind(text: str) -> str:
    """Return the deterministic ``span_kind`` for a source span.

    Precedence is deliberate and fail-closed:

    1. adjudicative / opinion keywords  -> not quotable
    2. **evaluative sentence shape**    -> ``unclassified`` (not quotable), even
       when a factual pattern also matches. Without this, a conclusion such as
       "契約変更に支障はない" would be promoted to ``observed_condition`` merely
       because it shares a word with genuinely factual text.
    3. factual patterns                 -> quotable
    4. anything else                    -> ``unclassified`` (not quotable)

    Normalisation strips whitespace and zero-width characters first so that
    spaced or zero-width-injected variants cannot slip past step 1.
    """
    if not isinstance(text, str) or not text.strip():
        return contracts.SPAN_KIND_UNCLASSIFIED

    normalised = _normalise(text)

    for marker in _ADJUDICATIVE_MARKERS:
        if marker in normalised:
            return contracts.SPAN_KIND_ADJUDICATIVE
    for marker in _OPINION_MARKERS:
        if marker in normalised:
            return contracts.SPAN_KIND_OPINION
    # Structural guard — an evaluative shape outranks any factual pattern.
    for shape in _EVALUATIVE_SHAPES:
        if shape in normalised:
            return contracts.SPAN_KIND_UNCLASSIFIED

    # A span must be ATOMIC to be quotable. Blessing a whole paragraph because
    # it happens to contain "30 日" lets "本変更を承認すべきである。工期30日"
    # through as `quantity`. Multi-clause prose is therefore never quotable —
    # the ingest side is expected to supply one fact per span.
    if _is_multi_clause(text):
        return contracts.SPAN_KIND_UNCLASSIFIED

    if _DATE_RE.search(text):
        return contracts.SPAN_KIND_DATE
    if _QUANTITY_RE.search(text):
        return contracts.SPAN_KIND_QUANTITY
    if _IDENTIFIER_RE.search(text):
        return contracts.SPAN_KIND_IDENTIFIER
    if any(marker in normalised for marker in _OBSERVED_MARKERS):
        return contracts.SPAN_KIND_OBSERVED_CONDITION
    if any(marker in normalised for marker in _PROCEDURAL_MARKERS):
        return contracts.SPAN_KIND_PROCEDURAL_RECORD

    return contracts.SPAN_KIND_UNCLASSIFIED


def build_source_registry(evidence: list[dict]) -> dict:
    """Mint deterministic source handles from supplied evidence spans.

    The LLM may only *select* handles produced here; it can never mint a
    ``source_id``, which is what stops fabricated citations at the root.
    """
    registry: dict[str, dict] = {}
    for doc_index, document in enumerate(evidence or [], start=1):
        if not isinstance(document, dict):
            continue
        doc_ref = str(document.get("doc_ref") or f"DOC-{doc_index:02d}")
        provenance = document.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        for span_index, span in enumerate(document.get("spans") or [], start=1):
            if not isinstance(span, dict):
                continue
            text = str(span.get("text") or "")
            source_id = f"S-{doc_index:02d}-{span_index:02d}"
            registry[source_id] = {
                "doc_ref": doc_ref,
                "span_ref": str(span.get("span_ref") or f"p{doc_index}-s{span_index}"),
                "span_kind": classify_span_kind(text),
                "text": text,
                "provenance": provenance,
            }
    return registry


def is_quotable(registry: dict, source_id: str) -> bool:
    entry = registry.get(source_id)
    if not isinstance(entry, dict):
        return False
    return entry.get("span_kind") in contracts.QUOTABLE_SPAN_KINDS


# ---------------------------------------------------------------------------
# Version reconciliation (review finding — design-entry acceptance criterion)
# ---------------------------------------------------------------------------

REQUIRED_VERSION_FIELDS = ("contract_version", "authority_matrix_version", "effective_date")


def check_versions(declared: dict, baseline: dict) -> dict:
    """Reconcile declared case versions against the approved baseline.

    Absent or mismatched versions must NOT be interpreted away: the caller
    fails closed to unknowns/questions instead of reasoning over a baseline it
    cannot trust.
    """
    mismatches: list[str] = []
    declared = declared if isinstance(declared, dict) else {}
    baseline = baseline if isinstance(baseline, dict) else {}

    for field in REQUIRED_VERSION_FIELDS:
        declared_value = declared.get(field)
        if not declared_value:
            mismatches.append(f"{field}:undeclared")
            continue
        baseline_value = baseline.get(field)
        if baseline_value and str(declared_value) != str(baseline_value):
            mismatches.append(f"{field}:mismatch")

    return {"ok": not mismatches, "mismatches": mismatches}


def match_authority(declared: dict, authority_matrix: dict) -> dict:
    """Deterministically compare the approver against the delegated-authority matrix.

    Reports agreement as a fact; it never concludes that the approval itself is
    valid or that the variation may proceed.
    """
    declared = declared if isinstance(declared, dict) else {}
    authority_matrix = authority_matrix if isinstance(authority_matrix, dict) else {}

    approver_role = declared.get("approver_role")
    band = declared.get("authority_band")
    expected = authority_matrix.get(str(band)) if band is not None else None

    if not approver_role or expected is None:
        return {"matched": False, "reason": "authority_undeterminable"}
    return {
        "matched": str(approver_role) == str(expected),
        "reason": "compared_against_matrix",
    }


def check_required_documents(evidence: list[dict], required_doc_types: list[str]) -> dict:
    """Presence check for the configured required document types."""
    present = {
        str(document.get("doc_type"))
        for document in (evidence or [])
        if isinstance(document, dict) and document.get("doc_type")
    }
    missing = [doc_type for doc_type in (required_doc_types or []) if doc_type not in present]
    return {"present": sorted(present), "missing": missing}


# ---------------------------------------------------------------------------
# claim -> source verification
# ---------------------------------------------------------------------------


def build_evidence_map(brief: dict, registry: dict) -> dict:
    """Build the claim->source map deterministically from the registry.

    Built from the registry rather than from whatever the model asserted, so
    the map cannot inherit a fabricated citation.
    """
    mapping: dict[str, dict] = {}
    for fact in (brief.get("stated_facts") or []):
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get("fact_id")
        source_id = fact.get("source_id")
        entry = registry.get(source_id) if isinstance(source_id, str) else None
        if not fact_id or not isinstance(entry, dict):
            continue
        mapping[str(fact_id)] = {
            "source_id": source_id,
            "doc_ref": entry.get("doc_ref", ""),
            "span_ref": entry.get("span_ref", ""),
            "span_kind": entry.get("span_kind", contracts.SPAN_KIND_UNCLASSIFIED),
        }
    return mapping


def verify_citations(brief: dict, registry: dict) -> list[str]:
    """Return violation codes for every unsound citation in ``brief``.

    Three distinct failures are detected:

    * ``citation_missing``      — the source_id does not exist in the registry
    * ``citation_span_mismatch``— the quote is not verbatim for that span
    * ``citation_not_quotable`` — the span exists and matches, but its
      ``span_kind`` is adjudicative/opinion/unclassified, so quoting it would
      emit a conclusion sourced from the evidence itself
    """
    violations: list[str] = []

    for fact in (brief.get("stated_facts") or []):
        if not isinstance(fact, dict):
            violations.append("citation_missing")
            continue
        entry = registry.get(fact.get("source_id"))
        if not isinstance(entry, dict):
            violations.append("citation_missing")
            continue
        if str(fact.get("verbatim_quote", "")).strip() != str(entry.get("text", "")).strip():
            violations.append("citation_span_mismatch")
            continue
        if entry.get("span_kind") not in contracts.QUOTABLE_SPAN_KINDS:
            violations.append("citation_not_quotable")

    # Perspectives, sources and contradictions must reference real handles too.
    for section, fields in (
        ("perspectives", ("source_id",)),
        ("evidence_sources", ("source_id",)),
        ("contradictions", ("source_id_a", "source_id_b")),
    ):
        for element in (brief.get(section) or []):
            if not isinstance(element, dict):
                violations.append("citation_missing")
                continue
            for field in fields:
                if element.get(field) not in registry:
                    violations.append("citation_missing")

    return violations


def coverage_gaps(brief: dict, recalled_perspectives: list[dict]) -> list[dict]:
    """Perspectives that were recalled but are not covered by the brief."""
    covered = {
        element.get("perspective_id")
        for element in (brief.get("perspectives") or [])
        if isinstance(element, dict)
    }
    gaps: list[dict] = []
    for perspective in (recalled_perspectives or []):
        if not isinstance(perspective, dict):
            continue
        perspective_id = perspective.get("perspective_id")
        if perspective_id and perspective_id not in covered:
            gaps.append({
                "perspective_id": perspective_id,
                "gap_code": "evidence_absent",
            })
    return gaps


def project_perspective_record(record: dict) -> dict:
    """Reduce a stored perspective to the retrieval-safe projection.

    Anything outside :data:`contracts.PERSPECTIVE_RECORD_FIELDS` — notably past
    decisions, adjudicative outcomes and free-text human feedback — is dropped
    so it can never re-enter an LLM context through retrieval.
    """
    if not isinstance(record, dict):
        return {}
    return {k: v for k, v in record.items() if k in contracts.PERSPECTIVE_RECORD_FIELDS}
