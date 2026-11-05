#!/usr/bin/env python3
"""政治整卷解析（**生物式结构**）→ 题库导入 JSON（`output_json_politics/<类型>/<卷名>.json`）。

链路：docx→MD（batch_convert_docx_to_md.py）→ 本脚本 → 题库导入脚本

输出结构（字段名与导入脚本严格一致，**不要改名字**）：

    {"paper": {"title","subject","source","grade","year","totalScore","duration","tags"},
     "sections": [
       {"type":"选择题","questionType":"politics-choice","title":"一、选择题","category":"选择","score":45,
        "questions":[{"id":1,"questionType":"single","content":…,
                      "options":[{"id":"A","label":…}×4],"answer":"B","analysis":"","score":3}]},
       {"type":"非选择题","questionType":"politics-fill","title":"二、非选择题（第16题）",
        "category":"哲学文化","score":7,
        "content":"材料 + 各小问设问，每个设问后紧跟 <AnswerBox></AnswerBox>",
        "questions":[{"id":1601,"questionType":"input","content":"设问原文","answer":…,
                      "analysis":"","score":7}]}
     ]}

与旧版（按模块分组、`split_subs()` 把材料复制进每个小问）的关键差别 —— 也是本次重做的原因：

- **一个大题一个 section**（16、17…），**材料只出现一次**在 `section.content` 里；
- 版式**单栏内联**：`section.content` = 材料（只出现一次）+ 按题面顺序排列的各小问设问，
  每个设问**紧接着**一个答题框占位符 `<AnswerBox></AnswerBox>`（英语「阅读表达」那一款大答题框，
  高度由前端统一给，标签不带属性）；
- 每个小问是一道 `questionType: "input"` 的独立小题：`content` = 该小问设问原文（不含材料、不含分值括号），
  `answer` = 该小问参考答案原文，`score` = 该小问分值；
  Σquestions.score == section.score == 题面标注的大题分值；答框的 `{stem}` 走 `questions[].content`。

关键约定：

- 选择题 15 题 × 3 分 = 45 分；选项恒 A–D，答案单个大写字母；选项里的 ①②③④ 组合**照原样**放 label；
- 卷面上「能源安全……回答第 13、14 题」这类**共享材料/共享图**复制进该组每道题的 content（每题自洽）；
- 答案区三种形态都要吃得下：①「1．B 2．C …」字母串（含无分隔符的「1.B2.A3.D」）；
  ②每个大题「16．（7分）」块 + (1)(2)(3) 逐问；③「等级水平 / 等级描述」GFM 表格（水平1–4）——
  等级表是评分依据，**必须原样留在该题 answer 里**，不能丢；
- 参考答案后面若还有「评分标准 / 阅卷细则」附录，只取「参考答案」正文，附录不并入 answer
  （在 `.review.txt` 里记一行）；
- 表格里塞图片（「图标列 + 文字列」两列表格）→ 改成列表并丢掉装饰图标；
  真的数据表 → GFM markdown 表格（`<br><br>` 保留）；
- 图片引用统一 `/exam-images/<类型>/政治/<卷名>/images/x.png`（卷名不带「（教师版）」）。
- 大题 module 标签只取 7 类：中特 / 经济社会 / 政治法治 / 哲学文化 / 国政国经 / 法律生活 / 逻辑思维
  （一次 AI 调用；`--no-ai` 落默认标签并在 review 记一行）。选择题 section 的 category 恒为 `选择`。

用法：
    python3 convert_politics_md.py -p <md 文件> [-T 一模] [--force] [--no-ai]
    python3 convert_politics_md.py -T 一模                      # 批量（单进程，别用进程池）
    python3 convert_politics_md.py --check -T 一模 [--report x.md]
"""
import argparse
import json
import re
import time
from pathlib import Path

from convert_chinese_md import chat, rewrite_images, split_paper, log
from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_POLITICS
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "政治"
OUT_ROOT = OUTPUT_JSON_POLITICS
OUT = {"root": OUT_ROOT}

MODULES = ["中特", "经济社会", "政治法治", "哲学文化", "国政国经", "法律生活", "逻辑思维"]
DEFAULT_CAT = MODULES[0]

N_CHOICE = 15            # 第一部分：15 道单选
MAX_CHOICE = 20          # 容错上限（题号重复/自动编号）
CHOICE_SCORE = 3         # 每题 3 分
PAPER_TOTAL = 100        # 北京政治卷 100 分

SYSTEM = "你是政治试卷结构化助手，只输出 JSON。"

# ──────────────────────────────────────────────────────────────────
# 正则
# ──────────────────────────────────────────────────────────────────

# 题号行（可带前置图片、可带 markdown 标题前缀）：`1．` `7\.` `## 16．（8分）`
QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、·]\s*\**"
)
# 分节标题：第一部分 / 第二部分（可带 # 与 **）
HDR_P1 = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\**\s*第[一1]部分")
HDR_P2 = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\**\s*第[二2]部分")
# 大题题号 + 分值：`16．（7分）` `16\. (8 分)` `## 18.(14 分)`
BIGMARK = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、]\s*\**\s*"
    r"[（(]\s*(\d+)\s*分\s*[）)]"
)
# 小问编号行：`（1）` `(2)`（行首，允许 ** 包裹）
SUBQ = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?[（(]\s*(\d{1,2})\s*[）)]\s*")
# 分值括号
SCORE_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")
# 选项标记：`A．` `A.` `A、`（前后允许空白/中文）
OPT_MARK = re.compile(r"(?:^|(?<=[\s\u3000\u00a0>)\u4e00-\u9fff]))([A-D])\s*[．.、]\s*")
# 「回答第13、14题」这类共享材料引导句
SHARED = re.compile(r"据此|据图|读图|如图|回答\s*第?\s*([\d\s、,，和及~～至\-－]+)\s*小?题")
IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
EXAM_IMG = re.compile(r"/exam-images/([^\"'\s)]+)")
BOILER = re.compile(r"[（(]\s*考生务必将答案[^）)]*[）)]")
RIGHT_LINE = re.compile(r"(?m)^[ \t]*(?:——|—|--|-{2,})\s*(\S.*)$")
PH = re.compile(r"<Input(?:Long)?\b[^>]*></Input(?:Long)?>|<AnswerBox\s*>\s*</AnswerBox>")
BOX = re.compile(r"<AnswerBox\b")

CAT_PROMPT = r"""下面是北京高三政治试卷的若干道非选择题（题号｜设问｜参考答案摘要）。
请为**每题**判断它考查的模块，只能从下面 7 个里选一个：
中特（中国特色社会主义）/ 经济社会（经济与社会）/ 政治法治（政治与法治）/
哲学文化（哲学与文化）/ 国政国经（当代国际政治与经济）/ 法律生活（法律与生活）/ 逻辑思维（逻辑与思维）

判定要点：
- 按**设问要求的学科知识**判（设问里写「运用《政治与法治》知识」就直接归「政治法治」，
  「运用《法律与生活》知识」归「法律生活」，以此类推）；
- 设问写「综合运用所学」且看不出单一模块的，按材料主线判；实在判不出来归「中特」；
- 形式逻辑/三段论/假言推理/联言选言/辩证思维/创新思维 → 「逻辑思维」；
- 一句话写 why（不超过 20 字，指出判据关键词）。

只输出 JSON：{{"16": {{"category": "哲学文化", "why": "文化功能+依法治国"}}}}
题目：
{ITEMS}
"""


# ──────────────────────────────────────────────────────────────────
# 通用小工具
# ──────────────────────────────────────────────────────────────────

def ai_json(prompt: str, max_tokens: int = 4000) -> dict:
    try:
        got = chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt}], max_tokens)
    except Exception as e:
        log(f"    AI 失败：{str(e)[:120]}")
        return {}
    return got if isinstance(got, dict) else {}


def _norm_cmp(s: str) -> str:
    return re.sub(r"[\s\u3000，。；：、,.;:!?！？（）()【】\[\]\"'“”‘’…—\-－]", "", s or "")


