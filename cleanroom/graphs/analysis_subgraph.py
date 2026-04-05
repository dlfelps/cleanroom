"""
cleanroom.graphs.analysis_subgraph
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The LangGraph sub-graph for the analysis coordinator (spec §8.7).

The analysis coordinator is itself a sub-graph, allowing independent scaling
and versioning of each analysis capability.  The sub-graph runs entirely within
the quarantine zone.

Graph structure (spec §8.7):
  START
    └─► api_extraction                (enumerate public symbols)
          ├─► behavior_probing        (depends on api_surface)
          └─► data_model              (depends on api_surface, runs in parallel with behavior_probing)
                    behavior_probing ─► edge_case          (depends on test_cases)
                    data_model       ─► property           (depends on type_contracts)
                              edge_case + property ─► assembler   (merge all outputs)
  END

The parallel edges (api_extraction → behavior_probing and api_extraction → data_model)
are expressed in LangGraph as two separate ``add_edge`` calls from the same source node.
LangGraph will fan out and run them concurrently when using async execution.

State schema for the sub-graph is separate from the main CleanRoomState because
the sub-graph operates entirely within the quarantine zone and only returns
its assembled output to the coordinator.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from cleanroom.agents.analysis.sub_agents import (
    build_api_extraction_node,
    build_behavior_probing_node,
    build_data_model_node,
    build_edge_case_node,
    build_property_node,
    build_spec_assembler_node,
)


# ---------------------------------------------------------------------------
# Sub-graph state (quarantine zone only)
# ---------------------------------------------------------------------------

class AnalysisState(TypedDict):
    """
    Internal state for the analysis sub-graph.

    This state is never directly accessible from the implementation zone —
    it lives entirely within the analysis coordinator node in the main graph.
    """

    # Input: passed in from the coordinator
    quarantine_artifacts: list[dict[str, Any]]  # source code, docs, observations
    current_module: str                          # module being analysed

    # Intermediate outputs (populated by sub-agents, consumed by later nodes)
    api_surface: list[dict[str, Any]]            # public function signatures
    test_cases: list[dict[str, Any]]             # black-box input/output pairs
    type_contracts: list[dict[str, Any]]         # normalised type definitions
    edge_cases: list[dict[str, Any]]             # error conditions + boundaries
    properties: list[dict[str, Any]]             # invariants + performance specs


# ---------------------------------------------------------------------------
# Sub-graph builder
# ---------------------------------------------------------------------------

def build_analysis_subgraph(analysis_llm: Any) -> Any:
    """
    Compile and return the analysis sub-graph.

    Parameters
    ----------
    analysis_llm:
        The quarantine-zone LLM instance.  All five sub-agents share this
        instance (they all use QUARANTINE_ZONE_API_KEY).

    Returns
    -------
    CompiledGraph
        A compiled LangGraph sub-graph, ready to be called as a node in the
        main graph via ``analysis_subgraph.invoke(state)``.
    """
    graph = StateGraph(AnalysisState)

    # --- Register nodes ---
    graph.add_node("api_extraction",   build_api_extraction_node(analysis_llm))
    graph.add_node("behavior_probing", build_behavior_probing_node(analysis_llm))
    graph.add_node("data_model",       build_data_model_node(analysis_llm))
    graph.add_node("edge_case",        build_edge_case_node(analysis_llm))
    graph.add_node("property",         build_property_node(analysis_llm))
    graph.add_node("assembler",        build_spec_assembler_node(analysis_llm))

    # --- Wire edges (spec §8.7) ---

    # api_extraction runs first; its output (api_surface) feeds the two
    # parallel downstream nodes.
    graph.add_edge(START, "api_extraction")

    # Fan out: behavior_probing and data_model run in parallel after api_extraction.
    graph.add_edge("api_extraction", "behavior_probing")
    graph.add_edge("api_extraction", "data_model")

    # Convergence: edge_case depends on test_cases from behavior_probing.
    graph.add_edge("behavior_probing", "edge_case")

    # property depends on type_contracts from data_model.
    graph.add_edge("data_model", "property")

    # Both edge_case and property must complete before the assembler can run.
    # LangGraph handles the fan-in when both source nodes have completed.
    graph.add_edge("edge_case", "assembler")
    graph.add_edge("property", "assembler")

    graph.add_edge("assembler", END)

    return graph.compile()
