"""Character-name matching: exact "name (series)", meta tags, bare names, fuzzy.

Verbatim extraction of the original functions. The only change is that
``_fuzzy_candidate_matches`` memoises its difflib scan behind a small LRU cache
keyed on the dataset version, so repeated prompts skip the O(N) similarity scan.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache

from . import dataset as _dataset_mod
from .constants import (
    _ARTIST_IN_NAME_RE,
    _BARE_NAME_MIN_LEN,
    _BARE_NAME_MODE_EXACT,
    _BARE_NAME_MODE_FUZZY,
    _BARE_NAME_MODE_OFF,
    _BARE_NAME_MODES,
    _BARE_NAME_STOPWORDS,
    _DISAMBIG_SUFFIX_RE,
    _FUZZY_CUTOFF,
    _FUZZY_MAX_LEN_DIFF,
    _META_SERIES,
    _META_SERIES_KEEP_NAME,
    _META_SERIES_MIN_LEN,
    _META_SERIES_SUFFIX,
    _META_SERIES_TAG_RE,
    _NUMBER_RE,
    _PLAIN_NAME_RE,
    _SERIES_NAME_RE,
    _SERIES_NAME_TAG_RE,
    _WEIGHT_RE,
)
from .dataset import (
    _CHARACTER_NAMES_LOWER,
    _character_output_name,
    _dataset_key,
)
from .textparse import (
    _is_artist_tag,
    _iter_tags,
    _sanitize_name,
    _series_name_text,
    _strip_weights,
    _unescape_tag,
)


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


def _auto_candidates(texts, bare_mode=_BARE_NAME_MODE_OFF):
    """Character names guessed from "name (series)" tags, artist tags skipped.

    三轮，先命中先用（重复候选只保留第一次，保证顺序稳定）：
    1. 每个 tag 的原有 "name (series)" 提取。注意 `<name> (cosplay)` 这类元标签
       由 _character_name_from_tag 的元标签分支处理，不会把 cosplay 写成作品名；
    2. bare_mode 打开时：数据集精确 / 模糊匹配纯名字 tag（第一轮没命中的 tag 才走到）；
    3. 原有回退扫描 _SERIES_NAME_RE（处理非逗号分隔的整段文本）。

    这里收集**所有**候选，不再有 max_tags 截断（第三轮改为自动检测角色数量）。

    性能：每段文本只分词一次（原实现三轮各自重新 split 一次），三轮复用同一份
    tag 列表；顺序与去重语义与原来完全一致。
    """
    seen = set()

    def _emit(candidate):
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
        return None

    tokenised = [(text, list(_iter_tags(text))) for text in texts]

    for _text, tags in tokenised:
        for tag in tags:
            candidate = _emit(_character_name_from_tag(tag))
            if candidate:
                yield candidate

    if bare_mode != _BARE_NAME_MODE_OFF:
        for _text, tags in tokenised:
            for tag in tags:
                candidate = _emit(_character_bare_name(tag, bare_mode))
                if candidate:
                    yield candidate

    # fallback for texts whose tags are not comma separated
    for _text, tags in tokenised:
        cleaned = ", ".join(t for t in tags if not _is_artist_tag(_unescape_tag(t)))
        for match in _SERIES_NAME_RE.finditer(cleaned):
            candidate = _emit(_character_name_from_tag(match.group(0)))
            if candidate:
                yield candidate
