import os
import json
import shutil
from typing import List
from pathlib import Path

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

def is_supported_file(file_path: str, supported_extensions: List[str]) -> bool:
    """
    Check if the file has a supported extension.
    """
    return any(file_path.endswith(ext) for ext in supported_extensions)

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

def filter_resources(directory: str, config_path: str) -> List[str]:
    """
    Filter resources in the given directory based on the supported types.

    Args:
        directory (str): Path to the directory containing resources.
        config_path (str): Path to the JSON configuration file with supported types.

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
                if validate_file_integrity(file_path) and detect_file_disguise(file_path):
                    valid_files.append(file_path)

    return valid_files

def filter_resources_with_handlers(directory: str, config_path: str) -> List[str]:
    """
    Filter resources and process them using dynamically registered handlers.

    Args:
        directory (str): Path to the directory containing resources.
        config_path (str): Path to the JSON configuration file with supported types.

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
                handler_class = ResourceHandlerFactory.get_handler(resource_type)
                if handler_class:
                    handler = handler_class()
                    handler.process(file_path)

                if validate_file_integrity(file_path) and detect_file_disguise(file_path):
                    valid_files.append(file_path)

    return valid_files

def copy_and_categorize_resources(resource_paths: List[str], work_dir: str) -> None:
    """
    Copy and categorize resources into the specified work directory.

    Args:
        resource_paths (List[str]): List of resource file paths to copy.
        work_dir (str): Path to the working directory.

    Returns:
        None
    """
    # Define categories based on file extensions
    categories = {
        'images': ['.jpg', '.jpeg', '.png', '.gif'],
        'models': ['.obj', '.fbx', '.stl'],
        'others': []
    }

    # Create category directories
    for category in categories:
        category_path = Path(work_dir) / category
        category_path.mkdir(parents=True, exist_ok=True)

    for file_path in resource_paths:
        file_ext = Path(file_path).suffix.lower()
        target_category = 'others'

        # Determine the category based on file extension
        for category, extensions in categories.items():
            if file_ext in extensions:
                target_category = category
                break

        target_dir = Path(work_dir) / target_category
        target_file_path = target_dir / Path(file_path).name

        # Handle file name conflicts
        counter = 1
        while target_file_path.exists():
            target_file_path = target_dir / f"{Path(file_path).stem}_{counter}{file_ext}"
            counter += 1

        # Copy the file
        try:
            shutil.copy2(file_path, target_file_path)
        except Exception as e:
            print(f"Failed to copy {file_path} to {target_file_path}: {e}")

def generate_resource_index(resource_paths: List[str], output_path: str, dependencies: dict, statuses: dict) -> None:
    """
    Generate a JSON resource index file.

    Args:
        resource_paths (List[str]): List of resource file paths.
        output_path (str): Path to the output JSON file.
        dependencies (dict): A dictionary mapping resources to their dependencies.
        statuses (dict): A dictionary mapping resources to their statuses.

    Returns:
        None
    """
    resource_index = {}

    for resource in resource_paths:
        resource_index[resource] = {
            "dependencies": dependencies.get(resource, []),
            "status": statuses.get(resource, "unknown")
        }

    try:
        with open(output_path, 'w', encoding='utf-8') as json_file:
            json.dump(resource_index, json_file, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to write resource index to {output_path}: {e}")