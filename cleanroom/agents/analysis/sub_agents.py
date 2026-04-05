"""
cleanroom.agents.analysis.sub_agents
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The five specialised sub-agents that make up the analysis coordinator
(spec §3.5).  Each sub-agent produces a typed output document; the
coordinator assembles them into the full specification package before
submitting to the response guard.

Sub-agent roster:
  APIExtractionAgent   — public signatures, type normalisation (spec §3.5)
  BehaviorProbingAgent — harness execution, test case generation (spec §3.6)
  DataModelAgent       — type contracts, schema extraction (spec §3.5)
  EdgeCaseAgent        — error conditions, boundary values (spec §3.5)
  PropertyAgent        — invariants, performance contracts (spec §3.5)

Each sub-agent is implemented as a single async-compatible function that takes
an ``AnalysisState`` dict and returns a partial update to that state.  The
LangGraph sub-graph in ``analysis_subgraph.py`` wires them together with the
appropriate parallel and sequential edges.

Constraints enforced by every sub-agent (spec §3.4):
  * MUST NOT include internal variable names, algorithm descriptions, data
    structure choices, or concurrency patterns in output.
  * MUST NOT explain why the system behaves a certain way.
  * MUST normalise types to their behavioral equivalents.
  * MUST derive test cases from observation, not from source code structure.
  * Safety test: "Could this spec have been written by someone with only
    public docs and a black-box running system?"
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_API_EXTRACTION_PROMPT = """You are the API extraction sub-agent for a clean room analysis system.

Your task: produce a complete, implementation-agnostic description of the public API surface.

You MUST:
  - List every public function, method, endpoint, or exported symbol.
  - Document parameter names, types (normalised to behavioral contracts), and whether they are required or optional.
  - Document return types normalised to their behavioral equivalents (e.g. LinkedList → ordered List).
  - Document which parameters are mutually exclusive or have ordering dependencies.

You MUST NOT:
  - Include internal class names, private methods, or implementation-specific types.
  - Describe HOW functions are implemented.
  - Include file paths, module paths, or internal namespace structure.

Output a JSON object with key "api_surface" containing a list of function descriptors."""

_BEHAVIOR_PROBING_PROMPT = """You are the behavior probing sub-agent for a clean room analysis system.

Your task: generate black-box behavioral test cases by observing the running system.

For each public function or endpoint:
  - Generate test cases for: valid inputs, boundary values, invalid inputs, empty/null inputs.
  - Record: return value, exception/error type, side effects, execution time.
  - Flag any non-deterministic observations.

You MUST:
  - Derive every test case from running the system, not from reading source code.
  - Document the derivation method for each case: "observed", "inferred_from_docs", "derived_from_issue_tracker".
  - Express test cases as plain input/output pairs, not as code.

You MUST NOT:
  - Reference internal implementation details in test case descriptions.
  - Include the probing harness code in your output (store it as a quarantine artifact instead).

Output a JSON object with key "test_cases" containing a list of test case records."""

_DATA_MODEL_PROMPT = """You are the data model sub-agent for a clean room analysis system.

Your task: produce normalised type contracts for all data structures in the public API.

For each type:
  - List its observable fields and their types (normalised, not implementation-specific).
  - Document field nullability, cardinality, and ordering guarantees.
  - Collapse implementation-specific containers to their behavioral equivalent
    (e.g. HashMap → Map with no ordering guarantee; TreeMap → Map with sorted-key iteration).

You MUST NOT:
  - Name the underlying data structure (HashMap, LinkedList, etc.).
  - Document internal fields not observable through the public API.
  - Describe memory layout or serialisation format unless observable.

Output a JSON object with key "type_contracts" containing a list of normalised type descriptors."""

_EDGE_CASE_PROMPT = """You are the edge case sub-agent for a clean room analysis system.

Your task: document error conditions, boundary values, and exceptional behaviors.

For each error condition:
  - What input or state triggers the error.
  - What observable output the error produces (error code, exception type, partial result).
  - Whether the error is recoverable and what the recovery path looks like.
  - Any documented known edge cases from issue trackers (cite issue ID, not internal code).

You MUST NOT:
  - Describe the internal error handling mechanism.
  - Reference internal exception class hierarchies unless they are part of the public API.
  - Speculate about undocumented behavior — flag it as "unobserved, needs probing" instead.

Output a JSON object with key "edge_cases" containing a list of edge case records."""

_PROPERTY_PROMPT = """You are the property specification sub-agent for a clean room analysis system.

Your task: identify and document system-level behavioral invariants.

For each property:
  - State the invariant in clear, implementation-agnostic terms.
  - Classify it: determinism, idempotency, commutativity, monotonicity, error_recoverability, ordering, atomicity.
  - Provide a black-box test scenario that would falsify the invariant.
  - Note scope: does it apply to a single function, a module, or the whole system?

You MUST NOT:
  - Describe how the property is implemented or enforced internally.
  - Reference locking mechanisms, transaction logs, or other implementation constructs.

Output a JSON object with key "properties" containing a list of property records."""


# ---------------------------------------------------------------------------
# Sub-agent node functions
# ---------------------------------------------------------------------------

def build_api_extraction_node(analysis_llm: Any):
    """Return a LangGraph node that extracts the public API surface."""

    def api_extraction_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Enumerate all public symbols and produce normalised type signatures.

        Reads ``quarantine_artifacts`` for source files and public docs.
        Writes ``api_surface`` into the analysis state.
        """
        artifacts = state.get("quarantine_artifacts", [])
        context = _build_analysis_context(artifacts, include_types=["source", "public_docs"])

        response = analysis_llm.invoke([
            SystemMessage(content=_API_EXTRACTION_PROMPT),
            HumanMessage(content=f"Analyse the following and produce the API surface:\n\n{context}"),
        ])

        api_data = _parse_json_output(response.content, "api_surface")
        return {"api_surface": api_data}

    return api_extraction_node


