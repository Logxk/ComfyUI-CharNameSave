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
# 模糊匹配最低长度：**第三轮修复**——"stage" 被误匹配成角色 "sage" 的触发案例，
# 短标签必须直接排除，避免 4 字以下的通用词参与模糊匹配。
_BARE_NAME_MIN_LEN = 4
# 模糊匹配阈值（difflib.get_close_matches 的 cutoff，越大越严格）。
# 第三轮从 0.85 提到 0.92：stage/sage 的相似度约 0.888，0.85 会命中、0.92 不会。
_FUZZY_CUTOFF = 0.92
# 模糊匹配的长度容差：候选角色名与 tag 的长度差超过这个值就不算匹配
_FUZZY_MAX_LEN_DIFF = 1
# 通用 tag / 场景词停用表：这些词**永不参与模糊匹配**（精确匹配仍然有效）。
# 第三轮修复的核心之一：stage、live house、guitar 之类的词与角色名形近，
# 例如 "stage" -> "sage"（相似度 8/9 ≈ 0.888）。
# 表里统一用 _dataset_key 归一化后的写法（小写 + 下划线转空格）。
_BARE_NAME_STOPWORDS = frozenset({
    # 与测试提示词相关的场景/动作词（第三轮触发案例）
    "stage", "stage lights", "live house", "livehouse", "guitar",
    "playing guitar", "playing guitar together", "singing", "smiling",
    "blush", "looking at viewer", "looking at each other", "holding hands",
    "standing", "sitting", "walking", "classroom", "park", "beach",
    "night", "sunset", "dynamic angle", "detailed background",
    "school uniform", "casual clothes",
    # 常见通用/构图 tag，同样不该被猜成角色
    "looking back", "looking down", "looking up", "looking away",
    "from above", "from below", "from behind", "from side", "upper body",
    "full body", "cowboy shot", "close-up", "portrait", "outdoors", "indoors",
    "day", "evening", "sky", "clouds", "water", "tree", "trees", "flower",
    "flowers", "window", "bed", "chair", "table", "sky", "simple background",
    "white background", "gradient background", "depth of field", "bokeh",
    "lens flare", "wide shot", "long hair", "short hair", "red hair",
    "pink hair", "blue hair", "yellow hair", "black hair", "white hair",
    "brown hair", "blonde hair", "green hair", "purple hair", "grey hair",
    "silver hair", "orange hair", "hair ornament", "hair bobbles",
    "hair ribbon", "side ponytail", "ponytail", "twintails", "braid",
    "track jacket", "track pants", "pleated skirt", "school bag", "uniform",
    "red ribbon", "ribbon", "jacket", "skirt", "pants", "shirt", "dress",
    "2girls", "3girls", "1girl", "1boy", "2boys", "multiple girls",
    "multiple boys", "solo", "solo focus", "couple", "hetero", "yuri",
    "masterpiece", "best quality", "highres", "absurdres", "ultra detailed",
    "very aesthetic", "official art", "anime style", "photorealistic",
    "artist name", "watermark", "signature", "english text", "japanese text",
})
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

# --- 多角色分组 ----------------------------------------------------------------
# 分组标签**写死**在这里（UI 参数已按第三轮要求移除）：
#   name 数量 == 2 -> _MULTI_GROUP_TAGS[0]（Duo）
#   name 数量 >= 3 -> _MULTI_GROUP_TAGS[1]（Group）
# 用英文目录名，避免中文路径在某些环境 / 外部工具下的兼容性问题
# （例如 zip 编码、git、同步盘、Linux 挂载盘把中文变成乱码）。
# 多角色时子目录/文件名里的角色名会做确定性排序（忽略大小写），因此同一条提示词
# 无论 tag 顺序怎么变，都落在同一个文件夹里，不再产生目录碎片。
# 注意：0 个角色（兜底命名）与 1 个角色（单角色命名）完全不分组，保持旧行为。
_MULTI_GROUP_TAGS = ("Duo", "Group")
# 多角色拼接后的长度上限（与单角色的 150 保持一致，避免超长路径）
_MULTI_NAME_MAX_LEN = 150
# 自动识别角色数量上限：超过这个数量视为异常提示词，只取前 N 个并打 warning
_MAX_AUTO_NAMES = 8


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


