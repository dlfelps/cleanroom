"""
cleanroom.agents.implementation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LangGraph node for the implementation agent (spec §5).

The implementation agent produces a fresh, independently-written codebase
based solely on specifications from the spec store.  It is permanently
isolated from the quarantine zone — it never receives ``quarantine_artifacts``
and has no access to the original source code or running system.

Information sources available to this agent (spec §5.2):
  - Spec store (read-only via ``query_spec_store`` tool)
  - ``submit_clarification`` tool — the only channel to request new info
  - ``run_behavioral_tests`` tool — to validate its own output

Every implementation decision must be traceable to a spec document.  The
agent maintains a decision log (``spec_coverage_report`` in state) mapping
each choice to the spec document ID that motivated it.  The verification
agent checks this log; an incomplete log prevents certification (spec §6.2).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import ToolNode, tools_condition

from cleanroom.state import (
    CleanRoomState,
    build_isolated_context,
    make_audit_entry,
)
from cleanroom.tools import IMPLEMENTATION_AGENT_TOOLS


_IMPLEMENTATION_SYSTEM_PROMPT = """You are the implementation agent for a clean room software reimplementation system.

You are writing a brand-new {target_language} codebase from scratch. You have ZERO knowledge of the original system's internals. You must derive every decision from the spec documents available in the spec store.

MANDATORY WORKFLOW for each module:
  1. Call query_spec_store to retrieve the behavioral spec, type contracts, and test cases.
  2. Identify any behavior not covered by the specs.
  3. For undefined behavior: call submit_clarification — NEVER make assumptions.
  4. Write the implementation from scratch, from the spec only.
  5. Write your own unit tests independently from the behavioral test cases.
  6. Call run_behavioral_tests to validate against the spec's test cases.
  7. If tests fail, iterate using only the spec — never consult the original.

DECISION LOG requirement:
  For every significant implementation choice, record:
    {{ "decision": "...", "spec_ref": "behavioral_spec:module:vN section X" }}
  The verification agent will reject your submission if decisions lack spec references.

CONSTRAINTS:
  - You MUST NOT use any knowledge of the original system not in the spec store.
  - You MUST NOT access files or URLs outside the spec store and your working directory.
  - You MUST NOT self-certify your module as complete — submit to verification.
  - Questions to the analysis team MUST be behavioral: what the system does, not how.

