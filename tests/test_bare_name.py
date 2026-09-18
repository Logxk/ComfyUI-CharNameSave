"""Unit tests for the dataset-backed "bare character name" recognition.

Run with the ComfyUI embedded interpreter, e.g.

    F:\\ComfyUI\\venv\\Scripts\\python.exe tests\\test_bare_name.py

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

    def test_fuzzy_match_tolerates_typos(self):
        self.assertEqual(node._character_bare_name("hakurei reimuu", FUZZY), "hakurei_reimu")

    def test_fuzzy_short_tag_is_ignored(self):
        # "2b" is 2 chars -> below _FUZZY_MIN_LEN, so no fuzzy guessing
        self.assertIsNone(node._character_bare_name("zb", FUZZY))

    def test_fuzzy_never_matches_noise(self):
        for tag in ("1girl", "masterpiece", "long hair", "zzzzzz"):
            self.assertIsNone(node._character_bare_name(tag, FUZZY), tag)

    def test_empty_index_disables_everything(self):
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_NAMES_LOWER.clear()
        for mode in (OFF, EXACT, FUZZY):
            self.assertIsNone(node._character_bare_name("emilia", mode))


class AutoCandidateTests(_IndexSwapMixin):

    def candidates(self, texts, bare_mode=OFF, character_list=None):
        return list(node._auto_candidates(list(texts), bare_mode, character_list))

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

    def test_character_list_matches_before_dataset(self):
        # the name as written in the list is kept ("CustomChar" -> CustomChar)
        self.assertEqual(
            self.candidates(["1girl, customchar, solo"], OFF, ["CustomChar"]),
            ["CustomChar"],
        )

    def test_character_list_is_skipped_for_artist_tags(self):
        self.assertEqual(self.candidates(["by customchar"], OFF, ["customchar"]), [])

    def test_mixed_rounds_order(self):
        self.assertEqual(
            self.candidates(
                ["masterpiece, frieren, denia (wuthering waves), emilia, rem"],
                EXACT,
            ),
            ["denia_(wuthering_waves)", "frieren", "emilia", "rem"],
        )


class CharNamesTests(_IndexSwapMixin):

    def test_max_tags_limits_bare_matches(self):
        self.assertEqual(
            node._char_names(["emilia, rem, frieren"], True, 2, EXACT),
            ["emilia", "rem"],
        )

    def test_char_tag_still_wins(self):
        self.assertEqual(
            node._char_names(["char:my_own_name, emilia"], True, 3, EXACT),
            ["my_own_name"],
        )

    def test_auto_extract_disabled_ignores_bare_names(self):
        self.assertEqual(node._char_names(["emilia, rem"], False, 3, EXACT), [])

    def test_character_list_parameter(self):
        self.assertEqual(
            node._char_names(["1girl, myoc, solo"], True, 2, OFF, ["myoc"]),
            ["myoc"],
        )

    def test_split_character_list_separators(self):
        self.assertEqual(node._split_character_list("a, b;c\nd  ,, e"), ["a", "b", "c", "d", "e"])
        self.assertEqual(node._split_character_list(""), [])
        self.assertEqual(node._split_character_list(None), [])
        self.assertEqual(node._split_character_list(["a,b", " c "]), ["a", "b", "c"])


class InputTypesTests(unittest.TestCase):

    def test_new_widgets_exist_with_chinese_modes(self):
        required = node.CharNameSaveImage.INPUT_TYPES()["required"]
        self.assertEqual(required["bare_name_mode"][0], ["关闭", "数据集精确匹配", "数据集模糊匹配"])
        self.assertEqual(required["bare_name_mode"][1]["default"], "关闭")
        self.assertEqual(required["character_list"][1]["default"], "")
        self.assertEqual(required["character_list"][1]["multiline"], True)
        # original parameters are still there
        for key in ("images", "mode", "auto_extract", "max_tags", "padding", "fallback_name"):
            self.assertIn(key, required)

    def test_bare_name_mode_is_after_max_tags(self):
        keys = list(node.CharNameSaveImage.INPUT_TYPES()["required"])
        self.assertLess(keys.index("max_tags"), keys.index("bare_name_mode"))
        self.assertLess(keys.index("bare_name_mode"), keys.index("character_list"))


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
        filename, display = self._save("masterpiece, 1girl, emilia, solo", bare_name_mode="数据集精确匹配")
        # 二期起采用「短名优先」：裸名识别不再补作品名，避免多角色文件名过长
        self.assertEqual(filename, "emilia_00001_.png")
        self.assertIn("emilia", display)

    def test_default_mode_is_unchanged(self):
        filename, display = self._save("masterpiece, 1girl, emilia, solo")
        self.assertEqual(filename, "ComfyUI_00001_.png")
        self.assertIn("未识别到角色名", display)

    def test_character_list_argument(self):
        filename, _ = self._save("1girl, myoc, solo", character_list="myoc, other")
        self.assertEqual(filename, "myoc_00001_.png")

    def test_character_list_accepts_workflow_list_value(self):
        filename, _ = self._save("1girl, myoc, solo", character_list=["myoc", "other"])
        self.assertEqual(filename, "myoc_00001_.png")


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