def _character_name_from_tag(tag):
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


def _fuzzy_candidate_matches(key, mode):
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
    """
    if mode != _BARE_NAME_MODE_FUZZY:
        return None
    if not key or len(key) < _BARE_NAME_MIN_LEN:
        return None
    if key in _BARE_NAME_STOPWORDS:
        return None

    candidates = get_close_matches(key, _CHARACTER_NAMES_LOWER, n=3, cutoff=_FUZZY_CUTOFF)
    for candidate in candidates:
        if not candidate:
            continue
        # 长度接近
        if abs(len(key) - len(candidate)) > _FUZZY_MAX_LEN_DIFF:
            continue
        # 首字母相同
        if key[0].lower() != candidate[0].lower():
            continue
        # 候选本身也必须达到最低长度（防止数据集里的短别名反向命中）
        if len(candidate) < _BARE_NAME_MIN_LEN:
            continue
        return candidate
    return None


def _dataset_lookup(tag, mode=_BARE_NAME_MODE_EXACT):
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
    record = _CHARACTER_INDEX.get(key)
    if record is not None:
        return record, key
    # --- 2) 去掉 "_(版本名)" 后缀的基础名精确匹配 ---
    base = _DISAMBIG_SUFFIX_RE.sub("", key).strip()
    record = _CHARACTER_INDEX.get(base)
    if record is not None:
        return record, base
    # --- 3) 模糊匹配：带全部约束 ---
    candidate = _fuzzy_candidate_matches(key, mode)
    if candidate is None:
        return None, None
    return _CHARACTER_INDEX.get(candidate), candidate


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


def _resolve_character_name(tag, mode=_BARE_NAME_MODE_OFF):
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


def _character_bare_name(tag, mode):
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

    # 元标签（"kita_ikuyo (cosplay)"）先剥壳，只匹配括号前的裸名字
    meta_name, _meta = _meta_series_name(tag)
    if meta_name:
        return _resolve_character_name(meta_name, mode)

    # 已经是 "name (series)" 的 tag 不归本函数管（元标签已在上面被剥掉）
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
    """
    seen = set()

    def _emit(candidate):
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
        return None

    for text in texts:
        for tag in _iter_tags(text):
            candidate = _emit(_character_name_from_tag(tag))
            if candidate:
                yield candidate

    if bare_mode != _BARE_NAME_MODE_OFF:
        for text in texts:
            for tag in _iter_tags(text):
                candidate = _emit(_character_bare_name(tag, bare_mode))
                if candidate:
                    yield candidate

    # fallback for texts whose tags are not comma separated
    for text in texts:
        cleaned = ", ".join(t for t in _iter_tags(text) if not _is_artist_tag(_unescape_tag(t)))
        for match in _SERIES_NAME_RE.finditer(cleaned):
            candidate = _emit(_character_name_from_tag(match.group(0)))
            if candidate:
                yield candidate


def _dedupe_character_names(names):
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


def _char_names(texts, bare_mode=_BARE_NAME_MODE_OFF):
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


def _group_tag_for_count(name_count):
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


def _clean_path_part(part):
    """清洗路径片段：只处理文件名非法字符，中文等非 ASCII 字符原样保留。"""
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


def _build_save_prefix(names, mode):
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

    # 理论上不会走到这里（2 个及以上一定有分组标签），保留旧拼接命名兜底
    prefix = f"{joined}/{joined}" if folder_mode else joined
    return prefix, f"角色名: {joined}"


