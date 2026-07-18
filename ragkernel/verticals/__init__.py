"""垂直层注册与解析。settings.yaml: vertical: <name> → 对应工厂；默认 none = NullVertical。"""

from .base import NullVertical, Vertical

_REGISTRY: dict[str, callable] = {"none": NullVertical}
_current: Vertical | None = None


def register(name: str, factory: callable) -> None:
    """将来的垂直模块在自己的 __init__ 里调用它登记工厂。"""
    _REGISTRY[name] = factory


def get_vertical() -> Vertical:
    """按 config 返回当前垂直层（进程内单例）。未知名字回退 NullVertical。"""
    global _current
    if _current is None:
        from .. import config

        name = config.settings().get("vertical") or "none"
        factory = _REGISTRY.get(name, NullVertical)
        _current = factory()
    return _current
