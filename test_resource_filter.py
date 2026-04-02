import unittest
import os
import shutil
from pathlib import Path
import json
from resource_filter import filter_resources, copy_and_categorize_resources, detect_malicious_file, generate_resource_index

class TestResourceFilter(unittest.TestCase):

    def setUp(self):
        """
        Create a temporary directory and files for testing.
        """
        self.test_dir = "test_resources"
        os.makedirs(self.test_dir, exist_ok=True)

        self.valid_file = os.path.join(self.test_dir, "valid.txt")
        with open(self.valid_file, "w") as f:
            f.write("Valid content")

        self.invalid_file = os.path.join(self.test_dir, "invalid.exe")
        with open(self.invalid_file, "w") as f:
            f.write("Invalid content")

        self.config_path = "test_config.json"
        with open(self.config_path, "w") as f:
            f.write('{"supported_extensions": [".txt"]}')

    def tearDown(self):
        """
        Clean up temporary files and directories.
        """
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def test_filter_resources(self):
        """
        Test the resource filtering functionality.
        """
        result = filter_resources(self.test_dir, self.config_path)
        self.assertIn(self.valid_file, result)
        self.assertNotIn(self.invalid_file, result)

    def test_copy_and_categorize_resources(self):
        """
        Test the copy and categorize functionality.
        """
        work_dir = Path(self.test_dir) / "output"
        work_dir.mkdir(exist_ok=True)

        resource_paths = [self.valid_file, self.invalid_file]
        copy_and_categorize_resources(resource_paths, str(work_dir))

        # Check if files are categorized correctly
        self.assertTrue((work_dir / "others" / "valid.txt").exists())
        self.assertTrue((work_dir / "others" / "invalid.exe").exists())

    def test_detect_malicious_file(self):
        """
        Test the malicious file detection functionality.
        """
        # Placeholder test, assuming all files are non-malicious
        self.assertFalse(detect_malicious_file(self.valid_file))
        self.assertFalse(detect_malicious_file(self.invalid_file))

    def test_generate_resource_index(self):
        """
        Test the resource index generation functionality.
        """
        resource_paths = [self.valid_file, self.invalid_file]
        output_path = os.path.join(self.test_dir, "resources.json")
        dependencies = {
            self.valid_file: ["dependency1", "dependency2"],
            self.invalid_file: []
        }
        statuses = {
            self.valid_file: "completed",
            self.invalid_file: "failed"
        }

        from resource_filter import generate_resource_index
        generate_resource_index(resource_paths, output_path, dependencies, statuses)

        # Verify the output JSON file
        with open(output_path, "r", encoding="utf-8") as json_file:
            data = json.load(json_file)

        self.assertIn(self.valid_file, data)
        self.assertIn(self.invalid_file, data)
        self.assertEqual(data[self.valid_file]["dependencies"], ["dependency1", "dependency2"])
        self.assertEqual(data[self.valid_file]["status"], "completed")
        self.assertEqual(data[self.invalid_file]["status"], "failed")

if __name__ == "__main__":
    unittest.main()