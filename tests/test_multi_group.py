"""多人自动分组（Duo / Group）、模糊匹配约束（stage→sage 回归）与面板精简的测试。

Run with the ComfyUI embedded interpreter, e.g.

    F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_multi_group.py

内容：

* 第三轮：模糊匹配的停用词 / 长度 / 首字母 / cutoff 约束（stage 曾被误判为 sage）；
* 第三轮：移除 max_tags 后的角色数量自动检测、父子角色去重、超量截断；
* 第三轮：节点面板只剩 images / positive_text / mode / bare_name_mode / padding /
  fallback_name 六个控件；
* 二期：Duo（2 人）/ Group（3 人及以上）分组、顺序无关、cosplay 元标签。

ComfyUI 路径语义：``get_save_image_path`` 把 prefix 拆成
``subfolder=os.path.dirname(prefix)`` 与 ``filename=os.path.basename(prefix)``，
再拼成 ``subfolder/filename_00001_.png``。因此 prefix ``Duo/gotoh_hitori_kita_ikuyo``
落盘为 ``Duo/gotoh_hitori_kita_ikuyo_00001_.png``。
"""

import json
import os
import re
import shutil
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "folder_paths" not in sys.modules:
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_output_directory = lambda: os.getcwd()
    folder_paths.get_save_image_path = lambda *a, **k: (os.getcwd(), "x", 1, "", None)
    sys.modules["folder_paths"] = folder_paths

if "comfy.cli_args" not in sys.modules:
    comfy = types.ModuleType("comfy")
    cli_args = types.ModuleType("comfy.cli_args")
    cli_args.args = types.SimpleNamespace(disable_metadata=True)
    comfy.cli_args = cli_args
    sys.modules["comfy"] = comfy
    sys.modules["comfy.cli_args"] = cli_args

import __init__ as node  # noqa: E402

OFF = node._BARE_NAME_MODE_OFF
EXACT = node._BARE_NAME_MODE_EXACT
FUZZY = node._BARE_NAME_MODE_FUZZY
FOLDER = "按角色分组文件夹"
FILE = "按角色命名文件"
DUO, GROUP = node._MULTI_GROUP_TAGS

# 合成数据集：模仿 danbooru 的两种写法
#   - 带 _(作品名) 消歧：denia_(wuthering_waves)
#   - 裸名 + copyright 字段：kita_ikuyo / gotoh_hitori（真实数据集里两者混着来）
# 另外故意放一个短角色名 "sage" 用来复现 stage -> sage 的误判。
FIXTURE = [
    {"name": "kita_ikuyo", "copyright": "bocchi_the_rock!", "post_count": 3000},
    {"name": "gotoh_hitori", "copyright": "bocchi_the_rock!", "post_count": 3500},
    {"name": "ijichi_nijika", "copyright": "bocchi_the_rock!", "post_count": 2000},
    {"name": "yamada_ryo", "copyright": "bocchi_the_rock!", "post_count": 2500},
    {"name": "denia_(wuthering_waves)", "copyright": "wuthering_waves", "post_count": 900},
    {"name": "hakurei_reimu", "copyright": "touhou", "post_count": 100588},
    {"name": "sage_(valorant)", "copyright": "valorant", "post_count": 1500},
]

# 用户报告的提示词（第三轮 bug 复现用）
REPORTED_PROMPT = (
    "masterpiece, best quality, highres, 2girls, kita ikuyo, red hair, long hair, "
    "side ponytail, yellow hair ornament, school uniform, red ribbon, pleated skirt, "
    "gotoh hitori, pink hair, long hair, blue hair bobbles, yellow hair bobbles, "
    "pink track jacket, black track pants, guitar, playing guitar together, live house, "
    "stage, stage lights, singing, smiling, looking at each other, holding hands, "
    "dynamic angle, detailed background, bocchi the rock!,"
)


def _strip_counter(filename):
    """去掉文件名尾部的 "_00001_" 编号，只留下前缀。"""
    return re.sub(r"_\d+_\.png$", "", filename)


