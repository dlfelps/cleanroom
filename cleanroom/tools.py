"""
cleanroom.tools
~~~~~~~~~~~~~~~
LangChain tools available to agent nodes.

Each tool is a plain Python function decorated with ``@tool`` so LangGraph's
``ToolNode`` can execute it.  The implementation agent receives a restricted
subset (spec §5.2); the analysis agent has a different subset.  The guard and
verification agents do not receive any LangChain tools — they operate on
structured inputs only.

Tool-to-agent mapping (spec §5.2, §3.2):
  Implementation agent: spec_query, submit_clarification, run_behavioral_tests
  Analysis agent:       probe_running_system, read_public_docs

The ``spec_store`` and ``audit_logger`` dependencies are injected at graph
build time via :func:`bind_tools_to_store`.  Individual tools do not import
these as module-level globals so they can be tested with mock stores.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

# These are populated by bind_tools_to_store() before the graph is compiled.
_spec_store: Any = None
_audit_logger: Any = None


def bind_tools_to_store(spec_store: Any, audit_logger: Any) -> None:
    """
    Inject the spec store and audit logger into this module's tool closures.

    Call this once during graph setup, after the store and logger are
    constructed, and before any node calls a tool.
    """
    global _spec_store, _audit_logger
    _spec_store = spec_store
    _audit_logger = audit_logger


# ---------------------------------------------------------------------------
# Implementation agent tools (spec §5.2)
# ---------------------------------------------------------------------------

@tool
def query_spec_store(module: str, doc_type: str | None = None) -> str:
    """
    Retrieve behavioral spec documents for a module from the spec store.

    The implementation agent should call this tool before writing any code.
    It returns all documents for the requested module, optionally filtered by
    document type.

    Parameters
    ----------
    module:
        The module name to look up (e.g. ``"auth_module"``).
    doc_type:
        Optional filter, e.g. ``"behavioral_spec"``, ``"type_contract"``,
        ``"test_case"``.  If omitted, all document types are returned.

    Returns
    -------
    str
        JSON-formatted list of spec documents for the module.
    """
    if _spec_store is None:
        return "ERROR: spec store has not been initialised."

    import json
    doc_ids = _spec_store.list_documents(module=module)
    docs = []
    for doc_id in doc_ids:
        doc = _spec_store.read(doc_id, requester_role="implementation_agent")
        if doc_type is None or doc["doc_type"] == doc_type:
            docs.append(doc)
    return json.dumps(docs, indent=2, default=str)


@tool
def submit_clarification(
    module: str,
    function: str,
    input_conditions: str,
    ambiguity: str,
    attempted_specs: list[str],
) -> str:
    """
    Submit a spec gap request when behavior is undefined in the spec store.

    This is the ONLY mechanism the implementation agent may use to request
    new information.  The request is routed through the request guard before
    reaching the analysis team.  The agent MUST NOT make assumptions — it must
    wait for a response (spec §5.4).

    Parameters
    ----------
    module:
        Name of the module that triggered the ambiguity.
    function:
        Specific function name.
    input_conditions:
        Description of the input state that triggers the undefined behavior.
    ambiguity:
        What exactly is undefined or contradictory in the current specs.
    attempted_specs:
        List of spec document IDs already consulted before raising this gap.

    Returns
    -------
    str
        A JSON object containing the submitted ``request_id``.  The agent
        should record this ID and wait for the corresponding spec gap response
        to appear in the spec store.
    """
    import json
    import uuid

    request: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "module": module,
        "function": function,
        "input_conditions": input_conditions,
        "ambiguity": ambiguity,
        "attempted_specs": attempted_specs,
        "what_not_how": True,  # self-attestation: question is behavioral
    }

    if _audit_logger:
        _audit_logger.log(
            event_type="spec_gap_submitted",
            agent_id="implementation_agent",
            details={"module": module, "function": function, "request_id": request["request_id"]},
        )

    return json.dumps({"status": "submitted", "request_id": request["request_id"]})


@tool
def run_behavioral_tests(module: str) -> str:
    """
    Execute behavioral test cases from the spec store against the implementation.

    Test cases are loaded from the spec store (``doc_type="test_case"``).
    The implementation agent must iterate against the spec only on failure —
    it must never consult the original system (spec §5.5 step 8-9).

    Parameters
    ----------
    module:
        The module to run tests for.

    Returns
    -------
    str
        A JSON summary of pass/fail counts and failing test case IDs.

    TODO
    ----
    * Load test cases from spec store by module.
    * Dynamically import the implementation under test.
    * Execute each test case in isolation with a timeout.
    * Collect pass/fail results and return structured output.
    """
    # TODO: implement test execution
    return '{"status": "not_implemented", "message": "run_behavioral_tests requires a test runner harness."}'


# ---------------------------------------------------------------------------
# Analysis agent tools (spec §3.2)
# ---------------------------------------------------------------------------

@tool
def probe_running_system(command: str, input_data: dict[str, Any]) -> str:
    """
    Execute a probe against the sandboxed running system and record the output.

    The analysis agent uses this to generate black-box behavioral test cases.
    The probing harness is stored in the quarantine zone as an artifact; only
    the resulting input/output pairs cross the firewall (spec §3.6).

    Parameters
    ----------
    command:
        The API endpoint or function call to invoke, e.g. ``"POST /auth/login"``.
    input_data:
        The request payload or function arguments.

    Returns
    -------
    str
        JSON-formatted observation: return value, exception type, side effects,
        execution time.  Non-deterministic observations are flagged.

    TODO
    ----
    * Spin up or connect to the sandboxed environment.
    * Execute the command with the given inputs.
    * Record and return the structured observation.
    * Flag if the same inputs produce different outputs across runs.
    """
    # TODO: implement sandbox probe
    return '{"status": "not_implemented", "message": "probe_running_system requires a sandbox environment."}'


@tool
def read_public_docs(source: str, section: str | None = None) -> str:
    """
    Retrieve public documentation for the target system.

    Permitted sources: README files, API reference pages, changelogs, and
    public specification documents.  Internal design documents, source code
    comments, and private wikis are not permitted inputs (spec §3.2).

    Parameters
    ----------
    source:
        A file path or URL identifying the documentation source.
    section:
        Optional section heading to extract, to avoid loading entire documents.

    Returns
    -------
    str
        The raw text of the requested documentation.

    TODO
    ----
    * Validate that *source* refers to a permitted public documentation source.
    * Fetch or read the content.
    * Extract *section* if provided.
    """
    # TODO: implement documentation fetcher
    return '{"status": "not_implemented", "message": "read_public_docs requires a document fetcher."}'


# ---------------------------------------------------------------------------
# Tool sets per agent role
# ---------------------------------------------------------------------------

#: Tools available to the implementation agent (spec §5.2).
IMPLEMENTATION_AGENT_TOOLS = [
    query_spec_store,
    submit_clarification,
    run_behavioral_tests,
]

#: Tools available to the analysis agent (spec §3.2).
ANALYSIS_AGENT_TOOLS = [
    probe_running_system,
    read_public_docs,
]
