"""Character-name matching: exact "name (series)", meta tags, bare names, fuzzy.

Verbatim extraction of the original functions. The only change is that
``_fuzzy_candidate_matches`` memoises its difflib scan behind a small LRU cache
keyed on the dataset version, so repeated prompts skip the O(N) similarity scan.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from . import dataset as _dataset_mod
from .overrides import is_always as _is_always, is_never as _is_never
from .constants import (
    _ARTIST_IN_NAME_RE,
    _CHAR_TAG_RE,
    _ACCEPT_THRESHOLD_EXACT,
    _ACCEPT_THRESHOLD_FUZZY,
    _BARE_NAME_EXACT_BLOCKLIST,
    _BARE_NAME_MIN_LEN,
    _BARE_NAME_MODE_EXACT,
    _BARE_NAME_MODE_FUZZY,
    _BARE_NAME_MODE_OFF,
    _BARE_NAME_MODES,
    _BARE_NAME_STOPWORDS,
    _DISAMBIG_SUFFIX_RE,
    _FUZZY_CUTOFF,
    _FUZZY_MAX_LEN_DIFF,
    _FUZZY_MIN_COVERAGE,
    _META_SERIES,
    _META_SERIES_KEEP_NAME,
    _META_SERIES_MIN_LEN,
    _META_SERIES_SUFFIX,
    _META_SERIES_TAG_RE,
    _NUMBER_RE,
    _PLAIN_NAME_RE,
    _SCORE_DATASET_ALIAS,
    _SCORE_DATASET_EXACT,
    _SCORE_DATASET_EXACT_CONTEXTUAL,
    _SCORE_DATASET_FUZZY,
    _SCORE_EXPLICIT_CHAR,
    _SCORE_EXPLICIT_SERIES,
    _is_generic_word_combination,
    _SCORE_FUZZY_COVERAGE,
    _SCORE_OTHER_COPYRIGHT,
    _SCORE_POST_100,
    _SCORE_POST_1000,
    _SCORE_POST_10000,
    _SCORE_SAME_COPYRIGHT,
    _SERIES_NAME_RE,
    _SERIES_NAME_TAG_RE,
    _WEIGHT_RE,
)
from .dataset import (
    _CHARACTER_NAMES_LOWER,
    _character_output_name,
    _dataset_key,
    _record_post_count,
)
from .textparse import (
    _is_artist_tag,
    _iter_tags,
    _sanitize_name,
    _series_name_text,
    _strip_weights,
    _unescape_tag,
)

_LOGGER = logging.getLogger(__name__)


def _character_name_from_tag(tag: str) -> str | None:
    """从单个 tag 提取角色名，返回 "name (series)"（已清洗），拿不到返回 None。

    两种分支：
    1. **元标签分支**：tag 形如 "<name> (cosplay)" / "<name>_(alternate_costume)"
       （括号里是 _META_SERIES 里的元标签）时，绝不把 cosplay 当作品名。改为：
       先用 _resolve_character_name 做数据集校验（命中就用数据集里的规范短名，
       多为 kita_ikuyo 这种短名字）；校验不通过时按 _META_SERIES_KEEP_NAME 决定是
       否仍采用括号前的裸名，并统一追加 _META_SERIES_SUFFIX（默认 "_cosplay"）。
    2. **原有分支**：普通 "name (series)" 结构，输出 "name (series)"，行为与旧版一致。

    画师 tag 在这里就被拒绝，绝不会变成文件名，例如
    "by (ningen mame:0.5)"、"@hiten (hitenkei):0.6"、"by ningen mame"。
    """
    stripped = _strip_weights(tag)

    # --- 分支 1：<name> (cosplay) 等元标签，不能当作品名 ---
    meta_name, _meta = _meta_series_name(stripped)
    if meta_name:
        resolved = _resolve_character_name(meta_name, _BARE_NAME_MODE_OFF)
        if resolved:
            # 数据集命中（元标签场景不要求名字长度，因为括号前的内容本身就是标签）
            return resolved
        # 数据集没命中：兜底保留括号前的裸名 + cosplay 后缀（名字太短则不猜）
        if _META_SERIES_KEEP_NAME and _looks_like_character_name(meta_name):
            return _sanitize_name(meta_name + _META_SERIES_SUFFIX)
        return None

    # --- 分支 2：原有 "name (series)" 逻辑（保持不变）---
    tag = _series_name_text(tag)
    if not tag:
        return None
    match = _SERIES_NAME_TAG_RE.match(tag)
    if not match:
        return None
    name = _WEIGHT_RE.sub("", match.group("name")).strip()
    series = _WEIGHT_RE.sub("", match.group("series")).strip()
    if not name or not series or _NUMBER_RE.match(series):
        return None
    if "@" in name or _ARTIST_IN_NAME_RE.search(name):
        return None
    return _sanitize_name(f"{name} ({series})")


def _exact_dataset_tag_name(tag: str) -> str | None:
    """tag 本身（归一化后）就是数据集键时，把它当作**显式**写法。

    典型："saori (dress)" 恰好是数据集里的键（saori_(dress)_(blue_archive)），
    但 _character_name_from_tag 只认单层括号里的**作品名**，解析不出来；
    若不在这里认领，它会掉进裸名路径，让提示词里的 `shy` 被认成角色。

    只接受**直接命中**（key 原样存在于索引）；不做「去括号基础名」回退，
    因为那正是把 "saori (dress)" 误解析成 saori_(blue_archive) 的那条路。
    """
    stripped = _strip_weights(tag)
    if not stripped or _is_artist_tag(stripped):
        return None
    text = _unescape_tag(stripped)
    # 只认领**带括号**的 tag（"saori (dress)"）。判据必须这么窄：
    #  - 单个词（shy / bow / ribbon…）属于裸名，要交给通用词屏蔽表管；
    #  - 不带括号的多词裸名（gotoh_hitori / kita_ikuyo）也必须走裸名路径，
    #    否则它们会被当成「显式写法」从而整段抑制裸名识别，
    #    把 "kita ikuy, gotoh hitori" 里的 kita_ikuyo 一起挤掉。
    if "(" not in text:
        return None
    key = _dataset_key(text)
    if key not in node_index():
        return None
    if _is_generic_bare_tag(key):
        return None
    return _dataset_short_name(text, node_index()[key])


def node_index() -> dict:
    """数据集索引（薄封装，便于测试替换与阅读）。"""
    return _dataset_mod._CHARACTER_INDEX


def _qualified_character_name(tag: str) -> str | None:
    r"""解析多层括号的限定写法：``角色 (皮肤) (作品名)``。

    数据集里这类角色只有**多层括号**一种规范写法（实测 saori 有
    saori_(blue_archive)、saori_(dress)_(blue_archive)、saori_(swimsuit)_(blue_archive)），
    而 _character_name_from_tag 只认单层括号，于是：
      - ``saori \(dress\) \(blue archive\)`` 解析失败 -> 落到裸名路径；
      - 提示词里剩下的 ``shy`` 反而被认成角色，产出 shy.png。
    这里从右往左逐层剥括号，每剥一层都拿归一化键去数据集里查：
      ``saori (dress) (blue archive)`` -> 命中该皮肤
      ``saori (dress)``                -> 命中同一条记录
    一旦剥到没有括号就停止 —— **绝不回退成裸名**，否则
    ``saori (bikini)`` 这种不存在的皮肤会被硬套成基础角色 saori，
    与用户写的皮肤不符。逗号/斜杠分隔的多个皮肤同理，交给显式路径判断。
    """
    stripped = _strip_weights(tag)
    if not stripped or _is_artist_tag(stripped):
        return None
    # 统一用反转义后的文本做判断，这样 "saori \(dress\)" 与 "saori (dress)" 等价
    text = _unescape_tag(stripped).strip()
    if "(" not in text:
        return None
    index = _dataset_mod._CHARACTER_INDEX
    # 记录最外层括号里的内容，作为「作品名」用于校验
    outer = text.rfind("(")
    outer_series = _dataset_key(text[outer + 1:].rstrip(") ")) if outer >= 0 else ""
    candidate = text
    while "(" in candidate:
        cut = candidate.rfind("(")
        candidate = candidate[:cut].strip()
        if not candidate:
            break
        key = _dataset_key(candidate)
        record = index.get(key) if key else None
        if record is not None:
            # 括号里的作品名必须与数据集记录一致（记录没写作品名时跳过校验）
            record_cp = _dataset_key(record.get("copyright") or "")
            if not (outer_series and record_cp and outer_series != record_cp):
                return _dataset_short_name(record.get("name") or candidate, record)
        # 继续往里剥；循环条件会保证不会退化成裸名
    return None


def _candidate_from_tag(tag, mode, list_lookup, character_list=None):
    """第二轮/第三轮共用的单 tag 候选提取（先原有逻辑，再名单，再数据集）。

    - mode 为「关闭」时只做原有 "name (series)" 与手动名单匹配；元标签 tag 会走
      _character_name_from_tag 的元标签分支，因此 cosplay 场景不依赖开关。
    - mode 打开时才继续做无作品名（数据集）识别。
    - list_lookup 是已规范化的小写名单映射，避免重复解析。
    """
    candidate = _character_name_from_tag(tag)
    if candidate:
        return candidate
    stripped = _strip_weights(tag)
    if list_lookup:
        listed = list_lookup.get(stripped.lower())
        if listed:
            return _sanitize_name(listed)
    if mode == _BARE_NAME_MODE_OFF:
        return None
    return _character_bare_name(tag, mode)


def _meta_series_of(series):
    """series 文本是否属于元标签（cosplay 等）。

    归一化后比较：大小写不敏感，且把空格/连字符都折算成下划线，因此
    "Cosplay"、"alternate costume"、"alternate-costume"、"alternate_costume"
    都会命中 _META_SERIES。
    """
    if not series:
        return False
    key = series.strip().lower().replace(" ", "_").replace("-", "_")
    return key in _META_SERIES


def _meta_series_name(tag):
    """识别 "<name> (cosplay)" / "<name>_(alternate_costume)" 这类元标签 tag。

    返回 (裸名字, 元标签名)；不是该结构时返回 (None, None)。
    只看括号里的内容是否属于 _META_SERIES，与作品名无关。
    """
    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None, None
    match = _META_SERIES_TAG_RE.match(tag)
    if not match:
        return None, None
    meta = match.group("meta").strip()
    if not _meta_series_of(meta):
        return None, None
    name = _WEIGHT_RE.sub("", match.group("name")).strip()
    if not name or "@" in name or _ARTIST_IN_NAME_RE.search(name):
        return None, None
    return name, meta


def _looks_like_character_name(name):
    """粗略判断一段文本是否「像」角色名，用于元标签场景下的兜底。

    要求：长度 >= _META_SERIES_MIN_LEN、不是纯数字/纯百分数、只含常见名字字符
    （字母数字下划线、连字符、空格、点、撇号）。数据集与手动名单都没命中时才用。
    """
    if not name or len(name) < _META_SERIES_MIN_LEN:
        return False
    if _NUMBER_RE.match(name):
        return False
    return bool(_PLAIN_NAME_RE.match(name))


# 模糊匹配候选索引：{(首字母, 候选名长度): (名字, ...)}。
# 由 _CHARACTER_NAMES_LOWER 惰性构建，条目为已排序元组，因此遍历顺序与进程的
# PYTHONHASHSEED 无关（原实现对 set 直接扫描，并列时胜者随进程变化）。
_FUZZY_BUCKETS: dict[tuple[str, int], tuple[str, ...]] = {}
# 与 _FUZZY_BUCKETS 对应的 (数据集版本, 名字集合规模)，用于判断是否需要重建。
_FUZZY_BUCKETS_SIGNATURE: tuple[int, int] | None = None


def _dataset_version() -> int:
    """当前数据集状态标识：(重载次数, 名字集合规模)。

    规模也参与标识，是为了让「外部原地改写索引容器」这种测试/扩展用法同样能让
    模糊匹配缓存与候选索引失效——只比对 _DATASET_VERSION 会读到过期结果。
    """
    return _dataset_mod._DATASET_VERSION, len(_CHARACTER_NAMES_LOWER)


def _fuzzy_buckets(signature: tuple[int, int]) -> dict[tuple[str, int], tuple[str, ...]]:
    """按 (首字母, 长度) 分桶缓存候选名，返回签名匹配的桶表。

    首字母本来就是模糊匹配的硬约束（key[0] 必须等于 candidate[0]），长度差也限制在
    _FUZZY_MAX_LEN_DIFF 之内，所以只在这些桶里比较与原来扫描整个集合**结果等价**，
    但比较次数从 ~31000 降到个位数~百位数。
    """
    global _FUZZY_BUCKETS, _FUZZY_BUCKETS_SIGNATURE
    if _FUZZY_BUCKETS_SIGNATURE == signature:
        return _FUZZY_BUCKETS
    buckets: dict[tuple[str, int], list[str]] = {}
    for name in _CHARACTER_NAMES_LOWER:
        if name:
            buckets.setdefault((name[0], len(name)), []).append(name)
    # 排序保证遍历顺序确定，从而并列时胜者确定（见 _fuzzy_scan）
    _FUZZY_BUCKETS = {bucket: tuple(sorted(names)) for bucket, names in buckets.items()}
    _FUZZY_BUCKETS_SIGNATURE = signature
    return _FUZZY_BUCKETS


def _score_candidate(key: str, candidate: str) -> float:
    """difflib 相似度（与 get_close_matches 的 ratio 口径一致）。

    先用 real_quick_ratio / quick_ratio 这两个**上界**快速排除，只有上界达标才做
    完整计算，避免在大量明显不相似的候选上浪费 SequenceMatcher 的 O(n²) 比较。
    """
    matcher = SequenceMatcher(None, key, candidate)
    if matcher.real_quick_ratio() < _FUZZY_CUTOFF:
        return 0.0
    if matcher.quick_ratio() < _FUZZY_CUTOFF:
        return 0.0
    return matcher.ratio()


def _fuzzy_scan(key: str) -> str | None:
    """在键/长度相邻的候选桶里找**唯一**可接受的角色名，找不到返回 None。

    返回值的语义与原实现（get_close_matches 取前 3 名后逐条筛选）一致；差别只在
    并列时不再依赖集合迭代顺序：先取相似度最高的，仍并列则取较短的名字，再并列
    取字典序最小者，因此同一提示词在任何进程里结果都相同。
    """
    buckets = _fuzzy_buckets(_dataset_version())
    first = key[0]
    # 择优口径：(相似度最高, 名字最短, 字典序最小)。用 min 配合取负的相似度表达，
    # 避免把字符串也取负（那会反转字典序）。
    best: tuple[float, int, str] | None = None
    for length in range(len(key) - _FUZZY_MAX_LEN_DIFF, len(key) + _FUZZY_MAX_LEN_DIFF + 1):
        if length < _BARE_NAME_MIN_LEN:
            # 候选本身也必须达到最低长度（防止数据集里的短别名反向命中）
            continue
        for candidate in buckets.get((first, length), ()):
            score = _score_candidate(key, candidate)
            if score < _FUZZY_CUTOFF:
                continue
            rank = (-score, len(candidate), candidate)
            if best is None or rank < best:
                best = rank
    return best[2] if best is not None else None


@lru_cache(maxsize=8192)
def _fuzzy_candidate_matches_cached(key, mode, signature):
    """_fuzzy_candidate_matches 的实际实现（带分桶相似度扫描）。

    signature 仅参与缓存键（见 _dataset_version）：数据集重新加载或名字集合规模
    变化都会让它改变，从而让旧结果自然失效。signature 本身不参与计算。
    """
    if mode != _BARE_NAME_MODE_FUZZY:
        return None
    if not key or len(key) < _BARE_NAME_MIN_LEN:
        return None
    if key in _BARE_NAME_STOPWORDS:
        return None
    if not _CHARACTER_NAMES_LOWER:
        return None
    return _fuzzy_scan(key)


def _fuzzy_candidate_matches(key: str, mode: str) -> str | None:
    """模糊匹配的候选筛选：返回**唯一**可接受的候选角色名，否则 None。

    第三轮修复（触发案例："stage" 被误判为角色 "sage"，相似度 ≈ 8/9 ≈ 0.888）：
    只有同时满足下面全部约束的候选才被接受，任何一条不满足都直接放弃：

    1. **停用词**：tag 命中 _BARE_NAME_STOPWORDS（stage / live house / guitar /
       looking at each other ... 等通用词与场景词）时，完全不参与模糊匹配；
    2. **最低长度**：len(tag) >= _BARE_NAME_MIN_LEN（4），排除 sage、2girls 这类短词；
    3. **长度接近**：abs(len(tag) - len(cand)) <= _FUZZY_MAX_LEN_DIFF（1），
       stage(5) 与 sage(4) 差 1 虽然满足，但 stage 已在停用表里被拦下；
    4. **首字母相同**：tag[0].lower() == cand[0].lower()；
    5. **相似度**：difflib ratio >= _FUZZY_CUTOFF（0.92，比旧值 0.85 严格）。

    另外：调用方会先做一次精确匹配（大小写不敏感，见 _dataset_lookup），
    只有精确未命中才会走到这里。

    性能：结果按数据集状态缓存（见 _fuzzy_candidate_matches_cached），相同提示词
    第二次起无需再比较；候选集合按首字母与长度分桶（见 _fuzzy_buckets），未命中的
    tag 只与极小一部分名字比较，不再每次扫描全部角色名。

    确定性：并列时按 (相似度, 长度, 字典序) 择优，不依赖集合迭代顺序，因此同一提示词
    在任何进程/任何 PYTHONHASHSEED 下都得到相同结果。
    """
    return _fuzzy_candidate_matches_cached(key, mode, _dataset_version())


def _dataset_lookup(tag: str, mode: str = _BARE_NAME_MODE_EXACT) -> tuple[dict | None, str | None]:
    """在数据集索引里查 tag，返回 (原始记录 dict, 命中的索引键)，未命中返回 (None, None)。

    匹配顺序：
    1. **精确匹配**（大小写/下划线空格不敏感）完整 tag，如 "hakurei reimu (touhou)"；
    2. 精确匹配去掉尾部 "_(版本名/服装名)" 后缀的基础名（如 "hakurei reimu"）；
    3. 精确都没命中、且 mode == 数据集模糊匹配 时，才走 _fuzzy_candidate_matches
       的近似匹配（带停用词 / 长度 / 首字母 / cutoff 约束）。

    返回值里的命中键用于区分「精确命中」与「模糊猜测」：只有精确命中数据集里的
    规范写法时才采用它的名字，模糊命中要保留用户/提示词里的原始拼写。
    """
    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None, None
    key = _dataset_key(tag)
    if not key or key.isdigit():
        return None, None
    if not _CHARACTER_NAMES_LOWER:
        return None, None

    # 通用词屏蔽表：即使数据集里存在同名角色（bow_(paper_mario)、elf_(dragon's_crown)），
    # 裸名也不允许命中。必须放在精确匹配之前——原实现只在模糊分支查停用词，
    # 精确路径完全绕过，通用 tag 因此被当成角色。
    # _dataset_lookup 也被 _character_bare_name 这条旧 API 复用，放在这里可保证新旧
    # 两条识别路径行为一致。显式写法走 _character_name_from_tag，不经过本函数，
    # 所以 char:bow / "bow (series)" 不受影响。
    if key in _BARE_NAME_EXACT_BLOCKLIST:
        return None, None

    # --- 1) 精确匹配（第三轮要求：模糊匹配之前必须先做一次精确匹配）---
    index = _dataset_mod._CHARACTER_INDEX
    record = index.get(key)
    if record is not None:
        return record, key
    # --- 2) 去掉 "_(版本名)" 后缀的基础名精确匹配 ---
    base = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
    record = index.get(base)
    if record is not None:
        return record, base
    # --- 3) 模糊匹配：带全部约束 ---
    candidate = _fuzzy_candidate_matches(key, mode)
    if candidate is None:
        return None, None
    return index.get(candidate), candidate


def _output_name_for_record(record, fallback=""):
    """dataset 记录 -> 输出用角色名；记录为空时退回 fallback（已清洗）。"""
    candidate = _character_output_name(record) if record else None
    if candidate:
        return _sanitize_name(candidate)
    return _sanitize_name(fallback) if fallback else None


def _dataset_short_name(tag, record, matched_key=None):
    """dataset 记录 -> 输出用**短名字**（多角色拼接 / cosplay 场景优先用短名）。

    规则（matched_key 保留在签名里以便将来细分，当前不使用）：
    1. 数据集记录的 **基础名**（去掉 "_(作品名)" 后缀）就是最终名字，存在即用：
       - "kita_ikuyo" / "hakurei_reimu"（真实裸名条目）-> 原样；
       - "emilia_(re:zero)" / "saber_(fate)"（只有带后缀的唯一写法）-> 用 emilia / saber，
         既缩短多角色文件名，也避免同一角色因提示词大小写不同落到两个目录；
    2. 提示词里写的是 "name (series)"、但记录的**作品名与提示词里的 series 不同**时
       （例如 "denia (xxx)" 命中别名叫法），退到数据集标准写法，不做无依据的收缩；
    3. 模糊匹配命中（tag 本身不是规范的 "name (series)" 写法）时保留用户原始拼写。
    """
    if not tag:
        return None
    key = _dataset_key(tag)
    canonical = _dataset_key((record or {}).get("name") or "")
    base = _DISAMBIG_SUFFIX_RE.sub("", canonical).strip()

    if _SERIES_NAME_TAG_RE.match(key):
        # 提示词自带作品名：只有与数据集里的作品名一致（或数据集本身就是裸名）才收缩
        match = _SERIES_NAME_TAG_RE.match(key)
        series = match.group("series").strip()
        canonical_match = _SERIES_NAME_TAG_RE.match(canonical)
        same_series = bool(canonical_match) and canonical_match.group("series").strip() == series
        if base and not base.isdigit() and (not canonical_match or same_series):
            return _sanitize_name(base) or None
        return _output_name_for_record(record, tag)

    if base and not base.isdigit():
        # 裸名/加权写法：用数据集的基础名（修正大小写与空格差异）
        return _sanitize_name(base) or None
    if canonical and not _SERIES_NAME_TAG_RE.match(canonical):
        return _sanitize_name(canonical) or None
    # 模糊命中且没有可用的基础名：保留用户原始拼写，不猜
    return _sanitize_name(tag) or None


def _resolve_character_name(tag: str, mode: str = _BARE_NAME_MODE_OFF) -> str | None:
    """把一个「开放写法」的**裸名字** tag 解析成角色名（优先返回短名，不带作品名）。

    参数 tag 应当已经剥掉元标签/作品名（元标签场景由 _meta_series_name 先剥壳）；
    这里只做「这个名字是哪个角色」的判定，不做括号解析、不做轮次编排。

    这是无作品名识别 / 元标签识别的**共用作答函数**，只负责判定，不做轮次编排，
    因此 _character_name_from_tag（元标签分支）与 _character_bare_name（第二轮）
    可以复用同一套判定而不互相递归：

    1. 内嵌 Danbooru 数据集精确匹配（大小写 / 下划线空格不敏感）；
    2. 都没命中且 mode == 数据集模糊匹配 时，再走 difflib 近似匹配
       （带停用词 / 长度 / 首字母 / cutoff 约束，见 _fuzzy_candidate_matches）；
    3. 命中后按「短名优先」返回（见 _dataset_short_name），这样多角色拼接与 cosplay
       场景不会出现 "kita_ikuyo_(bocchi_the_rock!)_gotoh_hitori_(...)" 这种超长组合。

    未命中一律返回 None，保证数据集缺失时行为与旧版一致。
    """
    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None

    # 数据集：精确 -> 去后缀基础名 ->（模糊模式）difflib
    dataset_mode = mode if mode in _BARE_NAME_MODES else _BARE_NAME_MODE_EXACT
    record, matched_key = _dataset_lookup(tag, dataset_mode)
    if record is None:
        return None

    # 短名优先（多角色/cosplay 场景的关键）
    return _dataset_short_name(tag, record, matched_key)


def _character_bare_name(tag: str, mode: str) -> str | None:
    """无作品名角色识别：判断纯名字 tag（如 "Emilia" / "rem"）是否为已知角色。

    识别顺序：数据集完整 tag 精确匹配 -> 去掉 "_(版本名/服装名)" 后缀的基础名精确
    匹配 -> （模糊模式）带上全部约束的近似匹配。命中则返回清洗后的短名，未命中返回
    None。数据集为空集合时永不命中，等同于关闭。

    已经是 "name (series)"（含 danbooru 的 name_(series) 写法）的 tag 交给第一轮处理；
    但 "<name> (cosplay)" 这类**元标签**除外：这里会先剥掉元标签，只拿 <name> 去匹配，
    因为 (cosplay) 不是作品名（见 _meta_series_name / _META_SERIES）。
    """
    if mode not in _BARE_NAME_MODES or mode == _BARE_NAME_MODE_OFF:
        return None
    if not _CHARACTER_NAMES_LOWER:
        return None

    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None

    # 元标签（"kita_ikuyo (cosplay)"）必须先剥壳再判断：它长得像 "name (series)"，
    # 若先走 _series_name_text 就会被误判成普通作品名 tag 而直接放弃。
    meta_name, _meta = _meta_series_name(tag)
    if meta_name:
        return _resolve_character_name(meta_name, mode)

    # 已经是 "name (series)"（含 danbooru 的 name_(series) 写法）的 tag 不归本函数管。
    # 这里用已剥权重的 tag 判断，避免 _series_name_text 内部再剥一次。
    if _series_name_text(tag):
        return None
    return _resolve_character_name(tag, mode)


def _iter_tokens(texts):
    """把多段文本分词一次，供后续各阶段复用（性能：原实现每轮重新 split）。"""
    return [(text, list(_iter_tags(text))) for text in texts]


def _as_tokenised(texts_or_tokens):
    """接受「原始文本序列」或「已分词结果」，统一返回已分词结果。"""
    if texts_or_tokens and isinstance(texts_or_tokens[0], tuple):
        return texts_or_tokens
    return _iter_tokens(texts_or_tokens)


# ---------------------------------------------------------------------------
# 候选角色（Candidate）：收集 → 评分 → 过滤 → 裁决 → 规范化 → 去重
#
# 设计要点（对应「角色识别鲁棒性」方案）：
#   * 显式写法（char:xxx / xxx (series)）是强证据，永不被通用词过滤掉；
#   * 裸名是弱证据，必须自己证明自己：先过通用词屏蔽表，再看热度与上下文；
#   * 弱证据不能覆盖强证据：例如 "professor_niyaniya (blue archive)" + "bow"
#     不能产出 bow_professor_niyaniya_(blue_archive)。
# ---------------------------------------------------------------------------

# 候选来源标识（内部使用，不对外暴露）
_SRC_EXPLICIT_CHAR = "explicit_char"
_SRC_DATASET_EXACT_CONTEXTUAL = "dataset_exact_contextual"
_SRC_EXPLICIT_SERIES = "explicit_series"
_SRC_DATASET_EXACT = "dataset_exact"
_SRC_DATASET_ALIAS = "dataset_alias"
_SRC_DATASET_FUZZY = "dataset_fuzzy"

_SOURCE_SCORES = {
    _SRC_EXPLICIT_CHAR: _SCORE_EXPLICIT_CHAR,
    _SRC_EXPLICIT_SERIES: _SCORE_EXPLICIT_SERIES,
    _SRC_DATASET_EXACT_CONTEXTUAL: _SCORE_DATASET_EXACT_CONTEXTUAL,
    _SRC_DATASET_EXACT: _SCORE_DATASET_EXACT,
    _SRC_DATASET_ALIAS: _SCORE_DATASET_ALIAS,
    _SRC_DATASET_FUZZY: _SCORE_DATASET_FUZZY,
}


@dataclass
class CharacterCandidate:
    """一个角色候选：名字 + 证据来源 + 数据集元数据 + 评分。"""

    name: str
    source: str
    source_tag: str | None = None
    canonical: str | None = None          # 数据集里的规范写法（用于规范化与去重）
    copyright: str | None = None
    post_count: int = 0
    score: int = 0
    order: int = 0                        # 首次出现顺序，保证输出稳定


@dataclass
class _CharacterContext:
    """提示词级上下文：从显式角色里抽出的作品名（copyright）。"""

    copyrights: set[str] = field(default_factory=set)


# --- 可选诊断输出（对应方案 §36 Logging / Debug Mode、§37 Debug 信息不要污染普通日志）---
# 默认关闭；环境变量 CHARNAMESAVE_DEBUG 取真值（1/true/yes/on，大小写与首尾空白不敏感）
# 时打开。每次判定现读环境变量、不做任何缓存，所以不重启进程也能开关。
_DEBUG_ENV_VAR = "CHARNAMESAVE_DEBUG"
_DEBUG_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _debug_enabled() -> bool:
    """诊断输出是否打开（惰性读取环境变量；关闭时只有这一次环境变量查询）。"""
    return os.environ.get(_DEBUG_ENV_VAR, "").strip().lower() in _DEBUG_TRUTHY


def _log_candidate_debug(candidate: CharacterCandidate, accepted: bool) -> None:
    """为单个角色候选打一条结构化 DEBUG 记录（仅诊断，不参与任何判定）。

    目的是让误识别能直接定位到「候选生成 / 评分 / 裁决」中的哪一步：
    source=explicit_* 却 decision=REJECT，说明候选被作品名一致性护栏
    （_apply_series_guard）丢弃，属于候选筛选阶段，与评分无关；其余 REJECT 都是
    评分没过 _accept_candidate 的门槛（score 即最终得分）。

    关闭时提前 return：_LOGGER.debug 的整段文本与全部参数都不会被求值，
    因此普通运行没有任何额外开销（也不改变任何输出）。
    """
    if not _debug_enabled():
        return
    _LOGGER.debug(
        "[CharNameSave] Candidate:\n"
        "  tag=%s\n"
        "  source=%s\n"
        "  dataset_name=%s\n"
        "  post_count=%d\n"
        "  copyright=%s\n"
        "  score=%d\n"
        "  decision=%s",
        candidate.source_tag or candidate.name,
        candidate.source,
        candidate.canonical or candidate.name,
        candidate.post_count,
        candidate.copyright or "",
        candidate.score,
        "ACCEPT" if accepted else "REJECT",
    )


def _post_count_score(posts: int) -> int:
    """热度加分（辅助证据，绝不作为「是不是角色」的判据）。"""
    if posts >= 10000:
        return _SCORE_POST_10000
    if posts >= 1000:
        return _SCORE_POST_1000
    if posts >= 100:
        return _SCORE_POST_100
    return 0


def _score_character_candidate(candidate: CharacterCandidate, context: _CharacterContext) -> int:
    """按来源 / 热度 / 通用词 / 上下文一致性给候选打分。"""
    score = _SOURCE_SCORES.get(candidate.source, 0)
    score += _post_count_score(candidate.post_count)
    own = _dataset_key(candidate.copyright or "")
    if candidate.source == _SRC_DATASET_EXACT_CONTEXTUAL:
        # 该证据层（75 分）本身已把「上下文一致」算进去了，再走下面的 +20 会重复
        # 计分，所以这里只处理「属于别的作品」的扣分。
        if own and context.copyrights and own not in context.copyrights:
            score += _SCORE_OTHER_COPYRIGHT
    elif own and context.copyrights:
        if own in context.copyrights:
            score += _SCORE_SAME_COPYRIGHT
        else:
            score += _SCORE_OTHER_COPYRIGHT
    # 模糊命中额外要求「打字完整度」：tag 越接近完整角色名越可信。
    # "kita ikuy"（9/10 个字) 远比 "elf" 这种只写了前几个词的猜测可信。
    if candidate.source == _SRC_DATASET_FUZZY and candidate.source_tag:
        typed = _dataset_key(candidate.source_tag)
        # 基准用候选自身的名字（被命中的角色键）；canonical 带作品名后缀，
        # 会让覆盖度被无故压低。
        basis = _dataset_key(candidate.name)
        if basis:
            coverage = min(len(typed) / len(basis), 1.0)
            if coverage < _FUZZY_MIN_COVERAGE:
                return score - 1000          # 覆盖度过低：直接不接受
            score += int(coverage * _SCORE_FUZZY_COVERAGE)
    return score


def _accept_candidate(candidate: CharacterCandidate) -> bool:
    """按来源分别设定接受门槛：显式写法直接通过，弱证据需要更高分。"""
    if candidate.source in (_SRC_EXPLICIT_CHAR, _SRC_EXPLICIT_SERIES):
        return True
    if candidate.source == _SRC_DATASET_FUZZY:
        return candidate.score >= _ACCEPT_THRESHOLD_FUZZY
    return candidate.score >= _ACCEPT_THRESHOLD_EXACT


def _collect_explicit_candidates(texts, bare_mode: str = _BARE_NAME_MODE_OFF
                                  ) -> list[CharacterCandidate]:
    """第一轮：显式写法（char:xxx 与 name (series)）。强证据，不过通用词过滤。"""
    collected: list[CharacterCandidate] = []
    order = 0
    for _text, tags in _as_tokenised(texts):
        for tag in tags:
            # 前两个是语法解析（与数据集无关，任何模式都生效）；
            # 第三个要查数据集，属于「无作品名识别」能力，必须在关闭模式下停用。
            name = _character_name_from_tag(tag) or _qualified_character_name(tag)
            if not name and bare_mode != _BARE_NAME_MODE_OFF:
                name = _exact_dataset_tag_name(tag)
            if not name:
                continue
            collected.append(CharacterCandidate(
                name=name, source=_SRC_EXPLICIT_SERIES,
                source_tag=tag, order=order))
            order += 1
    return collected


def _has_char_marker(texts) -> bool:
    """提示词里是否出现 char: 显式标记（与 "name (series)" 同属显式写法）。

    _char_names 会优先采用 char: 并在命中时直接返回，但 _auto_candidates 也必须
    知道「存在显式写法」才能抑制裸名判定，否则直接调用它时规则不生效。
    """
    for _text, tags in _as_tokenised(texts):
        for tag in tags:
            if _CHAR_TAG_RE.search(_unescape_tag(tag)):
                return True
    return False


def _series_copyrights(texts) -> set[str]:
    """从 "name (series)" 写法里抽出 series，作为提示词上下文（作品名）。

    这样 "miku (vocaloid)" 里的 vocaloid 会成为上下文，帮助裸名候选选中同作品角色
    （实测：裸键 "miku" 指向 darling_in_the_franxx，而未带上下文的评分会接受它）。
    """
    found: set[str] = set()
    for _text, tags in _as_tokenised(texts):
        for tag in tags:
            text = _series_name_text(tag)
            if not text:
                continue
            match = _SERIES_NAME_TAG_RE.match(text)
            if not match:
                continue
            series = _WEIGHT_RE.sub("", match.group("series")).strip()
            key = _dataset_key(series)
            if key and not _NUMBER_RE.match(series):
                found.add(key)
    return found


def _build_character_context(explicit: list[CharacterCandidate],
                             texts=None) -> _CharacterContext:
    """由显式角色（以及可选的原文本）构建上下文。"""
    context = _CharacterContext()
    for candidate in explicit:
        key = _dataset_key(candidate.copyright or "")
        if key:
            context.copyrights.add(key)
    if texts is not None:
        context.copyrights |= _series_copyrights(texts)
    return context


def _apply_series_guard(candidates: list[CharacterCandidate]) -> list[CharacterCandidate]:
    """显式 "name (series)" 的作品名必须与数据集一致，否则丢弃该候选。

    数据集里可能存在同名但属于别的作品的角色，例如裸键 "miku" 指向
    miku_(darling_in_the_franxx)。若提示词写的是 "miku (vocaloid)"，旧实现会
    回退到基础名从而张冠李戴；这里改为：作品名对不上就不产出角色。
    """
    kept: list[CharacterCandidate] = []
    for candidate in candidates:
        text = _series_name_text(candidate.source_tag or "")
        match = _SERIES_NAME_TAG_RE.match(text) if text else None
        if not match:
            kept.append(candidate)
            continue
        series_key = _dataset_key(_WEIGHT_RE.sub("", match.group("series")).strip())
        rec = _dataset_ref_for_name(candidate.name) or {}
        record_cp = _dataset_key(rec.get("copyright") or "")
        if series_key and record_cp and series_key != record_cp:
            # 括号里未必是作品名，也可能是**皮肤名**：danbooru 的
            # "saori (dress) (blue archive)" 同时有皮肤层与作品层。
            # 只有当这个限定词既不是记录里的括号段、也不是它的作品名时，才认定
            # 「作品名对不上」并丢弃；否则会把合法皮肤写法误杀，让提示词掉进裸名
            # 路径（实测输出 shy.png 而不是 saori_(dress)）。
            record_parens = _dataset_key(
                " ".join(re.findall(r"\(([^()]*)\)", rec.get("name") or "")))
            if series_key not in record_parens.split():
                # 这里丢弃的候选也要有诊断记录（否则日志里看不到「候选生成过但被护栏滤掉」）
                _log_candidate_debug(candidate, False)
                continue
        kept.append(candidate)
    return kept


def _dataset_ref_for_name(name: str) -> dict | None:
    """按输出名精确反查数据集记录（取 copyright / 热度 / canonical）。

    只做精确键匹配，**不做去后缀的基础名回退**：显式写法 "a (series one)" 若
    回退成基础名 "a"，会撞上完全无关的角色记录（真实数据集里 "a" 指向
    a_(xenoblade)），从而污染上下文与作品名一致性检查。
    """
    return _dataset_mod._CHARACTER_INDEX.get(_dataset_key(name))


def _fill_candidate_dataset_meta(candidate: CharacterCandidate) -> None:
    """补上 copyrigh / 热度 / 规范写法（显式候选用于上下文与规范化）。"""
    record = _dataset_ref_for_name(candidate.name)
    if record is None:
        return
    candidate.copyright = record.get("copyright") or ""
    candidate.post_count = _record_post_count(record)
    # canonical 只在为空时填：显式候选的 canonical 就是它输出的 "name (series)"，
    # 若在这里覆盖成 _character_output_name()（可能带上作品名后缀），会打乱去重键，
    # 也会污染 _build_character_context 抽出的上下文。
    if not candidate.canonical:
        candidate.canonical = _character_output_name(record) or candidate.name


def _exact_source_for(record: dict, context: _CharacterContext | None) -> str:
    """精确裸名命中时选证据层：作品名与提示词上下文一致 -> 75 分那一层。

    方案 §5 把「精确裸名 + 上下文支持」单独列为 dataset_exact_contextual(75)，
    高于普通 dataset_exact(55)。没有上下文可比时保持 55。
    """
    if context is None or not context.copyrights:
        return _SRC_DATASET_EXACT
    own = _dataset_key((record or {}).get("copyright") or "")
    if own and own in context.copyrights:
        return _SRC_DATASET_EXACT_CONTEXTUAL
    return _SRC_DATASET_EXACT


def _is_generic_bare_tag(key: str) -> bool:
    """裸名 key 是否属于「通用词」而不该被当作角色（方案 §30 的具名入口）。

    两个判据：整词命中通用词屏蔽表，或整个名字由通用词拼成（black hat）。
    """
    return key in _BARE_NAME_EXACT_BLOCKLIST or _is_generic_word_combination(key)


def _is_native_name_for_key(record_name: str, key: str) -> bool:
    """记录的「裸键」是否代表上游真有一条叫这个名字的记录。

    用于把「原生单字角色」（frieren / kita_ikuyo / hatsune_miku）与
    「从 单词_(作品名) 派生出来的幻影键」（bow_(paper_mario) -> "bow"）区分开：
    后者与提示词里的普通英文 tag 逐字相同，必须排除在裸名识别之外。

    判据基于记录的真实名字（不是归一化键）：把 `name_(series)` 拆成
    「名字 + 括号部分」。只有当括号前部分是**单个词**、且它不等于整条名字时，
    这个裸键才是「括号后作品的派生品」，需要排除。
    这样 bow_(paper_mario) -> "bow" 被排除，而
    rem_(re:zero) -> "rem"、rio_(blue_archive) -> "rio" 这类「不带作品名的
    规范短名」仍然可用（它们是数据集对同一角色的另一种等价写法）。
    """
    raw = (record_name or "").strip()
    if not raw or "(" not in raw:
        return True
    head = raw.split("(", 1)[0].rstrip("_ ").strip()
    head_key = head.lower().replace("_", " ").strip()
    if not head_key or " " in head_key:
        return True
    return head_key == _dataset_key(raw)


# 派生裸键的「压倒性胜出」判据：最高热度至少是次高的这么多倍，且自身不低于下限。
# 数据校准：rem 10255 vs 160（64x）通过；miku 220 vs 157（1.4x）不通过。
_HEAD_DOMINANCE_RATIO = 3
_HEAD_DOMINANCE_MIN_POSTS = 500


def _dominant_head_record(key: str) -> dict | None:
    """在「同一个角色短名」的多个记录里挑出压倒性胜出者，歧义时返回 None。

    只处理单词键。候选 = 记录名去掉括号后恰好等于该键的记录，
    例如 key="rem" 时匹配 rem_(re:zero) / rem_(death_note)。
    """
    index = _dataset_mod._CHARACTER_INDEX
    matches: list[dict] = []
    seen_ids: set[int] = set()
    for index_key, record in index.items():
        if _DISAMBIG_SUFFIX_RE.sub("", index_key).strip() != key:
            continue
        marker = id(record)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        matches.append(record)
    if not matches:
        return None
    matches.sort(key=_record_post_count, reverse=True)
    top = _record_post_count(matches[0])
    second = _record_post_count(matches[1]) if len(matches) > 1 else 0
    if top < _HEAD_DOMINANCE_MIN_POSTS:
        return None
    if second and top < second * _HEAD_DOMINANCE_RATIO:
        return None
    return matches[0]


def _aliased_records(key: str) -> list[tuple[str, dict]]:
    """复合名别名候选：(索引键, 记录)，按热度降序。"""
    out: list[tuple[str, dict]] = []
    index = _dataset_mod._CHARACTER_INDEX
    for alias_key in _dataset_mod._CHARACTER_ALIASES.get(key, ()):
        record = index.get(alias_key)
        if record is not None:
            out.append((alias_key, record))
    out.sort(key=lambda item: -_record_post_count(item[1]))
    return out


def _lookup_bare_candidate(tag: str, mode: str,
                           context: _CharacterContext | None = None) -> CharacterCandidate | None:
    """裸名 tag -> 候选：通用词屏蔽 → 精确 → 别名 → 模糊。

    返回 None 表示「不构成角色候选」。多角色的选择由调用方结合上下文裁决。
    """
    stripped = _strip_weights(tag)
    if not stripped or _is_artist_tag(stripped):
        return None
    key = _dataset_key(stripped)
    if not key or key.isdigit() or not _CHARACTER_NAMES_LOWER:
        return None
    # 带限定括号（含转义写法）的 tag 不是「裸名」，只能走显式路径。
    # 否则 "saori (dress)" 会经由「去括号基础名」回退成 saori_(blue_archive)，
    # 或干脆落到别的分支，与用户写的意思不符。
    # 注意只拦括号：frieren / kita ikuyo 这类**原生**裸名没有括号，不受影响。
    if "(" in _unescape_tag(stripped) and not _is_always(key):
        return None

    # §35 用户 override：never = 该裸名永不识别。
    # 显式写法不经过本函数，所以 never 不会影响 char: / "name (series)"。
    if _is_never(key):
        return None
    # 通用词屏蔽：即使数据集里有同名角色也不允许裸名命中（显式写法不受影响）。
    # 用户在 override 里明确 always 时跳过这道判定（见方案 §35 的优先级）。
    if not _is_always(key) and _is_generic_bare_tag(key):
        return None

    index = _dataset_mod._CHARACTER_INDEX

    def _make(record: dict, source: str, display: str) -> CharacterCandidate:
        # 输出用「短名」：与旧的 _dataset_short_name 行为一致（rem 而不是
        # rem_(re:zero)、frieren 而不是 frieren (sousou_no_frieren)）。
        # canonical 保留数据集规范写法，供上下文与去重使用。
        name = _dataset_short_name(record.get("name") or display, record) \
            or _sanitize_name(display) or display
        candidate = CharacterCandidate(
            name=name, source=source, source_tag=tag, canonical=record.get("name"))
        _fill_candidate_dataset_meta(candidate)
        return candidate

    # 结构收紧：单词派生键 = 数据集里的 "单词_(作品名)" 被派生成裸键，与普通英文
    # tag 无法区分（bow_(paper_mario) -> "bow"、elf_(dragon's_crown) -> "elf"）。
    # 判据（用记录的真实名字，而不是归一化键，避免 strip 掉括号后误判为原生）：
    #   名字的「括号前部分」是单词、且这个单词本身不是整条名字 => 该裸键是派生的。
    # 派生键只有在「同前缀候选里有一个压倒性胜出」时才允许继续，
    # 否则它就是歧义通用词，退回普通 tag 语义：
    #   rem_(re:zero) 10255 vs rem_(death_note) 160  -> 压倒性，识别 rem
    #   miku_(darling_in_the_franxx) 220 vs miku_(lee) 157 -> 歧义，不识别 miku
    record = index.get(key)
    if record is not None:
        if not _is_native_name_for_key(record.get("name") or "", key):
            dominant = _dominant_head_record(key)
            if dominant is None:
                # 歧义裸名：默认拒绝；用户 always 指定时仍采纳（方案 §35）
                if not _is_always(key):
                    return None
            else:
                record = dominant
        # 1) 精确命中：原生裸名（frieren / kita ikuyo）以及名字本身就是该词的记录
        return _make(record, _exact_source_for(record, context), stripped)

    # 2) 去 "_(版本名)" 后缀的基础名：只接受**多词**基础名，避免
    #    「单词_(作品名)」经由基础名路径泄漏成通用词角色。
    base = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
    if base and base != key and " " in base:
        record = index.get(base)
        if record is not None:
            return _make(record, _SRC_DATASET_EXACT, stripped)

    # 3) 复合名别名（如 remilia -> remilia_scarlet）：取热度最高者作为默认。
    #    单字也允许：别名只来自数据集的**复合名**（remilia_scarlet），
    #    不会像派生键那样与通用英文词重合。
    aliases = _aliased_records(key)
    if aliases:
        _alias_key, record = aliases[0]
        # 别名同样要有质量下限：只凭「唯一候选」就认别名会把普通 tag 拉进来
        # （实测 guitar -> guitar_little_sister，仅 115 热度，却让 "guitar" 变角色）。
        if _record_post_count(record) >= _HEAD_DOMINANCE_MIN_POSTS:
            return _make(record, _SRC_DATASET_ALIAS, stripped)

    # 4) 模糊匹配（最弱证据）：只对**多词** tag 生效。
    #    单词 tag 的容错空间太大（elf -> elf_(dragon's_crown)、stage -> sage），
    #    而所有原生单字角色都已由上面的精确/别名路径覆盖，因此单词 tag 到此为止。
    if " " not in key:
        return None

    matched = _fuzzy_candidate_matches(key, mode)
    if matched is None:
        return None
    record = index.get(matched)
    if record is None:
        return None
    # 模糊命中：输出**数据集里的规范短名**（kita ikuy -> kita_ikuyo），
    # 而不是用户拼错的原文，避免同一个角色因拼写差异落到两个目录。
    return _make(record, _SRC_DATASET_FUZZY, stripped)


def _collect_bare_candidates(texts, bare_mode: str,
                             has_explicit: bool = False,
                             context: _CharacterContext | None = None) -> list[CharacterCandidate]:
    """第二轮：裸名候选（弱证据，需要自己证明自己）。

    显式优先规则：只要提示词里出现**任何显式角色写法**（`char:xxx`、
    `name (series)`、`name \\(series\\)`），就完全不再做裸名判定。

    理由：显式写法已经明确了「这一张画的是谁」，此时提示词里剩下的裸词几乎都是
    服装 / 构图 / 场景 tag。而数据集里存在大量「普通 tag 恰好也是角色名」的条目
    （black_hat_(villainous)、bow_(paper_mario)…），继续跑裸名判定只会把它们
    当成第二个角色，把单人提示词凑成 Duo。

    代价（有意为之）：混写时裸名一律不认。例如
    `char:hikari, rem` 只会得到 hikari，rem 需要写成 `char:rem` 或
    `rem (re:zero)`。要恢复裸名能力，把这里改成只拦「弱候选」即可。
    """
    if bare_mode == _BARE_NAME_MODE_OFF:
        return []
    if has_explicit:
        # §36：这一步也要留痕，否则「一个候选都没有」的提示词无法从日志看出
        # 是「没有候选生成」还是「被显式写法整段抑制」。
        if _debug_enabled():
            _LOGGER.debug(
                "[CharNameSave] bare-name detection skipped: "
                "prompt contains an explicit character form (char: or \"name (series)\")")
        return []
    collected: list[CharacterCandidate] = []
    order = 0
    for _text, tags in _as_tokenised(texts):
        for tag in tags:
            stripped = _strip_weights(tag)
            # 元标签（"kita_ikuyo (cosplay)"）先剥壳，只匹配括号前的裸名字
            meta_name, _meta = _meta_series_name(stripped)
            probe = meta_name or stripped
            if not meta_name and _series_name_text(stripped):
                continue  # 已是 "name (series)"，归第一轮管
            candidate = _lookup_bare_candidate(probe, bare_mode, context)
            if candidate is None:
                continue
            candidate.order = order
            order += 1
            collected.append(candidate)
    return collected


def _select_character_candidates(candidates: list[CharacterCandidate],
                                 context: _CharacterContext) -> list[CharacterCandidate]:
    """评分 → 过滤 → 裁决 → 去重，返回按首次出现顺序排列的最终候选。"""
    for candidate in candidates:
        candidate.score = _score_character_candidate(candidate, context)
    if _debug_enabled():
        # 诊断：候选已打完分，ACCEPT/REJECT 就是最终裁决，正好定位「评分 vs 裁决」。
        # 关闭时整个循环（含每次函数调用）都不会执行。
        for candidate in candidates:
            _log_candidate_debug(candidate, _accept_candidate(candidate))
    accepted = [c for c in candidates if _accept_candidate(c)]

    # 规范化去重：同一个规范名只保留最高分（并列时保留先出现的）
    best: dict[str, CharacterCandidate] = {}
    order: dict[str, int] = {}
    for candidate in accepted:
        canonical = candidate.canonical or candidate.name
        key = _dataset_key(canonical)
        if not key:
            continue
        current = best.get(key)
        if current is None or candidate.score > current.score:
            best[key] = candidate
            order[key] = candidate.order
    return sorted(best.values(), key=lambda c: order[_dataset_key(c.canonical or c.name)])


def _auto_candidates(texts, bare_mode=_BARE_NAME_MODE_OFF):
    """Character names guessed from "name (series)" tags, artist tags skipped.

    重构后为「收集 → 评分 → 过滤 → 裁决 → 规范化 → 去重」五个阶段：
    1. 收集显式候选（char: / name (series)）——强证据；
    2. 收集裸名候选（数据集精确 / 别名 / 模糊）——弱证据；
    3. 用显式角色与 "name (series)" 抽出作品名上下文；
    4. 评分后裁决：弱证据必须自己达到门槛，且显式作品名对不上就丢弃；
    5. 规范化去重，最后再做一轮「非逗号分隔整段文本」的回退扫描。

    输出仍是角色名字符串序列（对外 API 不变），顺序沿用首次出现顺序。
    """
    tokenised = _iter_tokens(texts)

    explicit = _collect_explicit_candidates(texts, bare_mode)
    for candidate in explicit:
        _fill_candidate_dataset_meta(candidate)
    context = _build_character_context(explicit, tokenised)
    explicit = _apply_series_guard(explicit)

    has_explicit = bool(explicit) or _has_char_marker(texts)
    bare = _collect_bare_candidates(texts, bare_mode, has_explicit=has_explicit,
                                    context=context)

    selected = _select_character_candidates(explicit, context)
    selected += [c for c in _select_character_candidates(bare, context)
                 if _dataset_key(c.canonical or c.name)
                 not in {_dataset_key(s.canonical or s.name) for s in selected}]

    seen: set[str] = set()
    for candidate in selected:
        # 输出候选自身的名字（显式写法保留 "name (series)"，裸名用短名），
        # canonical 只用于去重与上下文，不参与输出。
        key = _dataset_key(candidate.canonical or candidate.name)
        if key and key not in seen and candidate.name not in seen:
            seen.add(key)
            seen.add(candidate.name)
            yield candidate.name

    # 非逗号分隔整段文本的回退扫描
    for _text, tags in tokenised:
        cleaned = ", ".join(t for t in tags if not _is_artist_tag(_unescape_tag(t)))
        for match in _SERIES_NAME_RE.finditer(cleaned):
            name = _character_name_from_tag(match.group(0))
            if not name:
                continue
            key = _dataset_key(name)
            if key and key not in seen:
                seen.add(key)
                yield name
