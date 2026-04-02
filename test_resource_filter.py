import unittest
import os
import shutil
from pathlib import Path
import json
from resource_filter import filter_resources, copy_and_categorize_resources, detect_malicious_file, generate_resource_index, filter_resources_with_handlers, ResourceHandlerFactory

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

    def test_filter_resources_with_handlers(self):
        """
        Test the resource filtering with dynamic handlers.
        """
        # Extend the configuration to include a new resource type
        extended_config = {
            "supported_extensions": {
                "images": [".jpg", ".png"],
                "documents": [".txt", ".docx"]
            }
        }

        with open(self.config_path, "w") as f:
            json.dump(extended_config, f)

        # Create additional test files
        image_file = os.path.join(self.test_dir, "image.jpg")
        with open(image_file, "w") as f:
            f.write("Image content")

        document_file = os.path.join(self.test_dir, "document.txt")
        with open(document_file, "w") as f:
            f.write("Document content")

        # Register a mock handler for testing
        class MockHandler:
            def process(self, file_path: str):
                print(f"Mock processing: {file_path}")

        ResourceHandlerFactory.register_handler("images", MockHandler)
        ResourceHandlerFactory.register_handler("documents", MockHandler)

        # Run the filter_resources_with_handlers function
        result = filter_resources_with_handlers(self.test_dir, self.config_path)

        # Validate results
        self.assertIn(image_file, result)
        self.assertIn(document_file, result)

if __name__ == "__main__":
    unittest.main()