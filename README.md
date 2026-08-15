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

### 1. 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```bash
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
```

### 2. 放入试卷

将 MD 格式的英语试卷放入 `data/input/` 目录。

### 3. 运行

```bash
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

# 完整示例
python3 main.py -i ./my_papers -o ./my_output -m deepseek-v4-flash --force --debug
```

### 4. 查看结果

解析结果保存在输出目录，文件名为 `{试卷名}.json`。

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i`, `--input` | 输入目录（MD 文件所在位置） | `data/input` |
| `-o`, `--output` | 输出目录（JSON 保存位置） | `data/output` |
| `-m`, `--model` | 模型名称 | 从 `.env` 读取 |
| `-k`, `--api-key` | API Key | 从 `.env` 读取 |
| `--base-url` | API 地址 | 从 `.env` 读取 |
| `--force` | 强制重新处理已存在的文件 | 默认跳过已处理 |
| `--debug` | 开启调试模式（打印详细日志） | 关闭 |

## 项目结构

```
exam-parser/
├── main.py              # 主入口
├── config.py            # 配置文件
├── test_with_existing_response.py  # 测试脚本（用已有 AI 响应）
├── prompts/
│   ├── __init__.py      # AI 提示词模板
│   └── extraction_prompt.py
├── src/
│   ├── ai_client.py     # AI API 客户端
│   ├── parser.py        # 主解析流程编排
│   ├── loader.py        # MD 文件加载
│   ├── locator.py       # 内容定位（start 锚点查找）
│   ├── json_builder.py  # JSON 构建器
│   └── validator.py     # 输出验证器
└── data/
    ├── input/           # 输入目录（放 .md 文件）
    ├── output/          # 输出目录（生成的 JSON）
    └── debug/           # 调试文件（AI 提示词、响应等）
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