"""GOV-C2-110 — graph package.

Re-export the agent class so AgentRegistry can resolve the manifest entry
``module: src.graph`` / ``class: ContractVariationEvidenceBriefAgent``.
Renaming the class without this re-export leaves the manifest unresolvable.
"""

# Imported lazily via __getattr__: src.nodes.main_node imports
# src.graph.domain_workflow_graph, so eagerly importing graph.py here would
# create a circular import (graph -> main_node -> graph package -> graph).

__all__ = [
    "ContractVariationEvidenceBriefAgent",
    "ContractVariationWorkflowGraph",
    "Graph",
]


def __getattr__(name):
    if name in ("ContractVariationEvidenceBriefAgent", "Graph"):
        from src.graph.graph import ContractVariationEvidenceBriefAgent

        return ContractVariationEvidenceBriefAgent
    if name == "ContractVariationWorkflowGraph":
        from src.graph.domain_workflow_graph import ContractVariationWorkflowGraph

        return ContractVariationWorkflowGraph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
