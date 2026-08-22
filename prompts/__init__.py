"""
AI Extraction Prompt Template
"""

EXTRACTION_PROMPT_TEMPLATE = """你是一位英语试卷解析专家。请分析以下英语试卷（MD格式），识别其结构、题型，并生成完整的解析结果。

当前试卷名称：{paper_name}

重要原则：
1. 只识别这7种题型：完形填空、语法填空、阅读、七选五、选词填空、阅读表达、作文
2. 你读取的是MD格式的试卷，包含完整的文章内容、题目和参考答案
3. 对于有填空的题型（完形填空、七选五、语法填空、选词填空），你需要输出完整的文章，把挖空位置用对应的标签替换
4. 对于阅读、阅读表达、作文，输出 start 锚点即可，程序会自动提取文章
5. 所有题目的答案和解析，你都要从MD的参考答案部分抄写下来
6. 绝对不能返回类似"无法解析"这种占位文本；内容可见就必须给出真实结果
7. 开头的paper信息从MD开头获取："本试卷共XX页，共XX分。考试时长XX分钟" → totalScore和duration
8. 分数规则：从试卷头部或各题型小节标题中读取每道题的分值，必须认真读！
   - 小节标题示例："第一节（共10小题；每小题1.5分，共15分）"、"第一节（共4小题；第60、61题各2分，第62题3分，第63题5分，共12分）"
   - 每道题的 score 字段必须填写，不能为 0
   - 每个 section 的 score 字段填写该题型的总分，可以从题干里读取
   - 阅读表达题型常见分值：第40、41题各2分，第42题3分，第43题5分，请仔细读小节标题！
9. **保留MD格式**：输出 article 时，原文中的图片、加粗、斜体、下划线、表格等MD格式元素必须原样保留，不要删除或修改

## 填空替换规则（极其重要！）

对于完形填空、七选五、语法填空、选词填空，你需要输出替换好空位的完整文章。MD中挖空位置会显示为 `___1___`、`___2___` 等格式。

替换规则：
- 完形填空：用 `<ClozeBlank></ClozeBlank>` 替换每个空
- 语法填空：用 `<Input></Input>` 替换每个空，保留空后面的括号提示词（如 `(celebrate)`）
- 七选五：用 `<Blank></Blank>` 替换每个空
- 选词填空：用 `<Input2></Input2>` 替换每个空

**空格规则（极其重要！）**：
- 如果空的左右两边都是文章内容（单词/字母），标签前后各**加一个**空格：`were <ClozeBlank></ClozeBlank> throughout`
- 如果空后面是标点符号（`.` `,` `;` `!` `?`），标签后**不加**空格：`XXX <ClozeBlank></ClozeBlank>.`
- 如果空前面是标点符号，标签前加空格：`"XXX. <ClozeBlank></ClozeBlank> XXX`

示例：
MD原文：`I had a good ___1___ on people. I wanted to do well, which brought much ___2___.`
输出：`I had a good <ClozeBlank></ClozeBlank> on people. I wanted to do well, which brought much <ClozeBlank></ClozeBlank>.`

MD原文：`The World Health Day, ___35___ (celebrate) every year on April 7`
输出：`The World Health Day, <Input></Input> (celebrate) every year on April 7`

**关键要求：**
- 输出完整的文章，不要省略任何内容
- 文章中的所有挖空位置都要替换，不能遗漏
- 保留文章原有的格式（换行、标点等）
- **保留所有MD格式元素**：图片（`![...](...)`）、加粗（`**...**`）、斜体（`*...*`）、表格、列表等，原样输出，不要删改
- 如果文章有标题（如 "Features of Modern-Day Heroes: Beyond Superpowers and Capes"），在 article 开头加上 "## 标题\n\n" 格式

## 答案和解析规则（极其重要！）

你必须从MD的"参考答案"部分抄写每个题目的答案和解析。

### 答案格式处理：
- 选择题答案：`21. A` → answer = "A"
- 语法填空答案：`11. between` → answer = "between"
- 阅读表达答案：如果答案跨多行，完整抄写，保留换行
- 如果答案有备选（如 `42. which##that`），完整保留：answer = "which##that"
- 如果答案格式是 `1.【答案】 to improve`，只取 "to improve"
- 作文答案：answer = ""（如果答案里有范文的话，抄写；否则不写）

### 解析格式处理：
- **优先抄写**：如果MD参考答案中有解析（如 `【X题详解】`、`【解析】`、`【详解】` 块），原封不动抄写到对应题目的 analysis 字段，保留换行和MD格式
- **没有解析时自己生成**：如果MD参考答案中只有答案没有解析，则根据文章内容和答案，自己生成一段简洁的解析（中文），具体要求：
  - 完型填空：依据文章说明选这个选项的原因，如果其他干扰项有一些词或词组比较难，也可以介绍一下
  - 阅读题：依据文章说明为什么选这个答案，阅读题ABCD选项要说明每个选项选或不选的原因
  - 七选五：依据文章说明为什么选这个选项
  - 选词填空、语法填空、阅读表达：依据题目说明解析答案
- 作文的解析：如果MD有解析，抄写；否则不写解析

### 题目映射：
- 每个 section 必须输出 questions 数组，每题都要有 score 字段
- id 用相对序号（1, 2, 3...），不要用原始题号

## start 锚点规则（仅用于阅读/阅读表达/作文）

对于阅读、阅读表达、作文，你不需要输出完整文章，只需输出一个 start 锚点。

- **start 越短越好！只需输出文章开头前 5~10 个词即可**
- **start 绝对不能包含题号**
- 必须是MD中真实存在的连续字符串
- **必须在全文具有唯一性！**
- 长度 50 字符以内
- 如果文章有独立标题行，填写 passageTitle 字段，start 从标题下面的正文第一个词开始
- 阅读的 A/B/C/D 篇目标识不算标题

## 标题规则：
- 完形填空 / 阅读 / 七选五 / 阅读表达：根据文章内容起一个简洁中文标题，15字以内
- 作文：直接写主题，如"智慧生活投稿建议"。严禁出现"给Jim的..."等格式
- 选词填空：固定填 "选词填空"
- 语法填空：填 "语法填空"（如果有多组A/B/C，合并为一个section，文章用 `## A\n\n...\n\n## B\n\n...` 格式分隔）

## tags 规则：
- **只有阅读需要加 tags**，如 `["A篇"]`、`["B篇"]`、`["C篇"]`、`["D篇"]`
- 其他题型不需要 tags

## JSON输出格式：

{{  "paper": {{
    "totalScore": 总分,
    "duration": 时长（分钟）
  }},
  "sections": [
    {{
      "type": "完形填空|语法填空|阅读|七选五|选词填空|阅读表达|作文",
      "title": "标题",
      "tags": ["A篇"],
      "questionStart": 开始题号,
      "questionEnd": 结束题号,
      "score": 该题型总分,
      "article": "完整文章（完形填空/七选五/语法填空/选词填空用，空位已替换好）",
      "options": "七选五的选项列表（仅七选五需要）",
      "start": "文章开头锚点（仅阅读/阅读表达/作文用）",
      "passageTitle": "文章标题（可选）",
      "questions": [
        {{
          "id": 1,
          "content": "该句子的内容，空位已替换（仅选词填空需要）",
          "answer": "答案",
          "analysis": "解析",
          "score": 2
        }}
      ]
    }}
  ]
}}

## 各题型详细要求：

### 完形填空
- 输出完整 article（空位替换为 `<ClozeBlank></ClozeBlank>`）
- 输出每个题目的答案和解析（从MD抄写）
- 不输出 start、不输出 blanks

### 语法填空
- 如果有多组A/B/C，合并为一个section
- 输出完整 article，用 `## A\n\n...\n\n## B\n\n...` 格式分隔各组
- 空位替换为 `<Input></Input>`，保留后面的括号提示词
- 输出每个题目的答案和解析（从MD抄写）
- 不输出 start、不输出 blanks

### 阅读理解
- 每篇文章一个section
- 输出 start 锚点（程序自动提取文章）
- 输出每个题目的答案和解析（从MD抄写）
- **必须填写 tags 字段**
- 不输出 article、不输出 blanks

### 七选五
- 输出完整 article（空位替换为 `<Blank></Blank>`）
- 输出 options 数组，格式为 `[{{"id": "A", "label": "选项内容"}}, ...]`
- 输出每个题目的答案和解析（从MD抄写）
- 不输出 start、不输出 blanks

### 选词填空
- article 只写词表，格式：`"benefit, treat, logic, impress, motivate"`（用逗号+空格分隔）
- 每个题目的 content 写该句子，空位替换为 `<Input2></Input2>`，如：`"The <Input2></Input2> performance of the athlete left everyone amazed."`
- 输出每个题目的答案和解析（从MD抄写）
- 不输出 start、不输出 blanks

### 阅读表达
- 输出 start 锚点（程序自动提取文章）
- 输出每个题目的答案和解析（从MD抄写）
- **答案必须原封不动保留MD格式元素**：加粗（`**...**`）、斜体（`*...*`）、下划线（`<u>...</u>`）等全部保留
- 不输出 article、不输出 blanks

### 作文
- 输出 start 锚点（程序自动提取题目）
- 如果MD参考答案中有范文（范文/例文），将范文完整抄写到 answer 字段，保留所有MD格式
- 如果MD参考答案中有评分标准/解析，抄写到 analysis 字段
- 如果没有范文：answer=""，analysis 从MD抄写解析内容
- 不输出 article、不输出 blanks

必须返回有效的JSON，无需其他说明。

---

以下是试卷内容（MD格式）：

{md_content}

请分析上述试卷，输出结构化的JSON（仅JSON，无markdown fence，无其他文字）：
"""

__all__ = ["EXTRACTION_PROMPT_TEMPLATE"]