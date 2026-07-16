from __future__ import annotations

from pathlib import Path
import sys


RERANKER_DIR = Path(__file__).resolve().parents[1] / "docker" / "reranker"
sys.path.insert(0, str(RERANKER_DIR))

from model_paths import huggingface_repo_cache_dir, resolve_huggingface_snapshot


def test_resolves_main_ref_to_existing_snapshot(tmp_path):
    repo = huggingface_repo_cache_dir(str(tmp_path), "BAAI/bge-reranker-v2-m3")
    snapshot = repo / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123\n", encoding="utf-8")

    assert resolve_huggingface_snapshot(str(tmp_path), "BAAI/bge-reranker-v2-m3") == str(snapshot)


def test_missing_or_stale_ref_returns_empty(tmp_path):
    repo = huggingface_repo_cache_dir(str(tmp_path), "BAAI/bge-reranker-v2-m3")
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("missing", encoding="utf-8")

    assert resolve_huggingface_snapshot(str(tmp_path), "BAAI/bge-reranker-v2-m3") == ""


def test_rejects_ref_that_escapes_snapshot_directory(tmp_path):
    repo = huggingface_repo_cache_dir(str(tmp_path), "BAAI/bge-reranker-v2-m3")
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("../outside", encoding="utf-8")

    assert resolve_huggingface_snapshot(str(tmp_path), "BAAI/bge-reranker-v2-m3") == ""
