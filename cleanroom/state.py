"""
cleanroom.state
~~~~~~~~~~~~~~~
All shared data types and the top-level LangGraph state for the Clean Room
Implementation System.

Every field in ``CleanRoomState`` is annotated with which zone(s) may read or
write it.  Context isolation (spec §8.4) is enforced at the node level via
:func:`build_isolated_context`, which accepts only the keys listed in each
node's allow-list and raises if any quarantine-only key is requested by an
implementation-zone node.

Terminology
-----------
*Quarantine zone*  — the left side of the firewall: analysis agents, source
                     code, running system.
*Implementation zone* — the right side: implementation agent, verification
                         agent, new codebase.
*Guard*            — the mandatory intermediary on both directions of
                     cross-zone communication.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from typing_extensions import TypedDict


# ===========================================================================
# Enumerations
# ===========================================================================


class GuardClassification(str, Enum):
    """
    All possible outcomes from either guard agent.

    Request guard outcomes (spec §4.2):
      APPROVED  — safe to forward to analysis team.
      REDIRECT  — already answered by an existing spec; return spec ID.
      REJECTED  — asks about implementation details; return rejection reason.
      REFRAME   — behaviorally valid intent but unsafely phrased; return
                  guidance on a safe reformulation.
      FLAG      — suggests the requester may have accessed quarantine materials;
                  escalate to human review.

    Response guard outcomes (spec §4.3):
      APPROVED  — contains only behavioral, black-box-derivable information.
      REDACT    — clean except for specific leaking phrases; remove them.
      REWRITE   — phrased to mirror original code structure; send back to
                  analysis team for rephrasing.
      REJECT    — cannot be salvaged; the underlying question needs rescoping.
    """

    APPROVED = "APPROVED"
    REDIRECT = "REDIRECT"
    REJECTED = "REJECTED"
    REFRAME = "REFRAME"
    FLAG = "FLAG"
    REDACT = "REDACT"
    REWRITE = "REWRITE"
    REJECT = "REJECT"


class SpecDocumentType(str, Enum):
    """Document types stored in the spec store (spec §7.2)."""

    BEHAVIORAL_SPEC = "behavioral_spec"
    TYPE_CONTRACT = "type_contract"
    TEST_CASE = "test_case"
    PROPERTY_SPEC = "property_spec"
    PARALLELIZATION_MAP = "parallelization_map"
    SPEC_GAP_RESPONSE = "spec_gap_response"
    GUARD_DECISION = "guard_decision"
    VERIFICATION_CERTIFICATE = "verification_certificate"


# ===========================================================================
# Core data models
# ===========================================================================


class SpecDocument(TypedDict):
    """
    An immutable, versioned document in the spec store (spec §7.2).

    ``doc_id`` follows the pattern ``{type}:{module}:v{version}``, e.g.
    ``behavioral_spec:auth_module:v3``.
    """

    doc_id: str               # "{type}:{module}:v{version}"
    doc_type: str             # SpecDocumentType value
    module: str               # target module name
    content: dict[str, Any]  # document payload (schema varies by doc_type)
    guard_decision_id: str    # the APPROVED guard decision that admitted this
    timestamp: str            # ISO-8601 UTC
    signature: str            # hex digest of content (see spec_store.py)


class GuardDecision(TypedDict):
    """
    Permanent audit record of a single guard evaluation (spec §4.4).

    Both REJECTED and FLAG decisions are escalated to the human review queue.
    Human rulings are stored in ``human_ruling`` and used as few-shot examples
    for future guard evaluations.
    """

    request_id: str                    # UUID for this evaluation
    direction: str                     # "request" or "response"
    timestamp: str                     # ISO-8601 UTC
    submitter: str                     # agent_id that submitted this item
    classification: str                # GuardClassification value
    reason: str                        # specific explanation from the guard
    reframe_guidance: str | None       # populated when classification=REFRAME
    spec_id: str | None                # populated when classification=REDIRECT
    redacted_content: list[str]        # sentences removed (if REDACT)
    human_escalated: bool
    human_ruling: str | None           # set after human review


class SpecGapRequest(TypedDict):
    """
    A structured clarification request from the implementation agent (spec §5.6).

    This is the *only* sanctioned communication channel from the implementation
    zone to the quarantine zone.  Every request is routed through the request
    guard before reaching the analysis team.
    """

    request_id: str        # UUID
    module: str            # which module triggered the ambiguity
    function: str          # specific function name
    input_conditions: str  # what inputs trigger the ambiguity
    ambiguity: str         # what behavior is undefined or contradictory
    attempted_specs: list[str]  # spec doc IDs already consulted
    what_not_how: bool     # self-attestation that the question is behavioral


class VerificationCertificate(TypedDict):
    """
    Signed certificate issued by the verification agent (spec §6.3).

    A module may not be considered complete until it holds a certificate.
    The implementation agent is explicitly prohibited from self-certifying.
    """

    certificate_id: str
    module: str
    timestamp: str                  # ISO-8601 UTC
    behavioral_tests_passed: int
    behavioral_tests_failed: int
    properties_verified: list[str]  # e.g. ["determinism", "error_recoverability"]
    spec_coverage: str              # e.g. "100%"
    decision_log_complete: bool
    certified: bool


class AuditEntry(TypedDict):
    """
    A single entry in the immutable audit log (spec §9.2).

    The audit log must demonstrate three things (spec §9.1):
      1. Every piece of information the implementation team received was
         behavioral, not structural.
      2. Information passed through a documented, mechanically-enforced review.
      3. The implementation team had no access to quarantine zone materials.
    """

    entry_id: str
    timestamp: str       # ISO-8601 UTC
    event_type: str      # e.g. "guard_decision", "spec_written", "cert_issued"
    agent_id: str
    details: dict[str, Any]


class EscalationItem(TypedDict):
    """An item in the human review queue (spec §4.4, §10.3)."""

    item_id: str
    guard_decision_id: str
    reason: str
    submitted_at: str    # ISO-8601 UTC
    resolved: bool
    ruling: str | None


# ===========================================================================
# Top-level LangGraph state (spec §8.3)
# ===========================================================================


class CleanRoomState(TypedDict):
    """
    The shared state object threaded through every node in the main graph.

    Zone annotations in comments indicate which agents may read each key.
    Context isolation (spec §8.4) is enforced by :func:`build_isolated_context`
    rather than by this TypedDict itself — Python cannot prevent attribute
    access at the type level.

    Note: ``quarantine_artifacts`` carries the ``'quarantine_only'`` annotation
    as a signal to :func:`build_isolated_context` to never expose it to nodes
    in the implementation zone.  LangGraph itself does not interpret this
    annotation; enforcement is in code.
    """

    # --- Project metadata (readable by all) ---
    project_id: str
    target_language: str

    # --- Analysis / quarantine zone (NEVER readable by implementation agent) ---
    quarantine_artifacts: Annotated[list[dict[str, Any]], "quarantine_only"]
    analysis_queue: list[str]           # modules pending analysis

    # --- Spec store (cross-zone, guard-validated only) ---
    spec_store_documents: list[SpecDocument]

    # --- Implementation zone (not readable by analysis agents) ---
    implementation_queue: list[str]     # modules pending implementation
    completed_modules: list[str]
    spec_gap_requests: list[SpecGapRequest]
    generated_files: list[dict[str, Any]]  # [{"module": str, "filename": str, "content": str}]

    # --- Guard state ---
    pending_guard_decisions: list[GuardDecision]

    # --- Audit ---
    audit_log: list[AuditEntry]
    human_review_queue: list[EscalationItem]

    # --- Orchestrator workflow state ---
    current_module: str | None          # module being processed right now
    error: str | None                   # last pipeline error, if any


# ===========================================================================
# Helper: context isolation (spec §8.4)
# ===========================================================================

#: Keys that must never be passed to implementation-zone nodes.
_QUARANTINE_ONLY_KEYS: frozenset[str] = frozenset({"quarantine_artifacts"})

#: Keys that must never be passed to analysis-zone nodes.
_IMPLEMENTATION_ONLY_KEYS: frozenset[str] = frozenset({
    "implementation_queue",
    "completed_modules",
    "spec_gap_requests",
})


def build_isolated_context(
    allowed_keys: list[str],
    state: CleanRoomState,
    node_zone: str = "implementation",
) -> dict[str, Any]:
    """
    Build a restricted view of *state* containing only *allowed_keys*.

    This is the mechanical enforcement of context isolation described in
    spec §8.4.  Every agent node **must** call this function rather than
    passing ``state`` directly to the LLM.

    Parameters
    ----------
    allowed_keys:
        The keys from ``CleanRoomState`` that this node is permitted to see.
    state:
        The full pipeline state.
    node_zone:
        Either ``"implementation"`` or ``"analysis"``.  Used to enforce that
        quarantine-only keys never appear in implementation node contexts, and
        vice versa.

    Returns
    -------
    dict
        A shallow copy of *state* containing only the permitted keys.

    Raises
    ------
    PermissionError
        If *allowed_keys* requests a quarantine-only key for an implementation
        node, or vice versa.
    """
    if node_zone == "implementation":
        forbidden = _QUARANTINE_ONLY_KEYS.intersection(allowed_keys)
        if forbidden:
            raise PermissionError(
                f"Implementation-zone node requested quarantine-only keys: "
                f"{forbidden}.  This would break clean room isolation."
            )
    elif node_zone == "analysis":
        forbidden = _IMPLEMENTATION_ONLY_KEYS.intersection(allowed_keys)
        if forbidden:
            raise PermissionError(
                f"Analysis-zone node requested implementation-only keys: "
                f"{forbidden}.  This would break clean room isolation."
            )

    return {k: state[k] for k in allowed_keys if k in state}


# ===========================================================================
# Helpers for creating typed records
# ===========================================================================

def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def make_guard_decision(
    direction: str,
    submitter: str,
    classification: GuardClassification,
    reason: str,
    *,
    reframe_guidance: str | None = None,
    spec_id: str | None = None,
    redacted_content: list[str] | None = None,
) -> GuardDecision:
    """Construct a ``GuardDecision`` with generated ID and timestamp."""
    return GuardDecision(
        request_id=str(uuid.uuid4()),
        direction=direction,
        timestamp=_utcnow(),
        submitter=submitter,
        classification=classification.value,
        reason=reason,
        reframe_guidance=reframe_guidance,
        spec_id=spec_id,
        redacted_content=redacted_content or [],
        human_escalated=classification in (GuardClassification.FLAG, GuardClassification.REJECT),
        human_ruling=None,
    )


def make_audit_entry(event_type: str, agent_id: str, details: dict[str, Any]) -> AuditEntry:
    """Construct an ``AuditEntry`` with generated ID and timestamp."""
    return AuditEntry(
        entry_id=str(uuid.uuid4()),
        timestamp=_utcnow(),
        event_type=event_type,
        agent_id=agent_id,
        details=details,
    )
