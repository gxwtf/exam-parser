# 广学五题坊 · 试卷解析器（多学科统一项目）

把高中试卷（docx / pdf / 扫描图）解析成结构化 JSON 的统一仓库。

**一个学科可能有多条解析链路**，它们不是替代关系，而是针对不同的“坏法”取长补短：

| 链路类型 | 做法 | 典型学科 |
| --- | --- | --- |
| 文本链路 | docx → MD（w2m）→ 脚本确定性切题 + AI 只做排版转换 | 英语、语文、政治、化学（选择题） |
| PDF 页图链路 | 每页渲染成图 → 多模态模型逐页读成 JSON | 数学、物理、化学（PDF 版） |
| 图片版链路 | 按题裁图 + 多模态逐题写解析，保留原版式 | 化学（当前主线） |

**学科 × 链路总表、每条链路的完整命令与输出目录，见 [PIPELINES.md](PIPELINES.md)。**

| 学科 | 已有链路 | 入口 |
| --- | --- | --- |
| 英语 | ① MD→JSON | `main.py`、`batch_convert_md_to_json.py` |
| 数学 | ② PDF 页图→JSON | `convert_math_pdf.py` |
| 物理 | ③ PDF 页图→JSON（选择题） | `convert_physics_pdf.py` |
| 化学 | ④ MD→JSON　⑤ 整卷　⑥ PDF 页图→JSON　⑦ 图片版 | `convert_chemistry_md.py`、`convert_chemistry_full.py`、`convert_chemistry_pdf.py`、`batch_image_papers.py` |
| 语文 | ⑧ MD→JSON | `convert_chinese_md.py` |
| 政治 | ⑨ MD→JSON | `convert_politics_md.py` |
| 生物 / 历史 / 地理 | 暂无 | — |

## 通用约定

- **数据根**：`gaozhong/`（原始卷）与所有 `output_*`（产物）都在 `EXAM_DATA_ROOT` 下，
  由 [`paths.py`](paths.py) 统一定义；默认 = 仓库的上级目录（本机即 `/home/gxwtf/jingshibang`）。
- **AI 网关**：默认本机 new-api（`127.0.0.1:8080` → vLLM，模型 `deepseek-v4.1-flash`，离线免费）。
- **图片直出**：`https://gxwtf.cn/exam-images/<类型>/<学科>/<卷>/images/…`（nginx alias 到 `output_docx_to_md/…/images/`）。
- **答案大小写**：**所有学科**的选择题 `answer` 与选项 `options[].id` **一律大写字母**（`"C"`、`{"id": "C"}`）：
  英语由 `src/json_builder.py` 的 `_normalize_answer` / `_normalize_ai_options` 按题型处理，
  其余学科在各自写盘出口调用共享模块 [`src/answer_case.py`](src/answer_case.py)（数学 / 物理 / 语文 / 政治 /
  化学 / 图片版 / 英语批量兜底）；语法填空 / 选词填空 / 阅读表达 / 作文的单词答案**保留原文大小写**
  （`faced`、`FACE` 不改写），否则判题会坏。存量 JSON 用 `python3 normalize_json_case.py`（默认 dry-run，
  `--apply` 会先备份到 `DATA_ROOT/_backup_json_case_<时间戳>/`）迁移；`validate_json.py` 会以 warning 报不一致。
  补漏/收尾脚本（`fix_math_missing_options` / `fill_physics_*` / `postprocess_physics` / `repair_physics_wmf` /
  `repair_missing` 等）读取旧文件（小写 id）与新文件（大写 id）都要能跑：匹配时过 `option_key()` 统一小写，
  写盘前再 `normalize_choice_case_in_paper()`，并用 `count_nonempty_labels()` 断言 label 不丢。
- **下游入库**：`~/gxexercise` 的 `scripts/import-math-json.ts` / `scripts/import-chinese-json.ts`。

## 英语链路工作流程（链路 ①）

```
MD 文件 → AI 分析 → 内容提取 → JSON 构建 → 验证 → 输出
```

1. **加载 MD 文件** — 直接读取 Markdown 格式的试卷
2. **AI 分析** — 调用大模型识别题型结构、生成完整文章、提取答案和解析
3. **内容提取** — 对阅读类题型用 start 锚点从 MD 提取文章
4. **JSON 构建** — 组装最终的结构化 JSON
5. **验证** — 检查输出完整性

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入 API Key。**默认已切换为本机 API**（new-api 网关 → 本机 vLLM 集群，离线、免费、上下文 1M）：

