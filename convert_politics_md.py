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
from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_POLITICS, REPO_ROOT
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
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、·]\s*\**"
)
# 分节标题：第一部分 / 第二部分（可带 # 与 **）；
# 老卷（2011–2016 文综）写的是「一、选择题」「二、非选择题 / 解答题 / 综合题」，2016 又写成
# 「二、第二部分（非选择题）」，2022 是「一、本部分共15题，每题3分…」——三种写法都要认，
# 否则「非选择题区」根本切不出来（旧版只认「第二部分」）。
HDR_P1 = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\**\s*(?:第[一1]部分|[一1][、.][ \t]*\**\s*(?:选择题|单项选择题|本部分))"
)
HDR_P2 = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\**\s*"
    r"(?:第[二2]部分|[二2][、.][ \t]*\**\s*(?:非选择题|解答题|综合题|第二部分))"
)
# 分节标题里带的分值信息：`每小题4分` / `每题3分`；`满分52分` / `共55分`
HDR_PER_SCORE = re.compile(r"每[题小]\s*题?\s*(\d+)\s*分")
HDR_TOTAL_SCORE = re.compile(r"满\s*分\s*(\d+)\s*分|共\s*(\d+)\s*分")
# 分节标题里的题量：`本部分共15题` / `共12小题` / `一、单项选择题，共12题`
HDR_COUNT = re.compile(r"共\s*(\d+)\s*[题小]")
# 大题题号 + 分值：`16．（7分）` `16\. (8 分)` `## 18.(14 分)`
BIGMARK = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、]\s*\**\s*"
    r"[（(]\s*(\d+)\s*分\s*[）)]"
)
# 小问编号行：`（1）` `(2)`（行首，允许 ** 包裹）
SUBQ = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?[（(]\s*(\d{1,2})\s*[）)]\s*")
# 分值括号
SCORE_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")
# 参考答案区的大题标记：`18．（11分）` / `18\. (11分)` / `17 (14分)`（题号后没有点号的卷面）
ANSMARK = re.compile(r"(?m)^[ \t]*\**\s*(\d{1,2})\s*[．.、]?\s*[（(]\s*(\d+)\s*分\s*[）)]")
# 选项标记：`A．` `A.` `A、`（前后允许空白/中文）
OPT_MARK = re.compile(
    r"(?:^|(?<=[\s\u3000\u00a0>)\u4e00-\u9fff①②③④⑤⑥⑦⑧⑨⑩|*]))([A-D])\s*"
    r"(?:[．.、]\s*|(?=[①②③④⑤⑥⑦⑧⑨⑩（(])|(?=\|))"   # 挤在一起/点号丢了/表格首列都要认
)
# 「回答第13、14题」这类共享材料引导句
SHARED = re.compile(r"据此|据图|读图|如图|回答\s*第?\s*([\d\s、,，和及~～至\-－]+)\s*小?题")
IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
EXAM_IMG = re.compile(r"/exam-images/((?:[^()\"'\s]|\([^()\s]*\))+)")
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
            # 单行「表格」= Word 排版残渣（只剩 `|`），硬留会变成分隔行缺失的坏表
            for r in rows:
                txt = " ".join(c for c in r if c.strip())
                if txt.strip():
                    out.append(txt.strip())
            out.append("")
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
    text = re.sub(r"!\[([^\]]*)\]", lambda m: "![" + re.sub(r"\s+", " ", m.group(1)) + "]", text)
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


LETTER_PATTERNS = (
    r"【答案】\s*\**\s*([A-D])(?![A-Za-z])",
    r"【解答】\s*\**\s*([A-D])(?![A-Za-z])",
    r"故(?:本)?题?选\s*[:：]?\s*\**\s*([A-D])(?![A-Za-z])",
    r"故选\s*[:：]?\s*\**\s*([A-D])(?![A-Za-z])",
    r"答案(?:应)?(?:为|是)\s*[:：]?\s*\**\s*([A-D])(?![A-Za-z])",
    r"(?:应该)?选择?\s*\**\s*([A-D])(?![A-Za-z])",
    r"应选\s*\**\s*([A-D])(?![A-Za-z])",
    r"([A-D])\s*(?:项)?(?:符合题意|正确且符合题意|当选)",
    r"选项\s*([A-D])\s*正确",
)


def choice_letter_of(seg: str) -> str:
    """从一个选择题答案块里取「最终答案字母」。

    教师版卷面写法五花八门：`【答案】C` / `【解答】A` / `故选：D` / `综上所述，应该选择A` /
    `B符合题意。A、C、D不合题意。` —— 逐个模式找，先看块尾（结论通常在结尾）。
    """
    if not seg:
        return ""
    tail = seg[-600:]
    for pat in LETTER_PATTERNS:
        m = re.search(pat, tail)
        if m:
            return m.group(1).upper()
    for pat in LETTER_PATTERNS:
        m = re.search(pat, seg)
        if m:
            return m.group(1).upper()
    return ""


def parse_choice_answers(ans: str, hi: int = MAX_CHOICE) -> dict[int, str]:
    """选择题答案：三种形态都吃。

    ① 字母串：`1．B 2．C`、`1\\. C 2. B`、无分隔符的 `1.B2.A3.D`；
    ② 逐题块（菁优/学科网教师版）：`1．【答案】C`；
    ③ 逐题块 + 解析：`1．【分析】…【解答】…故选：D。` / `故本题选B` / `故答案为C项`——
    老文综卷（2011–2019）只有第 ③ 种，只认第 ① 种会把整卷选择题答案全丢。
    """
    out: dict[int, str] = {}

    def put(n: int, letter: str, force: bool = False):
        if 1 <= n <= hi:
            if force or n not in out:
                out[n] = letter.upper()

    # ① 字母串（题号紧跟字母）：题号被 Word 写成 1./6./重复 时，按「下一个该有的题号」补齐
    expect = 1
    for m in re.finditer(r"(\d{1,2})\s*\\?[.．、]\s*\**\s*([A-D])(?![A-Za-z])", ans):
        n, letter = int(m.group(1)), m.group(2)
        if n in out and expect not in out and expect <= hi:
            put(expect, letter)
            expect += 1
        elif n not in out:
            put(n, letter)
            expect = max(expect, n + 1)
    # ①b 没编号的「【答案】X」（题号被吃掉）
    for m in re.finditer(r"【答案】\s*\**\s*([A-D])(?![A-Za-z])", ans):
        while expect in out and expect <= hi:
            expect += 1
        if 1 <= expect <= hi:
            put(expect, m.group(1))
            expect += 1
    # ①c 行首孤零零的字母（「A 2. C 3. C」「… 10. D⏎B 12. A」里被吃掉题号的那些）：
    #     按文档顺序补给「还缺的题号」（从前往后）
    bare = [m.group(1).upper() for m in
            re.finditer(r"(?m)^[ \t]*\**\s*([A-D])(?![A-Za-z])"
                        r"(?=\s*(?:$|\n|\**\s*\d{1,2}\s*[.．、]))", ans)]
    if bare:
        missing = [n for n in range(1, hi + 1) if n not in out and n <= max([k for k in out] or [0]) + len(bare)]
        for n, letter in zip(missing, bare):
            put(n, letter)

    # ② / ③ 按题号块切，再从块里找「【答案】X」或「故选：X」
    marks = [m for m in QMARK.finditer(ans) if 1 <= int(m.group(2)) <= hi]
    top = [m for m in marks
           if re.match(r"[ \t]*(?:\*\*)?(?:【|[A-D](?![A-Za-z])|（\s*\d+\s*分)",
                       ans[m.end():m.end() + 8])]
    if len(top) >= 2:
        marks = top
    for i, m in enumerate(marks):
        n = int(m.group(2))
        seg = ans[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(ans))]
        letter = choice_letter_of(seg)
        if letter:
            put(n, letter)
    if len(out) < hi:
        for n, letter in parse_choice_answer_tables(ans).items():
            put(n, letter)
    if len(out) < hi:
        for n, letter in parse_choice_answer_plain_table(ans).items():
            put(n, letter)
    if len(out) < hi:
        for n, letter in parse_choice_answer_number_rows(ans).items():
            put(n, letter)
    if len(out) < hi:
        # 紧凑写法：`1-15AAABD ADCAD CCBCC`
        for m in re.finditer(r"(\d{1,2})\s*[-~—－至]\s*(\d{1,2})\s*[:：]?\s*((?:[A-D][\s]*){5,})",
                             ans):
            a, b = int(m.group(1)), int(m.group(2))
            letters = re.findall(r"[A-D]", m.group(3))
            if b - a + 1 <= len(letters):
                for i, letter in enumerate(letters[:b - a + 1]):
                    put(a + i, letter)
    return out


def parse_choice_answer_number_rows(ans: str) -> dict[int, str]:
    """答案区写成「一行题号 + 一行答案」（没写「题号/答案」字样，2023 丰台实测）：

        1 2 3 4 5 6 7 8 9 10
        B C D C B A C D B C
        11 12 13 14 15
        D D A D A
    """
    out: dict[int, str] = {}
    lines = [l.strip() for l in (ans or "").split("\n") if l.strip()]
    i = 0
    while i < len(lines) - 1:
        nums = re.findall(r"\d{1,2}", lines[i])
        if len(nums) >= 5 and re.fullmatch(r"[\d\s、,，]+", lines[i]):
            lets = re.findall(r"[A-D](?![A-Za-z])", lines[i + 1])
            if len(lets) == len(nums) and re.fullmatch(r"[A-D\s、,，]+", lines[i + 1]):
                for n, letter in zip(nums, lets):
                    out.setdefault(int(n), letter.upper())
                i += 2
                continue
        i += 1
    return out


def parse_choice_answer_plain_table(ans: str) -> dict[int, str]:
    """无竖线的「题号/答案」两行表（joplin 转出来的 2023 卷面常见）：

        题号 1 2 3 4 5 6 7 8
        答案 B B C B D A B A

    也兼容「题号：1 2 3」「**答案**：A B C」这类写法。
    """
    out: dict[int, str] = {}
    lines = [l.strip().strip("*").strip() for l in (ans or "").split("\n")]
    for i, line in enumerate(lines):
        m = re.match(r"^题\s*号[\s:：\|]*(.+)$", line)
        if not m:
            continue
        nums = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
        if not nums:
            continue
        for j in range(i + 1, min(i + 6, len(lines))):
            mm = re.match(r"^答\s*案[\s:：\|]*(.+)$", lines[j])
            if not mm:
                continue
            lets = [x.upper() for x in re.findall(r"[A-D](?![A-Za-z])", mm.group(1))]
            if len(lets) == len(nums):
                for n, letter in zip(nums, lets):
                    out.setdefault(n, letter)
            break
    return out