def _comfy_like_get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
    """与 ComfyUI folder_paths.get_save_image_path 等价的实现（仅用于测试）。"""
    subfolder = os.path.dirname(os.path.normpath(filename_prefix))
    filename = os.path.basename(os.path.normpath(filename_prefix))
    full_output_folder = os.path.join(output_dir, subfolder)

    def map_filename(name):
        prefix_len = len(os.path.basename(filename_prefix))
        prefix = name[:prefix_len + 1]
        try:
            digits = int(name[prefix_len + 1:].split(".")[0].split("_")[0])
        except ValueError:
            digits = 0
        return digits, prefix

    try:
        counter = max(filter(
            lambda a: os.path.normcase(a[1][:-1]) == os.path.normcase(filename) and a[1][-1] == "_",
            map(map_filename, os.listdir(full_output_folder))))[0] + 1
    except (ValueError, FileNotFoundError):
        os.makedirs(full_output_folder, exist_ok=True)
        counter = 1
    return full_output_folder, filename, counter, subfolder, filename_prefix


class _FakeTensor:
    """Minimal stand-in for the torch IMAGE tensor used by save_images."""

    def __init__(self, array):
        self._array = array

    @property
    def shape(self):
        return self._array.shape

    def cpu(self):
        return self

    def numpy(self):
        return self._array

    def __getitem__(self, item):
        return type(self)(self._array[item])

    def __iter__(self):
        for row in self._array:
            yield type(self)(row)


class _IndexSwapMixin(unittest.TestCase):
    """把模块级索引换成合成夹具，测试结束再还原。"""

    def setUp(self):
        self._saved = (
            dict(node._CHARACTER_INDEX),
            set(node._CHARACTER_NAMES_LOWER),
            dict(node._CHARACTER_NAME_LOOKUP),
        )
        self.addCleanup(self._restore_index)
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_multi")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        dataset = os.path.join(self.tmp_dir, "characters.jsonl")
        with open(dataset, "w", encoding="utf-8") as handle:
            for record in FIXTURE:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        node._load_character_index(dataset)

    def _restore_index(self):
        index, names, lookup = self._saved
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX.update(index)
        node._CHARACTER_NAMES_LOWER.clear()
        node._CHARACTER_NAMES_LOWER.update(names)
        node._CHARACTER_NAME_LOOKUP.clear()
        node._CHARACTER_NAME_LOOKUP.update(lookup)


class _SaveHarnessMixin(_IndexSwapMixin):
    """用与 ComfyUI 等价的 get_save_image_path 跑完整的 save_images。"""

    def setUp(self):
        super().setUp()
        self._real_output_dir = node.folder_paths.get_output_directory
        self._real_save_image_path = node.folder_paths.get_save_image_path
        self.addCleanup(self._restore_folder_paths)
        node.folder_paths.get_output_directory = lambda: self.tmp_dir
        node.folder_paths.get_save_image_path = _comfy_like_get_save_image_path
        self.saver = node.CharNameSaveImage()

    def _restore_folder_paths(self):
        node.folder_paths.get_output_directory = self._real_output_dir
        node.folder_paths.get_save_image_path = self._real_save_image_path

    def save(self, text, **kwargs):
        """返回 (相对路径, subfolder, filename, display)。"""
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}
        result = self.saver.save_images(images, prompt=prompt, **kwargs)
        saved = result["ui"]["images"][0]
        parts = [p for p in saved["subfolder"].replace("\\", "/").split("/") if p]
        folder = os.path.join(self.tmp_dir, *parts)
        relative = os.path.relpath(os.path.join(folder, saved["filename"]), self.tmp_dir)
        return (relative.replace(os.sep, "/"), saved["subfolder"], saved["filename"],
                result["ui"]["text"][0])


