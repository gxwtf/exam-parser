# 语文 / 英语 流水线迁移到成熟格式（迁移契约）

> 目标学科：语文、英语。参照对象：生物、政治、历史、地理（链路 ⑨–⑫）。
> 测试卷：2026 西城 / 海淀 / 丰台 高三一模（语文可加对应二模做对比）。
> 本文是**双方共用的契约**，两个学科的转换器都必须满足第 2、3、4 节的硬性要求。

## 1. 现状与痛点

| 学科 | 现在的链路 | 痛点 |
| --- | --- | --- |
| 英语 | `batch_convert_docx_to_md.py` → `batch_convert_md_to_json.py`（内部走 `main.py` + `src/json_builder.py`）→ `validate_json.py` | md→JSON 走一整套 app（配置 + builder + 若干 backfill 脚本），入口分散、没有 `--check` 单卷自检、出问题要翻多个文件 |
| 语文 | `convert_chinese_md.py` + `merge_chinese_groups.py` + `normalize_chinese_scores.py` + `repair_chinese_tables.py` + `sanitize_chinese.py` | 一次转换要跑 4 个后处理脚本，等价逻辑分散；产出的顶层键是 `groups`（旧契约），与成熟链路的 `sections` 不一致 |

成熟链路（生物/历史/地理/政治）的样子：**一个 `convert_<学科>_md.py` 打通「md → JSON + review」，自带 `--check` 断言**，
不再需要额外的合并/归一/清理脚本；产出一份 JSON + 一份 `.review.txt`。

## 2. JSON 契约（必须与成熟链路一致）

顶层：

```json
{ "paper": { "title", "subject", "grade", "year", "totalScore", "duration", "tags" },
  "sections": [ { "type", "questionType", "title", "category", "score", "content", "questions": [...] } ] }
```

- **顶层键必须是 `sections`**（语文现为 `groups`，这次要改）；
- 一个「大题」= 一个 section；`content` 放材料（含各小问设问的内联文本，材料**只出现一次**）；
- 选择题 section：`questionType` 用学科自己的 choice 类型（见第 4 节），每题 `content` 自洽（共享材料复制进每题）；
- 非选择题 section：`content` = 材料 + 每个小问「设问原文 + 一个填空占位标签」（语言类学科按第 4 节用各自的占位标签）；
- `questions[]` 里每个小问 `questionType` 用成熟链路已有的类型（见第 4 节），`content` = 该小问设问原文；
- 分值：`Σ section.score == paper.totalScore`，`Σ 小问 score == section.score`。

## 3. 转换器硬性要求（与 `convert_politics_md.py` 同构）

1. **单文件、单次调用**：`python3 convert_<学科>_md.py -p <md 路径> [-o <输出根>]`，产出 `<卷>.json` + `<卷>.review.txt`；
2. **`--check`**：`python3 convert_<学科>_md.py --check -T <类型>` 与 `--check -p <单卷>`，把第 6 节的断言逐条跑一遍，
   有问题直接 `exit 2` 并打印 `卷名: 具体原因`；无问题打印「全部通过 ✓」；
3. **输出清洗**（出口处一次性做，不要散在后处理脚本里）：题干/材料开头的分区标题（`**一、选择题**`、`第一部分…`）、
   卷首说明（考生须知/本试卷共N页/满分/考试时间）、残留题号、选项标签泄漏；
4. **图片**：URL 固定 `/exam-images/<类型>/<学科>/<卷名>/images/<文件名>`（卷名去掉「（教师版）」），
   文件落盘到扁平目录 `output_docx_to_md/<类型>/<学科>/<卷名>/images/`；**docx 有图就不用 PDF 截图**；
5. **不复用旧的 app 路径**：英语不要再去调 `main.py` / `src/json_builder.py`（可以在新脚本里 import 它们的纯函数，
   但对外只有一个入口）；
6. **写 review**：凡是用「常规分摊」推分值、按图转录选项、清洗掉的残渣、疑似缺题缺答案，都写一行到 `.review.txt`。

## 4. 与题库的契约（**不能改**，前端/批改提示词依赖）

### 英语

| 板块（section.category） | section.questionType | 小题 questionType | 占位标签 |
| --- | --- | --- | --- |
| 完形填空 | `cloze` | `choice` | — |
| 语法填空 | `grammar` | `input` | `「<Input>` 系列，见下 |
| 选词填空 | `word-choice` | `input2` | — |
| 阅读 | `en-reading` | `choice` | — |
| 七选五 | `seven-choose-five` | `choice` | — |
| 阅读表达 | `reading-expression` | `text` | `<InputLong>` |
| 作文 | `en-writing` | `text` | `<InputLong>` |

- 英语选题字母答案 **必须大写**（`"C"`、`{"id":"C"}`）；语法填空/选词填空/阅读表达/作文的答案保留原文大小写；
- 完形填空 15 空：题干给 `(1)…(15)` 编号，答案按空号对；语法填空 10 空同理。

### 语文

