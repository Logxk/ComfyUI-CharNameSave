"""Unit tests for the character-name extraction of ComfyUI-CharNameSave.

Run with the ComfyUI embedded interpreter, e.g.

    F:\\ComfyUI-aki-v3\\python\\python.exe tests\\test_extract.py

`folder_paths` / `comfy.cli_args` are stubbed so the node module can be
imported outside of ComfyUI.
"""

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


def names(texts, auto_extract=True, max_tags=1):
    return node._char_names(list(texts), auto_extract, max_tags)


class _FakeTensor:
    """Minimal stand-in for the torch IMAGE tensor used by save_images."""

    def __init__(self, array, shape=None):
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


class ArtistTagTests(unittest.TestCase):
    """Artist tags must never be reported as character names (issue report)."""

    def test_by_parenthesised_artist_with_weight(self):
        self.assertEqual(
            names(["masterpiece, best quality, 1girl, by (ningen mame:0.5), solo"]),
            [],
        )

    def test_by_parenthesised_artist_space_weight(self):
        self.assertEqual(names(["1girl, by (ningen mame 0.5), solo"]), [])

    def test_by_underscored_artist(self):
        self.assertEqual(names(["by_ningen_mame_0.5, 1girl, solo"]), [])

    def test_by_without_space(self):
        self.assertEqual(names(["1girl, by(ningen mame:0.5), solo"]), [])

    def test_by_bare_artist(self):
        self.assertEqual(names(["by ningen mame, 1girl, solo"]), [])

    def test_drawn_by_artist(self):
        self.assertEqual(names(["drawn by wlop, 1girl, solo"]), [])

    def test_artist_prefix(self):
        self.assertEqual(names(["artist: wlop, 1girl, solo"]), [])

    def test_at_artist_with_site_and_weight(self):
        self.assertEqual(names(["1girl, @hiten (hitenkei):0.6, solo"]), [])

    def test_artist_tag_without_comma_separators(self):
        # legacy whole-text fallback must not resurrect the artist tag
        self.assertEqual(names(["1girl by (ningen mame:0.5) solo"]), [])

    def test_by_like_character_name_is_kept(self):
        self.assertEqual(names(["byakko (touhou)"]), ["byakko_(touhou)"])


class CharacterTagTests(unittest.TestCase):

    def test_plain_series_name(self):
        self.assertEqual(
            names(["masterpiece, denia (wuthering waves), solo"]),
            ["denia_(wuthering_waves)"],
        )

    def test_multi_word_character(self):
        self.assertEqual(
            names(["professor niyaniya (blue archive)"]),
            ["professor_niyaniya_(blue_archive)"],
        )

    def test_escaped_parentheses(self):
        self.assertEqual(
            names([r"1girl, denia \(wuthering waves\), solo"]),
            ["denia_(wuthering_waves)"],
        )

    def test_character_with_weight(self):
        self.assertEqual(
            names(["denia (wuthering waves):1.2"]),
            ["denia_(wuthering_waves)"],
        )

    def test_character_wrapped_in_weight(self):
        self.assertEqual(
            names(["(denia (wuthering waves):1.2), solo"]),
            ["denia_(wuthering_waves)"],
        )

    def test_character_next_to_artist_tag(self):
        self.assertEqual(
            names(["by (ningen mame:0.5), denia (wuthering waves), solo"]),
            ["denia_(wuthering_waves)"],
        )

    def test_character_after_artist_in_space_separated_text(self):
        self.assertEqual(
            names(["1girl by (ningen mame:0.5) denia (wuthering waves)"]),
            ["denia_(wuthering_waves)"],
        )

    def test_newline_separated_tags(self):
        self.assertEqual(
            names(["1girl\nby (ningen mame:0.5)\ndenia (wuthering waves)"]),
            ["denia_(wuthering_waves)"],
        )

    def test_character_with_parenthesised_weight_is_ignored(self):
        # "(0.8)" is an emphasis weight, not a series
        self.assertEqual(names(["1girl (0.8), solo"]), [])

    def test_bare_tags_are_not_guessed(self):
        self.assertEqual(names(["masterpiece, 1girl, solo, long hair"]), [])

    def test_max_tags_limit(self):
        self.assertEqual(
            names(["a (series one), b (series two)"], max_tags=1),
            ["a_(series_one)"],
        )
        self.assertEqual(
            names(["a (series one), b (series two)"], max_tags=2),
            ["a_(series_one)", "b_(series_two)"],
        )

    def test_auto_extract_disabled(self):
        self.assertEqual(names(["denia (wuthering waves)"], auto_extract=False), [])


