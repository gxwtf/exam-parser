#!/usr/bin/env python3
"""英语整卷解析（**成熟格式**：单文件、单次调用、自带 `--check`）→ `output_json_english/<类型>/<卷名>.json`。

链路：docx→MD（batch_convert_docx_to_md.py）→ **本脚本** → 题库导入（`import-english-json.ts`）。
本脚本取代旧的 app 链路（`batch_convert_md_to_json.py` / `main.py` / `src/json_builder.py`），
对外只有这一个入口；旧链路里能复用的纯函数（`split_paper` / `rewrite_images`）直接 import。

输出结构（与生物/政治/历史/地理同构，见 `MIGRATION-语文英语.md` 第 2 节）：

    {"paper": {"title","subject","grade","year","totalScore","duration","tags"},
     "sections": [
       {"type":"完形填空","questionType":"cloze","title":"一、完形填空","category":"完形填空",
        "score":15,"content":"短文（第 N 空处是 <ClozeBlank></ClozeBlank>）",
        "questions":[{"id":1,"questionType":"choice","content":"","options":[…A–D],"answer":"A",
                      "analysis":"","score":1.5}]},
       {"type":"语法填空","questionType":"grammar","title":"二、语法填空","category":"语法填空",
        "score":15,"content":"三篇短文（每空一个 <Input></Input>）",
        "questions":[{"id":11,"questionType":"input","content":"","options":[],
                      "answer":"calmly","analysis":"","score":1.5}]},
       {"type":"阅读","questionType":"en-reading","title":"三、阅读 A","category":"阅读","score":6,
        "content":"短文原文","questions":[…choice…]},
       {"type":"七选五","questionType":"seven-choose-five","title":"七、七选五","category":"七选五",
        "score":10,"content":"短文（每空一个 <Blank></Blank>）",
        "questions":[{"id":35,"questionType":"choice","options":[7 项 A–G],"answer":"C",…}]},
       {"type":"阅读表达","questionType":"reading-expression","title":"八、阅读表达","category":"阅读表达",
        "score":12,"content":"短文 + 各小问设问（每个设问后紧跟 <InputLong></InputLong>）",
        "questions":[{"id":40,"questionType":"text","content":"设问原文","answer":"…","score":2}]},
       {"type":"作文","questionType":"en-writing","title":"九、作文","category":"作文","score":20,
        "content":"题目 + <InputLong></InputLong>",
        "questions":[{"id":44,"questionType":"text","content":"题目原文","answer":"范文/评分要点","score":20}]}
     ]}

关键约定（前端组件 `components/article/english-reading.tsx` + `mdx-components.tsx` 依赖，别改）：

- **材料放在 `section.content`**（旧英语 JSON 用的是 `section.article`，导入端 `sec.article || sec.content`
  两处都认，本脚本**只写 `content`**，不要两处都写）；
- 题面上的空按板块换成既有标签，前端才对得上交互：
  - 完形填空 → `<ClozeBlank></ClozeBlank>`（短文里第 1–N 空，按出现顺序与 questions 一一对应）
  - 语法填空 → `<Input></Input>`（一个空一个单词/词形）
  - 选词填空 → `<Input2></Input2>`
  - 七选五 → `<Blank></Blank>`（短文里 5 个空），**每题 options 都是同一份 7 项池（A–G）**
  - 阅读表达 / 作文 → `<InputLong></InputLong>`（每小问一个）
- 选择题（`choice`）`answer` 与 `options[].id` **一律大写**（出口调 `normalize_choice_case_in_paper`）；
  语法填空/选词填空/阅读表达/作文的答案**保留原文大小写**；一个空有多个可接受答案用 `##` 分隔
  （如 `where##when`）；
- 一个「板块/篇章」= 一个 section：完形 1、语法 1、**阅读按篇分开（A–D 四篇 → 四个 section）**、
  七选五 1、阅读表达 1、作文 1（北京卷共 9 个 section / 44 题 / 100 分）；
- `analysis` 只填**源卷教师版自带的解析**（有的区给 `【N题详解】`）；源卷没有就留空，
  **不生成解析**（旧 app 链路的中文解析是模型生成的，不是卷面内容）。

`--check` 对**英语选择题板块**（cloze / en-reading / seven-choose-five）豁免「设问要在
`section.content` 里找得到」这条断言（MIGRATION-语文英语.md §6.3 例外条款）：这三类板块走
「左原文右题目」版式，题干只在 `questions[].content`；相应地改成断言「题干不得被抄进原文」。

用法：
    python3 convert_english_md.py -p <md 文件> [-T 一模] [-o <输出根>] [--force]
    python3 convert_english_md.py -T 一模                    # 批量（单进程）
    python3 convert_english_md.py --check -T 一模 [--report x.md]
    python3 convert_english_md.py --check -p <json 路径>     # 单卷自查
"""
import argparse
import json
import re
import time
from pathlib import Path

from convert_chinese_md import rewrite_images, split_paper, log
from paths import DATA_ROOT, GAOZHONG, OUTPUT_DOCX_TO_MD
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "英语"
OUT_ROOT = DATA_ROOT / "output_json_english"
OUT = {"root": OUT_ROOT}

PAPER_TOTAL = {"北京": 100}
BANDS = ["完形填空", "语法填空", "选词填空", "阅读", "七选五", "阅读表达", "作文"]

# 板块 → (section.questionType, 小题 questionType, 占位标签)
BAND_TYPE = {
    "完形填空": ("cloze", "choice", "<ClozeBlank></ClozeBlank>"),
    "语法填空": ("grammar", "input", "<Input></Input>"),
    "选词填空": ("word-choice", "input2", "<Input2></Input2>"),
    "阅读": ("en-reading", "choice", ""),
    "七选五": ("seven-choose-five", "choice", "<Blank></Blank>"),
    "阅读表达": ("reading-expression", "text", "<InputLong></InputLong>"),
    "作文": ("en-writing", "text", "<InputLong></InputLong>"),
}

# ──────────────────────────────────────────────────────────────────
# 正则
# ──────────────────────────────────────────────────────────────────

IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
EXAM_IMG = re.compile(r"/exam-images/([^\"'\s)]+)")
# 分区 / 小节标题（允许被 ** 包裹、允许前缀残留「第一部分：」这种）
PART = re.compile(r"(?m)^[ \t]*第([一二三1-3])部分")
SEG = re.compile(r"(?m)^[ \t]*第([一二1-2])节")
# 篇标记 A/B/C/D（独立成行）
PASSAGE = re.compile(r"(?m)^[ \t]*(?:#+[ \t]*)?([A-D])[ \t]*$")
# 题号行
QNO = re.compile(r"(?m)^[ \t*]*(\d{1,2})[ \t]*[.．、][ \t]*")
# 完形选项行：`1.A. read B. focus C. comment D. impression`
CLOZE_OPT = re.compile(
    r"(?m)^[ \t*]*(\d{1,2})[ \t]*[.．][ \t]*A[ \t]*[.．][ \t]*(.+?)[ \t]+B[ \t]*[.．][ \t]*(.+?)"
    r"[ \t]+C[ \t]*[.．][ \t]*(.+?)[ \t]+D[ \t]*[.．][ \t]*(.+?)[ \t]*$")
