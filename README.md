# 英语试卷解析器 (English Exam Parser)

基于 AI 的英语试卷解析工具，将 Markdown 格式的试卷解析为结构化 JSON。

## 工作流程

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
exam-parser/
├── main.py                      # 主入口（单目录模式）
├── batch_convert_md_to_json.py  # 批量转换脚本（保持类型/学科层级）
├── validate_json.py             # 只校验 JSON 结构（不调 AI）
├── backfill_re_stems.py         # 阅读表达题干回填（用 md 重算，不调 AI）
├── config.py                    # 配置文件
├── test_with_existing_response.py  # 测试脚本（用已有 AI 响应）
├── prompts/
│   ├── __init__.py              # AI 提示词模板
│   └── extraction_prompt.py
├── src/
│   ├── ai_client.py             # AI API 客户端
│   ├── parser.py                # 主解析流程编排
│   ├── loader.py                # MD 文件加载
│   ├── locator.py               # 内容定位（start 锚点查找）
│   ├── json_builder.py          # JSON 构建器
│   └── validator.py             # 输出验证器
└── data/
    ├── input/                   # 输入目录（放 .md 文件）
    ├── output/                  # 输出目录（生成的 JSON）
    └── debug/                   # 调试文件（AI 提示词、响应等）
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
