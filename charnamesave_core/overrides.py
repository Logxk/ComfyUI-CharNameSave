"""用户自定义 Override（方案 §35）。

可选文件 ``charnamesave_overrides.json``（放在插件目录，属于本地配置、不入仓库）::

    {
      "always": ["miku", "remilia"],
      "never":  ["bow", "black hat"]
    }

语义与优先级（与方案 §35 一致）::

    char:xxx  >  always  >  自动判定  >  never

- ``always``：把裸名**提升**为必定识别（即使自动判定因为歧义 / 通用词而拒绝）。
  注意它只影响「是否采纳这个裸名」，不会凭空造出不存在的角色。
- ``never``：把裸名**降级**为永不识别。它**不影响显式写法**——``char:bow``
  与 ``"bow (series)"`` 仍然照常识别，因为 :func:`is_never` 只在裸名路径被调用。

设计约束：
- 只依赖标准库；文件缺失 / 损坏 / 字段类型不对都只打日志并退回「无 override」，
  绝不影响插件运行（与数据集加载同一套降级原则）。
- 每次调用都重新读文件，便于用户改完立即生效；文件很小时开销可忽略，
  且失败路径会被缓存到下一次进程启动。
"""

from __future__ import annotations

import json
import logging
import os
import threading

_LOGGER = logging.getLogger(__name__)

# 允许的键与默认值
_KEYS = ("always", "never")

# 文件位置：默认放在插件根目录。
# 可用环境变量 CHARNAMESAVE_OVERRIDES 指向别的路径——主要给自动化测试用，
# 好让用例不受开发者本机那份配置影响（测试里设成空串即可完全禁用 override）。
_OVERRIDE_ENV_VAR = "CHARNAMESAVE_OVERRIDES"
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_PATH = os.path.join(_PLUGIN_ROOT, "charnamesave_overrides.json")

# 缓存：(路径, mtime, size) -> 解析结果，避免每次候选都读盘
_CACHE: dict[tuple[str, float, int], dict[str, frozenset[str]]] = {}
_LOCK = threading.Lock()


def override_path() -> str | None:
    """当前生效的 override 文件路径；返回 None 表示用户显式禁用了 override。

    设 ``CHARNAMESAVE_OVERRIDES=""``（空串）即可禁用，测试用这个把环境隔离干净。
    """
    env = os.environ.get(_OVERRIDE_ENV_VAR)
    if env is None:
        return _DEFAULT_PATH
    env = env.strip()
    return env or None



def _normalise(value) -> frozenset[str]:
    """把配置里的列表归一化成与 _dataset_key 相同的写法（小写 + 下划线转空格）。"""
    if not isinstance(value, (list, tuple, set)):
        return frozenset()
    out = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            out.add(item.lower().replace("_", " ").strip())
    return frozenset(out)


def _empty() -> dict[str, frozenset[str]]:
    return {"always": frozenset(), "never": frozenset()}


def load_overrides(path: str | None = None) -> dict[str, frozenset[str]]:
    """读取 override 配置；文件缺失 / 无法解析时返回空配置（不抛异常）。

    path 为 None 时按 :func:`override_path` 解析（默认插件目录下的文件，
    可用 ``CHARNAMESAVE_OVERRIDES`` 改写或置空禁用）。
    """
    if path is None:
        path = override_path()
    if not path:
        return _empty()
    try:
        stat = os.stat(path)
        signature = (path, stat.st_mtime, stat.st_size)
    except OSError:
        return _empty()
    with _LOCK:
        cached = _CACHE.get(signature)
    if cached is not None:
        return cached

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        _LOGGER.warning("override 配置无法读取，已忽略: %s", exc)
        return _empty()

    if not isinstance(raw, dict):
        _LOGGER.warning("override 配置顶层必须是对象，已忽略: %s", path)
        return _empty()

    result = _empty()
    for key in _KEYS:
        if key in raw:
            normalised = _normalise(raw[key])
            if not normalised and raw[key]:
                _LOGGER.warning("override.%s 不是字符串列表，已忽略该键。", key)
            result[key] = normalised
    with _LOCK:
        _CACHE.clear()
        _CACHE[signature] = result
    return result


def is_always(key: str, path: str | None = None) -> bool:
    """裸名 `key` 是否被用户强制采纳（key 需已按 _dataset_key 归一化）。"""
    return bool(key) and key in load_overrides(path)["always"]


def is_never(key: str, path: str | None = None) -> bool:
    """裸名 `key` 是否被用户禁用（key 需已按 _dataset_key 归一化）。

    只在**裸名**路径调用；显式写法（char: / name (series)）不经过本函数。
    """
    return bool(key) and key in load_overrides(path)["never"]
