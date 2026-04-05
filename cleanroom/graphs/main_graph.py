"""
cleanroom.graphs.main_graph
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The top-level LangGraph orchestration graph for the Clean Room Implementation
System (spec §8.2).

Graph topology
--------------
The graph enforces two strict information boundaries:

  QUARANTINE ZONE                │  FIREWALL  │  IMPLEMENTATION ZONE
  ─────────────────────────────  │            │  ──────────────────────────────
  analysis_coordinator           │            │  implementation_agent
  (analysis_subgraph internally) │            │  verification_agent
                                 │            │
            ─────────────────────►  request   ◄─────── spec_gap_requests
                                 │  guard     │
                                 │            │
            ◄─────────────────────  response  ────────► spec_store_writer
            analysis output      │  guard     │
                                 │            │
            ─────── human_review (shared) ───────────
                                 │            │

Edge routing summary:
  response_guard ─► {approved, redact, rewrite, reject}
    approved → spec_store_writer
    redact   → response_guard (self-loop with redacted document)
    rewrite  → analysis_coordinator
    reject   → human_review

  request_guard ─► {approved, redirect, rejected, reframe, flag}
    approved  → analysis_coordinator (forward gap request)
    redirect  → implementation_agent (spec already exists, return ID)
    rejected  → implementation_agent (with rejection reason)
    reframe   → implementation_agent (with reframe guidance)
    flag      → human_review

  verification_agent ─► {certified, rejected}
    certified → END
    rejected  → implementation_agent (with specific spec failure)

  human_review ─► {approved, rejected, reframe, blocked}
    approved  → spec_store_writer
    rejected  → implementation_agent
    reframe   → implementation_agent
    blocked   → END (with error)
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from cleanroom.agents.analysis.coordinator import build_analysis_coordinator_node
from cleanroom.agents.guard.request_guard import build_request_guard_node, route_request
from cleanroom.agents.guard.response_guard import build_response_guard_node, route_response
from cleanroom.agents.human_review import build_human_review_node, route_after_human_review
from cleanroom.agents.implementation import (
    build_implementation_agent_node,
    route_after_implementation,
)
from cleanroom.agents.verification import build_verification_agent_node, route_verification
from cleanroom.graphs.analysis_subgraph import build_analysis_subgraph
from cleanroom.state import CleanRoomState, SpecDocumentType, make_audit_entry
from cleanroom.tools import IMPLEMENTATION_AGENT_TOOLS


# ---------------------------------------------------------------------------
# Spec store writer node
# ---------------------------------------------------------------------------

def build_spec_store_writer_node(spec_store: Any):
    """
    Return a node that writes the latest guard-approved document to the spec store.

    This node is the only place where quarantine-zone content enters the spec
    store.  It only runs after the response guard has set a guard decision ID
    on the pending document (spec §8.6).
    """

    def spec_store_writer_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Persist the latest approved analysis document to the spec store.

        Reads the last entry from ``quarantine_artifacts`` (which the response
        guard has already stamped with ``_guard_decision_id``).  After writing,
        the document is also appended to ``spec_store_documents`` in state so
        the implementation agent can see it.
        """
        quarantine = state.get("quarantine_artifacts", [])
        if not quarantine:
            return {}

        pending_doc = quarantine[-1]
        guard_decision_id = pending_doc.get("_guard_decision_id")
        if not guard_decision_id:
            return {"error": "spec_store_writer: no guard_decision_id on pending document"}

        module = pending_doc.get("module", "unknown")
        content = pending_doc.get("content", {})

        # Determine document type from content shape.
        doc_type = _infer_doc_type(content)

        doc_id = spec_store.write(
            doc_type=doc_type,
            module=module,
            content=content,
            guard_decision_id=guard_decision_id,
            requester_role="analysis_agent",
        )

        # Read back the written document to add to state.
        written_doc = spec_store.read(doc_id, requester_role="orchestrator")

        audit_entry = make_audit_entry(
            event_type="spec_written",
            agent_id="spec_store_writer",
            details={
                "doc_id": doc_id,
                "module": module,
                "doc_type": doc_type.value,
                "guard_decision_id": guard_decision_id,
            },
        )

        # Remove the pending doc from quarantine_artifacts now that it's committed.
        updated_quarantine = list(quarantine[:-1])

        return {
            "quarantine_artifacts": updated_quarantine,
            "spec_store_documents": [
                *state.get("spec_store_documents", []),
                written_doc,
            ],
            "audit_log": [*state.get("audit_log", []), audit_entry],
        }

    return spec_store_writer_node


def _infer_doc_type(content: dict[str, Any]) -> SpecDocumentType:
    """Infer the document type from its content shape."""
    if content.get("gap_response"):
        return SpecDocumentType.SPEC_GAP_RESPONSE
    if "test_cases" in content:
        return SpecDocumentType.TEST_CASE
    if "type_contracts" in content:
        return SpecDocumentType.TYPE_CONTRACT
    if "properties" in content:
        return SpecDocumentType.PROPERTY_SPEC
    if "api_surface" in content:
        return SpecDocumentType.BEHAVIORAL_SPEC
    if "modules" in content:
        return SpecDocumentType.PARALLELIZATION_MAP
    return SpecDocumentType.BEHAVIORAL_SPEC  # default


# ---------------------------------------------------------------------------
# Orchestrator: module sequencing
# ---------------------------------------------------------------------------