def split_paras(text: str) -> list[str]:
    """按空行切段（表格块 / 列表块内部没有空行，会自然成为一段）。"""
    return [p.strip() for p in re.split(r"\n[ \t]*\n", (text or "").replace("\r", "")) if p.strip()]


# 这些开头的段落一律各自成段，不参与「拼回上一段」
PARA_HEAD = re.compile(
    r"^(?:[（(]\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*[）)]"
    r"|[①②③④⑤⑥⑦⑧⑨⑩]|注\s*[:：]|图\s*\d|表\s*\d|[A-Da-d]\s*[．.、]"
    r"|\*\*|#{1,6}\s|\||!\[|[-*+]\s|[⯎❖◇●◆■▲※★→]|【|材料[一二三四五]|参考答案)"
)
PARA_END_OK = "。！？；…\"”』）】"


def _can_merge(prev: str, nxt: str) -> bool:
    """上一段没写句末标点 → 这一行是 Word 硬换行/分页切断的半句，接回去。"""
    if not prev or prev.startswith(("|", "![")) or len(prev) < 12:
        return False
    if PARA_HEAD.match(nxt):
        return False
    if prev[-1] in PARA_END_OK or prev.endswith("*"):
        return False                      # `**第286条：**` / `**【政策要点】**` 是标题，不拼
    return True


def _join_sep(prev: str, nxt: str) -> str:
    """把被硬换行切断的两截接回去时，中间要不要空格（中文里只有「汉字 + 数字/字母」才留一个）。"""
    a, b = prev[-1], nxt[0]
    if re.match(r"[A-Za-z0-9]", a) and re.match(r"[A-Za-z0-9]", b):
        return " "
    if re.match(r"[\u4e00-\u9fff]", a) and re.match(r"[A-Za-z0-9]", b):
        return " "
    return ""


def reflow_paras(paras: list[str]) -> list[str]:
    """把 Word 里被硬换行/分页切断的句子拼回去（只拼「上一段没有句末标点」的情况）。"""
    out: list[str] = []
    for p in paras:
        if out and _can_merge(out[-1], p):
            prev = out[-1]
            out[-1] = prev + _join_sep(prev, p) + p
        else:
            out.append(p)
    return out


# MDX/CommonMark 口径：`**X：**Y` 这种「加粗串以中文标点结尾、紧跟正文」的写法，
# 闭合 ** 既不左向也不右向（前一字符是标点、后一字符是汉字），GFM 不会当加粗解析，
# 页面会原样显示星号（西城 19 题「⯎**乡镇政府：**必须…」实测）。把结尾标点挪到加粗外面
# 变成 `**X**：Y` 即可正常加粗，语义不变。
BOLD_PUNCT_RE = re.compile(r"\*\*([^*\n]*?[：:，,。、；;）)\"])" + r"\*\*(?=[^\s*])")


def fix_bold_punct(t: str) -> str:
    if not t or "**" not in t:
        return t
    return BOLD_PUNCT_RE.sub(lambda m: f"**{m.group(1)[:-1]}**{m.group(1)[-1]}", t)


def clean_text(t: str) -> str:
    """政治题干清洗（保留旧版能用的部分，另补上单格 HTML 表格）。

    1) joplin/HTML 表格包裹的问卷 → 单格转段落、多格转 GFM 表格；
    2) 去掉「（考生务必将答案答在答题卡上…）」这类考试须知；
    3) 「——XXX」「（取材于…）」这类出处行 → <RightAlign>。
    """
    if not t:
        return t

    def _html_inner(block: str) -> str:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", block, flags=re.S)
        cells_rows = [[c for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.S)]
                      for r in rows]
        cells_rows = [r for r in cells_rows if r]
        if len(cells_rows) == 1 and len(cells_rows[0]) == 1:
            # 单格表格：其实是「一段排版容器」（如问卷），拆成普通段落
            c = cells_rows[0][0]
            c = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>",
                       lambda m: "\n\n**" + re.sub(r"<[^>]+>", "", m.group(2)).strip() + "**\n\n",
                       c, flags=re.S)
            c = re.sub(r"<br\s*/?>", "\n", c, flags=re.I)
            c = re.sub(r"</p>", "\n\n", c, flags=re.I)
            c = re.sub(r"<[^>]+>", "", c)
            c = re.sub(r"[ \t\u3000]+\n", "\n", c)
            return "\n" + re.sub(r"\n{3,}", "\n\n", c).strip() + "\n"
        if cells_rows:
            out = []
            for r in cells_rows:
                cells = [re.sub(r"<br\s*/?>", " ", re.sub(r"<[^>]+>", "", c), flags=re.I).strip()
                         for c in r]
                out.append("| " + " | ".join(cells) + " |")
            if len(out) >= 2:
                ncol = out[0].count("|") - 1
                return "\n" + "\n".join([out[0], "| " + " | ".join(["---"] * ncol) + " |"] + out[1:]) + "\n"
            return "\n" + out[0] + "\n"
        return re.sub(r"<[^>]+>", "", block)

    t = re.sub(r"<div[^>]*joplin-table-wrapper[^>]*>.*?</div>", lambda m: _html_inner(m.group(0)), t, flags=re.S)
    t = re.sub(r"<table[^>]*>.*?</table>", lambda m: _html_inner(m.group(0)), t, flags=re.S)
    t = BOILER.sub("", t)
    lines = []
    for ln in t.split("\n"):
        mm = RIGHT_LINE.match(ln)
        if mm:
            lines.append(f"<RightAlign>——{mm.group(1).strip()}</RightAlign>")
        elif re.match(r"^[（(]\s*取材于", ln.strip()):
            lines.append(f"<RightAlign>{ln.strip()}</RightAlign>")
        else:
            lines.append(ln)
    t = "\n".join(lines)
    return fix_bold_punct(re.sub(r"\n{3,}", "\n\n", t).strip())


# ──────────────────────────────────────────────────────────────────
# markdown 表格：规范化（表头 / 列数对齐 / `<br><br>` 保留）
# ──────────────────────────────────────────────────────────────────

TBL_LINE = re.compile(r"^\s*\|")
TBL_CELL = re.compile(r"(?<!\\)\|")


def _cell_split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in TBL_CELL.split(s)]


def _is_sep_row(cells: list[str]) -> bool:
    body = [c.replace(" ", "") for c in cells if c.strip()]
    return bool(body) and all(re.fullmatch(r":?-+:?", c) for c in body)


def _clean_cell(cell: str) -> str:
    """单元格清洗：公式与 `<br>` 原样保留，只收拾 Word 转出来的空格残留。"""
    c = cell.strip()
    if not c:
        return ""
    c = c.replace("\\_\\_", "_").replace("\\.", ".").replace("\\-", "-")
    c = re.sub(r"(?<![\d.])(\d+(?:\.\s?\d+)+)", lambda m: re.sub(r"\.\s+", ".", m.group(1)), c)
    c = re.sub(r"(?<=\d)\s+%", "%", c)
    c = re.sub(r"\(\s*([a-zA-Z]+)\s*/\s*([a-zA-Z]+)\s*\)", r"(\1/\2)", c)
    c = re.sub(r"(?:<br\s*/?>\s*){2,}", "<br><br>", c, flags=re.I)      # 多个 <br> → <br><br>
    c = re.sub(r"[ \t\u3000\u00a0]{2,}", " ", c)
    return c.strip()


def normalize_md_tables(text: str) -> str:
    """把 Markdown 表格规范成 remark-gfm 一定认的形态（分隔行唯一、行列数与表头一致）。"""
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if not TBL_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip() and TBL_LINE.match(lines[i]):
            block.append(lines[i])
            i += 1
        rows = [_cell_split(l) for l in block if not _is_sep_row(_cell_split(l))]
        rows = [[_clean_cell(c) for c in r] for r in rows]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            out.extend(block)
            continue
        ncol = max(2, max(len(r) for r in rows))
        fixed = [(r + [""] * (ncol - len(r))) if len(r) < ncol else r[:ncol] for r in rows]
        head, body = fixed[0], fixed[1:]
        out.append("")
        out.append("| " + " | ".join(head) + " |")
        out.append("| " + " | ".join(["---"] * ncol) + " |")
        for r in body:
            out.append("| " + " | ".join(r) + " |")
        out.append("")
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return res.strip("\n") if text.strip() else res


