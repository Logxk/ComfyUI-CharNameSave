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

**可选（默认已随仓库提供）**：识别没有作品名的裸名字（`Emilia`、`Rem`、`frieren` 这类）需要角色数据集。本仓库已在 `data/characters.jsonl` 内置一份快照（2026-09-18），**开箱即用，无需任何额外操作**；只有想更新到上游最新数据时才需要下载一次。

想更新的话，先进入本插件目录（不同安装方式的路径不同，按自己的来），然后执行下载脚本：

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

### 随仓库分发的数据集快照

为了让使用者**下载插件后开箱可用**，本仓库直接包含了一份数据集快照：

| 项目 | 值 |
| --- | --- |
| 快照生成日期 | **2026-09-18** |
| 文件 | `data/characters.jsonl`（UTF-8、每行一个 JSON，无 BOM） |
| 体积 / 条数 | 2,101,144 字节 / 22,474 条 |
| 上游来源 | [Sn0w123/booru-characters](https://huggingface.co/datasets/Sn0w123/booru-characters) |
| 上游 revision | `9bd43cd9436e9fcb3e57f5cef6c4c813bc347655` |
| 精简后的字段 | `name` / `copyright` / `post_count` / `id` |

> 「快照生成日期」是这份文件在本仓库内由下载脚本转换产出的日期，**不是**上游数据集的
> 提交日期（上游提交日期需到数据集页面查看）。数据本身是 Danbooru 角色 tag 的热度统计，
> 因此快照越新、识别新角色的能力越好。
>
> 数据面向的角色统计口径与 Danbooru 一致，**版权归上游数据集与 Danbooru 所有**；本仓库
> 只做字段精简（见 `download_dataset.py`），请按上游数据集的许可条款使用。
>
> 想更新到上游最新版本，直接重跑 `python download_dataset.py` 覆盖即可（脚本会做增量
> 判断，远端没变就跳过下载）。

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
   - **方括号包裹会被剥掉**：`[[artist:siu_(siu0207)]]` / `[artist:mana_(remana)]`
     与不加方括号等价，画师标记照常被过滤；`[[char:hakurei_reimu]]` 同样可用。
     只剥**成对**的外层方括号，`[a` 这种不成对的保持原样。
   - **带皮肤的写法同样支持**：`角色名 (皮肤名) (作品名)`，例如
     `saori (dress) (blue archive)` → `saori_(dress)_(blue_archive)`；
     也可以只写 `saori (dress)`。转义写法（`saori \(dress\) \(blue archive\)`）与
     空格写法等价。
   - 皮肤/作品两层括号会**从右往左逐层比对数据集**，因此不会把皮肤名误当成作品名，
     也不会在比对失败时回退成裸名（`saori (bikini)` 这种不存在的皮肤不会被
     硬套成基础角色 `saori`）。
3. **无作品名角色识别**（需数据集）：用数据集检索 `Emilia`、`Rem` 这类裸名字。
   - `数据集精确匹配`：忽略大小写与下划线/空格差异。例如 `rem` 会命中数据集里的 `rem_(re:zero)`，但**文件名用短名 `rem`**（去掉括号后缀的写法）；同一角色在数据集里有多个版本时取 `post_count`（热度）最高的那条。
   - `数据集模糊匹配`：在精确匹配之后允许近似匹配（`difflib` 相似度，先用首字母与长度差把候选缩到极小范围再比较），带停用词、最低长度 4、长度差 ≤ 1、首字母相同、`cutoff 0.92` 等约束，避免 `stage` 被误判成角色 `sage`。
4. **以上都没有**：使用「兜底名称」。

> **同一提示词永远得到同一个名字**：相似度并列时按「相似度 → 名字长度 → 字典序」
> 确定性择优，不依赖集合遍历顺序，因此不受 Python 哈希随机化影响，重启 ComfyUI
> 或换一台机器都不会把同一张图存进不同目录。

#### 显式写法优先（重要）

**只要提示词里出现任何显式角色写法，就不再执行裸名判定。** 显式写法指：

```
char:hakurei_reimu          char: 标记
hikari (blue archive)       name (series)
hikari \(blue_archive\)     转义括号写法
```

所以下面这条提示词只会得到 `hikari_(blue_archive)`：

```
hikari (blue archive), blue archive, 1girl, black hat, halo, black jacket
```

`black hat` 之类的服装 tag 不会被拿去数据集里找同名角色——数据集里确实存在
`black_hat_(villainous)`，旧行为会把单人提示词凑成 `Duo`。

> **代价（有意设计）**：混写时裸名一律不认。例如 `char:hikari, rem` 只会得到
> `hikari`，`rem` 需要写成 `char:rem` 或 `rem (re:zero)`。整条提示词都不带显式
> 写法时（如 `emilia, rem, frieren`），裸名识别照常工作。

#### 什么会被拒绝，以及为什么

角色识别按「证据强弱」分层，弱证据必须自己证明自己：

| 情况 | 行为 | 例子 |
| --- | --- | --- |
| 显式写法 | **永远识别**，不受任何通用词过滤影响 | `char:bow`、`bow (some series)` |
| 通用词兼角色名 | **拒绝裸名**（要写全名或用 `char:`） | `bow`、`professor`、`ribbon`、`shirt`、`elf`、`black hat` |
| 歧义裸名 | **拒绝**，避免张冠李戴 | `miku`（数据集里叫 `miku` 的是《Darling in the Franxx》的角色，春未来是 `hatsune_miku`） |
| 压倒性唯一 | 识别 | `rem`（`rem_(re:zero)` 10255 热度 vs 次高 160）、`remilia` → `remilia_scarlet`（62115） |

判断「通用词」有两种入口，都会拒绝且都不影响显式写法：

1. **整词命中通用词表**：`bow`、`professor`、`ribbon` …
2. **整名由通用词拼成**：`black_hat_(villainous)` 的裸键 `black hat` 里
   `black` 与 `hat` 都是通用词，因此这个裸键不参与识别
   （实测该键会让提示词里的 `black hat` 变成角色、单人落进 `Duo`）。
   只要有一个词不是通用词就不受影响：`bow professor niyaniya` 正常识别。

原因是数据集里存在大量「普通英文词恰好也是某个角色名」的条目：
`bow_(paper_mario)`、`professor_(ragnarok_online)`、`ribbon_(kirby)`、`elf_(dragon's_crown)`、
`black_hat_(villainous)`… 若照单全收，`professor_niyaniya (blue archive), bow` 这种单人提示词
会被凑成双人并落进 `Duo` 目录（实测会产出 `Duo_bow_professor_niyaniya_(blue_archive)`）
——**弱证据不能覆盖强证据**。

拒绝时的处理：该 tag 退回普通 tag 语义，不再参与角色命名；若整条提示词没有别的角色，
就走「兜底名称」。想强制指定这些名字，用显式写法即可：

```
char:bow                     -> bow
bow (paper mario)            -> bow_(paper_mario)
char:black_hat_(villainous)  -> black_hat_(villainous)
```

模糊匹配在「只写了名字开头」时会被拒绝（要求打字完整度 ≥ 55%）：
`kita ikuy` → `kita_ikuyo` 可识别；`denia` 不会被猜成
`denia (wuthering waves)`，只会用你写的 `denia`。

### 用户自定义 override（可选）

如果自动判定与你的习惯不符，可以在**插件目录**下放一个 `charnamesave_overrides.json`
（本地配置，不需要提交到仓库）：

```json
{
  "always": ["miku", "remilia"],
  "never": ["bow", "black hat"]
}
```

| 键 | 作用 |
| --- | --- |
| `always` | 强制按角色处理。用来救回自动判定因**歧义**或**通用词**而拒绝的裸名，例如 `miku` |
| `never` | 该裸名永不识别，直接退回普通 tag 语义 |

优先级（与方案一致）：**`char:` > `always` > 自动判定 > `never`**。

因此：

```
never: ["bow"]        char:bow 仍然识别（char: 是最高优先级，不受 never 影响）
always: ["miku"]      裸写 miku 会被识别
```

> `always` 只影响「是否采纳这个裸名」，不会凭空造出不存在的角色——它仍然要能在
> 数据集里查到对应记录。文件不存在 / JSON 写坏 / 字段不是字符串列表都会安全忽略
> （只打 warning），不会影响插件运行。改完文件立即生效，无需重启。


`(cosplay)`、`(alternate_costume)`、`(clothing_swap)`、`(crossdressing)` 等元标签不会被当成作品名；数据集查不到该角色时会退化为 `角色名_cosplay`。

### 诊断输出（可选，默认关闭）

遇到误识别时可以打开诊断输出，插件会为**每一个角色候选**打一条 `DEBUG` 级日志，
直接看出是「压根没生成候选」「候选被作品名一致性护栏丢掉」还是「评分没过门槛」：

```bat
set CHARNAMESAVE_DEBUG=1
```

也可以写进系统环境变量或 ComfyUI 启动脚本；`1` / `true` / `yes` / `on` 都算打开
（大小写、首尾空格无所谓）。该变量在**每次识别时现读**，所以改完不用重启 ComfyUI。

输出形如：

```text
[CharNameSave] Candidate:
  tag=professor_niyaniya (blue archive)
  source=explicit_series
  dataset_name=professor_niyaniya_(blue_archive)
  post_count=3439
  copyright=blue_archive
  score=130
  decision=ACCEPT
```

`decision` 只有 `ACCEPT` / `REJECT` 两种取值，`score` 是最终得分：

- `source=explicit_*` 却 `decision=REJECT`：候选被作品名一致性护栏丢弃（例如
  `denia (cosplay)` 里的 `cosplay`，属于候选筛选阶段，与评分无关）；
- 其余 `REJECT`：评分没过接受门槛。

> 没有 `generic` 字段：通用词是在**候选生成**阶段就被拒绝的，根本不会成为候选，
> 因此不会出现在候选日志里。若日志里连一条候选都没有，说明问题出在生成阶段
> （通用词表或显式写法抑制），而不是评分阶段。

日志走 Python 的 `logging`（`DEBUG` 级），**默认完全静默**：不设这个环境变量时，
普通运行既不会多出任何输出，也不会因此变慢。注意 ComfyUI 控制台默认只显示 `INFO`
及以上，要看这些行得用 `--verbose` 启动，或自行把 `charnamesave_core.matching`
的日志级别调到 `DEBUG`。

> 说明：`char:xxx` 走的是更早的短路路径（只认显式标记、不产生候选），因此不会出现在
> 候选日志里；候选日志覆盖的是 `name (series)` 自动提取与无作品名（裸名）识别。

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
├── download_dataset.py       # 数据集下载/更新脚本（可选，仅更新数据时用）
└── data/characters.jsonl     # 角色数据集快照（随仓库分发，开箱即用）
```

> 兼容性：`__init__.py` 会重新导出全部历史内部名（`_char_names`、`_build_save_prefix`、
> `_dataset_lookup` …）。ComfyUI 以包方式加载时使用相对导入，直接 `import` 时使用绝对导入，
> 二者均已验证通过。

## 许可证

[MIT](LICENSE)
