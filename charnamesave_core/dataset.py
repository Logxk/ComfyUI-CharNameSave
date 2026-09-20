"""In-memory Danbooru character dataset: loading and index state.

The module owns the three index containers. Callers must treat them as **shared,
mutated-in-place** objects (tests swap their contents), so never rebind them.
"""

from __future__ import annotations

import json
import logging
import os

from .constants import (
    _DATASET_PATH,
    _DISAMBIG_SUFFIX_RE,
    _DISAMBIG_SUFFIX_FULL_RE,
    _MAX_DATASET_BYTES,
    _is_generic_word_combination,
)

_LOGGER = logging.getLogger(__name__)

# key: 角色名小写（下划线转空格），value: 原始记录 dict
_CHARACTER_INDEX = {}
# 所有角色名的小写集合（下划线转空格），供精确/模糊匹配
_CHARACTER_NAMES_LOWER = set()
# 复合名 -> 首个词别名。例如 "remilia_scarlet"（touhou, 62115）会生成别名
# "remilia" -> ["remilia_scarlet", ...]。数据集体量下这类别名约 1 万个，
# 其中 3400 个有多个候选（歧义），由调用方结合上下文/热度裁决，见 matching.py。
_CHARACTER_ALIASES: dict[str, list[str]] = {}
# 数据集版本号：每次重新加载自增，用于让上层模糊匹配缓存失效（性能优化）。
_DATASET_VERSION = 0
# 重入保护：避免并发调用 _load_character_index 时把索引清空到一半（读取方会看到
# 半填充状态）。ComfyUI 执行线程只读索引，重载通常来自启动或显式调用。
_LOADING = False


def _dataset_key(name: str) -> str:
    """角色名 -> 匹配键：小写 + 下划线转空格（danbooru 标签与提示词写法对齐）。"""
    return name.lower().replace("_", " ").strip()


def _record_post_count(record: dict) -> int:
    """记录热度，非数字（脏数据 / 手写 JSONL）一律按 0 处理，绝不抛异常。"""
    try:
        return int(record.get("post_count", 0))
    except (TypeError, ValueError):
        return 0


def _load_character_index(path: str = _DATASET_PATH) -> int:
    """读取 JSONL 数据集，填充 _CHARACTER_INDEX / _CHARACTER_NAMES_LOWER。

    文件不存在、为空或格式非法时都只打日志，绝不影响插件运行：此时索引为空，
    精确/模糊匹配永不命中，自动回退到原有的 "name (series)" 识别逻辑。
    """
    global _DATASET_VERSION, _LOADING
    if _LOADING:
        _LOGGER.warning("角色数据集正在加载，已跳过重复加载请求。")
        return len(_CHARACTER_INDEX)
    _LOADING = True
    try:
        return _load_character_index_locked(path)
    finally:
        _LOADING = False


def _load_character_index_locked(path: str) -> int:
    global _DATASET_VERSION
    _DATASET_VERSION += 1

    _CHARACTER_INDEX.clear()
    _CHARACTER_NAMES_LOWER.clear()
    _CHARACTER_ALIASES.clear()
    if not os.path.isfile(path):
        message = f"角色数据集不存在，已跳过加载（无作品名角色识别不可用）: {path}"
        try:
            _LOGGER.warning(message)
        except Exception:  # noqa: BLE001 - 日志异常不能拖垮插件
            print(f"[CharNameSave] {message}")
        return 0

    # 安全护栏：异常/损坏的超大文件直接跳过，避免一次性读入造成 OOM。
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > _MAX_DATASET_BYTES:
        _LOGGER.warning(
            "角色数据集文件过大（%d 字节 > 上限 %d 字节），已跳过加载: %s",
            size, _MAX_DATASET_BYTES, path)
        return 0

    loaded = 0
    bad_lines = 0
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError as exc:
        _LOGGER.warning("角色数据集无法读取，已跳过加载: %s", exc)
        return 0

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(record, dict):
                bad_lines += 1
                continue
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                bad_lines += 1
                continue
            name = name.strip()
            key = _dataset_key(name)
            if not key:
                bad_lines += 1
                continue
            # 热度只取一次，且容忍脏数据：原写法对同一字段取两次，且 post_count 为
            # null / 字符串时比较会抛 TypeError，直接打断整个数据集加载。
            posts = _record_post_count(record)
            _CHARACTER_NAMES_LOWER.add(key)
            # 同名多条（如 hatsune_miku 与其各种 costume 版本）保留热度最高的那条
            previous = _CHARACTER_INDEX.get(key)
            if previous is None or posts > _record_post_count(previous):
                _CHARACTER_INDEX[key] = record
            # danbooru 的角色 tag 大量写成 "name_(作品名)"（如 emilia_(re:zero)），
            # 而提示词里往往只写裸名 "emilia"，所以额外把「去掉括号后缀的基础名」
            # 也作为精确匹配键。若发生冲突（rem_(re:zero) / rem_(death_note)），
            # 仍然保留 post_count 更高的那个；数据集中没有裸名条目时才建立这个键。
            base_key = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
            # 若基础名整体由通用词组成（black_hat_(villainous) -> "black hat"），
            # 就不要派生这个裸键：它与提示词里的服装/构图 tag 完全同名，
            # 会被精确命中而变成角色（实测 bug：black hat 让单人提示词落进 Duo）。
            if (base_key and base_key != key
                    and not _is_generic_word_combination(base_key)
                    and base_key not in _CHARACTER_INDEX):
                _CHARACTER_INDEX[base_key] = record
                # 基础名同样进入「已知名字集合」：精确/模糊匹配都要能用，
                # 并且 _dataset_short_name 靠它判断能不能安全地只用短名。
                _CHARACTER_NAMES_LOWER.add(base_key)
            # 复合名的首个词别名：remilia_scarlet -> remilia。只在「该裸名本身
            # 没有原生记录」时登记，避免与真实同名角色抢匹配；歧义由调用方裁决。
            first_word = key.split(" ", 1)[0]
            if " " in key and first_word not in _CHARACTER_INDEX:
                _CHARACTER_ALIASES.setdefault(first_word, []).append(key)
            loaded += 1

    if bad_lines:
        _LOGGER.warning("角色数据集有 %d 行无法解析，已跳过。", bad_lines)
    if not loaded:
        _LOGGER.warning("角色数据集为空，已跳过加载: %s", path)
    else:
        _LOGGER.info("已加载角色数据集 %d 条（%s）。", loaded, path)
    return loaded


def _character_output_name(record: dict) -> str | None:
    """dataset 记录 -> 输出用角色名（交给 _sanitize_name 之前的形式）。

    - 数据集 name 已带 "_(作品名)" 后缀（如 emilia_(re:zero)）时原样保留，
      不重复拼接作品名。
    - 纯名字（如 frieren）且有作品名时拼成 danbooru 惯例的 "name (作品名)"，
      便于文件名保留作品信息、也与原有命名风格一致。
    """
    name = (record.get("name") or "").strip()
    if not name:
        return None
    if _DISAMBIG_SUFFIX_FULL_RE.search(name):
        return name
    copyright_ = record.get("copyright")
    copyright_ = copyright_.strip() if isinstance(copyright_, str) else ""
    if copyright_:
        return f"{name} ({copyright_})"
    return name
