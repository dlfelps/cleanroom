"""
cleanroom.agents.analysis.coordinator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The analysis coordinator is the entry point into the quarantine zone.

It does not perform analysis itself — it delegates to specialised sub-agents
via the analysis sub-graph (see ``graphs/analysis_subgraph.py``) and then
stages the assembled specification document for response guard evaluation.

In the main graph, this node is invoked in two situations:
  1. Initial analysis: processing a module for the first time.
  2. Spec gap resolution: the analysis team received an approved clarification
     request from the request guard and must produce a targeted response.

Both cases go through the same response guard before anything reaches the
spec store (spec §3.1).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from cleanroom.state import CleanRoomState, build_isolated_context, make_audit_entry


_COORDINATOR_SYSTEM_PROMPT = """You are the analysis coordinator for a clean room software reimplementation system.

You operate entirely within the quarantine zone. You have access to the original source code, the running system, and all documentation. Your job is to coordinate the production of complete, implementation-agnostic behavioral specifications.

Your output will be reviewed by a firewall agent before it can reach the implementation team. The firewall will REJECT any content that reveals internal implementation details.

Before producing any output, apply this safety test to every sentence:
  "Could this sentence have been written by someone who only had access to a black-box running system and public documentation?"
  If NO, remove or rephrase it.

You coordinate five sub-agents. When handling a spec gap request:
  1. Read the approved clarification request carefully.
  2. Identify which sub-agent(s) should respond.
  3. Produce a targeted behavioral answer — do not produce a full module spec for a focused question.
  4. Apply the safety test before submitting."""


def build_analysis_coordinator_node(analysis_subgraph: Any):
    """
    Factory that returns a LangGraph node for the analysis coordinator.

    Parameters
    ----------
    analysis_subgraph:
        The compiled analysis sub-graph (from ``graphs/analysis_subgraph.py``).
        The coordinator delegates full-module analysis to this sub-graph.

    Returns
    -------
    Callable[[CleanRoomState], dict]
        A LangGraph node function.
    """

    def analysis_coordinator_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Coordinate analysis for the current module or spec gap request.

        For initial module analysis, delegates to the analysis sub-graph.
        For spec gap responses, produces a targeted behavioral answer.

        Context isolation: this node reads ``quarantine_artifacts`` and
        ``analysis_queue``, but never reads implementation-zone state.
        """
        # Build isolated context: only quarantine zone keys are allowed here.
        context = build_isolated_context(
            allowed_keys=["quarantine_artifacts", "analysis_queue", "current_module", "project_id"],
            state=state,
            node_zone="analysis",
        )

        current_module = state.get("current_module")
        pending_decisions = state.get("pending_guard_decisions", [])

        # Check if we're responding to a spec gap request (i.e. an approved
        # clarification arrived from the request guard).
        incoming_gap_response = _find_approved_gap_request(pending_decisions)

        if incoming_gap_response:
            return _handle_spec_gap_response(incoming_gap_response, context, state)
        else:
            return _handle_initial_analysis(current_module, context, state, analysis_subgraph)

    return analysis_coordinator_node


# ---------------------------------------------------------------------------
# Internal handlers
# ---------------------------------------------------------------------------

def _handle_initial_analysis(
    module: str | None,
    context: dict[str, Any],
    state: CleanRoomState,
    analysis_subgraph: Any,
) -> dict[str, Any]:
    """
    Run the full analysis sub-graph for a new module.

    The sub-graph produces api_surface, test_cases, type_contracts, edge_cases,
    and properties.  The spec_assembler node then packages them into a single
    pending document staged in quarantine_artifacts for the response guard.
    """
    if not module:
        return {"error": "analysis_coordinator called with no current_module"}

    # Initialise the sub-graph state with quarantine artifacts for this module.
    subgraph_input = {
        "quarantine_artifacts": context.get("quarantine_artifacts", []),
        "current_module": module,
        "api_surface": [],
        "test_cases": [],
        "type_contracts": [],
        "edge_cases": [],
        "properties": [],
    }

    # TODO: invoke the compiled analysis_subgraph
    # subgraph_output = analysis_subgraph.invoke(subgraph_input)
    # For now, return a stub pending document.
    subgraph_output = subgraph_input  # placeholder

    audit_entry = make_audit_entry(
        event_type="analysis_started",
        agent_id="analysis_coordinator",
        details={"module": module},
    )

    return {
        "quarantine_artifacts": subgraph_output.get("quarantine_artifacts", []),
        "audit_log": [*state.get("audit_log", []), audit_entry],
    }


def _handle_spec_gap_response(
    gap_request: dict[str, Any],
    context: dict[str, Any],
    state: CleanRoomState,
) -> dict[str, Any]:
    """
    Produce a targeted behavioral response to an approved spec gap request.

    The response is staged in quarantine_artifacts for the response guard.
    It will only reach the spec store if the response guard approves it.
    """
    # The gap request text is already guard-approved — safe to forward to LLM.
    pending_doc = {
        "type": "pending_spec",
        "module": gap_request.get("module", "unknown"),
        "content": {
            "gap_response": True,
            "request_id": gap_request.get("request_id"),
            # TODO: actual LLM call to produce the behavioral response
            # response = analysis_llm.invoke([
            #     SystemMessage(content=_COORDINATOR_SYSTEM_PROMPT),
            #     HumanMessage(content=f"Approved clarification request:\n{gap_request}"),
            # ])
            # "behavioral_answer": parse_behavioral_answer(response.content),
            "behavioral_answer": "TODO: populate from LLM response",
        },
    }

    quarantine = list(context.get("quarantine_artifacts", []))
    quarantine.append(pending_doc)

    audit_entry = make_audit_entry(
        event_type="spec_gap_response_drafted",
        agent_id="analysis_coordinator",
        details={"request_id": gap_request.get("request_id")},
    )

    return {
        "quarantine_artifacts": quarantine,
        "audit_log": [*state.get("audit_log", []), audit_entry],
    }


def _find_approved_gap_request(decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Return the most recent APPROVED gap request decision, if any.

    This signals that the request guard passed a clarification request through
    to the analysis team and we should respond to it.
    """
    for decision in reversed(decisions):
        if (
            decision.get("direction") == "request"
            and decision.get("classification") == "APPROVED"
            and not decision.get("_handled")
        ):
            return decision
    return None
