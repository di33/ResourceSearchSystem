import unittest
import os
import shutil
from pathlib import Path
import json
from resource_filter import filter_resources, copy_and_categorize_resources, detect_malicious_file, generate_resource_index, filter_resources_with_handlers, ResourceHandlerFactory, check_file_integrity, PreviewGenerator

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

class TestFileIntegrity(unittest.TestCase):

    def setUp(self):
        self.test_dir = "test_integrity"
        os.makedirs(self.test_dir, exist_ok=True)

        self.valid_file = os.path.join(self.test_dir, "valid_file.txt")
        with open(self.valid_file, "wb") as f:
            f.write(b"HEAD")  # 模拟完整的文件头

        self.invalid_file = os.path.join(self.test_dir, "invalid_file.txt")
        with open(self.invalid_file, "wb") as f:
            f.write(b"H")  # 模拟不完整的文件头

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_check_file_integrity_valid(self):
        result = check_file_integrity(self.valid_file)
        self.assertTrue(result, "完整文件校验应通过")

    def test_check_file_integrity_invalid(self):
        result = check_file_integrity(self.invalid_file)
        self.assertFalse(result, "不完整文件校验应失败")

    def test_check_file_integrity_nonexistent(self):
        result = check_file_integrity(os.path.join(self.test_dir, "nonexistent.txt"))
        self.assertFalse(result, "不存在的文件校验应失败")

class TestPreviewGenerator(unittest.TestCase):

    def setUp(self):
        self.test_input = "test_input.txt"
        self.test_output_webp = "test_output.webp"
        self.test_output_mp4 = "test_output.mp4"
        self.test_output_jpeg = "test_output.jpeg"

        # 创建测试输入文件
        with open(self.test_input, "w") as f:
            f.write("Test content")

    def tearDown(self):
        # 删除测试文件
        for file in [self.test_input, self.test_output_webp, self.test_output_mp4, self.test_output_jpeg]:
            if os.path.exists(file):
                os.remove(file)

    def test_generate_preview_webp(self):
        result = PreviewGenerator.generate_preview(self.test_input, self.test_output_webp, "webp")
        self.assertTrue(result)
        self.assertTrue(os.path.exists(self.test_output_webp))

    def test_generate_preview_mp4(self):
        result = PreviewGenerator.generate_preview(self.test_input, self.test_output_mp4, "mp4")
        self.assertTrue(result)
        self.assertTrue(os.path.exists(self.test_output_mp4))

    def test_generate_preview_jpeg(self):
        result = PreviewGenerator.generate_preview(self.test_input, self.test_output_jpeg, "jpeg")
        self.assertTrue(result)
        self.assertTrue(os.path.exists(self.test_output_jpeg))

    def test_generate_preview_invalid_format(self):
        result = PreviewGenerator.generate_preview(self.test_input, "invalid_output.xyz", "xyz")
        self.assertFalse(result)

if __name__ == "__main__":
    unittest.main()