def parse_choice_answer_tables(ans: str) -> dict[int, str]:
    """选择题答案排成表格（西城/海淀/通州/房山二模…全是这种）：

        | 题号 | 1 | 2 | 3 | … |        | 1 | 2 | 3 | …
        | 答案 | B | A | A | … |        | B | A | A | …      （无「题号/答案」字样）

    两种都吃，另外兼容「一行一题」的竖表。
    """
    out: dict[int, str] = {}
    blocks, cur = [], []
    for line in (ans or "").split("\n"):
        if TBL_LINE.match(line):
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    def is_l(c):
        return bool(re.fullmatch(r"[A-D]", (c or "").strip().upper()))

    for block in blocks:
        rows = [[_clean_cell(c) for c in _cell_split(l)] for l in block
                if not _is_sep_row(_cell_split(l))]
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            continue
        # 题号行 + 答案行成对出现（一张表里可能连排好几段，东城/房山二模就是两段）
        for i in range(len(rows) - 1):
            head, body_r = rows[i], rows[i + 1]
            head_lab = any("题号" in c for c in head[:2])
            body_lab = any("答案" in c for c in body_r[:2])
            nums = [int(c) for c in head if re.fullmatch(r"\d{1,2}", c.strip())]
            lets = [c.strip().upper() for c in body_r if is_l(c)]
            if not nums or not lets or len(nums) != len(lets):
                continue
            labelled = head_lab or body_lab
            same_shape = len(head) == len(body_r)
            if labelled or same_shape:
                for n, letter in zip(nums, lets):
                    out.setdefault(n, letter)
        # 竖表：一行一题
        for r in rows:
            if len(r) >= 2 and re.fullmatch(r"\d{1,2}", (r[0] or "").strip()) and is_l(r[1]):
                out.setdefault(int(r[0]), r[1].strip().upper())
    return out


# ──────────────────────────────────────────────────────────────────
# 表格感知的选项切分（方案 A）
#   判据与前端一致：**表头行 + 分隔行**才算一张 GFM 表格
#   （~/gxexercise/src/lib/pdf/markdown.ts:254-256）；只有竖线的脏行一律不算表格，
#   题干正文里的 `|` 不会被误吃。认不出完整 A–D 就回落到原逻辑（宁可不修，不许误伤）。
# ──────────────────────────────────────────────────────────────────
PT_TBL_ROW = re.compile(r"^\s*\|.*\|\s*$")
PT_TBL_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 一个单元格里可能塞好几个选项（`| A.①② B.①④ C.②③ D.③④ |`，2024 通州期末 4 题）
PT_CELL_OPT = re.compile(r"(?:^|(?<=[\s\u3000|]))([A-D])\s*[．.、]\s*")
# 选项对照残渣整行（`A．A B．B C．C D．D`，2025 顺义一模/期末 7 题）
PT_OPT_ECHO = re.compile(r"^(?:[A-D]\s*[．.、]\s*[A-D]\s*)+$")
# 同一残渣粘在 label 末尾（`… D. ④ A. A B. B C. C D. D`）：字母列导出后被并进最后一项
PT_OPT_ECHO_TAIL = re.compile(r"(?:^|\s)(?:[A-D]\s*[．.、]\s*[A-D]\s*)+$")


def _pt_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _pt_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """段内所有 GFM 表格块 → [(start, end)) 半开区间（表头行 + 分隔行 + 数据行）。"""
    out, i, n = [], 0, len(lines)
    while i < n - 1:
        if PT_TBL_ROW.match(lines[i]) and PT_TBL_SEP.match(lines[i + 1]):
            j = i + 2
            while j < n and PT_TBL_ROW.match(lines[j]):
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _pt_opts_from_block(block: list[str]):
    """表格本身就是 A–D 选项 → (options, 用掉的行号集合)；认不出来就 (None, set())。

    覆盖三种版式：
      L3 首列字母   `| A | 观点 | 评析 |` 每行一选项（2024 昌平期末 3 题、2024/2025 顺义 7 题、
                    2026 顺义一模 5 题的 `| A. | … | … |`）——label 取全部单元格用「；」连接；
      L1 一格多选项 `| A.①② B.①④ C.②③ D.③④ |  |  |  |`（2024 通州期末 4 题）。
    只认「末尾四行」的首列字母，是为了让表头行留给 _pt_drop_empty_blocks 收尾，不漏进题干。
    """
    rows: list[tuple[int, list[str]]] = []
    for k, ln in enumerate(block):
        if PT_TBL_SEP.match(ln):
            continue
        rows.append((k, _pt_cells(ln)))
    if not rows:
        return None, set()
    tail4 = rows[-4:]
    bare = [re.sub(r"[\s．.、]", "", cells[0]) if cells else "" for _, cells in tail4]
    if len(tail4) == 4 and bare == list("ABCD"):
        opts = [{"id": L, "label": "；".join(c for c in cells[1:] if c)}
                for L, (_, cells) in zip("ABCD", tail4)]
        if all(o["label"] for o in opts):
            return opts, {k for k, _ in tail4}
    lead = [cells[0] if cells else "" for _, cells in tail4]
    if len(tail4) == 4 and all(re.match(rf"^{L}(?![A-Za-z])", t) for L, t in zip("ABCD", lead)):
        opts = []
        for L, (_, cells) in zip("ABCD", tail4):
            body = re.sub(r"^[A-D]\s*[．.、]?\s*", "", cells[0]).strip()
            parts = ([body] if body else []) + [c for c in cells[1:] if c]
            if not parts:
                return None, set()
            opts.append({"id": L, "label": "；".join(parts)})
        return opts, {k for k, _ in tail4}
    got: dict[str, str] = {}
    used: set[int] = set()
    for k, cells in rows:
        joined = "|".join(cells)
        marks = list(PT_CELL_OPT.finditer(joined))
        for n, m in enumerate(marks):
            end = marks[n + 1].start() if n + 1 < len(marks) else len(joined)
            L = m.group(1)
            txt = re.sub(r"\s+", " ", joined[m.end():end]).strip(" |")
            if L in got or not txt:
                continue
            got[L] = txt
            used.add(k)
    if sorted(got) == list("ABCD"):
        return [{"id": L, "label": got[L]} for L in "ABCD"], used
    return None, set()


def _pt_drop_empty_blocks(lines: list[str]) -> list[str]:
    """摘掉选项行后只剩「表头 + 分隔行」的空壳表格。"""
    out = list(lines)
    while True:
        for s, e in _pt_blocks(out):
            if not [ln for ln in out[s + 1:e] if not PT_TBL_SEP.match(ln)]:
                del out[s:e]
                break
        else:
            return out


def _pt_scrub_orphan_sep(lines: list[str]) -> list[str]:
    """分隔行只有紧跟在表格行后面才是真表格的一部分，否则是残渣。"""
    out: list[str] = []
    for ln in lines:
        if PT_TBL_SEP.match(ln):
            prev = out[-1] if out else ""
            if not (PT_TBL_ROW.match(prev) and not PT_TBL_SEP.match(prev)):
                continue
        out.append(ln)
    return out


def _pt_trailing_table(text: str):
    """text 末尾若整块拖着一张 GFM 表格 → (表格文本, 去掉表格的剩余文本)。"""
    lines = (text or "").rstrip().split("\n")
    for s, e in reversed(_pt_blocks(lines)):
        if e == len(lines):
            return "\n".join(lines[s:e]).strip("\n"), "\n".join(lines[:s]).strip()
    return None


def _pt_scrub_label(t: str) -> str:
    """选项 label 出门前的表格痕迹清扫：整条分隔行、行首行尾多余的竖线、单元格分隔符残留、
    以及原卷选项表「字母列」导出的对照尾巴（`… D. ④ A. A B. B C. C D. D`）。"""
    if "|" not in (t or ""):
        out = re.sub(r"\s+", " ", t or "").strip()
        return PT_OPT_ECHO_TAIL.sub("", out).strip()
    parts = []
    for ln in t.split("\n"):
        s = ln.strip()
        if not s or PT_TBL_SEP.match(s):
            continue
        s = s.strip("|").strip()
        if s:
            parts.append(s)
    out = re.sub(r"\s*\|\s*", " ", " ".join(parts))
    out = re.sub(r"\s{2,}", " ", out).strip()
    return PT_OPT_ECHO_TAIL.sub("", out).strip()


def options_from_choice_table(seg: str, review: list[str], no: int):
    """表格感知的选项切分 → (options, content)；段内没有「选项表」时返回 ([], None)。

    只在能完整还原出 A–D 时才接管，题干只摘掉被选项用掉的那几行（表头/分隔行按需收尾），
    表格之外的文字一个都不少（2026 顺义一模 5 题：选项表后面还粘着下一题的共享材料）。
    """
    lines = seg.split("\n")
    drop: set[int] = set()
    opts = None
    for s, e in _pt_blocks(lines):
        got, used = _pt_opts_from_block(lines[s:e])
        if not got or opts is not None:
            continue
        opts, drop = got, {s + k for k in used}
    if opts is None:
        return [], None
    keep = [ln for i, ln in enumerate(lines) if i not in drop]
    keep = _pt_scrub_orphan_sep(_pt_drop_empty_blocks(keep))
    content = "\n".join(ln for ln in keep if not PT_OPT_ECHO.match(ln.strip()))
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    review.append(f"第{no}题（选择题）：A–D 选项在原卷里排成表格，已按单元格还原成 options"
                  f"（多列用「；」连接），表格已从题干摘掉，需人工核对")
    return [{"id": o["id"], "label": _pt_scrub_label(o["label"])} for o in opts], content


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


GRID_LABELS = re.compile(r"^([A-D])[\s．.、]*([A-D])[\s．.、]*$")


def options_from_grid(seg: str, review: list[str], no: int) -> tuple[list[dict], str]:
    """两栏排版的选择题选项：标签被 Word 单独排成「A B」「C D」两行，正文分四块。

    逐段扫描：遇到「X Y」这样只有两个字母的段，就把它上面那一批段落按两半分给 X、Y
    （四个选项文字量相当，等分是最稳的还原方式）；标签行本身不进题干。
    """
    paras = split_paras(seg)
    label_rows = [(i, m) for i, p in enumerate(paras) for m in [GRID_LABELS.match(p.strip())] if m]
    if len(label_rows) < 2:
        return [], seg
    opts: dict[str, str] = {}
    stem_paras: list[str] = []
    prev = 0
    for k, (i, m) in enumerate(label_rows):
        chunk = [p for p in paras[prev:i] if p.strip()]
        if k == 0 and len(chunk) % 2 == 1 and len(chunk) > 1:
            stem_paras.append(chunk[0])      # 第一段是题干，不算选项
            chunk = chunk[1:]
        if len(chunk) % 2 == 0 and chunk:
            half = len(chunk) // 2
            opts[m.group(1)] = "\n\n".join(chunk[:half])
            opts[m.group(2)] = "\n\n".join(chunk[half:])
        elif chunk:
            opts[m.group(1)] = "\n\n".join(chunk)
        prev = i + 1
    tail = [p for p in paras[prev:] if p.strip()]
    if tail:
        stem_paras.extend(tail)
    if sorted(opts) != ["A", "B", "C", "D"] or not all(opts.values()):
        return [], seg
    head = paras[:label_rows[0][0]]
    stem_before = "\n\n".join(p for p in head if p.strip())
    stem = "\n\n".join(x for x in (stem_before, "\n\n".join(stem_paras)) if x)
    review.append(f"第{no}题（选择题）：选项标签排成两栏（「A B」「C D」各一行），"
                  f"已按两栏位置还原 4 个选项，需人工核对")
    return [{"id": k, "label": opts[k]} for k in "ABCD"], stem


