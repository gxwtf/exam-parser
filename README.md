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

复制 `.env.example` 为 `.env`，填入 API Key：

```bash
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
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

## 支持的题型

| 题型 | AI 输出 | 内容提取 |
|------|---------|----------|
| 完形填空 | 完整文章 + `<ClozeBlank>` 标签 | — |
| 语法填空 | 完整文章 + `<Input>` 标签 | — |
| 七选五 | 完整文章 + `<Blank>` 标签 + options | — |
| 选词填空 | 词表 + 句子 + `<Input2>` 标签 | — |
| 阅读 | start 锚点 | 从 MD 提取文章 |
| 阅读表达 | start 锚点 | 从 MD 提取文章 |
| 作文 | start 锚点 | 从 MD 提取题目 |

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