```bash
API_TYPE=openai
OPENAI_BASE_URL=http://localhost:8080/v1
OPENAI_API_KEY=your_new_api_key_here   # 本机网关 key 找运维要，不入库
OPENAI_MODEL=deepseek-v4.1-flash
OPENAI_TIMEOUT=600          # 本地推理出长 JSON 较慢，超时放宽（秒）
```

`.env` 里其余被注释的段落是本机网关公网入口（`https://ai.gxwtf.cn/v1`）与官方付费 API 的备选配置，按需启用即可。`API_TYPE` 可选 `openai` / `anthropic`。

数据根默认是**仓库的上级目录**（本机 `/home/gxwtf/jingshibang`），换机器/换目录时不用改代码，在 `.env` 里加一行即可：

```bash
EXAM_DATA_ROOT=/data/exam     # 目录下需有 gaozhong/ 与 output_*/（不存在会自动按需创建）
```

### 3. 放入试卷

将 MD 格式的英语试卷放入 `data/input/` 目录。支持两种目录结构：

**单文件模式**（`main.py`）：所有 MD 文件平铺在一个目录中。

**批量模式**（`batch_convert_md_to_json.py`）：保持 `类型/学科/试卷名/试卷名.md` 的层级结构，对接 `batch_convert_docx_to_md.py` 的输出。

### 4. 运行

```bash
# === 单文件模式（main.py）===
# 使用默认配置处理所有 MD 文件
python3 main.py

# 指定输入/输出目录
python3 main.py -i ./data/input -o ./data/output

# 指定模型
python3 main.py -m deepseek-v4-flash

# 强制重新处理（覆盖已有输出）
python3 main.py --force

# 指定试卷重新生成（自动强制模式）
python3 main.py -p "2019北京朝阳高三二模英语" "2016北京东城高三二模英语"

# 校验已有 JSON 文件（空跑，不调用 AI）
python3 main.py --validate

# 校验指定 JSON 文件
python3 main.py --validate -p "2019北京朝阳高三二模英语"

# 开启调试模式（查看详细日志）
python3 main.py --debug

# === 批量模式（batch_convert_md_to_json.py）===
# 默认读取 ../output_docx_to_md（batch_convert_docx_to_md.py 的输出）
python3 batch_convert_md_to_json.py

# 指定输入目录
python3 batch_convert_md_to_json.py --dir ./test-paper

# 按类型/学科过滤
python3 batch_convert_md_to_json.py -T 一模 -s 英语

# 预览待转换文件
python3 batch_convert_md_to_json.py -T 上期末 --dry-run

# 强制重新转换
python3 batch_convert_md_to_json.py --force

# 单文件转换
python3 batch_convert_md_to_json.py -p ./path/to/paper.md
```

### 5. 查看结果

- `main.py`：解析结果保存在输出目录，文件名为 `{试卷名}.json`。
- `batch_convert_md_to_json.py`：输出到 `output_json/类型/学科/试卷名.json`，默认为输入目录旁。

## 命令行参数

### main.py

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i`, `--input` | 输入目录（MD 文件所在位置） | `data/input` |
| `-o`, `--output` | 输出目录（JSON 保存位置） | `data/output` |
| `-m`, `--model` | 模型名称 | 从 `.env` 读取 |
| `-k`, `--api-key` | API Key | 从 `.env` 读取 |
| `--base-url` | API 地址 | 从 `.env` 读取 |
| `--force` | 强制重新处理已存在的文件 | 默认跳过已处理 |
| `-p`, `--papers` | 指定试卷名（多个用空格分隔），自动强制重处理 | — |
| `--debug` | 开启调试模式（打印详细日志） | 关闭 |

### batch_convert_md_to_json.py

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--dir` | 输入目录（保持类型/学科层级） | `../output_docx_to_md` |
| `-p`, `--path` | 单文件模式：指定单个 MD 文件路径 | — |
| `-o`, `--output` | 输出目录 | 输入目录旁的 `output_json` |
| `-m`, `--model` | 模型名称 | 从 `.env` 读取 |
| `-k`, `--api-key` | API Key | 从 `.env` 读取 |
| `--base-url` | API 地址 | 从 `.env` 读取 |
| `-s`, `--subjects` | 过滤学科（如 `英语 语文`） | 全部学科 |
| `-T`, `--types` | 过滤类型（如 `一模 上期末`） | 全部类型 |
| `--force` | 强制重新转换已存在的文件 | 默认跳过 |
| `--dry-run` | 仅列出待转换文件，不实际转换 | — |
| `--debug` | 开启调试模式 | 关闭 |

