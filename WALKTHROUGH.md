# Cleanroom — Code Walkthrough

This document explains the codebase for someone who knows Python but hasn't worked with LangChain or LangGraph before. It covers the key concepts you'll need, then walks through how the project uses them.

---

## Background: LangChain and LangGraph

### LangChain in 60 seconds

LangChain is a library for building applications with large language models. At its core it gives you two things:

1. **A standard interface to LLMs.** `ChatAnthropic`, `ChatOpenAI`, etc. all expose the same `.invoke()` method. You pass in a list of messages and get a response object back.
2. **Tools.** You can decorate a Python function with `@tool` and the LLM can call it during a conversation. The framework handles serializing the call and parsing the result.

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool

llm = ChatAnthropic(model="claude-sonnet-4-20250514", api_key="...")

response = llm.invoke([
    SystemMessage(content="You are a helpful assistant."),
    HumanMessage(content="What is 2+2?"),
])
print(response.content)  # "4"
```

When you give an LLM access to tools, you call `.bind_tools(tools)` first. The LLM can then respond with a tool call instead of text. A `ToolNode` runs those tool calls and returns the results as new messages.

### LangGraph in 60 seconds

LangGraph is a graph execution framework built on LangChain. You define:

- **State** — a typed dict that flows through the graph and accumulates results
- **Nodes** — Python functions (or LLM calls) that read state and return partial updates
- **Edges** — fixed connections between nodes
- **Conditional edges** — routing functions that examine state and return the name of the next node

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

class MyState(TypedDict):
    count: int

def increment(state: MyState) -> dict:
    return {"count": state["count"] + 1}

def route(state: MyState) -> str:
    return "increment" if state["count"] < 5 else END

graph = StateGraph(MyState)
graph.add_node("increment", increment)
graph.add_edge(START, "increment")
graph.add_conditional_edges("increment", route)

app = graph.compile()
result = app.invoke({"count": 0})
# result["count"] == 5
```

That's almost all the LangGraph you need to read this project.

---

## What the Project Does

Cleanroom automates *clean room reimplementation* — a legal technique for creating a new software component that is provably independent of an original implementation. The classic example is writing a new Java runtime from scratch using only the published specification, so there's no copyright contamination from Sun's source code.

The pipeline enforces strict information separation:

- **Quarantine zone** — agents that can read the original system. They produce specifications.
- **Implementation zone** — agents that write new code. They can only read approved specifications.
- **Guard layer** — firewall agents that sit between the zones and block anything that describes *how* the original works rather than *what* it does.

The goal is an audit trail demonstrating that the new code could have been written by someone with no access to the original.

---

## Entry Point: `main.py`

Start here. `main()` does five things:

1. Parses CLI arguments (`--project-id`, `--module`, `--language`, `--resume`, `--dry-run`)
2. Calls `build_llm_instances()` to create three isolated LLM clients
3. Calls `build_spec_store()` to create the shared document store
4. Calls `build_audit_logger()` to set up the append-only audit log
5. Builds and runs the LangGraph pipeline via `build_main_graph()`

The three LLM instances use separate API keys (`QUARANTINE_ZONE_API_KEY`, `GUARD_API_KEY`, `IMPL_ZONE_API_KEY`). Using separate keys means that if an implementation-zone agent somehow called a quarantine-zone LLM, it would fail authentication — zone isolation is enforced at the infrastructure level, not just in code.

---

## State: `cleanroom/state.py`

Every LangGraph graph has a state type. This project's state is `CleanRoomState` — a large `TypedDict` holding everything the pipeline needs. It's annotated with zone comments to document which nodes are allowed to read which fields.

Key fields:

