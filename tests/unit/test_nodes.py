"""Node-level behaviour: S-1 trust gates, S-2 input gate, S-3 output gate."""

from __future__ import annotations

import json

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import ContractVariationGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.workflow_nodes import (
    DeterministicCheckNode,
    EvidenceVerifyNode,
    FeedbackNode,
    IngestNode,
    MappingNode,
    RecallNode,
    UnpackInputNode,
)
from src.services import contracts
from tests.conftest import ADJUDICATIVE_TEXT, QUOTE, CapturingKB, make_brief

ALL_NODE_TYPES = [
    PreProcessNode,
    PostProcessNode,
    UnpackInputNode,
    DeterministicCheckNode,
    RecallNode,
    MappingNode,
    EvidenceVerifyNode,
    IngestNode,
    FeedbackNode,
]


def base_state(**over) -> dict:
    state = {
        "caller_id": "officer-1",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "session_id": "s",
        "correlation_id": "c",
        "trace_id": "t",
        "thread_id": "th",
        "status": AgentStatus.PENDING.value,
        "node_history": [],
        "error_log": [],
    }
    state.update(over)
    return state


class TestTrustDeclarations:
    """TC-08 — every node declares VERIFIED_EXTERNAL explicitly."""

    @pytest.mark.parametrize("node_cls", ALL_NODE_TYPES)
    def test_node_requires_verified_external(self, node_cls):
        assert node_cls.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_graph_node_declares_trust_explicitly(self):
        """GraphNode is not a FunctionNode: without this it inherits ANONYMOUS."""
        assert ContractVariationGraphNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    @pytest.mark.parametrize("node_cls", [PreProcessNode, PostProcessNode, UnpackInputNode])
    def test_anonymous_caller_is_refused_before_execute(self, node_cls, monkeypatch):
        """The S-1 gate must refuse *before* execute() runs."""
        node = node_cls()
        called = []
        monkeypatch.setattr(
            node_cls,
            "execute",
            lambda self, state: called.append(True) or {"status": AgentStatus.SUCCESS.value},
        )
        result = node(base_state(caller_trust_level=TrustLevel.ANONYMOUS.value))
        assert called == [], "execute() ran despite insufficient trust"
        assert result.get("status") == AgentStatus.ERROR.value


class TestPreProcessS2Gate:
    def test_my_number_is_rejected(self):
        node = PreProcessNode()
        with pytest.raises(ValueError, match="My Number"):
            node._extra_security_gate_input(base_state(user_input="個人番号 1234 5678 9012"))

    @pytest.mark.parametrize("marker", ["極秘", "取扱注意", "部外秘"])
    def test_classification_markers_are_rejected(self, marker):
        node = PreProcessNode()
        with pytest.raises(ValueError, match="classification"):
            node._extra_security_gate_input(base_state(user_input=f"{marker} 変更理由"))

    @pytest.mark.parametrize("marker", ["病歴", "犯罪歴", "信条"])
    def test_appi_sensitive_categories_are_rejected(self, marker):
        node = PreProcessNode()
        with pytest.raises(ValueError, match="APPI"):
            node._extra_security_gate_input(base_state(user_input=f"申請者の{marker}を記載"))

    @pytest.mark.parametrize(
        "payload,label",
        [
            (b"%PDF-1.7 ...", "pdf"),
            (b"PK\x03\x04xlsx", "zip_or_xlsx"),
        ],
    )
    def test_binary_formats_are_rejected(self, payload, label):
        node = PreProcessNode()
        with pytest.raises(ValueError, match="not accepted"):
            node._extra_security_gate_input(base_state(user_input=payload))

    def test_unknown_mode_is_rejected_at_the_gate(self):
        node = PreProcessNode()
        raw = json.dumps({"mode": "delete_everything"})
        with pytest.raises(ValueError, match="not an allowed mode"):
            node._extra_security_gate_input(base_state(user_input=raw))

    def test_gate_does_not_depend_on_execute_written_state(self):
        """The gate runs before execute(); reading its output would fail open."""
        node = PreProcessNode()
        # No mode/case_ref in state at all — the gate must still function.
        assert node._extra_security_gate_input(base_state(user_input="ordinary text"))

    def test_clean_input_is_accepted(self):
        node = PreProcessNode()
        assert node._extra_security_gate_input(base_state(user_input=QUOTE))


