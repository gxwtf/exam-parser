# 历史 / 地理 / 政治 一模流水线设计（2026 西城·海淀·丰台）

> 目标：三科各 3 份 2026 高三一模（教师版）走「docx → MD → JSON → 生产库」，
> **题目样式与生物一致**（生物链路 = 链路 ⑩ `convert_biology_md.py` + `scripts/import-biology-json.ts`）。
> 本文件是三科共用的**契约**，改结构先改这里。

## 0. 术语

- **卷** = 一套一模试卷；**题** = 卷面上的大題（16、17…）；**小问** = 大题里的 (1)(2)(3)。
- 生物式结构 = 选择题一组 + **一个大题一个 section**（材料挂在 section 上，小问是小 section 里的「空」）。

## 1. 输入

```
gaozhong/一模/<学科>/2026北京<区>高三一模<学科>（教师版）.docx
output_docx_to_md/一模/<学科>/2026北京<区>高三一模<学科>（教师版）/<同名>.md + images/
```

已转换好 9 份（西城 / 海淀 / 丰台 × 历史 / 地理 / 政治），**不要重跑 docx→md 覆盖别人的产出**：
三科各自只碰自己学科目录。

## 2. 输出（JSON，字段名与导入脚本严格一致，不要改名）

```json
{
  "paper": {
    "title": "2026北京西城高三一模历史",      // 去掉（教师版）/含答案 与空格
    "subject": "历史", "source": "2026北京西城高三一模历史",
    "grade": "高三", "year": 2026, "totalScore": 100, "duration": 90,
    "tags": ["历史", "高三", "一模", "2026"]
  },
  "sections": [
    {
      "type": "选择题", "questionType": "<subject>-choice", "title": "一、选择题",
      "category": "选择", "score": 45,
      "questions": [
        {"id": 1, "questionType": "single",
         "content": "【共享材料 + 题干】", "options": [{"id": "A", "label": "…"}, {"id": "B", "label": "…"}, {"id": "C", "label": "…"}, {"id": "D", "label": "…"}],
         "answer": "B", "analysis": "", "score": 3}
      ]
    },
    {
      "type": "非选择题", "questionType": "<subject>-fill", "title": "二、非选择题（第16题）",
      "category": "史料分析", "score": 10,
      "content": "材料一 …\n\n（1）依据材料一，概括宋代医学儒家化的表现。<AnswerBox></AnswerBox>\n\n（2）…<AnswerBox></AnswerBox>",
      "questions": [
        {"id": 1601, "questionType": "input",
         "content": "（1）依据材料一，概括宋代医学儒家化的表现。",
         "answer": "儒家知识分子从医；医生职业伦理体现儒家道德；……",
         "analysis": "", "score": 4}
      ]
    }
  ]
}
```

`<subject>-choice` / `<subject>-fill`：历史 `history-choice/history-fill`、地理 `geography-choice/geography-fill`、
政治 `politics-choice/politics-fill`。**政治旧的 `politics` 组型不再使用**（生产库里 1 份老卷保留不动）。

## 3. 硬约定（导入脚本与前端都按这些走）

### 3.1 选择题
- **每题自洽**：卷面上「读图，回答第 3、4 题」这种共享材料/共享图，**复制进每道题的 `content`**
  （单题练习、组卷、PDF 都要能独立显示）。材料里统一用 `\n\n` 分段，图片独占一段。
- `options` 恒为 A–D 四项目、`id` 大写、`answer` 单个大写字母（出口调
  `src/answer_case.normalize_choice_case_in_paper`）。
- 题干里的 ①②③④ 组合式选项（`A. ①②`）照原样放进 label，不要拆。
- `score` 按卷面（北京卷 3 分/题），`section.score = Σ`。
- `content` 里不要留题号前缀（`1.` / `4\.` / `（1）` 开头都去掉），前端自己编号。

### 3.2 非选择题（一个大题一个 section）

> **2026-11 口径**：版式仍是**单栏内联**（材料 + 设问都在题组 `content` 里，按题面顺序），
> 每个小问后面紧跟一个 **`<AnswerBox></AnswerBox>`** —— 前端把它渲染成英语「阅读表达」那种
> 大答题框（placeholder「请在此作答...」、可拖拽放大）。**不再使用** `<InputLong>` / `<Input>`。

- `section.content` = 材料 + 各小问设问（设问原样内联、**只出现一次**），每个设问后紧跟一个
  `<AnswerBox></AnswerBox>`；占位符个数 == `questions.length`，顺序与题面一致。
- `questions[i]` = `questionType: "input"`、`content` = 该小问设问原文（不含材料、不含分值括号，
  也是 AI 批改的 `{stem}`）、`answer` = 参考答案该小问原文、`score` = 该小问分值。
