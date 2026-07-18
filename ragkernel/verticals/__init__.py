"""垂直层注册与解析。settings.yaml: vertical: <name> → 对应工厂;默认 none = NullVertical。

`get_vertical()` 先按名字懒加载对应模块(它在自己的 import 期调用 register()),再查注册表 ——
这样只导入被选中的垂直,不必预载全部。
"""

import importlib

from .base import NullVertical, Vertical

_REGISTRY: dict[str, callable] = {"none": NullVertical}
_current: Vertical | None = None


def register(name: str, factory: callable) -> None:
    """垂直模块在自己的 import 期调用它登记工厂。"""
    _REGISTRY[name] = factory


def get_vertical() -> Vertical:
    """按 config 返回当前垂直层(进程内单例)。未知名字回退 NullVertical。"""
    global _current
    if _current is None:
        from .. import config

        name = config.settings().get("vertical") or "none"
        if name not in _REGISTRY:
            try:
                importlib.import_module(f"ragkernel.verticals.{name}")
            except ModuleNotFoundError:
                pass
        factory = _REGISTRY.get(name, NullVertical)
        _current = factory()
    return _current