def count_md_tables(text: str) -> int:
    n, prev = 0, False
    for line in (text or "").split("\n"):
        cur = bool(TBL_LINE.match(line))
        if cur and not prev:
            n += 1
        prev = cur
    return n


def table_shape_ok(text: str) -> list[str]:
    """表格可被简单解析：分隔行唯一、每行列数与表头一致、列数 ≥2。"""
    errs, i = [], 0
    lines = (text or "").split("\n")
    ti = 0
    while i < len(lines):
        if not TBL_LINE.match(lines[i]):
            i += 1
            continue
        ti += 1
        block = []
        while i < len(lines) and TBL_LINE.match(lines[i]):
            block.append(lines[i])
            i += 1
        cells = [_cell_split(l) for l in block]
        seps = [c for c in cells if _is_sep_row(c)]
        if len(seps) != 1:
            errs.append(f"表{ti}：分隔行 {len(seps)} 行（应为 1 行）")
            continue
        ncol = len(cells[0])
        if ncol < 2:
            errs.append(f"表{ti}：只有 {ncol} 列")
        for k, c in enumerate(cells):
            if len(c) != ncol:
                errs.append(f"表{ti} 第{k + 1}行：{len(c)} 列 ≠ 表头 {ncol} 列")
    return errs


def unwrap_single_col_tables(text: str, review: list[str], label: str) -> str:
    """单列表格（Word 里其实是给一段文字加了个「框」）→ 拆成普通段落。

    法条框（`| **《中华人民共和国商标法》** |` + 一行正文）这种，硬撑成两列表格会多出一个
    空列，改成普通段落更好读；单元格里的 `<br><br>` 拆成段落。
    """
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out, i, n = [], 0, 0
    while i < len(lines):
        if not TBL_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip() and TBL_LINE.match(lines[i]):
            block.append(lines[i])
            i += 1
        rows = [_cell_split(l) for l in block if not _is_sep_row(_cell_split(l))]
        rows = [[_clean_cell(c) for c in r] for r in rows if any(c.strip() for c in r)]
        if rows and all(len(r) == 1 for r in rows):
            n += 1
            out.append("")
            for r in rows:
                for piece in re.split(r"(?:<br\s*/?>\s*)+", r[0]):
                    piece = piece.strip()
                    if piece:
                        out.append(piece)
                        out.append("")
            continue
        out.extend(block)
    if n:
        review.append(f"{label}：{n} 张单列表格（纯「框」排版）已拆成普通段落")
    return "\n".join(out)


def flatten_icon_tables(text: str, review: list[str], label: str) -> str:
    """「图标列 + 文字列」的两列表格（单元格里塞装饰图标）→ 改写成列表，图标丢掉。

    Word 里用一张表把「图标 | 说明」并排排版，转成 GFM 后图标列只剩 `![](images/image7.png)`
    （47×53 的装饰图，不是有效图形），表格也失去了排版意义 —— 改成列表更贴近原意。
    """
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out, i, n_flatten = [], 0, 0
    while i < len(lines):
        if not TBL_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip() and TBL_LINE.match(lines[i]):
            block.append(lines[i])
            i += 1
        rows = [_cell_split(l) for l in block if not _is_sep_row(_cell_split(l))]
        rows = [[_clean_cell(c) for c in r] for r in rows if any(c.strip() for c in r)]
        if not rows:
            out.extend(block)
            continue
        icon_only = r"(?:\s*!\[[^\]]*\]\([^)]*\)\s*)+"
        # 首列全是图标的表：第一行往往也是数据（Word 表没有真正的表头），整表都当数据行
        start = 0 if re.fullmatch(icon_only, rows[0][0] or "") else 1
        body = rows[start:]
        icon_col = bool(body) and all(r and re.fullmatch(icon_only, r[0] or "") for r in body)
        if icon_col:
            items = []
            for r in body:
                rest = [c for c in r[1:] if c.strip() and not re.fullmatch(icon_only, c or "")]
                if rest:
                    items.append("- " + " ".join(rest))
            if items:
                n_flatten += 1
                out.append("")
                out.extend(items)
                out.append("")
                continue
        out.extend(block)
    if n_flatten:
        review.append(f"{label}：{n_flatten} 张「图标列 + 文字列」的排版表已改写成列表，装饰图标已去掉")
    return "\n".join(out)


IMG_TOKEN = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def separate_images(text: str) -> str:
    """图片一律独占一段（Word 常把「![](…png)①……」挤在同一行，前端会当成行内图）。"""
    if not text or "![" not in text:
        return text
    out: list[str] = []
    for p in split_paras(text):
        if p.startswith("|") or not IMG_TOKEN.search(p) or IMG_TOKEN.fullmatch(p):
            out.append(p)
            continue
        last = 0
        for m in IMG_TOKEN.finditer(p):
            pre = p[last:m.start()].strip()
            if pre:
                out.append(pre)
            out.append(m.group(0))
            last = m.end()
        tail = p[last:].strip()
        if tail:
            out.append(tail)
    return "\n\n".join(out)


def drop_tiny_images(text: str, review: list[str], label: str, md: Path) -> str:
    """丢掉「几十像素的装饰图」引用（卷首 logo、表内小图标），避免前端出现无意义碎图。"""
    if not text or "![" not in text:
        return text
    sizes: dict[str, tuple[int, int]] = {}

    def size_of(p: Path) -> tuple[int, int]:
        key = str(p)
        if key not in sizes:
            try:
                import subprocess
                out = subprocess.run(["identify", "-format", "%w %h", str(p)],
                                     capture_output=True, text=True, timeout=60)
                w, h = (out.stdout or "").split()[:2]
                sizes[key] = (int(w), int(h))
            except Exception:
                sizes[key] = (0, 0)
        return sizes[key]

    def repl(m):
        ref = m.group(1)
        w, h = size_of(md.parent / ref)
        if 0 < w <= 90 and 0 < h <= 90:
            review.append(f"{label}：去掉一张装饰小图 {ref}（{w}×{h}px，非有效图形）")
            return ""
        return m.group(0)

    return IMG_REF.sub(repl, text)


# ──────────────────────────────────────────────────────────────────
# 卷面切分
# ──────────────────────────────────────────────────────────────────

ANSWER_BOX = "<AnswerBox></AnswerBox>"


def ph_tag(score: int = 0) -> str:      # noqa: ARG001  （分值不再影响占位符，保留签名以便将来分档）
    """该小问的答题占位符：统一 `<AnswerBox></AnswerBox>`（大答题框，高度由前端给）。

    历史上按分值分过档（`<Input>` / `<InputLong rows={4}>` 等），2026-11 起三科大题统一改用语
    阅读表达那一款大答题框，**不再带属性**；`--check` 断言 `<Input`/`<InputLong` 残留为 0。
    """
    return ANSWER_BOX


def split_marks(text: str, lo: int, hi: int) -> list[dict]:
    """按题号行切段（lo..hi 限定题号范围）。题号前粘着的图片作为本题 lead。"""
    marks = [m for m in QMARK.finditer(text) if lo <= int(m.group(2)) <= hi]
    out = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        out.append({
            "_m": m,
            "no": int(m.group(2)),
            "lead": (m.group(1) or "").strip(),
            "text": text[m.end():(nxt.start() if nxt else len(text))],
        })
    return out


def parse_choice_answers(ans: str) -> dict[int, str]:
    """选择题答案串：`1．B 2．C`、`1\\. C 2. B`、无分隔符的 `1.B2.A3.D` 都吃。"""
    out: dict[int, str] = {}
    for m in re.finditer(r"(\d{1,2})\s*\\?[.．、]\s*\**\s*([A-D])(?![A-Za-z])", ans):
        n = int(m.group(1))
        if 1 <= n <= MAX_CHOICE:
            out.setdefault(n, m.group(2).upper())
    return out


