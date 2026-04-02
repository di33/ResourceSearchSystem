import unittest
import os
from resource_filter import filter_resources

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
        os.remove(self.valid_file)
        os.remove(self.invalid_file)
        os.rmdir(self.test_dir)
        os.remove(self.config_path)

    def test_filter_resources(self):
        """
        Test the resource filtering functionality.
        """
        result = filter_resources(self.test_dir, self.config_path)
        self.assertIn(self.valid_file, result)
        self.assertNotIn(self.invalid_file, result)

if __name__ == "__main__":
    unittest.main()