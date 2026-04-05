# Cleanroom

A multi-agent pipeline for legally defensible clean room software reimplementation. The system enforces strict information separation between agents that analyze an original system and agents that write new code, ensuring the new implementation is derived solely from observable behavior specifications.

## What It Does

Given an existing software system, Cleanroom:

1. **Analyzes** the original system in an isolated "quarantine zone" to produce behavioral specifications
2. **Guards** all information crossing zone boundaries — stripping or blocking anything that describes *how* the original works rather than *what* it does
3. **Implements** new code from scratch in a separate "implementation zone" where agents have no access to the original source
4. **Verifies** the new implementation against the behavioral specs
5. **Audits** the entire process with an immutable log suitable for legal review

## Requirements

- Python 3.10+
- Anthropic API keys (three separate keys are recommended for full zone isolation)

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and fill in your API keys
```

## Configuration

Copy `.env.example` to `.env` and set the following:

| Variable | Description |
|---|---|
| `QUARANTINE_ZONE_API_KEY` | API key for analysis agents |
| `GUARD_API_KEY` | API key for guard agents |
| `IMPL_ZONE_API_KEY` | API key for implementation agent |
| `ANALYSIS_MODEL` | Claude model for analysis (default: `claude-sonnet-4-20250514`) |
| `GUARD_MODEL` | Claude model for guards (default: `claude-haiku-4-5-20251001`) |
| `IMPL_MODEL` | Claude model for implementation (default: `claude-sonnet-4-20250514`) |
| `SPEC_STORE_BACKEND` | `memory` (development) or `s3` (production) |

Using three separate API keys is the recommended configuration for strong zone isolation — it makes cross-zone information leakage detectable at the infrastructure level.

## Usage

```bash
# Analyze and reimplement a single module
python main.py --project-id my-project --module auth

# Reimplement multiple modules
python main.py --project-id my-project --modules auth payments users

# Reimplement in a specific language
python main.py --project-id my-project --module auth --language typescript

# Resume an interrupted run
python main.py --project-id my-project --module auth --resume

# Dry run (validate config without running)
python main.py --project-id my-project --module auth --dry-run
```

The pipeline prints audit metrics when it finishes:

- **Spec gap rate** — clarification requests per spec document (high = underspecification)
- **Guard rejection rate** — fraction of requests blocked by the request guard
- **Flag count** — decisions escalated to human review
- **Verification failure rate** — fraction of modules that failed certification

## Architecture

```
Quarantine Zone                   Implementation Zone
─────────────────────────────     ────────────────────────────
analysis_coordinator              implementation_agent
  └── analysis_subgraph               └── tools (spec store, tests)
       ├── api_extraction_agent    
       ├── behavior_probing_agent  
       ├── data_model_agent        
       ├── edge_case_agent         
       └── property_agent          
                                   verification_agent

            ┌─── response_guard ───┐
            └─── request_guard  ───┘

                   spec_store  (shared, guard-gated)
                   audit_log   (append-only)
                   human_review (escalation queue)
```

All information flowing between zones passes through a guard agent. The guard blocks anything describing internal structure, algorithms, or implementation choices.

## Project Structure

```
cleanroom/
├── config.py            # Zone-isolated LLM instances
├── state.py             # Shared state types and zone isolation helpers
├── spec_store.py        # Immutable, versioned, signed spec document store
├── audit.py             # Append-only audit logger
├── tools.py             # LangChain tools available to agents
├── agents/
│   ├── analysis/
│   │   ├── coordinator.py    # Quarantine zone entry point
│   │   └── sub_agents.py     # Five specialized analysis agents
│   ├── guard/
│   │   ├── request_guard.py  # Firewall: implementation → analysis
│   │   ├── response_guard.py # Firewall: analysis → implementation
│   │   └── prompts.py        # Guard classification prompts
│   ├── implementation.py     # Writes new code from specs
│   ├── verification.py       # Certifies completed modules
│   └── human_review.py       # Escalation queue handler
└── graphs/
    ├── main_graph.py         # Top-level LangGraph orchestration
    └── analysis_subgraph.py  # Internal analysis pipeline
```

## Status

Core pipeline, guard logic, spec store, and audit trail are fully implemented. The following are stubs awaiting integration:

- `probe_running_system()` — executing probes against a sandboxed original system
- `run_behavioral_tests()` — running generated test cases against new implementations
- Human review dashboard (currently uses stdin)
- S3/DynamoDB spec store backend

## License

See `clean room spec.docx` for architecture specification and legal framework.