## 项目结构

```
exam-parser/                      # 本仓库只放代码/文档；数据在 EXAM_DATA_ROOT（默认仓库上级目录）
├── paths.py                      # ★ 统一数据根：DATA_ROOT / GAOZHONG / OUTPUT_*
├── PIPELINES.md                  # ★ 学科 × 链路总表 + 每条链路的完整命令
├── config.py                     # 模型 / API / 英语题型配置（读 .env）
├── .env.example
│
├── # —— 共用预处理 ——
├── batch_convert_docx_to_md.py   # docx/doc → MD（w2m + LibreOffice）
├── batch_convert_pdf_to_md.py    # PDF → MD（MinerU，历史存量用）
├──
├── # —— 英语（链路 ①）——
├── main.py                       # 单目录模式入口
├── batch_convert_md_to_json.py   # 批量（保持 类型/学科/卷 层级）
├── validate_json.py              # 只校验结构，不调 AI
├── backfill_re_stems.py          # 阅读表达题干回填
├── prompts/                      # AI 提示词模板
├── src/                          # ai_client / parser / loader / locator / json_builder / validator
├──
├── # —— 数学（②）· 物理（③）——
├── convert_math_pdf.py           # 多模态底座：页面渲染 / 预筛 / chat_json
├── fix_math_missing_options.py   # 数学：定向补缺失选项
├── convert_physics_pdf.py        # 物理：PDF 页图 → 选择题 JSON
├── repair_physics_wmf.py         # 物理：.wmf 公式让模型重转录
├── postprocess_physics.py        # 物理：图片/占位符收尾
├── attach_physics_images.py      # 物理：从 docx→MD 挂插图
├── attach_physics_tables.py      # 物理：从 MD 挂回表格
├── fill_physics_options_from_md.py / fill_physics_option_images.py
├── apply_manual_tables.py + manual_tables/   # 手工补表格
├──
├── # —— 化学（④ 文本 · ⑤ 整卷 · ⑥ PDF · ⑦ 图片版）——
├── convert_chemistry_md.py       # ④ docx→MD→JSON（选择题）
├── convert_chemistry_full.py     # ⑤ 整卷（选择 + 非选择）
├── convert_chemistry_pdf.py      # ⑥ PDF 页图 → JSON
├── convert_emf_to_png.py         # .emf → PNG（走 LibreOffice）
├── fix_images.py / sanitize_mdx.py / check_katex.py
├── add_chem_choice_analysis.py   # ④ 选择题补文字解析
├── split_chem_docx.py            # ⑦ 按题拆 docx
├── reflow_docx.py / normalize_docx_spacing.py   # ⑦ 行距统一 + 浮动图内联
├── crop_questions_pdf.py         # ⑦ 按题号裁图（pdftoppm + pdftotext -bbox）
├── build_image_paper.py / batch_image_papers.py / build_image_questions.py
├── add_image_choice_analysis.py  # ⑦ 每题截图 → 多模态解析
├──
├── # —— 语文（⑧）· 政治（⑨）——
├── convert_chinese_md.py / docx_dots.py / merge_chinese_groups.py /
│   normalize_chinese_scores.py / repair_chinese_tables.py / sanitize_chinese.py
├── convert_politics_md.py
├──
├── # —— 通用工具 / 测试 ——
├── src/answer_case.py            # ★ 出口规范化：选择题 answer / options[].id 统一大写（各学科共用）
├── normalize_json_case.py        # ★ 存量题库 JSON 大小写迁移（默认 dry-run，--apply 先备份）
├── attach_md_images.py / merge_answers_from_pdf.py / repair_missing.py
├── test_ai_short.py / test_with_existing_response.py
└── data/                         # 仅英语单目录模式（main.py）用
    ├── input/
    ├── output/
    └── debug/
```

## 日常运维：增量爬取 / 更新题库 / 修复存量数据

