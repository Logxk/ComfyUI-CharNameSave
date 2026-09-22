"""Pure tag/text parsing helpers (no dataset or filesystem dependencies).

Verbatim extraction of the original functions, plus one benign performance tweak
in ``_prompt_texts`` (set-based de-duplication instead of a linear scan).
"""

from __future__ import annotations

import re

from .constants import (
    _BAD_NAME_RE,
    _SERIES_NAME_TAG_RE,
    _TAG_SPLIT_RE,
    _WEIGHT_RE,
    _WRAPPED_WEIGHT_RE,
    _ARTIST_PREFIX_RE,
)

# 预编译正则：_sanitize_name / _clean_path_part 在数据集加载期与每次保存都会被调用，
# 原实现把 re.sub(pattern) 直接内联，每次调用都要走一遍正则编译缓存查找。
_WHITESPACE_RE = re.compile(r"\s+")
_UNDERSCORE_RUN_RE = re.compile(r"_+")
# 单个角色名长度上限（与 _sanitize_name 的 60 字符截断一致）
_NAME_MAX_LEN = 60


def _prompt_texts(prompt: dict | None) -> list[str]:
    # 性能优化：用 set 去重，避免对大量文本节点做 O(n^2) 的 list 成员判断。
    texts: list[str] = []
    seen: set[str] = set()
    for node in (prompt or {}).values():
        text = node.get("inputs", {}).get("text")
        if isinstance(text, str) and text.strip() and text not in seen:
            seen.add(text)
            texts.append(text)
    return texts


def _strip_brackets(tag: str) -> str:
    """剥掉成对的方括号外层，如 ``[[artist:siu_(siu0207)]]`` -> ``artist:siu_(siu0207)``。

    有些提示词前端会用方括号做分组/强调（实测用户的提示词里同时出现
    ``[[artist:siu_(siu0207)]]`` 与 ``[artist:mana_(remana)]``）。不剥掉的话，
    开头的 ``[`` 会挡住 _ARTIST_PREFIX_RE 的锚点，画师标记被当成角色名，
    最终产出 ``mana_(remana)_rio_(blue_archive)_siu_(siu0207)_5_.png``。
    只剥**成对**的外层括号（数量相等），避免误伤 "[" 开头的普通内容。
    """
    text = (tag or "").strip()
    while len(text) >= 2 and text[0] == "[" and text[-1] == "]":
        depth = 0
        balanced = True
        for index, char in enumerate(text):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        text = text[1:-1].strip()
    return text


def _iter_tags(text: str):
    """按分隔符切分 tag，并统一剥掉成对的外层方括号。

    方括号剥离放在这里做，是为了让**所有**下游阶段看到同一份文本：
    画师过滤（"[[artist:x]]"）、char: 提取（"[[char:x]]"）、
    name(series) 解析都会因此正确工作。只在个别函数里剥会漏。
    """
    for part in _TAG_SPLIT_RE.split(text or ""):
        tag = _strip_brackets(part)
        if tag:
            yield tag


def _unescape_tag(tag: str) -> str:
    return tag.replace("\\(", "(").replace("\\)", ")")


def _strip_weights(tag: str) -> str:
    """剥离外层强调括号与尾部 ":权重"，返回裸 tag（已反转义、已去空白）。

    与 _character_name_from_tag 用同一套正则，保证两种识别路径看到的 tag 一致，
    例如 "(denia (wuthering waves):1.2)" -> "denia (wuthering waves)"。
    """
    tag = _unescape_tag(_strip_brackets(tag)).strip()
    for _ in range(2):
        wrapped = _WRAPPED_WEIGHT_RE.match(tag)
        if not wrapped:
            break
        tag = wrapped.group(1).strip()
    return _WEIGHT_RE.sub("", tag).strip()


def _is_artist_tag(tag: str) -> bool:
    return bool(_ARTIST_PREFIX_RE.match(_strip_brackets(tag)))


def _series_name_text(tag: str) -> str | None:
    """把 tag 还原成 "name (series)" 文本；不是该结构则返回 None。

    同时接受两种写法（danbooru tag 用下划线，提示词里常用空格）：
    "denia (wuthering waves)" / "denia_(wuthering_waves)" / "hakurei_reimu_(touhou)"。
    下划线只在「判断用的副本」里替换成空格，因此返回值总是空格写法，
    与原有输出（_sanitize_name 会把空格转回下划线）保持一致。
    """
    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None
    if _SERIES_NAME_TAG_RE.match(tag):
        return tag
    if "_" not in tag:
        return None
    normalised = tag.replace("_", " ")
    if _SERIES_NAME_TAG_RE.match(normalised):
        return normalised
    return None


def _sanitize_name(name: str) -> str:
    name = _BAD_NAME_RE.sub("_", name)
    name = _WHITESPACE_RE.sub("_", name.strip())
    name = _UNDERSCORE_RUN_RE.sub("_", name).strip("_.")
    return name[:_NAME_MAX_LEN]


def _clean_path_part(part: str) -> str:
    """清洗路径片段：只处理文件名非法字符，中文等非 ASCII 字符原样保留。"""
    cleaned = _BAD_NAME_RE.sub("_", (part or "").strip())
    return _UNDERSCORE_RUN_RE.sub("_", cleaned).strip("_.")