class FuzzyConstraintTests(_IndexSwapMixin):
    """第三轮任务 1：模糊匹配的约束（stage 被误判为 sage 的回归测试）。"""

    def test_stopwords_contain_the_reported_tags(self):
        for word in ("stage", "stage lights", "live house", "guitar", "playing guitar",
                     "singing", "smiling", "blush", "looking at viewer",
                     "looking at each other", "holding hands", "standing", "sitting",
                     "walking", "classroom", "park", "beach", "night", "sunset",
                     "dynamic angle", "detailed background", "school uniform",
                     "casual clothes"):
            self.assertIn(word, node._BARE_NAME_STOPWORDS, word)

    def test_stage_never_matches_sage(self):
        # 数据集里确实有 sage_(valorant)，stage 必须依然匹配不到
        self.assertIsNotNone(node._dataset_lookup("sage", EXACT)[0])
        self.assertIsNone(node._fuzzy_candidate_matches("stage", FUZZY))
        self.assertIsNone(node._character_bare_name("stage", FUZZY))
        self.assertIsNone(node._character_bare_name("stage lights", FUZZY))

    def test_cutoff_raised_to_092(self):
        self.assertEqual(node._FUZZY_CUTOFF, 0.92)

    def test_minimum_length_constraint(self):
        self.assertEqual(node._BARE_NAME_MIN_LEN, 4)
        self.assertIsNone(node._fuzzy_candidate_matches("abc", FUZZY))
        self.assertIsNone(node._fuzzy_candidate_matches("2girls", FUZZY))

    def test_length_difference_constraint(self):
        # "kitaikuy" 与 "kita ikuyo" 长度差 1 以内 -> 允许（首字母相同）
        self.assertEqual(node._fuzzy_candidate_matches("kita ikuy", FUZZY), "kita ikuyo")
        # 长度差 > 1 -> 拒绝（_FUZZY_MAX_LEN_DIFF = 1）
        self.assertIsNone(node._fuzzy_candidate_matches("kit", FUZZY))
        self.assertIsNone(node._fuzzy_candidate_matches("kitaiku", FUZZY))

    def test_first_letter_constraint(self):
        # 首字母不同直接拒绝
        self.assertIsNone(node._fuzzy_candidate_matches("xita ikuyo", FUZZY))

    def test_exact_match_happens_before_fuzzy(self):
        # 精确命中（忽略大小写 / 下划线空格）不需要走模糊分支
        self.assertEqual(node._dataset_lookup("KITA_IKUYO", FUZZY)[1], "kita ikuyo")
        # 完全不存在的名字即使打开模糊也匹配不到（长度差 / cutoff 约束）
        self.assertIsNone(node._dataset_lookup("zzzzzzzz", FUZZY)[0])

    def test_real_typo_still_corrected(self):
        self.assertEqual(node._character_bare_name("kita ikuy", FUZZY), "kita_ikuyo")

    def test_reported_prompt_has_no_sage(self):
        names = node._char_names([REPORTED_PROMPT], FUZZY)
        self.assertFalse(any("sage" in name for name in names), names)
        self.assertEqual(sorted(names), ["gotoh_hitori", "kita_ikuyo"])
        prefix = node._build_save_prefix(names, FOLDER)[0]
        self.assertEqual(prefix, "Duo/gotoh_hitori_kita_ikuyo")

    def test_reported_prompt_in_off_mode(self):
        # 关闭数据集识别时只认 "name (series)"：这条提示词没有该结构 -> 空
        self.assertEqual(node._char_names([REPORTED_PROMPT], OFF), [])


class AutoCharacterCountTests(_IndexSwapMixin):
    """第三轮任务 2：自动检测角色数量（移除 max_tags）与父子去重。"""

    def test_all_names_collected(self):
        self.assertEqual(
            node._char_names(["kita_ikuyo, gotoh_hitori, ijichi_nijika, bocchi_the_rock!"], EXACT),
            ["kita_ikuyo", "gotoh_hitori", "ijichi_nijika"],
        )

    def test_duplicates_are_removed(self):
        self.assertEqual(
            node._char_names(["kita_ikuyo, gotoh_hitori, kita_ikuyo"], EXACT),
            ["kita_ikuyo", "gotoh_hitori"],
        )

    def test_parent_child_prefers_longer(self):
        self.assertEqual(
            node._dedupe_character_names(["denia", "denia (wuthering waves)"]),
            ["denia (wuthering waves)"],
        )
        self.assertEqual(
            node._dedupe_character_names(["denia (wuthering waves)", "denia"]),
            ["denia (wuthering waves)"],
        )
        self.assertEqual(
            node._dedupe_character_names(["hakurei_reimu", "hakurei_reimu_(touhou)"]),
            ["hakurei_reimu_(touhou)"],
        )

    def test_dedupe_is_case_insensitive(self):
        self.assertEqual(node._dedupe_character_names(["REM", "rem", "Rem"]), ["REM"])
        self.assertEqual(
            node._dedupe_character_names(["Denia", "denia (wuthering waves)"]),
            ["denia (wuthering waves)"],
        )

    def test_unrelated_same_base_names_are_kept(self):
        # 两条都带括号、只是基础名相同 -> 不合并（真实的多角色）
        self.assertEqual(
            node._dedupe_character_names(["a (series one)", "a (series two)"]),
            ["a (series one)", "a (series two)"],
        )

    def test_over_eight_is_capped(self):
        self.assertEqual(node._MAX_AUTO_NAMES, 8)
        names = node._dedupe_character_names([f"c{i}" for i in range(20)])
        capped = node._char_names([", ".join(f"char:c{i}" for i in range(20))], EXACT)
        self.assertEqual(len(capped), 8)
        self.assertEqual(len(names), 20)  # 纯去重函数本身不做上限