class TestPostProcessS3Gate:
    def test_valid_brief_passes(self):
        node = PostProcessNode()
        assert node._extra_security_gate_output(
            {
                "result": {"kind": "brief", "evidence_brief": make_brief(), "evidence_map": {}},
            }
        )

    def test_conclusion_field_is_blocked(self):
        node = PostProcessNode()
        brief = make_brief()
        brief["approval_recommendation"] = "承認可"
        with pytest.raises(Exception):
            node._extra_security_gate_output(
                {
                    "result": {"kind": "brief", "evidence_brief": brief},
                }
            )

    def test_nested_pii_is_detected(self):
        """The framework default only scans top-level strings."""
        node = PostProcessNode()
        brief = make_brief(
            stated_facts=[
                {
                    "fact_id": "F-01",
                    "perspective_id": "P-01",
                    "source_id": "S-01-01",
                    "span_ref": "p1",
                    "verbatim_quote": "連絡先 090-1234-5678",
                }
            ]
        )
        with pytest.raises(Exception, match="PII"):
            node._extra_security_gate_output(
                {
                    "result": {"kind": "brief", "evidence_brief": brief},
                }
            )

    def test_unknown_result_kind_is_blocked(self):
        node = PostProcessNode()
        with pytest.raises(Exception):
            node._extra_security_gate_output({"result": {"kind": "freeform", "text": "承認可"}})

    def test_error_status_passes_through_untouched(self):
        node = PostProcessNode()
        out = node.execute(base_state(status=AgentStatus.ERROR.value))
        assert out == {"status": AgentStatus.ERROR.value}

    def test_degradation_is_evaluated_before_the_brief_branch(self):
        """Order matters: otherwise a withheld brief becomes a missing-field error."""
        node = PostProcessNode()
        out = node.execute(
            base_state(
                mode="invoke",
                requires_human_review=True,
                degradation={"review_reason_code": "version_mismatch", "gap_codes": [], "unknown_codes": []},
                evidence_brief={},
            )
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["result"]["kind"] == "degradation"
        assert "evidence_brief" not in out["result"]


class TestStage1:
    def test_version_mismatch_stops_before_semantic_work(self, agent_config, case_payload):
        node = DeterministicCheckNode(config=agent_config)
        payload = dict(case_payload)
        payload["versions"] = {"contract_version": "v1"}
        out = node.execute(base_state(validated_input=payload, case_ref="CASE-1"))
        assert out["requires_human_review"] is True
        assert out["degradation"]["review_reason_code"] == "version_mismatch"

    def test_matching_versions_proceed(self, agent_config, case_payload):
        node = DeterministicCheckNode(config=agent_config)
        out = node.execute(base_state(validated_input=case_payload, case_ref="CASE-1"))
        assert not out.get("requires_human_review")
        assert out["version_check"]["ok"] is True
        assert "S-01-01" in out["source_registry"]


class TestStage4:
    def _state(self, brief, registry, **over):
        state = base_state(
            evidence_brief=brief,
            source_registry=registry,
            recalled_perspectives=[{"perspective_id": "P-01"}],
            # Stage 1 outputs: the release path requires these to be clean.
            authority_match={"matched": True, "reason": "compared_against_matrix"},
            docs_present={"present": ["kyogi"], "missing": []},
        )
        state.update(over)
        return state

    def _registry(self, text=QUOTE):
        from src.services import service

        return service.build_source_registry([{"doc_ref": "D-1", "spans": [{"span_ref": "p1-L8", "text": text}]}])

    def test_valid_citations_release_the_brief(self):
        out = EvidenceVerifyNode().execute(self._state(make_brief(), self._registry()))
        assert out["requires_human_review"] is False
        assert out["evidence_brief"]["stated_facts"]

    def test_fabricated_source_is_refused(self):
        brief = make_brief(
            stated_facts=[
                {
                    "fact_id": "F-01",
                    "perspective_id": "P-01",
                    "source_id": "S-99-99",
                    "span_ref": "x",
                    "verbatim_quote": QUOTE,
                }
            ]
        )
        out = EvidenceVerifyNode().execute(self._state(brief, self._registry()))
        assert out["degradation"]["review_reason_code"] == "citation_missing"
        assert out["evidence_brief"] == {}

    def test_altered_quote_is_refused(self):
        brief = make_brief(
            stated_facts=[
                {
                    "fact_id": "F-01",
                    "perspective_id": "P-01",
                    "source_id": "S-01-01",
                    "span_ref": "p1-L8",
                    "verbatim_quote": QUOTE + "（許容される）",
                }
            ]
        )
        out = EvidenceVerifyNode().execute(self._state(brief, self._registry()))
        assert out["degradation"]["review_reason_code"] == "citation_span_mismatch"

    def test_authority_mismatch_withholds_the_brief(self):
        """Stage 1 computes authority_match; releasing while it fails would make it decorative."""
        state = self._state(
            make_brief(), self._registry(), authority_match={"matched": False, "reason": "authority_undeterminable"}
        )
        out = EvidenceVerifyNode().execute(state)
        assert out["degradation"]["review_reason_code"] == "authority_unmatched"
        assert out["evidence_brief"] == {}

    def test_missing_required_document_withholds_the_brief(self):
        state = self._state(make_brief(), self._registry(), docs_present={"present": [], "missing": ["kyogi"]})
        out = EvidenceVerifyNode().execute(state)
        assert out["degradation"]["review_reason_code"] == "required_evidence_absent"

    def test_low_coverage_confidence_withholds_the_brief(self):
        """Deterministic coverage ratio, not an LLM self-score."""
        state = self._state(make_brief(), self._registry())
        state["recalled_perspectives"] = [
            {"perspective_id": f"P-{i:02d}"} for i in range(1, 5)
        ]  # brief covers 1 of 4 = 0.25 < 0.75
        node = EvidenceVerifyNode(config={"evidence_confidence_threshold": 0.75})
        out = node.execute(state)
        assert out["degradation"]["review_reason_code"] == "confidence_below_threshold"

    def test_adjudicative_span_cannot_be_quoted(self):
        """Provenance-valid but conclusion-bearing: must still be refused."""
        registry = self._registry(ADJUDICATIVE_TEXT)
        brief = make_brief(
            stated_facts=[
                {
                    "fact_id": "F-01",
                    "perspective_id": "P-01",
                    "source_id": "S-01-01",
                    "span_ref": "p1-L8",
                    "verbatim_quote": ADJUDICATIVE_TEXT,
                }
            ]
        )
        out = EvidenceVerifyNode().execute(self._state(brief, registry))
        assert out["degradation"]["review_reason_code"] == "citation_not_quotable"
        assert out["evidence_brief"] == {}
        assert ADJUDICATIVE_TEXT not in json.dumps(out, ensure_ascii=False)


class TestMappingNode:
    def test_missing_llm_degrades_without_raising(self):
        out = MappingNode(llm=None).execute(base_state(validated_input={}))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["evidence_brief"] == {}
        assert out["requires_human_review"] is True
        assert out["degradation"]["review_reason_code"] == "llm_not_configured"

    def test_provider_exception_text_is_not_propagated(self):
        from tests.conftest import FakeLLM

        out = MappingNode(llm=FakeLLM(raise_on_call=True)).execute(
            base_state(validated_input={"variation_rationale": "x"})
        )
        assert QUOTE not in json.dumps(out, ensure_ascii=False)
        assert out["degradation"]["review_reason_code"] == "llm_call_failed"

    def test_non_quotable_spans_are_withheld_from_the_prompt(self):
        from src.services.llm_client import build_mapping_prompt
        from src.services import service

        registry = service.build_source_registry(
            [
                {
                    "doc_ref": "D",
                    "spans": [
                        {"span_ref": "p1", "text": QUOTE},
                        {"span_ref": "p2", "text": ADJUDICATIVE_TEXT},
                    ],
                }
            ]
        )
        prompt = build_mapping_prompt("rationale", [], registry)
        assert QUOTE in prompt
        assert ADJUDICATIVE_TEXT not in prompt

    def test_prompt_renders_the_closed_brief_schema(self):
        from tests.conftest import FakeLLM

        llm = FakeLLM(make_brief())
        MappingNode(llm=llm).execute(
            base_state(
                validated_input={"variation_rationale": "rationale"},
                recalled_perspectives=[
                    {
                        "perspective_id": "P-01",
                        "perspective_code": "change_reason_basis",
                        "required_fact_codes": ["reason"],
                        "question_template_ids": ["Q-07"],
                    }
                ],
                source_registry={
                    "S-01-01": {
                        "span_kind": "observed_condition",
                        "span_ref": "p1-L8",
                        "text": QUOTE,
                    }
                },
            )
        )
        prompt = llm.calls[0][0]["content"]
        assert "additionalProperties" in prompt
        for field in contracts.BRIEF_FIELDS:
            assert field in prompt
        for element_fields in contracts.BRIEF_ELEMENT_FIELDS.values():
            for field in element_fields:
                assert field in prompt

    def test_empty_llm_response_sets_degradation_reason(self):
        from tests.conftest import FakeLLM

        out = MappingNode(llm=FakeLLM()).execute(base_state(validated_input={}))
        assert out["requires_human_review"] is True
        assert out["degradation"]["review_reason_code"] == "mapping_llm_empty_response"

    def test_schema_violation_sets_degradation_reason(self):
        from tests.conftest import FakeLLM

        out = MappingNode(llm=FakeLLM({"unexpected": []})).execute(base_state(validated_input={}))
        assert out["requires_human_review"] is True
        assert out["degradation"]["review_reason_code"] == "mapping_llm_schema_violation"

    def test_schema_valid_llm_values_are_reflected(self):
        from tests.conftest import FakeLLM

        brief = make_brief(gaps=[{"perspective_id": "P-01", "gap_code": "evidence_absent"}])
        out = MappingNode(llm=FakeLLM(brief)).execute(base_state(validated_input={}))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert not out.get("requires_human_review")
        assert out["evidence_brief"]["stated_facts"][0]["verbatim_quote"] == QUOTE
        assert out["evidence_brief"]["gaps"][0]["gap_code"] == "evidence_absent"


class TestFeedbackAuthority:
    def test_approver_comes_from_context_not_the_request_body(self):
        kb = CapturingKB()
        state = base_state(
            validated_input={
                "confirmed_perspectives": [
                    {
                        "perspective_id": "P-10",
                        "perspective_code": "estimate_basis",
                        "source_id": "S-1",
                        "required_fact_codes": [],
                        "question_template_ids": [],
                        "provenance": {},
                        "approved_by_role": "self-claimed-admin",
                        "approved_at": "x",
                        "record_version": 1,
                    }
                ]
            }
        )
        FeedbackNode(kb=kb).execute(state)
        assert kb.rows[0]["approved_by_role"] == "officer-1"

    def test_unresolvable_approver_is_refused(self):
        state = base_state(caller_id="", validated_input={"confirmed_perspectives": []})
        out = FeedbackNode().execute(state)
        assert out["status"] == AgentStatus.ERROR.value

    def test_free_text_is_not_written_to_the_kb(self):
        kb = CapturingKB()
        state = base_state(
            validated_input={
                "confirmed_perspectives": [
                    {
                        "perspective_id": "P-10",
                        "perspective_code": "estimate_basis",
                        "source_id": "S-1",
                        "required_fact_codes": [],
                        "question_template_ids": [],
                        "provenance": {},
                        "approved_at": "x",
                        "record_version": 1,
                        "reviewer_comment": "本件は約款上許容されると判断した",
                    }
                ]
            }
        )
        FeedbackNode(kb=kb).execute(state)
        assert "reviewer_comment" not in kb.rows[0]


class TestIngestNode:
    def test_unknown_perspective_code_is_rejected(self):
        kb = CapturingKB()
        out = IngestNode(kb=kb).execute(
            base_state(
                validated_input={
                    "records": [
                        {"perspective_id": "P-1", "perspective_code": "made_up_code"},
                    ]
                }
            )
        )
        assert out["ingest_result"]["written_ids"] == []
        assert out["ingest_result"]["rejected"]


class TestUpstreamErrorPassthrough:
    @pytest.mark.parametrize(
        "node",
        [
            UnpackInputNode(),
            DeterministicCheckNode(),
            RecallNode(),
            MappingNode(),
            EvidenceVerifyNode(),
            IngestNode(),
            FeedbackNode(),
        ],
    )
    def test_error_status_short_circuits(self, node):
        out = node.execute(base_state(status=AgentStatus.ERROR.value))
        assert out == {"status": AgentStatus.ERROR.value}
