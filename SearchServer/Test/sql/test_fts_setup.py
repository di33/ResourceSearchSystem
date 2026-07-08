"""Test FTS trigger logic (Python simulation of the SQL trigger functions)."""
import sys
import unittest
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def simulate_description_search_vector(full_description=""):
    """Simulate resource_description_search_vector_trigger in Python."""
    weights = []
    if full_description:
        weights.append(("A", full_description))
    return weights


class TestDescriptionSearchVectorTrigger(unittest.TestCase):

    def test_full_description_gets_weight_a(self):
        result = simulate_description_search_vector(full_description="完整描述")
        self.assertEqual(result[0], ("A", "完整描述"))

    def test_empty_fields_produce_empty_result(self):
        result = simulate_description_search_vector()
        self.assertEqual(result, [])


class TestFTSSqlFileExists(unittest.TestCase):

    def test_fts_setup_sql_exists(self):
        sql_path = _SERVER_DIR / "sql" / "fts_setup.sql"
        self.assertTrue(sql_path.exists(), f"FTS SQL file not found at {sql_path}")

    def test_fts_setup_sql_contains_trigger(self):
        sql_path = _SERVER_DIR / "sql" / "fts_setup.sql"
        content = sql_path.read_text(encoding="utf-8")
        self.assertNotIn("resource_task_search_vector_trigger", content)
        self.assertIn("resource_description_search_vector_trigger", content)
        self.assertIn("pg_jieba", content)
        self.assertIn("CREATE TRIGGER", content)

    def test_fts_setup_sql_has_no_backfill(self):
        """Backfill should NOT run on every startup — use backfill_fts.py instead."""
        sql_path = _SERVER_DIR / "sql" / "fts_setup.sql"
        content = sql_path.read_text(encoding="utf-8")
        self.assertNotIn("UPDATE resource_task", content)
        self.assertNotIn("UPDATE resource_description", content)


if __name__ == "__main__":
    unittest.main()
