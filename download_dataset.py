"""下载并转换「无作品名角色识别」所需的 Danbooru 角色数据集。

数据来源（Hugging Face）::

    https://huggingface.co/datasets/Sn0w123/booru-characters

数据集本身已经是「一行一个 JSON 对象」的 ``characters.jsonl``，本脚本负责：

1. 从 Hugging Face 下载 ``characters.jsonl``（优先使用 ``huggingface_hub``，
   不可用时回退到 ``urllib`` 直连；再不行则提示手动下载）；
2. 把字段映射/规范化成本插件使用的精简结构（见 ``FIELDS`` 注释）；
3. 写入 ``data/characters.jsonl``（原子替换，增量更新：远端文件没变就跳过下载）；
4. 打印统计信息（总条数、有作品名条数等）。

安全护栏（不影响正常使用，只在异常时生效）：

- 下载时校验实际字节数与 ``Content-Length`` 一致，并将总体积限制在 ``MAX_DOWNLOAD_BYTES``
  以内；不一致（连接被截断）或超限时**直接失败**并删除临时文件，绝不写出残缺数据集。
  （这些护栏不引入任何新依赖，只用标准库。）
- 转换时严格解析：任何一行不是合法 JSON 都视为文件损坏（截断的典型表现）并报错退出，
  不再静默跳过坏行——旧行为会把半个文件当成完整数据集写盘。

用法::

    # 先进入本插件目录，再用「你那份 ComfyUI 的 Python 解释器」执行
    cd /d <你的 ComfyUI>/custom_nodes/ComfyUI-CharNameSave
    python download_dataset.py

    # 常用参数
    python download_dataset.py --force            # 忽略增量检查，强制重新下载
    python download_dataset.py --print-url        # 只打印下载链接（手动下载用）
    python download_dataset.py --from-file raw.jsonl   # 用本地已下载的原始文件转换
    python download_dataset.py --limit 500        # 只保留前 500 条（调试用）

    便携版 / 整合包的解释器一般在 <你的 ComfyUI>/python_embeded/python.exe，
    手动部署的可能是 <你的 ComfyUI>/venv/Scripts/python.exe。

也可以完全不用本脚本：手动把符合下列格式的 ``characters.jsonl`` 放进 ``data/``
目录即可，插件启动时会自动加载。

``data/characters.jsonl`` 每行一个 JSON 对象，字段::

    {
      "id": 123,                 # 数据集内部 id（可选，原样保留）
      "name": "emilia_(re:zero)",  # 角色 tag 名（必填，danbooru 惯例用下划线）
      "copyright": "re:zero_kara_hajimeru_isekai_seikatsu",  # 作品名（可能为空串）
      "post_count": 5210         # 热度（可选）
    }
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.request


def _force_utf8_console():
    """Windows 控制台默认可能是 GBK，中文统计信息会乱码，这里尽量切成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_console()

# --- 数据集位置与输出位置 -------------------------------------------------

REPO_ID = "Sn0w123/booru-characters"
REPO_TYPE = "dataset"
# 远端文件名（数据集仓库根目录下就是这个文件）
REMOTE_FILENAME = "characters.jsonl"
# 本插件使用的精简数据集
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "characters.jsonl")
# 手动下载页面
DATASET_PAGE = f"https://huggingface.co/datasets/{REPO_ID}"
MANUAL_URL = f"{DATASET_PAGE}/resolve/main/{REMOTE_FILENAME}"

# 原始数据集字段 -> 输出字段。
# 原始结构（见数据集 dataset_info.json）为：
#   id(int) / name(str) / post_count(int) / gender(str) / clothing(list)
#   characteristics(list) / copyright(list[str]) / relationships(dict)
# 其中 name 即角色 tag 名，copyright 是「作品名 tag 列表」（取第一个最常见的作品），
# post_count 是热度。字段名如与预期不同，只需改这张表。
FIELD_ID = ("id",)
FIELD_NAME = ("name", "tag", "character", "character_tag")
FIELD_COPYRIGHT = ("copyright", "copyrights", "series", "franchise")
FIELD_POST_COUNT = ("post_count", "posts", "count", "post_count_sum")


def _first(data, keys, default=None):
    """按候选字段名依次取值（字段名映射的容错层）。"""
    for key in keys:
        if key in data and data[key] not in (None, "", []):
            return data[key]
    return default


