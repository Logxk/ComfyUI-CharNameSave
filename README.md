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

**可选**：想识别没有作品名的裸名字（`Emilia`、`Rem`、`frieren` 这类），需要下载一次角色数据集。

先进入本插件目录（不同安装方式的路径不同，按自己的来），然后执行下载脚本：

```bat
cd /d <你的 ComfyUI>\custom_nodes\ComfyUI-CharNameSave
python download_dataset.py
```

> `python` 要换成**你那份 ComfyUI 用的解释器**：便携版/整合包通常是 `..\..\python_embeded\python.exe`，手动部署的可能是 `..\..\venv\Scripts\python.exe`，桌面版请在它的 Python 环境里执行。系统里已经装好 Python 且能直接运行 `python` 的话，上面的命令就能用。
>
> 若不想先 `cd`，可以用解释器绝对路径直接执行脚本，例如
> `"F:\你的 ComfyUI\venv\Scripts\python.exe" "F:\你的 ComfyUI\custom_nodes\ComfyUI-CharNameSave\download_dataset.py"`
> —— 不加任何参数时它会写到脚本自己所在的 `data/`，与当前工作目录无关。

脚本会从 Hugging Face 下载 [`Sn0w123/booru-characters`](https://huggingface.co/datasets/Sn0w123/booru-characters) 并生成 `data/characters.jsonl`（约 2 MB，两万余条角色）。下载失败时脚本会打印手动下载指引，也可以主动查看：

```bat
python download_dataset.py --print-url
python download_dataset.py --from-file <下载到的 characters.jsonl>
```

> 上面两条是**接在同一个插件目录下**执行（第二条的 `<下载到的 characters.jsonl>` 换成你实际
> 下载到的文件，相对路径按当前工作目录解析）；把 `python` 换成你那份 ComfyUI 的解释器，
> 规则同前。`--from-file` 只做格式转换，**不经过下载完整性校验**，文件是否完整要自己确认。
>
> `--print-url` 里「下载后把文件放到」打印的是解析后的绝对路径，它在任何工作目录下运行都
> 指向插件自己的 `data/`，因此可以放心照抄。

> 脚本内置两项完整性护栏（都用标准库，不引入新依赖）：下载后校验实际字节数与
> 服务端声明的 `Content-Length` 一致（连接被截断时会**直接报错**，不会写出残缺数据集），
> 并把下载体积限制在 64 MB 以内；转换时任何一行不是合法 JSON 都会报错退出，
> 不再静默跳过坏行。遇到这类报错，删掉 `.hf_cache/` 下的原始文件后重跑即可，
> 或加 `--force` 强制重新下载。
>
> 脚本默认**覆盖** `data/characters.jsonl`。调试用的 `--limit N` 会写出一个只有前 N 条的
> 不完整数据集，用完请重新完整跑一次，否则「无作品名角色识别」会大面积漏识别。

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
   - `数据集精确匹配`：忽略大小写与下划线/空格差异。例如 `rem` 会命中数据集里的 `rem_(re:zero)`，但**文件名用短名 `rem`**（去掉括号后缀的写法）；同一角色在数据集里有多个版本时取 `post_count`（热度）最高的那条。
   - `数据集模糊匹配`：在精确匹配之后允许近似匹配（`difflib` 相似度，先用首字母与长度差把候选缩到极小范围再比较），带停用词、最低长度 4、长度差 ≤ 1、首字母相同、`cutoff 0.92` 等约束，避免 `stage` 被误判成角色 `sage`。
4. **以上都没有**：使用「兜底名称」。

> **同一提示词永远得到同一个名字**：相似度并列时按「相似度 → 名字长度 → 字典序」
> 确定性择优，不依赖集合遍历顺序，因此不受 Python 哈希随机化影响，重启 ComfyUI
> 或换一台机器都不会把同一张图存进不同目录。

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

在本插件目录下执行（同样把 `python` 换成你那份 ComfyUI 的解释器）：

```bat
cd /d <你的 ComfyUI>\custom_nodes\ComfyUI-CharNameSave
python tests\test_extract.py
python tests\test_bare_name.py
python tests\test_multi_group.py
python tests\test_hardening.py
```

| 测试 | 覆盖内容 |
| --- | --- |
| `test_extract.py` | tag 提取、画师过滤、显式 `char:`、保存流程 |
| `test_bare_name.py` | 数据集加载、裸名精确/模糊匹配、节点面板 |
| `test_multi_group.py` | 多人分组、cosplay 元标签、内置自测（`_self_test`） |
| `test_hardening.py` | 模糊匹配确定性与分桶等价性、脏数据（字段类型错误）不再中断加载、下载脚本的截断/超限/损坏文件护栏 |

> 这三个主测试会 `import numpy`（跑节点保存流程需要），用 ComfyUI 自己的解释器执行即可满足；
> `test_hardening.py` 只用标准库，任何 Python 3.8+ 都能跑。
> 前三个测试用自带的合成数据，**不需要** `data/characters.jsonl`；只有它们内部少数「真实数据集」
> 用例在数据集缺失时自动 skip，不会失败。测试会在 `tests/` 下建临时目录并自动清理。

## 代码结构

实现按职责拆分到 `charnamesave_core/` 子包，插件根目录的 `__init__.py` 只做注册与
向后兼容再导出（保持原有内部函数名，测试与外部扩展无需改动）：

```
ComfyUI-CharNameSave/
├── __init__.py               # 入口：注册节点 + 兼容层再导出（支持 ComfyUI 包加载与直接 import）
├── charnamesave_core/
│   ├── constants.py          # 全部常量 / 正则 / 阈值 / 停用词（值与原版一致）
│   ├── textparse.py          # tag 解析：去权重、反转义、清洗文件名…
│   ├── dataset.py            # Danbooru 数据集加载与索引状态
│   ├── matching.py           # 角色名匹配：name(series) / 元标签 / 裸名 / 模糊
│   ├── naming.py             # 去重、数量检测、分组与前缀组装
│   ├── node.py               # CharNameSaveImage 节点类
│   └── selftest.py           # 内置自测（_self_test / _run_self_test）
├── download_dataset.py       # 数据集下载脚本
├── data/characters.jsonl     # 角色数据集（可选，下载后生成）
└── tests/                    # 单元测试
```

> 兼容性：测试文件都以 `import __init__` 方式导入，`__init__.py` 会重新导出全部
> 历史内部名（`_char_names`、`_build_save_prefix`、`_dataset_lookup` …）。ComfyUI 以
> 包方式加载时使用相对导入，二者均已验证通过。

### 性能说明

模糊匹配把候选名按「首字母 + 长度」分桶后再比较（首字母相同、长度差 ≤ 1 本来就是硬
约束，所以结果与遍历整个集合完全等价），并按数据集状态缓存结果。数据集有两万余条
角色名，优化前后实测（同一台机器、同一份数据集）：

| 场景 | 优化前 | 优化后 |
| --- | --- | --- |
| 单个未命中 tag 的模糊匹配 | ~21 ms | ~0.9 ms |
| 一条长提示词的完整识别（冷启动） | ~165 ms | ~3.3 ms |
| 同一提示词再次识别（命中缓存） | ~159 ms | ~0.4 ms |

> 识别结果只在**首次**遇到某条提示词时付出这个开销，ComfyUI 自身的节点缓存会让重复
> 执行直接复用上次结果。

数据集在插件导入时加载一次（约 2 万条、60–200 ms），文件缺失或损坏时只打日志并回退
到「角色名 (作品名)」识别，不影响启动。

## 许可证

[MIT](LICENSE)
