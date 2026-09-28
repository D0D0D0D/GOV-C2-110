"""End-to-end behaviour through the real graph and the real HTTP adapter.

These run against the same config shape as production, so a gate that only
works on hand-built state is not enough to pass here.
"""

from __future__ import annotations

import json

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import ContractVariationEvidenceBriefAgent
from tests.conftest import ADJUDICATIVE_TEXT, QUOTE, CapturingKB, FakeLLM, make_brief


def run(config, payload, llm=None, mode="invoke", kb=None):
    agent = ContractVariationEvidenceBriefAgent(config=config, llm=llm, kb=kb)
    agent.compile()
    ctx = InvocationContext(
        correlation_id="c1",
        session_id="s1",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="officer-1",
    )
    return agent.invoke(
        json.dumps(payload, ensure_ascii=False),
        session_id="s1",
        ctx=ctx,
        input_context={"mode": mode},
    )


class TestInvokeFlow:
    def test_happy_path_returns_a_cited_brief(self, agent_config, case_payload):
        out = run(agent_config, case_payload, FakeLLM(make_brief()))["output"]
        assert out["kind"] == "brief"
        assert out["evidence_brief"]["stated_facts"][0]["verbatim_quote"] == QUOTE
        assert out["evidence_map"]["F-01"]["source_id"] == "S-01-01"

    def test_version_mismatch_withholds_the_brief(self, agent_config, case_payload):
        case_payload["versions"] = {"contract_version": "v1"}
        out = run(agent_config, case_payload, FakeLLM(make_brief()))["output"]
        assert out["kind"] == "degradation"
        assert out["degradation"]["review_reason_code"] == "version_mismatch"
        assert "evidence_brief" not in out

    def test_fabricated_citation_withholds_the_brief(self, agent_config, case_payload):
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
        out = run(agent_config, case_payload, FakeLLM(brief))["output"]
        assert out["degradation"]["review_reason_code"] == "citation_missing"

    def test_adjudicative_source_cannot_be_quoted_out(self, agent_config, case_payload):
        """The evidence itself contains a legal conclusion — refuse to quote it."""
        case_payload["evidence"] = [
            {
                "doc_ref": "D-1",
                "doc_type": "past_decision",
                "spans": [{"span_ref": "p1-L8", "text": ADJUDICATIVE_TEXT}],
            }
        ]
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
        result = run(agent_config, case_payload, FakeLLM(brief))
        out = result["output"]
        assert out["degradation"]["review_reason_code"] == "citation_not_quotable"
        assert ADJUDICATIVE_TEXT not in json.dumps(result, ensure_ascii=False)

    def test_injected_conclusion_field_never_reaches_the_caller(self, agent_config, case_payload):
        brief = make_brief()
        brief["approval_recommendation"] = "承認して差し支えない"
        result = run(agent_config, case_payload, FakeLLM(brief))
        blob = json.dumps(result, ensure_ascii=False)
        assert "approval_recommendation" not in blob
        assert "承認して差し支えない" not in blob

    def test_missing_llm_withholds_rather_than_inventing(self, agent_config, case_payload):
        out = run(agent_config, case_payload, None)["output"]
        assert out["kind"] == "degradation"

    def test_empty_llm_response_withholds_with_mapping_reason(self, agent_config, case_payload):
        out = run(agent_config, case_payload, FakeLLM())["output"]
        assert out["kind"] == "degradation"
        assert out["degradation"]["review_reason_code"] == "mapping_llm_empty_response"


class TestOtherModes:
    def test_ingest_without_a_kb_refuses(self, agent_config):
        """Reporting written_ids with no store would claim a write that never happened."""
        result = run(agent_config, {"records": []}, mode="ingest")
        assert result["status"] == "error"

    def test_ingest(self, agent_config):
        payload = {
            "records": [
                {
                    "perspective_id": "P-09",
                    "perspective_code": "estimate_basis",
                    "source_id": "S-1",
                    "required_fact_codes": [],
                    "question_template_ids": [],
                    "provenance": {},
                    "approved_by_role": "課長",
                    "approved_at": "2026-04-01",
                    "record_version": 1,
                }
            ]
        }
        out = run(agent_config, payload, mode="ingest", kb=CapturingKB())["output"]
        assert out["kind"] == "ingest"
        assert out["summary"]["written_ids"] == ["P-09"]

    def test_feedback(self, agent_config):
        payload = {
            "case_ref": "CASE-1",
            "confirmed_perspectives": [
                {
                    "perspective_id": "P-10",
                    "perspective_code": "estimate_basis",
                    "source_id": "S-1",
                    "required_fact_codes": [],
                    "question_template_ids": [],
                    "provenance": {},
                    "approved_at": "2026-04-01",
                    "record_version": 1,
                }
            ],
        }
        out = run(agent_config, payload, mode="feedback", kb=CapturingKB())["output"]
        assert out["kind"] == "feedback"
        assert out["summary"]["accepted_ids"] == ["P-10"]


class TestHttpAdapter:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-token-for-testing")
        from fastapi.testclient import TestClient

        import src.api.server as server_module

        return TestClient(server_module.app)

    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_resume_endpoint_is_not_registered(self, client):
        """No in-graph HITL means there is no suspended session to resume."""
        assert client.post("/resume", json={}).status_code == 404

    @pytest.mark.parametrize("endpoint", ["/invoke", "/ingest", "/feedback"])
    def test_all_write_routes_require_the_entry_token(self, client, endpoint):
        assert client.post(endpoint, json={"input": "{}"}).status_code == 401
        assert client.post(endpoint, json={"input": "{}"}, headers={"authorization": "Bearer wrong"}).status_code == 401

    def test_authenticated_call_succeeds(self, client, case_payload):
        response = client.post(
            "/invoke",
            json={"input": json.dumps(case_payload, ensure_ascii=False)},
            headers={"authorization": "Bearer mock-token-for-testing"},
        )
        assert response.status_code == 200

    def test_failures_never_leak_exception_text(self, client):
        response = client.post(
            "/invoke",
            json={"input": "\x00 not json"},
            headers={"authorization": "Bearer mock-token-for-testing"},
        )
        blob = json.dumps(response.json(), ensure_ascii=False)
        for leak in ("Traceback", 'File "', "line ", "Exception"):
            assert leak not in blob