def options_from_table(seg: str) -> tuple[list[dict], str, str]:
    """选项被排成表格的选择题：首列是 A/B/C/D 时，表格留在题干，options 只给字母。"""
    lines = seg.split("\n")
    blocks, cur = [], []
    for i, line in enumerate(lines):
        if TBL_LINE.match(line):
            cur.append(i)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    for idxs in blocks:
        rows = [_cell_split(lines[i]) for i in idxs]
        rows = [r for r in rows if not _is_sep_row(r) and any(c.strip() for c in r)]
        if len(rows) < 3:
            continue
        got = {re.sub(r"[^A-Da-d]", "", (r[0] or "").strip()).upper() for r in rows[1:]}
        if {"A", "B", "C", "D"} <= got:
            return [{"id": k, "label": k} for k in "ABCD"], seg, "first_col"
    return [], seg, ""


def parse_choice_segment(seg: str, no: int, review: list[str]) -> dict:
    """一道选择题 → {content, options}。选项可能一行一个、一行两个，也可能整块是表。"""
    seg = seg.strip()
    opts, content, mode = options_from_table(seg)
    if mode == "first_col":
        review.append(f"第{no}题（选择题）：选项原卷就是一张表（首列 A–D），表格已完整留在题干，"
                      f"options 只给字母 A–D")
        return {"content": content.strip(), "options": opts}
    found: dict[str, re.Match] = {}
    for m in OPT_MARK.finditer(seg):
        found.setdefault(m.group(1), m)
    if all(k in found for k in "ABCD") and \
            found["A"].start(1) < found["B"].start(1) < found["C"].start(1) < found["D"].start(1):
        content = seg[:found["A"].start(1)].strip()
        order = ["A", "B", "C", "D"]
        for i, k in enumerate(order):
            start = found[k].end()
            end = found[order[i + 1]].start(1) if i + 1 < len(order) else len(seg)
            opts.append({"id": k, "label": seg[start:end].strip()})
    else:
        log(f"    ⚠ 第{no}题选项没切全（标记 {sorted(found)}）")
        review.append(f"第{no}题（选择题）：选项没切全（只找到 {''.join(sorted(found)) or '无'}），需人工核对")
    for o in opts:
        o["label"] = re.sub(r"\s+", " ", o["label"]).strip()
    return {"content": content.strip(), "options": opts}


def shared_material_of(seg: str) -> tuple[str, str]:
    """从段落里摘出「……回答第 13、14 题」这类共享材料引导句 → (去掉引导句的正文, 引导句)。"""
    paras = split_paras(seg)
    for i, p in enumerate(paras):
        m = SHARED.search(p)
        if m and m.group(1) and re.search(r"\d", m.group(1)):
            material = p.strip()
            rest = "\n\n".join(paras[:i] + paras[i + 1:])
            return rest.strip(), material
    return seg, ""


def shared_targets(text: str) -> list[int]:
    """引导句里的题号列表（「回答第 13、14 题」→ [13,14]）。"""
    m = SHARED.search(text or "")
    if not m or not m.group(1):
        return []
    nums = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
    return [n for n in nums if 1 <= n <= MAX_CHOICE]


def parse_choice_part(choice_body: str, review: list[str]) -> list[dict]:
    """第一部分：15 道单选。共享材料/共享图复制进该组每道题的 content（每题自洽）。"""
    segs = split_marks(choice_body, 1, MAX_CHOICE)
    if not segs:
        return []
    pre = choice_body[:segs[0]["_m"].start()]
    shared: dict[int, list[str]] = {}

    # 卷首（第一题之前）的引导材料
    pre_rest, pre_mat = shared_material_of(pre)
    if pre_mat:
        for n in shared_targets(pre_mat):
            shared.setdefault(n, []).append(pre_mat)
        review.append(f"选择题：卷首引导材料「{pre_mat[:24]}…」已复制进第 "
                      f"{'、'.join(map(str, shared_targets(pre_mat)))} 题")

    items = []
    for s in segs:
        seg = s["text"]
        if s["lead"]:
            seg = s["lead"] + "\n" + seg
        rest, mat = shared_material_of(seg)
        if mat:
            tgt = shared_targets(mat)
            for n in tgt:
                shared.setdefault(n, []).append(mat)
            if tgt:
                review.append(f"第{s['no']}题段末的共享材料「{mat[:24]}…」已从本题移出，"
                              f"复制进第 {'、'.join(map(str, tgt))} 题")
            seg = rest
        items.append({"no": s["no"], "raw": seg})

    nos = [it["no"] for it in items]
    if nos != list(range(1, len(items) + 1)):
        log(f"    选择题题号不规整 {nos} → 按出现顺序重编 1–{len(items)}")
        for i, it in enumerate(items, 1):
            it["no"] = i
        if shared:
            review.append(f"选择题题号不规整（{nos}），共享材料按出现顺序对应，需人工复核")

    out = []
    for it in items:
        parsed = parse_choice_segment(it["raw"], it["no"], review)
        content = parsed["content"]
        if it["no"] in shared:
            content = "\n\n".join(shared[it["no"]] + [content])
        out.append({"id": it["no"], "questionType": "single",
                    "content": clean_text(content), "options": parsed["options"],
                    "answer": "", "analysis": "", "score": CHOICE_SCORE})
    return out


def split_essay(seg: str) -> tuple[list[str], list[dict]]:
    """一道大题 → (前置材料段落, [{no, ask_raw, material, scores}])。

    **先在原始段落上判「设问 / 材料」**（设问 = 含分值括号的那段，没有分值括号时取最后一段），
    再把材料段落做「拼回被 Word 硬换行切断的半句」——顺序不能反，否则材料末尾没写句号的
    那一句会被拼进设问里，把设问认错。
    """
    seg = BOILER.sub("", seg or "")
    paras = split_paras(seg)
    if not paras:
        return [], []
    marks = [i for i, p in enumerate(paras) if SUBQ.match(p)]
    if not marks:
        idx = None
        for i in range(len(paras) - 1, -1, -1):
            if SCORE_RE.search(paras[i]):
                idx = i
                break
        if idx is None:
            idx = len(paras) - 1
        ask_raw = "\n\n".join(paras[idx:])
        scores = [int(x) for x in SCORE_RE.findall(ask_raw)]
        return (reflow_paras(paras[:idx]),
                [{"no": 1, "ask_raw": ask_raw, "material": [], "scores": scores}])
    preamble = paras[:marks[0]]
    subs = []
    for k, i in enumerate(marks):
        end = marks[k + 1] if k + 1 < len(marks) else len(paras)
        chunk = paras[i:end]
        stop = 0
        for j in range(1, len(chunk)):
            if SCORE_RE.search(chunk[j]):
                stop = j
                break
        ask_raw = "\n\n".join(chunk[:stop + 1])
        subs.append({
            "no": int(SUBQ.match(chunk[0]).group(1)),
            "ask_raw": ask_raw,
            "material": reflow_paras(chunk[stop + 1:]),
            "scores": [int(x) for x in SCORE_RE.findall(ask_raw)],
        })
    return reflow_paras(preamble), subs


