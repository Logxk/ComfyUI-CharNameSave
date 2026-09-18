"""Unit tests for the dataset-backed "bare character name" recognition.

在仓库根目录（本插件目录）下用你那份 ComfyUI 的解释器运行：

    python tests/test_bare_name.py

Two kinds of tests:

* behaviour tests against a **synthetic** dataset fixture (always run);
* integration tests against the real ``data/characters.jsonl`` (skipped when the
  dataset has not been downloaded, so the suite stays green out of the box).
"""

import json
import os
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

# Synthetic dataset: mirrors the real one (danbooru tags use underscores, the
# copyright field is the series tag, post_count decides between duplicates).
FIXTURE = [
    {"name": "emilia_(re:zero)", "copyright": "re:zero_kara_hajimeru_isekai_seikatsu", "post_count": 5210},
    {"name": "rem_(re:zero)", "copyright": "re:zero_kara_hajimeru_isekai_seikatsu", "post_count": 10255},
    {"name": "rem_(death_note)", "copyright": "death_note", "post_count": 160},
    {"name": "frieren", "copyright": "sousou_no_frieren", "post_count": 16196},
    {"name": "hakurei_reimu", "copyright": "touhou", "post_count": 100588},
    {"name": "hatsune_miku", "copyright": "vocaloid", "post_count": 145572},
    {"name": "hatsune_miku_(append)", "copyright": "vocaloid", "post_count": 1621},
    {"name": "saber_(fate)", "copyright": "fate_(series)", "post_count": 21195},
    {"name": "sky_(shantae)", "copyright": "shantae", "post_count": 800},
    {"name": "2b_(nier:automata)", "copyright": "nier:automata", "post_count": 20000},
]


class _IndexSwapMixin(unittest.TestCase):
    """Replace the module level index with the synthetic fixture for a test."""

    def setUp(self):
        self._saved = (
            dict(node._CHARACTER_INDEX),
            set(node._CHARACTER_NAMES_LOWER),
            dict(node._CHARACTER_NAME_LOOKUP),
        )
        self.addCleanup(self._restore)
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_bare")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.dataset = os.path.join(self.tmp_dir, "characters.jsonl")
        with open(self.dataset, "w", encoding="utf-8") as handle:
            for record in FIXTURE:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        node._load_character_index(self.dataset)

    def _restore(self):
        index, names, lookup = self._saved
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX.update(index)
        node._CHARACTER_NAMES_LOWER.clear()
        node._CHARACTER_NAMES_LOWER.update(names)
        node._CHARACTER_NAME_LOOKUP.clear()
        node._CHARACTER_NAME_LOOKUP.update(lookup)


