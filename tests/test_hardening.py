"""Hardening tests: fuzzy determinism, dirty dataset rows, download integrity.

Covers the behaviour added by the optimization pass:

* fuzzy matching is bucketed but still returns exactly what the documented
  constraints allow, and is independent of set-iteration order (PYTHONHASHSEED);
* a dataset row with the wrong field types never breaks a save (the loader used to
  raise ``AttributeError`` on a non-string ``copyright``);
* ``download_dataset.py`` refuses to leave a truncated / oversized / corrupt
  dataset on disk.

Run with the ComfyUI interpreter from the plugin root:

    python tests/test_hardening.py
"""

import io
import json
import os
import shutil
import sys
import threading
import types
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

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
import download_dataset  # noqa: E402
from charnamesave_core import matching as matching_mod  # noqa: E402

OFF = node._BARE_NAME_MODE_OFF
EXACT = node._BARE_NAME_MODE_EXACT
FUZZY = node._BARE_NAME_MODE_FUZZY


class _IndexSwapMixin(unittest.TestCase):
    """Swap the module level index with a synthetic fixture for one test."""

    FIXTURE = ()

    def setUp(self):
        self._saved = (
            dict(node._CHARACTER_INDEX),
            set(node._CHARACTER_NAMES_LOWER),
        )
        self.addCleanup(self._restore)
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_hardening")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def _restore(self):
        index, names = self._saved
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX.update(index)
        node._CHARACTER_NAMES_LOWER.clear()
        node._CHARACTER_NAMES_LOWER.update(names)

    def write_dataset(self, records, filename="characters.jsonl"):
        path = os.path.join(self.tmp_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path


class FuzzyDeterminismTests(unittest.TestCase):
    """模糊匹配：结果稳定、与集合迭代顺序无关、并列时按文档口径择优。"""

    #: 真实数据集里的错拼用例（key -> 期望的唯一候选）
    REAL_PROBES = {
        "kita ikuy": "kita ikuyo",
        "hakurei reim": "hakurei reimu",
    }

    def setUp(self):
        if not node._CHARACTER_NAMES_LOWER:
            self.skipTest("dataset not downloaded")
        self._saved = (
            dict(node._CHARACTER_INDEX),
            set(node._CHARACTER_NAMES_LOWER),
        )
        self.addCleanup(self._restore)
        matching_mod._fuzzy_candidate_matches_cached.cache_clear()
        self.addCleanup(matching_mod._fuzzy_candidate_matches_cached.cache_clear)

    def _restore(self):
        index, names = self._saved
        node._CHARACTER_INDEX.clear()
        node._CHARACTER_INDEX.update(index)
        node._CHARACTER_NAMES_LOWER.clear()
        node._CHARACTER_NAMES_LOWER.update(names)

    def test_repeated_calls_are_identical(self):
        """清掉缓存后重复计算，结果必须完全一样（原来取决于集合迭代顺序）。"""
        for probe, expected in self.REAL_PROBES.items():
            first = matching_mod._fuzzy_candidate_matches(probe, FUZZY)
            self.assertEqual(first, expected)
            for _ in range(5):
                matching_mod._fuzzy_candidate_matches_cached.cache_clear()
                self.assertEqual(
                    matching_mod._fuzzy_candidate_matches(probe, FUZZY), first)

    def test_tie_is_broken_by_length_then_lexicographic(self):
        """并列时取更短者，再并列取字典序更小者，且与插入顺序无关。

        相似度并列在真实数据里几乎不出现，这里把打分函数固定成同分，专门检验择优
        分支本身——原实现直接在 set 上取前 3 名，并列胜者随进程的 PYTHONHASHSEED
        变化，同一个提示词可能落到不同目录。

        注意：扫描的候选集合是**归一化后的 key**（小写 + 下划线转空格），不是记录里的
        显示名，所以期望值也必须按 key 的长度与字典序计算。
        """
        probe = "kita ikuyo!"
        real_score = matching_mod._score_candidate
        matching_mod._score_candidate = lambda key, cand: 0.952381
        self.addCleanup(setattr, matching_mod, "_score_candidate", real_score)

        # 再塞两个「同分」候选：11 字的 "kita ixxxxx"（与 probe 等长、首字母与长度
        # 都落在同一批桶里）与首字母不同的 "sita ikuyo"（首字母约束让分桶看不到它）
        extras = ["kita ixxxxx", "sita ikuyo"]
        index, names = node._CHARACTER_INDEX, node._CHARACTER_NAMES_LOWER
        for key in extras:
            index[key] = {"name": key, "post_count": 0}
            names.add(key)

        # 期望值 = 文档口径（相似度相同 -> 最短 -> 字典序最小），只考虑与扫描集合、
        # 首个字母桶、长度容差一致的候选项
        eligible = [
            key for key in names
            if key and key[0] == probe[0]
            and abs(len(probe) - len(key)) <= node._FUZZY_MAX_LEN_DIFF
            and len(key) >= node._BARE_NAME_MIN_LEN
        ]
        self.assertIn("kita ixxxxx", eligible)
        self.assertNotIn("sita ikuyo", eligible)
        # 文档口径：相似度 -> 名字长度 -> 字典序，三者都升/降方向一致
        expected = min(eligible, key=lambda key: (len(key), key))
        # 必须真的测到并列分支：期望值来自数据集而不是我们插入的候选
        self.assertIn(expected, names)
        self.assertNotIn(expected, extras)
        matching_mod._fuzzy_candidate_matches_cached.cache_clear()

        self.assertEqual(matching_mod._fuzzy_candidate_matches(probe, FUZZY), expected)
        matching_mod._fuzzy_candidate_matches_cached.cache_clear()
        self.assertEqual(matching_mod._fuzzy_candidate_matches(probe, FUZZY), expected)

        # 用同样的候选集合、但换个插入顺序，结果必须一致
        self._restore()
        for key in reversed(extras):
            index[key] = {"name": key, "post_count": 0}
            names.add(key)
        matching_mod._fuzzy_candidate_matches_cached.cache_clear()
        self.assertEqual(matching_mod._fuzzy_candidate_matches(probe, FUZZY), expected)

    def test_stopwords_and_length_constraints_still_hold(self):
        """停用词、最低长度、长度差、cutoff 约束一个都不能松。"""
        self.assertIsNone(matching_mod._fuzzy_candidate_matches("stage", FUZZY))
        self.assertIsNone(matching_mod._fuzzy_candidate_matches("stge", FUZZY))
        self.assertIsNone(node._character_bare_name("stage", FUZZY))
        self.assertIsNone(matching_mod._fuzzy_candidate_matches("kita ikux", FUZZY))
        self.assertIsNone(matching_mod._fuzzy_candidate_matches("rema", FUZZY))


class BucketedScanTests(_IndexSwapMixin):
    """分桶扫描必须与「逐条比较整个集合」的朴素实现结果完全一致。"""

    FIXTURE = [
        {"name": "kita_ikuyo", "copyright": "bocchi_the_rock!", "post_count": 100},
        {"name": "sage", "copyright": "", "post_count": 5},
        {"name": "staged", "copyright": "", "post_count": 5},
        {"name": "emilia_(re:zero)", "copyright": "re:zero", "post_count": 900},
    ]

    def test_bucketed_scan_matches_naive_scan(self):
        from difflib import SequenceMatcher

        node._load_character_index(self.write_dataset(self.FIXTURE))
        candidates = sorted(node._CHARACTER_NAMES_LOWER)

        def naive(key):
            if len(key) < node._BARE_NAME_MIN_LEN or key in node._BARE_NAME_STOPWORDS:
                return None
            best = None
            for cand in candidates:
                if abs(len(key) - len(cand)) > node._FUZZY_MAX_LEN_DIFF:
                    continue
                if key[0] != cand[0] or len(cand) < node._BARE_NAME_MIN_LEN:
                    continue
                score = SequenceMatcher(None, key, cand).ratio()
                if score < node._FUZZY_CUTOFF:
                    continue
                rank = (score, -len(cand), cand)
                if best is None or rank > best:
                    best = rank
            return best[2] if best else None

        probes = ["kita", "kitaa", "kita ikuy", "sag", "sage", "sages", "staged",
                  "stage", "emil", "emilia", "emilias", "kita_ikuyo", "rema"]
        for probe in probes:
            matching_mod._fuzzy_candidate_matches_cached.cache_clear()
            self.assertEqual(
                matching_mod._fuzzy_candidate_matches(probe, FUZZY),
                naive(probe),
                f"mismatch for {probe!r}",
            )


class DirtyDatasetTests(_IndexSwapMixin):
    """脏数据（字段类型不对）不能拖垮加载，更不能在保存路径上抛异常。"""

    def test_non_string_fields_do_not_break_loading(self):
        path = self.write_dataset([
            {"name": "emilia_(re:zero)", "copyright": ["re:zero", "other"], "post_count": "many"},
            {"name": "frieren", "copyright": None, "post_count": None},
            {"name": "rem", "copyright": 123, "post_count": 7},
        ])
        self.assertEqual(node._load_character_index(path), 3)
        self.assertIn("emilia (re:zero)", node._CHARACTER_NAMES_LOWER)
        self.assertIn("emilia", node._CHARACTER_NAMES_LOWER)
        self.assertIn("frieren", node._CHARACTER_NAMES_LOWER)

    def test_output_name_never_raises_on_dirty_records(self):
        # copyright 是 list / 数字 / None，name 是纯名字或空
        self.assertEqual(node._character_output_name({"name": "rem", "copyright": ["re:zero"]}), "rem")
        self.assertEqual(node._character_output_name({"name": "rem", "copyright": 123}), "rem")
        self.assertEqual(node._character_output_name({"name": "rem", "copyright": None}), "rem")
        self.assertIsNone(node._character_output_name({"name": ""}))
        self.assertIsNone(node._character_output_name({"name": None}))

    def test_duplicate_names_keep_highest_post_count(self):
        path = self.write_dataset([
            {"name": "rem", "copyright": "a", "post_count": 5},
            {"name": "rem", "copyright": "b", "post_count": "12"},
            {"name": "rem", "copyright": "c", "post_count": None},
        ])
        node._load_character_index(path)
        self.assertEqual(node._CHARACTER_INDEX["rem"]["copyright"], "b")


class _TruncatingHandler(BaseHTTPRequestHandler):
    """声明一个比实际发送内容更长的 Content-Length（模拟连接被截断）。"""

    body = b"x" * 64
    declared = 100

    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(self.declared))
        self.end_headers()
        self.wfile.write(self.body)

    def do_HEAD(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Length", str(self.declared))
        self.end_headers()

    def log_message(self, *args):  # silence the test server
        pass


class DownloadIntegrityTests(unittest.TestCase):
    """download_dataset.py 的完整性护栏。"""

    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_download")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.server = HTTPServer(("127.0.0.1", 0), _TruncatingHandler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d/characters.jsonl" % self.server.server_port

    def test_truncated_download_is_rejected(self):
        dest = os.path.join(self.tmp_dir, "raw.jsonl")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(IOError):
                download_dataset.download_to(self.url, dest, quiet=True)
        # 目标文件（以及 .part 临时文件）都不能留下
        self.assertFalse(os.path.exists(dest))
        self.assertEqual([n for n in os.listdir(self.tmp_dir) if n.startswith("raw.jsonl")], [])

    def test_oversized_download_is_rejected(self):
        dest = os.path.join(self.tmp_dir, "raw2.jsonl")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(download_dataset.DownloadTooLarge):
                download_dataset.download_to(self.url, dest, quiet=True, max_bytes=10)
        self.assertFalse(os.path.exists(dest))

    def test_complete_download_succeeds(self):
        class _OkHandler(_TruncatingHandler):
            declared = 64

        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/characters.jsonl" % server.server_port
        dest = os.path.join(self.tmp_dir, "raw3.jsonl")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            download_dataset.download_to(url, dest, quiet=True)
        with open(dest, "rb") as handle:
            self.assertEqual(len(handle.read()), _OkHandler.declared)

    def test_corrupt_line_is_a_hard_error(self):
        """截断/损坏的原始文件必须报错，而不是静默产出半个数据集。"""
        corrupt = os.path.join(self.tmp_dir, "corrupt.jsonl")
        with open(corrupt, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": "rem", "copyright": "re:zero"}) + "\n")
            handle.write('{"name": "half_lid\n')  # 断在 JSON 中间

        out = os.path.join(self.tmp_dir, "out.jsonl")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = download_dataset.main(["--from-file", corrupt, "--out", out])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out))

    def test_valid_from_file_round_trip(self):
        raw = os.path.join(self.tmp_dir, "raw.jsonl")
        with open(raw, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": "rem", "copyright": ["re:zero"], "post_count": 9}) + "\n")
            handle.write(json.dumps({"name": "frieren", "post_count": None}) + "\n")
        out = os.path.join(self.tmp_dir, "out2.jsonl")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = download_dataset.main(["--from-file", raw, "--out", out])
        self.assertEqual(code, 0)
        with open(out, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual([r["name"] for r in records], ["rem", "frieren"])
        self.assertEqual(records[0]["copyright"], "re:zero")
        self.assertEqual(records[1]["copyright"], "")

    def test_bare_filename_out_does_not_crash(self):
        """--out 只给文件名时不能再因为 makedirs("") 而崩掉。"""
        raw = os.path.join(self.tmp_dir, "raw2.jsonl")
        with open(raw, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": "rem"}) + "\n")
        cwd = os.getcwd()
        os.chdir(self.tmp_dir)
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = download_dataset.main(["--from-file", raw, "--out", "plain.jsonl"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, "plain.jsonl")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