class PrefixBuildingTests(_IndexSwapMixin):

    def test_group_tags_are_hardcoded_english(self):
        self.assertEqual(node._MULTI_GROUP_TAGS, ("Duo", "Group"))
        self.assertEqual(node._group_tag_for_count(2), "Duo")
        self.assertEqual(node._group_tag_for_count(3), "Group")
        self.assertEqual(node._group_tag_for_count(9), "Group")
        self.assertEqual(node._group_tag_for_count(1), "")
        self.assertEqual(node._group_tag_for_count(0), "")

    def test_empty_names_returns_none(self):
        self.assertEqual(node._build_save_prefix([], FILE), (None, None))

    def test_single_character_keeps_old_prefix(self):
        self.assertEqual(node._build_save_prefix(["kita_ikuyo"], FILE)[0], "kita_ikuyo")
        self.assertEqual(node._build_save_prefix(["kita_ikuyo"], FOLDER)[0],
                         "kita_ikuyo/kita_ikuyo")

    def test_two_characters(self):
        names = ["kita_ikuyo", "gotoh_hitori"]
        self.assertEqual(node._sorted_character_names(names), ["gotoh_hitori", "kita_ikuyo"])
        self.assertEqual(node._build_save_prefix(names, FILE)[0], "Duo_gotoh_hitori_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(names, FOLDER)[0], "Duo/gotoh_hitori_kita_ikuyo")

    def test_order_independent(self):
        a = node._build_save_prefix(["kita_ikuyo", "gotoh_hitori"], FOLDER)[0]
        b = node._build_save_prefix(["gotoh_hitori", "kita_ikuyo"], FOLDER)[0]
        self.assertEqual(a, b)
        c = node._build_save_prefix(["Kita_Ikuyo", "gotoh_hitori"], FOLDER)[0]
        self.assertEqual(c, node._build_save_prefix(["gotoh_hitori", "Kita_Ikuyo"], FOLDER)[0])
        self.assertIn("Kita_Ikuyo", c)  # 写入的仍是原始大小写

    def test_three_characters_use_group(self):
        names = ["kita_ikuyo", "gotoh_hitori", "ijichi_nijika"]
        self.assertEqual(node._build_save_prefix(names, FOLDER)[0],
                         "Group/gotoh_hitori_ijichi_nijika_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(names, FILE)[0],
                         "Group_gotoh_hitori_ijichi_nijika_kita_ikuyo")

    def test_long_combination_is_truncated(self):
        joined = node._build_save_prefix(["x" * 60, "y" * 60, "z" * 60], FILE)[0]
        self.assertEqual(len(joined), len("Group_") + node._MULTI_NAME_MAX_LEN)
        self.assertFalse(joined.endswith("_"))


class ShortNameTests(_IndexSwapMixin):

    def test_bare_name_stays_bare(self):
        self.assertEqual(node._character_bare_name("kita_ikuyo", EXACT), "kita_ikuyo")
        self.assertEqual(node._character_bare_name("gotoh_hitori", EXACT), "gotoh_hitori")

    def test_series_form_uses_bare_base_when_known(self):
        self.assertEqual(node._resolve_character_name("kita_ikuyo_(bocchi_the_rock!)", EXACT),
                         "kita_ikuyo")

    def test_bare_base_not_in_dataset_keeps_dataset_name(self):
        self.assertIsNone(node._character_bare_name("denia (wuthering waves)", EXACT))
        self.assertEqual(node._resolve_character_name("denia_(wuthering_waves)", EXACT), "denia")
        self.assertEqual(node._resolve_character_name("denia_(other series)", EXACT),
                         "denia_(wuthering_waves)")
        self.assertEqual(node._resolve_character_name("hakurei_reimu_(touhou)", EXACT),
                         "hakurei_reimu")


class CosplayMetaTagTests(_IndexSwapMixin):

    def test_meta_tag_not_treated_as_series(self):
        self.assertEqual(node._character_name_from_tag("gotoh_hitori (cosplay)"), "gotoh_hitori")
        self.assertEqual(node._character_name_from_tag("gotoh_hitori_(cosplay)"), "gotoh_hitori")
        self.assertEqual(node._character_name_from_tag("kita_ikuyo (alternate_costume)"),
                         "kita_ikuyo")
        self.assertEqual(node._character_name_from_tag("kita_ikuyo (cosplaying)"), "kita_ikuyo")
        self.assertEqual(node._character_name_from_tag("kita_ikuyo (clothing swap)"),
                         "kita_ikuyo")
        self.assertEqual(node._character_name_from_tag("kita_ikuyo (crossdressing)"),
                         "kita_ikuyo")

    def test_meta_tag_with_weight_and_wrapper(self):
        self.assertEqual(node._character_name_from_tag("(gotoh_hitori (cosplay):1.2)"),
                         "gotoh_hitori")
        self.assertEqual(node._character_name_from_tag("gotoh_hitori (cosplay):0.8"),
                         "gotoh_hitori")

    def test_unknown_character_keeps_bare_name_with_suffix(self):
        self.assertEqual(node._character_name_from_tag("someone_unknown (cosplay)"),
                         "someone_unknown_cosplay")
        self.assertNotIn("(", node._character_name_from_tag("someone_unknown (cosplay)"))
        self.assertIsNone(node._character_name_from_tag("a (cosplay)"))  # 太短，不猜

    def test_real_series_still_kept(self):
        self.assertEqual(node._character_name_from_tag("denia (wuthering waves)"),
                         "denia_(wuthering_waves)")

    def test_meta_detection_helpers(self):
        for text in ("cosplay", "Cosplay", "alternate costume", "alternate-costume",
                     "clothing_swap", "crossdressing"):
            self.assertTrue(node._meta_series_of(text), text)
        self.assertFalse(node._meta_series_of("bocchi the rock!"))
        self.assertEqual(node._meta_series_name("kita_ikuyo (cosplay)"), ("kita_ikuyo", "cosplay"))
        self.assertEqual(node._meta_series_name("kita_ikuyo (bocchi the rock!)"), (None, None))
        self.assertEqual(node._meta_series_name("by (cosplay:0.5)"), (None, None))

    def test_artist_tag_with_meta_word_is_still_artist(self):
        self.assertIsNone(node._character_name_from_tag("@kita_ikuyo (cosplay)"))
        self.assertIsNone(node._character_name_from_tag("by kita_ikuyo (cosplay)"))

    def test_bare_name_strips_meta_tag(self):
        self.assertEqual(node._character_bare_name("kita_ikuyo (cosplay)", EXACT), "kita_ikuyo")
        self.assertEqual(node._character_bare_name("kita_ikuyo (cosplay)", FUZZY), "kita_ikuyo")

    def test_auto_candidates_cosplay(self):
        candidates = list(node._auto_candidates(
            ["masterpiece, gotoh_hitori (cosplay), cosplay, alternate_costume"], EXACT))
        self.assertEqual(candidates, ["gotoh_hitori"])
        self.assertFalse(any("cosplay" in c for c in candidates))

    def test_meta_only_prompt_produces_nothing(self):
        self.assertEqual(node._char_names(["cosplay, alternate_costume"], OFF), [])


class MultiGroupSaveTests(_SaveHarnessMixin):
    """端到端：默认（按角色分组文件夹）+ Duo / Group 英文目录。"""

    def test_two_characters_folder_mode(self):
        relative, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!", bare_name_mode=EXACT)
        self.assertEqual(subfolder, "Duo")
        self.assertEqual(filename, "gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(relative, "Duo/gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, *relative.split("/"))))

    def test_two_characters_reversed_gives_same_path(self):
        first = self.save("kita_ikuyo, gotoh_hitori, bocchi_the_rock!", bare_name_mode=EXACT)
        second = self.save("gotoh_hitori, kita_ikuyo, bocchi_the_rock!", bare_name_mode=EXACT)
        self.assertEqual(first[1], second[1])
        self.assertEqual(_strip_counter(first[2]), _strip_counter(second[2]))
        self.assertEqual(first[2], "gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(second[2], "gotoh_hitori_kita_ikuyo_00002_.png")

    def test_duplicate_names_are_deduplicated(self):
        relative, subfolder, _, _ = self.save(
            "kita_ikuyo, gotoh_hitori, kita_ikuyo, bocchi_the_rock!", bare_name_mode=EXACT)
        self.assertEqual(subfolder, "Duo")
        self.assertEqual(relative.count("kita_ikuyo"), 1)

    def test_two_characters_file_mode(self):
        relative, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FILE, bare_name_mode=EXACT)
        self.assertEqual(subfolder, "")
        self.assertEqual(filename, "Duo_gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(relative, filename)

    def test_three_characters_folder_mode(self):
        _, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, ijichi_nijika, bocchi_the_rock!", bare_name_mode=EXACT)
        self.assertEqual(subfolder, "Group")
        self.assertEqual(filename, "gotoh_hitori_ijichi_nijika_kita_ikuyo_00001_.png")

    def test_cosplay_prompt_never_contains_cosplay_series(self):
        relative, subfolder, filename, display = self.save(
            "gotoh_hitori (cosplay), cosplay, alternate_costume", bare_name_mode=EXACT)
        self.assertNotIn("(cosplay)", relative)
        self.assertEqual(subfolder, "gotoh_hitori")
        self.assertEqual(filename, "gotoh_hitori_00001_.png")
        self.assertIn("gotoh_hitori", display)

    def test_cosplay_two_characters_go_to_duo_folder(self):
        relative, subfolder, _, _ = self.save(
            "kita_ikuyo (cosplay), gotoh_hitori (cosplay), bocchi_the_rock!, cosplay",
            bare_name_mode=EXACT)
        self.assertEqual(subfolder, "Duo")
        self.assertNotIn("cosplay)", relative)

    def test_reported_prompt_end_to_end(self):
        relative, subfolder, filename, _ = self.save(REPORTED_PROMPT, bare_name_mode=FUZZY)
        self.assertNotIn("sage", relative)
        self.assertEqual(subfolder, "Duo")
        self.assertEqual(_strip_counter(filename), "gotoh_hitori_kita_ikuyo")

    def test_single_character_output_unchanged(self):
        relative, subfolder, filename, display = self.save("char:hakurei_reimu")
        self.assertEqual(subfolder, "hakurei_reimu")
        self.assertEqual(filename, "hakurei_reimu_00001_.png")
        self.assertEqual(display, "角色名: hakurei_reimu")

    def test_second_image_continues_numbering(self):
        text = "kita_ikuyo, gotoh_hitori, bocchi_the_rock!"
        self.save(text, bare_name_mode=EXACT)
        _, _, filename, _ = self.save(text, bare_name_mode=EXACT)
        self.assertEqual(filename, "gotoh_hitori_kita_ikuyo_00002_.png")

    def test_removed_widget_values_are_tolerated(self):
        # 旧工作流 JSON 里带的参数值（面板已移除）不应导致报错
        _, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            auto_extract=True, max_tags=1, character_list="someone",
            enable_multi_group=True, group_tags="双人,多人",
            bare_name_mode=EXACT)
        self.assertEqual(subfolder, "Duo")
        self.assertEqual(_strip_counter(filename), "gotoh_hitori_kita_ikuyo")


