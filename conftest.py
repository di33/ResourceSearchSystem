import os
import sys

# Load only the LLM prompt variables from .env so that prompt_config.py
# gets its defaults, without polluting other env vars (e.g. ZHIPUAI_API_KEY)
# that individual tests manage themselves.
_dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_dotenv_path):
    with open(_dotenv_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line and _line.startswith("LLM_"):
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip()
                # Only set if not already present (let tests override)
                if _key not in os.environ:
                    os.environ[_key] = _val

_root = os.path.dirname(os.path.abspath(__file__))
for sub in ("Client/Scripts", "Server/Scripts"):
    p = os.path.join(_root, *sub.split("/"))
    if p not in sys.path:
        sys.path.insert(0, p)

# Also add via pytest hook for earliest possible resolution
def pytest_configure(config):
    for sub in ("Client/Scripts", "Server/Scripts"):
        p = os.path.join(_root, *sub.split("/"))
        if p not in sys.path:
            sys.path.insert(0, p)