def options_from_label_table(seg: str, review: list[str], no: int) -> tuple[list[dict], str]:
    """选项被排成「一格一选项」的表格，且字母单独占一行（字母在内容下面）。

        甲…          |   | 乙…
        A            |   | B
        丙…          |   | 丁…
        C            |   | D

    遇到「这一行只有 A–D 字母」的行，就把上一行的非空单元格按顺序配给这些字母。
    """
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
        rows = [[_clean_cell(c) for c in _cell_split(lines[i])] for i in idxs
                if not _is_sep_row(_cell_split(lines[i]))]
        rows = [r for r in rows if any(c.strip() for c in r)]
        if len(rows) < 2:
            continue
        opts: dict[str, str] = {}
        for k in range(1, len(rows)):
            letters = [c.strip().upper() for c in rows[k] if re.fullmatch(r"[A-D]", c.strip())]
            prev = [c for c in rows[k - 1] if c.strip() and not re.fullmatch(r"[A-D]", c.strip())]
            if len(letters) >= 2 and len(prev) == len(letters):
                for letter, cell in zip(letters, prev):
                    opts.setdefault(letter, cell)
        if sorted(opts) == ["A", "B", "C", "D"] and all(opts.values()):
            keep = "\n".join(lines[j] for j in range(len(lines)) if j not in set(idxs))
            review.append(f"第{no}题（选择题）：选项排成两列两行的表格（字母单独一行），"
                          f"已还原 4 个选项、原表从题干里去掉，需人工核对")
            return [{"id": k, "label": opts[k]} for k in "ABCD"], keep.strip()
    return [], seg


QNUM_FIX = re.compile(
    r"(?m)^([ \t]*)(\*\*)?(\d{1,2})[ \t]*[，,：:][ \t]*(?=[\u4e00-\u9fff“《（(A-Za-z①-⑩])")


def norm_question_marks(text: str) -> str:
    """把 `1，设问` / `2：设问` 这种把逗号/冒号当题号分隔符的写法归一成 `1．`（2021 通州卷实测）。"""
    if not text:
        return text
    return QNUM_FIX.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}{m.group(3)}．", text)


def norm_option_labels(text: str) -> str:
    """Word 自动编号把「A.」变成「1.」的题（2024 顺义一模实测）：

        ... 故选 1. ①② B.①③ C.②④ D.③④

    这里「1.」后面紧跟 ①② 之类的组合项，只可能是选项标签 → 还原成 A.。
    """
    if not text:
        return text
    return re.sub(r"(?m)^[ \t]*(?:\*\*)?(?:1|A)[ \t]*[.、．][ \t]*(?=[①②③④⑤⑥⑦⑧⑨⑩])",
                  "A. ", text)


def _line_is_tbl_row(text: str, pos: int) -> bool:
    """pos 所在的那一整行是不是一行 GFM 表格行（`| A.①② B.①④ | … |`）。"""
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    le = le if le > 0 else len(text)
    return bool(PT_TBL_ROW.match(text[ls:le]))


def _row_has_options(text: str) -> bool:
    """段内有没有「整行是表格 + 这一行塞了多个选项」（选项被排成表格的判据）。

    只在命中时才对锚点结果做表格感知重切，保证残渣题以外一题不改。
    """
    for ln in (text or "").split("\n"):
        if not PT_TBL_ROW.match(ln):
            continue
        if len({m.group(1) for m in PT_CELL_OPT.finditer(ln)}) >= 2:
            return True
    return False


def _table_block_end(text: str, pos: int) -> int:
    """pos 所在的 markdown 表格块结束位置（用于「选项排成表」的题：题干要整表保留）。"""
    start = text.rfind("\n\n", 0, pos)
    start = 0 if start < 0 else start + 2
    lines = text[start:].split("\n")
    end = start
    for ln in lines:
        if ln.strip().startswith("|"):
            end += len(ln) + 1
        elif ln.strip():
            break
        else:
            break
    return max(end, pos)


def find_option_runs(text: str) -> list[tuple[int, int, list[dict]]]:
    """找出「A–D 四个选项」的连续块 → [(A 的起点, D 选项文本的终点, options)]。

    老卷（2023/2024）题号常常被 Word 自动编号写乱（材料里的 1./2./3. 也算题号），
    但**每题恰好一组 A–D 选项**这条规律是稳的，用它当锚点比用题号更可靠。
    """
    marks = list(OPT_MARK.finditer(text))
    runs = []
    i = 0
    while i < len(marks):
        if marks[i].group(1) != "A":
            i += 1
            continue
        got = {}
        j = i
        order = ["A", "B", "C", "D"]
        k = 0
        while j < len(marks) and k < 4 and marks[j].group(1) == order[k]:
            got[order[k]] = marks[j]
            k += 1
            j += 1
        if k == 4:
            a_pos = marks[i].start(1)
            d_end = marks[j - 1].end()
            labels = {}
            for idx, letter in enumerate(order):
                m = got[letter]
                if idx < 3:
                    nxt = got[order[idx + 1]].start(1)
                else:                       # 最后一个选项取到行尾
                    nl = text.find("\n", m.end())
                    nxt = nl if nl > 0 else len(text)
                labels[letter] = text[m.end():nxt].strip()
            runs.append((a_pos, d_end, [{"id": L, "label": labels[L]} for L in order]))
            i = j
        else:
            i += 1
    return runs


def split_choice_by_options(choice_body: str, choice_n: int, review: list[str]) -> list[dict]:
    """以「A–D 选项块」为锚点重切选择题（题号不规整的卷面用）。"""
    runs = find_option_runs(choice_body)
    if len(runs) != choice_n:
        return []
    items = []
    prev_end = 0
    for a_pos, d_end, opts in runs:
        end = d_end
        # 选项本身排成一行表格（`| A.①② B.①④ C.②③ D.③④ |  |  |`）：A–D 锚点在表格行内部，
        # 中间没有 `|`，所以单独判一次「这一整行是不是表格行」——否则分隔行会漏到下一题、
        # 表格不完整导致表格感知切分认不出来（2024 通州期末 4 题）。
        row_only = (_line_is_tbl_row(choice_body, a_pos)
                    and "|" not in choice_body[a_pos:d_end + 1])
        table_mode = "|" in choice_body[a_pos:d_end + 1] or row_only
        nxt_end = None
        if table_mode:                       # 选项就是一张表（首列 A–D）：整表留在题干
            end = _table_block_end(choice_body, a_pos)
            if row_only:
                # 只为让**本题** seg 含完整表格（表行 + 分隔行）；分段边界仍停在 D 所在行行尾，
                # 保证下一题 raw 与改前逐字节一致（零回归）。
                nl = choice_body.find("\n", d_end)
                nxt_end = nl if nl > 0 else len(choice_body)
        if not table_mode:
            # D 选项的文字通常就在同一行：截到行尾，免得「D.③④」的「③④」落到下一题题干开头
            nl = choice_body.find("\n", d_end)
            end = nl if nl > 0 else len(choice_body)
        seg = choice_body[prev_end:a_pos]
        seg = re.sub(r"^[ \t]*(?:#{1,6}[ \t]*)?((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*\d{1,2}\s*\\?[．.、·]\s*\**",
                     r"\1", seg, count=1)
        if table_mode:
            seg = choice_body[prev_end:end]
            seg = re.sub(r"^[ \t]*(?:#{1,6}[ \t]*)?((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*\d{1,2}\s*\\?[．.、·]\s*\**",
                         r"\1", seg, count=1)
            opts = [{"id": L, "label": L} for L in "ABCD"]
        items.append({"raw": seg.strip(), "options": opts})
        prev_end = end if nxt_end is None else nxt_end
    review.append(f"选择题题号被 Word 自动编号写乱 → 以 {len(runs)} 组「A–D 选项」为锚点重切并顺序编号")
    return items


LISTNUM_OPT = re.compile(r"^[ \t]*(?:\*\*)?(\d{1,2}|[A-D])[ \t]*[.、．][ \t]*", re.M)


def options_from_listnum(seg: str, review: list[str], no: int) -> dict[str, str]:
    """Word 把选项的 A/B/C 标签变成了列表编号（1./2./3.，只留 D. 还是字母）：

        1. 源于人类对数字计量的需求…
        2. 是一种渐进的变化…
        3. 对原计数单位既肯定又否定…
        D.使数量表达更加简洁、精准…

    题号切分时那些「1./2./3.」标记已被吃掉，所以这里改成：从段尾往前收**短段落**，
    有字母标签的按字母归位，没标签的按顺序补 A/B/C/D。
    """
    paras = split_paras(seg)
    if not paras:
        return {}
    tail: list[str] = []
    for p in reversed(paras):
        q = p.strip()
        if len(q) > 220 or q.startswith(("|", "![", "#")):
            break
        tail.insert(0, q)
        if len(tail) >= 4:
            break
    if len(tail) < 3:
        return {}
    got: dict[str, str] = {}
    pending: list[str] = []
    for blk in tail:
        m = LISTNUM_OPT.match(blk)
        if m and m.group(1) in "ABCD":
            got[m.group(1)] = blk[m.end():].strip()
        elif m:
            pending.append(blk[m.end():].strip())
        else:
            pending.append(blk)
    if not got and len(pending) < 4:
        return {}
    miss = [L for L in "ABCD" if L not in got]
    if len(pending) != len(miss) or not miss:
        return {}
    for L, body in zip(miss, pending):
        got[L] = body
    review.append(f"第{no}题（选择题）：选项标签被 Word 自动编号吃掉了，"
                  f"已按段尾顺序还原成 A–D，需人工核对")
    return got


