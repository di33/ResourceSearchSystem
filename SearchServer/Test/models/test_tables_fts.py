"""Test that ORM models define search_vector columns and GIN indexes."""
import sys
import unittest
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.models.tables import Base, ResourceTask, ResourceDescription


class TestSearchVectorColumns(unittest.TestCase):

    def test_resource_task_has_no_search_vector_column(self):
        columns = {c.name for c in ResourceTask.__table__.columns}
        self.assertNotIn("search_vector", columns)

    def test_resource_task_uses_package_object_fields(self):
        columns = {c.name for c in ResourceTask.__table__.columns}
        self.assertIn("package_storage_profile_id", columns)
        self.assertIn("package_object_key", columns)
        self.assertNotIn("download_object_key", columns)
        self.assertNotIn("download_file_name", columns)
        self.assertNotIn("download_content_type", columns)
        self.assertNotIn("download_file_size", columns)

    def test_resource_task_has_no_legacy_parent_child_fields(self):
        columns = {c.name for c in ResourceTask.__table__.columns}
        self.assertNotIn("parent_resource_id", columns)
        self.assertNotIn("parent_source_resource_id", columns)
        self.assertNotIn("child_resource_ids_json", columns)
        self.assertNotIn("child_source_resource_ids_json", columns)
        self.assertNotIn("child_resource_count", columns)
        self.assertNotIn("contains_resource_types_json", columns)

    def test_resource_description_has_search_vector_column(self):
        columns = {c.name for c in ResourceDescription.__table__.columns}
        self.assertIn("search_vector", columns)

    def test_resource_task_has_no_gin_index(self):
        indexes = ResourceTask.__table__.indexes
        gin_indexes = [idx for idx in indexes if "search_vector" in idx.name]
        self.assertEqual(len(gin_indexes), 0)

    def test_resource_description_has_gin_index(self):
        indexes = ResourceDescription.__table__.indexes
        gin_indexes = [idx for idx in indexes if idx.name == "ix_resource_description_search_vector"]
        self.assertEqual(len(gin_indexes), 1)
        idx = gin_indexes[0]
        self.assertIn("gin", str(idx.kwargs.get("postgresql_using", "")))


if __name__ == "__main__":
    unittest.main()