| Field | Zone | Purpose |
|---|---|---|
| `quarantine_artifacts` | Quarantine | Original source, docs, observations |
| `pending_spec` | Guard | Analysis output waiting for guard approval |
| `approved_specs` | Both | Guard-approved specs |
| `implementation_queue` | Impl | Modules waiting to be implemented |
| `spec_gap_requests` | Impl | Clarification requests from impl agent |
| `verification_certificates` | Both | Signed completion records |
| `audit_log` | Both | Immutable audit entries |
| `escalation_queue` | Both | Items needing human review |

The `build_isolated_context()` function enforces zone isolation in code — it takes the full state and returns a filtered copy containing only the fields appropriate for a given zone. Analysis-zone nodes never see implementation queue items; implementation-zone nodes never see quarantine artifacts.

Important enumerations:

- `GuardClassification` — the possible outcomes of a guard evaluation (APPROVED, REJECTED, REDACT, FLAG, etc.)
- `SpecDocumentType` — the kinds of spec documents the store holds (BEHAVIORAL_SPEC, TEST_CASE, TYPE_CONTRACT, etc.)

---

## The Spec Store: `cleanroom/spec_store.py`

The spec store is the legal boundary between the two zones. It has four invariants:

1. **Append-only.** You cannot overwrite a document. Every version is retained.
2. **Guard-gated.** Every write must include an APPROVED guard decision ID. Writing without one raises an exception.
3. **Access-controlled.** Implementation agents cannot read any document whose ID starts with `quarantine:`. If they try, they get back `None`.
4. **Cryptographically signed.** Every document stores a SHA-256 digest of its content.

```python
# Writing a document (simplified)
store.write(
    doc_id="behavioral_spec:auth:v1",
    content="The login function accepts ...",
    guard_decision_id="gd-abc123",  # must be APPROVED
)

# Reading (role-based)
doc = store.read("behavioral_spec:auth:v1", role="implementation")
```

The `MemorySpecStore` class is a plain dict. The `S3SpecStore` is a stub for a production backend.

---

## The Audit Log: `cleanroom/audit.py`

`AuditLogger` is an append-only list of `AuditEntry` records. Every significant event — guard decisions, spec writes, verification outcomes — is logged unconditionally, including rejected or flagged events.

It exposes a few computed metrics:

- `spec_gap_rate()` — clarification requests per spec (high means specs are incomplete)
- `guard_rejection_rate()` — fraction of requests the guard blocked
- `flag_count()` — events escalated for human investigation
- `verification_failure_rate()` — fraction of modules that didn't pass certification

These metrics print at the end of a pipeline run and are the primary signal for how well the clean room process is working.

---

## Tools: `cleanroom/tools.py`

LangChain tools are Python functions decorated with `@tool`. They're the only way agents interact with external state. This file defines two sets of tools, one per zone.

**Implementation agent tools:**

- `query_spec_store(module, doc_type)` — fetch a spec document. This is the only way the implementation agent reads specs.
- `submit_clarification(module, function, ...)` — the *only* sanctioned channel for the implementation agent to request information. This creates a `SpecGapRequest` that goes to the request guard for evaluation before it reaches the analysis zone.
- `run_behavioral_tests(module)` — run the generated test suite against the new code (stub).

**Analysis agent tools:**

- `probe_running_system(command, input_data)` — execute a probe against a sandboxed original system (stub).
- `read_public_docs(source, section)` — retrieve public documentation (stub).

`bind_tools_to_store()` is called at graph build time to inject the spec store and audit logger into tool closures via a factory pattern. This avoids passing them through state.

---

## The Main Graph: `cleanroom/graphs/main_graph.py`

`build_main_graph()` assembles the full pipeline. Here's the topology with routing logic:

```
START → orchestrator
         │
         ├─[modules remain]→ analysis_coordinator
         │                        │
         │                   (analysis done)
         │                        │
         │                   response_guard
         │                    /    |    \
         │               APPROVED REWRITE REJECT
         │                /         |       \
         │    spec_store_writer  analysis  human_review
         │           │           _coordinator
         │           └──────────────────────┐
         │                                  │
         ├─[specs ready]→ implementation_agent
         │                     │
         │                [has tool calls]
         │                     │
         │              implementation_tools
         │                     │
         │              [gap submitted]
         │                     │
         │              request_guard
         │               /   |   |  \
         │          APPROVED REDIRECT REJECTED REFRAME FLAG
         │             /      |       \          |      \
         │    analysis  impl_agent  impl_agent impl_agent human_review
         │    _coordinator
         │
         │           [implementation complete]
         │                     │
         │              verification_agent
         │               /           \
         │          certified        rejected
         │             /                \
         │         orchestrator    implementation_agent
         │
         └─[no modules remain]→ END
```

Most of the routing lives in small functions like `route_from_orchestrator()`, `route_request()`, `route_response()`, and `route_verification()`. Each reads a field from state and returns a node name string (or `END`).

The `orchestrator` node is unusual: it contains no LLM. It just pops the next module off the queue and sets `current_module` in state. When the queue is empty it returns `END`.

---

## The Analysis Subgraph: `cleanroom/graphs/analysis_subgraph.py`

The analysis pipeline is its own compiled subgraph. It runs five specialized agents in sequence:

```
api_extraction → behavior_probing → edge_case → assembler
             ↘ data_model → property ↗
```

The two branches (`behavior_probing`/`edge_case` and `data_model`/`property`) run sequentially within the subgraph but represent separate analytical concerns. All output converges at `assembler`, which combines everything into a single `pending_spec` document.

The subgraph has its own internal state type `AnalysisState` with quarantine-only fields. The assembled spec gets written back to the parent `CleanRoomState.pending_spec`, where it waits for the response guard.

---

## The Five Analysis Agents: `cleanroom/agents/analysis/sub_agents.py`

Each agent is a class whose `run()` method takes `AnalysisState` and returns a partial state update.

| Agent | Input | Output | Key constraint |
|---|---|---|---|
| `APIExtractionAgent` | Source, docs | `api_surface` (public signatures) | Normalize types to behavioral equivalents — `LinkedList` becomes "ordered List" |
| `BehaviorProbingAgent` | `api_surface`, observations | `test_cases` (I/O pairs) | Generate tests from observed behavior, not source |
| `DataModelAgent` | `api_surface` | `type_contracts` | Collapse implementation containers to abstract types |
| `EdgeCaseAgent` | `test_cases`, issue tracker | `edge_cases` | Document error conditions and recoverability |
| `PropertyAgent` | `type_contracts`, `test_cases` | `properties` (invariants) | Classify: determinism, idempotency, atomicity, etc. |

Each agent's system prompt has the same core constraint: *"Ask yourself — could this specification have been written by someone with only black-box access to the system?"* If not, the agent is leaking implementation details.

---

## The Guards: `cleanroom/agents/guard/`

The guard layer is the most security-critical part of the system. Two guard agents sit at the zone boundary:

### Request Guard (`request_guard.py`)

Evaluates clarification requests from the implementation agent before they reach the analysis zone. The concern is *covert channels* — an implementation agent could theoretically extract source code by asking leading questions ("Does the function use a red-black tree internally?").

Classifications:

| Classification | Meaning | Action |
|---|---|---|
| `APPROVED` | Behavioral question, not yet answered | Forward to analysis |
| `REDIRECT` | Already answered in spec store | Return spec ID |
| `REJECTED` | Asks "how" not "what" | Return with rejection message |
| `REFRAME` | Legitimate intent, unsafe phrasing | Return with rephrasing guidance |
| `FLAG` | Suggests knowledge of internals | Escalate to human review |

The `FLAG` classification is the most serious. It suggests the implementation agent has somehow acquired knowledge of the original system's internals and is trying to confirm it.

### Response Guard (`response_guard.py`)

Evaluates analysis output before it enters the spec store. The concern is that analysis agents, having access to the original source, will write specs that describe internal structure rather than observable behavior.