| 板块（section.category） | section.questionType | 小题 questionType | 占位标签 |
| --- | --- | --- | --- |
| 多文本 | `chinese-reading` | `single` / `text` | 主观小问用 `<AnswerBox>` |
| 文言文 | `chinese-reading` | `single` / `text` | 同上 |
| 古诗 | `chinese-reading` | `single` / `text` | 同上 |
| 文学类 | `chinese-reading` | `single` / `text` | 同上 |
| 语用 | `chinese-reading` | `single` / `text` | 同上 |
| 名著 | `chinese-reading` | `single` / `text` | 同上 |
| 默写 | `chinese-dictation` | `input` | 每空一个 `<Input2>`（或现有 `<Blank>`，保持与存量一致） |
| 微写作 | `chinese-micro-writing` | `text` | `<InputLong>` |
| 作文 | `chinese-essay` | `text` | `<InputLong>` |

- 语文整卷满分 150：默写 8 分、微写作 10 分、作文 50 分是固定口径；
- 名著阅读单问卷可为一题，多小问按 `(1)(2)` 拆题，材料进题组 `content`。

## 5. 测试卷（三区一模）

```
gaozhong/一模/语文/2026北京{西城,海淀,丰台}高三一模语文（教师版）.docx
gaozhong/一模/英语/2026北京{西城,海淀,丰台}高三一模英语（教师版）.docx
# 语文对比用二模（可选）：gaozhong/二模/语文/2026北京{西城,海淀,丰台}高三二模语文（教师版）.docx
```

md 由 `batch_convert_docx_to_md.py -p <docx> -o output_docx_to_md/<类型>/<学科>` 生成（单文件模式）。

## 6. `--check` 断言（两个学科都要有）

1. `Σ section.score == paper.totalScore`，且 `Σ 小问 score == section.score`；
2. 选择题每题 `options` 为 4 项（英语七选五为 7 选项池 / 完形为 4 项）、`answer` 是大写字母且在选项内；
3. 材料（section.content）里**每个小问的设问只出现一次**，且小问 content 能在 section.content 里找到；
   **例外（2026-11-07 定）**：英语的选择题板块（完形填空 `cloze`、阅读 `en-reading`、七选五 `seven-choose-five`）
   按前端 `ReadingLayout`（左原文右题目）版式，题干只放 `questions[].content`、**不进 section.content**，
   此时断言改为「题干不得被抄进原文」；语文/其它学科的选题板块仍按第 3 条字面执行；
4. 非选择题 section 的占位标签数量 == 小问数量（语文默写：标签数 == 空数）；
5. 题干/材料开头不得是分区标题、卷首说明、纯 ①②③④ 行（复用 `qa_scaffold.py` 的 `W_STEM_RESIDUE` 口径）；
6. 引用的图片路径必须存在且能被 nginx 命中（`output_docx_to_md/.../images/` 扁平目录里真实存在）；
7. 语文：`默写 = 8`、`微写作 = 10`、`作文 = 50` 的口径允许缺失板块但不能串位。

## 7. 验收（Lead 会按这个查）

- `--check -T 一模` 对三卷、以及全类型目录退出码 0；
- `qa_scaffold.py --subject <学科> --type 一模 --check` → 0 错 0 警（若脚手架对新契约有误报，允许在 `qa_scaffold.py` 里补规则，
  但**必须说明改了哪条规则、为什么**）；
- 与**存量数据对比**（Lead 出对比表）：基因数（section 数）、题数、题型分布、答案覆盖率、总分；
- 不碰数据库（导入由 Lead 统一做）、不 commit。

## 8. 补充事实（Lead 实测，2026-11-07）

1. **英语存量 JSON 用的是 `section.article`**（不是 `content`），导入脚本 `import-english-json.ts` 读的是
   `sec.article || sec.content`；所以新版**统一写 `content`** 即可，导入端已经兼容（不要两处都写）。
   旧 JSON 里 9 个 section 的题数分布实测：完形 10 / 语法 10 / 阅读 14（A–D 四个 section）/ 七选五 5 /
   阅读表达 4 / 作文 1 = 44 题，总分 100；答案覆盖率 95%–98%（西城 42/44、海淀 43/44、丰台 43/44，
   缺的是**阅读表达最后 1 空 + 作文**这类主观题）。这些数字就是「新版要打平的基线」。
2. **语文存量只有二模**（96 卷，`output_json_chinese/二模/`，顶层键 `groups`，与生产库一致）；一模从未跑过，
   所以语文一模是「新链路首跑」，二模才是「新旧对比」的对象。存量二模指标：150 分 / 9 板块 /
   31–33 题 / 答案覆盖 97–100% / **有解析的只有 13 题**（语文教师版多数不给逐题解析）。
3. 英语三卷（西城/海淀/丰台 2026 一模）**在生产库里的标题不带学科后缀**（如 `2026北京西城高三一模`），
   用 `compare_paper_quality.py --db --subject 英语 --title "2026北京西城高三一模"` 取基线。
4. 统一对比工具（我已写好，只读）：`python3 compare_paper_quality.py --pair --subject 英语 \
      --title "2026北京西城高三一模" --json output_json_english/一模/2026北京西城高三一模英语.json`
   输出「总分 / 板块数 / 题数 / 有答案 / 有解析 / 答案覆盖率 / 板块明细 / 顶层键」。
