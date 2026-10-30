# 学科 × 解析链路总表（PIPELINES）

> 本仓库是**多学科**试卷解析的统一项目。同一个学科往往有**多条互不替代的解析逻辑**
> （文本链路 / PDF 页图链路 / 图片版链路），它们共用同一套 AI 网关与数据根，但入口脚本、
> 输入、输出目录各不相同。本文件就是这张“哪里进、哪里出”的地图。
>
> - 数据根与路径约定见 [第 0 节](#0-共用底座先读这里)
> - 每学科的具体命令见对应章节；README 只保留英语主流程与通用参数
> - **质量脚手架（6 科通用的 `--check / --cross-check / --ai-audit / --repair`）见
>   [第 10 节](#10-质量脚手架跨学科qascaffoldpy)**
> - 最近一次校准：2026-10-30（本机 gpu-server）

## 0. 共用底座（先读这里）

### 0.1 AI 网关与模型

所有链路都走同一套 OpenAI 兼容接口，配置在仓库根 `.env`：

```bash
API_TYPE=openai
OPENAI_BASE_URL=http://localhost:8080/v1     # new-api 网关 → 本机 vLLM
OPENAI_MODEL=deepseek-v4.1-flash
OPENAI_TIMEOUT=600
OPENAI_MAX_TOKENS=64000
OPENAI_REASONING_EFFORT=none                 # 本机模型必须显式关闭思考，否则 content 为空
```

- 备选公网入口 `https://ai.gxwtf.cn/v1`（同一网关）、官方付费 API，均在 `.env.example` 里注释给出。
- 多模态链路（数学 / 物理 / 化学 PDF 版 / 图片版）默认复用 `OPENAI_MODEL`，也可用
  `MATH_VISION_MODEL` / `MATH_VISION_BASE_URL` / `MATH_VISION_API_KEY` 单独覆盖。

### 0.2 数据根（EXAM_DATA_ROOT）

所有 `gaozhong/`、`output_*` 目录都在**数据根**下，由 `paths.py` 统一定义：

```python
from paths import DATA_ROOT, GAOZHONG, OUTPUT_JSON_MATH, OUTPUT_DOCX_TO_MD
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EXAM_DATA_ROOT` | 仓库的**上级目录** | 本机即 `/home/gxwtf/jingshibang`；换机器/换目录时设它即可 |

默认值刻意等于历史行为（`Path(__file__).parent.parent`），所以本机现有命令一条都不用改。

### 0.3 目录约定

| 目录 | 内容 | 谁写 |
| --- | --- | --- |
| `gaozhong/<类型>/<学科>/` | 原始卷（doc/docx/pdf，常见“教师版”） | 人工/爬虫 |
| `output_docx_to_md/<类型>/<学科>/<卷>/` | docx→MD，`images/` 放导出的图片 | `batch_convert_docx_to_md.py` |
| `output_pdf_to_md/` | MinerU PDF→MD（历史存量） | `batch_convert_pdf_to_md.py` |
| `output_json/` | 英语 JSON | `main.py` / `batch_convert_md_to_json.py` |
| `output_json_math/<类型>/<学科>/` | 数学 / 物理 / 化学（选择题 JSON，同一套导入结构） | `convert_math_pdf.py` 等 |
| `output_json_chinese/` | 语文（按类型平铺） | `convert_chinese_md.py` |
| `output_json_politics/` | 政治（按类型平铺） | `convert_politics_md.py` |
| `output_json_chemistry_full/` | 化学整卷（选择+非选择） | `convert_chemistry_full.py` |
| `output_json_bio/` | 生物整卷（选择题 + 一题一 section 的非选择题） | `convert_biology_md.py` |
| `output_image_questions/<类型>/<学科>/<卷>/` | 图片版：`paper.json` + `qN.png` + `qN.json` | `batch_image_papers.py` 等 |
| `qa/` | **质量脚手架产物**：`<学科>.md`、`README.md`、`<学科>.json`、`.cache/` | `qa_scaffold.py` |
| `_backup_qa_repair_<时间戳>/` | `qa_scaffold.py --repair --apply` 写盘前的原文件备份 | `qa_scaffold.py` |

图片对外由 nginx 直出：`https://gxwtf.cn/exam-images/<类型>/<学科>/<卷>/images/<文件名>`
（alias 到 `output_docx_to_md/<类型>/<学科>/<卷>/images/`）。

### 0.4 题库导入（下游）

解析结果最终由 `~/gxexercise` 的脚本入库（不在本仓库）：

```bash
npx tsx scripts/import-math-json.ts   --file <output_json_math/.../*.json> --dry-run
npx tsx scripts/import-chinese-json.ts --file <output_json_chinese/.../*.json> --dry-run
```

## 1. 总览：学科 × 链路

| 学科 | 链路 | 入口脚本 | 输入 | 输出 | 状态（本机存量） |
| --- | --- | --- | --- | --- | --- |
| 英语 | ① MD→JSON（单目录/批量） | `main.py`、`batch_convert_md_to_json.py` | `output_docx_to_md/**/*.md` | `output_json/<类型>/英语/` | 已跑通（全量 445 卷） |
| 数学 | ② PDF 页图→JSON（多模态） | `convert_math_pdf.py` | `gaozhong/<类型>/数学/*.pdf` | `output_json_math/<类型>/数学/` | 已跑通（163 卷） |
| 物理 | ③ PDF 页图→JSON（只选择题） | `convert_physics_pdf.py` | `gaozhong/<类型>/物理/*.pdf` | `output_json_math/<类型>/物理/` | 已跑通（112 卷） |
| 化学 | ④ docx→MD→JSON（选择题，确定性切题） | `convert_chemistry_md.py` | `output_docx_to_md/<类型>/化学/<卷>/` | `output_json_math/<类型>/化学/` | 已跑通（107 卷） |
| 化学 | ⑤ 整卷（选择+非选择） | `convert_chemistry_full.py` | docx→MD 同上 | `output_json_chemistry_full/` | 试跑（1 卷） |
| 化学 | ⑥ PDF 页图→JSON（选择题，与物理同构） | `convert_chemistry_pdf.py` | `gaozhong/<类型>/化学/*.pdf` | `output_json_math/<类型>/化学/` | 可用的备选链路 |
| 化学 | ⑦ **图片版**（按题裁图 + 多模态解析） | `batch_image_papers.py` → `add_image_choice_analysis.py` | PDF + docx→MD | `output_image_questions/<类型>/化学/` | **当前主线**（二模 108 卷 / 1555 题；**只做选择题**，非选择题未做） |
| 语文 | ⑧ docx→MD→JSON | `convert_chinese_md.py` | `output_docx_to_md/<类型>/语文/<卷>/` | `output_json_chinese/` | 已跑通（二模 96 卷） |
| 政治 | ⑨ docx→MD→JSON | `convert_politics_md.py` | `output_docx_to_md/<类型>/政治/<卷>/` | `output_json_politics/` | 试跑（二模 1 卷） |
| 生物 | ⑩ docx→MD→整卷 JSON（选择 + 一题一 section 的非选择） | `convert_biology_md.py` | `output_docx_to_md/<类型>/生物/<卷>（教师版）/` | `output_json_bio/<类型>/` | 已跑通（123 卷：二模 28 / 一模 42 / 上期末 38 / 真题 15） |
| 历史 / 地理 | — | — | — | — | **仍无解析链路**（素材也没进 `gaozhong/`） |

> 生物链路自带一套学科专用脚手架：`convert_biology_md.py --check / --cross-check / --reclassify`
> （分值长句优先、表格可解析、答案图存在、选项答案与答案表/解析原文比对）。生物**不在**
> 第 10 节跨学科脚手架的范围内（口径不同：它按「选择 15 题 × 2 分 + 非选择题一题一 section」校验）。

同一学科为什么要多条链路：Word 里公式/装置图常是图片（`.wmf`/`.emf`），PDF 里浮动图会压住文字，
MD 里则可能丢公式——所以同一份卷会出现“文字完整但公式缺”“公式完整但选项被盖住”的不同坏法，
需要用不同入口取长补短，再用后处理脚本交叉回填。

---

## 2. 英语（链路 ①）

```bash
# ① doc/docx → MD（本仓库内，权威副本）
python3 batch_convert_docx_to_md.py -T 二模 -s 英语 -w 4

# ② MD → JSON（批量，保持 类型/学科/卷 层级）
python3 batch_convert_md_to_json.py -T 二模 -s 英语
python3 batch_convert_md_to_json.py -p ./path/to/paper.md          # 单份

# ③ 单目录模式（data/input/*.md → data/output/）
python3 main.py -i ./data/input -o ./data/output --force

# ④ 校验（不调 AI）
python3 main.py --validate
python3 validate_json.py <json 路径>
```

- 题型：完形填空 / 语法填空 / 阅读 / 七选五 / 选词填空 / 阅读表达 / 作文（见 `config.py`）。
- 阅读类用 `start` 锚点从 MD 抽原文；`backfill_re_stems.py` 可离线重算「阅读表达」题干。
- **答案大小写约定**：**所有学科**的选择题 `answer` 与选项 `options[].id` 一律大写字母（`"C"` / `{"id": "C"}`）。
  英语由 `src/json_builder.py` 按 `questionType` 处理；数学 / 物理 / 语文 / 政治 / 化学 / 图片版 / 英语批量
  都在写盘出口调 [`src/answer_case.py`](src/answer_case.py) 的 `normalize_choice_case_in_paper()`（一行，别改各脚本
  内部的解析/合并逻辑）。语法填空 / 选词填空 / 阅读表达 / 作文的单词答案保留原文大小写（`faced`、`FACE` 不改写）。
  存量 JSON 迁移：`python3 normalize_json_case.py`（默认 dry-run；`--apply` 先备份到 `_backup_json_case_<时间戳>/`）。

## 3. 数学（链路 ②）

```bash
python3 convert_math_pdf.py -p ../gaozhong/二模/数学/xxx.pdf --pages 2   # 调试单卷前 2 页
python3 convert_math_pdf.py -p ../gaozhong/二模/数学/xxx.pdf            # 单卷
python3 convert_math_pdf.py -T 二模 -w 4                               # 批量
python3 fix_math_missing_options.py                                    # 定向补缺失选项（不重跑整卷）
```

- 页面预筛（`select_pages`）只把「有题」的页送模型，实测 66s/份 → 9s/份。
- 输出旁挂 `<卷>.review.txt` 记录需人工复核点。

## 4. 物理（链路 ③）

```bash
python3 convert_physics_pdf.py -T 二模 -w 8           # PDF 页图 → 选择题 JSON
python3 repair_physics_wmf.py -T 二模                 # 含 .wmf 的题让模型重转录公式
python3 postprocess_physics.py -T 二模                # 图片/占位符收尾
python3 attach_physics_images.py -T 二模              # 从 docx→MD 挂插图
python3 attach_physics_tables.py -T 二模              # 从 MD 挂回表格
python3 fill_physics_options_from_md.py -T 二模       # MD 文本兜底补跨页选项
python3 fill_physics_option_images.py -T 二模         # 选项图补全
python3 apply_manual_tables.py -T 二模                # 手工表格（manual_tables/*.md）
python3 fix_images.py --type 二模 --subject 物理      # .tiff/.wmf/.emf → .png（nginx 只放行 web 图）
python3 keep_choice_only.py --type 二模 --subject 物理 --apply   # 只留选择题（新单科 1-14 / 理综 13-20 / 旧单科 1-8），分值统一 3 分
python3 fill_answers_from_pdf_text.py --type 二模 --subject 物理 # 用教师版 PDF 文字层补空答案（双跑一致才采纳）
```

- 只处理**选择题**；非选择题尚未做。
- docx 会把实验题的小问也拆成“选择题”混进 JSON（如 `2024海淀 q16` 其实是实验题 16 的（4）），
  导入题库前务必跑一次 `keep_choice_only.py`。
- `.wmf` 公式图：`wmf2gd`（免 root 解包 libwmf，见 `tools_wmf2png.sh`、`repair_physics_wmf.py`）。

## 5. 化学（链路 ④⑤⑥⑦，四条逻辑）

### 5.1 ④ 选择题文本链路（当前题库主数据）

```bash
# 前置：docx → MD，先导出图片
python3 batch_convert_docx_to_md.py -T 二模 -s 化学 -w 4

# Word 里的 .emf 公式/装置图 → PNG（ImageMagick 不支持 EMF，走 LibreOffice）
python3 convert_emf_to_png.py ../output_docx_to_md/二模/化学

# MD → JSON（确定性切题 + AI 只做 LaTeX 排版转换 + 保真校验）
python3 convert_chemistry_md.py -p 2025北京西城高三二模化学
python3 convert_chemistry_md.py --type 二模 -w 4 --force

# 收尾
python3 fix_images.py --dry-run                       # .tiff/.emf/.wmf → .png
python3 sanitize_mdx.py --dry-run                     # MDX/KaTeX 安全化
python3 check_katex.py                                # 用 KaTeX 渲染校验公式
python3 add_chem_choice_analysis.py -T 二模           # 给选择题补文字解析
```

### 5.2 ⑤ 整卷（选择 + 非选择）

```bash
python3 convert_chemistry_full.py -p ../output_docx_to_md/二模/化学/<卷>/<卷>.md
python3 convert_chemistry_full.py --dir ../output_docx_to_md -T 二模
```

非选择题按 `(1)(2)…` 拆小问、材料复制到每题，并打题型标签：
物质结构与性质 / 化学反应原理 / 有机化学基础 / 工业流程 / 实验探究。

### 5.3 ⑥ PDF 页图链路（与物理同构，备选）

```bash
python3 convert_chemistry_pdf.py -T 二模 -w 4
```

### 5.4 ⑦ 图片版链路（当前主线，保原版式）

```bash
# 单卷
python3 build_image_paper.py -d ../gaozhong/二模/化学/<卷>.docx -T 二模
# 批量：裁图 → paper.json（选择题走重排后裁图，非选择题走原 PDF 裁图）
python3 batch_image_papers.py -T 二模 -s 化学 [--limit N] [--only 关键词]
# 给每道选择题的截图补 AI 解析
python3 add_image_choice_analysis.py -T 二模 --workers 4
```

- 裁题：`pdftoppm` 渲染 + `pdftotext -bbox` 定位题号坐标（`crop_questions_pdf.py`），跨页自动拼接、白块盖题号。
- 选择题先经 `reflow_docx.py` 统一行距 / 浮动图内联，再转 PDF 裁图，避免图压字。
- 产物：`output_image_questions/<类型>/<学科>/<卷>/{paper.json,qN.png,qN.json}`；
  图片同写一份到 `output_docx_to_md/.../images/` 供 nginx 直出。
- 核对表与预览 HTML 是人工检查用的副产物（历史放在 `~/化学二模样例/`）。

## 6. 语文（链路 ⑧）

```bash
python3 batch_convert_docx_to_md.py -T 二模 -s 语文 -w 4
python3 convert_chinese_md.py -p ../output_docx_to_md/二模/语文/<卷>/<卷>.md -T 二模
python3 convert_chinese_md.py --dir ../output_docx_to_md -T 二模 -w 3
# 收尾
python3 merge_chinese_groups.py          # 合并题组
python3 normalize_chinese_scores.py      # 分值归一（满分 150）
python3 repair_chinese_tables.py --dry-run   # 表格用页面图像重识别
python3 sanitize_chinese.py              # MDX/KaTeX 安全化
```

- 名著阅读：单问卷为一题；多小问按 `(1)(2)` 拆题，材料进题组 `content`。
- `docx_dots.py`：从 docx 读 ①②③ 填空标记，补 MD 里丢掉的空位。

## 7. 政治（链路 ⑨）

```bash
python3 batch_convert_docx_to_md.py -T 二模 -s 政治 -w 4
python3 convert_politics_md.py -p ../output_docx_to_md/二模/政治/<卷>/<卷>.md -T 二模
```

按模块打标签：中特 / 经济社会 / 政治法治 / 哲学文化 / 国政国经 / 法律生活 / 逻辑思维。

## 8. 尚未覆盖的学科

**历史、地理**目前没有解析链路，素材也没有进 `gaozhong/`。若要新增，建议直接复用：

- 文字型（以文字为主、图少）→ 抄 `convert_politics_md.py`（docx→MD→JSON）
- 图文混合 / 公式多 → 抄 `convert_physics_pdf.py`（PDF 页图多模态）或图片版链路
- 整卷（选择 + 非选择、一题一 section）→ 抄 `convert_biology_md.py`（链路 ⑩，自带学科脚手架）

并同步补：`paths.py` 的输出常量、本文件的矩阵、README 的学科导航、nginx 图片白名单（如需要），
以及 [`qa_scaffold.py`](qa_scaffold.py) 里 `Layout` 的路径映射（见第 10 节）。

## 9. 新增一条链路的检查清单

1. 入口脚本落仓库根，路径一律从 `paths.py` 取，不写 `Path(__file__).parent.parent`；
2. 输出目录按 `<数据根>/output_<用途>` 命名，并在 `paths.py` 里加常量；
3. 命令写进本文件的矩阵和对应章节；
4. `python3 -m py_compile *.py` 通过；
5. 先 `--dry-run` / `--limit 1` 跑一份，再批量。

---

## 10. 质量脚手架（跨学科，`qa_scaffold.py`）

生物以外的 6 科（英语 / 语文 / 数学 / 物理 / 化学 / 政治）共用一套「**AI 检查 + 兜底修复**」脚手架，
思路对齐生物链路已有的 `--check / --cross-check`，但**不复制生物专用逻辑**：先把三种产物结构
归一成同一个模型再统一检查/修复。

```
Paper（一卷） → units（题组：groups | sections | 图片版 paper.json） → questions
```

| 产物结构 | 学科 / 链路 | 适配方式 |
| --- | --- | --- |
| `paper` + `sections` | 英语（`output_json/<类型>/英语/`）、数学 / 物理 / 化学（`output_json_math/<类型>/<学科>/`） | `sections` 即题组 |
| `paper` + `groups` | 语文（`output_json_chinese/<类型>/`）、政治（`output_json_politics/<类型>/`） | `groups` 即题组 |
| `paper.json` + `qN.json` | 化学图片版（`output_image_questions/<类型>/化学/<卷>/`） | 合成一个「图片版」题组；`--image` |

> ⚠ 现状与旧文档的说法不同：**英语产物实际是 `paper` + `sections`**（`question` 有
> `content/options/answer/score/analysis`，无 `module`），本文档据此校准；语文 `groups` 的
> `question` 多一个 `module` 字段（适配器原样保留，不参与检查）。

### 10.1 命令

```bash
# 确定性检查（不调模型，可全量跑；也是不给动作时的默认行为）
python3 qa_scaffold.py --subject 英语 --type 二模 --check
python3 qa_scaffold.py --subject 数学 --type all --check --report /path/to/数学.md
python3 qa_scaffold.py --subject 化学 --type 二模 --check --image      # 只查图片版

# AI 抽查（抽样控制成本）
python3 qa_scaffold.py --subject 英语 --type 二模 --cross-check 10          # 每卷最多 10 题
python3 qa_scaffold.py --subject 语文 --type 二模 --ai-audit 10             # 每卷最多 10 题

# 兜底修复（默认 dry-run，--apply 才写盘）
python3 qa_scaffold.py --subject 物理 --type 二模 --repair
python3 qa_scaffold.py --subject 物理 --type 二模 --repair --apply

# 其它开关
--type 二模,一模 | --type all          # 类型（默认 二模；all = 该学科所有类型）
--image                               # 化学图片版（仅确定性检查；AI 抽查/修复不适用）
--no-ai                               # 只吃 AI 缓存（复跑逐字节一致）
--force                               # 忽略缓存重跑
--workers N                           # AI 并发（默认 4）
--papers 海淀 西城 | --limit 20        # 只跑部分卷 / 调试
--ai-papers 20 --audit-papers 10       # AI 抽样卷数（默认 20 / 10）
--report <文件或目录>                  # 报告路径（默认 <数据根>/qa/<学科>.md）
```

**产物**（都在数据根下，不随仓库走）：

| 路径 | 内容 |
| --- | --- |
| `qa/<学科>.md` | 每科明细：覆盖面、问题计数、逐卷清单、AI 抽查、修复明细、需人工介入 |
| `qa/README.md` | 各科汇总表（自动生成，勿手改） |
| `qa/<学科>.json` | 分节累积的机器可读状态（`check` / `cross_check` / `ai_audit` / `repair` / `repair_history`） |
| `qa/.cache/<学科>/<kind>-<prompt-sha>.json` | AI 缓存（`--no-ai` 复跑靠它，逐字节一致） |
| `<卷>.review.txt` 旁挂 | 每处修复的留痕行（前缀 `qa修复：`，重复跑幂等） |
| `_backup_qa_repair_<时间戳>/` | `--apply` 前的原文件备份（按 `<数据根>/…` 相对路径存放） |

分节累积的意思是：`--check`、`--cross-check`、`--ai-audit`、`--repair` **可以分别跑**，
各自只更新自己那一节，最后合成同一份 `<学科>.md`。化学图片版单独记一份
`化学（图片版）.md`（同一学科两条链路不互相覆盖）。

### 10.2 确定性检查（8 项，全部不调模型）

| # | 代码 | 检查内容 |
| --- | --- | --- |
| 1 | `E_EMPTY_CHOICE` / `W_EMPTY_FILL` / `I_EMPTY_OPEN` | 空答案：选择题记错误、填空题记警告、开放题（作文/微写作/阅读表达/长篇简答）只记提示且**不参与 AI 兜底** |
| 2 | `E_ANSWER_NOT_IN_OPTIONS` | 选择题答案不在选项内（比对前先做全角→半角、去空白、大写） |
| 3 | `E_DUP_OPTION_ID` / `E_FEW_OPTIONS` / `W_EMPTY_OPTION_LABEL` | 选项 id 重复、选项数 < 2、选项 label 为空 |
| 4 | `E_SCORE_LE0` / `W_UNIT_SCORE_MISMATCH` / `W_TOTAL_SCORE_MISMATCH`（`I_TOTAL_SCORE_PARTIAL`） | 每题分值 ≤ 0 或缺失；题组分值 ≠ 小题合计；`paper.totalScore` ≠ 各题组合计（数学/物理/化学只收选择/填空，差额记「提示」） |
| 5 | `E_MISSING_IMAGE` / `W_IMAGE_NONWEB_EXT` | 图片引用（`![](/exam-images/…)`、`<QImg src=…>`、相对 `images/…`）逐张解析到磁盘；扩展名 nginx 不放行（`.wmf/.emf/.tiff`）单独记警告 |
| 6 | `W_BLANK_COUNT` | `<Input>/<Input2>/<InputLong>/<Blank>/<ClozeBlank>` 占位符数 ≠ 小题数 |
| 7 | `W_DUP_STEM` | 同卷重复题干（去空白/图片/标签后完全相同）：同题组内必报，跨题组还要求题面原文也相同（否则 "What can we infer…" 会误报） |
| 8 | `W_ANSWER_FORMAT` / `W_ANSWER_MULTI` / `W_ANSWER_SEPARATOR` | 答案大小写/全角/首尾空白/多字母/`/` `、` 分隔符未规范化 |
| + | `W_PAPER_SUBJECT` / `W_ANSWER_IS_STEM` / `W_ANSWER_EMBEDDED` / `W_ANALYSIS_IS_STEM` | 附加断言：`paper.subject` 不符；`answer` 里装的是题干/带 `【答案】【解析】`；`analysis` 与题干或别题题干相同（**整卷错位**的典型指纹，需重跑该卷转换） |
| + | `E_IMAGE_PNG_MISSING` / `W_IMAGE_JSON_MISMATCH` / `I_IMG_TEXT_QUESTION` | 图片版专项：缺 `qN.png`、`qN.json` 与 `paper.json` 不一致、非选择题未做（链路现状） |

图片路径口径（与 nginx 一致）：

- `/exam-images/<类型>/<学科>/<卷>/images/x.png` → `output_docx_to_md/<类型>/<学科>/<卷>/images/x.png`
  （`?v=…` 查询串忽略）；卷目录名会再试 `（教师版）`/`含答案` 等变体。
- 相对 `images/x.png` → 依次试 `output_docx_to_md/<类型>/<学科>/<卷名变体>/images/x.png`、
  JSON 同级目录、`output_json_math/images/`（数学 `images/<卷>_qN.png`）。

### 10.3 AI 抽查口径（两项，都带缓存）

**① `--cross-check N`：模型独立作答**

- 每道题只给 **题干 + 选项，不给答案**（也不给解析）；题干为空时（完形/语法填空/七选五）用
  该题组题面里第 k 个占位符附近的片段作上下文，占位符统一编号为 `【空k】`。
- 抽样：每科默认取 **20 卷 × 每卷最多 N 题**；卷按类型轮转抽取，题按「最大题组连续前 5 题 +
  其余题组轮转」抽取（保证同一题组有 ≥3 题连续样本，平移诊断才有意义）。
- 输出：每卷一致率、总体一致率、**选择题一致率**（非选择题是宽松匹配，仅供参考）、
  **平移诊断**（同一题组 ≥3 题时比 identity 与 ±1/±2 平移的吻合率；某题组平移明显优于
  identity → 疑似答案整体错位）、逐条分歧清单（原卷答案 / 模型答案 / 模型理由）。
- 完全不改 JSON。

**② `--ai-audit N`：题干-选项-答案自洽性**

- 每道题给 **题干 + 选项 + 已录入答案 +（截断的）解析**，让模型判断：是否自洽、是否串题、
  错别字、学科知识错误、答案与解析矛盾。
- 抽样：每科默认 **10 卷 × 每卷最多 N 题**；结论**只报告不改**（一律不落盘）。

**缓存与复跑**

- 缓存键 = `sha1(题目文本 + 固定提示词)`，与批处理顺序无关；`--no-ai` 只吃缓存，
  **同一命令复跑逐字节一致**（报告里的「生成命令」会自动去掉 `--no-ai/--force/--workers`，
  所以命令串不变）。缓存没命中的题在 `--no-ai` 下直接跳过、只在控制台提示。

### 10.4 兜底修复（`--repair`，默认 dry-run）

判定顺序（**先确定性、再解析原文、最后才问模型**，保真度从高到低）：

| 类别 | 触发条件 | 动作 |
| --- | --- | --- |
| 答案归一 `answer_case` | 选择题答案只因大小写/全角/空白与选项 id 不同 | 归一为选项 id，不改语义 |
| 取首字母 `answer_letter_prefix` | 答案不在选项内，但**以选项字母开头**（如 `A(A 项…)`、`B(3分)【解析…`） | 取该字母，余下文字迁到 `analysis`（`原答案字段余下内容（qa 迁移）`），信息不丢 |
| 取自解析 `answer_from_analysis` | 解析原文明确写了 `故选X` / `正确答案为X` 且 X 在选项内 | 用解析原文的字母（教师版依据，比模型硬） |
| 缺图替身 `image_alt` | 引用文件不存在，但同目录有同名 `.png/.jpg/.jpeg/.webp/.gif`，或同卷目录名变体（`（教师版）`/`含答案`）里存在同名图 | 改引用（保留目录与 `?v=` 查询串）；扩展名不放行且有 web 替身时同样改引用 |
| 分值兜底 `score` | 小题分值 0/缺失，且题组 `score` 还有剩余可分 | 按题组剩余分值均分（2 位小数，末位吃掉取整误差）；**题组分值已被某个小问占满时不兜底，只报告** |
| 空答案 `empty_answer` | 选择题答案空，或填空类（`input/input2/grammar/cloze/blank…`）答案空 | 模型**独立作答两次**，两次一致且格式合法才补；`analysis` 追加「AI 补答案（原卷缺失）」 |
| 选项外 `answer_not_in_options` | 前面几条都不适用 | 模型答案在选项内且两次一致才替换；`analysis` 追加「AI 修正答案（原卷答案不在选项内）」 |

**边界（哪些不许动）**

- **绝不改教师已给出的合法答案**：答案本来就在选项内 / 非空且合法 → 一律不动。
- **开放题不兜底**：作文、微写作、阅读表达、长篇简答的答案永不生成。
- **两次独立作答**：每次 `--repair` 对每道待修题各发一次「正向」和一次「换问法（第二遍独立判读）」
  的请求；两次归一后不一致、或答案不在选项内 / 超长 / 带句号，**一律不采纳**（只进「未采纳」清单）。
- **模型答案与解析冲突**时行尾标 `⚠解析里写的字母是 X`，报告单列条数——这些必须人工抽查。
- 每处改动写进该卷 `<卷>.review.txt`（前缀 `qa修复：`），并在报告里列明细；`--apply` 前先整卷备份到
  `_backup_qa_repair_<时间戳>/`。
- 修复**幂等**：同样的数据再跑一次 `--repair --apply` 应为 0 处（AI 结论来自逐题缓存，不受分批影响）。

### 10.5 成本与抽样建议

| 动作 | 调用量（本机实测） | 建议 |
| --- | --- | --- |
| `--check` | 0 次（纯 Python，全量 6 科约 1 秒） | 每次改解析逻辑后全量跑 |
| `--cross-check 10` | 每科 20 次（20 卷 × 1 次，10 题/次）；命中缓存后 0 次 | 每科 20 卷足够看趋势；只想知道「有没有整体错位」时可 `--ai-papers 8` |
| `--ai-audit 10` | 每科 10 次 | 每科 10 卷；新版模型或改了 prompt 才 `--force` |
| `--repair` | **每道待修题 2 次**（正向 + 第二遍），逐题调用（为幂等） | 先 dry-run 看条数；`--apply` 后再跑一次确认 0 处 |

本机模型 `deepseek-v4.1-flash`（关思考）单题调用约 0.5~2s，6 科全套（check + 抽样 + 一次修复）实测
十几分钟；`--no-ai` 复跑基本瞬时。
