"""
main.py — Clean Room Implementation System entry point
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Usage:
    python main.py --project-id my-project --module auth_module

This script:
  1. Validates that all required environment variables are set.
  2. Builds the three zone-isolated LLM instances.
  3. Initialises the spec store backend.
  4. Initialises the audit logger.
  5. Injects the spec query and clarification tools.
  6. Compiles the main LangGraph graph.
  7. Invokes the graph with the initial state.
  8. Prints the audit metrics on exit.

See .env.example for required environment variables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cleanroom.audit import build_audit_logger
from cleanroom.config import build_llm_instances
from cleanroom.graphs.main_graph import build_main_graph
from cleanroom.ingestion import ingest_github_repo
from cleanroom.spec_store import build_spec_store
from cleanroom.state import CleanRoomState
from cleanroom.tools import bind_tools_to_store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean Room Implementation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Point at a GitHub repo and reimplement a single module
  python main.py --target-url https://github.com/hukkin/tomli \\
      --docs-url https://toml.io/en/v1.0.0 \\
      --module tomli

  # Multiple modules
  python main.py --target-url https://github.com/owner/repo \\
      --modules auth session user

  # Resume a run
  python main.py --resume
""",
    )
    parser.add_argument(
        "--module",
        help="Single module to analyse and implement.",
    )
    parser.add_argument(
        "--modules",
        nargs="+",
        help="Multiple modules to process in dependency order.",
    )
    parser.add_argument(
        "--language",
        default="Python",
        help="Target language for the implementation (default: Python).",
    )
    parser.add_argument(
        "--target-url",
        help=(
            "GitHub URL of the project to reimplement, e.g. "
            "https://github.com/hukkin/tomli  "
            "The repository is downloaded automatically."
        ),
    )
    parser.add_argument(
        "--docs-url",
        nargs="+",
        metavar="URL",
        help=(
            "One or more public documentation URLs to include in the quarantine "
            "zone (e.g. the official spec page).  Fetched at startup."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        metavar="DIR",
        help="Directory to write generated implementation files (default: ./output).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a project by loading existing spec store state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the graph and validate config without executing.",
    )
    return parser.parse_args()


def build_initial_state(args: argparse.Namespace) -> CleanRoomState:
    """Construct the initial pipeline state from CLI arguments."""
    modules = args.modules or ([args.module] if args.module else [])
    if not modules:
        print("ERROR: Specify at least one module with --module or --modules.", file=sys.stderr)
        sys.exit(1)

    quarantine_artifacts: list = []

    if args.target_url:
        print(f"\nIngesting source from: {args.target_url}")
        quarantine_artifacts = ingest_github_repo(
            github_url=args.target_url,
            modules=modules,
            docs_urls=getattr(args, "docs_url", None),
        )
        print(f"  Total quarantine artifacts: {len(quarantine_artifacts)}")
    elif getattr(args, "docs_url", None):
        # docs URLs provided without a source repo — fetch docs only.
        from cleanroom.ingestion import _fetch_doc_url
        for url in args.docs_url:
            doc = _fetch_doc_url(url, modules[0])
            if doc:
                quarantine_artifacts.append(doc)

    return CleanRoomState(
        target_language=args.language,
        quarantine_artifacts=quarantine_artifacts,
        analysis_queue=modules,
        spec_store_documents=[],
        implementation_queue=modules,
        completed_modules=[],
        spec_gap_requests=[],
        pending_guard_decisions=[],
        audit_log=[],
        human_review_queue=[],
        current_module=None,
        error=None,
        generated_files=[],
    )


def _write_output(state: CleanRoomState, output_dir: str) -> None:
    """
    Write generated implementation files from state to disk.

    Files land in *output_dir*, preserving any subdirectory structure present
    in their filenames.  Existing files are overwritten so resumed runs
    converge correctly.
    """
    files = state.get("generated_files", [])
    if not files:
        print("\nNo generated files to write.")
        return

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    print(f"\nWriting {len(files)} generated file(s) to {root}/")
    for entry in files:
        dest = root / entry["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(entry["content"], encoding="utf-8")
        print(f"  ✓ {dest}")


def main() -> None:
    args = parse_args()

    print(f"\nClean Room Implementation System")
    print(f"Target lang:   {args.language}")
    print(f"Modules:       {args.modules or [args.module]}")
    print()

    # --- Step 1: Build infrastructure ---
    print("Initialising LLM instances...")
    try:
        llm_instances = build_llm_instances()
    except EnvironmentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print("  ✓ analysis LLM  (QUARANTINE_ZONE_API_KEY)")
    print("  ✓ guard LLM     (GUARD_API_KEY)")
    print("  ✓ impl LLM      (IMPL_ZONE_API_KEY)")
    print("  (All three may point to the same key — see README for details.)")

    print("\nInitialising spec store...")
    spec_store = build_spec_store()
    print(f"  ✓ backend: {spec_store.__class__.__name__}")

    print("\nInitialising audit logger...")
    audit_logger = build_audit_logger()
    print("  ✓ AuditLogger ready")

    # --- Step 2: Inject tools ---
    bind_tools_to_store(spec_store, audit_logger)
    print("  ✓ Tools bound to spec store")

    # --- Step 3: Compile graph ---
    print("\nCompiling LangGraph pipeline...")
    graph = build_main_graph(llm_instances, spec_store, audit_logger)
    print("  ✓ Graph compiled")

    if args.dry_run:
        print("\nDry run complete — graph compiled successfully, no execution.")
        return

    # --- Step 4: Build initial state and run ---
    initial_state = build_initial_state(args)

    print(f"\nStarting pipeline...")
    print("=" * 70)

    try:
        final_state = graph.invoke(initial_state)
    except KeyboardInterrupt:
        print("\nPipeline interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nPipeline error: {e}", file=sys.stderr)
        raise

    # --- Step 5: Print summary ---
    print("\n" + "=" * 70)
    print("PIPELINE COMPLETE")
    print("=" * 70)

    completed = final_state.get("completed_modules", [])
    print(f"\nCompleted modules ({len(completed)}):")
    for m in completed:
        print(f"  ✓ {m}")

    if final_state.get("error"):
        print(f"\nFinal error: {final_state['error']}")

    _write_output(final_state, args.output_dir)

    metrics = audit_logger.metrics_summary()
    print("\nPipeline health metrics:")
    print(json.dumps(metrics, indent=2))

    if metrics["flag_count"] > 0:
        print(
            f"\nWARNING: {metrics['flag_count']} FLAG event(s) detected.  "
            "Review the audit log immediately — this may indicate a clean room violation."
        )


if __name__ == "__main__":
    main()