class BareNameFunctionTests(_IndexSwapMixin):

    def test_off_mode_returns_nothing(self):
        self.assertIsNone(node._character_bare_name("emilia", OFF))
        self.assertIsNone(node._character_bare_name("rem", OFF))

    def test_exact_match_bare_name(self):
        self.assertEqual(node._character_bare_name("emilia", EXACT), "emilia")
        self.assertEqual(node._character_bare_name("rem", EXACT), "rem")

    def test_exact_match_is_case_insensitive(self):
        self.assertEqual(node._character_bare_name("Emilia", EXACT), "emilia")
        self.assertEqual(node._character_bare_name("EMILIA", EXACT), "emilia")

    def test_underscore_and_space_forms_are_equivalent(self):
        self.assertEqual(node._character_bare_name("hakurei reimu", EXACT), "hakurei_reimu")
        self.assertEqual(node._character_bare_name("hakurei_reimu", EXACT), "hakurei_reimu")

    def test_duplicate_bare_name_keeps_hottest_record(self):
        # rem_(re:zero) (10255) beats rem_(death_note) (160)
        self.assertEqual(node._character_bare_name("rem", EXACT), "rem")

    def test_name_without_copyright_is_returned_as_is(self):
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX["original kid"] = {"name": "original_kid", "copyright": None, "post_count": 5}
        node._CHARACTER_NAMES_LOWER.add("original kid")
        self.assertEqual(node._character_bare_name("original kid", EXACT), "original_kid")

    def test_weights_are_stripped(self):
        self.assertEqual(node._character_bare_name("(emilia:1.2)", EXACT), "emilia")
        self.assertEqual(node._character_bare_name("emilia:1.4", EXACT), "emilia")

    def test_artist_tags_are_never_matched(self):
        self.assertIsNone(node._character_bare_name("@emilia (emilia):0.6", EXACT))
        self.assertIsNone(node._character_bare_name("by rem", EXACT))
        self.assertIsNone(node._character_bare_name("artist: rem", EXACT))

    def test_name_series_tag_is_left_to_the_original_logic(self):
        # both spellings are "name (series)" tags, i.e. round 1 of _auto_candidates
        self.assertIsNone(node._character_bare_name("denia (wuthering waves)", EXACT))
        self.assertIsNone(node._character_bare_name("hakurei_reimu_(touhou)", EXACT))

    def test_unknown_name_returns_none(self):
        self.assertIsNone(node._character_bare_name("not_a_real_character", EXACT))
        self.assertIsNone(node._character_bare_name("1girl", EXACT))
        self.assertIsNone(node._character_bare_name("solo", EXACT))

    def test_numeric_tags_are_ignored(self):
        self.assertIsNone(node._character_bare_name("2", EXACT))
        self.assertIsNone(node._character_bare_name("2022", EXACT))

    def test_fuzzy_match_tolerates_same_length_typo(self):
        # 模糊约束：长度差 <= 1、首字母相同、长度 >= 4、不在停用表、cutoff >= 0.92
        self.assertEqual(node._character_bare_name("hakurei reimuu", FUZZY), "hakurei_reimu")

    def test_fuzzy_rejects_stage_like_tags(self):
        """第三轮修复：stage 曾被误判为角色 sage（相似度 ≈ 0.888）。"""
        # 停用表直接拦下
        self.assertIsNone(node._character_bare_name("stage", FUZZY))
        self.assertIsNone(node._character_bare_name("stage lights", FUZZY))
        self.assertIsNone(node._character_bare_name("live house", FUZZY))
        self.assertIsNone(node._character_bare_name("playing guitar", FUZZY))
        self.assertIsNone(node._character_bare_name("looking at each other", FUZZY))
        # 即便不在停用表里，长度差 > 1 的近似词也不接受
        self.assertIsNone(node._fuzzy_candidate_matches("stge", FUZZY))

    def test_fuzzy_constraints_helpers(self):
        # 低于最低长度
        self.assertIsNone(node._fuzzy_candidate_matches("abc", FUZZY))
        # 停用词
        self.assertIsNone(node._fuzzy_candidate_matches("stage", FUZZY))
        # 关闭 / 精确模式不做模糊匹配
        self.assertIsNone(node._fuzzy_candidate_matches("hakurei reimuu", OFF))
        self.assertIsNone(node._fuzzy_candidate_matches("hakurei reimuu", EXACT))

    def test_fuzzy_short_tag_is_ignored(self):
        # 低于 _BARE_NAME_MIN_LEN(4) 的 tag 不参与模糊匹配
        self.assertIsNone(node._character_bare_name("zb", FUZZY))
        self.assertIsNone(node._character_bare_name("emi", FUZZY))

    def test_fuzzy_never_matches_noise(self):
        for tag in ("1girl", "masterpiece", "long hair", "zzzzzz", "2girls",
                    "stage lights", "detailed background"):
            self.assertIsNone(node._character_bare_name(tag, FUZZY), tag)

    def test_fuzzy_cutoff_is_strict(self):
        # cutoff 已从 0.85 提高到 0.92
        self.assertGreaterEqual(node._FUZZY_CUTOFF, 0.92)
        self.assertEqual(node._FUZZY_MAX_LEN_DIFF, 1)
        self.assertEqual(node._BARE_NAME_MIN_LEN, 4)
        for required in ("stage", "stage lights", "live house", "guitar",
                         "playing guitar", "singing", "smiling", "blush",
                         "looking at viewer", "looking at each other",
                         "holding hands", "standing", "sitting", "walking",
                         "classroom", "park", "beach", "night", "sunset",
                         "dynamic angle", "detailed background",
                         "school uniform", "casual clothes"):
            self.assertIn(required, node._BARE_NAME_STOPWORDS)

    def test_empty_index_disables_everything(self):
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_NAMES_LOWER.clear()
        for mode in (OFF, EXACT, FUZZY):
            self.assertIsNone(node._character_bare_name("emilia", mode))


class AutoCandidateTests(_IndexSwapMixin):

    def candidates(self, texts, bare_mode=OFF):
        return list(node._auto_candidates(list(texts), bare_mode))

    def test_off_mode_is_unchanged(self):
        self.assertEqual(
            self.candidates(["masterpiece, 1girl, denia (wuthering waves), solo"]),
            ["denia_(wuthering_waves)"],
        )

    def test_off_mode_does_not_guess_bare_names(self):
        self.assertEqual(self.candidates(["masterpiece, 1girl, emilia, solo"]), [])

    def test_exact_mode_guesses_bare_names(self):
        self.assertEqual(
            self.candidates(["masterpiece, 1girl, emilia, solo"], EXACT),
            ["emilia"],
        )

    def test_series_tags_still_win_over_bare_names(self):
        self.assertEqual(
            self.candidates(["emilia, denia (wuthering waves)"], EXACT),
            ["denia_(wuthering_waves)", "emilia"],
        )

    def test_artist_and_noise_tags_are_skipped_in_bare_mode(self):
        self.assertEqual(
            self.candidates(["1girl, by (ningen mame:0.5), @hiten (hitenkei):0.6, solo"], EXACT),
            [],
        )

    def test_mixed_rounds_order(self):
        self.assertEqual(
            self.candidates(
                ["masterpiece, frieren, denia (wuthering waves), emilia, rem"],
                EXACT,
            ),
            ["denia_(wuthering_waves)", "frieren", "emilia", "rem"],
        )


