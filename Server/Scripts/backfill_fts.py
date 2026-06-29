"""Backfill search_vector for existing resources.

Usage:
    $env:PYTHONPATH="server"; python server/Scripts/backfill_fts.py           # fill NULLs only
    $env:PYTHONPATH="server"; python server/Scripts/backfill_fts.py --full     # recompute all rows
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text

from app.deps import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_BACKFILL_DESC_TEMPLATE = """
UPDATE resource_description SET search_vector =
    setweight(to_tsvector('jiebacfg', COALESCE(full_description, '')), 'A')
{where_clause}
"""

_COUNT_SQL = text("""
SELECT
    'resource_description' AS table_name,
    COUNT(*) AS total,
    COUNT(search_vector) AS with_vector
FROM resource_description
""")


async def main(full: bool = False) -> None:
    where_clause = "" if full else "WHERE search_vector IS NULL"
    mode = "full refresh" if full else "fill NULLs only"
    logger.info("Starting FTS backfill (%s) …", mode)

    backfill_desc_sql = text(_BACKFILL_DESC_TEMPLATE.format(where_clause=where_clause))

    async with engine.begin() as conn:
        before = (await conn.execute(_COUNT_SQL)).fetchall()
        logger.info("Before backfill:")
        for row in before:
            logger.info("  %s: %d total, %d with vector", row[0], row[1], row[2])

        r = await conn.execute(backfill_desc_sql)
        logger.info("resource_description backfill: %d rows updated", r.rowcount)

        after = (await conn.execute(_COUNT_SQL)).fetchall()
        logger.info("After backfill:")
        for row in after:
            logger.info("  %s: %d total, %d with vector", row[0], row[1], row[2])

    await engine.dispose()
    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill search_vector for FTS")
    parser.add_argument("--full", action="store_true", help="Recompute all rows instead of only NULLs")
    args = parser.parse_args()
    asyncio.run(main(full=args.full))