def split_ans_block(block: str) -> tuple[str, dict[int, str]]:
    """一道大题的参考答案块 → (整块, {小问号: 该问答案})。"""
    marks = list(SUBQ.finditer(block))
    if not marks:
        return block.strip(), {}
    out: dict[int, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        out.setdefault(int(m.group(1)), block[m.end():end].strip())
    return block.strip(), out


def clean_answer(ans: str, score: int | None, review: list[str], label: str) -> str:
    """答案出口规范化：去掉与本题分值相同的前/后「（N分）」、合并多余空行。"""
    a = (ans or "").strip()
    if not a:
        return ""
    lead = SCORE_RE.match(a)
    if lead and score and int(lead.group(1)) == score:
        a = a[lead.end():].lstrip()
    tail = SCORE_RE.search(a)
    if tail and score and int(tail.group(1)) == score and a[tail.end():].strip() == "":
        a = a[:tail.start()].rstrip()
    a = re.sub(r"\n{3,}", "\n\n", a)
    return a.strip()


def into_questions(n_chunks: int, blocks: dict[int, str], nos: list[int]) -> dict[int, str]:
    """把参考答案的小问块对到题面的小问号上（对不上就按顺序）。"""
    out: dict[int, str] = {}
    if not blocks:
        return out
    if all(n in blocks for n in nos):
        return {n: blocks[n] for n in nos}
    if len(nos) == 1 and len(blocks) == 1:
        return {nos[0]: next(iter(blocks.values()))}
    for n, v in zip(nos, [blocks[k] for k in sorted(blocks)]):
        out[n] = v
    return out


# ──────────────────────────────────────────────────────────────────
# 分类（一次 AI 调用）
# ──────────────────────────────────────────────────────────────────

def classify(items: list[tuple[int, str, str]], review: list[str], cache: Path,
             allow_ai: bool, force: bool) -> dict[int, str]:
    """items = [(题号, 设问摘要, 答案摘要)] → {题号: 模块}。"""
    if cache.exists() and (not allow_ai or not force):
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            got = {int(k): (v[0] if isinstance(v, list) else str(v)) for k, v in raw.items()}
            if got:
                for no, cat in got.items():
                    if cat not in MODULES:
                        review.append(f"第{no}题：缓存里的分类「{cat}」不合法 → 落 {DEFAULT_CAT}")
                        got[no] = DEFAULT_CAT
                    review.append(f"分类：第{no}题={got[no]}（沿用缓存）")
                return got
        except Exception:
            pass
    if not allow_ai:
        review.append(f"复核：--no-ai 未调用分类模型，{len(items)} 道大题一律落默认标签「{DEFAULT_CAT}」")
        return {no: DEFAULT_CAT for no, _a, _b in items}
    lines = "\n".join(f"- {no}｜{_norm_cmp(ask)[:90]}" + (f"｜参考答案：{_norm_cmp(ans)[:50]}" if ans else "")
                      for no, ask, ans in items)
    got = ai_json(CAT_PROMPT.replace("{ITEMS}", lines), max_tokens=3000)
    out: dict[int, str] = {}
    whis: dict[int, str] = {}
    for k, v in (got or {}).items():
        try:
            no = int(re.sub(r"\D", "", str(k)))
        except Exception:
            continue
        if isinstance(v, dict):
            cat, why = str(v.get("category") or "").strip(), str(v.get("why") or "").strip()[:40]
        else:
            cat, why = str(v).strip(), ""
        if cat not in MODULES:
            review.append(f"第{no}题：模型给的分类「{cat}」不在 7 类里 → 落 {DEFAULT_CAT}")
            cat = DEFAULT_CAT
        out[no] = cat
        whis[no] = why
    if not out:
        review.append(f"分类模型没有返回结果，{len(items)} 道大题一律落默认标签「{DEFAULT_CAT}」")
        return {no: DEFAULT_CAT for no, _a, _b in items}
    for no, cat in sorted(out.items()):
        review.append(f"分类：第{no}题={cat}" + (f"（{whis[no]}）" if whis.get(no) else ""))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({str(k): [v, whis.get(k, "")] for k, v in out.items()},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────

def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{dst.name}"

    review: list[str] = []
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    if not ans or len(ans.strip()) < 50:
        raise RuntimeError("该卷没有参考答案区，无法解析")

    # 参考答案后面常跟「评分标准 / 阅卷细则」附录 → 只取参考答案正文
    cut = None
    for ln in ans.split("\n"):
        s = re.sub(r"[#*\s]", "", ln)
        if s and len(s) <= 30 and re.search(r"(?:阅卷细则|评分标准|评分细则)", s):
            cut = ans.find(ln)
            break
    if cut and cut > 200:
        head = re.sub(r"[#*\s]", "", ans[cut:cut + 40]).strip()[:16]
        review.append(f"该卷「参考答案」后还有一份评分附录（「{head}」…），"
                      f"answer 只取参考答案原文，附录未并入")
        ans = ans[:cut]

    def rewrite(t: str) -> str:
        return rewrite_images(t, md.parent, title, type_dir, review, SUBJECT)

    # ---- 一、选择题 ----
    h2 = HDR_P2.search(body)
    if h2:
        choice_body, essay_body = body[:h2.start()], body[h2.end():]
    else:
        first_big = next((m for m in BIGMARK.finditer(body) if int(m.group(2)) > MAX_CHOICE), None)
        choice_body = body[:first_big.start()] if first_big else body
        essay_body = body[first_big.start():] if first_big else ""
        review.append("卷面没有「第二部分」标题，按大题分值标记切出非选择题区")
    h1 = HDR_P1.search(choice_body)
    if h1:
        choice_body = choice_body[h1.end():]
    choice_body = re.sub(r"(?m)^[ \t]*\**\s*本部分共\s*\d+\s*题[^\n]*\n?", "", choice_body)

    choice_qs = parse_choice_part(choice_body, review)
    declared = None
    m = re.search(r"共\s*(\d{1,2})\s*题", body[:2000])
    if m:
        declared = int(m.group(1))
    if declared and len(choice_qs) != declared:
        review.append(f"选择题切到 {len(choice_qs)} 道（卷面声明 {declared} 道）")

    ans_choice_area = ans
    p2 = re.search(r"第[二2]部分", ans)
    if p2:
        ans_choice_area = ans[:p2.start()]
    key = parse_choice_answers(ans_choice_area)
    for q in choice_qs:
        q["answer"] = key.get(q["id"], "")
        if not q["answer"]:
            review.append(f"第{q['id']}题（选择题）缺答案")
        if len(q.get("options") or []) != 4:
            review.append(f"第{q['id']}题（选择题）选项不是 4 项（{len(q.get('options') or [])}）")
        qc = separate_images(unwrap_single_col_tables(q["content"], review, f"第{q['id']}题"))
        qc = flatten_icon_tables(qc, review, f"第{q['id']}题")
        qc = drop_tiny_images(qc, review, f"第{q['id']}题", md)
        q["content"] = re.sub(r"\n{3,}", "\n\n", rewrite(normalize_md_tables(qc)))
        for o in q["options"]:
            o["label"] = rewrite(latex_clean(o["label"]))
    choice_section = {
        "type": "选择题", "questionType": "politics-choice", "title": "一、选择题",
        "category": "选择", "score": CHOICE_SCORE * len(choice_qs),
        "questions": choice_qs,
    }

    # ---- 二、非选择题（一题一个 section） ----
    bmarks = [m for m in BIGMARK.finditer(essay_body)]
    if not bmarks:
        raise RuntimeError("没切到非选择题（第二部分）的大题")
    essays = []
    for i, m in enumerate(bmarks):
        nxt = bmarks[i + 1] if i + 1 < len(bmarks) else None
        essays.append({
            "no": int(m.group(2)),
            "score": int(m.group(3)),
            "lead": (m.group(1) or "").strip(),
            "text": essay_body[m.end():(nxt.start() if nxt else len(essay_body))],
        })

    ans_marks = [m for m in BIGMARK.finditer(ans) if not p2 or m.start() > p2.end()]
    ans_blocks: dict[int, str] = {}
    if ans_marks:
        printed = [int(m.group(2)) for m in ans_marks]
        want = [e["no"] for e in essays]
        ordered = printed == want
        for i, m in enumerate(ans_marks):
            nxt = ans_marks[i + 1] if i + 1 < len(ans_marks) else None
            no = m.group(2) and int(m.group(2))
            no = no if ordered else (want[i] if i < len(want) else no)
            ans_blocks[no] = ans[m.end():(nxt.start() if nxt else len(ans))].strip()
        if not ordered:
            review.append(f"参考答案区的大题编号被 Word 自动编号写乱（{printed}），"
                          f"已按出现顺序对照题面对应到 {want}")
        if len(ans_marks) != len(essays):
            review.append(f"参考答案区切到 {len(ans_marks)} 个大题块，题面有 {len(essays)} 道大题")

    cat_cache = OUT["root"] / ".cache" / f"{title}-cats.json"
    cat_items = []
    for e in essays:
        pre, subs = split_essay(e["text"])
        ask_all = " ".join(s["ask_raw"] for s in subs)
        cat_items.append((e["no"], ask_all, ans_blocks.get(e["no"], "")))
    cat_map = classify(cat_items, review, cat_cache, args.ai, args.force)

    sections = [choice_section]
    for e in essays:
        no, big = e["no"], e["score"]
        label = f"第{no}题"
        seg = e["text"]
        if e["lead"]:
            seg = e["lead"] + "\n" + seg
        preamble, subs = split_essay(seg)

        # 分值：先看小问里的（N分），再拿大题分值配平
        got = [sum(s["scores"]) for s in subs]
        if len(subs) == 1 and not got[0]:
            got = [big]
        elif sum(got) != big:
            if sum(got) == 0:
                per, rem = divmod(big, len(subs))
                got = [per + (1 if i < rem else 0) for i in range(len(subs))]
                review.append(f"{label}：小问没写分值，按大题 {big} 分均摊 → {got}")
            else:
                review.append(f"{label}：小问分值合计 {sum(got)} ≠ 大题标注 {big} 分，"
                              f"差额记到最后一问")
                got[-1] += big - sum(got)
                if got[-1] <= 0:
                    got[-1] = 1
                    review.append(f"{label}：配平后最后一问仍 ≤0，已改为 1 分，需人工核对")

        # 参考答案
        block, sub_ans = split_ans_block(ans_blocks.get(no, ""))
        if not block:
            review.append(f"{label}：参考答案区没切到这道题")
        mapped = into_questions(len(subs), sub_ans, [s["no"] for s in subs])
        if not mapped and block:
            mapped = {subs[0]["no"]: block}
        if sub_ans and len(sub_ans) != len(subs):
            review.append(f"{label}：参考答案有 {len(sub_ans)} 个小问块、题面有 {len(subs)} 个小问，"
                          f"已按顺序对齐，需人工核对")

        # 组装 section.content：材料只出现一次 + 各小问「设问 + <AnswerBox>」（版式不变，单栏内联）
        parts: list[str] = list(preamble)
        qs = []
        n = len(subs)
        for i, s in enumerate(subs):
            score_i = got[i] if i < len(got) else 1
            if i == n - 1 and s["material"]:     # 最后一问后面的材料接在它的设问之前
                parts.extend(s["material"])
            ask = SCORE_RE.sub("", s["ask_raw"]).strip()
            ask = re.sub(r"[ \t\u3000]+", " ", ask).strip()
            parts.append(ask + ph_tag(score_i))
            if i < n - 1 and s["material"]:      # 两问之间的材料接在上一问的答题框之后
                parts.extend(s["material"])
            ans_i = clean_answer(mapped.get(s["no"], ""), score_i, review, label)
            if not ans_i:
                review.append(f"{label} 第（{s['no']}）问：没有参考答案，需人工补")
            qs.append({"id": no * 100 + i + 1, "questionType": "input",
                       "content": clean_text(ask),
                       "answer": rewrite(ans_i), "analysis": "", "score": score_i})

        content = "\n\n".join(p.strip() for p in parts if p and p.strip())
        content = clean_text(content)
        content = separate_images(unwrap_single_col_tables(content, review, label))
        content = flatten_icon_tables(content, review, label)
        content = drop_tiny_images(content, review, label, md)
        content = re.sub(r"\n{3,}", "\n\n", rewrite(normalize_md_tables(content)))
        cat = cat_map.get(no, DEFAULT_CAT)
        if cat not in MODULES:
            review.append(f"{label}：分类「{cat}」不合法 → 落 {DEFAULT_CAT}")
            cat = DEFAULT_CAT
        sections.append({
            "type": "非选择题", "questionType": "politics-fill",
            "title": f"二、非选择题（第{no}题）", "category": cat,
            "score": sum(q["score"] for q in qs), "content": content, "questions": qs,
        })

    year = re.search(r"(20\d{2})", title)
    total = sum(s["score"] for s in sections)
    if total != PAPER_TOTAL:
        review.append(f"全卷分值合计 {total} 分（北京卷应为 {PAPER_TOTAL} 分），请核对")
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": total or PAPER_TOTAL, "duration": 90,
        "tags": [SUBJECT, args.grade, type_dir] + ([year.group(1)] if year else []),
    }
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)      # 出口约定：选择题答案与选项 id 一律大写
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text(
            "\n".join(_rev_line(x) for x in review) + "\n", encoding="utf-8")
    n_essay = len(sections) - 1
    n_sub = sum(len(s["questions"]) for s in sections[1:])
    return (f"✓ {title}：选择 {len(choice_qs)} 题 / 大题 {n_essay} 道 / 小问 {n_sub} 个"
            f" / 卷面 {total} 分 → {dst.name}")


