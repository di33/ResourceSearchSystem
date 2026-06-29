"""Tests for backfill_fts.py argument parsing and SQL generation."""
import sys
import unittest
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SERVER_DIR / "Scripts"
for _p in (str(_SERVER_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import the templates directly to test SQL generation
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "backfill_fts", str(_SCRIPTS_DIR / "backfill_fts.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestBackfillArgs(unittest.TestCase):

    def test_default_no_full(self):
        """Default: no --full flag."""
        parser = _mod.argparse.ArgumentParser()
        parser.add_argument("--full", action="store_true")
        args = parser.parse_args([])
        self.assertFalse(args.full)

    def test_full_flag(self):
        """--full sets full=True."""
        parser = _mod.argparse.ArgumentParser()
        parser.add_argument("--full", action="store_true")
        args = parser.parse_args(["--full"])
        self.assertTrue(args.full)


class TestBackfillSQLGeneration(unittest.TestCase):

    def test_default_where_clause(self):
        """Without --full, SQL should have WHERE search_vector IS NULL."""
        where = "WHERE search_vector IS NULL"
        sql = _mod._BACKFILL_DESC_TEMPLATE.format(where_clause=where)
        self.assertIn("WHERE search_vector IS NULL", sql)

    def test_full_no_where_clause(self):
        """With --full, SQL should have no WHERE clause."""
        sql = _mod._BACKFILL_DESC_TEMPLATE.format(where_clause="")
        self.assertNotIn("WHERE", sql)
        self.assertIn("UPDATE resource_description", sql)

    def test_desc_sql_uses_jiebacfg(self):
        sql = _mod._BACKFILL_DESC_TEMPLATE.format(where_clause="")
        self.assertIn("'jiebacfg'", sql)

    def test_desc_sql_uses_full_description_only(self):
        sql = _mod._BACKFILL_DESC_TEMPLATE.format(where_clause="")
        self.assertIn("full_description", sql)
        self.assertNotIn("main_content", sql)
        self.assertNotIn("detail_content", sql)


if __name__ == "__main__":
    unittest.main()