整条流水线（爬取 → md → JSON → 入库）的操作手册在 [`../README.md`](../README.md)，这里只列
和解析器直接相关的脚本：

| 脚本 | 用途 |
|------|------|
| `../check_missing.py` | **仅爬新题**：比对线上清单与本地文件，只下载缺失的卷子（不碰解析器） |
| `../update_bank.py` | **手动更新题库数据**：按卷子依次跑 md → JSON → 校验 → 入库（内部调用本目录脚本） |
| `batch_convert_md_to_json.py` | md → JSON（调大模型）。**真题卷请用单文件模式 `-p`**，批量模式会因卷名没有年级而整批跳过 |
| `validate_json.py` | 只校验 JSON 结构、不调 AI：`venv/bin/python3 validate_json.py <json...>` |
| `backfill_re_stems.py` | 用 md 重算「阅读表达」题干并回填 JSON / 数据库（`content` 为空时用，不调 AI） |

```bash
# 常用组合（在 jingshibang 根目录执行）
PY=exam-parser/venv/bin/python3

python3 check_missing.py                                  # 看线上还缺哪些卷子（只读）
python3 check_missing.py --download                        # 只补缺失的
$PY update_bank.py --list                                  # 看哪些卷子还没 md / json / 入库
$PY update_bank.py --new --steps md,json,validate          # 跑到 JSON 并校验（不写库）
$PY update_bank.py --new --steps md,json,validate,import   # 含入库（写生产库，慎用）
$PY backfill_re_stems.py --all --dry-run                   # 阅读表达题干回填演练
```

改解析逻辑时留意三处历史坑（详见 [`../README.md`](../README.md) 第三节）：阅读表达的
`questionsText` 必须从 md 提取（不能指望 AI 给题干）、划线题作答提示的插入要容忍断行与
`explain why?` 写法、`find_questions_after_article` 的结束边界不能用任意 `**` 行。

## 支持的题型

| 题型 | 必考 | AI 输出 | 内容提取 |
|------|:--:|---------|----------|
| 完形填空 | ✓ | 完整文章 + `<ClozeBlank>` 标签 | — |
| 阅读 | ✓ | start 锚点 | 从 MD 提取文章 |
| 七选五 | ✓ | 完整文章 + `<Blank>` 标签 + options | — |
| 作文 | ✓ | start 锚点 | 从 MD 提取题目 |
| 语法填空 | | 完整文章 + `<Input>` 标签 | — |
| 选词填空 | | 词表 + 句子 + `<Input2>` 标签 | — |
| 阅读表达 | | start 锚点 | 从 MD 提取文章 |

> 作文可能有 1-2 个 section（如第一节+第二节）。

## 输出格式

```json
{
  "paper": {
    "totalScore": 100,
    "duration": 120
  },
  "sections": [
    {
      "type": "完形填空",
      "questionType": "cloze",
      "title": "第一节",
      "score": 15,
      "article": "完整文章，空位已替换为 <ClozeBlank></ClozeBlank>",
      "questions": [
        {
          "id": 1,
          "answer": "A",
          "analysis": "解析内容",
          "score": 1.5
        }
      ]
    }
  ]
}
```

## 调试

- 命令行加 `--debug` 开启调试模式，会打印详细日志
- 或在 `config.py` 中设置 `SHOW_AI_DEBUG = True`，AI 的提示词和响应会保存到 `data/debug/` 目录

## 输出优化

解析过程自动进行以下优化：

- **文件名清理**：自动去除"（教师版）"后缀，输出 JSON 文件名和 `title`/`source` 字段均不含该后缀
- **空标签空格修复**：修复 AI 在空标签周围的常见空格错误
  - `</ClozeBlank> .` → `</ClozeBlank>.`（删除空标签与标点间的多余空格）
  - `.I` → `. I`（标点后补空格）
  - 适用于所有空标签：`ClozeBlank`、`Input`、`Input2`、`Blank`

---

## 数学试卷解析（PDF 直读，多模态大模型）

数学不走 `MD → JSON` 那条链路。原因（实测 2024 西城一模）：

| 来源 | 结果 |
| --- | --- |
| mineru 转出的 MD | 数学公式和选项会漏（第 1 题 a/c 选项内容为空、第 7 题分段函数只识别出半句） |
| `pdftotext` 文字层 | 文字完整，但 `∪ ∞ ∩ √ π` 在 PDF 里是矢量图形，文字层里**根本不存在**（0 命中） |
| **PDF 页面图像 → 多模态模型** | 题干、选项、填空位、图形、答案一次性拿全 ✅ |