def latex_clean(t: str) -> str:
    """Word 残留的 <sub>/<sup> 与转义符号收拾干净（政治卷基本用不到公式，保底）。"""
    if not t:
        return t
    t = re.sub(r"<su[bp]>\s*([.．、])\s*</su[bp]>", r"\1", t)
    t = t.replace("\\_\\_", "_")
    return re.sub(r"[ \t\u3000]{2,}", " ", t).strip()


def _rev_line(x: str) -> str:
    """review 每条一行，前缀「复核：」（契约要求）。"""
    x = x.strip()
    return x if x.startswith("复核：") else "复核：" + x


# ──────────────────────────────────────────────────────────────────
# 自查
# ──────────────────────────────────────────────────────────────────

def _exam_images_root() -> Path:
    return OUTPUT_DOCX_TO_MD


def _source_md_of(jf: Path) -> Path | None:
    d = OUTPUT_DOCX_TO_MD / jf.parent.name / SUBJECT / f"{jf.stem}（教师版）"
    md = d / f"{jf.stem}（教师版）.md"
    return md if md.exists() else None


def dup_paragraphs(text: str) -> list[str]:
    """同一 section 里重复出现的成段文字（材料重复导入的探针）。"""
    seen, dups = {}, []
    for p in split_paras(PH.sub("", text or "")):
        key = _norm_cmp(p)
        if len(key) < 20:
            continue
        if key in seen:
            dups.append(p[:40])
        seen[key] = 1
    return dups


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    errs, notes = [], []
    data = json.loads(jf.read_text(encoding="utf-8"))
    paper = data.get("paper") or {}
    sections = data.get("sections") or data.get("groups") or []
    # 旧链路（`groups` + questionType=politics，按模块分组）的历史卷：本链路不再产出，
    # 生产库里保留不动，这里只提示、不做契约断言。
    if not data.get("sections") or any((s.get("questionType") or "") == "politics" for s in sections):
        notes.append("该卷是旧 politics 组型（groups + 按模块分组）的历史存量，"
                     "本链路不再产出，保留不动；契约校验跳过")
        return ({"title": paper.get("title") or jf.stem, "choice": 0, "essay": 0, "subs": 0,
                 "tables": 0, "imgs": set(), "sections": [], "declared": (None, None),
                 "matlen": []}, errs, notes)
    stat = {"title": paper.get("title"), "choice": 0, "essay": 0, "subs": 0, "tables": 0,
            "imgs": set(), "sections": [], "declared": None, "matlen": []}
    src_md = _source_md_of(jf)
    declared_choice = declared_essay = None
    if src_md is not None:
        try:
            whole = src_md.read_text(encoding="utf-8")
            body = split_paper(whole)[0]
            for m in re.finditer(r"本部分共\s*(\d+)\s*题", body):
                if "共15题" in m.group(0) or declared_choice is None:
                    declared_choice = declared_choice or int(m.group(1))
                else:
                    declared_essay = declared_essay or int(m.group(1))
            ms = list(re.finditer(r"本部分共\s*(\d+)\s*题", body))
            if len(ms) >= 2:
                declared_choice, declared_essay = int(ms[0].group(1)), int(ms[1].group(1))
        except Exception:
            pass
    stat["declared"] = (declared_choice, declared_essay)

    if paper.get("subject") != SUBJECT:
        errs.append(f"paper.subject 不是政治：{paper.get('subject')}")
    for k in ("title", "source", "grade", "year", "totalScore", "duration", "tags"):
        if k not in paper:
            errs.append(f"paper 缺字段 {k}")
    sec_sum = sum(s.get("score") or 0 for s in sections)
    if paper.get("totalScore") != sec_sum:
        errs.append(f"paper.totalScore={paper.get('totalScore')} ≠ 各 section 分值合计 {sec_sum}")

    choice_secs = [s for s in sections if s.get("type") == "选择题"]
    essay_secs = [s for s in sections if s.get("type") != "选择题"]
    if len(choice_secs) != 1:
        errs.append(f"选择题 section 应恰好 1 个，实际 {len(choice_secs)}")
    for s in choice_secs:
        qs = s.get("questions") or []
        stat["choice"] = len(qs)
        if s.get("questionType") != "politics-choice":
            errs.append(f"选择题 questionType = {s.get('questionType')}（应为 politics-choice）")
        if s.get("category") != "选择":
            errs.append("选择题 category 应为 选择")
        if declared_choice and len(qs) != declared_choice:
            errs.append(f"选择题 {len(qs)} 道 ≠ 卷面声明 {declared_choice} 道")
        elif not declared_choice and len(qs) != N_CHOICE:
            notes.append(f"选择题 {len(qs)} 道（常规 {N_CHOICE} 道）")
        if s.get("score") != CHOICE_SCORE * len(qs):
            errs.append(f"选择题 score={s.get('score')}，与 {CHOICE_SCORE}×{len(qs)} 不符")
        nos = [q.get("id") for q in qs]
        if nos != list(range(1, len(qs) + 1)):
            errs.append(f"选择题题号不是 1..{len(qs)} 连续：{nos}")
        for q in qs:
            if q.get("score") != CHOICE_SCORE:
                errs.append(f"选择题第{q.get('id')}题 score={q.get('score')}（应为 {CHOICE_SCORE}）")
            if q.get("questionType") != "single":
                errs.append(f"选择题第{q.get('id')}题 questionType={q.get('questionType')}")
            opts = q.get("options") or []
            if [o.get("id") for o in opts] != ["A", "B", "C", "D"]:
                errs.append(f"选择题第{q.get('id')}题选项 id 不是 A–D 四项：{[o.get('id') for o in opts]}")
            optids = {o.get("id") for o in opts}
            ans = str(q.get("answer") or "")
            if not re.fullmatch(r"[A-D]", ans):
                errs.append(f"选择题第{q.get('id')}题 answer={ans!r}（应为单个大写字母）")
            elif ans not in optids:
                errs.append(f"选择题第{q.get('id')}题 answer={ans} 不在 options 里")
            if "analysis" not in q:
                errs.append(f"选择题第{q.get('id')}题缺 analysis")
            if not (q.get("content") or "").strip():
                errs.append(f"选择题第{q.get('id')}题 content 为空")
            for o in opts:
                if not (o.get("label") or "").strip():
                    errs.append(f"选择题第{q.get('id')}题选项 {o.get('id')} label 为空")
            stat["tables"] += count_md_tables(q.get("content") or "")
            for e in table_shape_ok(q.get("content") or ""):
                errs.append(f"选择题第{q.get('id')}题表格：{e}")
        stat["sections"].append(("选择题", "选择", len(qs), s.get("score"),
                                 sum(q.get("score") or 0 for q in qs)))

    if not essay_secs:
        errs.append("没有非选择题 section")
    if declared_essay and len(essay_secs) != declared_essay:
        errs.append(f"非选择题 {len(essay_secs)} 道 ≠ 卷面声明 {declared_essay} 道")
    essay_nos, ids = [], set()
    for s in essay_secs:
        qs = s.get("questions") or []
        stat["essay"] += 1
        stat["subs"] += len(qs)
        content = s.get("content") or ""
        if s.get("questionType") != "politics-fill":
            errs.append(f"{s.get('title')}：questionType={s.get('questionType')}（应为 politics-fill）")
        if s.get("category") not in MODULES:
            errs.append(f"{s.get('title')}：category={s.get('category')!r} 不在 {MODULES}")
        # ★ 占位符：一律 <AnswerBox></AnswerBox>，个数 == 小问数；旧写法残留必须为 0
        n_box = len(BOX.findall(content))
        if n_box != len(qs):
            errs.append(f"{s.get('title')}：<AnswerBox> {n_box} 个，questions {len(qs)} 个")
        for bad in ("<Input>", "<InputLong", "<Input "):
            if bad in content:
                errs.append(f"{s.get('title')}：section.content 里还有旧占位符 {bad}"
                            f"（共 {content.count(bad)} 处，应全部换成 <AnswerBox>）")
        if not content.strip():
            errs.append(f"{s.get('title')}：section.content 为空（材料没切到）")
        ssum = sum(q.get("score") or 0 for q in qs)
        if ssum != s.get("score"):
            errs.append(f"{s.get('title')}：Σquestions.score={ssum} ≠ section.score={s.get('score')}")
        if s.get("score") in (None, 0):
            errs.append(f"{s.get('title')}：section.score 为 0")
        # ★ 红线：材料只出现一次
        dups = dup_paragraphs(content)
        if dups:
            errs.append(f"{s.get('title')}：content 里同一段材料出现了多次（{'；'.join(dups[:2])}…）")
        # ★ 每个小问的设问都要在 content 里内联出现；材料不得被抄进 questions.content
        paras = split_paras(content)
        mat_norms = [_norm_cmp(PH.sub("", p)) for p in paras if not BOX.search(p)]
        ask_norms = [_norm_cmp(PH.sub("", p)) for p in paras if BOX.search(p)]
        for q in qs:
            qk = _norm_cmp(PH.sub("", q.get("content") or ""))
            if len(qk) < 12:
                continue
            if not any(qk in a or a in qk for a in ask_norms):
                errs.append(f"{s.get('title')} id={q.get('id')}：设问在 section.content 里找不到")
            for mk in mat_norms:
                if len(mk) >= 25 and mk and mk in qk:
                    errs.append(f"{s.get('title')} id={q.get('id')}：questions.content 里复述了材料")
        stat["matlen"].append((s.get("title"), len(PH.sub("", content)),
                               len(qs), len(BOX.findall(content))))
        stat["tables"] += count_md_tables(content)
        for e in table_shape_ok(content):
            errs.append(f"{s.get('title')}：表格{e}")
        title_no = re.search(r"第\s*(\d{1,2})\s*题", s.get("title") or "")
        essay_nos.append(int(title_no.group(1)) if title_no else None)
        for q in qs:
            if q.get("questionType") != "input":
                errs.append(f"{s.get('title')} id={q.get('id')} questionType={q.get('questionType')}"
                            f"（应为 input）")
            if not (q.get("answer") or "").strip():
                errs.append(f"{s.get('title')} id={q.get('id')} 答案为空")
            if not (q.get("score") or 0) > 0:
                errs.append(f"{s.get('title')} id={q.get('id')} score={q.get('score')}（应 >0）")
            if not (q.get("content") or "").strip():
                errs.append(f"{s.get('title')} id={q.get('id')} content 为空")
            if q.get("id") in ids:
                errs.append(f"重复 id {q.get('id')}")
            ids.add(q.get("id"))
            for e in table_shape_ok(q.get("answer") or ""):
                errs.append(f"{s.get('title')} id={q.get('id')} answer 里的表格{e}")
        stat["sections"].append((s.get("title"), s.get("category"), len(qs), s.get("score"), ssum))

    if declared_essay is None and essay_nos:
        want = list(range(16, 16 + len(essay_nos)))
        if [x for x in essay_nos if x] != want:
            notes.append(f"大题题号 {essay_nos}（常规 {want}）")

    # 图片逐张落到磁盘
    raw = json.dumps(data, ensure_ascii=False)
    for ref in sorted(set(EXAM_IMG.findall(raw))):
        stat["imgs"].add(ref)
        if not (_exam_images_root() / ref).exists():
            errs.append(f"图片缺失：/exam-images/{ref}")
    if not stat["imgs"]:
        notes.append("该卷 JSON 里没有任何图片引用")
    return stat, errs, notes


