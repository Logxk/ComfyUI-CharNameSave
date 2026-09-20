"""Constants, regexes and tunables for ComfyUI-CharNameSave.

Extracted verbatim from the original single-file implementation. Values must stay
identical to preserve behaviour (UI labels, mode strings, thresholds, stopwords).
"""

import os
import re

# --- 内嵌 Danbooru 角色数据集路径（data/ 位于插件根目录，本文件在其子包内）-------
_DATASET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "characters.jsonl")

# --- tag / 角色名解析正则 -----------------------------------------------------
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

# 模糊匹配最低长度：**第三轮修复**——"stage" 被误匹配成角色 "sage" 的触发案例，
# 短标签必须直接排除，避免 4 字以下的通用词参与模糊匹配。
_BARE_NAME_MIN_LEN = 4
# 模糊匹配阈值（difflib.get_close_matches 的 cutoff，越大越严格）。
# 第三轮从 0.85 提到 0.92：stage/sage 的相似度约 0.888，0.85 会命中、0.92 不会。
_FUZZY_CUTOFF = 0.92
# 模糊匹配的长度容差：候选角色名与 tag 的长度差超过这个值就不算匹配
_FUZZY_MAX_LEN_DIFF = 1
# 通用 tag / 场景词停用表：这些词**永不参与模糊匹配**。
# 第三轮修复的核心之一：stage、live house、guitar 之类的词与角色名形近，
# 例如 "stage" -> "sage"（相似度 8/9 ≈ 0.888）。
# 表里统一用 _dataset_key 归一化后的写法（小写 + 下划线转空格）。
# 别名 _FUZZY_STOPWORDS：它们是同一份数据，只是名字点明了它的作用域——只挡模糊匹配。
_FUZZY_STOPWORDS = frozenset({
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
    "flowers", "window", "bed", "chair", "table", "simple background",
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
# 兼容别名：历史代码/测试引用 _BARE_NAME_STOPWORDS，语义等同 _FUZZY_STOPWORDS。
_BARE_NAME_STOPWORDS = _FUZZY_STOPWORDS

# --- 裸名精确匹配屏蔽表（_BARE_NAME_EXACT_BLOCKLIST）------------------------------
# 与 _FUZZY_STOPWORDS 的区别：这些词即使命中了数据集的精确匹配，也**不允许**当作
# 裸名角色。理由见下面的实测结论。
#
# 为什么需要单独一层：
#   数据集里存在大量「普通英文词恰好也是某角色 tag」的条目，例如
#     bow      -> bow_(paper_mario)          (posts 146)
#     professor-> professor_(ragnarok_online) (posts 753)
#     ribbon   -> ribbon_(kirby)             (posts 660)
#   提示词里的 `bow` / `ribbon` 是服装与构图 tag，被精确命中后会被当成角色，
#   于是「单一角色」的提示词凑成双人并落进 Duo 分组
#   （实测：professor_niyaniya (blue archive) + bow -> Duo_bow_professor_niyaniya…）。
#
# 为什么不能只靠 post_count：这些条目与真角色完全重叠——
#   doctor_(arknights) 12943 / professor_(ragnarok_online) 753 / bow_(paper_mario) 146，
#   而真角色热度只要有 100 就可以很低，不存在可分阈值。
#
# 为什么不能只靠「删除派生键」：派生出的单词裸键里包含高频真角色
#   （sensei 39183 / lumine 22083 / saber 21195 / rem 10255），一刀切会误伤。
#
# 因此这是一层**便宜的、可解释的**词表防线，只覆盖「明显是普通描述词」的词；
# 真正的裁决交给上下文与评分（见 matching.py 的候选评分）。显式写法
# （"bow (some series)" 或 char:bow）**不受本表影响**。
_BARE_NAME_EXACT_BLOCKLIST = frozenset({
    # 服装
    "shirt", "skirt", "dress", "pants", "shoes", "socks", "stockings",
    "pantyhose", "bra", "panties", "gloves", "scarf", "cape", "coat",
    "sweater", "hoodie", "shorts", "swimsuit", "bikini", "uniform",
    # 配饰
    "bow", "ribbon", "tie", "hat", "glasses", "necklace", "earrings",
    "bracelet", "crown", "veil", "headband", "hairband", "halo", "halos",
    # 身份 / 职业（同时也是普通名词，实测会被数据集的同名角色命中）
    "professor", "teacher", "student", "doctor", "nurse", "police",
    "officer", "soldier", "knight", "prince", "princess", "king", "queen",
    "master", "servant", "hero", "angel", "devil", "demon", "god", "goddess",
    "witch", "wizard", "priest", "nun", "maid", "butler", "idol", "singer",
    "dancer", "writer", "scientist", "engineer", "manager",
    # 种族 / 奇幻生物（同样是通用 tag，数据集里常有同名角色）
    "elf", "elves", "dwarf", "dwarves", "orc", "goblin", "slime", "fairy",
    "giant", "ghost", "zombie", "vampire", "werewolf", "mermaid", "centaur",
    "alien", "robot", "android", "human", "person", "child", "baby", "adult",
    # 动物 / 元素（同样是通用 tag，数据集里常有同名角色）
    "fox", "cat", "dog", "wolf", "bear", "bird", "fish", "horse", "dragon",
    "snake", "rabbit", "tiger", "lion", "monkey", "sheep", "cow", "pig",
    "deer", "frog", "bee", "spider", "star", "moon", "sun", "rose", "apple",
    "leaf", "tree", "flower",
    # 身体
    "hair", "eyes", "eye", "face", "head", "hand", "hands", "arm", "arms",
    "leg", "legs", "feet", "foot", "thigh", "thighs", "breasts", "skin",
    "mouth", "nose", "ear", "ears", "tail", "wings", "horn", "horns",
    # 物件
    "chair", "table", "window", "door", "book", "pen", "cup", "glass",
    "bottle", "sword", "knife", "gun", "box", "bag", "phone", "clock",
    # 场景
    "office", "classroom", "indoors", "outdoors", "room", "kitchen",
    "bedroom", "street", "garden", "forest", "mountain", "ocean",
    "sky", "cloud", "clouds", "rain", "snow", "wind", "fire", "water",
    # 通用描述词（_original 记录里的「无名」条目，名字本身就是描述）
    "girl", "boy", "fox girl", "fox girls", "cat girl", "cat girls",
    "bunny girl", "bunny girls", "mouse girl", "blonde girl", "blonde dog girl",
    "ahoge girl", "angel girl", "receptionist girl", "thai girl",
    "old man", "white-haired man", "gunpla boy",
})

# --- 候选评分常量（内部排序依据，不对外暴露、不作为 API 行为）----------------------
# 证据等级：显式写法永远最高；模糊匹配是最弱的一级（方案 §5/§17）。
_SCORE_EXPLICIT_CHAR = 100        # char:xxx
_SCORE_EXPLICIT_SERIES = 100      # xxx (series) / xxx_(series)
_SCORE_DATASET_EXACT = 55         # 裸名精确命中数据集
_SCORE_DATASET_ALIAS = 50         # 命中派生别名（如 remilia -> remilia_scarlet）
_SCORE_DATASET_FUZZY = 25         # 模糊匹配

# 热度是**辅助证据**，不是「是不是角色」的判据（避免误杀新作品角色）
_SCORE_POST_10000 = 20
_SCORE_POST_1000 = 10
_SCORE_POST_100 = 5

# 上下文一致 / 冲突
_SCORE_SAME_COPYRIGHT = 20        # 与提示词里显式角色的作品名一致
_SCORE_OTHER_COPYRIGHT = -10      # 属于另一个已知作品

# 通用词惩罚：足够大，使「裸名 + 通用词」的候选无法达到接受线
_SCORE_GENERIC_PENALTY = -80

# 接受阈值：不同来源用不同门槛（弱证据需要更高分）
_ACCEPT_THRESHOLD_EXACT = 50
_ACCEPT_THRESHOLD_FUZZY = 45

# 模糊命中的「打字完整度」加成与下限：
# coverage = len(tag) / len(规范名)，越高说明用户写得越完整。
# 已校准的用例：
#   "kita ikuy" -> "kita ikuyo"                9/10  = 0.90 -> 25+10+36 = 71 接受
#   "hakurei reim" -> "hakurei reimu"         12/13  = 0.92
#   "denia" -> "denia (wuthering waves)"       5/24  = 0.21 -> 低于下限，不接受
_FUZZY_MIN_COVERAGE = 0.55
_SCORE_FUZZY_COVERAGE = 40

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

# 数据集文件体积上限（安全护栏）：正常 characters.jsonl 约 2 MB；异常/损坏的超大文件
# 直接跳过加载，避免一次性把巨量 JSONL 读进内存导致 OOM。仅防御性限制，不影响正常数据。
_MAX_DATASET_BYTES = 64 * 1024 * 1024

# 保存方式选项：第三轮把默认值改为「按角色分组文件夹」，因此它排在第一位
_MODES = ["按角色分组文件夹", "按角色命名文件"]
# 默认保存方式：第三轮起改为「按角色分组文件夹」（用户仍可切到按角色命名文件）
_DEFAULT_MODE = "按角色分组文件夹"
# legacy English values saved by earlier versions, kept for workflow compatibility
_MODE_ALIASES = {"filename": "按角色命名文件", "folder": "按角色分组文件夹"}
