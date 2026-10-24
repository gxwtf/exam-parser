# 学科 × 解析链路总表（PIPELINES）

> 本仓库是**多学科**试卷解析的统一项目。同一个学科往往有**多条互不替代的解析逻辑**
> （文本链路 / PDF 页图链路 / 图片版链路），它们共用同一套 AI 网关与数据根，但入口脚本、
> 输入、输出目录各不相同。本文件就是这张“哪里进、哪里出”的地图。
>
> - 数据根与路径约定见 [第 0 节](#0-共用底座先读这里)
> - 每学科的具体命令见对应章节；README 只保留英语主流程与通用参数
> - 最近一次校准：2026-10-24（本机 gpu-server）

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
| `output_image_questions/<类型>/<学科>/<卷>/` | 图片版：`paper.json` + `qN.png` + `qN.json` | `batch_image_papers.py` 等 |

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
| 英语 | ① MD→JSON（单目录/批量） | `main.py`、`batch_convert_md_to_json.py` | `output_docx_to_md/**/*.md` | `output_json/` | 已跑通（二模 87 卷，全量 445 份 JSON） |
| 数学 | ② PDF 页图→JSON（多模态） | `convert_math_pdf.py` | `gaozhong/<类型>/数学/*.pdf` | `output_json_math/<类型>/数学/` | 已跑通（二模 158 卷） |
| 物理 | ③ PDF 页图→JSON（只选择题） | `convert_physics_pdf.py` | `gaozhong/<类型>/物理/*.pdf` | `output_json_math/<类型>/物理/` | 已跑通（二模 112 卷） |
| 化学 | ④ docx→MD→JSON（选择题，确定性切题） | `convert_chemistry_md.py` | `output_docx_to_md/<类型>/化学/<卷>/` | `output_json_math/<类型>/化学/` | 已跑通（二模 107 卷） |
| 化学 | ⑤ 整卷（选择+非选择） | `convert_chemistry_full.py` | docx→MD 同上 | `output_json_chemistry_full/` | 试跑（1 卷） |
| 化学 | ⑥ PDF 页图→JSON（选择题，与物理同构） | `convert_chemistry_pdf.py` | `gaozhong/<类型>/化学/*.pdf` | `output_json_math/<类型>/化学/` | 可用的备选链路 |
| 化学 | ⑦ **图片版**（按题裁图 + 多模态解析） | `batch_image_papers.py` → `add_image_choice_analysis.py` | PDF + docx→MD | `output_image_questions/` | **当前主线**（二模 108 卷 / 1555 题） |
| 语文 | ⑧ docx→MD→JSON | `convert_chinese_md.py` | `output_docx_to_md/<类型>/语文/<卷>/` | `output_json_chinese/` | 已跑通（二模 96 卷） |
| 政治 | ⑨ docx→MD→JSON | `convert_politics_md.py` | `output_docx_to_md/<类型>/政治/<卷>/` | `output_json_politics/` | 试跑（1 卷） |
| 生物 / 历史 / 地理 | — | — | — | — | **暂无解析链路** |

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
```

- 只处理**选择题**；非选择题尚未做。
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

生物、历史、地理目前**没有**解析链路，素材也没有进 `gaozhong/`。若要新增，建议直接复用：

- 文字型（以文字为主、图少）→ 抄 `convert_politics_md.py`（docx→MD→JSON）
- 图文混合 / 公式多 → 抄 `convert_physics_pdf.py`（PDF 页图多模态）或图片版链路

并同步补：`paths.py` 的输出常量、本文件的矩阵、README 的学科导航、nginx 图片白名单（如需要）。

## 9. 新增一条链路的检查清单

1. 入口脚本落仓库根，路径一律从 `paths.py` 取，不写 `Path(__file__).parent.parent`；
2. 输出目录按 `<数据根>/output_<用途>` 命名，并在 `paths.py` 里加常量；
3. 命令写进本文件的矩阵和对应章节；
4. `python3 -m py_compile *.py` 通过；
5. 先 `--dry-run` / `--limit 1` 跑一份，再批量。