# 选项标记（行内也可能两个）
OPT = re.compile(r"(?:^|(?<=[\s\u3000*]))([A-G])[ \t]*[.．][ \t]*")
# 题干残留：分区标题 / 卷首说明 / 纯 ①②③④ 行（对齐 qa_scaffold 的 W_STEM_RESIDUE 口径）
STEM_RESIDUE = re.compile(r"^\s*(?:[①②③④⑤⑥]{1,6}|(?:[一二三四五六]、)?(?:选择题|非选择题)\**|"
                          r"\*\*[^*\n]+(?:选择题|非选择题)[^*\n]+\*\*)\s*(?:\n|$)")
STEM_NOTICE = re.compile(r"(考生须知|本试卷分第|本部分共\s*\d+\s*题|下列各小题均有四个选项|"
                         r"答题前[，,]?\s*(?:请|考生)|考试时间\s*\d+\s*分钟\s*满分|"
                         r"本试卷共\d+页|考生务必将答案)")
# 作答提示行（阅读表达/作文的横线占位、答题卡提示）
ANSWER_LINE = re.compile(r"^\s*(?:\\?[_*—–\-]{3,}[\s\\_*—–\-]*)+$")
INSTRUCTION = re.compile(r"^\s*(?:阅读下列短文|阅读下面短文|阅读短文|根据短文内容|阅读下列材料|"
                         r"根据题目要求|请在各题目的答题区域内作答)")
# 多解答案分隔
MULTI = re.compile(r"\s*/\s*|、")


# ──────────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────────

def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _norm_cmp(s: str) -> str:
    return re.sub(r"[\s\u3000，。；：、,.;:!?！？（）()【】\[\]\"'“”‘’…—\-－]", "", s or "")


def _num(x: float):
    """整数值输出 int（15.0 → 15），否则保留一位小数（1.5）。"""
    x = float(x)
    return int(x) if abs(x - round(x)) < 1e-9 else round(x, 2)


