"""GOV-C2-110 — main slot: GraphNode wrapping the inner domain workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.graph.domain_workflow_graph import ContractVariationWorkflowGraph
from src.services import contracts
from src.services.egress import sanitize_audit_payload


class ContractVariationGraphNode(GraphNode):
    """Invoke the inner workflow graph and map its result back explicitly."""

    # S-1: GraphNode is a direct BaseNode subclass, NOT a FunctionNode, so it
    # picks up no trust level implicitly — without this declaration it would
    # inherit the permissive ANONYMOUS default and the S-1 gate would wave
    # through unauthenticated callers.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # Evidence verification fails fast; a partial brief is not a safe result.
    error_strategy: ClassVar[str] = "propagate"
    # No in-graph HITL in this template.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: dict | None = None, kb: Any = None, llm: Any = None) -> None:
        super().__init__()
        # Built and compiled once: rebuilding per call would recompile the
        # graph on every request for no benefit. What is held is immutable
        # graph identity, not mutable per-request business data.
        self._subgraph = ContractVariationWorkflowGraph(config=config or {}, kb=kb, llm=llm)
        self._subgraph.compile()

    def get_subgraph(self):
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """Pass an explicit minimal map to the inner graph."""
        return json.dumps(
            {
                "mode": state.get("mode", contracts.MODE_INVOKE),
                "case_ref": state.get("case_ref", ""),
                "validated_input": state.get("validated_input", {}),
            },
            ensure_ascii=False,
        )

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner results back per mode — never a raw pass-through.

        The inner graph overrides ``get_output()`` and returns its fields at the
        top level of ``sub_result``; only graphs that keep the default
        ``AgentBaseGraph.get_output()`` nest them under ``"output"``. Read both
        so this node does not depend on which shape the subgraph uses.
        """
        if not isinstance(sub_result, dict):
            sub_result = {}
        nested = sub_result.get("output")
        output = nested if isinstance(nested, dict) else sub_result
        status = sub_result.get("status") or output.get("status") or AgentStatus.SUCCESS.value
        if status == AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value}

        mode = state.get("mode") or contracts.MODE_INVOKE
        if mode == contracts.MODE_INGEST:
            return {"ingest_result": output.get("ingest_result") or {}, "status": status}
        if mode == contracts.MODE_FEEDBACK:
            return {"feedback_result": output.get("feedback_result") or {}, "status": status}

        requires_review = bool(output.get("requires_human_review"))
        merged: dict[str, Any] = {
            "gaps": output.get("gaps") or [],
            "requires_human_review": requires_review,
            "degradation": output.get("degradation") or {},
            "version_check": output.get("version_check") or {},
            "status": status,
        }
        # When the brief is withheld it must not reach the parent state at all:
        # mapping it here would let a later stage surface an unverified draft.
        if requires_review:
            merged["evidence_brief"] = {}
            merged["evidence_map"] = {}
        else:
            merged["evidence_brief"] = output.get("evidence_brief") or {}
            merged["evidence_map"] = output.get("evidence_map") or {}
        return merged

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("cv_subgraph_passthrough_error", {}, state)
            return {"status": AgentStatus.ERROR.value}

        # A GraphNode may be invoked by a framework conformance probe with an
        # incomplete state. Reject that with a normal fail-closed delta instead
        # of letting context reconstruction raise out of execute().
        missing = [
            field for field in ("session_id", "thread_id", "trace_id", "correlation_id")
            if not state.get(field)
        ]
        if missing:
            emit_trace_event(
                "cv_subgraph_rejected",
                sanitize_audit_payload({"status": "incomplete_context"}),
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["subgraph invocation context is incomplete"],
            }

        emit_trace_event(
            "cv_subgraph_invoked",
            sanitize_audit_payload({"mode": str(state.get("mode", ""))}),
            state,
        )
        return super().execute(state)