class PanelTests(unittest.TestCase):
    """第三轮任务 2：面板精简后的最终控件清单。"""

    EXPECTED_REQUIRED = ["images", "mode", "bare_name_mode", "padding", "fallback_name"]
    EXPECTED_OPTIONAL = ["positive_text"]

    def test_required_widgets(self):
        self.assertEqual(list(node.CharNameSaveImage.INPUT_TYPES()["required"]),
                         self.EXPECTED_REQUIRED)

    def test_optional_and_hidden(self):
        types_ = node.CharNameSaveImage.INPUT_TYPES()
        self.assertEqual(list(types_["optional"]), self.EXPECTED_OPTIONAL)
        self.assertEqual(list(types_["hidden"]), ["prompt", "extra_pnginfo"])

    def test_no_extra_widgets(self):
        types_ = node.CharNameSaveImage.INPUT_TYPES()
        all_keys = list(types_["required"]) + list(types_["optional"])
        self.assertEqual(len(all_keys), 6, all_keys)
        for removed in ("auto_extract", "max_tags", "character_list",
                        "enable_multi_group", "group_tags"):
            self.assertNotIn(removed, all_keys, removed)

    def test_defaults_and_options(self):
        required = node.CharNameSaveImage.INPUT_TYPES()["required"]
        self.assertEqual(required["mode"][0][:2], [FOLDER, FILE])
        self.assertEqual(required["mode"][1]["default"], FOLDER)
        self.assertEqual(required["bare_name_mode"][0],
                         [OFF, EXACT, FUZZY])
        self.assertEqual(required["bare_name_mode"][1]["default"], FUZZY)
        self.assertEqual(required["padding"][0], "INT")
        self.assertEqual(required["padding"][1]["default"], 5)
        self.assertEqual(required["fallback_name"][1]["default"], "ComfyUI")

    def test_description_mentions_new_behaviour(self):
        text = node.CharNameSaveImage.DESCRIPTION
        self.assertIn("Duo", text)
        self.assertIn("Group", text)