def _as_text(value):
    """把可能是 list/dict/str 的字段压成一个字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for item in value.values():
            text = _as_text(item)
            if text:
                return text
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_record(raw):
    """原始记录 -> 插件使用的精简记录；缺少 ``name`` 时返回 None。"""
    if not isinstance(raw, dict):
        return None
    name = _as_text(_first(raw, FIELD_NAME))
    if not name:
        return None
    record = {
        "name": name,
        # 作品名可能为空（没有作品名的角色，例如 "frieren"）
        "copyright": _as_text(_first(raw, FIELD_COPYRIGHT, "")),
        "post_count": _as_int(_first(raw, FIELD_POST_COUNT, 0)),
    }
    record_id = _first(raw, FIELD_ID)
    if record_id is not None:
        record["id"] = _as_int(record_id)
    return record


def _iter_raw_records(text, strict=True):
    """逐行解析 JSONL；bad line 的处理方式由 strict 决定。

    strict=True（默认，用于**新下载/新转换**的文件）：任何一行不是合法 JSON 都视为
    文件损坏——截断的下载正是这种表现——直接抛 ValueError，绝不把半个文件当成完整
    数据集写出。strict=False 保留旧行为（跳过坏行并打 warning），只给需要宽松解析的
    调用方使用。
    """
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(
                    f"第 {line_no} 行不是合法 JSON（文件可能已损坏/被截断）: {exc}") from exc
            print(f"[warn] 跳过第 {line_no} 行（JSON 解析失败：{exc}）", file=sys.stderr)


def convert_text(text):
    """原始 JSONL 文本 -> 精简记录列表（保持原有顺序）；坏行直接报错。"""
    records = []
    for raw in _iter_raw_records(text):
        record = normalize_record(raw)
        if record:
            records.append(record)
    return records


def convert_file(src_path, limit=None):
    with open(src_path, "r", encoding="utf-8") as handle:
        records = convert_text(handle.read())
    if limit:
        records = records[:limit]
    return records


# --- 下载 -----------------------------------------------------------------

# 下载体积上限：正常原始 JSONL 约 9 MB，留足余量。超限说明远端文件异常（或被换成了
# 别的东西），直接中止，避免把磁盘写满。
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class DownloadTooLarge(Exception):
    """下载体积超过 MAX_DOWNLOAD_BYTES。"""


def _open_url(url, timeout=30, method=None):
    """打开 URL；method 为 None 时用 GET，'HEAD' 时只取响应头。"""
    request = urllib.request.Request(url, method=method, headers=_headers())
    return urllib.request.urlopen(request, timeout=timeout)


def _declared_size(response):
    """响应的 Content-Length；缺失/非法/动态压缩时返回 None（此时不做字节数断言）。"""
    if (response.headers.get("Content-Encoding") or "").strip().lower() not in ("", "identity"):
        # 传输是 gzip 等压缩编码时，Content-Length 是压缩后长度，与落盘字节数不可比
        return None
    length = response.headers.get("Content-Length")
    try:
        return int(length) if length else None
    except (TypeError, ValueError):
        return None


def remote_file_size(url, timeout=30):
    """HEAD 请求取远端文件大小；失败返回 None（此时不做增量跳过）。"""
    try:
        with _open_url(url, timeout=timeout, method="HEAD") as response:
            return _declared_size(response)
    except Exception as exc:  # noqa: BLE001 - 网络问题一律降级处理
        print(f"[warn] 无法获取远端文件大小（{exc}）", file=sys.stderr)
        return None


def _headers():
    return {
        # 手动指定 UA，避免个别网络环境下默认 UA 被拒
        "User-Agent": "ComfyUI-CharNameSave-download_dataset/1.0",
    }


def resolve_url(repo_id=REPO_ID, filename=REMOTE_FILENAME):
    """优先用 huggingface_hub 生成 URL（会走正确的 revision），否则手工拼。"""
    try:
        from huggingface_hub import hf_hub_url

        return hf_hub_url(repo_id, filename, repo_type=REPO_TYPE)
    except Exception:  # noqa: BLE001 - 没有 huggingface_hub 也能用
        return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{filename}"


def download_to(url, dest_path, timeout=300, quiet=False, max_bytes=MAX_DOWNLOAD_BYTES):
    """流式下载到 ``dest_path``（先写临时文件，成功后再原子替换）。

    写出前会做两项校验，任一不通过都**不替换目标文件**并删除临时文件：

    1. 实际写入字节数与服务端声明的 ``Content-Length`` 不一致（连接被中断/截断）；
    2. 超过 ``max_bytes``（远端文件异常巨大，避免写满磁盘）。

    服务端未声明长度（分块传输/压缩编码）时跳过第 1 项，只保留第 2 项的硬上限，
    因此不会把正常下载误判为失败。
    """
    tmp_path = f"{dest_path}.part-{os.getpid()}"
    try:
        with _open_url(url, timeout=timeout) as response, open(tmp_path, "wb") as handle:
            declared = _declared_size(response)
            if declared is not None and declared > max_bytes:
                raise DownloadTooLarge(
                    f"远端文件声明大小 {declared} 字节，超过上限 {max_bytes} 字节")
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                done += len(chunk)
                if done > max_bytes:
                    raise DownloadTooLarge(
                        f"下载已超过上限 {max_bytes} 字节，已中止")
                handle.write(chunk)
                if not quiet and declared:
                    print(f"\r  下载中 {done / 1048576:.1f}/{declared / 1048576:.1f} MB", end="")
            handle.flush()
            os.fsync(handle.fileno())
            if not quiet and declared:
                print()
            # 截断检测：字节数对不上说明传输不完整，宁可不替换也不留半个文件
            if declared is not None and done != declared:
                raise IOError(
                    f"下载不完整：收到 {done} 字节，服务端声明 {declared} 字节")
        os.replace(tmp_path, dest_path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def try_hf_hub_download(repo_id=REPO_ID, filename=REMOTE_FILENAME, force=False):
    """使用 huggingface_hub 下载原始文件，返回本地缓存路径；失败返回 None。

    作为 urllib 直连失败时的备选方案。``hf_hub_download`` 的缓存目录默认在用户
    目录下，部分环境（受限权限 / 只读用户目录）不可写，因此这里把缓存放到本插件
    目录下的 ``.hf_cache/``。
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("[info] 未安装 huggingface_hub（可选依赖），跳过该下载方式。")
        return None

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hf_cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        return hf_hub_download(
            repo_id, filename, repo_type=REPO_TYPE,
            cache_dir=os.path.join(cache_dir, "hub"),
            force_download=force,
        )
    except Exception as exc:  # noqa: BLE001 - 无权限/无网络都视为该方式不可用
        print(f"[warn] huggingface_hub 下载失败（{type(exc).__name__}: {exc}）")
        return None


