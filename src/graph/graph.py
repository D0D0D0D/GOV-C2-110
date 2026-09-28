"""GOV-C2-110 — outer agent graph.

Municipal Procurement Contract-Variation Evidence Brief Agent.

Fixed five-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). ``initialize`` and ``finalize`` are injected by
``super().register_nodes()``; this template fills the three domain slots.

The class name here must stay in sync with ``config/agent.yaml``
(``module: src.graph`` / ``class: ContractVariationEvidenceBriefAgent``) AND be
re-exported from ``src/graph/__init__.py`` — AgentRegistry cannot resolve the
manifest entry unless both are true.
"""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import ContractVariationGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class ContractVariationEvidenceBriefAgent(AgentBaseGraph):
    """Draft a cited decision brief for a municipal contract variation.

    The agent maps a free-text variation rationale onto human pre-approved
    fact-check perspectives and reports which evidence supports which
    perspective. It never decides whether the variation is permissible, lawful,
    material, or should be approved — those fields do not exist in its output
    schema, and the final decision stays with the human approving officer.
    """

    def __init__(self, config: dict | None = None, kb: Any = None, llm: Any = None) -> None:
        super().__init__(config=config)
        self._kb = kb
        self._llm = llm

    @property
    def name(self) -> str:
        return "contract_variation_evidence_brief"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # REQUIRED first — injects the InitializeNode + FinalizeNode slots.
        super().register_nodes()

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ContractVariationGraphNode(
            config=self.config, kb=self._kb, llm=self._llm
        )
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() / route() are intentionally NOT overridden: the framework
    # already wires START -> initialize -> pre_process -> main -> {route} ->
    # post_process -> finalize -> END and routes on state["status"].


# Backwards-compatible alias for the scaffold's generic name.
Graph = ContractVariationEvidenceBriefAgent

__all__ = ["ContractVariationEvidenceBriefAgent", "Graph"]