class CharNamesTests(_IndexSwapMixin):

    def test_all_bare_names_are_collected(self):
        # 第三轮移除 max_tags：全部收集，不再截断
        self.assertEqual(
            node._char_names(["emilia, rem, frieren"], EXACT),
            ["emilia", "rem", "frieren"],
        )

    def test_char_tag_still_wins(self):
        self.assertEqual(
            node._char_names(["char:my_own_name, emilia"], EXACT),
            ["my_own_name"],
        )

    def test_off_mode_ignores_bare_names(self):
        self.assertEqual(node._char_names(["emilia, rem"], OFF), [])

    def test_parent_child_dedupe(self):
        # 同一角色以 name 与 name (series) 同时出现 -> 保留更长的那条
        self.assertEqual(
            node._char_names(["denia, denia (wuthering waves)"], OFF),
            ["denia_(wuthering_waves)"],
        )

    def test_over_limit_is_capped(self):
        text = ", ".join(f"char:c{i}" for i in range(20))
        self.assertEqual(len(node._char_names([text], OFF)), node._MAX_AUTO_NAMES)


class InputTypesTests(unittest.TestCase):

    def test_panel_has_exactly_the_six_expected_widgets(self):
        """第三轮精简后的节点面板：除这 6 项外不应出现任何其它控件。"""
        types_ = node.CharNameSaveImage.INPUT_TYPES()
        required = types_["required"]
        optional = types_["optional"]
        self.assertEqual(list(required), ["images", "mode", "bare_name_mode",
                                          "padding", "fallback_name"])
        self.assertEqual(list(optional), ["positive_text"])
        self.assertEqual(list(types_["hidden"]), ["prompt", "extra_pnginfo"])

    def test_removed_widgets_are_gone(self):
        types_ = node.CharNameSaveImage.INPUT_TYPES()
        flat = list(types_["required"]) + list(types_["optional"])
        for removed in ("auto_extract", "max_tags", "character_list",
                        "enable_multi_group", "group_tags"):
            self.assertNotIn(removed, flat, removed)

    def test_defaults(self):
        required = node.CharNameSaveImage.INPUT_TYPES()["required"]
        self.assertEqual(required["mode"][1]["default"], "按角色分组文件夹")
        self.assertEqual(required["bare_name_mode"][0],
                         ["关闭", "数据集精确匹配", "数据集模糊匹配"])
        self.assertEqual(required["bare_name_mode"][1]["default"], "数据集模糊匹配")
        self.assertEqual(required["padding"][1]["default"], 5)
        self.assertEqual(required["fallback_name"][1]["default"], "ComfyUI")
        self.assertEqual(required["images"][0], "IMAGE")
        self.assertIs(node.CharNameSaveImage.INPUT_TYPES()["optional"]["positive_text"][1]["forceInput"],
                      True)


class SaveImagesBareModeTests(_IndexSwapMixin):
    """End to end: the file name produced when bare name mode is enabled."""

    def setUp(self):
        super().setUp()
        node.folder_paths.get_output_directory = lambda: self.tmp_dir

        def fake_get_save_image_path(prefix, output_dir, *a, **k):
            subfolder = os.path.dirname(prefix)
            filename = os.path.basename(prefix)
            folder = os.path.join(output_dir, subfolder) if subfolder else output_dir
            os.makedirs(folder, exist_ok=True)
            return folder, filename, 1, subfolder, filename

        node.folder_paths.get_save_image_path = fake_get_save_image_path
        self.saver = node.CharNameSaveImage()

    def _save(self, text, **kwargs):
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}
        result = self.saver.save_images(images, prompt=prompt, **kwargs)
        return result["ui"]["images"][0]["filename"], result["ui"]["text"][0]

    def test_bare_name_names_the_file(self):
        # 默认「按角色分组文件夹」：emilia 落到 output/emilia/emilia_00001_.png
        filename, display = self._save("masterpiece, 1girl, emilia, solo",
                                       bare_name_mode="数据集精确匹配")
        self.assertEqual(filename, "emilia_00001_.png")
        self.assertIn("emilia", display)

    def test_bare_name_file_mode(self):
        filename, display = self._save("masterpiece, 1girl, emilia, solo",
                                       mode="按角色命名文件",
                                       bare_name_mode="数据集精确匹配")
        self.assertEqual(filename, "emilia_00001_.png")
        self.assertIn("emilia", display)

    def test_default_widgets_use_fuzzy_mode(self):
        # 面板默认值：按角色分组文件夹 + 数据集模糊匹配
        filename, display = self._save("masterpiece, 1girl, emilia, solo")
        self.assertEqual(filename, "emilia_00001_.png")
        self.assertIn("emilia", display)

    def test_off_mode_uses_fallback(self):
        filename, display = self._save("masterpiece, 1girl, emilia, solo",
                                       bare_name_mode="关闭")
        self.assertEqual(filename, "ComfyUI_00001_.png")
        self.assertIn("未识别到角色名", display)

    def test_removed_arguments_are_ignored(self):
        # 旧工作流里遗留的控件值不会导致报错（面板已移除这些参数）
        filename, _ = self._save("masterpiece, 1girl, emilia, solo",
                                 auto_extract=True, max_tags=1, character_list="myoc",
                                 enable_multi_group=True, group_tags="双人,多人")
        self.assertEqual(filename, "emilia_00001_.png")


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


class DatasetLoadingTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_load")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._saved = (
            dict(node._CHARACTER_INDEX),
            set(node._CHARACTER_NAMES_LOWER),
            dict(node._CHARACTER_NAME_LOOKUP),
        )
        self.addCleanup(self._restore)

    def _restore(self):
        index, names, lookup = self._saved
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX.update(index)
        node._CHARACTER_NAMES_LOWER.clear()
        node._CHARACTER_NAMES_LOWER.update(names)
        node._CHARACTER_NAME_LOOKUP.clear()
        node._CHARACTER_NAME_LOOKUP.update(lookup)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(node._load_character_index(os.path.join(self.tmp_dir, "nope.jsonl")), 0)
        self.assertEqual(node._CHARACTER_NAMES_LOWER, set())

    def test_bad_lines_are_skipped(self):
        path = os.path.join(self.tmp_dir, "characters.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": "good_one", "copyright": "s", "post_count": 1}) + "\n")
            handle.write("{not json}\n")
            handle.write("\n")
            handle.write(json.dumps({"copyright": "no name"}) + "\n")
            handle.write(json.dumps(["not a dict"]) + "\n")
        self.assertEqual(node._load_character_index(path), 1)
        self.assertEqual(node._CHARACTER_NAMES_LOWER, {"good one"})

    def test_empty_file_yields_empty_index(self):
        path = os.path.join(self.tmp_dir, "characters.jsonl")
        open(path, "w", encoding="utf-8").close()
        self.assertEqual(node._load_character_index(path), 0)
        self.assertEqual(node._CHARACTER_NAMES_LOWER, set())


class RealDatasetTests(unittest.TestCase):
    """Integration checks against the shipped/downloaded dataset."""

    def setUp(self):
        if not os.path.isfile(node._DATASET_PATH):
            self.skipTest(f"dataset not downloaded: {node._DATASET_PATH}")
        if not node._CHARACTER_NAMES_LOWER:
            node._load_character_index()

    def test_index_is_loaded(self):
        self.assertGreater(len(node._CHARACTER_NAMES_LOWER), 1000)

    def test_known_bare_characters(self):
        # 数据集里这些角色都是裸名条目（kita_ikuyo 式），因此输出保持短名：
        # 这是二期改进的预期行为 —— 避免多角色拼接出超长文件名。
        expected = {
            "emilia": "emilia",
            "rem": "rem",
            "saber": "saber",
            "frieren": "frieren",
        }
        for tag, want in expected.items():
            self.assertEqual(node._character_bare_name(tag, EXACT), want, tag)

    def test_bare_names_are_not_expanded_with_series(self):
        # 明确锁定「短名优先」：kita_ikuyo / gotoh_hitori 不会被写成
        # kita_ikuyo_(bocchi_the_rock!) 这种冗长形式
        for tag in ("kita_ikuyo", "gotoh_hitori"):
            got = node._character_bare_name(tag, EXACT)
            if got is None:
                continue  # 数据集里没有这个角色
            self.assertNotIn("(", got, tag)
            self.assertEqual(got, tag)

    def test_noise_tags_do_not_match(self):
        for tag in ("1girl", "solo", "masterpiece", "best quality", "long hair", "absurdres"):
            self.assertIsNone(node._character_bare_name(tag, EXACT), tag)

    def test_artist_tags_do_not_match(self):
        for tag in ("@hiten (hitenkei):0.6", "by (ningen mame:0.5)", "artist: wlop"):
            self.assertIsNone(node._character_bare_name(tag, EXACT), tag)

    def test_off_mode_never_matches(self):
        self.assertIsNone(node._character_bare_name("emilia", OFF))


if __name__ == "__main__":
    unittest.main(verbosity=2)
