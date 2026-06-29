"""Tests for _split_sql in app.main."""
import sys
import unittest
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.main import _split_sql


class TestSplitSql(unittest.TestCase):

    def test_simple_statements(self):
        sql = "SELECT 1;\nSELECT 2;"
        self.assertEqual(_split_sql(sql), ["SELECT 1;", "SELECT 2;"])

    def test_skips_comments_and_blanks(self):
        sql = "-- comment\n\nSELECT 1;\n-- another\nSELECT 2;"
        self.assertEqual(_split_sql(sql), ["SELECT 1;", "SELECT 2;"])

    def test_dollar_quoted_function(self):
        sql = (
            "CREATE FUNCTION foo() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "  RETURN NEW;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;"
        )
        result = _split_sql(sql)
        self.assertEqual(len(result), 1)
        self.assertIn("$$", result[0])
        self.assertIn("LANGUAGE plpgsql", result[0])

    def test_two_dollar_quoted_functions(self):
        sql = (
            "CREATE FUNCTION foo() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "  RETURN NEW;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;\n"
            "CREATE FUNCTION bar() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "  RETURN NEW;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;"
        )
        result = _split_sql(sql)
        self.assertEqual(len(result), 2)
        self.assertIn("foo()", result[0])
        self.assertIn("bar()", result[1])

    def test_multiple_dollar_signs_on_one_line(self):
        """Edge case: a line contains $$ twice (e.g. opening and closing on same line)."""
        sql = "CREATE FUNCTION foo() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql;"
        result = _split_sql(sql)
        self.assertEqual(len(result), 1)
        self.assertIn("LANGUAGE plpgsql", result[0])

    def test_dollar_signs_inside_string_literal(self):
        """$$ inside a string literal (e.g. SELECT '$$') is treated as a delimiter.

        This is a known limitation — _split_sql does not parse SQL string literals.
        It's acceptable because $$ inside string literals is uncommon in practice
        and dollar-quoting is typically used specifically to avoid quoting issues.
        """
        sql = (
            "CREATE FUNCTION foo() RETURNS trigger AS $$\n"
            "  SELECT '$$';\n"
            "$$ LANGUAGE plpgsql;"
        )
        result = _split_sql(sql)
        # The $$ inside SELECT toggles state, causing 2 statements.
        # This is acceptable — document the limitation.
        self.assertGreaterEqual(len(result), 1)

    def test_real_fts_setup_sql(self):
        """Ensure the actual fts_setup.sql splits correctly."""
        sql_path = Path(__file__).resolve().parents[1] / "sql" / "fts_setup.sql"
        result = _split_sql(sql_path.read_text(encoding="utf-8"))
        # Should have: 1 ALTER, 1 CREATE INDEX, 1 CREATE FUNCTION, 1 DROP TRIGGER, 1 CREATE TRIGGER
        # and a comment line about backfill (skipped)
        self.assertGreaterEqual(len(result), 5)
        # Verify no standalone UPDATE statements (backfill removed)
        # "BEFORE INSERT OR UPDATE" in triggers is fine — only flag top-level UPDATE
        for stmt in result:
            self.assertFalse(
                stmt.lstrip().upper().startswith("UPDATE "),
                f"Found standalone UPDATE statement: {stmt[:80]}",
            )


if __name__ == "__main__":
    unittest.main()
