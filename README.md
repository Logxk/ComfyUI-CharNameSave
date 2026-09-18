# ComfyUI-CharNameSave

按提示词里的**角色名**自动命名 / 分组保存图像的自定义输出节点。

| 项目 | 说明 |
| --- | --- |
| 节点名 | **Save Image by Char Tag（按角色名保存）** |
| 节点类名 | `CharNameSaveImage` |
| 分类 | `image` |
| 输入 / 输出 | `images` (IMAGE) → `images` (IMAGE，原样透传) |
| 依赖 | 无（只用 ComfyUI 自带的 numpy 与 Pillow） |
| 许可证 | [MIT](LICENSE) |

它做的事情只有一件：**用识别到的角色名决定文件名或文件夹**。

| 提示词里的角色名 | 输出（默认「按角色分组文件夹」） |
| --- | --- |
| `char:hakurei_reimu` | `output/hakurei_reimu/hakurei_reimu_00001_.png` |
| `denia (wuthering waves)` | `output/denia_(wuthering_waves)/denia_(wuthering_waves)_00001_.png` |
| `kita_ikuyo, gotoh_hitori`（2 个角色） | `output/Duo/gotoh_hitori_kita_ikuyo_00001_.png` |
| 3 个及以上角色 | `output/Group/gotoh_hitori_ijichi_nijika_kita_ikuyo_00001_.png` |
| 只有 `1girl, solo, long hair` 之类 | `output/ComfyUI/ComfyUI_00001_.png`（兜底名称） |
| 只有画师串 `by (ningen mame:0.5)` | `output/ComfyUI/ComfyUI_00001_.png`（画师不算角色） |

> 「按角色命名文件」方式下，多角色的分组标签会写进文件名，例如 2 个角色 → `output/Duo_gotoh_hitori_kita_ikuyo_00001_.png`。

## 安装

1. 把本目录放到 `ComfyUI/custom_nodes/ComfyUI-CharNameSave`。
2. **重启 ComfyUI**（改动过插件代码也要重启，Python 模块会被缓存）。
3. 无需 `pip install`。

**可选**：想识别没有作品名的裸名字（`Emilia`、`Rem`、`frieren` 这类），需要下载一次角色数据集：

```bat
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py
```

脚本会从 Hugging Face 下载 [`Sn0w123/booru-characters`](https://huggingface.co/datasets/Sn0w123/booru-characters) 并生成 `data/characters.jsonl`（约 2 MB，两万余条角色）。下载失败时会打印手动下载指引：

```bat
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py --print-url
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py --from-file characters.jsonl
```

不下载也能正常使用，只是「无作品名角色识别」不可用，其余功能不受影响。

## 怎么用

1. 添加节点 **Save Image by Char Tag（按角色名保存）**（分类 `image`），把图像接到 `images`。
2. 提示词里写角色名 tag：

   ```
   masterpiece, best quality, 1girl, solo, char:hakurei_reimu, red dress
   ```

   也可以直接用 danbooru 风格的 tag，节点会自动识别：

   ```
   masterpiece, 1girl, denia (wuthering waves), solo
   masterpiece, 1girl, emilia, rem, solo          （需要数据集）
   ```

3. 执行后节点下方会显示本次识别结果，图片按角色名保存到 `output/` 下。

> 提示词由通配符 / 中间件 / 多场景切换动态生成时，请把**最终合成的文本**接到节点的「正向提示词」输入口，否则静态扫描拿不到运行时真实文本。

### 节点参数

| 界面参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `图像` | IMAGE | — | 要保存的图像批次 |
| `保存方式` | 下拉 | `按角色分组文件夹` | `按角色分组文件夹` / `按角色命名文件` |
| `无作品名角色识别` | 下拉 | `数据集模糊匹配` | `关闭` / `数据集精确匹配` / `数据集模糊匹配` |
| `编号位数` | 整数 1–8 | `5` | 编号补零位数，5 → `00001` |
| `兜底名称` | 字符串 | `ComfyUI` | 识别不到角色名时的名称前缀 |
| `正向提示词` | 字符串（输入口） | 空 | 可选，只能连线；连接后只用它提取角色名 |

### 角色名的来源

按以下顺序，**先命中先使用**：

1. **`char:角色名`**（最可靠）：提示词里写 `char:hakurei_reimu`，兼容 `<char:hakurei_reimu>`；可以写多个。
2. **自动提取**：danbooru 的 `角色名 (作品名)` 结构（`hakurei_reimu_(touhou)` 这种下划线写法同样识别），自动跳过画师 tag（`@画师` / `by 画师` / `by (画师:权重)` / `drawn by 画师` / `artist: 画师`）。
3. **无作品名角色识别**（需数据集）：用数据集检索 `Emilia`、`Rem` 这类裸名字。
   - `数据集精确匹配`：忽略大小写与下划线/空格差异；`rem` → `rem_(re:zero)`（同名多角色取热度最高者）。
   - `数据集模糊匹配`：在精确匹配之后允许 `difflib` 近似匹配，带停用词、最低长度 4、长度差 ≤ 1、首字母相同、`cutoff 0.92` 等约束，避免 `stage` 被误判成角色 `sage`。
4. **以上都没有**：使用「兜底名称」。

`(cosplay)`、`(alternate_costume)`、`(clothing_swap)`、`(crossdressing)` 等元标签不会被当成作品名；数据集查不到该角色时会退化为 `角色名_cosplay`。

### 命名规则

- 文件名格式：`名称_编号_.png`，编号按「编号位数」补零。
- 名称清洗：`\ / : * ? " < > |` 与控制字符替换为 `_`，空白替换为 `_`，连续 `_` 合并，去掉首尾 `_` 和 `.`。
- 长度上限：单个角色名 60 字符，多角色拼接后 150 字符。
- 编号取同名前缀在磁盘上的最大编号 + 1（与内置 Save Image 一致）。
- 不支持 `%date%` / `%width%` 之类的变量。

### 角色数量与分组

- 角色数量自动检测，最多采用 8 个（超过时只取前 8 个并在日志里警告）。
- 同一角色同时以 `name` 与 `name (series)` 出现时只保留更长的那条。
- 2 个角色 → `Duo` 目录，3 个及以上 → `Group` 目录；子目录名对角色名排序，**提示词里 tag 顺序变化不会新建目录**。
- 单个角色不加任何分组前缀。

## 测试

```bat
F:\ComfyUI\venv\Scripts\python.exe tests\test_extract.py
F:\ComfyUI\venv\Scripts\python.exe tests\test_bare_name.py
F:\ComfyUI\venv\Scripts\python.exe tests\test_multi_group.py
```

## 许可证

[MIT](LICENSE)