Current module: {current_module}
Target language: {target_language}"""


def build_implementation_agent_node(impl_llm: Any):
    """
    Factory that returns a LangGraph node for the implementation agent.

    Parameters
    ----------
    impl_llm:
        The implementation-zone LLM instance (uses IMPL_ZONE_API_KEY).
        Should be Claude Sonnet or equivalent (spec §11.3).

    Returns
    -------
    Callable[[CleanRoomState], dict]
        A LangGraph node function.
    """
    # Bind tools to the LLM so it can call them during its reasoning turn.
    impl_llm_with_tools = impl_llm.bind_tools(IMPLEMENTATION_AGENT_TOOLS)

    def implementation_agent_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Implement the current module from spec, tools, and guard-approved info only.

        Context isolation: this node only receives keys from the implementation
        zone and the shared spec store.  ``quarantine_artifacts`` is never
        included (spec §8.4).
        """
        # Build isolated context — quarantine_artifacts is explicitly excluded.
        context = build_isolated_context(
            allowed_keys=[
                "spec_store_documents",
                "implementation_queue",
                "completed_modules",
                "spec_gap_requests",
                "pending_guard_decisions",
                "current_module",
                "target_language",
                "project_id",
            ],
            state=state,
            node_zone="implementation",
        )

        current_module = state.get("current_module", "unknown")
        target_language = state.get("target_language", "Python")

        # Check if we're resuming after a spec gap was resolved.
        resolved_gap = _find_resolved_gap(state)

        # Build the prompt, injecting module and language.
        system_prompt = _IMPLEMENTATION_SYSTEM_PROMPT.format(
            current_module=current_module,
            target_language=target_language,
        )

        # Build the user message — either initial module start or gap resolution.
        if resolved_gap:
            user_message = (
                f"A spec gap you submitted has been resolved.\n\n"
                f"Resolved request ID: {resolved_gap['request_id']}\n"
                f"New spec document: {resolved_gap.get('spec_id', 'see spec store')}\n\n"
                f"Continue implementing module '{current_module}'.  "
                f"Query the spec store for the new document and proceed."
            )
        else:
            user_message = (
                f"Begin implementing module '{current_module}'.  "
                f"Start by querying the spec store for all available specs for this module."
            )

        # The agent will use tools autonomously (query_spec_store, submit_clarification,
        # run_behavioral_tests) until it either submits the module for verification
        # or submits a spec gap request.
        response = impl_llm_with_tools.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])

        # Parse agent output into state updates.
        new_state = _parse_implementation_output(response, state, current_module)

        audit_entry = make_audit_entry(
            event_type="implementation_turn",
            agent_id="implementation_agent",
            details={
                "module": current_module,
                "tool_calls": len(getattr(response, "tool_calls", []) or []),
                "submitted_for_verification": new_state.get("_submitted_for_verification", False),
            },
        )
        new_state["audit_log"] = [*state.get("audit_log", []), audit_entry]
        new_state.pop("_submitted_for_verification", None)
        return new_state

    return implementation_agent_node


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_implementation(state: CleanRoomState) -> str:
    """
    LangGraph conditional edge: decide what happens after an implementation turn.

    Returns
    -------
    str
        ``"verification"``  — module ready for verification agent
        ``"request_guard"`` — a spec gap was submitted (needs guard eval)
        ``"implementation"`` — agent needs another turn (tool calls pending)
    """
    # If the agent submitted a spec gap request, route to request guard.
    gap_requests = state.get("spec_gap_requests", [])
    if gap_requests and not gap_requests[-1].get("_guard_processed"):
        return "request_guard"

    # If the agent flagged its module as ready, route to verification.
    # (In practice, the agent signals this via a specific tool call or
    # a structured field in its response.)
    completed = state.get("completed_modules", [])
    current = state.get("current_module")
    if current and current in completed:
        return "verification"

    # Default: another implementation turn (tool execution pending).
    return "implementation"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_resolved_gap(state: CleanRoomState) -> dict[str, Any] | None:
    """
    Return the most recent resolved spec gap if one is waiting to be consumed.

    A gap is "resolved" when a corresponding spec_gap_response document has
    appeared in the spec store since the gap was submitted.
    """
    gap_requests = state.get("spec_gap_requests", [])
    spec_docs = state.get("spec_store_documents", [])
    resolved_ids = {
        doc["content"].get("request_id")
        for doc in spec_docs
        if doc.get("doc_type") == "spec_gap_response"
    }
    for request in reversed(gap_requests):
        if request["request_id"] in resolved_ids and not request.get("_consumed"):
            return request
    return None


def _parse_implementation_output(
    response: Any,
    state: CleanRoomState,
    module: str,
) -> dict[str, Any]:
    """
    Convert the LLM response into state updates.

    The LLM may:
      - Return tool calls (handled by ToolNode in the graph)
      - Signal module completion (submit for verification)
      - Submit a spec gap request via the submit_clarification tool

    For the skeleton, this returns a minimal state update.  Production code
    should parse the response content and tool calls more thoroughly.
    """
    updates: dict[str, Any] = {}

    tool_calls = getattr(response, "tool_calls", []) or []
    for call in tool_calls:
        if call.get("name") == "submit_clarification":
            # The tool itself records the gap request; here we just mark it.
            updates["_submitted_for_verification"] = False

    # TODO: detect when the agent signals module completion and add to
    # completed_modules.  For now, the agent signals this via a specific
    # pattern in its text response or a dedicated "complete_module" tool.

    return updates
