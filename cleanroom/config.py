"""
cleanroom.config
~~~~~~~~~~~~~~~~
Central configuration for the Clean Room Implementation System.

Every LLM instance and storage backend is configured here.  The three
separate API keys are the infrastructure-level foundation of zone isolation:
  - QUARANTINE_ZONE_API_KEY  → analysis agent only
  - GUARD_API_KEY            → both guard agents (request + response)
  - IMPL_ZONE_API_KEY        → implementation agent only

Keeping these keys separate ensures that even if an agent node calls the wrong
LLM instance by accident, it will fail at the API layer before any cross-zone
information can be exchanged.  See spec §11.4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()


# ---------------------------------------------------------------------------
# Model identifiers (spec §11.3)
# ---------------------------------------------------------------------------

#: Strong model for complex reasoning: spec synthesis and code generation.
ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "claude-sonnet-4-20250514")

#: Fast classification model for the guard agents on the critical path.
GUARD_MODEL: str = os.getenv("GUARD_MODEL", "claude-haiku-4-5-20251001")

#: Strong model for the implementation agent, matching the analysis agent tier.
IMPL_MODEL: str = os.getenv("IMPL_MODEL", "claude-sonnet-4-20250514")


# ---------------------------------------------------------------------------
# Zone API keys (spec §11.4)
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Return the value of *name* from the environment, or raise if missing."""
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Copy .env.example to .env and fill in your API keys."
        )
    return value


@dataclass(frozen=True)
class LLMInstances:
    """
    Holds the three zone-isolated LLM instances.

    Constructed once at startup via :func:`build_llm_instances` and injected
    into every node that needs an LLM.  Nodes that do *not* need an LLM
    (orchestrator, spec store, verification core) never receive this object.

    Attributes
    ----------
    analysis:
        Quarantine-zone model.  Receives source code and running-system
        observations.  Uses QUARANTINE_ZONE_API_KEY.
    guard:
        Shared by both guard agents.  Receives only the text being evaluated,
        never zone-specific context.  Uses GUARD_API_KEY.
    implementation:
        Implementation-zone model.  Receives spec documents only.  Uses
        IMPL_ZONE_API_KEY.
    """

    analysis: ChatAnthropic
    guard: ChatAnthropic
    implementation: ChatAnthropic


def build_llm_instances() -> LLMInstances:
    """
    Instantiate the three zone-isolated LLM clients.

    Reads API keys from environment variables.  Raises :exc:`EnvironmentError`
    if any key is missing so the pipeline fails fast rather than silently
    using the wrong credentials.

    Returns
    -------
    LLMInstances
        Ready-to-use LLM clients, one per zone.
    """
    return LLMInstances(
        analysis=ChatAnthropic(
            model=ANALYSIS_MODEL,
            api_key=_require_env("QUARANTINE_ZONE_API_KEY"),
        ),
        guard=ChatAnthropic(
            model=GUARD_MODEL,
            api_key=_require_env("GUARD_API_KEY"),
        ),
        implementation=ChatAnthropic(
            model=IMPL_MODEL,
            api_key=_require_env("IMPL_ZONE_API_KEY"),
        ),
    )


# ---------------------------------------------------------------------------
# Spec store backend selection
# ---------------------------------------------------------------------------

#: "memory" for local development; "s3" for production (see spec §10.2).
SPEC_STORE_BACKEND: str = os.getenv("SPEC_STORE_BACKEND", "memory")

#: S3 bucket name — only required when SPEC_STORE_BACKEND="s3".
SPEC_STORE_S3_BUCKET: str | None = os.getenv("SPEC_STORE_S3_BUCKET")

#: DynamoDB table name — only required when SPEC_STORE_BACKEND="s3".
SPEC_STORE_DYNAMO_TABLE: str | None = os.getenv("SPEC_STORE_DYNAMO_TABLE")


# ---------------------------------------------------------------------------
# Human review queue
# ---------------------------------------------------------------------------

#: Minutes before a FLAG event times out and escalates further.
HUMAN_REVIEW_SLA_MINUTES: int = int(os.getenv("HUMAN_REVIEW_SLA_MINUTES", "60"))
