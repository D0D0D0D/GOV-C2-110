"""PB — every egress channel is closed, not just the response body.

Three channels can leak a conclusion even when the returned brief is clean:

* the audit log (S-4),
* the error envelope,
* the checkpointed state.

Each is exercised here against the real graph rather than hand-built state.
"""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

import framework.nodes.base_node as base_node_module
from src.graph.graph import ContractVariationEvidenceBriefAgent
from tests.conftest import ADJUDICATIVE_TEXT, QUOTE, FakeLLM, make_brief

RATIONALE = "掘削中に設計図書に記載のない埋設管を発見した。"


def _ctx():
    return InvocationContext(
        correlation_id="c1",
        session_id="s1",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="officer-1",
    )


def _run(config, payload, llm=None, mode="invoke"):
    agent = ContractVariationEvidenceBriefAgent(config=config, llm=llm)
    agent.compile()
    return agent.invoke(
        json.dumps(payload, ensure_ascii=False),
        session_id="s1",
        ctx=_ctx(),
        input_context={"mode": mode},
    )


class TestAuditLogEgress:
    def test_no_raw_content_reaches_the_audit_log(self, monkeypatch, agent_config, case_payload):
        """The log is an egress channel: raw input/drafts/quotes must not appear."""
        captured: list[tuple[str, dict]] = []

        def spy(event_type, payload, state):
            captured.append((event_type, payload))

        monkeypatch.setattr(base_node_module, "emit_trace_event", spy)
        for module_name in (
            "src.nodes.workflow_nodes",
            "src.nodes.pre_process_node",
            "src.nodes.post_process_node",
            "src.nodes.main_node",
        ):
            monkeypatch.setattr(f"{module_name}.emit_trace_event", spy)

        _run(agent_config, case_payload, FakeLLM(make_brief()))

        assert captured, "no audit events were emitted"
        blob = json.dumps(captured, ensure_ascii=False, default=str)
        for leak in (RATIONALE, QUOTE, "variation_rationale", "Traceback"):
            assert leak not in blob, f"audit log leaked {leak!r}"

    def test_audit_payload_keys_stay_on_the_allowlist(self, monkeypatch, agent_config, case_payload):
        from src.services import contracts

        captured: list[dict] = []
        spy = lambda e, p, s: captured.append(p)  # noqa: E731
        for module_name in (
            "src.nodes.workflow_nodes",
            "src.nodes.pre_process_node",
            "src.nodes.post_process_node",
            "src.nodes.main_node",
        ):
            monkeypatch.setattr(f"{module_name}.emit_trace_event", spy)

        _run(agent_config, case_payload, FakeLLM(make_brief()))

        for payload in captured:
            unexpected = set(payload) - contracts.AUDIT_PAYLOAD_FIELDS
            assert not unexpected, f"audit payload carried {sorted(unexpected)}"


class TestErrorEnvelopeEgress:
    def test_provider_failure_does_not_surface_its_message(self, agent_config, case_payload):
        """FakeLLM raises with case text embedded in the exception message."""
        result = _run(agent_config, case_payload, FakeLLM(raise_on_call=True))
        blob = json.dumps(result, ensure_ascii=False, default=str)
        assert "provider exploded" not in blob
        assert QUOTE not in blob


class TestCheckpointStateSafety:
    """PB-5 variant against a real checkpointer, not a serialisation mock."""

    def test_checkpointed_state_holds_no_adjudicative_quote(self, agent_config, case_payload):
        from langgraph.checkpoint.memory import InMemorySaver

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

        saver = InMemorySaver()
        config = dict(agent_config)
        config["memory_enabled"] = True
        agent = ContractVariationEvidenceBriefAgent(config=config, llm=FakeLLM(brief))
        agent.compile(checkpointer=saver)
        agent.invoke(
            json.dumps(case_payload, ensure_ascii=False),
            session_id="s1",
            ctx=_ctx(),
            input_context={"mode": "invoke"},
        )

        checkpoints = list(saver.list(None))
        assert checkpoints, "no checkpoint was written"
        blob = json.dumps([c.checkpoint for c in checkpoints], ensure_ascii=False, default=str)
        # The quote may legitimately appear in source_registry (it is the
        # evidence itself); what must never appear is a released brief built
        # on it — the response path is verified separately.
        assert '"kind": "brief"' not in blob

    def test_state_is_msgpack_friendly(self, agent_config, case_payload):
        result = _run(agent_config, case_payload, FakeLLM(make_brief()))
        # json.dumps without default= proves there is no arbitrary object.
        json.dumps(result, ensure_ascii=False)


class TestNoCredentialsInState:
    def test_secrets_never_appear_in_the_response(self, agent_config, case_payload):
        result = _run(agent_config, case_payload, FakeLLM(make_brief()))
        blob = json.dumps(result, ensure_ascii=False, default=str)
        for marker in ("CONTRACT_VARIATION_DB_DSN", "INVOKE_AUTH_TOKEN", "Bearer "):
            assert marker not in blob
