"""
cleanroom.spec_store
~~~~~~~~~~~~~~~~~~~~
The Spec Store is the single legal artifact boundary of the clean room process
(spec §7).  It is not merely a database — it is the document a court or auditor
would inspect to verify that the clean room process was followed correctly.

Properties enforced here
------------------------
* **Immutability** — documents are append-only.  A new version of a spec does
  not overwrite the previous version; both are retained permanently.
* **Guard gating** — every write requires the ID of an APPROVED guard decision.
  The store verifies that decision before accepting the document.
* **Access control** — each ``read()`` call declares the requester's role;
  quarantine-zone documents (prefixed ``quarantine:``) are never returned to
  implementation-role requesters.
* **Cryptographic signing** — each document is signed with a SHA-256 digest of
  its serialised content so tampering is detectable during audit.
* **Versioning** — document IDs include a monotonic version component, e.g.
  ``behavioral_spec:auth_module:v3``.

Backends
--------
Two backends are provided:
  ``MemorySpecStore``  — in-process dict; suitable for development and tests.
  ``S3SpecStore``      — stub for the production S3 + DynamoDB backend
                         described in spec §10.2.  Not yet implemented.

Use :func:`build_spec_store` to select the backend via the ``SPEC_STORE_BACKEND``
environment variable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from cleanroom.state import (
    GuardClassification,
    GuardDecision,
    SpecDocument,
    SpecDocumentType,
    make_audit_entry,
)


# ---------------------------------------------------------------------------
# Role-based access control table (spec §7.4)
# ---------------------------------------------------------------------------

#: Maps role name → set of document types that role may write.
_WRITE_PERMISSIONS: dict[str, set[str]] = {
    "analysis_agent":   {t.value for t in SpecDocumentType} - {
        SpecDocumentType.VERIFICATION_CERTIFICATE.value,
        SpecDocumentType.GUARD_DECISION.value,
    },
    "guard_agent":      {SpecDocumentType.GUARD_DECISION.value},
    "verification_agent": {SpecDocumentType.VERIFICATION_CERTIFICATE.value},
    "orchestrator":     {"audit_log", "workflow_state"},
}

#: Roles that may read spec-store documents (all except quarantine: prefix).
_READ_ROLES: frozenset[str] = frozenset({
    "guard_agent",
    "implementation_agent",
    "verification_agent",
    "orchestrator",
})


def _sign(content: dict[str, Any]) -> str:
    """Return a hex SHA-256 digest of the JSON-serialised *content*."""
    serialised = json.dumps(content, sort_keys=True, default=str).encode()
    return hashlib.sha256(serialised).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class SpecStore:
    """
    Abstract base class defining the spec store interface.

    All concrete backends must implement :meth:`write`, :meth:`read`,
    :meth:`list_documents`, and :meth:`get_guard_decision`.
    """

    def write(
        self,
        doc_type: SpecDocumentType,
        module: str,
        content: dict[str, Any],
        guard_decision_id: str,
        requester_role: str = "analysis_agent",
    ) -> str:
        """
        Write a new spec document after verifying guard approval.

        Parameters
        ----------
        doc_type:
            The type of document being written (spec §7.2).
        module:
            The module this document describes.
        content:
            The document payload.  Schema varies by ``doc_type``; the store
            does not validate structure — that is the guard agent's job.
        guard_decision_id:
            ID of the APPROVED ``GuardDecision`` that admitted this content.
        requester_role:
            Role of the agent writing this document.  Must appear in
            ``_WRITE_PERMISSIONS`` for the given ``doc_type``.

        Returns
        -------
        str
            The canonical document ID: ``{doc_type}:{module}:v{version}``.

        Raises
        ------
        PermissionError
            If the guard decision is not found or not APPROVED, or if
            ``requester_role`` lacks write permission for this document type.
        """
        raise NotImplementedError

    def read(self, doc_id: str, requester_role: str) -> SpecDocument:
        """
        Retrieve a document by its full versioned ID.

        Raises
        ------
        PermissionError
            If ``requester_role`` is in the implementation zone and the
            document is quarantine-zone-only.
        KeyError
            If no document with *doc_id* exists.
        """
        raise NotImplementedError

    def list_documents(self, module: str | None = None) -> list[str]:
        """Return all document IDs, optionally filtered by module."""
        raise NotImplementedError

    def get_guard_decision(self, decision_id: str) -> GuardDecision | None:
        """Return a stored guard decision by ID, or None if not found."""
        raise NotImplementedError

    def store_guard_decision(self, decision: GuardDecision) -> None:
        """Persist a guard decision so it can be referenced by later writes."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory backend (development / tests)
# ---------------------------------------------------------------------------

