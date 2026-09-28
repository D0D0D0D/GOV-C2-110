"""GOV-C2-110 — inner domain workflow graph.

``AgentBaseGraph`` enforces the fixed five-node backbone, which cannot express
the mode branch plus the Stage 1-4 chain. This graph therefore inherits
``BaseGraph`` directly and implements all seven abstract members.

Design: docs/02_design.md "内側 domain workflow graph".
"""

from __future__ import annotations

from typing import Any

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from langgraph.graph import END, START

from src.nodes.workflow_nodes import (
    DeterministicCheckNode,
    EvidenceVerifyNode,
    FeedbackNode,
    IngestNode,
    MappingNode,
    RecallNode,
    UnpackInputNode,
)
from src.schemas.state import State
from src.services import contracts


class ContractVariationWorkflowGraph(BaseGraph):
    """Mode router followed by ingest, feedback, or the staged invoke pipeline."""

    def __init__(
        self,
        config: dict | None = None,
        kb: Any = None,
        llm: Any = None,
    ) -> None:
        super().__init__(config=config)
        self._kb = kb
        self._llm = llm

    @property
    def name(self) -> str:
        return "contract_variation_workflow"

    @property
    def state_schema(self):
        return State

    def _validate_config(self) -> None:
        top_k = self.config.get("top_k", 8)
        if not isinstance(top_k, int) or top_k <= 0:
            raise ConfigError("top_k must be a positive integer")
        threshold = self.config.get("evidence_confidence_threshold", 0.75)
        if not isinstance(threshold, (int, float)) or not 0 <= float(threshold) <= 1:
            raise ConfigError("evidence_confidence_threshold must be between 0 and 1")

    def register_nodes(self) -> None:
        self._nodes["unpack"] = UnpackInputNode()
        self._nodes["stage1_check"] = DeterministicCheckNode(config=self.config)
        self._nodes["stage2_recall"] = RecallNode(config=self.config, kb=self._kb)
        self._nodes["stage3_map"] = MappingNode(config=self.config, llm=self._llm)
        self._nodes["stage4_verify"] = EvidenceVerifyNode(config=self.config)
        self._nodes["ingest"] = IngestNode(config=self.config, kb=self._kb)
        self._nodes["feedback"] = FeedbackNode(config=self.config, kb=self._kb)

    def add_edges(self) -> None:
        # Decode the parent's serialised map first, then branch on mode.
        self._sg.add_edge(START, "unpack")
        self._sg.add_conditional_edges(
            "unpack",
            self.route,
            {
                "ingest": "ingest",
                "feedback": "feedback",
                "invoke": "stage1_check",
            },
        )
        self._sg.add_edge("stage1_check", "stage2_recall")
        self._sg.add_edge("stage2_recall", "stage3_map")
        self._sg.add_edge("stage3_map", "stage4_verify")
        self._sg.add_edge("stage4_verify", END)
        self._sg.add_edge("ingest", END)
        self._sg.add_edge("feedback", END)

    def route(self, state: State) -> str:
        """Route on mode.

        The parameter is annotated with this graph's own schema on purpose:
        annotating it with ``AgentState`` would drop the custom fields and
        misroute.
        """
        mode = state.get("mode") or contracts.MODE_INVOKE
        if mode == contracts.MODE_INGEST:
            return "ingest"
        if mode == contracts.MODE_FEEDBACK:
            return "feedback"
        return "invoke"

    def get_output(self, state: State) -> dict:
        return {
            "evidence_brief": state.get("evidence_brief") or {},
            "evidence_map": state.get("evidence_map") or {},
            "gaps": state.get("gaps") or [],
            "requires_human_review": bool(state.get("requires_human_review")),
            "degradation": state.get("degradation") or {},
            "version_check": state.get("version_check") or {},
            "ingest_result": state.get("ingest_result") or {},
            "feedback_result": state.get("feedback_result") or {},
            "status": state.get("status") or AgentStatus.SUCCESS.value,
        }
