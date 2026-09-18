"""Character-name de-duplication, collection and save-prefix construction."""

from __future__ import annotations

import logging

from .constants import (
    _BARE_NAME_MODE_OFF,
    _CHAR_TAG_RE,
    _DISAMBIG_SUFFIX_RE,
    _MODE_ALIASES,
    _MULTI_GROUP_TAGS,
    _MULTI_NAME_MAX_LEN,
    _MAX_AUTO_NAMES,
    _SERIES_NAME_TAG_RE,
)
from .dataset import _dataset_key
from .matching import _auto_candidates
from .textparse import _clean_path_part, _sanitize_name

_LOGGER = logging.getLogger(__name__)


def _dedupe_character_names(names: list[str]) -> list[str]:
    """角色名去重，并做**父子角色合并**（同一名字同时以 name 与 name (series) 出现）。

    规则（全部大小写不敏感，用 _dataset_key 归一化后比较）：
    1. 完全相同的名字只保留一条（保留第一次出现的原大小写）；
    2. **父子合并**：短的那条本身是纯名字（不含括号），长的形如 `短名 (作品名)`
       （下划线/空格写法都算），且短名就是长名的括号前部分时，丢弃短名、保留长名。
       例如 ["denia", "denia (wuthering waves)"] -> ["denia (wuthering waves)"]；
       ["hakurei_reimu", "hakurei_reimu_(touhou)"] -> ["hakurei_reimu_(touhou)"]；
       注意两条都带括号、只是基础名相同的不同角色（如 "a (series one)" 与
       "a (series two)"）不会被合并，避免误删真实的多角色。

    返回新列表，顺序沿用首次出现的顺序。
    """
    if not names:
        return []

    # 归一化 -> 原始名字（同键保留更长的写法，长度相同则保留先出现的）
    chosen = {}
    for name in names:
        key = _dataset_key(name)
        if not key:
            continue
        previous = chosen.get(key)
        if previous is None or len(name) > len(previous):
            chosen[key] = name

    keys = list(chosen)
    dropped = set()
    for short in keys:
        # 只有「纯名字」（不带括号）才可能被带作品名的版本吸收
        if _SERIES_NAME_TAG_RE.match(short):
            continue
        for long in keys:
            if long == short or short in dropped:
                continue
            if _DISAMBIG_SUFFIX_RE.sub("", long).strip() == short:
                dropped.add(short)
                break

    return [chosen[key] for key in keys if key not in dropped]


def _char_names(texts: list[str], bare_mode: str = _BARE_NAME_MODE_OFF) -> list[str]:
    """收集提示词里识别到的**全部**角色名（去重、自动检测数量）。

    第三轮：移除 max_tags 与 character_list——
    - 显式 `char:角色名` 标记优先：只要出现就只采用它们（保持旧行为）；
    - 否则用 _auto_candidates 收集所有候选（不再按数量截断）；
    - 再去重（父子角色保留更长的那条，见 _dedupe_character_names）；
    - 兜底保护：超过 _MAX_AUTO_NAMES 个视为异常提示词，只保留前 N 个并打 warning。
    """
    explicit = []
    for text in texts:
        for match in _CHAR_TAG_RE.findall(text):
            name = _sanitize_name(match)
            if name and name not in explicit:
                explicit.append(name)
    if explicit:
        return _dedupe_character_names(explicit)[:_MAX_AUTO_NAMES]

    names = _dedupe_character_names(list(_auto_candidates(texts, bare_mode)))
    if len(names) > _MAX_AUTO_NAMES:
        _LOGGER.warning(
            "识别到 %d 个角色，超过上限 %d，只保留前 %d 个（请检查提示词或用 char: 标记）。",
            len(names), _MAX_AUTO_NAMES, _MAX_AUTO_NAMES)
        names = names[:_MAX_AUTO_NAMES]
    return names


def _group_tag_for_count(name_count: int) -> str:
    """按角色数量返回外层分组标签（**标签写死在 _MULTI_GROUP_TAGS**）。

    判定：name_count == 2 -> _MULTI_GROUP_TAGS[0]（Duo）；
    name_count >= 3 -> _MULTI_GROUP_TAGS[1]（Group）。
    0 个（兜底命名）与 1 个（单角色）角色永远不分组，保持旧行为。

    第三轮起多人分组是**固定行为**（UI 参数 enable_multi_group / group_tags 已移除），
    所以这里不再有开关分支。返回空字符串表示不分组。
    """
    if name_count == 2:
        return _MULTI_GROUP_TAGS[0]
    if name_count >= 3:
        return _MULTI_GROUP_TAGS[1]
    return ""


def _sorted_character_names(names: list[str]) -> list[str]:
    """对多角色名字做确定性排序，返回排序后的新列表。

    排序键为 `str.lower()`（大小写不敏感），因此同一条提示词无论 tag 顺序如何变化，
    结果都完全一致；返回的仍是原始大小写的角色名，不做任何改写。
    """
    return sorted(names, key=str.lower)


def _join_character_names(names: list[str]) -> str:
    """用 _ 连接多个角色名，并整体截断到 _MULTI_NAME_MAX_LEN，避免超长路径。

    截断只在超长时发生，并且会去掉截断产生的尾部 "_"。
    """
    joined = "_".join(names)
    if len(joined) > _MULTI_NAME_MAX_LEN:
        joined = joined[:_MULTI_NAME_MAX_LEN].rstrip("_")
    return joined


def _build_save_prefix(names: list[str], mode: str) -> tuple[str | None, str | None]:
    """把识别结果组装成 get_save_image_path 需要的 prefix。

    返回 (prefix, display)。display 是写回前端的提示文本。

    分支：
    1. names 为空 -> 兜底名称（返回 None，由调用方处理）；
    2. 1 个角色 -> 「角色名」/「角色名/角色名」（旧行为，绝不加单人/单人前缀）；
    3. 2 个及以上 -> 外层分组标签（写死在 _MULTI_GROUP_TAGS：Duo / Group）：
       - 文件夹模式 prefix = "Duo/名字A_名字B"（每个组合一个子文件夹，内部连续编号）；
       - 文件名模式 prefix = "Duo_名字A_名字B"（文件名里带分组前缀）；
       名字列表先做确定性排序（见 _sorted_character_names），因此 tag 顺序无关。
    """
    if not names:
        return None, None
    if len(names) == 1:
        # 单角色：完全保持旧行为
        name = names[0]
        prefix = f"{name}/{name}" if _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹" else name
        return prefix, f"角色名: {name}"

    sorted_names = _sorted_character_names(names)
    joined = _join_character_names(sorted_names)
    group_tag = _group_tag_for_count(len(names))
    folder_mode = _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹"

    if group_tag:
        parent = _clean_path_part(group_tag) or group_tag
        if folder_mode:
            # 文件夹模式：外层分组 + 角色组合子文件夹（前缀只到目录，文件名由编号生成）
            prefix = f"{parent}/{joined}"
            return prefix, f"角色名: {prefix}"
        # 文件名模式：文件名前缀带分组标签，如 Duo_kita_ikuyo_gotoh_hitori
        prefix = f"{parent}_{joined}"
        return prefix, f"角色名: {prefix}"

    # 理论上不会走到这里（2 个及以上一定有分组标签：_group_tag_for_count 对 >=2 恒返回
    # 非空值，_MULTI_GROUP_TAGS 是写死的二元组）。这段是防御性兜底，保持既有的拼接
    # 命名行为不变：标签表若被改成空值，命名也不会退化成异常或空前缀。
    prefix = f"{joined}/{joined}" if folder_mode else joined
    return prefix, f"角色名: {joined}"
