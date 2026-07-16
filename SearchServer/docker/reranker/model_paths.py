"""Resolve locally cached reranker snapshots without pinning a commit hash."""

from __future__ import annotations

from pathlib import Path


def huggingface_repo_cache_dir(hf_home: str, model_name: str) -> Path:
    """Return the Hugging Face cache directory for a model repository."""
    repo_key = model_name.strip().replace("/", "--")
    return Path(hf_home).expanduser() / "hub" / f"models--{repo_key}"


def resolve_huggingface_snapshot(hf_home: str, model_name: str, revision: str = "main") -> str:
    """Resolve ``refs/<revision>`` to an existing local snapshot directory.

    An empty string means the cache is absent or inconsistent, in which case
    the caller can safely fall back to the normal Hugging Face loader.
    """
    repo_dir = huggingface_repo_cache_dir(hf_home, model_name)
    ref_path = repo_dir / "refs" / revision
    try:
        commit = ref_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    if not commit or "/" in commit or "\\" in commit or commit in {".", ".."}:
        return ""
    snapshot = repo_dir / "snapshots" / commit
    return str(snapshot) if snapshot.is_dir() else ""
