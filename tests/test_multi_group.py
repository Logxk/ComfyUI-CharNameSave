"""二期改进的单元测试：多人自动分组（问题 1）与 cosplay 元标签（问题 2）。

Run with the ComfyUI embedded interpreter, e.g.

    F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_multi_group.py

两类测试：

* 行为测试使用**内置小数据集夹具**（一定会跑）；
* 另有针对真实 ``data/characters.jsonl`` 的集成测试（含 spec 里的
  bocchi_the_rock 双人用例），数据集没下载 / 角色不在数据集里时自动跳过。

注意 ComfyUI 的路径语义：``get_save_image_path`` 把 prefix 拆成
``subfolder=os.path.dirname(prefix)`` 与 ``filename=os.path.basename(prefix)``，
再拼成 ``subfolder/filename_00001_.png``。因此
prefix ``双人/gotoh_hitori_kita_ikuyo`` 落盘为
``双人/gotoh_hitori_kita_ikuyo_00001_.png``。
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

# 合成数据集：模仿 danbooru 的两种写法
#   - 带 _(作品名) 消歧：denia_(wuthering_waves)
#   - 裸名 + copyright 字段：kita_ikuyo / gotoh_hitori（真实数据集里两者混着来）
FIXTURE = [
    {"name": "kita_ikuyo", "copyright": "bocchi_the_rock!", "post_count": 3000},
    {"name": "gotoh_hitori", "copyright": "bocchi_the_rock!", "post_count": 3500},
    {"name": "ijichi_nijika", "copyright": "bocchi_the_rock!", "post_count": 2000},
    {"name": "yamada_ryo", "copyright": "bocchi_the_rock!", "post_count": 2500},
    {"name": "denia_(wuthering_waves)", "copyright": "wuthering_waves", "post_count": 900},
    {"name": "hakurei_reimu", "copyright": "touhou", "post_count": 100588},
]


def _strip_counter(filename):
    """去掉文件名尾部的 "_00001_" 编号，只留下前缀（用于比较路径是否同一组合）。"""
    return re.sub(r"_\d+_\.png$", "", filename)


def _comfy_like_get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
    """与 ComfyUI folder_paths.get_save_image_path 等价的实现（仅用于测试）。

    subfolder = os.path.dirname(prefix)，filename = os.path.basename(prefix)，
    目录不存在时自动创建，counter 由同前缀已有文件推算。
    """
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
        """返回 (相对路径, subfolder, filename, display)，子文件夹缺失时自动创建。"""
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}
        result = self.saver.save_images(images, prompt=prompt, **kwargs)
        saved = result["ui"]["images"][0]
        parts = [p for p in saved["subfolder"].replace("\\", "/").split("/") if p]
        folder = os.path.join(self.tmp_dir, *parts)
        relative = os.path.relpath(os.path.join(folder, saved["filename"]), self.tmp_dir)
        return (relative.replace(os.sep, "/"), saved["subfolder"], saved["filename"],
                result["ui"]["text"][0])


class GroupTagParsingTests(unittest.TestCase):

    def test_default_when_missing(self):
        self.assertEqual(node._parse_group_tags(""), ["双人", "多人"])
        self.assertEqual(node._parse_group_tags(None), ["双人", "多人"])
        self.assertEqual(node._parse_group_tags("双人"), ["双人", "多人"])

    def test_custom_values_and_whitespace(self):
        self.assertEqual(node._parse_group_tags(" Couple , Group "), ["Couple", "Group"])
        self.assertEqual(node._parse_group_tags("a,b,c"), ["a", "b", "c"])

    def test_group_tag_for_count(self):
        tags = node._parse_group_tags("双人,多人")
        self.assertIsNone(node._group_tag_for_count(0, tags, True))
        self.assertIsNone(node._group_tag_for_count(1, tags, True))
        self.assertEqual(node._group_tag_for_count(2, tags, True), "双人")
        self.assertEqual(node._group_tag_for_count(3, tags, True), "多人")
        self.assertEqual(node._group_tag_for_count(5, tags, True), "多人")
        for count in (0, 1, 2, 3, 5):
            self.assertIsNone(node._group_tag_for_count(count, tags, False))

    def test_chinese_group_tag_is_not_mangled(self):
        self.assertEqual(node._clean_path_part("双人"), "双人")
        self.assertEqual(node._clean_path_part("多人"), "多人")
        self.assertEqual(node._clean_path_part("双人/多人"), "双人_多人")


class PrefixBuildingTests(_IndexSwapMixin):

    def test_empty_names_returns_none(self):
        self.assertEqual(node._build_save_prefix([], FILE), (None, None))

    def test_single_character_keeps_old_prefix(self):
        self.assertEqual(node._build_save_prefix(["kita_ikuyo"], FILE)[0], "kita_ikuyo")
        self.assertEqual(node._build_save_prefix(["kita_ikuyo"], FOLDER)[0],
                         "kita_ikuyo/kita_ikuyo")

    def test_two_characters(self):
        names = ["kita_ikuyo", "gotoh_hitori"]
        self.assertEqual(node._sorted_character_names(names), ["gotoh_hitori", "kita_ikuyo"])
        self.assertEqual(node._build_save_prefix(names, FILE)[0], "双人_gotoh_hitori_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(names, FOLDER)[0], "双人/gotoh_hitori_kita_ikuyo")

    def test_order_independent(self):
        a = node._build_save_prefix(["kita_ikuyo", "gotoh_hitori"], FOLDER)[0]
        b = node._build_save_prefix(["gotoh_hitori", "kita_ikuyo"], FOLDER)[0]
        self.assertEqual(a, b)
        # 大小写差异不影响排序，但写入的仍是原始大小写
        c = node._build_save_prefix(["Kita_Ikuyo", "gotoh_hitori"], FOLDER)[0]
        self.assertEqual(c, node._build_save_prefix(["gotoh_hitori", "Kita_Ikuyo"], FOLDER)[0])
        self.assertIn("Kita_Ikuyo", c)

    def test_three_characters_use_second_label(self):
        names = ["kita_ikuyo", "gotoh_hitori", "ijichi_nijika"]
        self.assertEqual(node._build_save_prefix(names, FOLDER)[0],
                         "多人/gotoh_hitori_ijichi_nijika_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(names, FILE)[0],
                         "多人_gotoh_hitori_ijichi_nijika_kita_ikuyo")

    def test_custom_group_labels(self):
        custom = node._parse_group_tags("Couple,Group")
        self.assertEqual(
            node._build_save_prefix(["kita_ikuyo", "gotoh_hitori"], FOLDER, True, custom)[0],
            "Couple/gotoh_hitori_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(["a", "b", "c"], FILE, True, custom)[0],
                         "Group_a_b_c")

    def test_disabled_multi_group_falls_back(self):
        names = ["kita_ikuyo", "gotoh_hitori"]
        self.assertEqual(node._build_save_prefix(names, FOLDER, False, None)[0],
                         "gotoh_hitori_kita_ikuyo/gotoh_hitori_kita_ikuyo")
        self.assertEqual(node._build_save_prefix(names, FILE, False, None)[0],
                         "gotoh_hitori_kita_ikuyo")

    def test_long_combination_is_truncated(self):
        joined = node._build_save_prefix(["x" * 60, "y" * 60, "z" * 60], FILE)[0]
        self.assertEqual(len(joined), len("多人_") + node._MULTI_NAME_MAX_LEN)
        self.assertFalse(joined.endswith("_"))


class ShortNameTests(_IndexSwapMixin):

    def test_bare_name_stays_bare(self):
        # 数据集里的裸名不再被展开成 name_(copyright)
        self.assertEqual(node._character_bare_name("kita_ikuyo", EXACT), "kita_ikuyo")
        self.assertEqual(node._character_bare_name("gotoh_hitori", EXACT), "gotoh_hitori")

    def test_series_form_uses_bare_base_when_known(self):
        self.assertEqual(node._resolve_character_name("kita_ikuyo_(bocchi_the_rock!)", EXACT),
                         "kita_ikuyo")

    def test_bare_base_not_in_dataset_keeps_dataset_name(self):
        # denia_(wuthering_waves) 只有带作品名的唯一写法 -> 收缩成裸名 denia（二期期望）
        self.assertIsNone(node._character_bare_name("denia (wuthering waves)", EXACT))
        self.assertEqual(node._resolve_character_name("denia_(wuthering_waves)", EXACT), "denia")
        # 提示词自带的作品名与数据集不一致时，不做无依据的收缩
        self.assertEqual(node._resolve_character_name("denia_(other series)", EXACT),
                         "denia_(wuthering_waves)")
        # hakurei_reimu 是真实裸名条目 -> 完整写法也解析成数据集里的裸名
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
        # 数据集/名单都查不到时退化为裸名 + _cosplay（稳定、可预测，且不含括号）
        self.assertEqual(node._character_name_from_tag("someone_unknown (cosplay)"),
                         "someone_unknown_cosplay")
        self.assertNotIn("(", node._character_name_from_tag("someone_unknown (cosplay)"))
        self.assertIsNone(node._character_name_from_tag("a (cosplay)"))  # 太短，不猜

    def test_character_list_is_consulted_for_meta_tags(self):
        # _resolve_character_name 接收的是「已经剥掉元标签」的裸名字
        self.assertEqual(node._resolve_character_name("myoc", OFF, ["myoc"]), "myoc")
        # 完整链路：数据集为空时，名单里的短名字也能被 cosplay tag 认出来
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_NAMES_LOWER.clear()
        self.assertEqual(node._character_name_from_tag("myoc (cosplay)", ["myoc"]), "myoc")
        self.assertEqual(node._character_bare_name("myoc (cosplay)", EXACT, ["myoc"]), "myoc")
        self.assertEqual(node._char_names(["myoc (cosplay)"], True, 3, OFF, ["myoc"]), ["myoc"])
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
        self.assertEqual(node._char_names(["cosplay, alternate_costume"], True, 5, OFF), [])


class MultiGroupSaveTests(_SaveHarnessMixin):
    """spec 里的测试用例（端到端，直接看落盘路径）。"""

    def test_two_characters_folder_mode(self):
        relative, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=2)
        self.assertEqual(subfolder, "双人")
        self.assertEqual(filename, "gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(relative, "双人/gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, *relative.split("/"))))

    def test_two_characters_reversed_gives_same_path(self):
        kwargs = dict(mode=FOLDER, bare_name_mode=EXACT, max_tags=2)
        first = self.save("kita_ikuyo, gotoh_hitori, bocchi_the_rock!", **kwargs)
        second = self.save("gotoh_hitori, kita_ikuyo, bocchi_the_rock!", **kwargs)
        self.assertEqual(first[1], second[1])
        # 同目录同前缀 -> 只是编号继续递增（编号差别不算「不同组合」）
        self.assertEqual(_strip_counter(first[2]), _strip_counter(second[2]))
        self.assertEqual(first[2], "gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(second[2], "gotoh_hitori_kita_ikuyo_00002_.png")

    def test_duplicate_names_are_deduplicated(self):
        relative, subfolder, _, _ = self.save(
            "kita_ikuyo, gotoh_hitori, kita_ikuyo, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=5)
        self.assertEqual(subfolder, "双人")
        self.assertEqual(relative.count("kita_ikuyo"), 1)

    def test_two_characters_file_mode(self):
        relative, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FILE, bare_name_mode=EXACT, max_tags=2)
        self.assertEqual(subfolder, "")
        self.assertEqual(filename, "双人_gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(relative, filename)

    def test_three_characters_folder_mode(self):
        _, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, ijichi_nijika, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=3)
        self.assertEqual(subfolder, "多人")
        self.assertEqual(filename, "gotoh_hitori_ijichi_nijika_kita_ikuyo_00001_.png")

    def test_disabled_grouping_falls_back_to_old_naming(self):
        relative, subfolder, filename, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=2, enable_multi_group=False)
        self.assertEqual(subfolder, "gotoh_hitori_kita_ikuyo")
        self.assertEqual(filename, "gotoh_hitori_kita_ikuyo_00001_.png")
        self.assertEqual(relative, "gotoh_hitori_kita_ikuyo/gotoh_hitori_kita_ikuyo_00001_.png")

    def test_custom_group_tags(self):
        _, subfolder_one, _, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=2, group_tags="Couple")
        self.assertEqual(subfolder_one, "双人")  # 只填 1 项 -> 回退默认
        _, subfolder_two, _, _ = self.save(
            "kita_ikuyo, gotoh_hitori, bocchi_the_rock!",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=2, group_tags="Couple,Group")
        self.assertEqual(subfolder_two, "Couple")

    def test_cosplay_prompt_never_contains_cosplay_series(self):
        relative, subfolder, filename, display = self.save(
            "gotoh_hitori (cosplay), cosplay, alternate_costume",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=5)
        self.assertNotIn("(cosplay)", relative)
        self.assertEqual(subfolder, "gotoh_hitori")
        self.assertEqual(filename, "gotoh_hitori_00001_.png")
        self.assertIn("gotoh_hitori", display)

    def test_cosplay_two_characters_go_to_duo_folder(self):
        relative, subfolder, _, _ = self.save(
            "kita_ikuyo (cosplay), gotoh_hitori (cosplay), bocchi_the_rock!, cosplay",
            mode=FOLDER, bare_name_mode=EXACT, max_tags=5)
        self.assertEqual(subfolder, "双人")
        self.assertNotIn("cosplay)", relative)

    def test_single_character_output_unchanged(self):
        relative, subfolder, filename, display = self.save("char:hakurei_reimu", mode=FOLDER)
        self.assertEqual(subfolder, "hakurei_reimu")
        self.assertEqual(filename, "hakurei_reimu_00001_.png")
        self.assertEqual(display, "角色名: hakurei_reimu")

    def test_second_image_continues_numbering(self):
        text = "kita_ikuyo, gotoh_hitori, bocchi_the_rock!"
        kwargs = dict(mode=FOLDER, bare_name_mode=EXACT, max_tags=2)
        self.save(text, **kwargs)
        _, _, filename, _ = self.save(text, **kwargs)
        self.assertEqual(filename, "gotoh_hitori_kita_ikuyo_00002_.png")


class InputTypesTests(unittest.TestCase):

    def test_multi_group_widgets(self):
        required = node.CharNameSaveImage.INPUT_TYPES()["required"]
        self.assertEqual(required["enable_multi_group"][0], "BOOLEAN")
        self.assertIs(required["enable_multi_group"][1]["default"], True)
        self.assertEqual(required["group_tags"][0], "STRING")
        self.assertEqual(required["group_tags"][1]["default"], "双人,多人")

    def test_widget_order_keeps_old_params(self):
        keys = list(node.CharNameSaveImage.INPUT_TYPES()["required"])
        for key in ("images", "mode", "auto_extract", "max_tags", "bare_name_mode",
                    "character_list", "enable_multi_group", "group_tags",
                    "padding", "fallback_name"):
            self.assertIn(key, keys)
        self.assertLess(keys.index("character_list"), keys.index("enable_multi_group"))
        self.assertLess(keys.index("enable_multi_group"), keys.index("group_tags"))


class SelfTestTests(unittest.TestCase):
    """_self_test() 必须通过（它同时是给用户的本地回归入口）。"""

    def test_self_test_passes(self):
        if not node._CHARACTER_NAMES_LOWER:
            self.skipTest("dataset not downloaded")
        self.assertEqual(node._self_test(), 0)


class RealDatasetBocchiTests(unittest.TestCase):
    """spec 里的 bocchi_the_rock 用例（需要真实数据集）。"""

    PROMPT_A = "kita_ikuyo, gotoh_hitori, bocchi_the_rock!"
    PROMPT_B = "gotoh_hitori, kita_ikuyo, bocchi_the_rock!"

    def setUp(self):
        if not os.path.isfile(node._DATASET_PATH) or not node._CHARACTER_NAMES_LOWER:
            if os.path.isfile(node._DATASET_PATH):
                node._load_character_index()
        if not node._CHARACTER_NAMES_LOWER:
            self.skipTest("dataset not downloaded")
        for name in ("kita_ikuyo", "gotoh_hitori"):
            if node._dataset_lookup(name, EXACT) is None:
                self.skipTest(f"{name} not in dataset")
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_bocchi")
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

    def test_duo_folder_and_order_independence(self):
        kwargs = dict(mode=FOLDER, bare_name_mode=EXACT, max_tags=2)
        path_a, saved_a = self._save(self.PROMPT_A, **kwargs)
        path_b, saved_b = self._save(self.PROMPT_B, **kwargs)
        self.assertEqual(saved_a["subfolder"], "双人")
        self.assertEqual(saved_a["filename"], "gotoh_hitori_kita_ikuyo_00001_.png")
        # 顺序颠倒得到同一个组合目录/同一前缀，只是同目录内编号继续递增
        self.assertEqual(saved_a["subfolder"], saved_b["subfolder"])
        self.assertEqual(_strip_counter(saved_a["filename"]), _strip_counter(saved_b["filename"]))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, *path_a.split("/"))))

    def test_duo_file_mode(self):
        _, saved = self._save(self.PROMPT_A, mode=FILE, bare_name_mode=EXACT, max_tags=2)
        self.assertEqual(saved["filename"], "双人_gotoh_hitori_kita_ikuyo_00001_.png")

    def test_cosplay_prompt_with_real_dataset(self):
        text = ("kita_ikuyo (cosplay), gotoh_hitori (cosplay), bocchi_the_rock!, "
                "cosplay, alternate_costume")
        path, saved = self._save(text, mode=FOLDER, bare_name_mode=EXACT, max_tags=5)
        self.assertNotIn("cosplay)", path)
        self.assertEqual(saved["subfolder"], "双人")
        self.assertEqual(saved["filename"], "gotoh_hitori_kita_ikuyo_00001_.png")

    def test_three_characters_in_real_dataset(self):
        third = None
        for candidate in ("ijichi_nijika", "yamada_ryo", "nijika_ijichi"):
            if node._dataset_lookup(candidate, EXACT) is not None:
                third = candidate
                break
        if third is None:
            self.skipTest("no third bocchi character in dataset")
        text = f"kita_ikuyo, gotoh_hitori, {third}, bocchi_the_rock!"
        _, saved = self._save(text, mode=FOLDER, bare_name_mode=EXACT, max_tags=3)
        self.assertEqual(saved["subfolder"], "多人")
        self.assertIn(third, saved["filename"])

    def test_disabled_grouping_with_real_dataset(self):
        _, saved = self._save(self.PROMPT_A, mode=FOLDER, bare_name_mode=EXACT,
                              max_tags=2, enable_multi_group=False)
        self.assertEqual(saved["subfolder"], "gotoh_hitori_kita_ikuyo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
