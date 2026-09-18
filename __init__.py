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
    """Return "name (series)" from a single prompt tag, or None.

    Artist tags are rejected here so that they never become a file name, e.g.
    "by (ningen mame:0.5)", "@hiten (hitenkei):0.6", "by ningen mame".
    """
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


def _character_bare_name(tag, mode):
    """无作品名角色识别：判断纯名字 tag（如 "Emilia" / "rem"）是否为已知角色。

    识别顺序：数据集完整 tag 精确匹配 -> 去掉 "_(版本名/服装名)" 后缀的基础名精确
    匹配 -> （模糊模式）difflib 近似匹配。命中则返回清洗后的角色名，未命中返回 None。
    数据集为空集合时永不命中，等同于关闭。
    """
    if mode not in _BARE_NAME_MODES or mode == _BARE_NAME_MODE_OFF:
        return None
    if not _CHARACTER_NAMES_LOWER:
        return None

    tag = _strip_weights(tag)
    if not tag or _is_artist_tag(tag):
        return None
    # 已经是 "name (series)"（含 danbooru 的 name_(series) 写法）的 tag 归第一轮处理，
    # 这里只负责裸名字，避免两种识别路径互相抢候选。
    if _series_name_text(tag):
        return None
    key = _dataset_key(tag)
    if not key or key.isdigit():
        return None

    # 精确匹配：索引里同时有完整 tag（hakurei reimu (touhou)）与去后缀的基础名
    # （hakurei reimu），两种写法都能命中，见 _load_character_index。
    record = _CHARACTER_INDEX.get(key)
    if record is None:
        record = _CHARACTER_INDEX.get(_DISAMBIG_SUFFIX_RE.sub("", key).strip())
    if record is not None:
        return _sanitize_name(_character_output_name(record) or tag)

    if mode == _BARE_NAME_MODE_EXACT:
        return None

    # 模糊匹配：cutoff 0.85，过短的 tag 不参与，避免误判
    if len(key) < _FUZZY_MIN_LEN:
        return None
    matches = get_close_matches(key, _CHARACTER_NAMES_LOWER, n=1, cutoff=_FUZZY_CUTOFF)
    if not matches:
        return None
    record = _CHARACTER_INDEX.get(matches[0])
    if record is None:
        return None
    return _sanitize_name(_character_output_name(record) or tag)


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


def _auto_candidates(texts, bare_mode=_BARE_NAME_MODE_OFF, character_list=None):
    """Character names guessed from "name (series)" tags, artist tags skipped.

    四轮，先命中先用（重复候选只保留第一次，保证顺序稳定）：
    1. 现有逻辑：每个 tag 里的 "name (series)"；
    2. 手动名单 character_list（大小写不敏感）——若有；
    3. bare_mode 打开时：数据集精确/模糊匹配纯名字 tag；
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
            candidate = _emit(_character_name_from_tag(tag))
            if candidate:
                yield candidate

    list_lookup = _character_list_names(character_list)
    if list_lookup:
        for text in texts:
            for tag in _iter_tags(text):
                if _character_name_from_tag(tag):
                    continue
                candidate = _emit(_sanitize_name(list_lookup.get(_strip_weights(tag).lower()) or ""))
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
                    "tooltip": "自动提取时最多采用的角色名数量，多个名字用 _ 连接；多角色建议用 char: 角色名 显式标记。",
                }),
                "bare_name_mode": (_BARE_NAME_MODES, {
                    "default": _BARE_NAME_MODE_OFF,
                    "label": "无作品名角色识别",
                    "tooltip": "识别没有作品名后缀的裸名字 tag（如 Emilia、Rem、frieren）。关闭: 只按原有「角色名 (作品名)」规则识别；数据集精确匹配: 名字需与内嵌 Danbooru 角色数据集命中（忽略大小写与下划线/空格差异，并自动补上数据集里的作品名）；数据集模糊匹配: 在精确匹配基础上允许 difflib 近似匹配（cutoff 0.85），可容忍拼写/空格差异，但可能误判。数据集位于 data/characters.jsonl，用 download_dataset.py 下载；文件缺失时三种模式都自动回退到原有逻辑。",
                }),
                "character_list": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "label": "已知角色名名单",
                    "tooltip": "可选的手动补充名单（大小写不敏感），用逗号、分号或换行分隔。名单里的纯名字 tag 也会被识别为角色名，作为数据集之外的补充；留空则只依赖数据集与原有规则。",
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
        "没有作品名的裸名字 tag。"
    )

    def save_images(self, images, mode="按角色命名文件", auto_extract=True, max_tags=1,
                    bare_name_mode=_BARE_NAME_MODE_OFF, character_list="",
                    padding=5, fallback_name="ComfyUI", positive_text=None,
                    prompt=None, extra_pnginfo=None):
        if isinstance(positive_text, str) and positive_text.strip():
            texts = [positive_text]
        else:
            texts = _prompt_texts(prompt)
        names = _char_names(texts, auto_extract, max_tags, bare_name_mode,
                            _split_character_list(character_list))
        if names:
            name = "_".join(names)[:150]
            display = f"角色名: {name}"
        else:
            name = (_sanitize_name(fallback_name) or "ComfyUI")[:150]
            display = f"未识别到角色名，使用默认命名: {name}"
        prefix = f"{name}/{name}" if _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹" else name

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

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
