"""The ComfyUI node class and its registration mappings."""

import json
import logging
import os

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args

from .constants import (
    _BARE_NAME_MODE_FUZZY,
    _BARE_NAME_MODES,
    _DEFAULT_MODE,
    _MODE_ALIASES,
    _MODES,
)
from .naming import _build_save_prefix, _char_names
from .textparse import _prompt_texts, _sanitize_name

_LOGGER = logging.getLogger(__name__)


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
                    "default": _DEFAULT_MODE,
                    "label": "保存方式",
                    "tooltip": "按角色分组文件夹（默认）: 在输出目录下按角色名建立文件夹，文件在文件夹内按编号保存；"
                               "按角色命名文件: 以「角色名_编号」命名输出文件。"
                               "识别到多个角色时会自动加一层分组目录（2 人 Duo、3 人及以上 Group）。",
                }),
                "bare_name_mode": (_BARE_NAME_MODES, {
                    "default": _BARE_NAME_MODE_FUZZY,
                    "label": "无作品名角色识别",
                    "tooltip": "识别没有作品名后缀的裸名字 tag（如 Emilia、Rem、frieren）。"
                               "关闭: 只按原有「角色名 (作品名)」规则识别（与最初版本一致）；"
                               "数据集精确匹配: 名字需与内嵌 Danbooru 角色数据集精确命中（忽略大小写与下划线/空格差异）；"
                               "数据集模糊匹配（默认）: 在精确匹配之后允许 difflib 近似匹配，"
                               "但带停用词、最低长度 4、长度差 ≤ 1、首字母相同、cutoff 0.92 等约束，"
                               "避免 stage→sage 这类短词误判。"
                               "数据集位于 data/characters.jsonl，用 download_dataset.py 下载；文件缺失时自动回退原有逻辑。",
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
    DESCRIPTION = (
        "根据提示词中的角色名 tag（char:xxx 标记，或自动提取）生成文件名/文件夹来保存图像。"
        "自动提取支持 danbooru 惯例的「角色名 (作品名)」tag；「无作品名角色识别」可用内嵌的"
        "Danbooru 角色数据集（data/characters.jsonl）检索 Emilia、Rem 这类裸名字 tag。"
        "角色数量自动检测：2 个角色归入 Duo 目录、3 个及以上归入 Group 目录（可用英文名以"
        "避免中文路径的兼容性问题），子目录名对角色名做确定性排序，同一提示词顺序变化也落在"
        "同一目录；cosplay 等元标签（(cosplay)、(alternate_costume)…）不会被当成作品名。"
    )

    def save_images(self, images, mode=_DEFAULT_MODE,
                    bare_name_mode=_BARE_NAME_MODE_FUZZY,
                    padding=5, fallback_name="ComfyUI", positive_text=None,
                    prompt=None, extra_pnginfo=None, **legacy):
        # **legacy 兼容旧工作流里遗留的参数（auto_extract / max_tags / character_list /
        # enable_multi_group / group_tags）。它们已从节点面板移除：
        #   auto_extract / enable_multi_group 永久开启；max_tags / character_list 不再生效；
        #   group_tags 写死为 _MULTI_GROUP_TAGS。这里只是接收后忽略，避免旧工作流报错。
        if legacy:
            _LOGGER.debug("忽略已移除的历史参数: %s", sorted(legacy))
        if isinstance(positive_text, str) and positive_text.strip():
            texts = [positive_text]
        else:
            texts = _prompt_texts(prompt)
        # 自动检测角色数量（不再有 max_tags 上限，超过 _MAX_AUTO_NAMES 会打 warning）
        names = _char_names(texts, bare_name_mode)
        # 分组决策：0 个走兜底、1 个保持原样、2 个进 Duo、3 个及以上进 Group
        prefix, display = _build_save_prefix(names, mode)
        if prefix is None:
            name = (_sanitize_name(fallback_name) or "ComfyUI")[:150]
            prefix = f"{name}/{name}" if _MODE_ALIASES.get(mode, mode) == "按角色分组文件夹" else name
            display = f"未识别到角色名，使用默认命名: {name}"

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