class ExplicitCharTagTests(unittest.TestCase):

    def test_char_tag_wins(self):
        self.assertEqual(
            names(["char:hakurei_reimu, by (ningen mame:0.5)"]),
            ["hakurei_reimu"],
        )

    def test_char_tag_bracketed(self):
        self.assertEqual(
            names(["<char:denia (wuthering waves)>, 1girl"]),
            ["denia_(wuthering_waves)"],
        )

    def test_char_tag_ignores_auto_extract_setting(self):
        self.assertEqual(
            names(["char:hakurei_reimu, denia (wuthering waves)"], auto_extract=False),
            ["hakurei_reimu"],
        )

    def test_multiple_char_tags_ignore_max_tags(self):
        # documented: max_tags only limits auto extraction
        self.assertEqual(
            names(["char:a, char:b, char:c"], max_tags=1),
            ["a", "b", "c"],
        )

    def test_non_ascii_char_tag(self):
        self.assertEqual(names(["char:初音ミク, 1girl"]), ["初音ミク"])

    def test_char_tag_name_truncated_to_60_chars(self):
        self.assertEqual(names(["char:" + "x" * 80]), ["x" * 60])

    def test_duplicate_texts_are_scanned_once(self):
        self.assertEqual(names(["char:a", "char:a"]), ["a"])


class SaveImagesTests(unittest.TestCase):
    """End to end check of the produced file name (what the issue reported)."""

    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_output")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        node.folder_paths.get_output_directory = lambda: self.tmp_dir

        def fake_get_save_image_path(prefix, output_dir, *a, **k):
            subfolder = os.path.dirname(prefix)
            filename = os.path.basename(prefix)
            folder = os.path.join(output_dir, subfolder) if subfolder else output_dir
            os.makedirs(folder, exist_ok=True)
            counter = 1
            while os.path.exists(
                    os.path.join(folder, f"{filename}_{counter:05d}_.png")):
                counter += 1
            return folder, filename, counter, subfolder, filename

        node.folder_paths.get_save_image_path = fake_get_save_image_path
        self.saver = node.CharNameSaveImage()

    def _save(self, text, **kwargs):
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}
        result = self.saver.save_images(images, prompt=prompt, **kwargs)
        return result["ui"]["images"][0]["filename"], result["ui"]["text"][0]

    def test_artist_tag_uses_fallback_name(self):
        filename, display = self._save(
            "masterpiece, best quality, 1girl, by (ningen mame:0.5), solo")
        self.assertEqual(filename, "ComfyUI_00001_.png")
        self.assertIn("未识别到角色名", display)
        self.assertEqual(sorted(os.listdir(self.tmp_dir)), ["ComfyUI_00001_.png"])

    def test_character_still_names_the_file(self):
        filename, display = self._save(
            "masterpiece, 1girl, by (ningen mame:0.5), denia (wuthering waves)")
        self.assertEqual(filename, "denia_(wuthering_waves)_00001_.png")
        self.assertIn("denia_(wuthering_waves)", display)

    def test_joined_name_truncated_to_150_chars(self):
        text = ", ".join("char:" + c * 60 for c in "abc")
        joined = ("a" * 60 + "_" + "b" * 60 + "_" + "c" * 60)[:150]
        # 3 个角色 + 多人自动分组（默认开启）: 文件名带「多人」前缀
        filename, _ = self._save(text)
        self.assertEqual(filename, "多人_" + joined + "_00001_.png")
        # 关闭多人自动分组后回到旧的多角色拼接命名
        filename, _ = self._save(text, enable_multi_group=False)
        self.assertEqual(filename, joined + "_00001_.png")
        self.assertEqual(len(filename) - len("_00001_.png"), 150)

    def test_grouped_folder_mode(self):
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"inputs": {"text": "char:hakurei_reimu"}}}
        result = self.saver.save_images(images, mode="按角色分组文件夹", prompt=prompt)
        saved = result["ui"]["images"][0]
        self.assertEqual(saved["subfolder"], "hakurei_reimu")
        self.assertEqual(saved["filename"], "hakurei_reimu_00001_.png")
        self.assertTrue(os.path.isfile(os.path.join(
            self.tmp_dir, "hakurei_reimu", "hakurei_reimu_00001_.png")))

    def test_legacy_english_mode_value(self):
        images = _FakeTensor(np.zeros((1, 8, 8, 3), dtype=np.float32))
        prompt = {"1": {"inputs": {"text": "char:hakurei_reimu"}}}
        result = self.saver.save_images(images, mode="folder", prompt=prompt)
        self.assertEqual(result["ui"]["images"][0]["subfolder"], "hakurei_reimu")


if __name__ == "__main__":
    unittest.main(verbosity=2)
