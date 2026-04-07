import os
import json
import shutil
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path
import subprocess
# 设置日志记录
logging.basicConfig(level=logging.ERROR, filename='integrity_check.log',
                    format='%(asctime)s - %(levelname)s - %(message)s')

class ResourceHandlerFactory:
    """
    Factory class to register and retrieve resource handlers dynamically.
    """
    _handlers = {}

    @classmethod
    def register_handler(cls, resource_type: str, handler_class):
        """
        Register a handler for a specific resource type.

        Args:
            resource_type (str): The resource type (e.g., 'images').
            handler_class (type): The handler class to register.
        """
        cls._handlers[resource_type] = handler_class

    @classmethod
    def get_handler(cls, resource_type: str):
        """
        Retrieve the handler for a specific resource type.

        Args:
            resource_type (str): The resource type.

        Returns:
            type: The handler class for the resource type.
        """
        return cls._handlers.get(resource_type)

# Example handler class for demonstration
class ImageHandler:
    def process(self, file_path: str):
        print(f"Processing image: {file_path}")

# Register the ImageHandler dynamically
ResourceHandlerFactory.register_handler("images", ImageHandler)

class PreviewGenerator:
    SUPPORTED_FORMATS = {"webp", "mp4", "jpeg"}

    @staticmethod
    def generate_preview(input_path: str, output_path: str, format: str) -> bool:
        """
        根据目标格式生成预览文件。

        Args:
            input_path (str): 输入文件路径。
            output_path (str): 输出文件路径。
            format (str): 目标格式（webp, mp4, jpeg）。

        Returns:
            bool: True 表示生成成功，False 表示失败。
        """
        if not os.path.exists(input_path):
            logging.error(f"输入文件不存在: {input_path}")
            return False

        if format.lower() not in PreviewGenerator.SUPPORTED_FORMATS:
            logging.error(f"不支持的格式: {format}")
            return False

        try:
            # 使用 ffmpeg 生成预览文件
            command = [
                "ffmpeg", "-i", input_path, output_path
            ]
            subprocess.run(command, check=True)
            logging.info(f"预览文件已生成: {output_path}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"预览生成失败: {e}")
            return False
        except Exception as e:
            logging.error(f"未知错误: {e}")
            return False

    @staticmethod
    def save_preview(input_path: str, output_dir: str, format: str) -> str:
        """
        保存预览文件到指定路径。

        Args:
            input_path (str): 输入文件路径。
            output_dir (str): 输出目录。
            format (str): 目标格式。

        Returns:
            str: 生成的预览文件路径。
        """
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        file_name = f"preview.{format}"
        output_path = os.path.join(output_dir, file_name)

        try:
            success = PreviewGenerator.generate_preview(input_path, output_path, format)
            if not success:
                raise RuntimeError(f"预览文件生成失败: {output_path}")

            return output_path
        except Exception as e:
            logging.error(f"保存预览文件失败: {e}")
            raise

def is_supported_file(file_path: str, supported_extensions: List[str]) -> bool:
    """
    Check if the file has a supported extension.
    """
    return any(file_path.endswith(ext) for ext in supported_extensions)

def check_file_integrity(file_path: str) -> bool:
    """
    检查文件头信息是否完整。

    Args:
        file_path (str): 文件路径。

    Returns:
        bool: 校验结果，True 表示通过，False 表示失败。
    """
    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件未找到: {file_path}")

        with open(file_path, 'rb') as file:
            header = file.read(4)  # 假设文件头信息为前 4 个字节
            if len(header) < 4:
                raise ValueError("文件头信息不完整")

        return True

    except Exception as e:
        logging.error(f"文件完整性校验失败: {file_path}, 错误: {e}")
        return False

def validate_file_integrity(file_path: str) -> bool:
    """
    Validate the file's integrity by checking its header.
    """
    try:
        with open(file_path, 'rb') as f:
            header = f.read(4)  # Example: Read the first 4 bytes
            # Add specific header validation logic here
            return True  # Placeholder for actual validation
    except Exception:
        return False

def detect_file_disguise(file_path: str) -> bool:
    """
    Detect if the file type is disguised.
    """
    # Placeholder for file disguise detection logic
    return True

def detect_malicious_file(file_path: str) -> bool:
    """
    Detect if a file is malicious.

    Args:
        file_path (str): Path to the file to check.

    Returns:
        bool: True if the file is malicious, False otherwise.
    """
    # Placeholder for actual malicious file detection logic
    # Example: Check for suspicious patterns or scan with antivirus
    return False


def filter_resources(directory: str, config_path: str, max_file_size: int = None, max_file_count: int = None) -> List[str]:
    """
    Filter resources in the given directory based on the supported types, file size and count limits.

    Args:
        directory (str): Path to the directory containing resources.
        config_path (str): Path to the JSON configuration file with supported types.
        max_file_size (int, optional): 单个文件最大字节数，超出则跳过。
        max_file_count (int, optional): 最多返回的文件数量。

    Returns:
        List[str]: List of valid resource file paths.
    """
    try:
        with open(config_path, 'r') as config_file:
            supported_types = json.load(config_file).get("supported_extensions", [])
    except Exception as e:
        raise ValueError(f"Failed to load configuration: {e}")

    valid_files = []

    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            if is_supported_file(file_path, supported_types):
                if max_file_size is not None and os.path.getsize(file_path) > max_file_size:
                    continue
                if check_file_integrity(file_path) and validate_file_integrity(file_path) and detect_file_disguise(file_path):
                    valid_files.append(file_path)
                    if max_file_count is not None and len(valid_files) >= max_file_count:
                        return valid_files

    return valid_files


def filter_resources_with_handlers(directory: str, config_path: str, max_file_size: int = None, max_file_count: int = None) -> List[str]:
    """
    Filter resources and process them using dynamically registered handlers, with file size/count limits.

    Args:
        directory (str): Path to the directory containing resources.
        config_path (str): Path to the JSON configuration file with supported types.
        max_file_size (int, optional): 单个文件最大字节数，超出则跳过。
        max_file_count (int, optional): 最多返回的文件数量。

    Returns:
        List[str]: List of valid resource file paths.
    """
    try:
        with open(config_path, 'r') as config_file:
            config = json.load(config_file)
            supported_types = config.get("supported_extensions", {})
    except Exception as e:
        raise ValueError(f"Failed to load configuration: {e}")

    valid_files = []

    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            file_ext = Path(file_path).suffix.lower()

            # Determine the resource type based on the extension
            resource_type = None
            for r_type, extensions in supported_types.items():
                if file_ext in extensions:
                    resource_type = r_type
                    break

            if resource_type:
                if max_file_size is not None and os.path.getsize(file_path) > max_file_size:
                    continue
                handler_class = ResourceHandlerFactory.get_handler(resource_type)
                if handler_class:
                    handler = handler_class()
                    handler.process(file_path)

                if check_file_integrity(file_path) and validate_file_integrity(file_path) and detect_file_disguise(file_path):
                    valid_files.append(file_path)
                    if max_file_count is not None and len(valid_files) >= max_file_count:
                        return valid_files

    return valid_files

def copy_single_categorized_resource(file_path: str, work_dir: str) -> Optional[Path]:
    """
    将单个资源拷贝到 work_dir 下对应类型子目录，重名时自动加 _1、_2 后缀。

    Returns:
        目标文件的绝对路径；拷贝失败时为 None。
    """
    categories = {
        "images": [".jpg", ".jpeg", ".png", ".gif"],
        "models": [".obj", ".fbx", ".stl"],
        "others": [],
    }
    file_ext = Path(file_path).suffix.lower()
    target_category = "others"
    for category, extensions in categories.items():
        if file_ext in extensions:
            target_category = category
            break

    target_dir = Path(work_dir) / target_category
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file_path = target_dir / Path(file_path).name

    counter = 1
    while target_file_path.exists():
        target_file_path = target_dir / f"{Path(file_path).stem}_{counter}{file_ext}"
        counter += 1

    try:
        shutil.copy2(file_path, target_file_path)
    except Exception as e:
        print(f"Failed to copy {file_path} to {target_file_path}: {e}")
        return None
    return target_file_path.resolve()


def copy_and_categorize_resources(resource_paths: List[str], work_dir: str) -> Dict[str, str]:
    """
    Copy and categorize resources into the specified work directory.

    Args:
        resource_paths (List[str]): List of resource file paths to copy.
        work_dir (str): Path to the working directory.

    Returns:
        源路径 -> 拷贝后绝对路径 的映射。
    """
    categories = {
        "images": [".jpg", ".jpeg", ".png", ".gif"],
        "models": [".obj", ".fbx", ".stl"],
        "others": [],
    }
    for category in categories:
        category_path = Path(work_dir) / category
        category_path.mkdir(parents=True, exist_ok=True)

    mapping: Dict[str, str] = {}
    for file_path in resource_paths:
        dest = copy_single_categorized_resource(file_path, work_dir)
        if dest is not None:
            mapping[file_path] = str(dest)
    return mapping

def generate_resource_index(
    resource_paths: List[str],
    output_path: str,
    dependencies: dict,
    statuses: dict,
    extra: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """
    Generate a JSON resource index file.

    Args:
        resource_paths (List[str]): List of resource file paths.
        output_path (str): Path to the output JSON file.
        dependencies (dict): A dictionary mapping resources to their dependencies.
        statuses (dict): A dictionary mapping resources to their statuses.
        extra: 每条资源额外字段（如 copied_path、preview_path），会合并进对应条目。

    Returns:
        None
    """
    resource_index = {}

    for resource in resource_paths:
        entry: Dict[str, Any] = {
            "dependencies": dependencies.get(resource, []),
            "status": statuses.get(resource, "unknown"),
        }
        if extra and resource in extra:
            for k, v in extra[resource].items():
                if v is not None:
                    entry[k] = v
        resource_index[resource] = entry

    try:
        with open(output_path, 'w', encoding='utf-8') as json_file:
            json.dump(resource_index, json_file, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to write resource index to {output_path}: {e}")


# ---------------------------------------------------------------------------
# Multi-file aggregation: group by directory
# ---------------------------------------------------------------------------

def group_files_by_directory(file_paths: List[str]) -> Dict[str, List[str]]:
    """
    Group files by their immediate parent directory.
    Files in the same directory belong to the same resource.

    Returns:
        Dict mapping directory path -> list of file paths in that directory.
    """
    groups: Dict[str, List[str]] = {}
    for fp in file_paths:
        parent = os.path.dirname(os.path.abspath(fp))
        groups.setdefault(parent, []).append(fp)
    return groups


def determine_file_role(file_path: str, all_files: List[str]) -> tuple:
    """
    Determine the role of a file within a resource group.
    Returns (file_role, is_primary).

    Priority: 3D model > main image > other.
    The first file (alphabetically) among equals gets is_primary=True.
    """
    primary_exts = {".fbx", ".obj", ".stl", ".gltf", ".glb"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
    texture_exts = {".png", ".jpg", ".jpeg", ".webp", ".tga"}

    ext = Path(file_path).suffix.lower()
    name = Path(file_path).stem.lower()

    if ext in primary_exts:
        return "model", True
    if ext in texture_exts and ("texture" in name or "tex" in name or "diffuse" in name or "normal" in name):
        return "texture", False
    if ext in image_exts:
        return "main", False
    return "attachment", False


def compute_composite_md5(file_paths: List[str]) -> str:
    """
    Compute a composite fingerprint for a group of files.
    MD5(sorted([md5(f) for f in files])).
    """
    import hashlib
    individual_md5s = []
    for fp in sorted(file_paths):
        h = hashlib.md5()
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        individual_md5s.append(h.hexdigest())
    combined = hashlib.md5("".join(sorted(individual_md5s)).encode()).hexdigest()
    return combined