Classifications:

| Classification | Meaning | Action |
|---|---|---|
| `APPROVED` | Pure behavioral content | Write to spec store |
| `REDACT` | Mostly clean, a few leaking phrases | Strip phrases, then APPROVED |
| `REWRITE` | Structure mirrors original code | Return to analysis for rephrasing |
| `REJECT` | Fundamentally unsuitable | Escalate to human review |

The `REDACT` path is automatic: `_apply_redactions()` does a simple string replacement to strip the offending phrases, then the guard re-evaluates the result as approved.

### Guard Prompts (`prompts.py`)

Both guards require JSON output with a specific schema (`classification`, `reasoning`, `redactions` or `guidance`). The prompts include explicit few-shot examples for hard edge cases — for instance, distinguishing a legitimate behavioral question ("What error does it return for invalid input?") from a covert channel attempt ("Does the error message include the stack frame from `InternalParser`?").

---

## Implementation Agent: `cleanroom/agents/implementation.py`

The implementation agent is an LLM with tool access. Its workflow is enforced through the system prompt:

1. Query the spec store for all relevant documents
2. Identify anything undefined or ambiguous
3. Submit clarification requests for gaps (never assume)
4. Write the implementation from spec only
5. Write unit tests
6. Run behavioral tests
7. Iterate on spec failures (not intuition)

Every implementation decision must be traceable to a spec reference — the agent maintains a decision log that the verification agent checks.

The routing after implementation runs is:
- Tool calls pending → `implementation_tools` node
- Spec gap submitted → `request_guard`
- Implementation complete → `verification_agent`

---

## Verification Agent: `cleanroom/agents/verification.py`

Verification is mostly deterministic:

1. Load test cases and property specs from the spec store
2. Execute test cases against the new code (dynamic import + isolated execution)
3. Run property checkers (e.g., verify idempotency by calling the function twice)
4. Check spec coverage — every spec requirement has a corresponding test
5. Check the decision log — every implementation choice cites a spec reference
6. Issue a signed `VerificationCertificate` or send the module back for fixes

A certified module produces a certificate with:
- Test pass/fail counts
- Properties verified
- Spec coverage percentage
- Decision log completeness flag

The certificate is stored in the spec store and in state. When all modules are certified, the orchestrator routes to `END`.

---

## Human Review: `cleanroom/agents/human_review.py`

Human review is the escalation mechanism for `FLAG` and `REJECT` guard decisions. The queue is FIFO. For each item, the handler prints the escalation details and reads a ruling from stdin: `approve`, `reject`, or `reframe`.

The ruling determines the next edge:
- `approve` → the document goes to `spec_store_writer`
- `reject` or `reframe` → the implementation agent continues without that information

The stub prints to stdout and reads from stdin. Production integration points (Slack, web dashboard, GitHub issues) are noted in comments.

---

## Configuration: `cleanroom/config.py`

`LLMInstances` is a frozen dataclass holding the three `ChatAnthropic` clients — one per zone. `build_llm_instances()` reads environment variables and constructs them. `build_spec_store()` returns either `MemorySpecStore` (default) or `S3SpecStore` based on `SPEC_STORE_BACKEND`.

The frozen dataclass pattern means that once the instances are created, they can't be accidentally modified. They're passed by value into each graph node factory function.

---

## Reading Order

If you want to understand the system from the outside in:

1. `main.py` — what gets called and in what order
2. `cleanroom/state.py` — what data flows through the system
3. `cleanroom/graphs/main_graph.py` — how the nodes connect
4. `cleanroom/agents/guard/prompts.py` — what the classification rules actually are
5. `cleanroom/spec_store.py` — what the legal boundary looks like in code
6. `cleanroom/agents/analysis/sub_agents.py` — how specs are generated
7. `cleanroom/agents/implementation.py` — how new code is written
8. `cleanroom/agents/verification.py` — how completion is certified
