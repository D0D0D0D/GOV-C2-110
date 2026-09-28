"""GOV-C2-110 — LLM injection seam.

The template never names a concrete client or model id: the graph is
constructed with ``config["llm"]`` and the object only has to expose
``complete(messages) -> dict`` (the :class:`BaseLLM` shape). Deployments inject
whichever client they are allowed to use.

When no LLM is injected the agent degrades deterministically rather than
failing: :func:`draft_evidence_brief` returns an empty, schema-valid brief and
a ``degradation_reason``. An empty brief is safe by construction — it carries
no claim, so it can carry no conclusion — and Stage 4 turns the missing
coverage into gaps.
"""

from __future__ import annotations

import json
from typing import Any

from src.services import contracts, egress


def _field_schema(field: str) -> dict[str, Any]:
    allowed = contracts.BRIEF_ELEMENT_FIELDS[field]
    optional = contracts.BRIEF_OPTIONAL_ELEMENT_FIELDS.get(field, frozenset())
    properties: dict[str, Any] = {}
    for key in sorted(allowed):
        if key == "gap_code":
            properties[key] = {"type": "string", "enum": sorted(contracts.GAP_CODES)}
        elif key == "contradiction_code":
            properties[key] = {"type": "string", "enum": sorted(contracts.CONTRADICTION_CODES)}
        elif key == "unknown_code":
            properties[key] = {"type": "string", "enum": sorted(contracts.UNKNOWN_CODES)}
        elif key == "provenance":
            properties[key] = {
                "type": "object",
                "description": "opaque provenance handles only; use {} when no provenance is available",
            }
        elif key == "verbatim_quote":
            properties[key] = {
                "type": "string",
                "description": "must exactly equal text from the selected quotable source span",
            }
        else:
            properties[key] = {
                "type": "string",
                "pattern": contracts.ID_PATTERN,
                "description": "opaque handle selected from the prompt, never prose",
            }
    return {
        "type": "array",
        "items": {
            "type": "object",
            "required": sorted(allowed - optional),
            "optional": sorted(optional),
            "properties": properties,
            "additionalProperties": False,
        },
    }


def brief_response_schema() -> dict[str, Any]:
    """Render the closed brief contract from ``contracts.py`` for the prompt."""
    return {
        "type": "object",
        "required": sorted(contracts.BRIEF_FIELDS),
        "properties": {field: _field_schema(field) for field in sorted(contracts.BRIEF_FIELDS)},
        "additionalProperties": False,
    }


def build_mapping_prompt(
    rationale: str,
    perspectives: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
) -> str:
    """Assemble the Stage 3 prompt.

    The model is asked to *select* handles, never to author factual strings:
    every id it may use is enumerated in the prompt, and non-quotable spans are
    withheld from the quotable list so it cannot choose them.
    """
    quotable = {
        source_id: entry
        for source_id, entry in registry.items()
        if entry.get("span_kind") in contracts.QUOTABLE_SPAN_KINDS
    }
    lines = [
        "あなたは自治体の契約変更レビューの補助を行う。",
        "与えられた『変更理由の自由記述』を、人間が事前承認した事実確認観点に対応づけよ。",
        "",
        "厳守事項:",
        "- 出力は指定された JSON スキーマのみ。散文を書かない。",
        "- トップレベルおよび各要素で additionalProperties は false。未定義キーを出さない。",
        "- source_id / perspective_id / question_template_id は与えられた一覧からの選択のみ。新規に作らない。",
        "- verbatim_quote は与えられた span の text をそのまま写す（要約・言い換え・加筆をしない）。",
        "- すべて空配列の応答は禁止。対応できる候補がある場合は根拠を持つ section を埋める。",
        "- 約款上の許容/違法・実質的変更・競争性・承認推奨・積算や工期の妥当性を判断しない。",
        "  それらを述べるフィールドは存在しない。事実の対応づけと不足の指摘だけを行う。",
        "",
        "## 出力 JSON スキーマ（src/services/contracts.py から生成）",
        json.dumps(brief_response_schema(), ensure_ascii=False, sort_keys=True),
        "",
        "## 変更理由（自由記述）",
        rationale or "(未記載)",
        "",
        "## 承認済み事実確認観点",
        json.dumps(
            [
                {
                    "perspective_id": p.get("perspective_id"),
                    "perspective_code": p.get("perspective_code"),
                    "required_fact_codes": p.get("required_fact_codes", []),
                    "question_template_ids": p.get("question_template_ids", []),
                }
                for p in (perspectives or [])
            ],
            ensure_ascii=False,
        ),
        "",
        "## 引用可能な証跡 span（これ以外は引用してはならない）",
        json.dumps(
            [{"source_id": sid, "span_ref": e.get("span_ref"), "text": e.get("text")} for sid, e in quotable.items()],
            ensure_ascii=False,
        ),
    ]
    return "\n".join(lines)


def empty_brief() -> dict[str, list[Any]]:
    """A schema-valid brief with no content (safe degradation target)."""
    return {field: [] for field in contracts.BRIEF_FIELDS}


def _is_empty_brief(brief: dict[str, Any]) -> bool:
    return all(not brief.get(field) for field in contracts.BRIEF_FIELDS)


def _coerce_brief(payload: Any) -> tuple[dict[str, Any] | None, str]:
    """Parse and validate the model reply against the closed brief contract."""
    if isinstance(payload, dict):
        candidate = payload
    elif isinstance(payload, str):
        try:
            candidate = json.loads(payload)
        except (ValueError, TypeError):
            return None, "llm_response_unparsable"
    else:
        return None, "llm_response_unparsable"
    if not isinstance(candidate, dict):
        return None, "mapping_llm_schema_violation"
    if not candidate:
        return None, "mapping_llm_empty_response"
    try:
        brief = egress.validate_brief(candidate)
    except egress.EgressViolation:
        return None, "mapping_llm_schema_violation"
    if _is_empty_brief(brief):
        return None, "mapping_llm_empty_response"
    return brief, ""


def draft_evidence_brief(
    llm: Any,
    rationale: str,
    perspectives: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Return ``(brief, degradation_reason)``.

    ``degradation_reason`` is an empty string on the normal path.
    """
    if llm is None:
        return empty_brief(), "llm_not_configured"

    prompt = build_mapping_prompt(rationale, perspectives, registry)
    try:
        reply = llm.complete([{"role": "user", "content": prompt}])
    except Exception:
        # Never surface the provider's exception text — it can echo the case
        # body back out. Degrade to an empty brief instead.
        return empty_brief(), "llm_call_failed"

    content = reply.get("content") if isinstance(reply, dict) else reply
    brief, degradation_reason = _coerce_brief(content)
    if brief is None:
        return empty_brief(), degradation_reason
    return brief, ""
