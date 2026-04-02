import os
import json
from typing import List

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