"""
cleanroom.agents.guard.response_guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LangGraph node for the response guard agent (spec §4.3).

The response guard intercepts analysis team output before it is written to the
spec store.  It operates in the inbound direction — from the quarantine zone
toward the implementation zone.

Unlike the request guard, the response guard may produce a modified document
(REDACT classification).  When it does, the redacted content is stripped from
the document and the cleaned version is returned for spec store writing.

Node contract
-------------
Input state keys read:  ``quarantine_artifacts`` (raw analysis output only),
                         ``pending_guard_decisions``
Output state keys written: ``pending_guard_decisions``, ``audit_log``,
                            ``spec_store_documents``, ``human_review_queue``

The node does NOT write to the spec store directly.  It sets state flags that
the ``spec_store_writer`` node reads to perform the write.  This keeps the
node side-effect-free with respect to external storage.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from cleanroom.agents.guard.prompts import RESPONSE_GUARD_SYSTEM_PROMPT
from cleanroom.state import (
    CleanRoomState,
    EscalationItem,
    GuardClassification,
    make_audit_entry,
    make_guard_decision,
    _utcnow,
)
import uuid


def build_response_guard_node(guard_llm: Any, spec_store: Any):
    """
    Factory that returns a LangGraph node function for the response guard.

    Parameters
    ----------
    guard_llm:
        Guard-zone LLM instance.  Same instance as the request guard —
        both guards use GUARD_API_KEY (spec §11.4).
    spec_store:
        Used to persist the guard decision record.

    Returns
    -------
    Callable[[CleanRoomState], CleanRoomState]
        A LangGraph node function.
    """

    def response_guard_node(state: CleanRoomState) -> dict[str, Any]:
        """
        Evaluate the latest analysis output and classify it.

        The guard receives ONLY the document text — never the surrounding
        quarantine context or implementation state (spec §4.3, §11.5).

        After classification:
          APPROVED  → document is safe; spec_store_writer will persist it.
          REDACT    → guard strips listed phrases; cleaned doc goes to store.
          REWRITE   → doc returned to analysis_coordinator for rephrasing.
          REJECT    → escalated to human_review; nothing reaches spec store.
        """
        # The pending analysis output is staged in quarantine_artifacts.
        # The last entry is the document awaiting guard evaluation.
        quarantine = state.get("quarantine_artifacts", [])
        if not quarantine:
            return {}

        pending_doc = quarantine[-1]
        doc_text = _format_doc_for_guard(pending_doc)

        # --- Invoke the guard LLM ---
        guard_response = guard_llm.invoke([
            SystemMessage(content=RESPONSE_GUARD_SYSTEM_PROMPT),
            HumanMessage(content=doc_text),
        ])

        decision_data = _parse_guard_json(guard_response.content)
        classification = GuardClassification(decision_data.get("classification", "REJECT"))
        redacted_phrases: list[str] = decision_data.get("redacted_content", [])

        decision = make_guard_decision(
            direction="response",
            submitter="analysis_coordinator",
            classification=classification,
            reason=decision_data.get("reason", "No reason provided."),
            redacted_content=redacted_phrases,
        )

        # --- Apply redactions if REDACT ---
        approved_doc = None
        if classification == GuardClassification.APPROVED:
            approved_doc = pending_doc
        elif classification == GuardClassification.REDACT:
            approved_doc = _apply_redactions(pending_doc, redacted_phrases)
            # After redaction, reclassify the decision as effectively APPROVED
            # so the spec store writer can accept it.  The redaction record is
            # preserved in the guard decision for audit purposes.

        # --- Unconditional audit logging ---
        audit_entry = make_audit_entry(
            event_type="guard_decision",
            agent_id="guard_agent_response",
            details={
                "guard_decision_id": decision["request_id"],
                "direction": "response",
                "classification": decision["classification"],
                "reason": decision["reason"],
                "redaction_count": len(redacted_phrases),
                "human_escalated": decision["human_escalated"],
            },
        )

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

        # Attach the guard decision ID to the approved doc so the spec store
        # writer can reference it during the write.
        if approved_doc is not None:
            approved_doc = {**approved_doc, "_guard_decision_id": decision["request_id"]}

        # Stage approved doc back into quarantine_artifacts with guard decision ID.
        # spec_store_writer will consume it.
        updated_quarantine = list(quarantine[:-1])  # drop the pending doc
        if approved_doc is not None:
            updated_quarantine.append(approved_doc)

        return {
            "quarantine_artifacts": updated_quarantine,
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

    return response_guard_node


def route_response(state: CleanRoomState) -> str:
    """
    LangGraph conditional edge function: route based on the latest response
    guard decision.

    Returns one of the edge labels defined in the main graph:
      ``"approved"``  → spec_store_writer (includes post-redaction approvals)
      ``"redact"``    → response_guard (self-loop with redacted content applied)
      ``"rewrite"``   → analysis_coordinator (rephrase the document)
      ``"reject"``    → human_review
    """
    if not state.get("pending_guard_decisions"):
        return "reject"

    latest = state["pending_guard_decisions"][-1]
    classification = latest["classification"].lower()

    # After applying redactions the doc is effectively approved; route to writer.
    if classification == "redact":
        # If the last quarantine artifact has a _guard_decision_id, the
        # response_guard node already applied redactions — route to writer.
        quarantine = state.get("quarantine_artifacts", [])
        if quarantine and "_guard_decision_id" in quarantine[-1]:
            return "approved"
        return "redact"  # self-loop for a second pass (shouldn't happen normally)

    return classification


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_doc_for_guard(doc: dict[str, Any]) -> str:
    """
    Serialise a pending analysis document into the text the guard will evaluate.

    Only the document content is passed to the guard — not the surrounding
    pipeline state or quarantine zone context.
    """
    import json as _json
    content = doc.get("content", doc)
    if isinstance(content, dict):
        return _json.dumps(content, indent=2, default=str)
    return str(content)


def _apply_redactions(doc: dict[str, Any], phrases: list[str]) -> dict[str, Any]:
    """
    Remove each phrase in *phrases* from the document content.

    The guard identifies exact strings to remove (spec §4.3).  This function
    does a literal string replacement on the serialised content.

    Important: This is a best-effort textual redaction.  The response guard
    node is responsible for ensuring the remaining document is coherent.
    """
    import json as _json

    content_str = _json.dumps(doc.get("content", doc), default=str)
    for phrase in phrases:
        content_str = content_str.replace(phrase, "[REDACTED]")

    try:
        redacted_content = _json.loads(content_str)
    except _json.JSONDecodeError:
        redacted_content = {"raw": content_str}

    return {**doc, "content": redacted_content}


def _parse_guard_json(content: str) -> dict[str, Any]:
    """Parse the guard's JSON response with a safe REJECT fallback."""
    try:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned)
    except (json.JSONDecodeError, IndexError, ValueError):
        return {
            "classification": "REJECT",
            "reason": f"Guard returned unparseable response: {content[:200]}",
            "redacted_content": [],
        }