### 用法

```bash
# 单份（调试：只跑前 2 页）
python3 convert_math_pdf.py -p ../gaozhong/一模/数学/2024北京西城高三一模数学（教师版）.pdf --pages 2

# 单份（完整）
python3 convert_math_pdf.py -p ../gaozhong/一模/数学/2024北京西城高三一模数学（教师版）.pdf

# 批量：gaozhong/一模/数学 下所有含「高三」的 PDF
python3 convert_math_pdf.py -T 一模 -g 高三 -w 1

# 关闭定向补漏（更快，但跨页选项/漏答案不会补）
python3 convert_math_pdf.py -p xxx.pdf --no-repair
```

输出：`../output_json_math/<类型>/数学/<试卷名>.json`，另附 `<试卷名>.review.txt`（需人工复核清单）。

### 处理流程

1. `pdftoppm -r 150` 把 PDF 逐页转 PNG（150 dpi，一页约 1.2k tokens）
2. 每页丢给多模态模型，要求返回 `{questions, answers, figures}`：
   - 只取**选择题 + 填空题**，解答题忽略
   - 选择题判 `single`/`multiple`（按卷头「选出符合题目要求的一项」/「有多项符合题目要求」）
   - 填空题每空写成 `<Input2></Input2>`（题库的无序号短输入框组件）
   - 图形按归一化 bbox 返回 → `convert -crop` 裁剪成 `images/<试卷>_q<题号>.png` 并挂到题干
3. 跨页拼接：同一题号在多页出现时按页序拼接去重（题干被页边界截断的情况）
4. 定向补漏：选项空缺/缺答案的题，回原页 + 下一頁 + 答案页再问一次
5. 输出 JSON（`paper` + `sections`，选择题 `math-choice`、填空题 `math-fill`，每题 `questionType` 为 `single`/`multiple`/`input2`）

### 配置

`.env` 里可加：

```bash
MATH_VISION_MODEL=deepseek-v4.1-flash   # 多模态模型（默认取 OPENAI_MODEL）
MATH_DPI=150                            # 渲染分辨率
MATH_MAX_TOKENS=8000
```

`OPENAI_BASE_URL` / `OPENAI_API_KEY` 复用现有配置。本机 `deepseek-v4.1-flash` 支持读图，单页 1~2 秒，一份 10~15 页的卷子约 40~90 秒。

### 实测（2026-10-18，本机模型）

| 试卷 | 题数 | 答案 | 选项齐全 | 空位 | 图形 |
| --- | --- | --- | --- | --- | --- |
| 2024北京西城高三一模 | 15（选择10+填空5） | 15/15 | 10/10 | 8 | 1 |
| 2019北京通州高三一模（理） | 14（8+6） | 14/14 | 8/8 | 7 | 2 |
| 2014北京东城高三一模（理） | 14（8+6） | 14/14 | 8/8 | 8 | 3 |
| 2014北京东城高三一模（文） | 14（8+6） | 14/14 | 8/8 | 6 | 0 |
| 2016北京东城高三一模（理） | 14（8+6） | 13/14 | 8/8 | 8 | 3 |

注意：**北京卷选择题数量随年份变化**（2014 年是 8 题、2024 年是 10 题），脚本按模型返回的题型判定分组，不看题号区间。

### 速度优化（实测 66s/份 → 9s/份）

| 优化 | 说明 |
| --- | --- |
| `reasoning_effort: none` | 关闭思考模式（最大的提速项，单页 15~37s → 2~5s）；可用 `MATH_REASONING=on` 关闭该行为 |
| 页面预筛 | 先用 `pdftotext` 看文字层，跳过解答题页/解答题答案页（每份能跳过 2~6 页） |
| 页级并发 `-w` | 本机建议 8，vLLM 批处理吃满 |
| 请求超时/重试 | `MATH_REQUEST_TIMEOUT`（默认 240s），429/502 自动退避重试 |

换用其它多模态模型（如 OpenRouter）：

```bash
python3 convert_math_pdf.py -p xxx.pdf \
  --base-url https://openrouter.ai/api/v1 \
  --api-key sk-or-xxx \
  -m nvidia/nemotron-3.5-super-vl:free -w 3
```