def parse_choice_segment(seg: str, no: int, review: list[str]) -> dict:
    """一道选择题 → {content, options}（表格感知 + 原逻辑两段式）。

    **残渣判据**：原卷把 A–D 排成「首列字母的多列行」或「一格塞多个选项」时，老逻辑（OPT_MARK
    的 `(?=\\|)` 分支）会把表格的 `|` 与分隔行一起切进 option.label。所以先跑老逻辑，
    **只有老结果的 label 里确实带 `|`** 时才用 `options_from_choice_table()` 重算一次；
    老结果干净就一字节不动 —— 保证「残渣题以外零回归」（宁可不修，不许误伤）。
    """
    rv: list[str] = []
    got = _parse_choice_segment_legacy(seg.strip(), no, rv)
    if not any("|" in (o.get("label") or "") for o in (got.get("options") or [])):
        review.extend(rv)
        return got
    topts, tcontent = options_from_choice_table(seg.strip(), review, no)
    if topts:
        return {"content": tcontent, "options": topts}
    review.extend(rv)
    return got


def _parse_choice_segment_legacy(seg: str, no: int, review: list[str]) -> dict:
    """原来的切分逻辑（除「选项后拖一张题干材料表」的搬移外一字未改，作为基准逻辑保留）。"""
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
        # 选项后面还整块拖着一张题干材料表（2024 延庆一模 7 题「提问 | 回答」）→ 搬回题干末尾，
        # 别让它粘进最后一个选项的 label。
        tail = _pt_trailing_table(opts[-1]["label"]) if opts else None
        if tail:
            tbl, rest = tail
            opts[-1]["label"] = rest
            content = content.rstrip() + "\n\n" + tbl
            review.append(f"第{no}题（选择题）：选项后面跟着一张题干材料表，已从 options 搬回 content，"
                          f"需人工核对")
    else:
        gopts, gstem = options_from_grid(seg, review, no)
        if gopts:
            return {"content": gstem.strip(), "options": gopts}
        topts, tstem = options_from_label_table(seg, review, no)
        if topts:
            return {"content": tstem, "options": topts}
        lnum = options_from_listnum(seg, review, no)
        if lnum:
            head = seg
            for L, body in lnum.items():
                head = head.replace(body, "", 1)
            head = re.sub(r"(?m)^[ \t]*(?:\*\*)?(?:\d{1,2}|[A-D])[ \t]*[.、．][ \t]*$", "", head)
            return {"content": head.strip() or seg,
                    "options": [{"id": L, "label": lnum[L]} for L in "ABCD"]}
        if IMG_TOKEN.search(seg):
            # 选项整块是图片（原卷就这么排的）：保留图片在题干，options 先给 A–D 字母占位
            review.append(f"第{no}题（选择题）：选项文字在原卷里是图片，options 只能给 A–D 占位，"
                          f"答案字母仍需人工核对")
            return {"content": seg, "options": [{"id": k, "label": k} for k in "ABCD"]}
        log(f"    ⚠ 第{no}题选项没切全（标记 {sorted(found)}）")
        review.append(f"第{no}题（选择题）：选项没切全（只找到 {''.join(sorted(found)) or '无'}），需人工核对")
    for o in opts:
        o["label"] = _pt_scrub_label(o["label"])
    if any(not o["label"] for o in opts):
        for alt_fn in ("_grid", "_ltable", "_listnum"):
            got = None
            if alt_fn == "_grid":
                got, stem = options_from_grid(seg, [], no)
            elif alt_fn == "_ltable":
                got, stem = options_from_label_table(seg, [], no)
            else:
                got = options_from_listnum(seg, [], no)
            if got and all(o["label"] for o in got):
                return {"content": content.strip(), "options": got}
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


OPT_FRAG = re.compile(r"^[ \t]*(?:\*\*)?(?:\d{1,2}|[A-D])[ \t]*[.、．]")


def _is_opt_fragment(raw: str) -> bool:
    """这一段是不是「选项被 Word 写成 1./2./3./D.」后切出来的碎片。"""
    first = (split_paras(raw) or [""])[0].strip()
    return bool(OPT_FRAG.match(first)) and len(raw) < 400


_MANUAL_CHOICES: dict[str, dict] = {}


def _manual_choices(title: str) -> dict:
    """manual_choices/政治/<卷名>.json → {"2": {"A": "...", "B": "...", "C": "...", "D": "..."}}"""
    if title in _MANUAL_CHOICES:
        return _MANUAL_CHOICES[title]
    got: dict = {}
    f = REPO_ROOT / "manual_choices" / SUBJECT / f"{title}.json"
    if f.exists():
        try:
            got = {str(k): v for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
        except Exception:
            got = {}
    _MANUAL_CHOICES[title] = got
    return got


CIRC_ONLY = re.compile(r"^[\s①②③④⑤⑥⑦⑧⑨⑩]+$")
SECTION_LINE = re.compile(
    r"^(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*"
    r"(?:(?:第[一二1-2]部分|第[一二1-2]卷|[一二1-2][、.])[ \t]*[、.]?[ \t]*)?"
    r"(?:\*\*)?[ \t]*(?:[（(][ \t]*)?"
    r"(?:选择题|非选择题|单项选择题|多项选择题|综合题|选择题部分)"
    r"[^。\n]{0,24}(?:[）)])?[ \t]*(?:\*\*)?$")
BOILER_LINE = re.compile(
    r"^(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*(?:[（(][ \t]*)?(?:[一二1-2][、.][ \t]*)?(?:\*\*)?[ \t]*"
    r"(?:考生须知|考生注意|考生务必将|注意事项|"
    r"本试卷(?:共|分|满分|分第)|本卷(?:共|分|满分)|本部分共|本大题共|"
    r"下列各小题均有四个选项|在每小题列出的四个选项中|在每题列出的四个选项中|"
    r"答题前|请将答案|考试结束后|温馨提示|试卷说明|本试卷分第|"
    r"考试时间|满分\s*\d+\s*分)")
DATE_LINE = re.compile(r"^\d{4}\s*[.．/年]\s*\d{1,2}\s*月?[日]?$")
LEAD_MARK = re.compile(r"^(\d{1,2})[ \t]*[.．、][ \t]*(?=[\u4e00-\u9fff“《（(A-Za-z])")


def clean_choice_lead(text: str) -> str:
    """清掉选择题题干开头的排版残渣（入库扫描发现的系统性缺陷）：

    - 上一题 D 选项的标签文字被复制过来（`③④`）；
    - 分区标题（`选择题` / `第一部分 选择题` / `一、选择题`）；
    - 卷首说明（`考生须知` / `本部分共15题…` / `下列各小题均有四个选项…`）；
    - 题号残渣（`2.` 被拆到段首）。
    只处理**开头独段**，题干正文里合法出现的 ①②③④ 一律不动。
    """
    paras = split_paras(text or "")
    while paras:
        p = paras[0].strip()
        q = p.strip("*").strip()
        if not q:
            paras.pop(0)
            continue
        if CIRC_ONLY.match(q) or SECTION_LINE.match(p) or BOILER_LINE.match(p) or DATE_LINE.match(q) \
                or (p.lstrip().startswith("|") and re.search(r"考生须知|注意事项|答题卡", p[:200])):
            paras.pop(0)
            continue
        m = LEAD_MARK.match(q)
        if m:
            rest = q[m.end():].strip()
            if len(rest) >= 6:
                paras[0] = rest
                continue
            paras.pop(0)
            continue
        break
    return "\n\n".join(paras)


def parse_choice_part(choice_body: str, review: list[str],
                      choice_n: int = N_CHOICE, choice_score: int = CHOICE_SCORE,
                      title: str = "") -> list[dict]:
    """第一部分：单选（常规 15 题，文综卷 12 题）。共享材料/共享图复制进该组每道题的 content。"""
    choice_body = norm_question_marks(choice_body)
    choice_body = norm_option_labels(choice_body)
    segs = split_marks(choice_body, 1, max(choice_n, MAX_CHOICE))
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

    def build(segs_or_items, use_marks: bool):
        items = []
        for s_ in segs_or_items:
            if use_marks:
                no, seg, lead = s_["no"], s_["text"], s_["lead"]
            else:
                no, seg, lead = s_["no"], s_["raw"], ""
            rest, mat = shared_material_of(seg)
            if mat:
                tgt = shared_targets(mat)
                for n in tgt:
                    shared.setdefault(n, []).append(mat)
                if tgt:
                    review.append(f"第{no}题段末的共享材料「{mat[:24]}…」已从本题移出，"
                                  f"复制进第 {'、'.join(map(str, tgt))} 题")
                seg = rest
            items.append({"no": no, "raw": seg, "lead": lead})
        # 选项整块是图片时，Word 往往把那张图粘在**下一题题号前**（被当成下一题的 lead）
        for i in range(len(items) - 1):
            if items[i + 1]["lead"] and not OPT_MARK.search(items[i]["raw"]):
                items[i]["raw"] = (items[i]["raw"].rstrip() + "\n\n" + items[i + 1]["lead"]).strip()
                items[i + 1]["lead"] = ""
        for it in items:
            if it["lead"]:
                it["raw"] = it["lead"] + "\n\n" + it["raw"]
        nos = [it["no"] for it in items]
        if nos != list(range(1, len(items) + 1)):
            log(f"    选择题题号不规整 {nos} → 按出现顺序重编 1–{len(items)}")
            for i, it in enumerate(items, 1):
                it["no"] = i
            if shared:
                review.append(f"选择题题号不规整（{nos}），共享材料按出现顺序对应，需人工复核")
        return items

    preset: dict[int, list[dict]] = {}
    # ① 先试「以 A–D 选项块为锚点」重切：题号被 Word 写乱时最可靠
    alt = split_choice_by_options(choice_body, choice_n, [])
    if alt and any(not a["raw"].strip() for a in alt):
        alt = []
    elif alt:
        review.append(f"选择题题号不可靠 → 以 {len(alt)} 组「A–D 选项」为锚点重切并顺序编号")
    if alt:
        shared.clear()
        items = build([{"no": i + 1, "raw": a["raw"]} for i, a in enumerate(alt)], False)
        preset = {i + 1: a["options"] for i, a in enumerate(alt)}
        # 以「A–D 选项块」为锚点重切时，「选项排成表格」的题会把 `|` 与分隔行切进 label
        # （2024 通州期末 4 题 `| A.①② B.①④ C.②③ D.③④ |`）。这里只对**锚点结果带 `|`**、
        # 或**段内确有「整行是表格且塞了多个选项」**的题再试一次表格感知切分：表格能完整还原
        # A–D 就以表格为准，还原不出就原样不动 —— 保证除残渣题外一题不改（宁可不修，不许误伤）。
        for it in items:
            dirty = [o for o in (preset.get(it["no"]) or []) if "|" in (o.get("label") or "")]
            if not dirty and not _row_has_options(it["raw"]):
                continue
            tops, tcont = options_from_choice_table(it["raw"], review, it["no"])
            if tops:
                preset[it["no"]] = tops
                it["raw"] = tcont
    else:
        items = build(segs, True)
        # ② 选项被写成「1./2./3./D.」时，小段落会被当成新题号 → 并回上一题
        merged = True
        while merged:
            merged = False
            for i in range(1, len(items)):
                prev_opts = parse_choice_segment(items[i - 1]["raw"], items[i - 1]["no"], [])["options"]
                if len(prev_opts) != 4 and _is_opt_fragment(items[i]["raw"]):
                    items[i - 1]["raw"] = items[i - 1]["raw"].rstrip() + "\n\n" + items[i]["raw"].strip()
                    del items[i]
                    merged = True
                    break
        # ③ 选项被拆成多个「列表编号」小段（1./2./3./D.）：短段一路并回上一题，直到凑齐选项
        i = 0
        while i < len(items):
            opts = parse_choice_segment(items[i]["raw"], items[i]["no"], [])["options"]
            if len(opts) < 4 and i + 1 < len(items) and len(items[i + 1]["raw"]) < 300:
                items[i]["raw"] = items[i]["raw"].rstrip() + "\n\n" + items[i + 1]["raw"].strip()
                del items[i + 1]
                continue
            i += 1
        # ④ 仍多于声明题量：丢掉「其实是被切碎的选项段」（短、且没有设问口吻）
        ASK_HINT = re.compile(r"（\s*[）)]|下列|的是|表明|体现|说明|因为|启示|推断|正确的是|\?|？")
        dropped = []
        while len(items) > choice_n:
            cands = [(len(it["raw"]), k) for k, it in enumerate(items)
                     if len(it["raw"]) < 170 and not ASK_HINT.search(it["raw"])
                     and not OPT_MARK.search(it["raw"])]
            if not cands:
                break
            _, k = min(cands)
            dropped.append(items[k]["raw"][:24].replace("\n", " "))
            del items[k]
        if dropped:
            review.append(f"选择题：{len(dropped)} 段「被 Word 切碎的选项文字」已丢弃并并回相邻题目"
                          f"（{'；'.join(dropped)}），需人工核对选项归属")
        if len(items) > choice_n:
            keep = [it for it in items
                    if len(parse_choice_segment(it["raw"], it["no"], [])["options"]) == 4]
            if len(keep) == choice_n:
                items = keep
        for i, it in enumerate(items, 1):
            it["no"] = i

    out = []
    for it in items:
        if it["no"] in preset:
            parsed = {"content": it["raw"], "options": preset[it["no"]]}
        else:
            parsed = parse_choice_segment(it["raw"], it["no"], review)
        # 老卷（2011–2019 文综）把「（4分）」写在题干开头，不该留在 content 里
        content = re.sub(r"^[ \t]*[（(]\s*\d+\s*分\s*[）)]\s*", "", parsed["content"].strip())
        if it["no"] == 1 and items and len(items) > 1:
            m0 = QMARK.search(content)
            if m0 and m0.start() > 0:
                content = content[m0.start():]
        content = clean_choice_lead(content)
        if it["no"] in shared:
            content = "\n\n".join(shared[it["no"]] + [content])
        # 人工校订的选项（原卷选项文字被 Word 拆碎/丢失时用）：manual_choices/政治/<卷名>.json
        man_opts = _manual_choices(title).get(str(it["no"]))
        if man_opts:
            parsed["options"] = [{"id": L, "label": man_opts[L]} for L in "ABCD" if L in man_opts]
            review.append(f"第{it['no']}题（选择题）：选项按 manual_choices 的人工校订内容填充，需抽查")
        if not man_opts:
            for o in parsed["options"]:
                if not (o.get("label") or "").strip():
                    o["label"] = "（原卷此项空白）"
                    review.append(f"第{it['no']}题（选择题）：选项 {o['id']} 在原卷里是空白"
                                  f"（docx 与 PDF 都缺），已置占位文字，需人工核对")
            if parsed["options"] and all(o["label"] == o["id"] for o in parsed["options"]) \
                    and IMG_TOKEN.search(content):
                review.append(f"第{it['no']}题（选择题）：选项在原卷里是图片，options 只能给 A–D 占位，"
                              f"答案字母仍需人工核对")
        out.append({"id": it["no"], "questionType": "single",
                    "content": clean_text(content), "options": parsed["options"],
                    "answer": "", "analysis": "", "score": choice_score})
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
    def _sub_no(p: str):
        m = SUBQ.match(p)
        if m:
            return int(m.group(1))
        m = re.match(r"^[ \t]*(?:\*\*)?(\d{1,2})[ \t]*[.、．]", p)
        return int(m.group(1)) if (m and SCORE_RE.search(p)) else None

    marks = [i for i, p in enumerate(paras) if _sub_no(p) is not None]
    if not marks:
        idx = None
        for i in range(len(paras) - 1, -1, -1):
            if SCORE_RE.search(paras[i]):
                idx = i
                break
        if idx is None:
            # 没有分值括号：最后一段通常是设问；但「填表题」最后一段是表格（2024 第19题），
            # 这时要往前退到那一行文字要求（「阅读材料，参考示例，完成下表。」）
            idx = len(paras) - 1
            for i in range(len(paras) - 1, -1, -1):
                q = paras[i].strip()
                if q.startswith("|") or q.startswith("![") or len(q) < 5 or BOX.search(q):
                    continue
                idx = i
                break
        ask_raw = "\n\n".join(paras[idx:])
        pre = paras[:idx]
        # 设问常被 Word 硬换行切成两段（前半句在上一段、答题框在后一段）：并回设问、合成一段，
        # 否则前半句会被当成「材料」，questions.content 就会「复述材料」（2026 第17题实测）
        glued = []
        while pre:
            prev = pre[-1].strip()
            if not prev or prev.startswith(("|", "![", "#", "**", "【")) or prev[-1] in PARA_END_OK \
                    or len(prev) < 6:
                break
            glued.insert(0, pre.pop().strip())
        if glued:
            ask_raw = "".join(glued) + ask_raw.lstrip()
        scores = [int(x) for x in SCORE_RE.findall(ask_raw)]
        return (reflow_paras(pre),
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
            "no": _sub_no(chunk[0]) or (k + 1),
            "ask_raw": ask_raw,
            "material": reflow_paras(chunk[stop + 1:]),
            "scores": [int(x) for x in SCORE_RE.findall(ask_raw)],
        })
    return reflow_paras(preamble), subs


