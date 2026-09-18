# ComfyUI-CharNameSave

按提示词里的**角色名 tag** 自动命名 / 分组保存图像的自定义输出节点。

| 项目 | 说明 |
| --- | --- |
| 节点名 | **Save Image by Char Tag（按角色名保存）** |
| 节点类名 | `CharNameSaveImage` |
| 分类 | `image` |
| 输入 / 输出 | `images` (IMAGE) → `images` (IMAGE，原样透传) |
| 额外依赖 | 无（仅使用 ComfyUI 自带的 numpy 与 Pillow）；「无作品名角色识别」需要可选的本地数据集 `data/characters.jsonl` |

它在内置 Save Image 的基础上做了一件事：**用识别到的角色名决定文件名或文件夹**。

| 提示词里的角色名来源 | 实际输出（`保存方式 = 按角色命名文件`） |
| --- | --- |
| `char:hakurei_reimu` | `output/hakurei_reimu_00001_.png` |
| `denia (wuthering waves)`（自动提取） | `output/denia_(wuthering_waves)_00001_.png` |
| 只有 `1girl, solo, long hair` 等裸 tag | `output/ComfyUI_00001_.png`（走兜底名称） |
| 只有画师串 `by (ningen mame:0.5)` | `output/ComfyUI_00001_.png`（画师不算角色） |
| `emilia`（开启「无作品名角色识别」） | `output/emilia_(re_zero)_00001_.png`（数据集检索） |

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [角色名的来源](#角色名的来源)
- [无作品名角色识别（数据集）](#无作品名角色识别数据集)
- [多人自动分组](#多人自动分组)
- [cosplay 等元标签](#cosplay-等元标签)
- [命名规则](#命名规则)
- [两种保存模式](#两种保存模式)
- [参数](#参数)
- [输入与输出](#输入与输出)
- [常见问题与排错](#常见问题与排错)
- [已知限制](#已知限制)
- [测试](#测试)
- [更新记录](#更新记录)

## 安装

本目录已位于 `ComfyUI/custom_nodes/ComfyUI-CharNameSave`，**重启 ComfyUI** 后生效（改动过插件代码同样要重启，Python 模块会被缓存）。节点本身无额外依赖，不需要 `pip install`。

「无作品名角色识别」需要一份**可选**的本地数据集 `data/characters.jsonl`（约 2 MB）。下载方式见[无作品名角色识别（数据集）](#无作品名角色识别数据集)；**不下载也能正常用**，只是该功能不可用，其余行为完全不变。

## 快速开始

1. 添加节点 **Save Image by Char Tag（按角色名保存）**（分类 `image`），把图像接到 `images`。
2. 在提示词里写显式标记：

   ```
   masterpiece, best quality, 1girl, solo, char:hakurei_reimu, red dress
   ```

3. 执行后节点下方会显示本次识别结果，输出文件形如 `output/hakurei_reimu_00001_.png`。

提示词由通配符 / 中间件 / 多场景切换动态生成时，请把**最终合成的文本**接到节点的「正向提示词」输入口（见下一节）。

## 角色名的来源

按以下顺序决定名称，**先命中先使用**：

1. **正向提示词输入口（推荐）**：把最终合成的提示词文本（例如 AnimaPromptPlus 的 `text` 输出）接到「正向提示词」输入口。连接后**只用该文本**提取角色名，工作流里其它文本节点被忽略。通配符 / 中间件 / 多场景切换必须走这条路，否则静态扫描拿不到运行时真实文本。
2. **显式标记（最可靠）**：提示词里写 `char:角色名`，同时兼容 `<char:角色名>`：

   ```
   masterpiece, best quality, 1girl, solo, char:hakurei_reimu, red dress
   ```

   可以写多个 `char:`，全部采用（不受「角色名数量上限」限制），用 `_` 连接。
3. **自动提取**（需 `自动提取角色名` 开启，且上面两种都没命中才执行）：
   - 先剔除画师 tag，避免画师名被当成角色。被识别为画师的写法：`@画师`（Anima 惯例，如 `@hiten (hitenkei):0.6`）、`by 画师` / `by (画师:权重)`（如 `by (ningen mame:0.5)`）、`by_画师`、`drawn by 画师`、`artist: 画师`；
   - 再按 danbooru 惯例找 `角色名 (作品名)` 结构，如 `denia (wuthering waves)`、`professor niyaniya (blue archive)`；danbooru 的下划线写法同样支持：`hakurei_reimu_(touhou)`；
   - 权重写法会被还原：`角色名 (作品名):1.2` 与 `(角色名 (作品名):1.2)` 都算 `角色名 (作品名)`；括号里只有数字时按权重处理，不算作品名（`1girl (0.8)` 不会产出名字）；
   - 最多取「角色名数量上限」个，多个名字用 `_` 连接；
   - 转义括号（`\(` `\)`）会先还原再判断。
4. **兜底**：以上都没识别到时使用「兜底名称」（默认 `ComfyUI`，即核心默认命名格式 `ComfyUI_00001_.png`）。提示词里只有裸 tag（没有 `作品名` 括号结构）时不会乱猜，直接走兜底 —— 这类提示词请用 `char:角色名`，或开启下面的「无作品名角色识别」。

未连接「正向提示词」输入口时，工作流里**所有带 `text` 输入的节点**（正向、负向文本节点都会被扫到）都会参与静态扫描，重复文本自动去重。

## 无作品名角色识别（数据集）

有些提示词只写裸名字 tag，没有 `(作品名)` 后缀，老逻辑认不出来（例如 `Emilia`、`Rem`、`frieren`）。开启「无作品名角色识别」后，节点会用一份内嵌的 **Danbooru 角色数据集** 去检索这类 tag。

### 数据集从哪来

数据集来源：[`Sn0w123/booru-characters`](https://huggingface.co/datasets/Sn0w123/booru-characters)（从 Danbooru API 抽取的角色 tag 元数据，两万余条）。用插件根目录的脚本一键下载并转换成插件使用的格式：

```bat
:: 用 ComfyUI 自带解释器（推荐，环境最稳）
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py

:: 强制重新下载 / 只看下载链接 / 用本地已下载的原始文件转换
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py --force
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py --print-url
F:\ComfyUI\venv\Scripts\python.exe download_dataset.py --from-file characters.jsonl
```

脚本会写入 `data/characters.jsonl`（每行一个 JSON 对象：`name` 角色 tag、`copyright` 作品名、`post_count` 热度），并在结束后打印总条数等统计信息。**增量更新**：远端文件没变（ETag/大小一致）时直接跳过下载；联网下载失败时会打印手动下载指引。转换脚本会把 `tag`/`series` 之类的其它字段名自动映射到 `name`/`copyright`，也会跳过坏行。

不想跑脚本也可以手动下载：打开[数据集页面](https://huggingface.co/datasets/Sn0w123/booru-characters)，下载 `characters.jsonl`，然后用 `--from-file` 转换（或直接把符合上述格式的文件命名为 `data/characters.jsonl` 放好）。**文件不存在时插件照常启动**，只在后台日志里打一条 warning。

### 三种模式

| 模式 | 行为 | 例子（提示词里写 `emilia`） |
| --- | --- | --- |
| `关闭`（默认） | 只用原有规则，裸名字一律不猜 | 走兜底 `ComfyUI_00001_.png` |
| `数据集精确匹配`（推荐） | 名字必须命中数据集：忽略大小写、忽略下划线/空格差异，并自动补上数据集里的作品名 | `emilia_(re_zero)_00001_.png` |
| `数据集模糊匹配` | 在精确匹配基础上，再用 `difflib` 近似匹配（`cutoff=0.85`），能容忍拼写/空格差异，但**可能误判** | `emilie` 之类也可能命中 `emilia_(re_zero)` |

匹配细节：

- 先按数据集里的完整 tag 精确匹配（`hakurei_reimu_(touhou)`），再按去掉 `_(版本/服装名)` 后缀的基础名匹配（`hakurei reimu` → `hakurei_reimu_(touhou)`）；
- 同名多角色时取 `post_count` 最高的那条（如 `rem` → `rem_(re:zero)` 而非 `rem_(death_note)`）；
- 已经是 `角色名 (作品名)` 的 tag 仍走原有逻辑，优先级更高；画师 tag、`1girl`/`solo` 之类的通用 tag 不会被匹配；
- 太短的名字（< 3 字符）不参与模糊匹配，避免误判；
- 数据集缺失或为空时，三种模式都不会命中，自动回退到原有逻辑。

### 手动名单 `已知角色名名单`

数据集覆盖不到的原创角色（OC）或自造名字，可以在「已知角色名名单」里手填，用逗号、分号或换行分隔：

```
myoc, 原创角色A
another_char
```

名单匹配**大小写不敏感**，且在数据集匹配之前生效（作为补充，不影响 `char:` 标记与原逻辑）。

### 裸名字输出「短名」

数据集里一个角色可能写作 `emilia_(re:zero)`、`saber_(fate)`，而多角色提示词里每个名字都带作品名拼起来会得到超长文件名。因此数据集命中的角色名统一取**基础名（裸名）**：

| 提示词 | 识别结果 | 说明 |
| --- | --- | --- |
| `emilia` / `Emilia` | `emilia` | 数据集里只有 `emilia_(re:zero)`，取基础名 |
| `kita_ikuyo` | `kita_ikuyo` | 数据集里就是裸名条目 |
| `hakurei_reimu_(touhou)` | `hakurei_reimu` | 完整写法也归一到裸名 |
| `denia (wuthering waves)`（数据集里没有 denia 裸名） | `denia` | 取基础名 |
| `denia (别的作品)` | `denia_(wuthering_waves)` | 提示词作品名与数据集不一致时不做无依据的收缩 |

好处：同一角色无论提示词大小写/写法如何，都落到同一个文件夹；多角色组合名也不会长得离谱。

## 多人自动分组

一条提示词里出现多个角色时（`max_tags` ≥ 2 且确实识别到多个名字），会自动按人数分组，避免出现
`gotoh_hitori_(cosplay)_kita_ikuyo_(bocchi_the_rock!)_00001_.png` 这类超长文件名，也避免 tag 顺序变化导致目录碎片化。

| 识别到的角色数 | 分组 | 文件夹模式输出 | 文件名模式输出 |
| --- | --- | --- | --- |
| 0 | 不分组（兜底名称） | `output/ComfyUI_00001_.png` | 同左 |
| 1 | 不分组（保持旧行为） | `output/hakurei_reimu/hakurei_reimu_00001_.png` | `output/hakurei_reimu_00001_.png` |
| 2 | `双人` | `output/双人/kita_ikuyo_gotoh_hitori/kita_ikuyo_gotoh_hitori_00001_.png` | `output/双人_kita_ikuyo_gotoh_hitori_00001_.png` |
| ≥ 3 | `多人` | `output/多人/<排序后的名字>/<排序后的名字>_00001_.png` | `output/多人_<排序后的名字>_00001_.png` |

要点：

- **顺序无关**：子目录/文件名里的角色名会先做确定性排序（`sorted(names, key=str.lower)`），所以
  `kita_ikuyo, gotoh_hitori` 与 `gotoh_hitori, kita_ikuyo` 落在**完全相同**的路径里，不会产生新组合目录；
  写入的仍是名字的原始大小写。
- **自动去重**：同一条提示词里重复出现的角色只算一次（`kita_ikuyo, gotoh_hitori, kita_ikuyo` → 双人）。
- **可关闭**：「多人自动分组」关掉后回到旧的多角色拼接命名（`gotoh_hitori_kita_ikuyo/...`），保证旧工作流升级后行为可预测。
- **可改标签**：「分组标签」默认 `双人,多人`（两个逗号分隔的标签，依次用于双人、多人）；中文标签不会被清洗，只过滤文件名非法字符。
- 单个角色**不会**加「单人」前缀，输出与旧版本完全一致。

## cosplay 等元标签

`(cosplay)`、`(alternate_costume)`、`(clothing_swap)`、`(crossdressing)`、`(cosplaying)`、`(cosplay_costume)` 这些是**元标签**（描述玩法/服装），不是作品名。以前 `gotoh_hitori (cosplay)` 会被当成 `name (series)` 结构，产出 `gotoh_hitori_(cosplay)_00001_.png`；现在改为：

| 提示词 | 识别结果 | 输出（文件夹模式） |
| --- | --- | --- |
| `gotoh_hitori (cosplay)` | `gotoh_hitori`（数据集命中，取规范短名） | `output/gotoh_hitori/gotoh_hitori_00001_.png` |
| `kita_ikuyo (cosplay), gotoh_hitori (cosplay), cosplay` | `kita_ikuyo` + `gotoh_hitori` | `output/双人/gotoh_hitori_kita_ikuyo/...` |
| `someone_unknown (cosplay)`（数据集/名单都没有） | `someone_unknown_cosplay` | 裸名 + 固定后缀，稳定可预测 |

- 命中数据集或「已知角色名名单」时，直接采用规范名字，不加后缀；
- 完全查不到时才用 `裸名 + _cosplay` 兜底（不含括号，`(cosplay)` 绝不会出现在路径里）；
- 想改成「cosplay 图与普通图放同一个目录」，把 `__init__.py` 里的 `_META_SERIES_SUFFIX` 改为 `""` 即可；
- 想扩充/缩减元标签表，改 `_META_SERIES`（`cosplay`、`alternate costume`、`alternate-costume` 等写法都能识别）。

## 命名规则

- 文件名格式：`名称_编号_.png`，编号按「编号位数」补零，例如 `denia_(wuthering_waves)_00001_.png`。
- 名称清洗：`\ / : * ? " < > |` 与控制字符替换为 `_`，空白替换为 `_`，连续 `_` 合并成一个，去掉首尾的 `_` 和 `.`。所以 `denia (wuthering waves)` → `denia_(wuthering_waves)`，`by (ningen mame:0.5)` → `by_(ningen_mame_0.5)`。
- 长度上限：单个角色名清洗后截断到 60 字符，拼接后的整体名称截断到 150 字符。
- 编号 = 输出目录中**同名前缀**已有文件的最大编号 + 1，与内置 Save Image 一致。因此删除已保存的文件后编号会按剩余文件重新计算，**可能再次使用被删掉的编号**。
- 不支持 `%date%` / `%width%` 之类的变量，文件名与文件夹名完全由角色名决定。

## 两种保存模式

| 保存方式 | 效果 |
| --- | --- |
| `按角色命名文件`（默认） | 同一角色名共用一条编号序列：`output/hakurei_reimu_00001_.png`、`output/hakurei_reimu_00002_.png`… |
| `按角色分组文件夹` | 在输出目录下按角色名建文件夹，文件夹内单独编号：`output/hakurei_reimu/hakurei_reimu_00001_.png`… |

## 参数

| 界面参数 | 内部名 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `保存方式` | `mode` | 下拉 | `按角色命名文件` | `按角色命名文件` / `按角色分组文件夹`；旧版本保存的英文值 `filename` / `folder` 仍被接受并自动映射，下拉里保留仅为兼容旧工作流 |
| `自动提取角色名` | `auto_extract` | 布尔 | 开启 | 无 `char:` 标记时是否按 `角色名 (作品名)` 结构自动提取；自动提取会自动跳过画师 tag |
| `角色名数量上限` | `max_tags` | 整数 1–10 | 1 | **仅对自动提取生效**，多个名字用 `_` 连接；显式 `char:` 标记不受此限制 |
| `无作品名角色识别` | `bare_name_mode` | 下拉 | `关闭` | `关闭` / `数据集精确匹配` / `数据集模糊匹配`，用内嵌 Danbooru 角色数据集识别没有作品名的裸名字 tag（如 `Emilia`）；详见[上文](#无作品名角色识别数据集) |
| `已知角色名名单` | `character_list` | 多行字符串 | 空 | 可选的手动补充名单（逗号 / 分号 / 换行分隔，大小写不敏感），先于数据集匹配生效，适合 OC 或数据集里没有的名字 |
| `多人自动分组` | `enable_multi_group` | 布尔 | 开启 | 识别到 2 个角色归入「双人」、3 个及以上归入「多人」（子目录对角色名做确定性排序，顺序无关）；关闭则沿用旧的多角色拼接命名；详见[多人自动分组](#多人自动分组) |
| `分组标签` | `group_tags` | 字符串 | `双人,多人` | 两个逗号分隔的标签，依次用于双人、多人分组；不足 2 项时回退默认值 |
| `编号位数` | `padding` | 整数 1–8 | 5 | 编号补零位数，5 → `00001` |
| `兜底名称` | `fallback_name` | 字符串 | `ComfyUI` | 识别不到角色名时使用的名称前缀，默认即核心格式 `ComfyUI_00001_.png` |
| `正向提示词` | `positive_text` | 字符串（输入口） | 空 | 可选输入口（无组件，只能连线）；连接后只用它提取角色名 |

## 输入与输出

- 输入 `images`（IMAGE）：要保存的图像批次，逐个保存并连续编号。
- 可选输入 `正向提示词`（`positive_text`，STRING）：最终合成的提示词文本。
- 隐藏输入 `prompt` / `extra_pnginfo`：ComfyUI 自动注入，用于把工作流写进 PNG 元数据。
- 输出 `images`（IMAGE）：原样透传，可继续串联其它节点。
- 前端反馈：执行后节点下方显示本次结果 —— `角色名: denia_(wuthering_waves)`（识别成功，此名称即文件前缀；多角色时显示 `角色名: 双人/gotoh_hitori_kita_ikuyo` 这样的完整前缀）或 `未识别到角色名，使用默认命名: ComfyUI`（走了兜底）；图片照常出现在节点预览区与队列历史中。

## 常见问题与排错

**文件名一直是 `ComfyUI_00001_.png`，没识别到角色名。**

- 提示词里只有裸 tag（如 `1girl, solo, long hair`）——自动提取只认 danbooru 的 `角色名 (作品名)` 结构，请改用 `char:角色名`，或把「无作品名角色识别」设为 `数据集精确匹配`；
- 开了「无作品名角色识别」但仍不命中——确认 `data/characters.jsonl` 已下载（跑一次 `download_dataset.py`），以及该角色确实在数据集里（数据集只有 Danbooru 上热度较高的角色）；
- 文本由通配符 / 中间件动态生成，静态扫描拿不到 —— 把最终文本接到「正向提示词」输入口。
- `自动提取角色名` 被关掉了，或 `char:` 标记拼写有误（必须是 `char:` 开头，`<char:xxx>` 也支持）。

**开了模糊匹配后名字识别错了。**

模糊匹配（`cutoff=0.85`）本质是猜，相近的短名字容易互相误判（如 `asuka`、`rei` 这类同名角色多的名字）。对命名准确度要求高时请用 `数据集精确匹配`，或直接写 `char:角色名`。

**双人图为什么进了「双人」文件夹 / 我还在用旧的多角色拼接命名。**

这是「多人自动分组」（默认开启）的效果：2 个角色 → `双人`，3 个及以上 → `多人`，子目录名对角色名排序，所以 tag 顺序变化不会新建文件夹。若要保持旧的多角色拼接命名，把「多人自动分组」关掉即可；想换标签名就改「分组标签」（默认 `双人,多人`）。

**文件名里出现了 `(cosplay)` / `cosplay` 后缀。**

`(cosplay)`、`(alternate_costume)` 等属于元标签，不会再被当成作品名写进 `( )` 里。数据集或「已知角色名名单」能查到该角色时只输出角色名；查不到时才用 `裸名_cosplay` 兜底。都不想要的话，把 `__init__.py` 里的 `_META_SERIES_SUFFIX` 改成 `""`。

**文件名里跑进了画师名。**

- `@画师`、`by 画师`、`by (画师:权重)`、`by_画师`、`drawn by 画师`、`artist: 画师` 都会被过滤，画师串不会再变成文件名。
- 例外：画师名**本身带括号**、又不带任何前缀时（如 `yagi (ningen)`），结构与 `角色名 (作品名)` 完全一致，无法只靠提示词区分 —— 请写成 `@yagi (ningen)` 或 `by yagi (ningen)`。

**文件名里跑进了 `1girl`、`solo` 之类的词。**

说明这段文本不是按逗号分隔的：自动提取的兜底扫描会在整段文本里抓 `单词 (括号)` 结构。把 tag 用逗号分隔，或直接使用 `char:角色名`。

**负向提示词里的内容影响了命名。**

未连接输入口时正、负向文本节点都会被扫描。请把正向文本接到「正向提示词」输入口，或用 `char:` 显式标记。

**中文 / 日文角色名能用吗？**

能，`char:初音ミク` 会保存为 `初音ミク_00001_.png`（文件名非法字符会被替换为 `_`）。自动提取只匹配 danbooru 风格的 `名称 (作品名)`，中文提示词一般识别不到，建议显式标记。

**旧工作流里「保存方式」显示英文值。**

`filename` / `folder` 会被自动映射为对应中文选项，无需改动工作流。

**PNG 里还能看到工作流吗？**

能，`prompt` 与 `workflow` 都会写入 PNG 元数据；以 `--disable-metadata` 启动 ComfyUI 时则不写入。

**同一角色多次生成会覆盖文件吗？**

不会。编号取同名前缀在磁盘上的最大编号 + 1，同一角色连续生成自动接续。

## 已知限制

1. 自动提取只认 danbooru 的 `角色名 (作品名)` 结构，裸 tag 一律不猜（宁可走兜底）；要识别裸名字需开启「无作品名角色识别」并准备好数据集。
2. 「无作品名角色识别」只能回答「这个名字是否是数据集里某个已知角色」，**同名多角色时取热度最高的那个**（`rem` → `rem_(re:zero)`），无法从提示词判断你指的是哪一个；需要精确指定时请写 `角色名 (作品名)` 或 `char:角色名`。
3. 模糊匹配是启发式的，可能误判，且只对长度 ≥ 3 的名字生效。
4. 数据集只覆盖 Danbooru 上热度较高的角色（约 2.2 万条），原创角色 / 冷门角色需要自己填「已知角色名名单」或用 `char:` 标记。
5. 数据集命中的角色名会归一成**裸名**（如 `emilia_(re:zero)` → `emilia`），因此同名不同作品的冷门角色可能落到同一目录；需要区分时请写 `char:角色名 (作品名)` 显式指定。
6. 元标签表是固定清单（cosplay 系列）；其他「其实不是作品名」的括号后缀需要自行加进 `_META_SERIES`。
7. 不带 `@` / `by` 前缀、且名称含括号的画师 tag 与角色 tag 无法区分，请给画师加前缀。
8. 不支持自定义文件名前缀、子目录或日期变量（不要用本节点做通用命名模板）。
9. 连接「正向提示词」输入口是最稳的用法；依赖静态扫描时，负向提示词也可能贡献候选名。

## 测试

角色名提取的单元测试（会 stub 掉 `folder_paths`，**不需要启动 ComfyUI**）：

```bash
python tests/test_extract.py      # 原有逻辑：角色名 (作品名) / 画师过滤 / 命名
python tests/test_bare_name.py    # 数据集裸名字识别
python tests/test_multi_group.py  # 多人自动分组 + cosplay 元标签
```

Windows 下若系统 Python 缺少依赖，可用 ComfyUI 自带的解释器：

```bat
F:\ComfyUI\venv\Scripts\python.exe tests\test_extract.py
F:\ComfyUI\venv\Scripts\python.exe tests\test_bare_name.py
F:\ComfyUI\venv\Scripts\python.exe tests\test_multi_group.py
```

`test_bare_name.py` / `test_multi_group.py` 用内置的小型数据集夹具验证行为，另外对真实的 `data/characters.jsonl` 做集成检查（**文件不存在或角色不在数据集里时自动跳过**，不会导致失败）。

还有一个极简自测入口 `_self_test()`（纯函数、不写磁盘），跑法：

```bat
F:\ComfyUI\venv\Scripts\python.exe -c "import sys; sys.path.insert(0, r'F:\ComfyUI\custom_nodes\ComfyUI-CharNameSave'); import __init__ as m; raise SystemExit(m._run_self_test())"
```

覆盖内容包括：`by (画师:权重)` 等各种画师串被过滤、`角色名 (作品名)` 自动提取、转义括号与权重写法、`char:` 优先级、兜底命名、端到端产出的文件名；数据集精确/模糊匹配、短名归一、同名取热度最高、数据集缺失时的回退、手动名单；多角色分组的顺序无关与去重、`enable_multi_group=False` 回退、自定义分组标签、cosplay 元标签判定与落盘路径。

## 更新记录

- **2026-09-14（二期）**：修复多角色命名问题——新增「多人自动分组」（2 人 → `双人`、3 人及以上 → `多人`，子目录名对角色名做确定性排序，tag 顺序变化不再产生新目录；可用「分组标签」自定义、可关闭回退旧拼接命名）；新增 cosplay 等元标签处理（`(cosplay)`、`(alternate_costume)` 等不再被当成作品名，查不到角色时用 `裸名_cosplay` 兜底）；数据集命中的角色名统一归一为**裸名**（`emilia_(re:zero)` → `emilia`），避免 `kita_ikuyo_(bocchi_the_rock!)_gotoh_hitori_(...)` 这类超长组合；新增 `tests/test_multi_group.py` 与 `_self_test()`。
- **2026-09-14**：新增「无作品名角色识别」——内嵌 Danbooru 角色数据集（`data/characters.jsonl`，来源 [`Sn0w123/booru-characters`](https://huggingface.co/datasets/Sn0w123/booru-characters)），可识别 `Emilia`、`Rem` 这类没有作品名的裸名字 tag，支持「数据集精确匹配 / 数据集模糊匹配」两种模式，另有可选的「已知角色名名单」。同时支持 danbooru 下划线写法 `hakurei_reimu_(touhou)`；数据集缺失时自动回退原有逻辑，`关闭` 模式输出与旧版完全一致。新增 `download_dataset.py`（下载/转换/增量更新）与 `tests/test_bare_name.py`。
- **2026-09-13**：修复画师串 tag 被误判为角色名的问题（典型现象：`by (ningen mame:0.5)` 产出 `by_(ningen_mame_0.5)_00001_.png`）。自动提取改为逐 tag 判定，过滤 `@画师` / `by 画师` / `by (画师:权重)` / `drawn by 画师` / `artist: 画师`，并补齐权重与转义括号处理；新增 `tests/test_extract.py`。
- **2026-08-31**：首个版本。
