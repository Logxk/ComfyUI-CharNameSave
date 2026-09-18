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


def names(texts, mode=None):
    """识别文本里的角色名。

    第三轮起 _char_names 不再有 auto_extract / max_tags 参数（自动提取永久开启、
    角色数量自动检测），默认用「关闭」数据集识别，即只认 "name (series)"。
    """
    return node._char_names(list(texts), mode or node._BARE_NAME_MODE_OFF)


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

    def test_all_series_names_are_collected(self):
        # 第三轮移除 max_tags：自动检测，全部收集
        self.assertEqual(
            names(["a (series one), b (series two)"]),
            ["a_(series_one)", "b_(series_two)"],
        )
        self.assertEqual(
            names(["a (series one), b (series two), c (series three)"]),
            ["a_(series_one)", "b_(series_two)", "c_(series_three)"],
        )

    def test_parent_child_names_keep_the_longer_one(self):
        # 同一角色同时以 name 与 name (series) 出现 -> 只保留更长的那条
        self.assertEqual(
            names(["denia, denia (wuthering waves)"]),
            ["denia_(wuthering_waves)"],
        )
        self.assertEqual(
            names(["denia (wuthering waves), denia"]),
            ["denia_(wuthering_waves)"],
        )

    def test_too_many_characters_are_capped(self):
        text = ", ".join(f"char:c{i}" for i in range(12))
        self.assertEqual(len(names([text])), node._MAX_AUTO_NAMES)


class ExplicitCharTagTests(unittest.TestCase):

    def test_char_tag_wins_over_auto_extraction(self):
        # char: 标记存在时只采用它们（保持旧行为），不再混入自动提取结果
        self.assertEqual(
            names(["char:hakurei_reimu, denia (wuthering waves)"]),
            ["hakurei_reimu"],
        )

    def test_char_tag_bracketed(self):
        self.assertEqual(
            names(["<char:denia (wuthering waves)>, 1girl"]),
            ["denia_(wuthering_waves)"],
        )

    def test_multiple_char_tags_are_all_kept(self):
        self.assertEqual(
            names(["char:a, char:b, char:c"]),
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
        # 默认「按角色分组文件夹」：兜底名称会建同名子目录
        self.assertEqual(sorted(os.listdir(self.tmp_dir)), ["ComfyUI"])

    def test_character_still_names_the_file(self):
        filename, display = self._save(
            "masterpiece, 1girl, by (ningen mame:0.5), denia (wuthering waves)")
        self.assertEqual(filename, "denia_(wuthering_waves)_00001_.png")
        self.assertIn("denia_(wuthering_waves)", display)

    def test_joined_name_truncated_to_150_chars(self):
        text = ", ".join("char:" + c * 60 for c in "abc")
        # 多角色拼接后按 _MULTI_NAME_MAX_LEN(150) 截断，去掉截断产生的尾部 "_"
        joined = ("a" * 60 + "_" + "b" * 60 + "_" + "c" * 60)[:150].rstrip("_")
        self.assertEqual(len(joined), 150)
        # 3 个角色 -> 自动归入 Group 分组（英文目录名）；这里只看文件名模式
        filename, _ = self._save(text, mode="按角色命名文件")
        self.assertEqual(filename, "Group_" + joined + "_00001_.png")
        self.assertEqual(filename, node._MULTI_GROUP_TAGS[1] + "_" + joined + "_00001_.png")

    def test_default_mode_is_grouped_folder(self):
        filename, _ = self._save("char:hakurei_reimu")
        # 默认保存方式已改为按角色分组文件夹
        self.assertEqual(filename, "hakurei_reimu_00001_.png")
        self.assertTrue(os.path.isfile(os.path.join(
            self.tmp_dir, "hakurei_reimu", "hakurei_reimu_00001_.png")))

    def test_two_characters_get_duo_prefix(self):
        filename, _ = self._save("char:kita_ikuyo, char:gotoh_hitori",
                                 mode="按角色命名文件")
        self.assertEqual(filename, "Duo_gotoh_hitori_kita_ikuyo_00001_.png")

    def test_legacy_widget_values_do_not_crash(self):
        # 旧工作流里遗留的参数（已从面板移除）应被忽略而不是报错
        filename, _ = self._save(
            "char:hakurei_reimu",
            auto_extract=True, max_tags=1, character_list="", group_tags="双人,多人",
            enable_multi_group=True)
        self.assertEqual(filename, "hakurei_reimu_00001_.png")

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
