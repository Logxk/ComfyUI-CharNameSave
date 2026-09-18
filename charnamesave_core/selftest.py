"""In-module self test (``_self_test`` / ``_run_self_test``), kept verbatim."""

from .constants import (
    _BARE_NAME_MODE_EXACT,
    _BARE_NAME_MODE_FUZZY,
    _BARE_NAME_MODE_OFF,
    _BARE_NAME_STOPWORDS,
    _DEFAULT_MODE,
    _MULTI_GROUP_TAGS,
    _MAX_AUTO_NAMES,
)
from .matching import (
    _character_bare_name,
    _character_name_from_tag,
    _dataset_lookup,
    _fuzzy_candidate_matches,
)
from .naming import (
    _build_save_prefix,
    _char_names,
    _dedupe_character_names,
    _sorted_character_names,
)
from .textparse import _clean_path_part


def _self_test():
    """极简自测：模糊匹配约束（stage/sage）、多人分组与 cosplay 元标签。

    只依赖纯函数（不写磁盘、不需要 ComfyUI 启动），配合已下载的数据集效果最好。
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

    在本插件目录下，用你那份 ComfyUI 的解释器执行：

        python -c "import types,sys; sys.modules['folder_paths']=types.ModuleType('folder_paths'); c=types.ModuleType('comfy'); a=types.ModuleType('comfy.cli_args'); a.args=types.SimpleNamespace(disable_metadata=True); c.cli_args=a; sys.modules['comfy']=c; sys.modules['comfy.cli_args']=a; import __init__ as m; raise SystemExit(m._run_self_test())"

    配合已下载/随仓库分发的 data/characters.jsonl 效果最好（含数据集相关的检查项）。
    """
    return _self_test()
