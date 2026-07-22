from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _path in (str(_ROOT), str(_SERVER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.routers.resources import FileStructureOut, ResourceDetailOut
from check_server import _print_file_structure


class TestResourceFileStructure(unittest.TestCase):
    def test_detail_contract_contains_file_structure(self):
        detail = ResourceDetailOut(
            content_md5="md5",
            resource_type="tileset",
            process_state="committed",
            file_structure={
                "source": "client",
                "state": "complete",
                "source_object_checksum": "zip-md5",
                "entry_count": 2,
                "total_size": 30,
                "entries": [
                    {"path": "tiles/grass.png", "name": "grass.png", "size": 10, "format": "png", "is_primary": True},
                    {"path": "docs/readme.txt", "name": "readme.txt", "size": 20, "format": "txt"},
                ],
            },
        )

        dumped = detail.model_dump(mode="json")
        self.assertEqual(dumped["file_structure"]["entry_count"], 2)
        self.assertEqual(dumped["file_structure"]["entries"][0]["path"], "tiles/grass.png")

    def test_directory_command_renders_tree(self):
        entries = [
            {"path": "tiles/grass.png", "name": "grass.png", "size": 10, "format": "png", "is_primary": True},
            {"path": "docs/readme.txt", "name": "readme.txt", "size": 20, "format": "txt"},
        ]
        output = StringIO()
        with redirect_stdout(output):
            _print_file_structure(entries)

        text = output.getvalue()
        self.assertIn("tiles/", text)
        self.assertIn("grass.png [primary]", text)
        self.assertIn("docs/", text)


if __name__ == "__main__":
    unittest.main()