脚本会自动处理 `\(...\)` → `$...$` 分隔符归一化，并对 429/502 重试、对 `finish=length` 自动加大 max_tokens。

---

# 化学（选择题）导入流程

化学二模和数学/物理的导入**结果结构一样**（`output_json_math/<类型>/化学/<卷>.json`，字段
`paper/sections/questions{id,questionType,content,options,answer,score}`），但**取文字的方式不同**：

> **别用「AI 直接读 PDF 页面」这条路。** 化学教师版 PDF 里大量浮动插图压在文字上
> （实测 2025 西城第 2–4 题选项被分子图/表格图盖住），模型读图会把被盖住的选项截断。
> 改用 Word 转出的 Markdown 取文字（完整），AI 只做「排版转换」。

## 一条龙命令

```bash
cd ~/jingshibang

# ① doc/docx → Markdown（w2m，图片导出到 <卷>/images/）
python3 batch_convert_docx_to_md.py -T 二模 -s 化学 -w 4

# ② Word 里的 .emf 图形（公式箭头/装置图）→ PNG（ImageMagick 不支持 EMF，走 LibreOffice）
python3 exam-parser/convert_emf_to_png.py output_docx_to_md/二模/化学

# ③ Markdown → 题库 JSON（切题/选项/答案/图片占位，AI 只做 LaTeX 排版转换）
cd exam-parser
python3 convert_chemistry_md.py -T 二模 -w 4

# ④ 收尾：缺题补题、缺答案回退教师版 PDF、图片扩展名与 MDX/KaTeX 安全化
python3 repair_missing.py --type 二模                    # 读 review.txt 里的「缺题号」，逐页读 PDF 补回
python3 merge_answers_from_pdf.py --type 二模             # 学生版 docx 没答案时，用（教师版）PDF 取答案
python3 fix_images.py                                    # .tiff/.emf/.wmf → .png（nginx 只放行 png/jpg/jpeg/gif/webp）
python3 sanitize_mdx.py                                  # 裸 < > 转 $<$；sub/sup 转 Unicode；{ } 转义
NODE_PATH=~/gxexercise/node_modules python3 check_katex.py # 全量 KaTeX 校验，应为 0 失败

# ⑤ 导入题库（先 --dry-run 看份数/题数）
cd ~/gxexercise
npx tsx scripts/import-math-json.ts --type 二模 --subject 化学 --dry-run
npx tsx scripts/import-math-json.ts --type 二模 --subject 化学
npx tsx scripts/sync-math-json.ts   --type 二模 --subject 化学   # 把 images/… 改写成 /exam-images/…
```

## 每一步在解决什么坑

| 坑 | 处理 |
| --- | --- |
| 教师版 PDF 图压字 → 选项截断 | 文字取自 docx→MD，不用 PDF 读图 |
| `![]()` 占位会被模型抄丢 | 先把图片换成 `⟦IMGn⟧` 占位符交给模型，只在原位保留 |
| Word 公式/图形是 .wmf/.emf | `wmf2gd` 转 png；EMF 走 `convert_emf_to_png.py`（LibreOffice+pdftoppm+trim） |
| 选项整块是图（四宫格/表格） | `ocr_options()` 读图转录；失败再四等分裁切并写进 review |
| 选项是表格列（表头 `A|B|C|D`） | `fix_table_options()` 按列合成选项 |
| 选项挤在同一行 `A．… B．…` | `expand_inline()` 拆开 |
| 末尾粘进下一题题干/「第二部分」 | `trim_tail()` 按「题号 > 本题号」或部分标题截断 |
| Word 把「10．」转成编号列表「1.」 | 用后继题号（11 = 9+2）认回来 |
| 题号被吞/选项全图切不出 | `repair_missing.py` 逐页读 PDF 补题 |
| 学生版 docx 没有答案 | `merge_answers_from_pdf.py` 用（教师版）PDF 取答案 |
| `{ }` 被 MDX 当表达式 | `sanitize_mdx.py` 转义；`check_katex.py` + next-mdx-remote 序列化双校验 |

## 实测结果（2026-10-22）

