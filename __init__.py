import json
import os
import re

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args

_CHAR_TAG_RE = re.compile(r"(?<![a-z0-9_])char:([^<>,\n]+)", re.IGNORECASE)
# danbooru "name (series)" convention, e.g. "denia (wuthering waves)"
_SERIES_NAME_RE = re.compile(r"[\w'\-.]+(?:\s+[\w'\-.]+)*\s*\([^()]*\)")
# artist tags: "@artist" (Anima convention) with optional "(site)" and ":weight", e.g. "@hiten (hitenkei):0.6"
_ARTIST_TAG_RE = re.compile(r"@\S+(?:\s*\([^()]*\))?(?:\s*:\s*\d+(?:\.\d+)?)?")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _sanitize_name(name):
    name = _BAD_NAME_RE.sub("_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name).strip("_.")
    return name[:60]


def _prompt_texts(prompt):
    texts = []
    for node in (prompt or {}).values():
        text = node.get("inputs", {}).get("text")
        if isinstance(text, str) and text.strip() and text not in texts:
            texts.append(text)
    return texts


def _char_names(texts, auto_extract, max_tags):
    names = []
    for text in texts:
        for match in _CHAR_TAG_RE.findall(text):
            name = _sanitize_name(match)
            if name and name not in names:
                names.append(name)
    if not names and auto_extract:
        cleaned_texts = [
            _ARTIST_TAG_RE.sub(" ", t.replace("\\(", "(").replace("\\)", ")"))
            for t in texts
        ]
        for text in cleaned_texts:
            for match in _SERIES_NAME_RE.finditer(text):
                name = _sanitize_name(match.group(0))
                if name and name not in names:
                    names.append(name)
                if len(names) >= max_tags:
                    return names
    return names


_MODES = ["按角色命名文件", "按角色分组文件夹"]
# legacy English values saved by earlier versions, kept for workflow compatibility
_MODE_ALIASES = {"filename": "按角色命名文件", "folder": "按角色分组文件夹"}


class CharNameSaveImage:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.compress_level = 4

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE", {"label": "图像"}),
                "mode": (_MODES + list(_MODE_ALIASES), {
                    "default": "按角色命名文件",
                    "label": "保存方式",
                    "tooltip": "按角色命名文件: 以「角色名_编号」命名输出文件；按角色分组文件夹: 在输出目录下按角色名建立文件夹，文件在文件夹内按编号保存。",
                }),
                "auto_extract": ("BOOLEAN", {
                    "default": True,
                    "label": "自动提取角色名",
                    "tooltip": "提示词中没有 char:角色名 标记时，识别提示词中「角色名 (作品名)」结构的 tag（danbooru 惯例）。",
                }),
                "max_tags": ("INT", {
                    "default": 1, "min": 1, "max": 10,
                    "label": "角色名数量上限",
                    "tooltip": "自动提取时最多采用的角色名数量，多个名字用 _ 连接；多角色建议用 char: 角色名 显式标记。",
                }),
                "padding": ("INT", {
                    "default": 5, "min": 1, "max": 8,
                    "label": "编号位数",
                    "tooltip": "编号位数，例如 5 生成 00001。",
                }),
                "fallback_name": ("STRING", {
                    "default": "ComfyUI",
                    "label": "兜底名称",
                    "tooltip": "识别不到角色名时的名称前缀；默认 ComfyUI 即核心默认命名格式（ComfyUI_00001_.png）。",
                }),
            },
            "optional": {
                "positive_text": ("STRING", {
                    "forceInput": True,
                    "label": "正向提示词",
                    "tooltip": "连接最终合成的正向提示词文本（如 AnimaPromptPlus 的 text 输出）。连接后仅用该文本提取角色名，忽略工作流里其它文本节点。",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "image"
    DESCRIPTION = "根据提示词中的角色名 tag（char:xxx 标记，或自动提取）生成文件名/文件夹来保存图像。"

    def save_images(self, images, mode="按角色命名文件", auto_extract=True, max_tags=1, padding=5, fallback_name="ComfyUI", positive_text=None, prompt=None, extra_pnginfo=None):
        if isinstance(positive_text, str) and positive_text.strip():
            texts = [positive_text]
        else:
            texts = _prompt_texts(prompt)
        names = _char_names(texts, auto_extract, max_tags)
        if names:
            name = "_".join(names)[:150]
            display = f"角色名: {name}"
        else:
            name = (_sanitize_name(fallback_name) or "ComfyUI")[:150]
            display = f"未识别到角色名，使用默认命名: {name}"
        prefix = f"{name}/{name}" if _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹" else name

        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        for (batch_number, image) in enumerate(images):
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            metadata = None
            if not args.disable_metadata:
                metadata = PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for x in extra_pnginfo:
                        metadata.add_text(x, json.dumps(extra_pnginfo[x]))
            file = f"{filename}_{counter:0{padding}d}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=self.compress_level)
            results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            counter += 1

        return {"ui": {"images": results, "text": [display]}, "result": (images,)}


NODE_CLASS_MAPPINGS = {"CharNameSaveImage": CharNameSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {"CharNameSaveImage": "Save Image by Char Tag（按角色名保存）"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