- `Σ questions.score == section.score == 题面标注的大题分值`；对不上时把差额给最后一问并在
  `<卷>.review.txt` 记一行。**绝不留 0 分**。
- `category` 从各学科固定标签里选（见第 4 节），整题一个标签。
- 答题框**不带编号**：编号由前端按文档顺序自动给，所以 `content` 里不要手写「1.」前缀
  （设问本身以「（1）」开头是原卷写法，保留）。

### 3.3 图片 / 图表
- 引用一律写绝对地址：`/exam-images/一模/<学科>/<卷名>/images/<文件名>`（**卷名不带「（教师版）」**），
  文件真实存在于 `output_docx_to_md/一模/<学科>/<卷名>（教师版）/images/`。nginx 只放行 png/jpg/jpeg/gif/webp。
- **能用 docx 导出图就用导出图**；下列情况改用 **PDF 截图**（`pdftoppm -r 200` + `convert -crop`，
  产物存同名 `images/` 目录，命名 `fig-<题号>.png`），并在 review 里记一行：
  1. docx 导出图宽度 < 900px 且是**复杂地图/统计图/多要素示意图**（看不清标注）；
  2. md 里图被拆散/丢图/顺序错位（docx 浮动图常见）；
  3. 表格其实是一张图，或单元格里塞图片导致 GFM 表格没法复原；
  4. 需要「画图/补图」的参考答案（答案本身是图）。
- 表格：真的数据表 → GFM markdown 表格（单元格内 `<br>` 允许）；否则改图或列表，**不要留半截表格**。

## 4. 学科标签（= 题库 `categoriesBySubject`，一字不差）

| 学科 | 可选 category（非选择题） |
| --- | --- |
| 历史 | 观点论证 / 史料分析 / 修改补写 |
| 地理 | 自然地理 / 人文地理 / 区域发展 / 图表判读 |
| 政治 | 中特 / 经济社会 / 政治法治 / 哲学文化 / 国政国经 / 法律生活 / 逻辑思维 |

选择题 section 的 category 恒为 `选择`。分类可用一次 AI 调用（给题号+设问摘要，返回 {题号: 标签}），
返回不在表里一律落到该学科第一个标签并在 review 记一行。

## 5. 脚本要求（每科一个，落 `exam-parser/`）

- 入口名：`convert_history_md.py` / `convert_geography_md.py` / `convert_politics_md.py`（政治＝重写原文件）。
- 复用现成件：`from convert_chinese_md import chat, rewrite_images, split_paper, log`、
  `from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_HISTORY|OUTPUT_JSON_GEOGRAPHY|OUTPUT_JSON_POLITICS`、
  `from src.answer_case import normalize_choice_case_in_paper`。切题/清空/占位符可参考
  `convert_biology_md.py` 的写法（尤其是 QMARK 题号行带前置图片的正则、分值配平、图片校验）。
- CLI 与现有一致：`-p <md 或卷目录>`、`-T 一模`、`--force`、`--no-ai`（不调模型，分类走缓存/默认）、
  `-w`；不带 `-p` 时处理 `output_docx_to_md/一模/<学科>/*`。
- 输出：`output_json_history|output_json_geography|output_json_politics/一模/<卷名>.json`，
  旁挂 `<卷名>.review.txt`（每条一行，前缀 `复核：`）。
- **必备 `--check`**（纯 Python，不调模型）：逐卷断言
  1. 选择题：选项 4 个、id 为 A–D、answer ∈ options、answer 大写；
  2. 非选择题：`<AnswerBox>` 个数 == questions 数且必须成对、`content` 里不得残留 `<Input`/`<InputLong`、
     每个设问在 `content` 里**恰好出现一次**、小问 `questionType` 一律 `input`、
     Σscore == section.score、每题 answer 非空、score > 0；
  3. 题号连续、多数题数对得上卷面声明（第一部分 N 题 / 第二部分 M 题）；
  4. 图片引用逐张落到磁盘（`/exam-images/一模/<学科>/<卷>/images/x` → 实际目录含 `（教师版）` 时也要能命中）；
  5. section.category 在该学科标签表里。
  输出「卷 × 检查项」表 + 退出码（全过 0，否则 1）。

## 6. 入库（lead 做，别抢）

`gxexercise/scripts/import-subject-json.ts --subject 历史|地理|政治 --file <json> [--dry-run|--force]`，
结构自检 + 作答快照 + 事务提交，与 `import-biology-json.ts` 同口径。9 份卷由 lead 统一导入生产库。

## 7. 验收

- `--check` 三科全绿；
- lead 用 QA 脚手架 `python3 qa_scaffold.py --subject <学科> --type 一模 --check` 复查（图片/分值/空答案）；
- 人工抽看：西城那份在中库后页面渲染（选择题、图表题、材料+小问）。
