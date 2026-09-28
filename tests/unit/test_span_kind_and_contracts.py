"""Span-kind classification and the closed output/degradation contracts.

The central safety property under test: a verbatim quote proves *provenance*,
not *non-conclusiveness*. A supplied record may itself contain a legal
conclusion, so quotability is restricted by span_kind and the default for
anything unrecognised is "not quotable".
"""

from __future__ import annotations

import pytest

from src.services import contracts, egress, service
from tests.conftest import ADJUDICATIVE_TEXT, QUOTE, make_brief


class TestSpanKindClassification:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("埋設管移設に伴う土工増 ¥3,200,000", contracts.SPAN_KIND_QUANTITY),
            ("増額 3,200,000円", contracts.SPAN_KIND_QUANTITY),
            ("工期 30 日延長", contracts.SPAN_KIND_QUANTITY),
            ("令和8年5月12日 管理者協議を実施", contracts.SPAN_KIND_DATE),
            ("工事番号 R8-道改-014", contracts.SPAN_KIND_IDENTIFIER),
            ("掘削中に設計図書に記載のない埋設管を発見", contracts.SPAN_KIND_OBSERVED_CONDITION),
            ("管理者協議を実施し指示を受領", contracts.SPAN_KIND_PROCEDURAL_RECORD),
        ],
    )
    def test_factual_spans_are_quotable(self, text, expected):
        assert service.classify_span_kind(text) == expected
        assert expected in contracts.QUOTABLE_SPAN_KINDS

    @pytest.mark.parametrize(
        "text",
        [
            "本変更は約款上許容される",
            "本変更は適法と解される",
            "承認して差し支えないと考えられる",
            "実質的変更には当たらない",
            "競争性を損なうものではない",
            "積算は妥当と判断する",
        ],
    )
    def test_adjudicative_spans_are_not_quotable(self, text):
        kind = service.classify_span_kind(text)
        assert kind in contracts.NON_QUOTABLE_SPAN_KINDS, (text, kind)

    def test_conclusive_marker_wins_over_factual_content(self):
        """A sentence that looks factual but concludes must NOT become quotable."""
        kind = service.classify_span_kind("増額 3,200,000円は約款上許容される")
        assert kind == contracts.SPAN_KIND_ADJUDICATIVE

    @pytest.mark.parametrize(
        "text",
        [
            # Conclusions phrased to dodge the adjudicative keyword list.
            "本変更は約款上ゆるされる",
            "変更は契約条項に照らし問題がない",
            "本変更を承認することが適当である",
            "設計変更は正当である",
            "変更内容は契約の範囲内である",
            "変更は認容できる",
            "埋設管の移設は可能である",
            # Shares the word 支障 with genuinely factual text — the evaluative
            # shape must still outrank the factual marker.
            "契約変更に支障はない",
        ],
    )
    def test_evaluative_shapes_are_never_quotable(self, text):
        """Keyword evasion must degrade to unclassified, not to a factual kind."""
        kind = service.classify_span_kind(text)
        assert kind in contracts.NON_QUOTABLE_SPAN_KINDS, (text, kind)

    @pytest.mark.parametrize(
        "text",
        [
            "本変更は約款上、許 容 される",  # spaced
            "本変更は約款上許容さ​れる",  # zero-width space
            "ｱ本変更は約款上許容される",  # halfwidth kana prefix
            "本変更は約款上　許容　される",  # ideographic space
        ],
    )
    def test_normalisation_defeats_separator_injection(self, text):
        assert service.classify_span_kind(text) in contracts.NON_QUOTABLE_SPAN_KINDS

    @pytest.mark.parametrize("text", ["", "   ", "備考欄", None])
    def test_unrecognised_text_fails_closed(self, text):
        assert service.classify_span_kind(text) == contracts.SPAN_KIND_UNCLASSIFIED

    def test_unclassified_is_not_quotable(self):
        assert contracts.SPAN_KIND_UNCLASSIFIED not in contracts.QUOTABLE_SPAN_KINDS

    def test_registry_records_span_kind(self):
        registry = service.build_source_registry(
            [
                {
                    "doc_ref": "D-1",
                    "spans": [
                        {"span_ref": "p1", "text": QUOTE},
                        {"span_ref": "p2", "text": ADJUDICATIVE_TEXT},
                    ],
                },
            ]
        )
        assert service.is_quotable(registry, "S-01-01") is True
        assert service.is_quotable(registry, "S-01-02") is False


