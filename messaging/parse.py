"""事件 → ChatMessage。

实现集中在 ``models.py``（纯数据、无网络），这里保持一个稳定的导入路径：

    from messaging.parse import parse_event
"""

from .models import parse_event

__all__ = ["parse_event"]
