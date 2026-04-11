"""
cleanroom.ingestion
~~~~~~~~~~~~~~~~~~~
Downloads a target project from GitHub and ingests its source files as
quarantine artifacts for the analysis zone.

This is the only place in the pipeline that touches the original system's
source code.  After ingestion, the content is locked inside
``quarantine_artifacts`` and never crosses the guard unreviewed.

Usage
-----
Pass a public GitHub URL to :func:`ingest_github_repo` along with the
module names to analyze.  The result is a list of dicts ready to populate
``CleanRoomState["quarantine_artifacts"]``.
"""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Extensions treated as source code artifacts.
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({".py"})

#: Extensions treated as public documentation artifacts.
_DOCS_EXTENSIONS: frozenset[str] = frozenset({".md", ".rst", ".txt"})

#: Directory names to skip — tests, build outputs, caches, etc.
_SKIP_DIRS: frozenset[str] = frozenset({
    "test", "tests", "testing",
    "__pycache__", ".git", ".github",
    "build", "dist", ".tox", ".mypy_cache", ".ruff_cache",
    "docs", "doc",
    "benchmarks", "benchmark",
    "examples", "example",
    "scripts", "tools",
})

#: Individual file names to skip regardless of extension.
_SKIP_FILES: frozenset[str] = frozenset({
    "setup.py", "setup.cfg", "conftest.py",
    "noxfile.py", "Makefile",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_github_repo(
    github_url: str,
    modules: list[str],
    docs_urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Download a public GitHub repository and convert its contents to quarantine
    artifact dicts.

    Parameters
    ----------
    github_url:
        URL of the GitHub repository, e.g. ``"https://github.com/hukkin/tomli"``.
        Branch-specific URLs (``/tree/branch-name``) are accepted; the branch
        is extracted and used directly.  When the branch cannot be determined
        from the URL, ``main`` is tried first then ``master``.
    modules:
        Logical module names the analysis agents will process.  Source files
        are assigned to the closest matching module by directory or filename.
        If a single module is given, all files are tagged with it.
    docs_urls:
        Optional list of public documentation URLs to fetch and include as
        ``"public_docs"`` artifacts in the quarantine zone.

    Returns
    -------
    list[dict]
        Quarantine artifact dicts, each with keys:
        ``type`` (``"source"`` or ``"public_docs"``), ``path`` or ``url``,
        ``content``, and ``module``.

    Raises
    ------
    ValueError
        If *github_url* cannot be parsed as a GitHub repository URL.
    RuntimeError
        If the repository archive cannot be downloaded (e.g. private repo,
        network error, or branch not found).
    """
    owner, repo, branch = _parse_github_url(github_url)
    print(f"  Downloading {owner}/{repo} (branch: {branch})...")

    zip_data = _download_zip(owner, repo, branch)
    print(f"  Downloaded {len(zip_data) // 1024} KB — extracting...")

    artifacts = _extract_artifacts(zip_data, modules)
    print(f"  Ingested {len(artifacts)} source artifacts.")

    for url in (docs_urls or []):
        print(f"  Fetching documentation: {url}")
        doc = _fetch_doc_url(url, modules[0] if modules else "unknown")
        if doc:
            artifacts.append(doc)

    return artifacts


def fetch_url(url: str) -> str:
    """
    Fetch *url* and return the response body as a string.

    Used by :func:`ingest_github_repo` and by the ``read_public_docs`` tool.

    Raises
    ------
    RuntimeError
        On HTTP errors or network failures.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cleanroom/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error fetching {url}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_github_url(url: str) -> tuple[str, str, str]:
    """
    Return ``(owner, repo, branch)`` from a GitHub URL.

    Accepted forms::

        https://github.com/owner/repo
        https://github.com/owner/repo.git
        https://github.com/owner/repo/tree/branch
        https://github.com/owner/repo/tree/branch/subpath
    """
    url = url.rstrip("/")

    # URL with explicit branch: .../tree/<branch>[/subpath]
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/tree/([^/]+)(?:/.*)?$",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    # Plain repo URL, no branch specified — try main later, master as fallback
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2), "main"

    raise ValueError(
        f"Cannot parse GitHub repository URL: {url!r}\n"
        "Expected: https://github.com/owner/repo"
    )


def _download_zip(owner: str, repo: str, branch: str) -> bytes:
    """
    Download the ZIP archive for *branch*.  Falls back to ``master`` when
    *branch* is ``main`` and the request returns 404.
    """
    candidates = [branch, "master"] if branch == "main" else [branch]
    last_err: Exception | None = None

    for b in candidates:
        url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{b}.zip"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_err = exc
            continue

    raise RuntimeError(
        f"Could not download {owner}/{repo}: {last_err}.  "
        "Ensure the repository is public and the URL is correct."
    )


def _extract_artifacts(zip_data: bytes, modules: list[str]) -> list[dict[str, Any]]:
    """
    Unpack the archive and return one artifact dict per relevant file.

    The top-level ``repo-branch/`` directory in the ZIP is stripped so that
    paths in artifacts are relative to the repository root.
    """
    artifacts: list[dict[str, Any]] = []

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        for name in zf.namelist():
            path = Path(name)
            # Strip the archive root directory (e.g. "tomli-main/").
            parts = path.parts[1:]
            if not parts:
                continue
            relative = Path(*parts)

            if _should_skip(relative):
                continue

            suffix = relative.suffix.lower()
            if suffix in _SOURCE_EXTENSIONS:
                artifact_type = "source"
            elif suffix in _DOCS_EXTENSIONS:
                artifact_type = "public_docs"
            else:
                continue

            try:
                content = zf.read(name).decode("utf-8", errors="replace")
            except Exception:
                continue

            artifacts.append({
                "type": artifact_type,
                "path": str(relative),
                "content": content,
                "module": _assign_module(relative, modules),
            })

    return artifacts


def _should_skip(path: Path) -> bool:
    """Return True if *path* should be excluded from quarantine artifacts."""
    if any(part in _SKIP_DIRS for part in path.parts):
        return True
    if path.name in _SKIP_FILES:
        return True
    return False


def _assign_module(path: Path, modules: list[str]) -> str:
    """
    Assign *path* to the closest matching module name.

    Compares directory components and the file stem against each module name
    case-insensitively.  Falls back to the first module if nothing matches.
    """
    if len(modules) == 1:
        return modules[0]

    parts_lower = {p.lower() for p in path.parts}
    for module in modules:
        if module.lower() in parts_lower:
            return module

    return modules[0]


def _fetch_doc_url(url: str, module: str) -> dict[str, Any] | None:
    """Fetch a documentation URL and return it as a ``public_docs`` artifact."""
    try:
        content = fetch_url(url)
        return {
            "type": "public_docs",
            "url": url,
            "content": content,
            "module": module,
        }
    except RuntimeError as exc:
        print(f"  WARNING: Could not fetch {url} ({exc}) — skipping.")
        return None