def prep(text: str, review: list[str], label: str) -> str:
    """正文预处理：去广告图、去 ** 粗体、去转义、归一空标记、去残渣行。"""
    if not text:
        return text
    # 学科网之类的广告小图（27×24 之类）是页面水印，不是题面图
    for m in IMG_REF.finditer(text):
        review.append(f"{label}：去掉页面水印图 {m.group(1)}（非题面图）")
    text = IMG_REF.sub("", text)
    text = text.replace("**", "")
    text = re.sub(r"<su[bp]>([^<]*)</su[bp]>", r"\1", text)
    text = text.replace("\\_", "_").replace("\\.", ".").replace("\\-", "-").replace("\\ ", " ")
    text = re.sub(r"(?<=_)\*+|\*+(?=_)|(?<=_)\*+", "", text)      # 去掉与下划线相邻的 *
    text = re.sub(r"_{2,}", "____", text)                        # 长下划线统一
    text = re.sub(r"____\s*(\d{1,2})\s*____", r"__\1__", text)   # ____11____ → __11__
    text = re.sub(r"[ \t\u3000]+", " ", text)
    lines = []
    for ln in text.split("\n"):
        s = ln.strip()
        if ANSWER_LINE.match(s):                 # 阅读表达的作答横线
            lines.append("")
            continue
        lines.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def paras(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n[ \t]*\n", (text or "").replace("\r", "")) if p.strip()]


def band_text(body: str, part_no: int, seg_no: int | None = None) -> str:
    """取「第 N 部分」/「其中第 M 节」的正文；找不到返回空串。"""
    pm = list(PART.finditer(body))
    hi_num = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
    starts = [(hi_num.get(m.group(1)), m) for m in pm]
    cur = next((m for n, m in starts if n == part_no), None)
    if cur is None:
        return ""
    nxt = next((m.start() for n, m in starts if n > part_no), len(body))
    chunk = _drop_header_tail(body[cur.end():nxt])
    if seg_no is None:
        return chunk
    sm = list(SEG.finditer(chunk))
    ok = [(hi_num.get(m.group(1)), m) for m in sm]
    cur2 = next((m for n, m in ok if n == seg_no), None)
    if cur2 is None:
        return chunk
    nxt2 = next((m.start() for n, m in ok if n > seg_no), len(chunk))
    return _drop_header_tail(chunk[cur2.end():nxt2])


HEAD_TAIL = re.compile(r"^[ \t]*[（(][^）)\n]{0,40}(?:小题|分)[^）)\n]{0,30}[）)]")


def _drop_header_tail(chunk: str) -> str:
    """切掉「第一节（共10小题；每小题1.5分，共15分）」剩下的括号说明那一截。"""
    head, sep, rest = chunk.partition("\n")
    if HEAD_TAIL.match(head) or re.match(r"^[ \t]*[（(]?", head) and ("小题" in head or "分" in head):
        return rest
    return chunk


def band_header(body: str, part_no: int, seg_no: int | None = None) -> str:
    """该部分/该节的**标题行原文**（「第一节（共4小题；第40、41题各2分…）」）——解析分值用它。"""
    pm = list(PART.finditer(body))
    hi_num = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
    starts = [(hi_num.get(m.group(1)), m) for m in pm]
    cur = next((m for n, m in starts if n == part_no), None)
    if cur is None:
        return ""
    nxt = next((m.start() for n, m in starts if n > part_no), len(body))
    chunk = body[cur.end():nxt]
    line1 = chunk.split("\n")[0] if chunk else ""
    if seg_no is None:
        return line1
    sm = [(hi_num.get(m.group(1)), m) for m in SEG.finditer(chunk)]
    cur2 = next((m for n, m in sm if n == seg_no), None)
    if cur2 is None:
        return line1
    nxt2 = next((m.start() for n, m in sm if n > seg_no), len(chunk))
    sub = chunk[cur2.start():nxt2]
    return sub.split("\n")[0] if sub else ""


def drop_instructions(text: str) -> str:
    """砍掉每个小节开头的作答说明段（「阅读下列短文…将该项涂黑。」之类，丰台每篇前都重复一遍）。"""
    out = []
    for p in paras(text):
        if INSTRUCTION.match(p):
            continue
        out.append(p)
    return "\n\n".join(out)


def norm_blanks(text: str) -> str:
    """把各种空写法统一成 `__N__`（能认出编号）；返回处理后的文本。"""
    return re.sub(r"__\s*(\d{1,2})\s*__", r"__\1__", text)


def iter_blanks(text: str, expect: list[int]) -> list[tuple[int, int, int]]:
    """在 text 里定位 expect 这些空号，返回 [(序号, 起始, 结束)]（按位置升序）。

    先认 `__N__` 写法；认不全时退化为「按顺序找裸题号」（西城/海淀那种把空写成裸数字的卷子）。
    """
    hits = [(m.start(), m.end(), int(m.group(1))) for m in re.finditer(r"__(\d{1,2})__", text)]
    got = {no for _s, _e, no in hits}
    if set(expect) <= got and hits:
        ordered = sorted(hits)
        return [(i, s, e) for i, (s, e, _no) in enumerate(ordered)]
    # 裸题号：按 expect 顺序逐个找「独立的数字」
    out, pos = [], 0
    for i, no in enumerate(expect):
        m = re.compile(r"(?<![\w.\-/])%d(?![\w.\-/%%)])" % no).search(text, pos)
        if not m:
            return []
        out.append((i, m.start(), m.end()))
        pos = m.end()
    return out


def apply_blanks(text: str, expect: list[int], tag: str) -> tuple[str, list[int]]:
    """把 expect 这些空依次换成 tag；返回 (新文本, 实际替换掉的空号顺序)。"""
    hits = iter_blanks(text, expect)
    if not hits:
        return text, []
    pieces, last, used = [], 0, []
    for i, s, e in hits:
        pieces.append(text[last:s])
        pieces.append(tag)
        last = e
        used.append(expect[i] if i < len(expect) else i + 1)
    pieces.append(text[last:])
    return "".join(pieces), used


def separate_images(text: str) -> str:
    """图片独占一段。"""
    if not text or "![" not in text:
        return text
    out = []
    for p in paras(text):
        if IMG_REF.fullmatch(p.strip()) or "![" not in p:
            out.append(p)
            continue
        last = 0
        for m in IMG_REF.finditer(p):
            pre = p[last:m.start()].strip()
            if pre:
                out.append(pre)
            out.append(m.group(0))
            last = m.end()
        tail = p[last:].strip()
        if tail:
            out.append(tail)
    return "\n\n".join(out)


# ──────────────────────────────────────────────────────────────────
# 答案区解析
# ──────────────────────────────────────────────────────────────────

def parse_key(ans: str) -> tuple[dict[int, str], dict[int, str]]:
    """参考答案区 → ({题号: 答案}, {题号: 解析})。

    兼容两种卷面写法：
    - 西城/海淀：`1. A 2. B …` / `11. calmly 12. where/ when …` / `40. After dinner, …`；
    - 丰台（解析版）：`【答案】1. C 2. B …` + 后面的 `【N题详解】…` 整段解析。
    """
    key: dict[int, str] = {}
    ana: dict[int, str] = {}

    # ① 解析版：`【N题详解】` 之后到下一个 `【…】` 之间
    for m in re.finditer(r"【\s*(\d{1,2})\s*题详解】", ans):
        nxt = re.search(r"【\s*\d{1,2}\s*题详解】|【点睛】|^第[一二三]部分", ans[m.end():], re.M)
        seg = ans[m.end():m.end() + (nxt.start() if nxt else len(ans) - m.end())].strip()
        if seg:
            ana[int(m.group(1))] = seg

    # ② 「N. 答案」清单（一行可能多个；答案可能是字母、单词、短语）
    inline = re.compile(r"(?<![\w.])(\d{1,2})\s*[.．]\s*(.*?)(?=\s{1,}\d{1,2}\s*[.．]\s|\s*$)")
    for line in ans.split("\n"):
        s0 = re.sub(r"^[ \t*]*【\s*答案】[ \t]*", "", line).strip()
        if not re.match(r"^\d{1,2}\s*[.．]", s0):
            continue
        for mm in inline.finditer(s0):
            no, val = int(mm.group(1)), mm.group(2).strip()
            if 1 <= no <= 39 and val:
                key.setdefault(no, val)

    # ③ 主观题（40 以后）按「块」取：从 `40.` 到下一个题号/【…】/分区标题
    subj_marks = [(m.start(), m.end(), int(m.group(1)))
                  for m in re.finditer(r"(?m)^[ \t*]*(\d{1,2})[ \t]*[.．]\s*", ans)
                  if int(m.group(1)) >= 40]
    for i, (s0, e0, no) in enumerate(subj_marks):
        end = subj_marks[i + 1][0] if i + 1 < len(subj_marks) else len(ans)
        block = ans[e0:end]
        cut = re.search(r"(?m)^[ \t*]*(?:【[^】]*】|第[一二三]部分)", block)
        if cut:
            block = block[:cut.start()]
        block = "\n".join(ln for ln in block.split("\n")
                          if not re.fullmatch(r"[ \t*]*\d{1,2}[ \t]*[.．][ \t*]*", ln))
        block = re.sub(r"\n{3,}", "\n\n", block).strip()
        if block:
            key.setdefault(no, block)
    return key, ana


OMIT = re.compile(r"^(?:略|答案略|（略）|\(略\)|omitted|no answer\.?)$", re.I)


def clean_key_answer(no: int, raw: str, band: str) -> str:
    """答案出口清洗：选择题只留大写字母；填空/主观题保留原文大小写，多解用 ## 连。"""
    v = (raw or "").strip()
    if not v or OMIT.match(v):
        return ""
    if band in ("完形填空", "阅读", "七选五"):
        m = re.match(r"[A-G]", v)
        return m.group(0).upper() if m else ""
    if re.fullmatch(r"[A-G]", v):            # 语填里单独一个字母（如 "A"）保持原样
        return v
    v = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", v)
    v = re.sub(r"[ \t]+", " ", v).strip()
    if band in ("语法填空", "选词填空"):
        v = re.sub(r"^(?:答案[:：]\s*)", "", v)
        parts = [p.strip() for p in re.split(r"\s*/\s*", v) if p.strip()]
        return "##".join(parts) if parts else v
    return v


def reading_expression_answers(ans: str, nos: list[int]) -> dict[int, str]:
    """阅读表达（40–43）的答案：在答案区的「第三部分 → 第一节」区间里按**位置**对到设问号上。

    卷面写法差异很大（西城/海淀逐题一行、丰台是 `【答案】40. …` 解析版，海淀还把 42 题写成了
    Word 自动编号的 `1.`），按位置对齐比按题号可靠。
    """
    p3 = [m for m in re.finditer(r"(?m)^[ \t*]*第[三3]部分", ans)]
    start = p3[-1].end() if p3 else 0
    seg = ans[start:]
    m1 = re.search(r"(?m)^[ \t*]*第?[一1]节", seg)
    if m1:
        seg = seg[m1.end():]
    m2 = re.search(r"(?m)^[ \t*]*第?[二2]节", seg)
    if m2:
        seg = seg[:m2.start()]
    seg = re.sub(r"【\s*答案】\s*", "", seg)
    marks = [(m.start(), m.end()) for m in re.finditer(r"(?m)^[ \t*]*\d{1,2}\s*[.．]\s*", seg)]
    out: dict[int, str] = {}
    for i, (s0, e0) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(seg)
        if i >= len(nos):
            break
        block = seg[e0:end]
        cut = re.search(r"(?m)^[ \t*]*(?:【\s*导语】|【\s*详解】|【\s*点睛】|【\s*\d+\s*题详解】)", block)
        if cut:
            block = block[:cut.start()]
        block = "\n".join(ln for ln in block.split("\n")
                          if not re.fullmatch(r"[ \t*]*\d{1,2}[ \t]*[.．][ \t*]*", ln))
        block = re.sub(r"\n{3,}", "\n\n", block).strip()
        if block:
            out[nos[i]] = block
    return out


def parse_writing_model(ans: str) -> str:
    """作文范文：`One possible version:` / `参考范文` 之后到下一个大题块之前。"""
    m = re.search(r"(?i)(one possible version|possible version|参考范文|范文|one possible answer)", ans)
    if not m:
        return ""
    tail = ans[m.start():]                 # 「One possible version:」这类卷面标签原样保留
    cut = re.search(r"(?m)^\s*(?:【\s*导语】|【\s*详解】|第[一二三]部分|【\s*\d+\s*题详解】)", tail)
    body = tail[:cut.start()] if cut else tail
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def writing_guide(ans: str, no: int) -> str:
    """作文的教师版「写作指导」（【导语】/【详解】/【点睛】），源卷没有就给空。"""
    pat = re.compile(r"(?m)^[ \t*]*%d\s*[.．]\s*" % no)
    m = pat.search(ans)
    seg = ans[m.start():] if m else ans
    i = seg.find("【导语】")
    if i < 0:
        i = seg.find("【详解】")
    if i < 0:
        return ""
    return re.sub(r"\n{3,}", "\n\n", seg[i:]).strip()


def writing_model_from_block(ans: str, no: int) -> str:
    """在答案区里找作文范文。

    三种卷面写法都要吃得下：
    1. `44. 【答案】One possible version: …`（丰台）；
    2. 答案区最后一个「第二节」标题之后直接给范文、不给题号（海淀）；
    3. 只给「（请务必将作文写在答题卡指定区域内）」——没有范文 → 返回空。
    """
    pat = re.compile(r"(?m)^[ \t*]*%d\s*[.．]\s*" % no)
    m = pat.search(ans)
    if m:
        nxt = re.search(r"(?m)^[ \t*]*\d{1,2}\s*[.．]\s|^第[一二三]部分", ans[m.end():])
        block = ans[m.start():m.end() + (nxt.start() if nxt else len(ans) - m.end())]
        cand = parse_writing_model(block)
        if cand:
            return cand
    # 兜底：答案区「最后一个第三部分」之后、第一处「第二节」之后（海淀那种不给题号的写法）
    p3 = [mm for mm in re.finditer(r"(?m)^[ \t*]*第[三3]部分", ans)]
    seg = ans[p3[-1].end():] if p3 else ""
    m2 = re.search(r"(?m)^[ \t*]*第?[二2]节[^\n]*$", seg)
    if m2:
        tail = seg[m2.end():]
        cut = re.search(r"(?m)^[ \t*]*(?:【\s*导语】|【\s*详解】|【\s*点睛】|【\s*\d+\s*题详解】)",
                        tail)
        cand = (tail[:cut.start()] if cut else tail).strip()
        cand = re.sub(r"[（(]\s*请务必将作文写在答题卡指定区域内\s*[）)]", "", cand).strip()
        if len(cand) > 80 and re.search(r"[A-Za-z]{3,}", cand) and "第[一二三]部分" not in cand:
            return cand
    return ""


# ──────────────────────────────────────────────────────────────────
# 各板块解析
# ──────────────────────────────────────────────────────────────────

def parse_cloze(seg: str, review: list[str], label: str) -> dict:
    """完形填空：短文 + `N. A. x B. y C. z D. w` 选项行。"""
    opt_marks = list(CLOZE_OPT.finditer(seg))
    if not opt_marks:
        review.append(f"{label}：完形填空没找到 4 选项行，需人工核对")
        return {"content": "", "questions": []}
    body = seg[:opt_marks[0].start()]
    text = drop_instructions(body)
    nums = [int(m.group(1)) for m in opt_marks]
    text_n, used = apply_blanks(text, nums, BAND_TYPE["完形填空"][2])
    if len(used) != len(nums):
        review.append(f"{label}：完形填空短文里只定位到 {len(used)} 个空（选项行 {len(nums)} 个），"
                      f"需人工核对空位")
    qs = []
    for m in opt_marks:
        no = int(m.group(1))
        opts = [{"id": k, "label": m.group(i).strip()} for i, k in zip(range(2, 6), "ABCD")]
        qs.append({"id": no, "questionType": "choice", "content": "", "options": opts,
                   "answer": "", "analysis": "", "score": 1.5})
    if [q["id"] for q in qs] != list(range(1, len(qs) + 1)):
        review.append(f"{label}：完形填空空号不连续 {[q['id'] for q in qs]}")
    return {"content": text_n, "questions": qs}


def parse_grammar(seg: str, review: list[str], label: str, band: str,
                  expect: list[int] | None = None,
                  pdf_ctx: dict[int, tuple[str, str]] | None = None) -> dict:
    """语法填空 / 选词填空：若干篇短文（A/B/C），每空一个标签。"""
    text = drop_instructions(seg)
    # 篇标记单独成行：A / B / C
    parts, cur = [], []
    for p in paras(text):
        if re.fullmatch(r"[A-D]", p):
            if cur:
                parts.append(cur)
            cur = ["**" + p + "**"]
            continue
        cur.append(p)
    if cur:
        parts.append(cur)
    joined = "\n\n".join("\n\n".join(x) for x in parts).strip()
    joined = norm_blanks(joined)
    found = sorted({int(m.group(1)) for m in re.finditer(r"__(\d{1,2})__", joined)})
    expect = found or expect or list(range(11, 21))
    text_n, used = apply_blanks(joined, expect, BAND_TYPE[band][2])
    if not used and pdf_ctx:                 # md 把空标记丢了 → 用教师版 PDF 的上下文补回
        text_n, used = apply_blanks_by_context(joined, pdf_ctx, BAND_TYPE[band][2], review, label)
        if used:
            review.append(f"{label}：md 里{band}的空标记丢失，已用教师版 PDF 的上下文补回 "
                          f"{len(used)} 个空（{min(used)}–{max(used)}）")
    if not used:
        review.append(f"{label}：{band}一个空位都没定位到，需人工核对")
    qs = [{"id": n, "questionType": BAND_TYPE[band][1], "content": "", "options": [],
           "answer": "", "analysis": "", "score": 1.5} for n in expect]
    return {"content": text_n, "questions": qs}


def apply_blanks_by_context(text: str, ctx: dict[int, tuple[str, str]], tag: str,
                            review: list[str], label: str) -> tuple[str, list[int]]:
    """按 PDF 里「空前 N 个词 / 空后 N 个词」把标签插回 md 正文（md 丢了空标记时兜底）。"""
    out, used = text, []
    for no in sorted(ctx):
        before, after = ctx[no]
        if not after:
            continue
        pat = re.compile(r"(.{0,120}?)" + re.escape(after), re.S)
        best = None
        for m in pat.finditer(out):
            if not before or _norm_cmp(before)[-12:] in _norm_cmp(m.group(1))[-40:]:
                best = m
                break
        if best is None:
            for m in pat.finditer(out):
                best = m
                break
        if best is None:
            review.append(f"{label}：第{no}空在 md 正文里找不到锚点「{after[:24]}」，未补占位符")
            continue
        pos = best.start(1) + len(best.group(1))
        nxt_ch = out[pos:pos + 1]
        sep = " " if nxt_ch and (nxt_ch.isalnum() or nxt_ch == "(") else ""
        out = out[:pos] + tag + sep + out[pos:]
        used.append(no)
    return out, used


def parse_reading(p2seg1: str, review: list[str], label: str) -> list[dict]:
    """阅读第一节：按篇切（A–D），每篇一个 section。"""
    text = drop_instructions(p2seg1)
    marks = [(m.start(), m.group(1)) for m in PASSAGE.finditer(text)]
    if not marks:
        review.append(f"{label}：阅读部分没找到 A–D 的篇标记，需人工核对")
        return []
    out = []
    for i, (s, tag) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chunk = text[s:end]
        chunk = PASSAGE.sub("", chunk, count=1).strip()
        qmarks = list(QNO.finditer(chunk))
        if not qmarks:
            review.append(f"{label}：阅读第 {tag} 篇没切到题目")
            continue
        passage = chunk[:qmarks[0].start()].strip()
        qs = []
        for j, m in enumerate(qmarks):
            qend = qmarks[j + 1].start() if j + 1 < len(qmarks) else len(chunk)
            block = chunk[m.end():qend].strip()
            qs.append({"no": int(m.group(1)), "block": block})
        questions = []
        for q in qs:
            stem, opts = split_stem_options(q["block"])
            questions.append({"id": q["no"], "questionType": "choice", "content": stem,
                              "options": opts, "answer": "", "analysis": "", "score": 2})
        out.append({"tag": tag, "content": passage, "questions": questions})
    return out


def split_stem_options(block: str) -> tuple[str, list[dict]]:
    """把「题干 + A./B./C./D.」拆开（选项可能一行两项）。"""
    text = re.sub(r"[ \t]+", " ", block).strip()
    marks = {}
    for m in OPT.finditer(text):
        marks.setdefault(m.group(1), m)
    letters = [k for k in "ABCD" if k in marks]
    if len(letters) < 2:
        return text, []
    first = marks[letters[0]].start(1)
    stem = text[:first].strip()
    opts = []
    for i, k in enumerate(letters):
        s = marks[k].end()
        e = marks[letters[i + 1]].start(1) if i + 1 < len(letters) else len(text)
        opts.append({"id": k, "label": text[s:e].strip()})
    if [o["id"] for o in opts] != ["A", "B", "C", "D"]:
        opts = [o for o in opts if o["label"]]
    return stem, opts


def parse_seven(seg: str, review: list[str], label: str) -> dict:
    """七选五：短文（35–39 空）+ A–G（或 1–7）七选项池。"""
    text = drop_instructions(seg)
    # 选项池：从第一行「A.」/「1.」成组开始
    pool, body_end = [], len(text)
    lines = text.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^[ \t]*[A-G][ \t]*[.．][ \t]*\S", ln) or \
                (re.match(r"^[ \t]*1[ \t]*[.．][ \t]*\S", ln) and i > 3):
            idx = i
            break
    if idx is None:
        review.append(f"{label}：七选五没找到选项池")
        return {"content": text, "questions": []}
    body_end = len("\n".join(lines[:idx]))
    body = "\n".join(lines[:idx]).strip()
    tail = "\n".join(lines[idx:])
    # 选项可能是「A. xxx」也可能被写成一行的多项
    letters = "ABCDEFG"
    marks = {}
    for m in OPT.finditer(tail):
        marks.setdefault(m.group(1), m)
        if len(marks) >= 7:
            break
    if len(marks) < 7:                        # 数字编号 1–7 ↔ A–G
        num = {}
        for m in re.finditer(r"(?m)^[ \t]*(\d)[ \t]*[.．][ \t]*([^\n]+)$", tail):
            num[int(m.group(1))] = m.group(2).strip()
        if len(num) == 7:
            pool = [{"id": letters[k - 1], "label": num[k]} for k in range(1, 8)]
    if not pool:
        ks = [k for k in letters if k in marks]
        for i, k in enumerate(ks):
            s = marks[k].end()
            e = marks[ks[i + 1]].start(1) if i + 1 < len(ks) else len(tail)
            pool.append({"id": k, "label": tail[s:e].strip()})
    blanks = sorted({int(m.group(1)) for m in re.finditer(r"__(\d{1,2})__", norm_blanks(body))})
    expect = blanks or list(range(35, 40))
    body_n, used = apply_blanks(norm_blanks(body), expect, BAND_TYPE["七选五"][2])
    if len(used) != len(expect):
        review.append(f"{label}：七选五短文只定位到 {len(used)}/{len(expect)} 个空，需人工核对")
    qs = [{"id": n, "questionType": "choice", "content": "",
           "options": [dict(o) for o in pool], "answer": "", "analysis": "", "score": 2}
          for n in expect]
    if len(pool) != 7:
        review.append(f"{label}：七选五选项池只有 {len(pool)} 项（应为 7）")
    return {"content": body_n, "questions": qs}


def parse_reading_expression(seg: str, review: list[str], label: str) -> dict:
    """阅读表达：短文 + 40–43 四个设问（各跟一个 <InputLong>）。"""
    text = drop_instructions(seg)
    text = re.sub(r"(?m)^[ \t]*(?:➤|●|◆|■)[ \t]*", "", text)
    qmarks = list(QNO.finditer(text))
    if not qmarks:
        review.append(f"{label}：阅读表达没切到设问")
        return {"content": text, "questions": []}
    passage = text[:qmarks[0].start()].strip()
    asks, nos = [], []
    for i, m in enumerate(qmarks):
        end = qmarks[i + 1].start() if i + 1 < len(qmarks) else len(text)
        block = text[m.end():end].strip()
        asks.append(block)
        nos.append(int(m.group(1)))
    parts = [passage]
    qs = []
    for no, ask in zip(nos, asks):
        ask_clean = re.sub(r"\n{2,}", "\n\n", ask).strip()
        parts.append(ask_clean + BAND_TYPE["阅读表达"][2])
        qs.append({"id": no, "questionType": "text", "content": ask_clean, "options": [],
                   "answer": "", "analysis": "", "score": 3})
    return {"content": "\n\n".join(p for p in parts if p), "questions": qs}


def parse_writing(seg: str, review: list[str], label: str) -> dict:
    """作文：题目 + 提纲 + <InputLong>。"""
    text = drop_instructions(seg)
    text = re.sub(r"[（(]\s*请务必将作文写在答题卡指定区域内\s*[）)]", "", text)
    text = re.sub(r"(?m)^[ \t]*[_—\-]{6,}[ \t]*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    m = re.match(r"^[ \t*]*(\d{1,2})[ \t]*[.．、][ \t]*", text)
    no = int(m.group(1)) if (m and int(m.group(1)) >= 40) else 44
    body = text[m.end():].strip() if (m and int(m.group(1)) >= 40) else text
    keep = [p for p in paras(body) if not re.fullmatch(r"[_—\-\s]*", p)]
    body = "\n\n".join(keep)
    if not body.strip():
        review.append(f"{label}：作文题目是空的，需人工核对")
    return {"content": body + "\n\n" + BAND_TYPE["作文"][2],
            "questions": [{"id": no, "questionType": "text", "content": body, "options": [],
                           "answer": "", "analysis": "", "score": 20}]}


def unit_score(header: str, default: float) -> float:
    m = re.search(r"每小题\s*(\d+(?:\.\d+)?)\s*分", header or "")
    return float(m.group(1)) if m else default


def re_scores(header: str) -> dict[int, float]:
    """阅读表达各小问分值：`第40、41题各2分，第42题3分，第43题5分`。"""
    out: dict[int, float] = {}
    for m in re.finditer(r"第\s*([\d、,，]+?)\s*题各\s*(\d+(?:\.\d+)?)\s*分", header or ""):
        for n in re.findall(r"\d+", m.group(1)):
            out[int(n)] = float(m.group(2))
    for m in re.finditer(r"第\s*(\d+)\s*题\s*(\d+(?:\.\d+)?)\s*分", header or ""):
        out.setdefault(int(m.group(1)), float(m.group(2)))
    return out


def section_score(header: str, default: float) -> float:
    m = re.search(r"共\s*(\d+)\s*分", header or "")
    return float(m.group(1)) if m else default


# ──────────────────────────────────────────────────────────────────
# PDF 兜底：从教师版 PDF 的文本层取每个空的上下文
# ──────────────────────────────────────────────────────────────────

def pdf_blank_context(text: str, lo: int, hi: int, n_before: int = 3, n_after: int = 2) -> dict:
    """在一段 PDF 文本里找 lo..hi 这些空号，返回 {空号: (前文, 后文)}。"""
    out: dict[int, tuple[str, str]] = {}
    pos = 0
    for no in range(lo, hi + 1):
        m = re.compile(r"(?<![\w.])%d(?![\w.])" % no).search(text, pos)
        if not m:
            continue
        pos = m.end()
        before = text[max(0, m.start() - 90):m.start()]
        after = text[m.end():m.end() + 90]
        bw = re.findall(r"[^\s]{1,30}", before)[-n_before:]
        aw = re.findall(r"[^\s]{1,30}", after)[:n_after]
        out[no] = (" ".join(bw), " ".join(aw))
    return out


def pdf_text(pdf: Path) -> str:
    import subprocess
    try:
        r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                           capture_output=True, text=True, timeout=600)
        return r.stdout or ""
    except Exception:
        return ""


def grammar_ctx_from_pdf(pdf: Path | None, lo: int, hi: int, review: list[str],
                         label: str) -> dict:
    """从 PDF 的「第二节 语法填空」区间里取空上下文（md 把空标记转丢时用）。"""
    if pdf is None or not pdf.exists():
        return {}
    t = pdf_text(pdf)
    if not t:
        return {}
    i = t.find("第二节")
    j = t.find("第二部分", i + 1)
    seg = t[i:j] if i >= 0 and j > i else t
    return pdf_blank_context(seg, lo, hi)


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────

TITLE_SUFFIX = re.compile(r"(一模|二模|上期末|下期末|期中|期末|真题)\s*英\s*语")


def paper_title(stem: str) -> str:
    """卷名：去「（教师版）/含答案」+ 去空格，**并去掉学科名「英语」**。

    英语题库里 442 卷全部是 `2026北京西城高三一模` 这种不带学科后缀的名字，
    `import-english-json.ts` 按 title 匹配，带后缀会匹配不上（语文/历史/地理/政治保持带后缀）。
    """
    t = re.sub(r"（教师版）|\(教师版\)|含答案", "", stem).strip()
    t = re.sub(r"\s+", "", t)
    t2 = TITLE_SUFFIX.sub(r"\1", t)
    if t2 == t:
        t2 = re.sub(r"英\s*语$", "", t)
    return t2


def process(md: Path, type_dir: str, args) -> str:
    title = paper_title(md.stem)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{dst.name}"

    review: list[str] = []
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    if not ans or len(ans.strip()) < 50:
        raise RuntimeError("该卷没有参考答案区，无法解析")
    body = prep(body, review, "正文")
    ans = prep(ans, review, "答案区")

    def rewrite(t: str) -> str:
        return rewrite_images(t, md.parent, title, type_dir, review, SUBJECT)

    pdf = None
    for cand in (GAOZHONG / type_dir / SUBJECT / f"{md.parent.name}.pdf",
                 GAOZHONG / type_dir / SUBJECT / f"{title}（教师版）.pdf",
                 GAOZHONG / type_dir / SUBJECT / f"{title}.pdf"):
        if cand.exists():
            pdf = cand
            break

    key, analysis = parse_key(ans)
    if not key:
        review.append("参考答案区一个答案都没解析出来，需人工核对")

    p1 = band_text(body, 1)
    p2 = band_text(body, 2)
    p3 = band_text(body, 3)
    if not p2:
        raise RuntimeError("正文里没找到「第二部分 阅读理解」")
    h_cloze = band_text(body, 1, 1)
    h_gram = band_text(body, 1, 2)
    h_read = band_text(body, 2, 1)
    h_seven = band_text(body, 2, 2)
    h_re = band_text(body, 3, 1)
    h_wr = band_text(body, 3, 2)
    # 分值只在标题行里（「第40、41题各2分」），用标题行原文解析
    hdr_cloze, hdr_gram = band_header(body, 1, 1), band_header(body, 1, 2)
    hdr_read, hdr_seven = band_header(body, 2, 1), band_header(body, 2, 2)
    hdr_re, hdr_wr = band_header(body, 3, 1), band_header(body, 3, 2)

    sections: list[dict] = []

    def add(band: str, title_cn: str, content: str, qs: list[dict], score: float,
            unit: float, score_map: dict[int, float] | None = None,
            answers_band: str | None = None,
            overrides: dict[int, str] | None = None,
            ana_overrides: dict[int, str] | None = None) -> None:
        aband = answers_band or band
        overrides = overrides or {}
        ana_overrides = ana_overrides or {}
        for q in qs:
            raw = overrides.get(q["id"]) or key.get(q["id"], "")
            q["answer"] = clean_key_answer(q["id"], raw, aband)
            q["score"] = _num(score_map.get(q["id"], unit) if score_map else unit)
            q["analysis"] = ana_overrides.get(q["id"]) or analysis.get(q["id"], "")
            if not q["answer"]:
                review.append(f"{title_cn} 第{q['id']}题：没有答案（主观题属正常缺口，已留空）")
        sections.append({
            "type": band, "questionType": BAND_TYPE[band][0], "title": title_cn,
            "category": band, "score": _num(sum(q["score"] for q in qs)),
            "content": rewrite(separate_images(content)), "questions": qs,
        })

    # 一、完形填空
    cloze = parse_cloze(h_cloze, review, "完形填空")
    if cloze["questions"]:
        add("完形填空", "一、完形填空", cloze["content"], cloze["questions"],
            section_score(hdr_cloze, 15), unit_score(hdr_cloze, 1.5))

    # 二、语法填空（个别地区卷是选词填空）
    band2 = ("选词填空" if re.search(r"选词填空|选词|方框|word\s*bank",
                                     (hdr_gram or "") + "\n" + (h_gram or ""), re.I)
             else "语法填空")
    lo, hi = (11, 20) if band2 == "语法填空" else (11, 20)
    ctx = grammar_ctx_from_pdf(pdf, lo, hi, review, "语法填空") if band2 == "语法填空" else {}
    cloze_ids = [q["id"] for q in cloze["questions"]] or list(range(1, 11))
    g_expect = list(range(max(cloze_ids) + 1, max(cloze_ids) + 11))
    gram = parse_grammar(h_gram, review, "语法填空", band2, expect=g_expect, pdf_ctx=ctx)
    if gram["questions"]:
        add(band2, f"二、{band2}", gram["content"], gram["questions"],
            section_score(hdr_gram, 15), unit_score(hdr_gram, 1.5))

    # 三～六、阅读（每篇一个 section）
    for i, rd in enumerate(parse_reading(h_read, review, "阅读")):
        add("阅读", f"{'三四五六'[i]}、阅读 {rd['tag']}", rd["content"], rd["questions"],
            len(rd["questions"]) * 2.0, 2.0)

    # 七、七选五
    seven = parse_seven(h_seven, review, "七选五")
    if seven["questions"]:
        add("七选五", "七、七选五", seven["content"], seven["questions"],
            section_score(hdr_seven, 10), 2.0)

    # 八、阅读表达
    re_block = parse_reading_expression(h_re, review, "阅读表达")
    if re_block["questions"]:
        smap = re_scores(hdr_re)
        re_ans = reading_expression_answers(ans, [q["id"] for q in re_block["questions"]])
        add("阅读表达", "八、阅读表达", re_block["content"], re_block["questions"],
            section_score(hdr_re, 12), 3.0, score_map=smap, overrides=re_ans)

    # 九、作文（范文：源卷给了就当答案）
    wr = parse_writing(h_wr, review, "作文")
    if wr["questions"]:
        no = wr["questions"][0]["id"]
        model = writing_model_from_block(ans, no)
        if model:
            review.append(f"作文 第{no}题：源卷自带范文（{len(model)} 字），已写入 answer"
                          f"（主观题，供批改参考；旧数据此处为空）")
        guide = writing_guide(ans, no)
        if guide:
            review.append(f"作文 第{no}题：源卷带写作指导（导语/详解/点睛），已写入 analysis")
        add("作文", "九、作文", wr["content"], wr["questions"],
            section_score(hdr_wr, 20), 20.0,
            overrides={no: model} if model else None,
            ana_overrides={no: guide} if guide else None)

    year = re.search(r"(20\d{2})", title)
    total = _num(sum(s["score"] for s in sections))
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": total, "duration": 90,
        "tags": [SUBJECT, args.grade, type_dir] + ([year.group(1)] if year else []),
    }
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text(
            "\n".join(_rev_line(x) for x in review) + "\n", encoding="utf-8")
    n_q = sum(len(s["questions"]) for s in sections)
    return (f"✓ {title}：{len(sections)} 个板块 / {n_q} 题 / 卷面 {total} 分 → {dst.name}")


