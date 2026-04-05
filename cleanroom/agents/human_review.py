"""
cleanroom.agents.human_review
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LangGraph node for the human review queue (spec §4.4, §10.3).

The human review queue is a first-class operational concern (spec §10.3).
A backed-up queue blocks the entire pipeline on FLAG events.  Every FLAG or
REJECT guard decision is escalated here; the pipeline does not proceed until
a human ruling is recorded.

Human rulings serve two purposes:
  1. Immediate: unblock the pipeline by providing a definitive ruling.
  2. Long-term: become few-shot examples in the guard prompts to improve
     future classification consistency (spec §4.4).

In the skeleton, ``human_review_node`` is a stub that prints the escalation
to stdout and waits for input.  In production, replace the body with a call
to your chosen review dashboard (e.g. a Slack notification, a web queue UI,
or a ticketing system).
"""

from __future__ import annotations

from typing import Any

from cleanroom.state import CleanRoomState, EscalationItem, make_audit_entry, _utcnow


def build_human_review_node():
    """
    Factory that returns a LangGraph node for the human review queue.

    Returns
    -------
    Callable[[CleanRoomState], dict]
        A LangGraph node function.

    TODO: Production implementation
    -------
    Replace the stdin-based stub below with an integration to your chosen
    review interface:
      - REST webhook to a review dashboard
      - Slack message via the Slack API
      - GitHub issue or Jira ticket creation
      - Email notification with a structured review form

    The node should block until the review is complete (the SLA is defined
    in HUMAN_REVIEW_SLA_MINUTES).  If the SLA is exceeded, escalate further
    or surface a pipeline alert.
    """

    def human_review_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Handle escalated FLAG or REJECT guard decisions.

        Presents the escalation to a human reviewer and waits for a ruling.
        The ruling is recorded in the state and in the audit log.

        In production, this function would be an async callback that resumes
        the LangGraph checkpoint when the human submits their decision via an
        external interface.
        """
        queue = state.get("human_review_queue", [])
        unresolved = [item for item in queue if not item["resolved"]]

        if not unresolved:
            # Nothing to review — node should not have been called.
            return {}

        # Process the oldest unresolved item first (FIFO queue).
        item = unresolved[0]

        print("\n" + "=" * 70)
        print("HUMAN REVIEW REQUIRED")
        print("=" * 70)
        print(f"Item ID:           {item['item_id']}")
        print(f"Guard Decision ID: {item['guard_decision_id']}")
        print(f"Reason:            {item['reason']}")
        print(f"Submitted at:      {item['submitted_at']}")
        print()
        print("Options:")
        print("  approve  — Allow this content to proceed through the pipeline")
        print("  reject   — Permanently block this content")
        print("  reframe  — Return to submitter with guidance to rephrase")
        print()

        # TODO: Replace this with an external review interface call.
        # For the skeleton, we read from stdin (blocking).
        ruling = input("Enter ruling (approve/reject/reframe): ").strip().lower()
        rationale = input("Enter rationale (stored in audit log): ").strip()

        # Update the item in the queue.
        updated_queue = []
        for q_item in queue:
            if q_item["item_id"] == item["item_id"]:
                updated_queue.append(EscalationItem(
                    **{**q_item, "resolved": True, "ruling": f"{ruling}: {rationale}"}
                ))
            else:
                updated_queue.append(q_item)

        audit_entry = make_audit_entry(
            event_type="human_review_completed",
            agent_id="human_reviewer",
            details={
                "item_id": item["item_id"],
                "guard_decision_id": item["guard_decision_id"],
                "ruling": ruling,
                "rationale": rationale,
                "timestamp": _utcnow(),
            },
        )

        print(f"\nRuling recorded: {ruling.upper()}")
        print("=" * 70)

        return {
            "human_review_queue": updated_queue,
            "audit_log": [*state.get("audit_log", []), audit_entry],
        }

    return human_review_node


def route_after_human_review(state: CleanRoomState) -> str:
    """
    LangGraph conditional edge: route based on the human reviewer's ruling.

    Returns
    -------
    str
        ``"approved"``  → spec_store_writer (for response-direction items)
        ``"rejected"``  → implementation_agent (with rejection)
        ``"reframe"``   → implementation_agent (with reframe guidance)
        ``"blocked"``   → END with error (unresolvable)
    """
    queue = state.get("human_review_queue", [])
    resolved = [item for item in queue if item["resolved"] and item.get("ruling")]

    if not resolved:
        return "blocked"

    latest_ruling = resolved[-1]["ruling"] or ""
    if latest_ruling.startswith("approve"):
        return "approved"
    elif latest_ruling.startswith("reframe"):
        return "reframe"
    else:
        return "rejected"
