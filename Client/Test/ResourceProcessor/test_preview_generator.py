import os
import unittest
from unittest.mock import patch, MagicMock
from ResourceProcessor.resource_filter import PreviewGenerator
import subprocess
class TestPreviewGenerator(unittest.TestCase):

    def setUp(self):
        self.input_path = "test_input.mp4"
        self.output_dir = "test_output"
        self.supported_formats = ["webp", "mp4", "jpeg"]
        self.unsupported_format = "txt"

        # 创建一个测试输入文件
        with open(self.input_path, "w") as f:
            f.write("test")

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def tearDown(self):
        # 清理测试文件和目录
        if os.path.exists(self.input_path):
            os.remove(self.input_path)

        if os.path.exists(self.output_dir):
            for file in os.listdir(self.output_dir):
                os.remove(os.path.join(self.output_dir, file))
            os.rmdir(self.output_dir)

    @patch("ResourceProcessor.resource_filter.os.path.exists", return_value=True)
    @patch("ResourceProcessor.resource_filter.subprocess.run")
    def test_generate_preview_success(self, mock_subprocess, _mock_exists):
        mock_subprocess.return_value = MagicMock()
        result = PreviewGenerator.generate_preview("input.mp4", "output.webp", "webp")
        self.assertTrue(result)

    @patch("ResourceProcessor.resource_filter.subprocess.run")
    def test_generate_preview_failure(self, mock_subprocess):
        mock_subprocess.side_effect = subprocess.CalledProcessError(1, "ffmpeg")
        result = PreviewGenerator.generate_preview("input.mp4", "output.webp", "webp")
        self.assertFalse(result)

    @patch("ResourceProcessor.resource_filter.PreviewGenerator.generate_preview")
    @patch("os.makedirs")
    def test_save_preview_success(self, mock_makedirs, mock_generate_preview):
        mock_generate_preview.return_value = True
        output_dir = "test_output"
        output_path = PreviewGenerator.save_preview("input.mp4", output_dir, "webp")
        self.assertEqual(output_path, os.path.join(output_dir, "preview.webp"))

    @patch("ResourceProcessor.resource_filter.PreviewGenerator.generate_preview")
    def test_save_preview_failure(self, mock_generate_preview):
        mock_generate_preview.return_value = False
        with self.assertRaises(RuntimeError):
            PreviewGenerator.save_preview("input.mp4", "test_output", "webp")

    @patch("ResourceProcessor.resource_filter.subprocess.run")
    def test_generate_preview_supported_formats(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock()
        for fmt in self.supported_formats:
            output_path = os.path.join(self.output_dir, f"preview.{fmt}")
            result = PreviewGenerator.generate_preview(self.input_path, output_path, fmt)
            self.assertTrue(result)
    @patch("ResourceProcessor.resource_filter.PreviewGenerator.generate_preview")
    def test_save_preview(self, mock_generate_preview):
        mock_generate_preview.return_value = True
        for fmt in self.supported_formats:
            output_path = PreviewGenerator.save_preview(self.input_path, self.output_dir, fmt)
            self.assertEqual(output_path, os.path.join(self.output_dir, f"preview.{fmt}"))
    def test_save_preview_failure(self):
        with self.assertRaises(RuntimeError):
            PreviewGenerator.save_preview("non_existent_file.mp4", self.output_dir, "mp4")

if __name__ == "__main__":
    unittest.main()