class SelfTestTests(unittest.TestCase):
    """_self_test() 必须通过（它同时是给用户的本地回归入口）。"""

    def test_self_test_passes(self):
        if not node._CHARACTER_NAMES_LOWER:
            self.skipTest("dataset not downloaded")
        self.assertEqual(node._self_test(), 0)


class RealDatasetTests(unittest.TestCase):
    """需要真实数据集的集成测试（缺失时自动跳过）。"""

    def setUp(self):
        if not os.path.isfile(node._DATASET_PATH) or not node._CHARACTER_NAMES_LOWER:
            if os.path.isfile(node._DATASET_PATH):
                node._load_character_index()
        if not node._CHARACTER_NAMES_LOWER:
            self.skipTest("dataset not downloaded")
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_real")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._real_output_dir = node.folder_paths.get_output_directory
        self._real_save_image_path = node.folder_paths.get_save_image_path
        self.addCleanup(self._restore_folder_paths)
        node.folder_paths.get_output_directory = lambda: self.tmp_dir
        node.folder_paths.get_save_image_path = _comfy_like_get_save_image_path
        self.saver = node.CharNameSaveImage()

    def _restore_folder_paths(self):
        node.folder_paths.get_output_directory = self._real_output_dir
        node.folder_paths.get_save_image_path = self._real_save_image_path

    def _save(self, text, **kwargs):
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        result = self.saver.save_images(
            images, prompt={"1": {"inputs": {"text": text}}}, **kwargs)
        saved = result["ui"]["images"][0]
        parts = [p for p in saved["subfolder"].replace("\\", "/").split("/") if p]
        folder = os.path.join(self.tmp_dir, *parts)
        relative = os.path.relpath(os.path.join(folder, saved["filename"]), self.tmp_dir)
        return relative.replace(os.sep, "/"), saved

    def test_reported_prompt_with_real_dataset(self):
        """用户报告的提示词：默认参数下不应出现 sage。"""
        path, saved = self._save(REPORTED_PROMPT)
        self.assertNotIn("sage", path)
        self.assertEqual(saved["subfolder"], "Duo")
        self.assertEqual(_strip_counter(saved["filename"]), "gotoh_hitori_kita_ikuyo")

    def test_reported_prompt_grouped_numbering(self):
        path, saved = self._save(REPORTED_PROMPT)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, *path.split("/"))))

    def test_cosplay_prompt_with_real_dataset(self):
        text = ("kita_ikuyo (cosplay), gotoh_hitori (cosplay), bocchi_the_rock!, "
                "cosplay, alternate_costume")
        path, saved = self._save(text, bare_name_mode=EXACT)
        self.assertNotIn("cosplay)", path)
        self.assertEqual(saved["subfolder"], "Duo")
        self.assertEqual(_strip_counter(saved["filename"]), "gotoh_hitori_kita_ikuyo")

    def test_three_characters_in_real_dataset(self):
        third = None
        for candidate in ("ijichi_nijika", "yamada_ryo", "nijika_ijichi"):
            if node._dataset_lookup(candidate, EXACT)[0] is not None:
                third = candidate
                break
        if third is None:
            self.skipTest("no third bocchi character in dataset")
        text = f"kita_ikuyo, gotoh_hitori, {third}, bocchi_the_rock!"
        _, saved = self._save(text, bare_name_mode=EXACT)
        self.assertEqual(saved["subfolder"], "Group")
        self.assertIn(third, saved["filename"])

    def test_mode_switch(self):
        text = "kita_ikuyo, gotoh_hitori, bocchi_the_rock!"
        _, folder_saved = self._save(text, bare_name_mode=EXACT)
        _, file_saved = self._save(text, mode=FILE, bare_name_mode=EXACT)
        self.assertEqual(folder_saved["subfolder"], "Duo")
        self.assertEqual(file_saved["subfolder"], "")
        self.assertTrue(file_saved["filename"].startswith("Duo_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
