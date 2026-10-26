"""测试环境：把项目根目录放进 ``sys.path``。

新项目是常规包布局，测试统一从根目录导入（``config`` / ``messaging`` / ``agent`` …），
不再像旧版那样把 ``Minitor`` 插入路径。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