def _rev_line(x: str) -> str:
    x = x.strip()
    return x if x.startswith("复核：") else "复核：" + x


# ──────────────────────────────────────────────────────────────────
# 自查
# ──────────────────────────────────────────────────────────────────

BLANK_ANY = re.compile(r"<ClozeBlank\b|<Blank\b|<InputLong\b|<Input2\b|<Input\b|<AnswerBox\b")


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    errs, notes = [], []
    data = json.loads(jf.read_text(encoding="utf-8"))
    paper = data.get("paper") or {}
    sections = data.get("sections") or []
    stat = {"title": paper.get("title") or jf.stem, "sections": 0, "q": 0, "ans": 0,
            "bands": [], "imgs": set(), "choice": 0, "fill": 0, "subj": 0}

    if "groups" in data:
        errs.append("顶层键是 groups（旧契约），应为 sections")
    if paper.get("subject") != SUBJECT:
        errs.append(f"paper.subject 不是英语：{paper.get('subject')!r}")
    for k in ("title", "subject", "grade", "year", "totalScore", "duration", "tags"):
        if k not in paper:
            errs.append(f"paper 缺字段 {k}")
    if not sections:
        errs.append("没有 sections")

    # 1) 分值合计
    sec_sum = sum(s.get("score") or 0 for s in sections)
    if abs(sec_sum - (paper.get("totalScore") or 0)) > 1e-6:
        errs.append(f"Σsection.score={_num(sec_sum)} ≠ paper.totalScore={paper.get('totalScore')}")
    if abs(sec_sum - 100) > 1e-6:
        notes.append(f"卷面合计 {_num(sec_sum)} 分（北京卷应为 100）")

    raw = json.dumps(data, ensure_ascii=False)
    for s in sections:
        title = s.get("title") or s.get("category")
        band = s.get("category")
        qs = s.get("questions") or []
        content = s.get("content") or ""
        stat["sections"] += 1
        stat["bands"].append(band)
        if band not in BANDS:
            errs.append(f"{title}：category={band!r} 不在 7 个板块里")
        want_qt = BAND_TYPE.get(band, ("", "", ""))[0]
        if s.get("questionType") != want_qt:
            errs.append(f"{title}：questionType={s.get('questionType')!r}（应为 {want_qt}）")
        if "article" in s:
            errs.append(f"{title}：同时写了 article（旧字段），新契约只写 content")
        if not content.strip():
            errs.append(f"{title}：section.content 为空（材料没进去）")
        ssum = sum(q.get("score") or 0 for q in qs)
        if abs(ssum - (s.get("score") or 0)) > 1e-6:                       # 断言 1
            errs.append(f"{title}：Σquestions.score={_num(ssum)} ≠ section.score={s.get('score')}")
        if not qs:
            errs.append(f"{title}：没有 questions")

        # 2) 选择题：选项数与答案
        is_choice = all(q.get("questionType") == "choice" for q in qs)
        want_opts = 7 if band == "七选五" else 4
        for q in qs:
            stat["q"] += 1
            if str(q.get("answer") or "").strip():
                stat["ans"] += 1
            qt = q.get("questionType")
            if qt == "choice":
                stat["choice"] += 1
                opts = q.get("options") or []
                ids = [o.get("id") for o in opts]
                if len(opts) != want_opts:
                    errs.append(f"{title} 第{q.get('id')}题：选项 {len(opts)} 项（应为 {want_opts}）")
                if len(set(ids)) != len(ids):
                    errs.append(f"{title} 第{q.get('id')}题：选项 id 重复 {ids}")
                ans = str(q.get("answer") or "")
                if not re.fullmatch(r"[A-G]", ans):
                    errs.append(f"{title} 第{q.get('id')}题：answer={ans!r}（应为单个大写字母）")
                elif ans not in ids:
                    errs.append(f"{title} 第{q.get('id')}题：answer={ans} 不在选项 {ids} 里")
                for o in opts:
                    if not (o.get("label") or "").strip():
                        errs.append(f"{title} 第{q.get('id')}题：选项 {o.get('id')} label 为空")
                if (q.get("content") or "").strip():
                    for e in (STEM_RESIDUE, STEM_NOTICE):
                        if e.match(q["content"]) or e.search((q.get("content") or "")[:120]):
                            errs.append(f"{title} 第{q.get('id')}题：题干有残渣 "
                                        f"{q['content'][:20]!r}")
            elif qt in ("input", "input2"):
                stat["fill"] += 1
                if not str(q.get("answer") or "").strip():
                    errs.append(f"{title} 第{q.get('id')}题：填空没有答案")
            elif qt == "text":
                stat["subj"] += 1
                if not (q.get("content") or "").strip():
                    errs.append(f"{title} 第{q.get('id')}题：主观题题干为空")
            else:
                errs.append(f"{title} 第{q.get('id')}题：questionType={qt!r} 不认识")
            if not (q.get("score") or 0) > 0:
                errs.append(f"{title} 第{q.get('id')}题：score={q.get('score')}（应 >0）")

        # 3) 材料与小问的对应（断言 3）
        #    非选择题 section：设问内联在 content 里，必须找得到且只出现一次；
        #    选择题 section（完形/阅读/七选五）：content 只放原文，题干在 questions[].content
        #    （前端 ReadingLayout 左侧原文右侧题目，题干不重复进原文），这里只查「设问不得抄进原文」。
        if not is_choice:
            for q in qs:
                qc = (q.get("content") or "").strip()
                if len(qc) < 12:
                    continue
                if _norm_cmp(qc) not in _norm_cmp(content):
                    errs.append(f"{title} 第{q.get('id')}题：设问在 section.content 里找不到")
                if content.count(qc) > 1:
                    errs.append(f"{title} 第{q.get('id')}题：设问在 content 里出现了 "
                                f"{content.count(qc)} 次（应只出现一次）")
        else:
            # 英语选择题豁免（MIGRATION §6.3 例外条款）：cloze / en-reading / seven-choose-five
            # 走「左原文右题目」版式，题干只在 questions[].content；这里只查反向——题干不得抄进原文。
            for q in qs:
                qc = (q.get("content") or "").strip()
                if len(qc) >= 12 and content.count(qc) > 0:
                    errs.append(f"{title} 第{q.get('id')}题：题干被抄进了原文（英语选择题豁免版式："
                                f"题干只应在 questions[].content）")

        # 4) 占位标签数 == 小问数，且标签类型必须是该板块那一种（断言 4，只对非选择题 section）
        if not is_choice:
            n_tag = len(BLANK_ANY.findall(content))
            if n_tag != len(qs):
                errs.append(f"{title}：占位标签 {n_tag} 个 ≠ 小问 {len(qs)} 个")
            want_tag = BAND_TYPE.get(band, ("", "", ""))[2]
            if want_tag:
                name = want_tag.split(">")[0].lstrip("<")          # Input / Input2 / InputLong
                n_ok = content.count("<" + name + ">")
                if n_ok != len(qs):
                    errs.append(f"{title}：应为 {len(qs)} 个 <{name}>，实际 {n_ok} 个")
                if n_tag != n_ok:
                    errs.append(f"{title}：混入了其它占位标签（{n_tag - n_ok} 个，"
                                f"该板块只认 <{name}>）")
        else:
            if band == "七选五":
                n_tag = content.count("<Blank>")
                if n_tag != len(qs):
                    errs.append(f"{title}：七选五空 {n_tag} 个 ≠ 小问 {len(qs)} 个")
            if band == "完形填空":
                n_tag = content.count("<ClozeBlank>")
                if n_tag != len(qs):
                    errs.append(f"{title}：完形空 {n_tag} 个 ≠ 小问 {len(qs)} 个")

        # 5) 材料开头不得是分区标题/卷首说明（断言 5）
        head = content.strip()[:120]
        if STEM_RESIDUE.match(content.strip()):
            errs.append(f"{title}：section.content 以分区标题开头 {head[:20]!r}")
        if STEM_NOTICE.search(head):
            errs.append(f"{title}：section.content 混进卷首说明 "
                        f"{STEM_NOTICE.search(head).group(1)!r}")

    # 6) 图片逐张落盘（断言 6）
    for ref in sorted(set(EXAM_IMG.findall(raw))):
        stat["imgs"].add(ref)
        if not (OUTPUT_DOCX_TO_MD / ref).exists():
            errs.append(f"图片缺失：/exam-images/{ref}")

    stat["rate"] = f"{stat['ans']}/{stat['q']}"
    return stat, errs, notes