- 二模化学 108 份源卷 → **导入 107 卷 / 1098 题**：0 缺答案、0 空选项；2263 个图片引用全部存在且线上 200。
- 全量校验：MDX（next-mdx-remote + remark-gfm/math + rehype-katex）序列化 5490 段 0 失败；KaTeX 4803 段公式 0 失败。
- 未导入 1 份：`2020北京门头沟高三二模化学`（源文件是学生版，整卷无答案，无法判分）。
- 删除 1 题：`2018北京西城` 第 12 题（答案表只到第 11 题，无答案）。
- 待人工抽查：16 题「选项整块是图、四等分裁切」、99 处「排版校验未过改用 Unicode 上下标」（见各卷 `.review.txt`）。

---

# 语文（docx → MD → JSON）

语文走的是**和英语同一套「锚点」思路**：文章/材料不交给模型复述，模型只给结构规划（题组划分、
题号、材料起止锚点），正文与题目一律由脚本从 Markdown 里精确切出。

```bash
# 单卷（-T 决定输出到 output_json_chinese/<类型>/）
python3 convert_chinese_md.py -p ../output_docx_to_md/二模/语文/2025北京西城高三二模语文（教师版）/2025北京西城高三二模语文（教师版）.md -T 二模 --force

# 目录批量
python3 convert_chinese_md.py --dir ../output_docx_to_md -T 二模 -w 3
```

输出 `../output_json_chinese/<类型>/<卷>.json`：`{paper, groups:[{category, questionType, title,
intro, content, score, questions[]}]}`，`content` 是该组的**题面材料**，`questions` 是小问。
`category` 取「多文本 / 文言文 / 古诗 / 文学类 / 名著 / 默写 / 语用 / 微写作 / 作文」，
入库脚本按它上架到对应题型页。文言文选择题的加点字用 `<Dot>x</Dot>` 标注（读 docx 的 `w:em(dot)`，
见 `docx_dots.py`）。

## 名著阅读的约定（2026-10-22）

北京卷名著题常见形态是「一句空引导语 + 判词/选段 + (1)(2) 两小问」，题库里按「题面 + 每小题
一个答题框」呈现，**不再走左右两栏的阅读区**：

1. **去掉空引导语**：`根据要求，回答问题。` / `阅读下面的材料，回答问题。` / `从下面两个题目中
   任选一题作答。` 这类只有指令、没有实际内容的开头行由 `strip_leadin()` 删掉（`LEADIN_RES` 里维护）。
2. **材料进 `content`**：第一个小题标号 `(1)`/`（1）` 之前的文字（判词、选段）作为题组 `content`，
   前端把它排在题干上方（`Problem` 的 `articleLayout="inline"`）。
3. **多小问拆成多道题**：以行首 `(1)(2)…` 为界拆开，各题从原文里取出自己的分值（`（4分）`），
   答案同样按 `(1)(2)` 从答案区分段并去掉 `【评分参考】`；分值对不上时有兜底规则（见
   `split_named_book()`）。
4. **单问的名著**（没有 `(1)(2)`）：整段仍是一道题，只去掉空引导语，`content` 为空。

## 语文入库

```bash
cd ~/gxexercise
npx tsx scripts/import-chinese-json.ts --file ~/jingshibang/output_json_chinese/二模/2025北京西城高三二模语文.json --dry-run
npx tsx scripts/import-chinese-json.ts --file ... --force   # 覆盖：先删同名套卷再导（作答记录一并删）
```

前端约定：`category=名著` 的 `chinese-reading` 题组按单栏渲染（材料 + 每小题一个答题框，参考英语
「阅读表达」的作答体验），其它阅读类（多文本/文言文/古诗/文学类）仍是左文章右题目的两栏；题组
`content` 为空时不留空的阅读区。

## 其它学科 / 其它链路

README 上面几节是**英语**（链路 ①）的完整说明，以及数学 / 化学 / 语文的历史章节。物理、政治、
化学图片版等链路的命令与产物，统一维护在 [PIPELINES.md](PIPELINES.md)：

- **物理**（PDF 页图 → 选择题 JSON，`.wmf` 公式修复 + 图片/表格挂载）→ PIPELINES 第 4 节
- **化学四条链路**（文本 / 整卷 / PDF / 图片版）→ PIPELINES 第 5 节
- **政治**（docx→MD→JSON，按模块打标签）→ PIPELINES 第 7 节
- **生物 / 历史 / 地理**：尚无链路，新增方式的检查清单在 PIPELINES 第 8～9 节