def fetch_raw_file(args, url):
    """取到「原始 JSONL 文件」的本地路径；全部方式失败返回 None。"""
    raw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hf_cache", args.filename)

    # 1) 直连下载（不依赖任何第三方库与缓存目录，最稳）
    print(f"[info] 下载: {url}")
    try:
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        download_to(url, raw_path)
        return raw_path
    except DownloadTooLarge as exc:
        # 体积护栏触发：远端文件异常，换 huggingface_hub 也一样超限，直接给出结论
        print(f"[error] {exc}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 直连下载失败（{type(exc).__name__}: {exc}）")

    # 2) 回退 huggingface_hub
    print("[info] 尝试 huggingface_hub 下载 …")
    hf_path = try_hf_hub_download(args.repo, args.filename, force=args.force)
    if hf_path:
        return hf_path

    # 3) 全部失败：打印手动下载指引
    print("[error] 自动下载失败，请手动下载后转换：", file=sys.stderr)
    print(f"  1) 打开数据集页面: {DATASET_PAGE}", file=sys.stderr)
    print(f"  2) 下载 {args.filename}: {url}", file=sys.stderr)
    print(f"  3) 运行: python {os.path.basename(__file__)} --from-file <下载到的文件>", file=sys.stderr)
    return None


# --- 输出 -----------------------------------------------------------------


