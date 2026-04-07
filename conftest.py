import os
import sys

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
