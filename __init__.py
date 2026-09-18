import json
import logging
import os
import re
from difflib import get_close_matches

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args

_LOGGER = logging.getLogger(__name__)

_CHAR_TAG_RE = re.compile(r"(?<![a-z0-9_])char:([^<>,\n]+)", re.IGNORECASE)
# danbooru "name (series)" convention, e.g. "denia (wuthering waves)"
_SERIES_NAME_RE = re.compile(r"[\w'\-.]+(?:\s+[\w'\-.]+)*\s*\([^()]*\)")
# a whole tag that is exactly "name (series)"
_SERIES_NAME_TAG_RE = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<series>[^()]*)\)\s*$")
# artist markers, never character names: "@artist" (Anima convention),
# "by artist" / "by (artist:0.5)" / "drawn by artist" wording, "artist: xxx"
_ARTIST_PREFIX_RE = re.compile(
    r"^(?:@|artist\s*[:=]|drawn\s+by(?![a-z0-9])|by(?![a-z0-9]))", re.IGNORECASE)
# whole-tag emphasis wrapper, e.g. "(denia (wuthering waves):1.2)"
_WRAPPED_WEIGHT_RE = re.compile(r"^\(\s*(.+?)\s*:\s*\d+(?:\.\d+)?\s*\)$")
# trailing emphasis weight, e.g. "denia (wuthering waves):1.2"
_WEIGHT_RE = re.compile(r":\s*\d+(?:\.\d+)?\s*$")
# a parenthesised pure number is an emphasis weight, not a series, e.g. "1girl (0.8)"
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?\s*%?$")
# a candidate name part that itself carries an artist marker, e.g. "1girl by" in
# a space separated text "1girl by (ningen mame:0.5)"
_ARTIST_IN_NAME_RE = re.compile(r"(?:^|\s)(?:by|artist)\s*$", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[,;\n\r]+")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# --- 内嵌 Danbooru 角色数据集（无作品名角色识别） ------------------------------
# data/characters.jsonl 由 download_dataset.py 生成（数据来源 Sn0w123/booru-characters），
# 每行一个 JSON 对象，字段见 download_dataset.py 的模块注释：
#   name（角色 tag，danbooru 惯例用下划线，可能带 "_(作品名)" 后缀）
#   copyright（作品名 tag，可能为空串）
#   post_count（热度，用于同名角色消歧）
_DATASET_PATH = os.path.join(os.path.dirname(__file__), "data", "characters.jsonl")

# key: 角色名小写（下划线转空格），value: 原始记录 dict
_CHARACTER_INDEX = {}
# 所有角色名的小写集合（下划线转空格），供精确/模糊匹配
_CHARACTER_NAMES_LOWER = set()
# 精确匹配用：规范化后的 tag -> 原始 dataset name（保住 "_(作品名)" 后缀写法）
_CHARACTER_NAME_LOOKUP = {}
# 模糊匹配 >= n 个字符，避免 "a" / "rei" 这类短词误命中
_FUZZY_MIN_LEN = 3
# 模糊匹配阈值（difflib.get_close_matches 的 cutoff，越大越严格）
_FUZZY_CUTOFF = 0.85
# 尾部 "_xxx)" 或 " xxx)" 后缀：danbooru 用来消歧的 costume/版本名
_DISAMBIG_SUFFIX_RE = re.compile(r"[_\s]\(([^()]*)\)$")
# 数据集里 `name` 本身是否已带 "_(xxx)" 后缀（danbooru 常见消歧写法）
_DISAMBIG_SUFFIX_FULL_RE = re.compile(r"_\([^()]*\)$")

# 无作品名角色识别的三种模式（与节点下拉框取值一致，中文以对齐现有 mode 参数风格）
_BARE_NAME_MODE_OFF = "关闭"
_BARE_NAME_MODE_EXACT = "数据集精确匹配"
_BARE_NAME_MODE_FUZZY = "数据集模糊匹配"
_BARE_NAME_MODES = [_BARE_NAME_MODE_OFF, _BARE_NAME_MODE_EXACT, _BARE_NAME_MODE_FUZZY]

# --- 元标签（meta tag）：不是作品名，绝不能被当成 "name (series)" 里的 series ---------
# danbooru 里 "(cosplay)" 这类后缀描述的是「这张图画的是什么玩法/服装」，不是作品名。
# 典型误判：提示词 "gotoh_hitori (cosplay), cosplay, alternate_costume" 以前会产出
# "gotoh_hitori_(cosplay)_00001_.png"（把 cosplay 当作品名写进文件名）。
# 命中这张表的 tag 会被改判为「裸角色名 + 可选的 _cosplay 后缀」。
_META_SERIES = frozenset({
    "cosplay", "cosplaying", "alternate_costume", "alternate_costumes",
    "clothing_swap", "crossdressing", "cosplay_costume",
})
# 命中的 `<name> (meta)` 是否追加 "_cosplay" 后缀：保留 cosplay 语义，便于和普通图分开
# 查找；若你的工作流希望 cosplay 图与普通图落到同一个文件夹，把下面改成 False 即可。
_META_SERIES_SUFFIX = "_cosplay"
# 元标签场景下无法判定是角色名时的兜底策略：
# True  -> 仍然采用括号前的裸名（把 "denia (cosplay)" 记为 denia_cosplay）
# False -> 直接放弃该 tag（宁可走兜底名称也不猜）
_META_SERIES_KEEP_NAME = True
# 裸名字至少这么长才允许作为元标签场景的兜底（避免 "a (cosplay)" 之类噪音）
_META_SERIES_MIN_LEN = 3
# 匹配 "<name> (<meta>)" / "<name>_(<meta>)"（含可选尾部 ":权重"）
_META_SERIES_TAG_RE = re.compile(r"^(?P<name>[^()]+?)[_\s]*\(\s*(?P<meta>[^()]*?)\s*\)$")

# 匹配裸名字（判断兜底是否像个人名）：字母/数字/下划线/连字符/空格/点
_PLAIN_NAME_RE = re.compile(r"^[\w'\-.\s]+$", re.UNICODE)

# --- 多角色分组（问题 1） ------------------------------------------------------
# name 数量 -> 外层分组目录：2 个角色进「双人」，3 个及以上进「多人」。
# 多角色时子目录/文件名里的角色名会做确定性排序（忽略大小写），因此同一条提示词
# 无论 tag 顺序怎么变，都落在同一个文件夹里，不再产生目录碎片。
# 注意：0 个角色（兜底命名）与 1 个角色（单角色命名）完全不分组，保持旧行为。
_MULTI_GROUP_PARENTS = {2: "双人", 3: "多人"}
_MULTI_GROUP_FALLBACK = ["双人", "多人"]
# 多角色拼接后的长度上限（与单角色的 150 保持一致，避免超长路径）
_MULTI_NAME_MAX_LEN = 150


def _dataset_key(name):
    """角色名 -> 匹配键：小写 + 下划线转空格（danbooru 标签与提示词写法对齐）。"""
    return name.lower().replace("_", " ").strip()


def _load_character_index(path=_DATASET_PATH):
    """读取 JSONL 数据集，填充 _CHARACTER_INDEX / _CHARACTER_NAMES_LOWER。

    文件不存在、为空或格式非法时都只打日志，绝不影响插件运行：此时索引为空，
    精确/模糊匹配永不命中，自动回退到原有的 "name (series)" 识别逻辑。
    """
    _CHARACTER_INDEX.clear()
    _CHARACTER_NAMES_LOWER.clear()
    _CHARACTER_NAME_LOOKUP.clear()
    if not os.path.isfile(path):
        message = f"角色数据集不存在，已跳过加载（无作品名角色识别不可用）: {path}"
        try:
            _LOGGER.warning(message)
        except Exception:  # noqa: BLE001 - 日志异常不能拖垮插件
            print(f"[CharNameSave] {message}")
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
            _CHARACTER_NAMES_LOWER.add(key)
            # 同名多条（如 hatsune_miku 与其各种 costume 版本）保留热度最高的那条
            previous = _CHARACTER_INDEX.get(key)
            if previous is None or record.get("post_count", 0) > previous.get("post_count", 0):
                _CHARACTER_INDEX[key] = record
                _CHARACTER_NAME_LOOKUP[key] = name
            # danbooru 的角色 tag 大量写成 "name_(作品名)"（如 emilia_(re:zero)），
            # 而提示词里往往只写裸名 "emilia"，所以额外把「去掉括号后缀的基础名」
            # 也作为精确匹配键。若发生冲突（rem_(re:zero) / rem_(death_note)），
            # 仍然保留 post_count 更高的那个；数据集中没有裸名条目时才建立这个键。
            base_key = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
            if base_key and base_key != key and base_key not in _CHARACTER_INDEX:
                _CHARACTER_INDEX[base_key] = record
                _CHARACTER_NAME_LOOKUP[base_key] = name
                # 基础名同样进入「已知名字集合」：精确/模糊匹配都要能用，
                # 并且 _dataset_short_name 靠它判断能不能安全地只用短名。
                _CHARACTER_NAMES_LOWER.add(base_key)
            loaded += 1

    if bad_lines:
        _LOGGER.warning("角色数据集有 %d 行无法解析，已跳过。", bad_lines)
    if not loaded:
        _LOGGER.warning("角色数据集为空，已跳过加载: %s", path)
    else:
        _LOGGER.info("已加载角色数据集 %d 条（%s）。", loaded, path)
    return loaded


def _character_output_name(record):
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


def _sanitize_name(name):
    name = _BAD_NAME_RE.sub("_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name).strip("_.")
    return name[:60]


def _prompt_texts(prompt):
    texts = []
    for node in (prompt or {}).values():
        text = node.get("inputs", {}).get("text")
        if isinstance(text, str) and text.strip() and text not in texts:
            texts.append(text)
    return texts


def _iter_tags(text):
    for part in _TAG_SPLIT_RE.split(text or ""):
        tag = part.strip()
        if tag:
            yield tag


def _unescape_tag(tag):
    return tag.replace("\\(", "(").replace("\\)", ")")


def _strip_weights(tag):
    """剥离外层强调括号与尾部 ":权重"，返回裸 tag（已反转义、已去空白）。

    与 _character_name_from_tag 用同一套正则，保证两种识别路径看到的 tag 一致，
    例如 "(denia (wuthering waves):1.2)" -> "denia (wuthering waves)"。
    """
    tag = _unescape_tag(tag or "").strip()
    for _ in range(2):
        wrapped = _WRAPPED_WEIGHT_RE.match(tag)
        if not wrapped:
            break
        tag = wrapped.group(1).strip()
    return _WEIGHT_RE.sub("", tag).strip()


def _is_artist_tag(tag):
    return bool(_ARTIST_PREFIX_RE.match(tag))


def _series_name_text(tag):
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


def _character_name_from_tag(tag, character_list=None):
    """从单个 tag 提取角色名，返回 "name (series)"（已清洗），拿不到返回 None。

    两种分支：
    1. **元标签分支**：tag 形如 "<name> (cosplay)" / "<name>_(alternate_costume)"
       （括号里是 _META_SERIES 里的元标签）时，绝不把 cosplay 当作品名。改为：
       先用 _resolve_character_name 做名单/数据集校验（命中就用数据集里的规范短名，
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
        resolved = _resolve_character_name(meta_name, _BARE_NAME_MODE_OFF, character_list)
        if resolved:
            # 数据集 / 手动名单命中（名单里的人名长度不受 _META_SERIES_MIN_LEN 限制，
            # 因为用户是显式声明的）；只有「凭感觉猜」的兜底才要求名字足够长。
            return resolved
        # 数据集/名单都没命中：兜底保留括号前的裸名 + cosplay 后缀
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
    candidate = _character_name_from_tag(tag, character_list)
    if candidate:
        return candidate
    stripped = _strip_weights(tag)
    if list_lookup:
        listed = list_lookup.get(stripped.lower())
        if listed:
            return _sanitize_name(listed)
    if mode == _BARE_NAME_MODE_OFF:
        return None
    return _character_bare_name(tag, mode, character_list)


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


def _character_list_names(character_list):
    """把用户手填的「已知角色名名单」规范化成 (小写名 -> 原始名) 映射。"""
    lookup = {}
    for item in character_list or []:
        if not isinstance(item, str):
            continue
        name = _unescape_tag(item).strip()
        if not name or _is_artist_tag(name):
            continue
        lookup.setdefault(name.lower(), name)
    return lookup


def _dataset_lookup(tag, mode=_BARE_NAME_MODE_EXACT):
    """在数据集索引里查 tag，返回 (原始记录 dict, 命中的索引键)，未命中返回 (None, None)。

    匹配顺序：
    1. 完整 tag（"hakurei reimu (touhou)"，下划线/空格等价）；
    2. 去掉尾部 "_(版本名/服装名)" 后缀的基础名（"hakurei reimu"，用于裸名识别）；
    3. 仅当 mode == 数据集模糊匹配 时，再按 difflib 近似匹配（cutoff 0.85，
       长度 < _FUZZY_MIN_LEN 的 tag 不参与，避免 "a" 这类短词误命中）。

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

    record = _CHARACTER_INDEX.get(key)
    if record is not None:
        return record, key
    base = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
    record = _CHARACTER_INDEX.get(base)
    if record is not None:
        return record, base
    if mode != _BARE_NAME_MODE_FUZZY or len(key) < _FUZZY_MIN_LEN:
        return None, None
    matches = get_close_matches(key, _CHARACTER_NAMES_LOWER, n=1, cutoff=_FUZZY_CUTOFF)
    if not matches:
        return None, None
    return _CHARACTER_INDEX.get(matches[0]), matches[0]


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


def _resolve_character_name(tag, mode=_BARE_NAME_MODE_OFF, character_list=None):
    """把一个「开放写法」的**裸名字** tag 解析成角色名（优先返回短名，不带作品名）。

    参数 tag 应当已经剥掉元标签/作品名（元标签场景由 _meta_series_name 先剥壳）；
    这里只做「这个名字是哪个角色」的判定，不做括号解析、不做轮次编排。

    这是无作品名识别 / 元标签识别的**共用作答函数**，只负责判定，不做轮次编排，
    因此 _character_name_from_tag（元标签分支）与 _character_bare_name（第三轮）
    可以复用同一套判定而不互相递归：

    1. 手动名单（大小写不敏感）—— 用户填什么就用什么；
    2. 内嵌 Danbooru 数据集（mode 决定是否启用模糊匹配）：命中后按「短名优先」返回
       （见 _dataset_short_name），这样多角色拼接与 cosplay 场景不会出现
       "kita_ikuyo_(bocchi_the_rock!)_gotoh_hitori_(bocchi_the_rock!)" 这种超长组合；
    3. 都没命中且 mode == 数据集模糊匹配 时，_dataset_lookup 内部会再试 difflib。

    未命中一律返回 None，保证数据集缺失时行为与旧版一致。
    """
    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None

    # 1) 手动名单
    lookup = _character_list_names(character_list)
    if lookup:
        listed = lookup.get(tag.lower())
        if listed:
            return _sanitize_name(listed)

    # 2) 数据集：精确 -> 去后缀基础名 ->（模糊模式）difflib
    dataset_mode = mode if mode in _BARE_NAME_MODES else _BARE_NAME_MODE_EXACT
    record, matched_key = _dataset_lookup(tag, dataset_mode)
    if record is None:
        return None

    # 3) 短名优先（多角色/cosplay 场景的关键）
    return _dataset_short_name(tag, record, matched_key)


def _character_bare_name(tag, mode, character_list=None):
    """无作品名角色识别：判断纯名字 tag（如 "Emilia" / "rem"）是否为已知角色。

    识别顺序：手动名单（若有）-> 数据集完整 tag 精确匹配 -> 去掉 "_(版本名/服装名)"
    后缀的基础名精确匹配 -> （模糊模式）difflib 近似匹配。命中则返回清洗后的短名，
    未命中返回 None。数据集为空集合时永不命中，等同于关闭。

    已经是 "name (series)"（含 danbooru 的 name_(series) 写法）的 tag 交给第一轮处理；
    但 "<name> (cosplay)" 这类**元标签**除外：这里会先剥掉元标签，只拿 <name> 去匹配，
    因为 (cosplay) 不是作品名（见 _meta_series_name / _META_SERIES）。
    """
    if mode not in _BARE_NAME_MODES or mode == _BARE_NAME_MODE_OFF:
        return None
    if not _CHARACTER_NAMES_LOWER and not character_list:
        return None

    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None

    # 元标签（"kita_ikuyo (cosplay)"）先剥壳，只匹配括号前的裸名字
    meta_name, _meta = _meta_series_name(tag)
    if meta_name:
        return _resolve_character_name(meta_name, mode, character_list)

    # 已经是 "name (series)" 的 tag 不归本函数管（元标签已在上面被剥掉）
    if _series_name_text(tag):
        return None
    return _resolve_character_name(tag, mode, character_list)


def _auto_candidates(texts, bare_mode=_BARE_NAME_MODE_OFF, character_list=None):
    """Character names guessed from "name (series)" tags, artist tags skipped.

    四轮，先命中先用（重复候选只保留第一次，保证顺序稳定）：
    1. 每个 tag 的原有 "name (series)" 提取。注意 `<name> (cosplay)` 这类元标签
       由 _character_name_from_tag 的元标签分支处理（返回裸名 + _cosplay 后缀），
       因此不会把 cosplay 写成作品名；
    2. 手动名单 character_list（大小写不敏感）——若有；
    3. bare_mode 打开时：数据集精确/模糊匹配纯名字 tag（第二轮没命中的 tag 才会走到）；
    4. 原有回退扫描 _SERIES_NAME_RE（处理非逗号分隔的整段文本）。
    """
    seen = set()

    def _emit(candidate):
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
        return None

    for text in texts:
        for tag in _iter_tags(text):
            candidate = _emit(_character_name_from_tag(tag, character_list))
            if candidate:
                yield candidate

    list_lookup = _character_list_names(character_list)
    for text in texts:
        for tag in _iter_tags(text):
            # 第二轮走 _candidate_from_tag：先原有逻辑，再名单，最后数据集裸名
            # （bare_mode 为「关闭」时不会启用数据集那一步）
            candidate = _emit(_candidate_from_tag(tag, bare_mode, list_lookup, character_list))
            if candidate:
                yield candidate

    # fallback for texts whose tags are not comma separated
    for text in texts:
        cleaned = ", ".join(t for t in _iter_tags(text) if not _is_artist_tag(_unescape_tag(t)))
        for match in _SERIES_NAME_RE.finditer(cleaned):
            candidate = _emit(_character_name_from_tag(match.group(0), character_list))
            if candidate:
                yield candidate


def _char_names(texts, auto_extract, max_tags, bare_mode=_BARE_NAME_MODE_OFF, character_list=None):
    names = []
    for text in texts:
        for match in _CHAR_TAG_RE.findall(text):
            name = _sanitize_name(match)
            if name and name not in names:
                names.append(name)
    if not names and auto_extract:
        for candidate in _auto_candidates(texts, bare_mode, character_list):
            if candidate not in names:
                names.append(candidate)
            if len(names) >= max_tags:
                break
    return names


def _split_character_list(character_list):
    """把节点里的多行名单字符串切成列表（逗号、分号、换行都算分隔符）。"""
    if isinstance(character_list, str):
        parts = _TAG_SPLIT_RE.split(character_list)
    elif isinstance(character_list, (list, tuple, set)):
        parts = []
        for item in character_list:
            parts.extend(_TAG_SPLIT_RE.split(item) if isinstance(item, str) else [])
    else:
        return []
    return [part.strip() for part in parts if part and part.strip()]


def _parse_group_tags(group_tags):
    """解析「分组标签」参数，返回至少 2 项的分组标签列表。

    规则（与 tooltip 一致）：按逗号切分、去掉空白；第 1 项给双人、第 2 项给多人
    （多写的第 3 项起忽略）；为空或不足 2 项时整体回退到默认值 ["双人", "多人"]。
    标签本身不做 _sanitize_name，中文原样保留；只在装配路径时用 _clean_path_part
    去掉非法字符，避免破坏中文。
    """
    parts = [part.strip() for part in _TAG_SPLIT_RE.split(group_tags or "") if part.strip()]
    if len(parts) < 2:
        return list(_MULTI_GROUP_FALLBACK)
    return parts


def _group_tag_for_count(name_count, group_tags, enable_multi_group):
    """决定多角色分组的外层标签；不需要分组时返回 None。

    判定：enable_multi_group 关闭 -> None（回退旧的多角色拼接命名）；
    name_count == 2 -> 分组标签第 1 项（默认「双人」）；
    name_count >= 3 -> 分组标签第 2 项（默认「多人」）。
    0 个（兜底命名）与 1 个（单角色）角色永远不分组，保持旧行为。
    """
    if not enable_multi_group:
        return None
    if name_count == 2:
        return group_tags[0]
    if name_count >= 3:
        return group_tags[1]
    return None


def _clean_path_part(part):
    """清洗路径片段：只处理文件名非法字符，**不动中文**（双人 / 多人原样保留）。"""
    cleaned = _BAD_NAME_RE.sub("_", (part or "").strip())
    return re.sub(r"_+", "_", cleaned).strip("_.")


def _sorted_character_names(names):
    """对多角色名字做确定性排序，返回排序后的新列表。

    排序键为 `str.lower()`（大小写不敏感），因此同一条提示词无论 tag 顺序如何变化，
    结果都完全一致；返回的仍是原始大小写的角色名，不做任何改写。
    """
    return sorted(names, key=str.lower)


def _join_character_names(names):
    """用 _ 连接多个角色名，并整体截断到 _MULTI_NAME_MAX_LEN，避免超长路径。

    截断只在超长时发生，并且会去掉截断产生的尾部 "_"。
    """
    joined = "_".join(names)
    if len(joined) > _MULTI_NAME_MAX_LEN:
        joined = joined[:_MULTI_NAME_MAX_LEN].rstrip("_")
    return joined


def _build_save_prefix(names, mode, enable_multi_group=True, group_tags=None):
    """把识别结果组装成 get_save_image_path 需要的 prefix。

    返回 (prefix, display)。display 是写回前端的提示文本。

    分支：
    1. names 为空 -> 兜底名称（单段 prefix，旧行为）；
    2. 1 个角色 -> 「角色名」/「角色名/角色名」（旧行为，绝不加「单人」前缀）；
    3. 2 个及以上且开启多人分组 -> 外层 双人/多人：
       - 文件夹模式 prefix = "双人/名字A_名字B"（每个组合一个子文件夹，内部连续编号）；
       - 文件名模式 prefix = "双人_名字A_名字B"（文件名里带分组前缀）；
       名字列表先做确定性排序（见 _sorted_character_names），因此顺序无关；
    4. 2 个及以上且关闭多人分组 -> 旧的多角色拼接命名（兼容旧工作流）。
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
    group_tag = _group_tag_for_count(len(names), group_tags or _MULTI_GROUP_FALLBACK,
                                     enable_multi_group)
    folder_mode = _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹"

    if group_tag:
        parent = _clean_path_part(group_tag) or group_tag
        if folder_mode:
            # 文件夹模式：外层分组 + 角色组合子文件夹（前缀只到目录，文件名由编号生成）
            prefix = f"{parent}/{joined}"
            return prefix, f"角色名: {prefix}"
        # 文件名模式：文件名前缀带分组标签，如 双人_kita_ikuyo_gotoh_hitori
        prefix = f"{parent}_{joined}"
        return prefix, f"角色名: {prefix}"

    # 关闭多人分组：保留旧的多角色拼接命名（仅排序以保证顺序无关）
    prefix = f"{joined}/{joined}" if folder_mode else joined
    return prefix, f"角色名: {joined}"


_MODES = ["按角色命名文件", "按角色分组文件夹"]
# legacy English values saved by earlier versions, kept for workflow compatibility
_MODE_ALIASES = {"filename": "按角色命名文件", "folder": "按角色分组文件夹"}


class CharNameSaveImage:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.compress_level = 4

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE", {"label": "图像"}),
                "mode": (_MODES + list(_MODE_ALIASES), {
                    "default": "按角色命名文件",
                    "label": "保存方式",
                    "tooltip": "按角色命名文件: 以「角色名_编号」命名输出文件；按角色分组文件夹: 在输出目录下按角色名建立文件夹，文件在文件夹内按编号保存。",
                }),
                "auto_extract": ("BOOLEAN", {
                    "default": True,
                    "label": "自动提取角色名",
                    "tooltip": "提示词中没有 char:角色名 标记时，识别「角色名 (作品名)」结构的 tag（danbooru 惯例）。会自动跳过画师 tag（@画师、by 画师、by (画师:权重)、drawn by 画师、artist: 画师）。",
                }),
                "max_tags": ("INT", {
                    "default": 1, "min": 1, "max": 10,
                    "label": "角色名数量上限",
                    "tooltip": "自动提取时最多采用的角色名数量，多个名字用 _ 连接；识别到 2 个及以上时按「多人自动分组」处理。多角色建议用 char: 角色名 显式标记。",
                }),
                "bare_name_mode": (_BARE_NAME_MODES, {
                    "default": _BARE_NAME_MODE_OFF,
                    "label": "无作品名角色识别",
                    "tooltip": "识别没有作品名后缀的裸名字 tag（如 Emilia、Rem、frieren）。关闭: 只按原有「角色名 (作品名)」规则识别；数据集精确匹配: 名字需与内嵌 Danbooru 角色数据集命中（忽略大小写与下划线/空格差异，输出数据集里的短名，如 emilia）；数据集模糊匹配: 在精确匹配基础上允许 difflib 近似匹配（cutoff 0.85），可容忍拼写/空格差异，但可能误判。数据集位于 data/characters.jsonl，用 download_dataset.py 下载；文件缺失时三种模式都自动回退到原有逻辑。",
                }),
                "character_list": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "label": "已知角色名名单",
                    "tooltip": "可选的手动补充名单（大小写不敏感），用逗号、分号或换行分隔。名单里的纯名字 tag 也会被识别为角色名，作为数据集之外的补充；留空则只依赖数据集与原有规则。",
                }),
                "enable_multi_group": ("BOOLEAN", {
                    "default": True,
                    "label": "多人自动分组",
                    "tooltip": "开启后，识别到 2 个角色时归入「双人」文件夹，3 个及以上归入「多人」文件夹；关闭则沿用旧的多角色拼接命名。",
                }),
                "group_tags": ("STRING", {
                    "default": "双人,多人",
                    "label": "分组标签",
                    "tooltip": "两个逗号分隔的标签，依次用于双人、多人分组。默认：双人,多人。",
                }),
                "padding": ("INT", {
                    "default": 5, "min": 1, "max": 8,
                    "label": "编号位数",
                    "tooltip": "编号位数，例如 5 生成 00001。",
                }),
                "fallback_name": ("STRING", {
                    "default": "ComfyUI",
                    "label": "兜底名称",
                    "tooltip": "识别不到角色名时的名称前缀；默认 ComfyUI 即核心默认命名格式（ComfyUI_00001_.png）。",
                }),
            },
            "optional": {
                "positive_text": ("STRING", {
                    "forceInput": True,
                    "label": "正向提示词",
                    "tooltip": "连接最终合成的正向提示词文本（如 AnimaPromptPlus 的 text 输出）。连接后仅用该文本提取角色名，忽略工作流里其它文本节点。",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "image"
    DESCRIPTION = (
        "根据提示词中的角色名 tag（char:xxx 标记，或自动提取）生成文件名/文件夹来保存图像。"
        "自动提取支持 danbooru 惯例的「角色名 (作品名)」tag；开启「无作品名角色识别」后，"
        "还会用内嵌的 Danbooru 角色数据集（data/characters.jsonl）检索 Emilia、Rem 这类"
        "没有作品名的裸名字 tag。开启「多人自动分组」后，识别到 2 个角色会归入「双人」、"
        "3 个及以上归入「多人」，子文件夹名对角色名做确定性排序，同一提示词顺序变化也落在"
        "同一目录；cosplay 等元标签（(cosplay)、(alternate_costume)…）不会被当成作品名。"
    )

    def save_images(self, images, mode="按角色命名文件", auto_extract=True, max_tags=1,
                    bare_name_mode=_BARE_NAME_MODE_OFF, character_list="",
                    enable_multi_group=True, group_tags="双人,多人",
                    padding=5, fallback_name="ComfyUI", positive_text=None,
                    prompt=None, extra_pnginfo=None):
        if isinstance(positive_text, str) and positive_text.strip():
            texts = [positive_text]
        else:
            texts = _prompt_texts(prompt)
        names = _char_names(texts, auto_extract, max_tags, bare_name_mode,
                            _split_character_list(character_list))
        # 分组决策：0 个走兜底、1 个保持原样、2 个进「双人」、3 个及以上进「多人」
        prefix, display = _build_save_prefix(
            names, mode, enable_multi_group, _parse_group_tags(group_tags))
        if prefix is None:
            name = (_sanitize_name(fallback_name) or "ComfyUI")[:150]
            prefix = f"{name}/{name}" if _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹" else name
            display = f"未识别到角色名，使用默认命名: {name}"

        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        for (batch_number, image) in enumerate(images):
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            metadata = None
            if not args.disable_metadata:
                metadata = PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for x in extra_pnginfo:
                        metadata.add_text(x, json.dumps(extra_pnginfo[x]))
            file = f"{filename}_{counter:0{padding}d}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=self.compress_level)
            results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            counter += 1

        return {"ui": {"images": results, "text": [display]}, "result": (images,)}


NODE_CLASS_MAPPINGS = {"CharNameSaveImage": CharNameSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {"CharNameSaveImage": "Save Image by Char Tag（按角色名保存）"}

# 模块加载时加载一次角色数据集：文件缺失/损坏只会打 warning，索引为空时自动
# 回退到原有的 "name (series)" 识别逻辑，不影响插件运行。
_load_character_index()

# ============================== 使用示例 =====================================
#
# 例 1：双人提示词（无作品名，需要 bare_name_mode 打开数据集检索）
#   提示词: masterpiece, kita_ikuyo, gotoh_hitori, bocchi_the_rock!, 1girl, 2girls
#   参数:   无作品名角色识别 = 数据集精确匹配（角色名数量上限 >= 2）
#   结果:   names = ["kita_ikuyo", "gotoh_hitori"]
#           - 文件夹模式: output/双人/kita_ikuyo_gotoh_hitori/kita_ikuyo_gotoh_hitori_00001_.png
#           - 文件名模式: output/双人_kita_ikuyo_gotoh_hitori_00001_.png
#   顺序无关: 写成 gotoh_hitori, kita_ikuyo 得到的路径完全相同（内部按 str.lower() 排序）。
#
# 例 2：cosplay 提示词（元标签不当作品名）
#   提示词: masterpiece, kita_ikuyo (cosplay), gotoh_hitori (cosplay), alternate_costume
#   结果:   names = ["gotoh_hitori_cosplay", "kita_ikuyo_cosplay"]（后缀保留 cosplay 语义）
#           - 文件夹模式: output/双人/gotoh_hitori_cosplay_kita_ikuyo_cosplay/..._00001_.png
#   若数据集里没有该角色，则退化为括号前的裸名 + _cosplay（如 denia_cosplay）；
#   把 _META_SERIES_SUFFIX 改成 "" 即可去掉后缀（cosplay 图与普通图合并到同一目录）。
#
# 例 3：三人及以上
#   3 个角色 -> output/多人/<排序后的三个名字>/..._00001_.png
#
# 例 4：兼容旧行为
#   enable_multi_group=False 时，多角色回到旧的拼接命名（单角色/兜底命名不受影响）；
#   bare_name_mode="关闭" 且只有 1 个角色时，输出与本插件旧版本逐字节一致。
# ============================================================================

def _self_test():
    """极简自测：验证多角色分组、排序稳定性与 cosplay 元标签判定。

    只依赖纯函数（不写磁盘、不需要 ComfyUI），直接运行本文件即可：

        python __init__.py
        F:\\ComfyUI\\venv\\Scripts\\python.exe __init__.py

    输出每项的 "期望 -> 实际"，全部通过时打印 OK。
    """
    def names_of(prompt, mode, limit=10):
        return _char_names([prompt], True, limit, mode)

    exact = _BARE_NAME_MODE_EXACT
    checks = []

    def check(label, got, want):
        checks.append((label, got, want))

    # --- 问题 1：多角色分组 + 顺序无关 ---
    two = names_of("kita_ikuyo, gotoh_hitori, bocchi_the_rock!", exact)
    check("双人识别（顺序 A）", two, ["kita_ikuyo", "gotoh_hitori"])
    # 提取顺序跟随 tag 顺序（这是 _char_names 的既有行为），分组只看排序后的结果
    check("双人识别（顺序 B，集合相同）",
          sorted(names_of("gotoh_hitori, kita_ikuyo, bocchi_the_rock!", exact)), sorted(two))
    check("双人识别（含重复去重）",
          names_of("kita_ikuyo, gotoh_hitori, kita_ikuyo, bocchi_the_rock!", exact), two)

    folder = "按角色分组文件夹"
    prefix_a = _build_save_prefix(two, folder, True, _parse_group_tags("双人,多人"))[0]
    check("文件夹模式前缀", prefix_a, "双人/gotoh_hitori_kita_ikuyo")
    prefix_b = _build_save_prefix(_sorted_character_names(two[::-1]), folder, True,
                                  _parse_group_tags("双人,多人"))[0]
    check("文件夹模式前缀（顺序无关）", prefix_b, prefix_a)
    check("文件名模式前缀", _build_save_prefix(two, "按角色命名文件", True, None)[0],
          "双人_gotoh_hitori_kita_ikuyo")

    # 3 个角色 -> 多人
    three = names_of("kita_ikuyo, gotoh_hitori, ijichi_nijika, bocchi_the_rock!", exact)
    check("多人识别", len(three), 3)
    check("多人文件夹前缀", _build_save_prefix(three, folder, True, None)[0].split("/")[0], "多人")

    # 关闭多人分组 -> 旧拼接命名
    check("关闭分组回退", _build_save_prefix(two, folder, False, None)[0],
          "gotoh_hitori_kita_ikuyo/gotoh_hitori_kita_ikuyo")

    # --- 问题 2：cosplay 元标签 ---
    check("cosplay 不当作品名", _character_name_from_tag("gotoh_hitori (cosplay)"),
          "gotoh_hitori")
    check("cosplay 下划线写法", _character_name_from_tag("gotoh_hitori_(cosplay)"),
          "gotoh_hitori")
    check("alternate_costume 不当作品名",
          _character_name_from_tag("kita_ikuyo (alternate_costume)"), "kita_ikuyo")
    check("_character_bare_name 剥元标签",
          _character_bare_name("gotoh_hitori (cosplay)", exact), "gotoh_hitori")
    check("未知角色的 cosplay 兜底",
          _character_name_from_tag("someone_unknown (cosplay)"), "someone_unknown_cosplay")
    check("普通作品名不受影响", _character_name_from_tag("denia (wuthering waves)"),
          "denia_(wuthering_waves)")
    check("画师 tag 仍被过滤", _character_name_from_tag("by (ningen mame:0.5)"), None)
    check("cosplay 提示词整体", names_of("gotoh_hitori (cosplay), cosplay, alternate_costume",
                                      exact), ["gotoh_hitori"])

    # --- 兼容性：关闭数据集 + 单角色时与旧版一致 ---
    check("旧逻辑（关闭 + 单角色）",
          names_of("denia (wuthering waves)", _BARE_NAME_MODE_OFF), ["denia_(wuthering_waves)"])
    check("分组标签回退默认", _parse_group_tags(""), list(_MULTI_GROUP_FALLBACK))
    check("分组标签自定义", _parse_group_tags(" Couple , Group "), ["Couple", "Group"])
    check("中文标签不被清洗", _clean_path_part("双人"), "双人")

    failed = 0
    for label, got, want in checks:
        ok = got == want
        failed += 0 if ok else 1
        print(f"[{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    print(f"self test: {len(checks) - failed}/{len(checks)} passed")
    return failed


def _run_self_test():
    """调用 _self_test() 并打印结果（本地自测用）。

    注意：本项目是 ComfyUI 自定义节点包，模块名必须是 `__init__`，所以这里**不能**
    用 `if __name__ == "__main__"` 做入口（那样会在单元测试 / ComfyUI 里被误触发）。
    请用下面任意一种方式运行：

        python -c "import sys; sys.path.insert(0, r'F:\\ComfyUI\\custom_nodes\\ComfyUI-CharNameSave'); import __init__ as m; m._run_self_test()"

    或者直接跑正式测试（推荐，已包含同样断言）：

        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_extract.py
        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_bare_name.py
        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_multi_group.py
    """
    return _self_test()


__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
