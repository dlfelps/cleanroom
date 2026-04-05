"""
cleanroom.audit
~~~~~~~~~~~~~~~
Append-only audit logger for the Clean Room Implementation System.

The audit trail is one of the three primary legal artifacts of the clean room
process (spec §9.2).  It must demonstrate that:

  1. Every piece of information the implementation team received was
     behavioral, not structural.
  2. Information passed through a documented, mechanically-enforced review.
  3. The implementation team had no access — direct or indirect — to quarantine
     zone materials.

This module provides :class:`AuditLogger`, a lightweight wrapper that enforces
the append-only invariant and exposes the surface-level metrics the orchestrator
should monitor as early warning signals (spec §9.3).

In production, swap the in-memory list for an append-only log service such as
AWS CloudTrail or an immutable time-series database.  The interface is kept
narrow so the swap is a one-function change in :func:`build_audit_logger`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from cleanroom.state import AuditEntry, GuardDecision, make_audit_entry


class AuditLogger:
    """
    In-process append-only audit log.

    All writes go through :meth:`log`; there is no delete or update method.
    This is intentional: the audit trail must be immutable.

    Attributes
    ----------
    entries:
        The ordered list of all audit entries.  Read-only from outside this
        class — callers should not mutate the list directly.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    @property
    def entries(self) -> list[AuditEntry]:
        """Return a shallow copy of the audit log (read-only view)."""
        return list(self._entries)

    def log(self, event_type: str, agent_id: str, details: dict[str, Any]) -> AuditEntry:
        """
        Append a new entry to the audit log.

        Parameters
        ----------
        event_type:
            A short string identifying the class of event, e.g.
            ``"guard_decision"``, ``"spec_written"``, ``"cert_issued"``,
            ``"spec_gap_submitted"``.
        agent_id:
            The agent that generated this event.
        details:
            Arbitrary key-value payload.  Should include any IDs or references
            needed to reconstruct the full provenance chain (spec §9.2).

        Returns
        -------
        AuditEntry
            The created entry (with auto-generated ID and timestamp).
        """
        entry = make_audit_entry(event_type, agent_id, details)
        self._entries.append(entry)
        return entry

    def log_guard_decision(self, decision: GuardDecision) -> AuditEntry:
        """
        Convenience method: append a guard decision to the audit log.

        The guard decision is also stored in the spec store separately; this
        entry links to it by ID so audit reconstruction does not require
        spec store access.
        """
        return self.log(
            event_type="guard_decision",
            agent_id=f"guard_agent_{decision['direction']}",
            details={
                "guard_decision_id": decision["request_id"],
                "direction": decision["direction"],
                "classification": decision["classification"],
                "reason": decision["reason"],
                "human_escalated": decision["human_escalated"],
            },
        )

    # ------------------------------------------------------------------
    # Metrics (spec §9.3)
    # ------------------------------------------------------------------

    def spec_gap_rate(self, module: str | None = None) -> float:
        """
        Return the spec gap request rate (gap requests / spec documents written).

        A high rate for a specific module indicates the analysis agent
        underspecified that module's behavior (spec §9.3).
        """
        gaps = [
            e for e in self._entries
            if e["event_type"] == "spec_gap_submitted"
            and (module is None or e["details"].get("module") == module)
        ]
        specs = [
            e for e in self._entries
            if e["event_type"] == "spec_written"
            and (module is None or e["details"].get("module") == module)
        ]
        if not specs:
            return 0.0
        return len(gaps) / len(specs)

    def guard_rejection_rate(self, direction: str | None = None) -> float:
        """
        Return the fraction of guard decisions that were REJECTED or REFRAME.

        A rising rejection rate may indicate implementation agent behavior
        drift (spec §9.3).

        Parameters
        ----------
        direction:
            ``"request"``, ``"response"``, or ``None`` for both directions.
        """
        decisions = [
            e for e in self._entries
            if e["event_type"] == "guard_decision"
            and (direction is None or e["details"].get("direction") == direction)
        ]
        if not decisions:
            return 0.0
        rejections = sum(
            1 for e in decisions
            if e["details"].get("classification") in ("REJECTED", "REFRAME", "REJECT")
        )
        return rejections / len(decisions)

    def flag_count(self) -> int:
        """
        Return the number of FLAG classifications.

        Any non-zero count warrants immediate investigation (spec §9.3).
        """
        return sum(
            1 for e in self._entries
            if e["event_type"] == "guard_decision"
            and e["details"].get("classification") == "FLAG"
        )

    def redaction_rate(self) -> float:
        """
        Return the fraction of response guard decisions that included redactions.

        A high redaction rate indicates the analysis team needs retraining on
        output format (spec §9.3).
        """
        response_decisions = [
            e for e in self._entries
            if e["event_type"] == "guard_decision"
            and e["details"].get("direction") == "response"
        ]
        if not response_decisions:
            return 0.0
        redacted = sum(
            1 for e in response_decisions
            if e["details"].get("classification") == "REDACT"
        )
        return redacted / len(response_decisions)

    def verification_failure_rate(self) -> float:
        """
        Return the fraction of submitted modules that failed verification.

        Tracks implementation quality against spec coverage (spec §9.3).
        """
        certs = [e for e in self._entries if e["event_type"] == "cert_issued"]
        if not certs:
            return 0.0
        failures = sum(
            1 for e in certs
            if not e["details"].get("certified", True)
        )
        return failures / len(certs)

    def metrics_summary(self) -> dict[str, Any]:
        """Return all pipeline health metrics as a single dict."""
        return {
            "spec_gap_rate": self.spec_gap_rate(),
            "guard_rejection_rate_requests": self.guard_rejection_rate("request"),
            "guard_rejection_rate_responses": self.guard_rejection_rate("response"),
            "flag_count": self.flag_count(),
            "redaction_rate": self.redaction_rate(),
            "verification_failure_rate": self.verification_failure_rate(),
            "total_audit_entries": len(self._entries),
        }


def build_audit_logger() -> AuditLogger:
    """
    Factory for the audit logger.

    In production, swap the body of this function to return a logger backed
    by an append-only external service (e.g. AWS CloudTrail, Kafka, Loki).
    The :class:`AuditLogger` interface is narrow enough that any append-only
    log service can implement it with a thin adapter.
    """
    return AuditLogger()
