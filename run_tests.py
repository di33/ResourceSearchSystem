"""
运行 unittest：在 import 测试前把 Scripts 加入 sys.path（ResourceProcessor 位于 Scripts/ 下）。

用法（在仓库根目录）:
  python run_tests.py
"""
from __future__ import annotations

import os
import sys
import unittest


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.join(root, "Client", "Scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    if root not in sys.path:
        sys.path.append(root)

    loader = unittest.TestLoader()
    test_dir = os.path.join(root, "Client", "Test")
    suite = loader.discover(test_dir, pattern="test_*.py", top_level_dir=root)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