# 保存方式选项：第三轮把默认值改为「按角色分组文件夹」，因此它排在第一位
_MODES = ["按角色分组文件夹", "按角色命名文件"]
# 默认保存方式：第三轮起改为「按角色分组文件夹」（用户仍可切到按角色命名文件）
_DEFAULT_MODE = "按角色分组文件夹"
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
                    "default": _DEFAULT_MODE,
                    "label": "保存方式",
                    "tooltip": "按角色分组文件夹（默认）: 在输出目录下按角色名建立文件夹，文件在文件夹内按编号保存；"
                               "按角色命名文件: 以「角色名_编号」命名输出文件。"
                               "识别到多个角色时会自动加一层分组目录（2 人 Duo、3 人及以上 Group）。",
                }),
                "bare_name_mode": (_BARE_NAME_MODES, {
                    "default": _BARE_NAME_MODE_FUZZY,
                    "label": "无作品名角色识别",
                    "tooltip": "识别没有作品名后缀的裸名字 tag（如 Emilia、Rem、frieren）。"
                               "关闭: 只按原有「角色名 (作品名)」规则识别（与最初版本一致）；"
                               "数据集精确匹配: 名字需与内嵌 Danbooru 角色数据集精确命中（忽略大小写与下划线/空格差异）；"
                               "数据集模糊匹配（默认）: 在精确匹配之后允许 difflib 近似匹配，"
                               "但带停用词、最低长度 4、长度差 ≤ 1、首字母相同、cutoff 0.92 等约束，"
                               "避免 stage→sage 这类短词误判。"
                               "数据集位于 data/characters.jsonl，用 download_dataset.py 下载；文件缺失时自动回退原有逻辑。",
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
        "自动提取支持 danbooru 惯例的「角色名 (作品名)」tag；「无作品名角色识别」可用内嵌的"
        "Danbooru 角色数据集（data/characters.jsonl）检索 Emilia、Rem 这类裸名字 tag。"
        "角色数量自动检测：2 个角色归入 Duo 目录、3 个及以上归入 Group 目录（可用英文名以"
        "避免中文路径的兼容性问题），子目录名对角色名做确定性排序，同一提示词顺序变化也落在"
        "同一目录；cosplay 等元标签（(cosplay)、(alternate_costume)…）不会被当成作品名。"
    )

    def save_images(self, images, mode=_DEFAULT_MODE,
                    bare_name_mode=_BARE_NAME_MODE_FUZZY,
                    padding=5, fallback_name="ComfyUI", positive_text=None,
                    prompt=None, extra_pnginfo=None, **legacy):
        # **legacy 兼容旧工作流里遗留的参数（auto_extract / max_tags / character_list /
        # enable_multi_group / group_tags）。它们已从节点面板移除：
        #   auto_extract / enable_multi_group 永久开启；max_tags / character_list 不再生效；
        #   group_tags 写死为 _MULTI_GROUP_TAGS。这里只是接收后忽略，避免旧工作流报错。
        if legacy:
            _LOGGER.debug("忽略已移除的历史参数: %s", sorted(legacy))
        if isinstance(positive_text, str) and positive_text.strip():
            texts = [positive_text]
        else:
            texts = _prompt_texts(prompt)
        # 自动检测角色数量（不再有 max_tags 上限，超过 _MAX_AUTO_NAMES 会打 warning）
        names = _char_names(texts, bare_name_mode)
        # 分组决策：0 个走兜底、1 个保持原样、2 个进 Duo、3 个及以上进 Group
        prefix, display = _build_save_prefix(names, mode)
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
# 例 1：双人提示词（无作品名，需要「无作品名角色识别」= 数据集精确/模糊匹配）
#   提示词: masterpiece, kita_ikuyo, gotoh_hitori, bocchi_the_rock!, 1girl, 2girls
#   结果:   names = ["kita_ikuyo", "gotoh_hitori"]
#           - 文件夹模式（默认）: output/Duo/kita_ikuyo_gotoh_hitori/kita_ikuyo_gotoh_hitori_00001_.png
#           - 文件名模式:         output/Duo_kita_ikuyo_gotoh_hitori_00001_.png
#   顺序无关: 写成 gotoh_hitori, kita_ikuyo 得到的路径完全相同（内部按 str.lower() 排序）。
#   分组目录用英文 Duo / Group，避免中文路径在 zip / git / 挂载盘上的编码兼容性问题。
#
# 例 2：cosplay 提示词（元标签不当作品名）
#   提示词: masterpiece, kita_ikuyo (cosplay), gotoh_hitori (cosplay), alternate_costume
#   结果:   names = ["gotoh_hitori", "kita_ikuyo"]（数据集命中，取规范短名）
#           - 文件夹模式: output/Duo/gotoh_hitori_kita_ikuyo/..._00001_.png
#   若数据集里没有该角色，则退化为括号前的裸名 + _cosplay（如 denia_cosplay）；
#   把 _META_SERIES_SUFFIX 改成 "" 即可去掉后缀（cosplay 图与普通图合并到同一目录）。
#
# 例 3：三人及以上
#   3 个角色 -> output/Group/<排序后的三个名字>/..._00001_.png
#
# 例 4：模糊匹配的约束（第三轮修复 stage -> sage）
#   提示词里的 stage / live house / guitar / looking at each other 等通用词不会参与模糊
#   匹配（_BARE_NAME_STOPWORDS），另外还要求长度 ≥ 4、与候选长度差 ≤ 1、首字母相同、
#   相似度 ≥ 0.92。因此那条 bocchi 双人提示词不会再产出 gotoh_hitori_kita_ikuyo_sage。
#
# 例 5：兼容旧行为
#   bare_name_mode="关闭" 时只识别 "name (series)"（与最初版本一致）；
#   单角色不加任何分组前缀；数据集缺失时只做精确匹配与原有逻辑。
# ============================================================================

def _self_test():
    """极简自测：模糊匹配约束（stage/sage）、多人分组与 cosplay 元标签。

    只依赖纯函数（不写磁盘、不需要 ComfyUI），配合已下载的数据集效果最好：

        F:\\ComfyUI\\venv\\Scripts\\python.exe -c "import sys; sys.path.insert(0, r'F:\\ComfyUI\\custom_nodes\\ComfyUI-CharNameSave'); import __init__ as m; raise SystemExit(m._run_self_test())"

    每项打印「标签: got=… want=…」，全部通过时返回 0。
    """
    exact = _BARE_NAME_MODE_EXACT
    fuzzy = _BARE_NAME_MODE_FUZZY
    checks = []

    def check(label, got, want):
        checks.append((label, got, want))

    def names_of(prompt, mode=exact):
        """识别一条提示词的全部角色名（自动检测数量，无 max_tags）。"""
        return _char_names([prompt], mode)

    # --- 任务 1：模糊匹配不再把 stage 当成 sage ---
    check("stage 命中停用表", "stage" in _BARE_NAME_STOPWORDS, True)
    check("stage lights 命中停用表", "stage lights" in _BARE_NAME_STOPWORDS, True)
    check("stage 不参与模糊匹配", _fuzzy_candidate_matches("stage", fuzzy), None)
    check("stage 不会识别成角色", _character_bare_name("stage", fuzzy), None)
    check("stage lights 不会识别成角色", _character_bare_name("stage lights", fuzzy), None)
    check("停用表外的相似词仍需约束",
          _fuzzy_candidate_matches("stge", fuzzy), None)  # 长度差 > 1
    if _dataset_lookup("sage", exact)[0] is not None:
        # 数据集里确实有 sage 时，stage 必须被拦下（这就是第三轮的 bug 复现用例）
        check("数据集里的 sage 不会被 stage 命中",
              _character_bare_name("stage", fuzzy), None)
    # 真实的错拼纠正仍然有效（同长度、同首字母、cutoff 0.92 以内）
    if _dataset_lookup("kita_ikuyo", exact)[0] is not None:
        check("同长度错拼仍可纠正",
              _character_bare_name("kita_ikuy", fuzzy), "kita_ikuyo")

    # 用户报告的那条提示词：不能出现 sage
    bocchi_prompt = (
        "masterpiece, best quality, highres, 2girls, kita ikuyo, red hair, long hair, "
        "side ponytail, yellow hair ornament, school uniform, red ribbon, pleated skirt, "
        "gotoh hitori, pink hair, long hair, blue hair bobbles, yellow hair bobbles, "
        "pink track jacket, black track pants, guitar, playing guitar together, live house, "
        "stage, stage lights, singing, smiling, looking at each other, holding hands, "
        "dynamic angle, detailed background, bocchi the rock!,"
    )
    reported = names_of(bocchi_prompt, fuzzy)
    check("回归：报告中不含 sage", any("sage" in n for n in reported), False)
    if _dataset_lookup("kita_ikuyo", exact)[0] is not None:
        check("回归：双人仍被识别", sorted(reported), ["gotoh_hitori", "kita_ikuyo"])
        check("回归：双人落在 Duo 目录",
              _build_save_prefix(reported, _DEFAULT_MODE)[0].startswith("Duo/"), True)

    # --- 任务 2：自动检测数量 + 分组（英文目录名）---
    two = names_of("kita_ikuyo, gotoh_hitori, bocchi_the_rock!")
    check("双人识别（顺序 A）", two, ["kita_ikuyo", "gotoh_hitori"])
    check("双人识别（顺序 B，集合相同）",
          sorted(names_of("gotoh_hitori, kita_ikuyo, bocchi_the_rock!")), sorted(two))
    check("双人识别（含重复去重）",
          names_of("kita_ikuyo, gotoh_hitori, kita_ikuyo, bocchi_the_rock!"), two)
    check("文件夹模式前缀", _build_save_prefix(two, _DEFAULT_MODE)[0],
          "Duo/gotoh_hitori_kita_ikuyo")
    check("文件夹模式顺序无关",
          _build_save_prefix(_sorted_character_names(two[::-1]), _DEFAULT_MODE)[0],
          "Duo/gotoh_hitori_kita_ikuyo")
    check("文件名模式前缀", _build_save_prefix(two, "按角色命名文件")[0],
          "Duo_gotoh_hitori_kita_ikuyo")

    three = names_of("kita_ikuyo, gotoh_hitori, ijichi_nijika, bocchi_the_rock!")
    check("多人识别数量", len(three), 3)
    check("多人文件夹前缀", _build_save_prefix(three, _DEFAULT_MODE)[0].split("/")[0], "Group")
    check("分组标签写死为英文", _MULTI_GROUP_TAGS, ("Duo", "Group"))

    # 父子角色去重：name 与 name (series) 同时出现时保留更长的那条
    check("父子去重（保留带作品名的那条）",
          _dedupe_character_names(["denia", "denia (wuthering waves)"]),
          ["denia (wuthering waves)"])
    check("父子去重（大小写不敏感）",
          _dedupe_character_names(["Hakurei_Reimu", "hakurei_reimu_(touhou)"]),
          ["hakurei_reimu_(touhou)"])
    check("父子去重（相同名字只留一条）",
          _dedupe_character_names(["rem", "REM", "rem"]), ["rem"])
    check("超量角色只保留前 8 个",
          len(_char_names([", ".join(f"char:c{i}" for i in range(12))], exact)), _MAX_AUTO_NAMES)

    # --- cosplay 元标签 ---
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
    check("cosplay 提示词整体",
          names_of("gotoh_hitori (cosplay), cosplay, alternate_costume", exact),
          ["gotoh_hitori"])

    # --- 兼容性：关闭数据集识别时只认 "name (series)" ---
    check("关闭模式：只认 name (series)",
          names_of("denia (wuthering waves)"), ["denia_(wuthering_waves)"])
    check("关闭模式：裸名字不猜",
          names_of("kita_ikuyo, gotoh_hitori, bocchi_the_rock!", _BARE_NAME_MODE_OFF), [])
    check("路径片段清洗不动中文", _clean_path_part("双人"), "双人")

    failed = 0
    for label, got, want in checks:
        ok = got == want
        failed += 0 if ok else 1
        print(f"[{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    print(f"self test: {len(checks) - failed}/{len(checks)} passed")
    return failed


def _run_self_test():
    """调用 _self_test() 并打印结果（本地自测用），返回失败项数量。

    注意：本项目是 ComfyUI 自定义节点包，模块名必须是 `__init__`，所以这里不用
    `if __name__ == "__main__"` 做入口（那样会在 ComfyUI / 单元测试里被误触发）。
    运行方式：

        F:\\ComfyUI\\venv\\Scripts\\python.exe -c "import sys; sys.path.insert(0, r'F:\\ComfyUI\\custom_nodes\\ComfyUI-CharNameSave'); import __init__ as m; raise SystemExit(m._run_self_test())"

    或者直接跑正式测试（推荐，断言更全）：

        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_extract.py
        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_bare_name.py
        F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_multi_group.py
    """
    return _self_test()


__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