def build_behavior_probing_node(analysis_llm: Any):
    """Return a LangGraph node that generates behavioral test cases."""

    def behavior_probing_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Generate black-box input/output test cases from the running system.

        Uses ``api_surface`` from the previous node to enumerate probe targets.
        The probing harness itself stays in ``quarantine_artifacts``; only
        the observed input/output pairs flow forward (spec §3.6).
        """
        api_surface = state.get("api_surface", [])
        artifacts = state.get("quarantine_artifacts", [])
        running_system_observations = [
            a for a in artifacts if a.get("type") == "observation"
        ]

        context = (
            f"API Surface:\n{api_surface}\n\n"
            f"Running System Observations:\n{running_system_observations}"
        )

        response = analysis_llm.invoke([
            SystemMessage(content=_BEHAVIOR_PROBING_PROMPT),
            HumanMessage(content=context),
        ])

        test_cases = _parse_json_output(response.content, "test_cases")
        return {"test_cases": test_cases}

    return behavior_probing_node


def build_data_model_node(analysis_llm: Any):
    """Return a LangGraph node that produces normalised type contracts."""

    def data_model_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Extract and normalise all data types in the public API.

        Works from ``api_surface``; does not need running system access.
        """
        api_surface = state.get("api_surface", [])

        response = analysis_llm.invoke([
            SystemMessage(content=_DATA_MODEL_PROMPT),
            HumanMessage(content=f"Produce type contracts for:\n\n{api_surface}"),
        ])

        type_contracts = _parse_json_output(response.content, "type_contracts")
        return {"type_contracts": type_contracts}

    return data_model_node


def build_edge_case_node(analysis_llm: Any):
    """Return a LangGraph node that documents error conditions and boundary values."""

    def edge_case_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Document all observable error conditions and boundary behaviors.

        Uses ``test_cases`` from the behavior probing node plus any issue
        tracker artifacts in ``quarantine_artifacts``.
        """
        test_cases = state.get("test_cases", [])
        artifacts = state.get("quarantine_artifacts", [])
        issue_tracker_items = [
            a for a in artifacts if a.get("type") == "issue_tracker"
        ]

        context = (
            f"Observed Test Cases (including failures):\n{test_cases}\n\n"
            f"Issue Tracker Items:\n{issue_tracker_items}"
        )

        response = analysis_llm.invoke([
            SystemMessage(content=_EDGE_CASE_PROMPT),
            HumanMessage(content=context),
        ])

        edge_cases = _parse_json_output(response.content, "edge_cases")
        return {"edge_cases": edge_cases}

    return edge_case_node


def build_property_node(analysis_llm: Any):
    """Return a LangGraph node that identifies system-level invariants."""

    def property_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Document implementation-agnostic behavioral invariants.

        Works from ``type_contracts`` and the full observed behavior so far.
        """
        type_contracts = state.get("type_contracts", [])
        test_cases = state.get("test_cases", [])

        context = (
            f"Type Contracts:\n{type_contracts}\n\n"
            f"Observed Test Cases:\n{test_cases}"
        )

        response = analysis_llm.invoke([
            SystemMessage(content=_PROPERTY_PROMPT),
            HumanMessage(content=context),
        ])

        properties = _parse_json_output(response.content, "properties")
        return {"properties": properties}

    return property_node


def build_spec_assembler_node(analysis_llm: Any):
    """
    Return a LangGraph node that assembles sub-agent outputs into a full spec package.

    The assembler collects ``api_surface``, ``test_cases``, ``type_contracts``,
    ``edge_cases``, and ``properties`` and packages them into the final
    ``pending_spec_document`` that the response guard will evaluate.

    The assembled document is staged as the last item in ``quarantine_artifacts``
    so the response guard can find it.
    """

    def spec_assembler_node(state: dict[str, Any]) -> dict[str, Any]:
        """
        Combine all sub-agent outputs into a single specification package.

        The response guard will evaluate this package before it can be written
        to the spec store.
        """
        assembled = {
            "type": "pending_spec",
            "module": state.get("current_module", "unknown"),
            "content": {
                "api_surface": state.get("api_surface", []),
                "test_cases": state.get("test_cases", []),
                "type_contracts": state.get("type_contracts", []),
                "edge_cases": state.get("edge_cases", []),
                "properties": state.get("properties", []),
            },
        }

        quarantine = list(state.get("quarantine_artifacts", []))
        quarantine.append(assembled)

        return {"quarantine_artifacts": quarantine}

    return spec_assembler_node


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_analysis_context(artifacts: list[dict[str, Any]], include_types: list[str]) -> str:
    """Select quarantine artifacts of the requested types and format them."""
    import json
    selected = [a for a in artifacts if a.get("type") in include_types]
    return json.dumps(selected, indent=2, default=str)


def _parse_json_output(content: str, key: str) -> Any:
    """
    Extract the value at *key* from a JSON response string.

    Falls back to an empty list if parsing fails, to avoid breaking the
    sub-graph on a malformed response.
    """
    import json
    try:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        data = json.loads(cleaned)
        return data.get(key, [])
    except (json.JSONDecodeError, AttributeError):
        return []
