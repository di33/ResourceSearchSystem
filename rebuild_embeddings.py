"""
Rebuild all server embeddings and refresh Milvus vectors.

Usage:
  python rebuild_embeddings.py
  python rebuild_embeddings.py --dry-run
  python rebuild_embeddings.py --limit 100
  python rebuild_embeddings.py --no-recreate-collection
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import struct
import sys
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Ensure imports work when running from repo root.
_ROOT = Path(__file__).resolve().parent
_SERVER_DIR = _ROOT / "Server"
_CLIENT_SCRIPTS = _ROOT / "Client" / "Scripts"
_SERVER_SCRIPTS = _ROOT / "Server" / "Scripts"
for _p in (str(_SERVER_DIR), str(_CLIENT_SCRIPTS), str(_SERVER_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import settings  # noqa: E402
from app.deps import async_session_factory, close_milvus, engine, get_milvus  # noqa: E402
from app.models.tables import ResourceDescription, ResourceEmbedding, ResourceTask  # noqa: E402
from app.services.embedding_client import generate_embedding, get_model_version  # noqa: E402
from app.services.milvus_search_client import ensure_collection  # noqa: E402


def _compute_checksum(vector: list[float]) -> str:
    raw = struct.pack(f"{len(vector)}f", *vector)
    return hashlib.md5(raw).hexdigest()


def _pick_description(task: ResourceTask) -> str:
    """Pick embedding text from latest description.

    IMPORTANT: only main_content is used for embedding regeneration.
    """
    if not task.descriptions:
        return ""
    latest = sorted(task.descriptions, key=lambda d: d.id, reverse=True)[0]
    return (latest.main_content or "").strip()


async def _main(args) -> int:
    t0 = time.time()
    model_version = get_model_version()
    print("=" * 68)
    print("  Rebuild Embeddings")
    print(f"  provider={settings.embedding_provider}")
    print(f"  model={settings.embedding_model}")
    print(f"  dim={settings.embedding_dimension}")
    print(f"  milvus_collection={settings.milvus_collection}")
    print(f"  dry_run={args.dry_run}")
    print("=" * 68)

    milvus = get_milvus()
    if args.recreate_collection and not args.dry_run:
        if milvus.has_collection(settings.milvus_collection):
            print(f"[Milvus] dropping collection: {settings.milvus_collection}")
            milvus.drop_collection(settings.milvus_collection)
        print(f"[Milvus] creating collection: {settings.milvus_collection}")
        ensure_collection(milvus)
    elif not milvus.has_collection(settings.milvus_collection) and not args.dry_run:
        ensure_collection(milvus)

    ok = 0
    fail = 0
    skipped = 0
    vector_rows: list[dict] = []

    async with async_session_factory() as session:
        stmt = (
            select(ResourceTask)
            .where(ResourceTask.process_state == "committed")
            .where(ResourceTask.resource_id.is_not(None))
            .options(
                selectinload(ResourceTask.descriptions),
                selectinload(ResourceTask.embeddings),
            )
            .order_by(ResourceTask.id.asc())
        )
        if args.limit > 0:
            stmt = stmt.limit(args.limit)

        tasks = (await session.execute(stmt)).scalars().all()
        total = len(tasks)
        print(f"[DB] committed resources: {total}")

        for idx, task in enumerate(tasks, start=1):
            rid = task.resource_id or ""
            desc_text = _pick_description(task)
            if not desc_text:
                skipped += 1
                print(f"[{idx}/{total}] skip {rid}: no description")
                continue

            try:
                vector = await generate_embedding(desc_text)
                if len(vector) != settings.embedding_dimension:
                    raise RuntimeError(
                        f"dimension mismatch: got {len(vector)} expected {settings.embedding_dimension}"
                    )

                checksum = _compute_checksum(vector)
                vector_rows.append(
                    {
                        "id": task.id,
                        "resource_id": rid,
                        "vector": vector,
                        "resource_type": task.resource_type,
                    }
                )

                # Upsert embedding metadata in Postgres.
                if not args.dry_run:
                    if task.embeddings:
                        emb = sorted(task.embeddings, key=lambda e: e.id, reverse=True)[0]
                    else:
                        emb = ResourceEmbedding(task_id=task.id)
                        session.add(emb)
                    emb.dimension = settings.embedding_dimension
                    emb.model_version = model_version
                    emb.checksum = checksum
                    emb.generate_time = 0.0

                ok += 1
                if idx % 20 == 0 or idx == total:
                    print(f"[{idx}/{total}] ok={ok} fail={fail} skipped={skipped}")
            except Exception as exc:
                fail += 1
                print(f"[{idx}/{total}] fail {rid}: {exc}")

        if not args.dry_run:
            # Batch insert vectors into Milvus.
            for i in range(0, len(vector_rows), args.batch_size):
                batch = vector_rows[i : i + args.batch_size]
                milvus.insert(collection_name=settings.milvus_collection, data=batch)
                print(
                    f"[Milvus] inserted {i + len(batch)}/{len(vector_rows)} vectors"
                )
            await session.commit()

    close_milvus()
    await engine.dispose()

    elapsed = time.time() - t0
    print("-" * 68)
    print(
        f"done in {elapsed:.1f}s | total={ok + fail + skipped}, "
        f"ok={ok}, fail={fail}, skipped={skipped}"
    )
    if args.dry_run:
        print("dry-run mode: no DB or Milvus changes were written.")
    return 0 if fail == 0 else 1


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Rebuild all committed resource embeddings and refresh Milvus.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write DB/Milvus.")
    parser.add_argument("--limit", type=int, default=0, help="Process first N resources.")
    parser.add_argument("--batch-size", type=int, default=100, help="Milvus insert batch size.")
    parser.add_argument(
        "--recreate-collection",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Drop and recreate Milvus collection before rebuild (default: true).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parse_args())))
