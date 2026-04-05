"""
cleanroom.agents.guard.request_guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LangGraph node for the request guard agent (spec §4.2).

The request guard intercepts every clarification request from the
implementation team before it can reach the analysis team.  It is the
mechanical enforcement of the information firewall in the outbound direction.

Node contract
-------------
Input state keys read:  ``spec_gap_requests``, ``spec_store_documents``
Output state keys written: ``pending_guard_decisions``, ``audit_log``,
                            ``human_review_queue``

The node itself is side-effect-free with respect to the spec store — it only
appends to ``pending_guard_decisions`` and ``audit_log``.  The routing function
``route_request`` reads the latest guard decision and returns the appropriate
LangGraph edge label.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from cleanroom.agents.guard.prompts import REQUEST_GUARD_SYSTEM_PROMPT
from cleanroom.state import (
    CleanRoomState,
    EscalationItem,
    GuardClassification,
    GuardDecision,
    make_audit_entry,
    make_guard_decision,
    _utcnow,
)
import uuid


def build_request_guard_node(guard_llm: Any, spec_store: Any):
    """
    Factory that returns a LangGraph node function for the request guard.

    Parameters
    ----------
    guard_llm:
        The guard-zone LLM instance (uses GUARD_API_KEY, spec §11.4).
        Should be Claude Haiku or equivalent fast classifier (spec §11.3).
    spec_store:
        The spec store instance.  Used to check REDIRECT candidates — the
        guard verifies whether the behavior is already documented before
        routing the request to the analysis team.

    Returns
    -------
    Callable[[CleanRoomState], CleanRoomState]
        A LangGraph node function.
    """

    def request_guard_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Evaluate the most recent spec gap request and classify it.

        The guard receives ONLY the request text — it never sees quarantine
        artifacts or implementation context (spec §4.2, §11.5).

        The decision is logged unconditionally before routing, so FLAG and
        REJECT outcomes are always recorded even if they never reach the
        analysis team (spec §4.4).
        """
        if not state["spec_gap_requests"]:
            # No pending requests — this node should not have been called.
            return {}

        request = state["spec_gap_requests"][-1]

        # --- Build the guard's input (request text only) ---
        # The guard must never receive surrounding context from either zone.
        request_text = _format_request_for_guard(request)

        # --- Invoke the guard LLM ---
        guard_response = guard_llm.invoke([
            SystemMessage(content=REQUEST_GUARD_SYSTEM_PROMPT),
            HumanMessage(content=request_text),
        ])

        # --- Parse the structured JSON response ---
        decision_data = _parse_guard_json(guard_response.content)

        classification = GuardClassification(decision_data.get("classification", "REJECTED"))

        decision = make_guard_decision(
            direction="request",
            submitter=f"implementation_agent:{request['module']}",
            classification=classification,
            reason=decision_data.get("reason", "No reason provided."),
            reframe_guidance=decision_data.get("reframe_guidance"),
            spec_id=decision_data.get("spec_id"),
        )

        # --- Unconditional audit logging (spec §4.4) ---
        audit_entry = make_audit_entry(
            event_type="guard_decision",
            agent_id="guard_agent_request",
            details={
                "guard_decision_id": decision["request_id"],
                "direction": "request",
                "classification": decision["classification"],
                "reason": decision["reason"],
                "module": request["module"],
                "function": request["function"],
                "human_escalated": decision["human_escalated"],
            },
        )

        # --- Persist the guard decision in the spec store ---
        # (Needed so implementation agent can reference it; see spec §7.2)
        spec_store.store_guard_decision(decision)

        # --- Build escalation if needed ---
        new_escalations = list(state.get("human_review_queue", []))
        if decision["human_escalated"]:
            new_escalations.append(EscalationItem(
                item_id=str(uuid.uuid4()),
                guard_decision_id=decision["request_id"],
                reason=decision["reason"],
                submitted_at=_utcnow(),
                resolved=False,
                ruling=None,
            ))

        return {
            "pending_guard_decisions": [
                *state.get("pending_guard_decisions", []),
                decision,
            ],
            "audit_log": [
                *state.get("audit_log", []),
                audit_entry,
            ],
            "human_review_queue": new_escalations,
        }

    return request_guard_node


def route_request(state: CleanRoomState) -> str:
    """
    LangGraph conditional edge function: route based on the latest guard decision.

    Returns one of the edge labels defined in the main graph:
      ``"approved"``  → forward to analysis_coordinator
      ``"redirect"``  → return to implementation_agent with spec_id
      ``"rejected"``  → return to implementation_agent with rejection reason
      ``"reframe"``   → return to implementation_agent with reframe guidance
      ``"flag"``      → route to human_review node
    """
    if not state.get("pending_guard_decisions"):
        return "rejected"  # safe default

    latest = state["pending_guard_decisions"][-1]
    return latest["classification"].lower()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_request_for_guard(request: dict[str, Any]) -> str:
    """
    Serialise a spec gap request into the text the guard will evaluate.

    The guard receives the request text and nothing else — no surrounding
    implementation or analysis context (spec §4.2, §11.5).
    """
    return (
        f"MODULE: {request['module']}\n"
        f"FUNCTION: {request['function']}\n"
        f"INPUT CONDITIONS: {request['input_conditions']}\n"
        f"AMBIGUITY: {request['ambiguity']}\n"
        f"SPECS ALREADY CONSULTED: {', '.join(request.get('attempted_specs', []))}"
    )


def _parse_guard_json(content: str) -> dict[str, Any]:
    """
    Parse the guard's JSON response, falling back to a REJECTED decision on error.

    The guard prompt requires JSON output.  If it returns prose or malformed
    JSON, we treat it as a failure and reject the request rather than
    propagating an unclassified decision.
    """
    try:
        # Strip markdown code fences if the model added them despite instructions
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned)
    except (json.JSONDecodeError, IndexError, ValueError):
        return {
            "classification": "REJECTED",
            "reason": f"Guard returned unparseable response: {content[:200]}",
            "reframe_guidance": None,
            "spec_id": None,
        }