def build_orchestrator_node():
    """
    Return a node that manages the module processing queue.

    The orchestrator reads the parallelization map from the spec store and
    advances ``current_module`` to the next unprocessed module in dependency
    order.  It does not require an LLM (spec §11.1).
    """

    def orchestrator_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Advance the pipeline to the next module in dependency order.

        Reads ``analysis_queue`` and ``implementation_queue`` to determine
        what to process next.  Updates ``current_module`` accordingly.
        """
        completed = set(state.get("completed_modules", []))

        # Check if there are modules still pending implementation.
        impl_queue = state.get("implementation_queue", [])
        pending_impl = [m for m in impl_queue if m not in completed]

        if pending_impl:
            return {"current_module": pending_impl[0]}

        # All modules complete — pipeline finished.
        return {"current_module": None}

    return orchestrator_node


def route_from_orchestrator(state: CleanRoomState) -> str:
    """Route to analysis or END based on the orchestrator's decision."""
    if state.get("current_module"):
        return "analysis_coordinator"
    return END


# ---------------------------------------------------------------------------
# Main graph builder (spec §8.2)
# ---------------------------------------------------------------------------

def build_main_graph(
    llm_instances: Any,
    spec_store: Any,
    audit_logger: Any,
) -> Any:
    """
    Compile and return the top-level orchestration graph.

    Parameters
    ----------
    llm_instances:
        The three zone-isolated LLM clients (from :func:`config.build_llm_instances`).
    spec_store:
        The spec store backend (from :func:`spec_store.build_spec_store`).
    audit_logger:
        The audit logger (from :func:`audit.build_audit_logger`).

    Returns
    -------
    CompiledGraph
        The compiled LangGraph main graph, ready for ``graph.invoke()``.
    """
    # Build the analysis sub-graph (quarantine zone).
    analysis_subgraph = build_analysis_subgraph(llm_instances.analysis)

    # Build all node functions via factories (dependency injection pattern).
    analysis_coordinator = build_analysis_coordinator_node(analysis_subgraph)
    request_guard        = build_request_guard_node(llm_instances.guard, spec_store)
    response_guard       = build_response_guard_node(llm_instances.guard, spec_store)
    spec_store_writer    = build_spec_store_writer_node(spec_store)
    implementation_agent = build_implementation_agent_node(llm_instances.implementation)
    verification_agent   = build_verification_agent_node(spec_store, audit_logger)
    human_review         = build_human_review_node()
    orchestrator         = build_orchestrator_node()

    # Tool execution node for the implementation agent's LangChain tool calls.
    tool_node = ToolNode(IMPLEMENTATION_AGENT_TOOLS)

    # --- Build the graph ---
    graph = StateGraph(CleanRoomState)

    # Register all nodes.
    graph.add_node("orchestrator",          orchestrator)
    graph.add_node("analysis_coordinator",  analysis_coordinator)
    graph.add_node("response_guard",        response_guard)
    graph.add_node("spec_store_writer",     spec_store_writer)
    graph.add_node("request_guard",         request_guard)
    graph.add_node("implementation_agent",  implementation_agent)
    graph.add_node("implementation_tools",  tool_node)
    graph.add_node("verification_agent",    verification_agent)
    graph.add_node("human_review",          human_review)

    # --- Entry point ---
    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "analysis_coordinator": "analysis_coordinator",
            END: END,
        },
    )

    # --- Analysis pipeline (quarantine zone) ---
    # Analysis output → response guard → spec store writer.
    graph.add_edge("analysis_coordinator", "response_guard")
    graph.add_conditional_edges(
        "response_guard",
        route_response,
        {
            "approved": "spec_store_writer",
            "redact":   "response_guard",   # self-loop: re-evaluate after redaction
            "rewrite":  "analysis_coordinator",
            "reject":   "human_review",
        },
    )

    # Spec store writer → implementation agent (new spec available).
    graph.add_edge("spec_store_writer", "implementation_agent")

    # --- Spec gap workflow (bidirectional across the firewall) ---
    # Implementation agent submits a gap request → request guard evaluates it.
    graph.add_conditional_edges(
        "implementation_agent",
        route_after_implementation,
        {
            "request_guard":  "request_guard",    # spec gap submitted
            "verification":   "verification_agent",
            "implementation": "implementation_tools",  # tool calls pending
        },
    )

    # Tool execution returns to the implementation agent.
    graph.add_edge("implementation_tools", "implementation_agent")

    graph.add_conditional_edges(
        "request_guard",
        route_request,
        {
            "approved":  "analysis_coordinator",  # forward gap to analysis team
            "redirect":  "implementation_agent",  # spec already exists
            "rejected":  "implementation_agent",  # return rejection + reason
            "reframe":   "implementation_agent",  # return reframe guidance
            "flag":      "human_review",
        },
    )

    # --- Verification pipeline ---
    graph.add_conditional_edges(
        "verification_agent",
        route_verification,
        {
            "certified": "orchestrator",      # advance to next module
            "rejected":  "implementation_agent",
        },
    )

    # --- Human review routing ---
    graph.add_conditional_edges(
        "human_review",
        route_after_human_review,
        {
            "approved": "spec_store_writer",      # response-direction items
            "rejected": "implementation_agent",
            "reframe":  "implementation_agent",
            "blocked":  END,
        },
    )

    return graph.compile()