class TestBriefContract:
    def test_valid_brief_passes(self):
        assert egress.validate_brief(make_brief())

    @pytest.mark.parametrize(
        "field",
        [
            "legality_verdict",
            "clause_permissibility_verdict",
            "material_change_verdict",
            "competition_impact_verdict",
            "approval_recommendation",
            "estimate_validity_verdict",
        ],
    )
    def test_conclusion_fields_are_rejected(self, field):
        brief = make_brief()
        brief[field] = "適法"
        with pytest.raises(egress.EgressViolation):
            egress.validate_brief(brief)

    def test_free_text_field_inside_an_element_is_rejected(self):
        """Nested allowlisting: a key-only check would let this through."""
        brief = make_brief(
            gaps=[
                {
                    "perspective_id": "P-01",
                    "gap_code": "evidence_absent",
                    "note": "本変更は約款上許容されるため承認可",
                }
            ]
        )
        with pytest.raises(egress.EgressViolation):
            egress.validate_brief(brief)

    def test_unknown_gap_code_is_rejected(self):
        brief = make_brief(gaps=[{"perspective_id": "P-01", "gap_code": "約款上許容される"}])
        with pytest.raises(egress.EgressViolation):
            egress.validate_brief(brief)

    def test_missing_section_is_rejected(self):
        brief = make_brief()
        del brief["unknowns"]
        with pytest.raises(egress.EgressViolation):
            egress.validate_brief(brief)


class TestDegradationContract:
    def test_valid_envelope_passes(self):
        assert egress.validate_degradation(
            {
                "review_reason_code": "version_mismatch",
                "gap_codes": ["evidence_absent"],
                "unknown_codes": [],
            }
        )

    def test_free_text_reason_code_is_rejected(self):
        """Without a closed enum the degradation path is itself a leak channel."""
        with pytest.raises(egress.EgressViolation):
            egress.validate_degradation(
                {
                    "review_reason_code": "約款上許容されるため承認して差し支えない",
                    "gap_codes": [],
                    "unknown_codes": [],
                }
            )

    def test_free_text_gap_code_is_rejected(self):
        with pytest.raises(egress.EgressViolation):
            egress.validate_degradation(
                {
                    "review_reason_code": "version_mismatch",
                    "gap_codes": ["本変更は適法"],
                    "unknown_codes": [],
                }
            )

    def test_extra_field_is_rejected(self):
        with pytest.raises(egress.EgressViolation):
            egress.validate_degradation(
                {
                    "review_reason_code": "version_mismatch",
                    "gap_codes": [],
                    "unknown_codes": [],
                    "recommendation": "承認可",
                }
            )


class TestErrorEnvelope:
    def test_error_carries_only_a_closed_code(self):
        env = egress.build_error("upstream_failure")
        assert env == {"status": "error", "error_code": "upstream_failure"}

    def test_unknown_code_is_coerced(self):
        env = egress.build_error("Traceback (most recent call last): " + QUOTE)
        assert env["error_code"] == "internal_error"
        assert QUOTE not in str(env)


class TestAuditAllowlist:
    def test_raw_content_is_dropped_from_audit_payloads(self):
        cleaned = egress.sanitize_audit_payload(
            {
                "case_ref": "CASE-1",
                "validated_input": {"variation_rationale": QUOTE},
                "draft": QUOTE,
                "error": "Traceback ...",
                "count": 3,
            }
        )
        assert cleaned == {"case_ref": "CASE-1", "count": 3}
        assert QUOTE not in str(cleaned)


class TestRetrievalSafeProjection:
    def test_conclusions_and_free_text_are_not_projected(self):
        projected = service.project_perspective_record(
            {
                "perspective_id": "P-01",
                "perspective_code": "change_reason_basis",
                "source_id": "S-1",
                "prior_decision": "許容と判断",
                "reviewer_comment": "本件は適法と解される",
            }
        )
        assert "prior_decision" not in projected
        assert "reviewer_comment" not in projected
        assert projected["perspective_id"] == "P-01"