def run_check(args) -> int:
    files: list[Path] = []
    if args.path:
        p = Path(args.path)
        files = [p] if p.is_file() else sorted(p.glob("*.json"))
    else:
        types = [t for t in (args.type or "").replace("，", ",").split(",") if t] or ["一模"]
        if "all" in types:
            types = sorted(x.name for x in Path(args.output).iterdir()
                           if x.is_dir() and x.name != ".cache")
        for t in types:
            files.extend(sorted((Path(args.output) / t).glob("*.json")))
    bad, blocks = 0, []
    for jf in files:
        try:
            stat, errs, notes = check_one(jf)
        except Exception as e:
            print(f"\n===== {jf} =====")
            print(f"  ✗ 解析失败：{str(e)[:160]}")
            bad += 1
            continue
        print(f"\n===== {jf.parent.name}/{jf.name} =====")
        print(f"板块 {stat['sections']} 个 / 题 {stat['q']}（选择 {stat['choice']} · "
              f"填空 {stat['fill']} · 主观 {stat['subj']}）/ 有答案 {stat['rate']}")
        print("  " + "，".join(stat["bands"]))
        print("  ✓ 契约校验通过" if not errs else f"  ✗ 错误 {len(errs)} 条：")
        for e in errs:
            print(f"     - {e}")
        for n in notes:
            print(f"  · 提示：{n}")
        if errs:
            bad += 1
        blocks.append((jf, stat, errs, notes))
    if args.report:
        lines = [f"# 英语 {len(blocks)} 套卷 · 解析自查报告", "",
                 "| 类型 | 卷 | 板块 | 题 | 选择 | 填空 | 主观 | 有答案 | 契约错误 |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for jf, st, errs, _n in blocks:
            lines.append(f"| {jf.parent.name} | {st['title']} | {st['sections']} | {st['q']} | "
                         f"{st['choice']} | {st['fill']} | {st['subj']} | {st['rate']} | "
                         f"{len(errs)} |")
        lines.append("")
        for jf, st, errs, notes in blocks:
            lines.append(f"## {st['title']}")
            lines.append("")
            lines.append("```")
            lines.append(json.dumps({"sections": st["bands"]}, ensure_ascii=False))
            lines.append("```")
            lines.append("")
            if errs:
                lines += [f"- ✗ {e}" for e in errs]
            if notes:
                lines += [f"- · {n}" for n in notes]
            lines.append("")
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    print(f"\n合计 {len(blocks)} 份，出错 {bad} 份")
    return 0 if bad == 0 else 2


# ──────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="英语整卷 → 题库 JSON（成熟格式，一板块一 section）")
    ap.add_argument("-p", "--path", help="单份 MD 路径（或卷目录）；配合 --check 时也接受 JSON 路径")
    ap.add_argument("--dir", default=str(OUTPUT_DOCX_TO_MD), help="MD 根目录")
    ap.add_argument("-T", "--type", default="一模", help="试卷类型（输出子目录名；--check 可 all 或逗号分隔）")
    ap.add_argument("-s", "--subject", default=SUBJECT, help="学科（默认 英语）")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--papers", nargs="*", default=None, help="只处理这几套（按卷名片段匹配）")
    ap.add_argument("--force", action="store_true", help="忽略已有 JSON，重跑")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="本链路不调模型（保留参数兼容）")
    ap.add_argument("--check", action="store_true", help="只做自查（纯 Python，不调模型）")
    ap.add_argument("--report", default=None, help="自查报告输出路径（配合 --check）")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(run_check(args))

    OUT["root"] = Path(args.output)
    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md"))[:1] if p.is_dir() else [p]
    else:
        root = Path(args.dir) / args.type / args.subject
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir())
                 if d.is_dir() and sorted(d.glob("*.md"))] if root.is_dir() else []
        if args.papers:
            files = [f for f in files if any(k in f.stem for k in args.papers)]
    log(f"共 {len(files)} 份 MD → {Path(args.output) / args.type}")
    for f in files:
        t0 = time.time()
        try:
            log("  " + process(f, args.type, args) + f"（{time.time() - t0:.0f}s）")
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