class MemorySpecStore(SpecStore):
    """
    In-process implementation of :class:`SpecStore`.

    All data lives in Python dicts and is lost when the process exits.  This
    backend is suitable for local development and unit tests.  It faithfully
    enforces immutability, signing, versioning, and access control — the only
    difference from the production backend is that data is not durable.

    For production, replace with :class:`S3SpecStore` (see spec §10.2).
    """

    def __init__(self) -> None:
        # Primary document store: doc_id → SpecDocument
        self._documents: dict[str, SpecDocument] = {}
        # Version counter per (doc_type, module): used to auto-increment
        self._version_counters: dict[str, int] = {}
        # Guard decisions indexed by request_id
        self._guard_decisions: dict[str, GuardDecision] = {}

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def write(
        self,
        doc_type: SpecDocumentType,
        module: str,
        content: dict[str, Any],
        guard_decision_id: str,
        requester_role: str = "analysis_agent",
    ) -> str:
        self._check_write_permission(requester_role, doc_type)
        self._verify_guard_approval(guard_decision_id)

        version = self._increment_version(doc_type, module)
        doc_id = f"{doc_type.value}:{module}:v{version}"

        doc: SpecDocument = {
            "doc_id": doc_id,
            "doc_type": doc_type.value,
            "module": module,
            "content": content,
            "guard_decision_id": guard_decision_id,
            "timestamp": _utcnow(),
            "signature": _sign(content),
        }

        self._documents[doc_id] = doc
        return doc_id

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def read(self, doc_id: str, requester_role: str) -> SpecDocument:
        if requester_role not in _READ_ROLES:
            raise PermissionError(
                f"Role '{requester_role}' does not have read access to the spec store."
            )
        if doc_id.startswith("quarantine:") and requester_role == "implementation_agent":
            raise PermissionError(
                f"Implementation agent attempted to read quarantine document '{doc_id}'. "
                "This would violate clean room isolation."
            )
        if doc_id not in self._documents:
            raise KeyError(f"Spec document '{doc_id}' not found.")
        return self._documents[doc_id]

    def list_documents(self, module: str | None = None) -> list[str]:
        """Return all doc IDs, optionally filtered by module name."""
        if module is None:
            return list(self._documents.keys())
        return [
            doc_id for doc_id, doc in self._documents.items()
            if doc["module"] == module
        ]

    # ------------------------------------------------------------------
    # Guard decision storage
    # ------------------------------------------------------------------

    def store_guard_decision(self, decision: GuardDecision) -> None:
        """Persist a guard decision so it can later be referenced by writes."""
        self._guard_decisions[decision["request_id"]] = decision

    def get_guard_decision(self, decision_id: str) -> GuardDecision | None:
        return self._guard_decisions.get(decision_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_write_permission(self, role: str, doc_type: SpecDocumentType) -> None:
        allowed = _WRITE_PERMISSIONS.get(role, set())
        if doc_type.value not in allowed:
            raise PermissionError(
                f"Role '{role}' is not permitted to write '{doc_type.value}' documents."
            )

    def _verify_guard_approval(self, decision_id: str) -> None:
        decision = self._guard_decisions.get(decision_id)
        if decision is None:
            raise PermissionError(
                f"Guard decision '{decision_id}' not found.  "
                "All spec store writes require a preceding APPROVED guard decision."
            )
        if decision["classification"] != GuardClassification.APPROVED.value:
            raise PermissionError(
                f"Guard decision '{decision_id}' has classification "
                f"'{decision['classification']}', not APPROVED.  "
                "Only APPROVED decisions unlock spec store writes."
            )

    def _increment_version(self, doc_type: SpecDocumentType, module: str) -> int:
        key = f"{doc_type.value}:{module}"
        self._version_counters[key] = self._version_counters.get(key, 0) + 1
        return self._version_counters[key]


# ---------------------------------------------------------------------------
# Production S3 backend (stub — not yet implemented)
# ---------------------------------------------------------------------------

class S3SpecStore(SpecStore):
    """
    Production spec store backed by S3 (Object Lock for immutability) and
    DynamoDB (provenance index and version counters).

    See spec §10.2 for the full infrastructure architecture.

    TODO: Implement using ``boto3`` with:
      - S3 Object Lock in COMPLIANCE mode for document immutability
      - DynamoDB conditional writes for atomic version counter increments
      - AWS KMS for document signing / verification
      - IAM role-based access control mapping to the role table in spec §7.4
    """

    def __init__(self, s3_bucket: str, dynamo_table: str) -> None:
        self._s3_bucket = s3_bucket
        self._dynamo_table = dynamo_table
        # TODO: initialise boto3 clients here

    def write(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError("S3SpecStore.write() is not yet implemented.")

    def read(self, *args: Any, **kwargs: Any) -> SpecDocument:
        raise NotImplementedError("S3SpecStore.read() is not yet implemented.")

    def list_documents(self, *args: Any, **kwargs: Any) -> list[str]:
        raise NotImplementedError("S3SpecStore.list_documents() is not yet implemented.")

    def get_guard_decision(self, *args: Any, **kwargs: Any) -> GuardDecision | None:
        raise NotImplementedError("S3SpecStore.get_guard_decision() is not yet implemented.")

    def store_guard_decision(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("S3SpecStore.store_guard_decision() is not yet implemented.")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_spec_store() -> SpecStore:
    """
    Return the appropriate spec store backend based on ``SPEC_STORE_BACKEND``.

    Import this function in the main entry point and pass the resulting store
    into the graph builder.  All nodes receive it via dependency injection
    rather than importing it as a global, so backends can be swapped in tests.
    """
    from cleanroom.config import SPEC_STORE_BACKEND, SPEC_STORE_S3_BUCKET, SPEC_STORE_DYNAMO_TABLE

    if SPEC_STORE_BACKEND == "memory":
        return MemorySpecStore()
    elif SPEC_STORE_BACKEND == "s3":
        if not SPEC_STORE_S3_BUCKET or not SPEC_STORE_DYNAMO_TABLE:
            raise EnvironmentError(
                "SPEC_STORE_BACKEND=s3 requires SPEC_STORE_S3_BUCKET and "
                "SPEC_STORE_DYNAMO_TABLE to be set."
            )
        return S3SpecStore(SPEC_STORE_S3_BUCKET, SPEC_STORE_DYNAMO_TABLE)
    else:
        raise ValueError(f"Unknown SPEC_STORE_BACKEND: '{SPEC_STORE_BACKEND}'")