def run_check(args) -> int:
    types = [t for t in (args.type or "").replace("，", ",").split(",") if t] or ["一模"]
    root = Path(args.output)
    bad, blocks, summary = 0, [], []
    for type_dir in types:
        files = sorted((root / type_dir).glob("*.json"))
        if not files:
            print(f"没有找到 JSON：{root / type_dir}")
            continue
        for jf in files:
            try:
                stat, errs, notes = check_one(jf)
            except Exception as e:
                stat, errs, notes = ({"title": jf.stem, "choice": 0, "essay": 0, "subs": 0,
                                      "tables": 0, "imgs": set(), "sections": [],
                                      "declared": (None, None), "matlen": []},
                                     [f"解析该 JSON 失败：{str(e)[:120]}"], [])
                print(f"\n===== {type_dir}/{jf.name} =====")
                print(f"  ✗ 解析失败：{str(e)[:160]}")
            rev = jf.with_suffix(".review.txt")
            review = [x for x in rev.read_text(encoding="utf-8").splitlines() if x.strip()] if rev.exists() else []
            manual = [x for x in review
                      if not re.match(r"复核：(?:分类：|该卷|选择题题号不规整|第\d+题段末的共享材料)", x)]
            rows = "\n".join(f"| {t} | {c or '—'} | {n} | {sc} | {ss} |"
                             for t, c, n, sc, ss in stat["sections"])
            head = (f"## {stat['title']}\n\n"
                    f"- 类型：{type_dir}　文件：`{jf}`\n"
                    f"- 选择题：**{stat['choice']} 题 × {CHOICE_SCORE} 分 = "
                    f"{stat['choice'] * CHOICE_SCORE} 分**"
                    f"（卷面声明 {stat['declared'][0] or '—'} 题）\n"
                    f"- 非选择题：**{stat['essay']} 道大题，{stat['subs']} 个小问**"
                    f"（卷面声明 {stat['declared'][1] or '—'} 题）\n"
                    f"- markdown 表格：**{stat['tables']} 张**（分隔行唯一、行列数与表头一致）\n"
                    f"- 图片引用：**{len(stat['imgs'])} 张**，逐张 os.path.exists 校验\n"
                    f"- 材料字数 / 小问数 / `<AnswerBox>` 数：" + "，".join(
                        f"{t} {m} 字/{n} 问/{b} 框" for t, m, n, b in stat["matlen"]) + "\n\n")
            tbl = ("| section | category | 小问数 | section.score | Σquestions.score |\n"
                   "| --- | --- | --- | --- | --- |\n" + rows + "\n")
            skipped = any("旧 politics 组型" in x for x in notes)
            verdict = ("\n**契约校验：✗ 有错误**\n\n" + "\n".join(f"- {e}" for e in errs) + "\n"
                       if errs else
                       "\n**契约校验：— 跳过**（旧 politics 组型的历史存量卷，保留不动）\n"
                       if skipped else
                       "\n**契约校验：✓ 通过**（json.load / 选项恒 A–D / answer 单个大写字母且在 options 里 / "
                       "<AnswerBox> 数 == 小问数 / <Input(<InputLong>) 残留 0 / "
                       "设问在 content 里内联 / 小问 questionType 一律 input / Σscore == section.score / "
                       "每问 content·answer 非空且 score>0 / 材料只出现一次 / 表格可解析 / "
                       "图片全部存在 / category 在 7 类里）\n")
            man = ("\n**需人工核对的点**\n\n" + "\n".join(f"- {x}" for x in manual) + "\n"
                   if manual else "\n**需人工核对的点**：无\n")
            blocks.append(head + verdict + tbl + man)
            summary.append((type_dir, stat["title"], stat["choice"], stat["essay"], stat["subs"],
                            stat["tables"], len(stat["imgs"]), len(errs), len(manual)))
            if errs:
                bad += 1
            print(f"\n===== {type_dir}/{jf.name} =====")
            print(f"选择题 {stat['choice']} 题 / 大题 {stat['essay']} 道 / 小问 {stat['subs']} 个"
                  f" / 表格 {stat['tables']} 张 / 图片 {len(stat['imgs'])} 张")
            for t, c, n, sc, ss in stat["sections"]:
                print(f"   {t}：{c}　{n} 问　{sc} 分（Σ{ss}）")
            for t, m, n, b in stat["matlen"]:
                print(f"     材料：{t} → {m} 字 / {n} 问 / <AnswerBox>×{b} / <Input 残留 0")
            print("  — 契约校验跳过（旧 politics 组型历史卷）" if skipped else
                  ("  ✓ 契约校验通过" if not errs else f"  ✗ 错误 {len(errs)} 条："))
            for e in errs:
                print(f"     - {e}")
            for n in notes:
                print(f"  · 提示：{n}")
    for s in summary:
        print(f"  [{s[0]}] {s[1]}：选择 {s[2]} / 大题 {s[3]} / 小问 {s[4]} / 表格 {s[5]}"
              f" / 图片 {s[6]} / 错误 {s[7]} / 人工点 {s[8]}")
    if args.report:
        top = (f"# 政治 {len(summary)} 套卷 · 解析自查报告（{'、'.join(types)}）\n\n"
               "生成方式：`venv/bin/python3 convert_politics_md.py --check -T 一模 --report …`\n\n"
               "| 类型 | 卷 | 选择题 | 大题 | 小问 | 表格 | 图片 | 契约错误 | 人工点 |\n"
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
               + "\n".join(f"| {s[0]} | {s[1]} | {s[2]} 题 | {s[3]} 道 | {s[4]} | {s[5]} | {s[6]} | "
                           f"{s[7]} | {s[8]} |" for s in summary) + "\n\n")
        Path(args.report).write_text(top + "\n".join(blocks), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    print(f"\n合计 {len(summary)} 份，出错 {bad} 份")
    return 0 if bad == 0 else 2


# ──────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="政治整卷 → 题库 JSON（生物式：一题一 section）")
    ap.add_argument("-p", "--path", help="单份 MD 路径（或卷目录）")
    ap.add_argument("--dir", default=str(OUTPUT_DOCX_TO_MD), help="MD 根目录")
    ap.add_argument("-T", "--type", default="一模",
                    help="试卷类型（输出子目录名；--check 时可写 all 或逗号分隔多个）")
    ap.add_argument("-s", "--subject", default=SUBJECT, help="学科（默认 政治）")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--papers", nargs="*", default=None, help="只处理这几套（按卷名片段匹配）")
    ap.add_argument("--force", action="store_true", help="忽略已有 JSON 与 AI 缓存，重跑")
    ap.add_argument("--no-ai", dest="ai", action="store_false",
                    help="不调模型（分类走缓存/默认标签）")
    ap.add_argument("--no-ai-analysis", dest="ai", action="store_false", help="--no-ai 的别名")
    ap.add_argument("--check", action="store_true", help="只做自查（纯 Python，不调模型）")
    ap.add_argument("--report", default=None, help="自查报告输出路径（配合 --check）")
    args = ap.parse_args()

    if args.check:
        if args.type == "all":
            root = Path(args.output)
            args.type = ",".join(sorted(p.name for p in root.iterdir() if p.is_dir()
                                        and p.name != ".cache")) or "一模"
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