ANS_TAG = re.compile(r"【\s*([^】\n]{1,12})\s*】")
ANALYSIS_TAGS = ("分析", "解析", "点评", "详解", "易错", "思路点拨", "点睛", "举一反三",
                 "考点", "背景素材", "能力考查", "核心素养", "审题", "小问", "方法", "试题分析")


def split_answer_analysis(text: str) -> tuple[str, str]:
    """把一段参考答案拆成 (answer 正文, analysis 正文)。

    教师版答案区的标记：`【答案】`/`【解答】` 的正文 → answer；
    `【分析】`/`【解析】`/`【详解】`/`【点评】`/`【小问1详解】`/`【易错分析】` 的正文 → analysis。
    **标记本身一律去掉**（`qa_scaffold --check` 的 W_ANSWER_EMBEDDED 查的就是 answer 里带【…】）。
    「等级水平表」是评分依据，落在哪一段都抢回 answer（红线）。
    """
    if not text:
        return "", ""
    t = text.replace("\r", "")
    marks = list(ANS_TAG.finditer(t))
    if not marks:
        return t.strip(), ""
    ans_parts: list[str] = []
    ana_parts: list[str] = []
    head = t[:marks[0].start()].strip()
    if head:
        ans_parts.append(head)
    for i, m in enumerate(marks):
        tag = m.group(1)
        body = t[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(t))].strip()
        if not body:
            continue
        if any(k in tag for k in ANALYSIS_TAGS):
            ana_parts.append(body)
        else:
            ans_parts.append(body)
    answer = "\n\n".join(ans_parts).strip()
    analysis = "\n\n".join(ana_parts).strip()

    if analysis and "等级水平" in analysis:
        keep, moved = [], []
        for blk in re.split(r"\n{2,}", analysis):
            (moved if re.match(r"^\s*\|", blk) else keep).append(blk)
        table = "\n\n".join(moved).strip()
        if table and "等级水平" in table:
            answer = (answer + "\n\n" + table).strip()
            analysis = "\n\n".join(keep).strip()
    return answer, analysis