def write_jsonl(records, dest_path=OUTPUT_PATH):
    """原子写入 JSONL，返回写入字节数。"""
    directory = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".characters-", suffix=".tmp")
    written = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
            written = handle.tell()
        os.replace(tmp_path, dest_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return written


def _meta_path(dest_path=OUTPUT_PATH):
    return dest_path + ".meta"


def _write_meta(dest_path, meta):
    """记录本次转换的远端标识，供下次增量判断使用。"""
    try:
        with open(_meta_path(dest_path), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[warn] 写入元信息失败（不影响使用）: {exc}", file=sys.stderr)


def _is_up_to_date(dest_path, remote_size):
    """输出文件与元信息里的远端标识都存在且一致 -> 无需重新下载。"""
    if remote_size is None:
        return False
    try:
        with open(_meta_path(dest_path), "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    return meta.get("remote_size") == remote_size


def print_stats(records, dest_path=OUTPUT_PATH):
    total = len(records)
    with_series = sum(1 for r in records if r.get("copyright"))
    without_series = total - with_series
    parenthesised = sum(1 for r in records if "(" in r.get("name", ""))
    ambiguous = {}
    for record in records:
        base = record["name"].rsplit("_(", 1)[0].strip().lower().replace("_", " ")
        ambiguous[base] = ambiguous.get(base, 0) + 1
    print("=" * 60)
    print(f"数据集: {REPO_ID}")
    print(f"输出文件: {dest_path}")
    print(f"总条数: {total}")
    print(f"  含作品名(copyright 非空): {with_series}")
    print(f"  作品名为空: {without_series}")
    print(f"  name 里已带括号后缀: {parenthesised}")
    print(f"  去掉括号后缀后的重名组数: {sum(1 for c in ambiguous.values() if c > 1)}")
    print("=" * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="下载/转换 Sn0w123/booru-characters 数据集到本插件的 data/characters.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default=OUTPUT_PATH,
                        help=f"输出路径（默认插件目录下的 {OUTPUT_PATH}；相对路径按当前工作目录解析）")
    parser.add_argument("--repo", default=REPO_ID, help=f"Hugging Face 数据集仓库（默认 {REPO_ID}）")
    parser.add_argument("--filename", default=REMOTE_FILENAME, help="远端文件名（默认 characters.jsonl）")
    parser.add_argument("--from-file", default=None,
                        help="不联网，直接把本地已下载的原始 JSONL 转成精简数据集；"
                             "相对路径按当前工作目录解析（注意：这条路径不经过下载完整性校验）")
    parser.add_argument("--force", action="store_true", help="忽略增量检查，强制重新下载")
    parser.add_argument("--limit", type=int, default=None,
                        help="只保留前 N 条（调试用；会写出一个不完整的数据集，别当成正式数据用）")
    parser.add_argument("--print-url", action="store_true", help="只打印手动下载链接后退出")
    args = parser.parse_args(argv)

    url = resolve_url(args.repo, args.filename)

    if args.print_url:
        # 这里打印解析后的绝对路径，与真正写出的位置一致（普通模式也是先 abspath 再写）
        print("手动下载地址:")
        print(f"  数据文件: {url}")
        print(f"  数据集页: {DATASET_PAGE}")
        print(f"下载后把文件放到: {os.path.abspath(args.out)}")
        return 0

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    # --out 只给文件名时 dirname 是空串，旧写法会 makedirs("") 直接抛异常
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    raw_file = None
    remote_size = None
    if args.from_file:
        raw_file = os.path.abspath(args.from_file)
        if not os.path.isfile(raw_file):
            print(f"[error] 找不到文件: {raw_file}", file=sys.stderr)
            return 2
        print(f"[info] 使用本地原始文件: {raw_file}")
    else:
        # 增量更新：把「远端文件标识（ETag/大小）」记在 characters.jsonl.meta 里，
        # 标识没变且输出存在时跳过下载。注意不能直接比文件大小：下载的是原始
        # JSONL（字段多），写出的是精简后的 JSONL，两者大小不同。
        remote_size = remote_file_size(url)
        if not args.force and os.path.isfile(out_path) and _is_up_to_date(out_path, remote_size):
            print(f"[info] 已是最新（远端 {remote_size} 字节未变），跳过下载。")
            records = convert_file(out_path, args.limit)
            if records:
                print_stats(records, out_path)
                return 0
            print("[warn] 本地输出无法解析或为空，改为重新下载。")

        raw_file = fetch_raw_file(args, url)
        if raw_file is None:
            return 1

    try:
        records = convert_file(raw_file, args.limit)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"[error] 原始文件无法解析: {exc}", file=sys.stderr)
        print("       文件可能下载不完整，请删掉 .hf_cache/ 下对应文件后重试，"
              "或加 --force 强制重新下载。", file=sys.stderr)
        return 1
    if not records:
        print("[error] 转换后没有任何有效记录，请检查原始文件格式。", file=sys.stderr)
        return 1
    written = write_jsonl(records, out_path)
    _write_meta(out_path, {
        "repo": args.repo,
        "filename": args.filename,
        "remote_size": remote_size if not args.from_file else None,
    })
    print_stats(records, out_path)
    print(f"[ok] 已写入 {written} 字节 -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