def split_ans_block(block: str) -> tuple[str, dict[int, str]]:
    """一道大题的参考答案块 → (整块, {小问号: 该问答案})。

    只从**答案正文**里切小问：`【解答】`/`【答案】` 之前（【分析】/【解析】）和 `【点评】` 之后
    都是解析，不算答案。老卷（2011–2016 文综）基金把「分析」和「解答」各写一遍、两遍都用 `（1）`，
    只按行首切会把解析当答案、把真正的解答（含等级水平表）整段丢掉。
    """
    block = block or ""
    block = re.sub(r"(【[^】\n]{1,8}】)[ \t]*[（(]\s*(\d{1,2})\s*[）)]", r"\1\n\n（\2）", block)

    # 答案正文区间：从【答案】/【解答】/【参考答案】起，到下一个【分析】/【解析】/【点评】/【详解】止
    start_m = re.search(r"【\s*(?:答案|解答|参考答案)\s*】", block)
    region = block
    if start_m:
        tail = block[start_m.end():]
        stop = re.search(r"【\s*(?:分析|解析|点评|详解|易错|思路点拨|点睛|举一反三|考点|背景素材)\s*】", tail)
        region = tail[:stop.start()] if stop else tail

    marks = list(SUBQ.finditer(region))
    if not marks:
        # 「第（1）问：」式（2012 文综卷）
        ms = list(re.finditer(r"第\s*[（(]\s*(\d{1,2})\s*[）)]\s*问", region))
        if not ms:
            txt = region.strip() or block.strip()
            return block.strip(), ({1: txt} if txt else {})
        out_q: dict[int, str] = {}
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(region)
            out_q.setdefault(int(m.group(1)), region[m.end():end].strip())
        return block.strip(), out_q

    out: dict[int, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(region)
        out.setdefault(int(m.group(1)), region[m.end():end].strip())
    return block.strip(), out


def clean_answer(ans: str, score: int | None, review: list[str], label: str) -> str:
    """答案出口规范化：去掉与本题分值相同的前/后「（N分）」、合并多余空行。"""
    a = (ans or "").strip()
    if not a:
        return ""
    # 「（10![](images/x.png)分）」这种分值括号里夹了图片的写法也要去掉
    a = re.sub(r"^[（(]\s*\d+\s*(?:!\[[^\]]*\]\([^)]*\))?\s*分\s*[）)]\s*", "", a)
    lead = SCORE_RE.match(a)
    if lead and a[lead.end():].strip():
        # 「（9分）」这种分值残渣不能进 answer；与本题分值不同也没关系，照样去掉
        a = a[lead.end():].lstrip()
    tail = SCORE_RE.search(a)
    if tail and score and int(tail.group(1)) == score and a[tail.end():].strip() == "":
        a = a[:tail.start()].rstrip()
    # 段末孤零零的「（三）」是小问编号残留，别留在答案里
    a = re.sub(r"\n+\s*[（(]\s*[一二三四五六七八九十]{1,2}\s*[）)]\s*$", "", a)
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
    ks = sorted(blocks)
    if len(ks) == len(nos):
        return {n: blocks[k] for n, k in zip(nos, ks)}
    # 数量不一致：能按题号对上的先对，剩下的顺位补
    for i, n in enumerate(nos):
        if n in blocks:
            out[n] = blocks[n]
        elif i < len(ks):
            out[n] = blocks[ks[i]]
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
# 卷面版式识别（分节 / 题量 / 分值）
# ──────────────────────────────────────────────────────────────────

def _heading_scores(text: str) -> tuple[int | None, int | None]:
    """从分节标题里取 (每题分值, 本节总分)。取不到返回 (None, None)。"""
    per = tot = None
    m = HDR_PER_SCORE.search(text or "")
    if m:
        per = int(m.group(1))
    m = HDR_TOTAL_SCORE.search(text or "")
    if m:
        tot = int(m.group(1) or m.group(2))
    return per, tot


def _distribute(total: int, weights: list[int]) -> list[int]:
    """按权重把 total 分成整数（最大余数法），每份 ≥1 分，Σ == total。"""
    n = len(weights)
    if n == 0:
        return []
    total = max(total, n)
    w = [max(1, int(x)) for x in weights]
    s = sum(w)
    exact = [total * x / s for x in w]
    ints = [max(1, int(x)) for x in exact]
    guard = 0
    while sum(ints) < total and guard < 1000:
        guard += 1
        order = sorted(range(n), key=lambda i: exact[i] - int(exact[i]), reverse=True)
        for i in order:
            if sum(ints) >= total:
                break
            ints[i] += 1
    guard = 0
    while sum(ints) > total and guard < 1000 and any(x > 1 for x in ints):
        guard += 1
        order = sorted(range(n), key=lambda i: exact[i] - int(exact[i]))
        for i in order:
            if sum(ints) <= total:
                break
            if ints[i] > 1:
                ints[i] -= 1
    return ints


def _mark_score_at(text: str, mark) -> int | None:
    """题号后面紧跟的「（N分）」→ N（`16．（8分）`，也吃题号下一段才写分值的卷面）。"""
    tail = text[mark.end():mark.end() + 60]
    m = re.match(r"[ \t]*\n*[ \t]*\**[ \t]*[（(]\s*(\d+)\s*分\s*[）)]", tail)
    return int(m.group(1)) if m else None


def layout_of(body: str, ans: str, review: list[str]) -> dict:
    """切出「选择题区 / 非选择题区」，并定出选择题题量、每题分值、非选择题总分。

    北京卷有三种写法，都要吃：
    - 新卷：「第一部分 / 第二部分」（西城、通州、2021+ 真题…）；
    - 老文综卷：「一、选择题 / 二、非选择题|解答题|综合题」，且**没有**「本部分共N题」的声明；
    - 2022 这类只有「一、本部分共15题…」而没有第二部分标题的。
    切点找不到时，退化成「第一个分值≠选择题单题分值的大题题号」。
    """
    h1 = HDR_P1.search(body)
    h2 = HDR_P2.search(body)
    choice_hdr = body[h1.start():h1.start() + 200] if h1 else body[:200]

    if h2:
        choice_body, essay_body = body[:h2.start()], body[h2.end():]
        essay_hdr = body[h2.start():h2.start() + 300]
        choice_body = choice_body[h1.end():] if h1 else choice_body
    else:
        essay_body, essay_hdr = "", ""
        choice_body = body[h1.end():] if h1 else body
        review.append("卷面没有「第二部分 / 二、非选择题」标题，按大题分值标记切出非选择题区")

    # 选择题题量
    choice_n = None
    m = HDR_COUNT.search(choice_hdr) or HDR_COUNT.search(body[:400])
    if m:
        choice_n = int(m.group(1))

    # 每题分值：标题「每小题N分」→ 题干里的「（N分）」→ 常规（文综 12 题 4 分 / 新卷 15 题 3 分）
    per_hdr, _ = _heading_scores(choice_hdr)
    paren = [int(x) for x in SCORE_RE.findall(choice_body[:6000])]
    per_paren = paren[0] if paren and len(set(paren)) == 1 else None
    choice_score = next((s for s in (per_hdr, per_paren) if s and 2 <= s <= 5), None)
    if choice_score is None:
        guess_n = choice_n or 15
        choice_score = 4 if guess_n <= 13 else 3
        review.append(f"选择题每题分值卷面没写清楚（标题「{per_hdr}」分、题干「{per_paren}」分）→ "
                      f"按北京卷常规 {choice_score} 分/题")
    if not essay_body:      # 没有第二部分标题：找到第一道「分值≠选择题单题分值」的大题
        first = None
        for m in BIGMARK.finditer(choice_body):
            if int(m.group(3)) != choice_score:
                first = m
                break
        if first is None:   # 连分值都没有（2022）：按题量声明切
            n0 = choice_n or N_CHOICE
            first = next((m for m in QMARK.finditer(choice_body) if int(m.group(2)) > n0), None)
        if first is not None:
            essay_body = choice_body[first.start():]
            choice_body = choice_body[:first.start()]
    if choice_n is None:
        choice_n = max((int(m.group(2)) for m in QMARK.finditer(choice_body)), default=N_CHOICE)

    # 非选择题总分：标题「满分N分 / 共N分」→ 卷面 100 分 − 选择题总分
    _, essay_total = _heading_scores(essay_hdr)
    if not essay_total or essay_total < 10:
        essay_total = PAPER_TOTAL - choice_score * choice_n
    return {"choice_body": choice_body, "essay_body": essay_body,
            "choice_n": choice_n, "choice_score": choice_score,
            "essay_total": essay_total}


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────

def resolve_essay_marks(essay_body: str, ans: str, choice_n: int,
                        review: list[str]) -> list[tuple]:
    """非选择题区 → [(mark, 题号)]，含两种「题号被 Word 自动编号写成 1.」的兜底：

    ① 序列中间（2021 昌平：16 → 17 → **1.** → 19）：带 ≥5 分 + 后一题号 == 前一题号+2 +
       答案区存在「前一题号+1」的块；
    ② 序列末尾（2024 顺义：19 → **1.** → **1.**）：带 ≥6 分 + 这一段足够长。
    """
    marks = [m for m in QMARK.finditer(essay_body) if int(m.group(2)) > choice_n]
    ans_nos: set[int] = set()
    for m in QMARK.finditer(ans):
        if int(m.group(2)) > choice_n:
            ans_nos.add(int(m.group(2)))
    for m in ANSMARK.finditer(ans):
        if int(m.group(1)) > choice_n:
            ans_nos.add(int(m.group(1)))

    forced: dict[int, int] = {}
    if marks:
        have = {m.start() for m in marks}
        for m in QMARK.finditer(essay_body):
            if m.start() in have:
                continue
            if (_mark_score_at(essay_body, m) or 0) < 5:
                continue
            pos = m.start()
            prev = [x for x in marks if x.start() < pos]
            nxt_m = [x for x in marks if x.start() > pos]
            if not prev or not nxt_m:
                continue
            p_no, n_no = int(prev[-1].group(2)), int(nxt_m[0].group(2))
            if n_no == p_no + 2 and (p_no + 1) in ans_nos:
                marks.append(m)
                forced[pos] = p_no + 1
        if forced:
            marks = sorted(marks, key=lambda m: m.start())
            review.append(f"非选择题区有 {len(forced)} 道大题的题号在序列中间被 Word 自动编号写成"
                          f"「1.」（应为第 {'、'.join(str(v) for v in sorted(forced.values()))} 题），"
                          f"已按「前一题号+1 + 答案区印证」捞回")

    if marks:
        tail = essay_body[marks[-1].start():]
        cand = []
        for m in QMARK.finditer(tail):
            if m.start() == 0:
                continue
            if (_mark_score_at(tail, m) or 0) >= 6 and len(tail[m.end():]) > 400:
                cand.append(m.start() + marks[-1].start())
        if cand:
            extra = [m for m in QMARK.finditer(essay_body) if m.start() in set(cand)]
            marks = sorted(marks + extra, key=lambda m: m.start())
            review.append(f"非选择题区有 {len(extra)} 道大题的题号被 Word 自动编号吃掉"
                          f"（写成「1.」），已按「≥6 分 + 段长」捞回并顺序编号")

    if not marks:
        return []
    nos = [int(m.group(2)) for m in marks]
    renumber = any(b <= a for a, b in zip(nos, nos[1:]))
    out = []
    for i, m in enumerate(marks):
        no = forced.get(m.start()) or ((i + 1 + choice_n) if renumber else int(m.group(2)))
        out.append((m, no))
    return out


def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"[（(]\s*教师版\s*[）)]|[（(]\s*含答案\s*[）)]", "", md.stem).strip()
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
        # 只认「评分标准 / 阅卷细则」这类**小节标题**；「（1）评分标准：…」是答案正文，不能当附录切
        if s and len(s) <= 16 and re.match(r"^(?:参考答案与?)?(?:评分标准|评分细则|阅卷细则|评分说明|"
                                           r"答案和评分参考|答案与评分参考)", s):
            cut = ans.find(ln)
            break
    if cut and cut > 200:
        head = re.sub(r"[#*\s]", "", ans[cut:cut + 40]).strip()[:16]
        review.append(f"该卷「参考答案」后还有一份评分附录（「{head}」…），"
                      f"answer 只取参考答案原文，附录未并入")
        ans = ans[:cut]

    img_title = title.replace("(", "（").replace(")", "）")

    def rewrite(t: str) -> str:
        return rewrite_images(t, md.parent, img_title, type_dir, review, SUBJECT)

    # ---- 一、选择题 ----
    lay = layout_of(body, ans, review)
    choice_body, essay_body = lay["choice_body"], lay["essay_body"]
    essay_body = norm_question_marks(essay_body)
    choice_n, choice_score, essay_total = lay["choice_n"], lay["choice_score"], lay["essay_total"]
    choice_body = re.sub(r"(?m)^[ \t]*\**\s*本部分共\s*\d+\s*题[^\n]*\n?", "", choice_body)

    choice_qs = parse_choice_part(choice_body, review, choice_n, choice_score, title)
    if not choice_qs:
        raise RuntimeError("没切到选择题")
    if len(choice_qs) != choice_n:
        review.append(f"选择题切到 {len(choice_qs)} 道（卷面声明 {choice_n} 道）")

    # 参考答案区：选择题区 / 非选择题区
    ah2 = HDR_P2.search(ans)
    if ah2:
        ans_choice_area, ans_essay_area = ans[:ah2.start()], ans[ah2.end():]
    else:
        first_ans_essay = next((m for m in QMARK.finditer(ans) if int(m.group(2)) > choice_n), None)
        if first_ans_essay:
            ans_choice_area = ans[:first_ans_essay.start()]
            ans_essay_area = ans[first_ans_essay.start():]
        else:
            ans_choice_area, ans_essay_area = ans, ""
    key = parse_choice_answers(ans_choice_area, max(choice_n, MAX_CHOICE))
    # 少数卷把选择题答案做成一张图片（文本里没有字母）：人工转录的覆盖表放 manual_answers/政治/<卷名>.json
    man = REPO_ROOT / "manual_answers" / SUBJECT / f"{title}.json"
    if man.exists():
        try:
            override = {int(k): str(v).strip().upper() for k, v in
                        json.loads(man.read_text(encoding="utf-8")).items()}
            key.update(override)
            review.append(f"选择题答案在原卷里是图片/表格，已按 {man.name} 的人工转录结果填充"
                          f"（{len(override)} 题），需抽查")
        except Exception as e:
            review.append(f"人工答案表 {man.name} 读取失败：{str(e)[:60]}")
    # 选择题的解析（【分析】/【详解】/【点评】…）按题号块收进 analysis 字段
    ans_blocks_raw: dict[int, str] = {}
    cms = [m for m in QMARK.finditer(ans_choice_area) if 1 <= int(m.group(2)) <= choice_n]
    ctop = [m for m in cms
            if re.match(r"[ \t]*(?:\*\*)?(?:【|[A-D](?![A-Za-z])|（\s*\d+\s*分)",
                        ans_choice_area[m.end():m.end() + 8])]
    if len(ctop) >= 2:
        cms = ctop
    for i, m in enumerate(cms):
        ans_blocks_raw.setdefault(
            int(m.group(2)),
            ans_choice_area[m.end():(cms[i + 1].start() if i + 1 < len(cms) else len(ans_choice_area))].strip())
    for q in choice_qs:
        q["answer"] = key.get(q["id"], "")
        if not q["answer"]:
            review.append(f"第{q['id']}题（选择题）缺答案")
        if len(q.get("options") or []) != 4:
            review.append(f"第{q['id']}题（选择题）选项不是 4 项（{len(q.get('options') or [])}）")
        block_txt = ans_blocks_raw.get(q["id"], "")
        _ab, _aa = split_answer_analysis(block_txt)
        q["analysis"] = rewrite(clean_answer(_aa, None, [], f"第{q['id']}题"))
        qc = drop_running_heads(q["content"], title)
        qc = separate_images(unwrap_single_col_tables(qc, review, f"第{q['id']}题"))
        qc = flatten_icon_tables(qc, review, f"第{q['id']}题")
        qc = drop_tiny_images(qc, review, f"第{q['id']}题", md)
        q["content"] = re.sub(r"\n{3,}", "\n\n", rewrite(normalize_md_tables(qc)))
        for o in q["options"]:
            o["label"] = rewrite(latex_clean(o["label"]))
    choice_section = {
        "type": "选择题", "questionType": "politics-choice", "title": "一、选择题",
        "category": "选择", "score": choice_score * len(choice_qs),
        "questions": choice_qs,
    }

    # ---- 二、非选择题（一题一个 section） ----
    pairs = resolve_essay_marks(essay_body, ans, choice_n, review)
    if not pairs:
        raise RuntimeError("没切到非选择题（第二部分）的大题")
    emarks = [m for m, _no in pairs]
    essays = []
    for i, (m, _no) in enumerate(pairs):
        nxt = emarks[i + 1] if i + 1 < len(emarks) else None
        essays.append({
            "no": _no,
            "printed": _mark_score_at(essay_body, m),
            "lead": (m.group(1) or "").strip(),
            "text": re.sub(r"^[ \t]*\n*[ \t]*\**[ \t]*[（(]\s*\d+\s*分\s*[）)]\s*",
                           "", essay_body[m.end():(nxt.start() if nxt else len(essay_body))], count=1),
        })

    # ---- 非选择题分值：题面 → 参考答案区 → 按卷面总分均摊（老卷/新卷都可能整卷不印单题分值） ----
    want = [e["no"] for e in essays]
    ans_cands = {}
    for m in QMARK.finditer(ans_essay_area):
        if int(m.group(2)) > choice_n:
            ans_cands[m.start()] = m
    for m in ANSMARK.finditer(ans_essay_area):
        if int(m.group(1)) > choice_n:
            ans_cands.setdefault(m.start(), m)
    ans_marks = [ans_cands[k] for k in sorted(ans_cands)]

    def _ans_no(m):
        return int(m.group(2) if m.re is QMARK else m.group(1))

    ans_printed: dict[int, int] = {}
    ans_blocks: dict[int, str] = {}
    dup_nos: list[int] = []
    if ans_marks:
        printed_nos = [_ans_no(m) for m in ans_marks]
        ordered = printed_nos == want
        for i, m in enumerate(ans_marks):
            nxt = ans_marks[i + 1] if i + 1 < len(ans_marks) else None
            no = _ans_no(m) if ordered else (want[i] if i < len(want) else _ans_no(m))
            blk = ans_essay_area[m.end():(nxt.start() if nxt else len(ans_essay_area))].strip()
            if no in ans_blocks:
                dup_nos.append(no)
                continue
            ans_blocks[no] = blk
            sc = _mark_score_at(ans_essay_area, m)
            if sc:
                ans_printed[no] = sc
        if not ordered:
            review.append(f"参考答案区的大题编号被 Word 自动编号写乱（{printed_nos}），"
                          f"已按出现顺序对照题面对应到 {want}")
        if len(ans_marks) != len(essays):
            review.append(f"参考答案区切到 {len(ans_marks)} 个大题块，题面有 {len(essays)} 道大题")
        if dup_nos:
            review.append(f"参考答案区里第 {'、'.join(map(str, sorted(set(dup_nos))))} 题的答案出现了两份"
                          f"（该卷有「评分参考」+「答案和评分参考」两节），已保留前一份")

    scored = {}
    for e in essays:
        scored[e["no"]] = e["printed"] or ans_printed.get(e["no"])
    if all(v is None for v in scored.values()):
        review.append(f"卷面与参考答案区都没印单题分值 → 按第二部分总分 {essay_total} 分"
                      f"依小问数分摊到 {len(essays)} 道大题")
    miss = [no for no in want if not scored.get(no)]
    if miss and len(miss) < len(want):
        review.append(f"第 {'、'.join(map(str, miss))} 题没印分值 → 用第二部分剩余分值补齐")
    whole = [dict(no=e["no"], subs=len(split_essay(e["text"])[1])) for e in essays]
    weights = [max(1, w["subs"]) for w in whole]
    if miss:
        rest = max(0, essay_total - sum(v for v in scored.values() if v))
        if rest < len(miss):
            # 已印的分值之和已经超了（老文综卷把跨学科综合题全分计入）→ 均摊覆盖全部大题
            got_all = _distribute(essay_total, weights)
            for no, v in zip(want, got_all):
                if not scored.get(no):
                    scored[no] = v
        else:
            for no, v in zip(miss, _distribute(rest, [weights[want.index(n)] for n in miss])):
                scored[no] = v
    else:
        ssum = sum(scored.values())
        if essay_total and ssum and abs(ssum - essay_total) > max(2, 0.25 * essay_total):
            if ssum < essay_total * 0.75:
                review.append(f"题面印的单题分值合计 {ssum} 分明显不足第二部分 {essay_total} 分"
                              f"（原卷分值疑似错印）→ 改按 {essay_total} 分依小问数分摊")
                for no, v in zip(want, _distribute(essay_total, weights)):
                    scored[no] = v
            else:
                review.append(f"题面印的单题分值合计 {ssum} 分 ≠ 卷面第二部分 {essay_total} 分"
                              f"（老文综卷把跨学科综合题全分计入，保留原卷标注）")
    for e in essays:
        e["score"] = int(scored.get(e["no"]) or 1)

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
        if subs and [x["no"] for x in subs] != list(range(1, len(subs) + 1)):
            for _i, _x in enumerate(subs, 1):
                _x["no"] = _i
            review.append(f"{label}：小问编号在原卷里重复/跳号，已按出现顺序重编 1–{len(subs)}")
        if not subs:                      # 兜底：整段当一问，别让后面除以 0
            preamble, subs = [], [{"no": 1, "ask_raw": seg.strip(),
                                   "material": [], "scores": []}]

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
                if sum(got) > big:
                    review.append(f"{label}：小问分值合计 {sum(got)} 分 > 大题分值 {big} 分，"
                                  f"按原比例压到 {big} 分")
                    got = _distribute(big, got)
                else:
                    review.append(f"{label}：小问分值合计 {sum(got)} ≠ 大题分值 {big} 分，"
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
            if "\n" in ask and not any(ln.strip().startswith("|") for ln in ask.split("\n")):
                ask = re.sub(r"\s*\n\s*", "", ask)      # 设问是一句话，合成一段
            # 答题框恒挂在「文字设问」后面：表格（填表题）与图片挪到框之后，
            # 否则 separate_images 会把图片拆成独立段、答题框孤零零落在一段里
            ask_blocks = split_paras(separate_images(ask))
            ask_text = "\n\n".join(b for b in ask_blocks
                                   if not b.lstrip().startswith("|") and not IMG_TOKEN.fullmatch(b.strip()))
            ask_other = [b for b in ask_blocks
                         if b.lstrip().startswith("|") or IMG_TOKEN.fullmatch(b.strip())]
            parts.append((ask_text.strip() or ask.strip()) + ph_tag(score_i))
            parts.extend(ask_other)
            if i < n - 1 and s["material"]:      # 两问之间的材料接在上一问的答题框之后
                parts.extend(s["material"])
            ans_raw_i = mapped.get(s["no"], "")
            ans_body_i, ana_body_i = split_answer_analysis(ans_raw_i)
            ans_i = clean_answer(ans_body_i, score_i, review, label)
            if not ans_i and block:
                # 参考答案区这一问只有「评分说明 / 分析」，没有标准答案 → 用那段说明顶，别留空
                _ana = split_answer_analysis(block)[1]
                _sub_ana = split_ans_block(_ana)[1] if _ana else {}
                _fallback = _sub_ana.get(s["no"]) or (_ana if len(subs) == 1 else "")
                _fb = clean_answer(split_answer_analysis(_fallback)[0] or _fallback, score_i, [], label)
                if _fb:
                    ans_i = _fb
                    review.append(f"{label} 第（{s['no']}）问：参考答案区没有标准答案，"
                                  f"answer 取该卷的评分说明/分析原文（开放题）")
            if not ans_i:
                # 开放题（写短评/任选其一/写提纲…）教师版本就没给标准答案
                ans_i = "（本题为开放性试题，原卷参考答案区没有给出标准答案）"
                review.append(f"{label} 第（{s['no']}）问：开放题，参考答案区没有标准答案"
                              f"（answer 置说明文字，判分走 AI）")
            qs.append({"id": no * 100 + i + 1, "questionType": "input",
                       "content": clean_text(ask_text.strip() or ask),
                       "answer": rewrite(ans_i), "analysis": rewrite(clean_answer(ana_body_i, None, [], label)),
                       "score": score_i})

        # 大题答案块里【分析】/【点评】这些「块级解析」（在小问编号之前）→ 挂到第一问的 analysis
        if qs and block:
            norm_blk = re.sub(r"(【[^】\n]{1,8}】)[ \t]*[（(]\s*(\d{1,2})\s*[）)]",
                              r"\1\n\n（\2）", block)
            m0 = SUBQ.search(norm_blk)
            preface = norm_blk[:m0.start()].strip() if m0 else ""
            if preface:
                _pa, _pb = split_answer_analysis(preface)
                _ptxt = clean_answer((_pa + "\n\n" + _pb).strip(), None, [], label)
                _ptxt = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", _ptxt).strip()   # 别把分值残渣写进 analysis
                if _ptxt:
                    qs[0]["analysis"] = rewrite((_ptxt + ("\n\n" + (qs[0]["analysis"] or "")).strip()).strip())

        content = "\n\n".join(p.strip() for p in parts if p and p.strip())
        content = drop_running_heads(content, title)
        _seen, _dedup = set(), []
        for _p in split_paras(content):
            _k = _norm_cmp(PH.sub("", _p))
            if len(_k) >= 20 and _k in _seen:
                review.append(f"{label}：段落「{_p[:24]}…」重复出现，已去掉后一次")
                continue
            if len(_k) >= 20:
                _seen.add(_k)
            _dedup.append(_p)
        content = "\n\n".join(_dedup)
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
        detail = "；".join(f"第{s['title'].split('第')[-1].rstrip('题）')}题 {s['score']} 分"
                          for s in sections[1:]) if len(sections) > 1 else ""
        review.append(f"全卷分值合计 {total} 分 ≠ 国家卷标称 {PAPER_TOTAL} 分：卷面标注为"
                      f"选择 {choice_score}×{len(choice_qs)}={choice_score * len(choice_qs)} 分 + {detail}"
                      f"（老文综卷教师版把跨学科综合题的分值一并计入），"
                      f"按卷面原样保留、未做折算，不是 bug")
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


RUNNING_HEAD = re.compile(r"^20\d{2}\s*北京[^\n]{0,24}政治$")


def drop_running_heads(text: str, title: str) -> str:
    """去掉 Word 页眉/页脚转进来的卷眉行（「2020北京高考真题政治」这类，会被当成材料留在段末）。"""
    if not text:
        return text
    paras = split_paras(text)
    keep = []
    for p in paras:
        q = p.strip().strip("*").strip()
        if q and (q == title or RUNNING_HEAD.fullmatch(q)):
            continue
        keep.append(p)
    return "\n\n".join(keep)


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
        key = re.sub(r"[\\_*]{2,}", "", key)         # 填空题的横线（\_\_\_）不是材料
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
                 "matlen": [], "choice_score": CHOICE_SCORE, "essay_total": 0}, errs, notes)
    stat = {"title": paper.get("title"), "choice": 0, "essay": 0, "subs": 0, "tables": 0,
            "imgs": set(), "sections": [], "declared": None, "matlen": [],
            "choice_score": None, "essay_total": 0, "md_essay": 0}
    src_md = _source_md_of(jf)
    declared_choice = declared_essay = None
    declared_choice_score = declared_essay_total = None
    if src_md is not None:
        try:
            whole = src_md.read_text(encoding="utf-8")
            body = split_paper(whole)[0]
            lay = layout_of(body, "", [])
            declared_choice = lay["choice_n"]
            declared_choice_score = lay["choice_score"]
            declared_essay_total = lay["essay_total"]
            m = re.search(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\**\s*(?:第[二2]部分|[二2][、.][ \t]*\**\s*"
                          r"(?:非选择题|解答题|综合题|第二部分))", body)
            md_essay_n = len(resolve_essay_marks(lay["essay_body"], split_paper(whole)[1],
                                                 lay["choice_n"], []))
            stat["md_essay"] = md_essay_n
            if m:
                mm = HDR_COUNT.search(body[m.start():m.start() + 300])
                if mm:
                    declared_essay = int(mm.group(1))
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
    choice_score = None
    for s in choice_secs:
        qs = s.get("questions") or []
        stat["choice"] = len(qs)
        if s.get("questionType") != "politics-choice":
            errs.append(f"选择题 questionType = {s.get('questionType')}（应为 politics-choice）")
        if s.get("category") != "选择":
            errs.append("选择题 category 应为 选择")
        if declared_choice and len(qs) != declared_choice:
            errs.append(f"选择题 {len(qs)} 道 ≠ 卷面声明 {declared_choice} 道")
        # 每题分值：来自卷面（新卷 3 分 / 文综卷 4 分），只要求「全卷一致且在 2–5 之间」
        if qs and s.get("score") and s["score"] % len(qs) == 0:
            choice_score = s["score"] // len(qs)
        if not choice_score or not (2 <= choice_score <= 5):
            errs.append(f"选择题 score={s.get('score')} / {len(qs)} 道 → 每题 {choice_score} 分（应在 2–5 分）")
            choice_score = choice_score or 3
        if declared_choice_score and declared_choice_score != choice_score:
            notes.append(f"选择题每题 {choice_score} 分（卷面标题写 {declared_choice_score} 分）")
        nos = [q.get("id") for q in qs]
        if nos != list(range(1, len(qs) + 1)):
            errs.append(f"选择题题号不是 1..{len(qs)} 连续：{nos}")
        for q in qs:
            if q.get("score") != choice_score:
                errs.append(f"选择题第{q.get('id')}题 score={q.get('score')}（应为 {choice_score}）")
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
            _cparas = split_paras(q.get("content") or "")
            _first = (_cparas[0] if _cparas else "").strip()
            if _first and (CIRC_ONLY.match(_first.strip("*").strip())
                           or SECTION_LINE.match(_first)
                           or BOILER_LINE.match(_first)):
                errs.append(f"选择题第{q.get('id')}题 content 开头是排版残渣（{_first[:20]!r}）")
            for o in opts:
                if not (o.get("label") or "").strip():
                    errs.append(f"选择题第{q.get('id')}题选项 {o.get('id')} label 为空")
            stat["tables"] += count_md_tables(q.get("content") or "")
            for e in table_shape_ok(q.get("content") or ""):
                errs.append(f"选择题第{q.get('id')}题表格：{e}")
        stat["choice_score"] = choice_score
        stat["sections"].append(("选择题", "选择", len(qs), s.get("score"),
                                 sum(q.get("score") or 0 for q in qs)))

    rev_path = jf.with_suffix(".review.txt")
    review_lines = ([x for x in rev_path.read_text(encoding="utf-8").splitlines() if x.strip()]
                    if rev_path.exists() else [])

    if not essay_secs:
        errs.append("没有非选择题 section")
    md_essay_n = stat.get("md_essay")
    if md_essay_n and len(essay_secs) != md_essay_n:
        errs.append(f"非选择题 {len(essay_secs)} 道 ≠ 源 MD 切出的大题数 {md_essay_n} 道")
    if declared_essay and md_essay_n and declared_essay != md_essay_n:
        notes.append(f"卷面写「本部分共{declared_essay}题」，源 MD 只有 {md_essay_n} 道大题"
                     f"（题面与参考答案区一致，疑卷面声明笔误）")
    open_subs = set()
    for line in review_lines:
        m = re.search(r"第\s*(\d{1,2})\s*题\s*第\s*[（(]\s*(\d{1,2})\s*[）)]\s*问.*(?:开放题|无标准答案)", line)
        if m:
            open_subs.add((int(m.group(1)), int(m.group(2))))

    def no_of_title(sec):
        t = re.search(r"第\s*(\d{1,2})\s*题", sec.get("title") or "")
        return int(t.group(1)) if t else -1

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
        ask_norms = [_norm_cmp(PH.sub("", p)) for p in paras if BOX.search(p)]
        qk_all = [_norm_cmp(PH.sub("", q.get("content") or "")) for q in qs]
        # 设问自己带的表格/图片（填表题、参观手册）排在答题框之后，它们属于设问不属于材料
        mat_norms = [_norm_cmp(PH.sub("", p)) for p in paras if not BOX.search(p)]
        mat_norms = [m for m in mat_norms if m and not any(m in qk for qk in qk_all)]
        for q in qs:
            qparas = split_paras(PH.sub("", q.get("content") or "")) or [""]
            qk = _norm_cmp(PH.sub("", q.get("content") or ""))
            qk_first = _norm_cmp(qparas[0])
            if len(qk) < 12 or (q.get("content") or "").lstrip().startswith("|"):
                continue      # 填表题的「设问」就是那张表，规范化后必然与 content 里的表不同
            if not any(qk in a or a in qk or qk_first in a or a.startswith(qk_first) for a in ask_norms):
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
                if (no_of_title(s), q.get("id") % 100) in open_subs:
                    notes.append(f"{s.get('title')} id={q.get('id')} 开放题无标准答案（answer 留空，按约定）")
                else:
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

    # 非选择题总分 == 卷面第二部分总分（新卷 55 / 文综卷 52；老文综个别卷把跨学科综合题全分计入 → 只提示）
    essay_sum = sum(s.get("score") or 0 for s in essay_secs)
    stat["essay_total"] = essay_sum
    if declared_essay_total:
        if essay_sum != declared_essay_total:
            notes.append(f"非选择题分值合计 {essay_sum} 分（卷面第二部分写 {declared_essay_total} 分，"
                         f"老文综卷把跨学科综合题全分计入时会出现差额）")
    if declared_essay is None and essay_nos:
        start = 13 if (declared_choice or 15) <= 12 else 16
        want = list(range(start, start + len(essay_nos)))
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
                                      "declared": (None, None), "matlen": [],
                                      "choice_score": CHOICE_SCORE, "essay_total": 0,
                                      "md_essay": 0},
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
                    f"- 选择题：**{stat['choice']} 题 × {stat['choice_score'] or CHOICE_SCORE} 分 = "
                    f"{stat['choice'] * (stat['choice_score'] or CHOICE_SCORE)} 分**"
                    f"（卷面声明 {stat['declared'][0] or '—'} 题）\n"
                    f"- 非选择题：**{stat['essay']} 道大题，{stat['subs']} 个小问，"
                    f"合计 {stat['essay_total']} 分**"
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
