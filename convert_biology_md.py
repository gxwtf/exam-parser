#!/usr/bin/env python3
"""生物整卷解析（选择题 + 非选择题）→ 题库导入 JSON（`output_json_bio/<类型>/<卷名>.json`）。

链路：docx→MD（batch_convert_docx_to_md.py）→ 本脚本 → 题库导入脚本

输出结构（字段名与导入脚本严格一致，**不要改名字**）：

    {"paper": {"title","subject","source","grade","year","totalScore","duration","tags"},
     "sections": [
       {"type":"选择题","questionType":"biology-choice","title":"一、选择题","category":"选择","score":30,
        "questions":[{"id":1,"questionType":"single","content":…,
                      "options":[{"id":"A","label":…}×4],"answer":"B","analysis":"","score":2}]},
       {"type":"非选择题","questionType":"biology-fill","title":"二、非选择题（第16题）",
        "category":"稳态与调节","score":10,
        "content":"材料 + 小问，每个答题空按顺序替换成 <Input></Input>/<InputLong></InputLong>",
        "questions":[{"id":1601,"questionType":"input","content":"","answer":…,"analysis":"","score":2}]}
     ]}

关键约定（导入后前端绑定的唯一依据）：

- 选择题 15 题 × 2 分，选项恒为 A–D，答案单个大写字母；
- 非选择题**一题一个 section**（16、17…），`questions` 是这道大题的「空」，
  顺序与 `content` 里 `<Input` 出现的顺序完全一致，Σquestions.score == section.score；
- 空答短/长：参考答案（不含空格）≤ 12 字 → `<Input></Input>`，否则 `<InputLong></InputLong>`；
- **每空分值 = 长句空优先吃高分**：长句空（`<InputLong>`）权重 2、短空权重 1，按权重
  比例（最大余数法，四舍五入）把大题满分分到各空，每空至少 1 分；长句分恒不低于短空
  （必要时做单调修正）；Δ=0 时严格 Σ == 大题满分，结果稳定可复现，逐题写进 `.review.txt`。
  例：12 分 / 5 空含 2 长句 → 长 3、长 3、短 2、短 2、短 2。
- **题干里的数据表输出成 GFM markdown 表格**：表头保留、每行单元格数对齐（缺的补空单元格、
  多出的按列数截掉）、分隔行唯一且列数与表头一致；单元格内公式仍是 LaTeX，数字与单位只
  清洗空格残留（`10. 00` → `10.00`、`43 %` → `43%`），不重排；AI 把表拍平成文字时按原题补回。
- **画图/补图题的参考答案用图片**：`answer` 直接写
  `![](/exam-images/<类型>/<学科>/<卷名>/images/xxx.png)`，文件必须真实存在；
  `.emf/.wmf/.tiff` 先转 png；docx 里没有的答案图，从教师版 PDF 渲染该页
  （`pdftoppm -r 200`）→ 多模态模型给 bbox → `convert -crop` 裁成 `ans-<题号>.png`。
  定位不到就在 `.review.txt` 里记一条，并保留原文字答案。
- 一个空有多个可接受答案用 `##` 分隔；图片走 `/exam-images/<类型>/<学科>/<卷名>/images/x.png`。

做法：选择题/答案表**规则解析**（稳），每道非选择题交给本机模型「整理题干 + 认空 + 摘抄答案」
（生物卷的空有下划线、空格、填表、看图补画、选填①②③等多种形态，纯规则切不干净），
随后全部**确定性校验 + 出口规范**（占位符配对、长短判定、分值配平、图片重写、答案大小写）。

用法：
    python3 convert_biology_md.py -T 二模 -s 生物 -w 0        # 批量（可 --papers 限定几套）
    python3 convert_biology_md.py -p <md 文件> [--force] [--no-ai]
    python3 convert_biology_md.py --check -T all --report …    # 自查（分值规则/表格/图片 三类断言）
"""
import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from convert_chinese_md import chat, rewrite_images, split_paper, log
from convert_math_pdf import chat_json as vision_json      # 多模态（PDF 页面 → bbox）
from paths import GAOZHONG, OUTPUT_DOCX_TO_MD, OUTPUT_JSON_BIO
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "生物"
OUT_ROOT = OUTPUT_JSON_BIO
OUT = {"root": OUT_ROOT}

N_CHOICE = 15          # 第一部分：15 道单选（个别卷题号重复会多出 1 道，见 MAX_CHOICE）
MAX_CHOICE = 20        # 选择题区允许的上限（题号重复/自动编号会导致 16 道）
CHOICE_SCORE = 2       # 每题 2 分
SHORT_MAX = 12         # 答案 ≤12 字 → <Input>，否则 <InputLong>

CATS = ["遗传与进化", "稳态与调节", "生物技术与工程", "实验探究"]

# 题号行：可带前置图片、可带（N分）
QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\**\s*\\?[．.、]\s*\**\s*"
    r"(?:[（(]\s*(\d+)\s*分\s*[）)])?\s*"
)
# 分节标题：一、选择题 / 第二部分 / 二、非选择题（解答题）——用来切「选择题区 / 大题区」
HDR_ESSAY = re.compile(r"(?m)^[ \t]*\**[ \t]*(?:第[二2]部分|二\s*[、.]\s*(?:非选择题|解答题|综合题))")
# 卷面声明的选择题题数（「本部分共 15 题」/「共 5 小题」）
DECLARED_N = re.compile(r"共\s*(\d{1,2})\s*(?:道|个)?\s*小?题")
# 选项标记：`A．` 或 `A.`，也允许「A 后面直接跟一张图片」（有些卷子把选项字母做成了图）
OPT_MARK = re.compile(
    r"(?:^|(?<=[\s\u3000\u00a0>)\u4e00-\u9fff]))([A-D])"
    r"(?:[ \t]*[．.、][ \t]*|(?=[ \t]*!\[))", re.M)
# 模型可能输出的各种占位符写法
PH = re.compile(r"<Input(?:Long)?\s*/?>(?:\s*</Input(?:Long)?>)?")
LATEX_SUBSUP = re.compile(r"([A-Za-z][A-Za-z0-9]*|\d+)?((?:<su[bp]>[^<]*</su[bp]>)+)")
IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
EXAM_IMG = re.compile(r"/exam-images/([^\"'\s)]+)")
LEAD_LABEL = re.compile(r"^(?:[（(]\s*\d{1,2}\s*[）)]|[①②③④⑤⑥⑦⑧]|\d{1,2}\s*[.．、])\s*")
DASH = re.compile(r"(?<![\w-])--(?![\w-])")
ADJ_MATH = re.compile(r"\$\\mathrm\{([^{}]*)\}\$\$\\mathrm\{([^{}]*)\}\$")
CJK_CH = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
PARA_START = re.compile(
    r"^(?:[（(]\s*\d{1,2}\s*[）)]|[①②③④⑤⑥⑦⑧⑨]|注\s*[:：]|图\s*\d|表\s*\d"
    r"|[A-Da-d]\s*[．.、]\s|\*\*|#|\||!\[)")

SYSTEM = "你是生物试卷结构化助手，只输出 JSON。"

ESSAY_PROMPT = r"""你是北京高考生物试卷的结构化录入员。下面是一道生物非选择题的【原题】（Markdown，来自 Word 转换；形如 \_**\_**\_ 或 \___\__ 的下划线是答题空，也可能是空白字符）和这道题的【参考答案】。

请输出一个 JSON：
{
  "category": "遗传与进化 / 稳态与调节 / 生物技术与工程 / 实验探究 四选一",
  "content": "整理后的完整大题（材料 + 全部小问）",
  "blanks": [{"answer": "该空的参考答案（逐字摘抄）"}]
}

硬性规则：
1. 每个需要学生作答的位置，按在文中出现的先后顺序，替换成一个 <Input></Input>（答案≤12字）或 <InputLong></InputLong>（长句表达）。
2. 题面上没有画下划线但实际要求作答的地方（例如「请说明理由」「请提出一个假设」「请在图2中补充结果」「填入①～③」的①②③、填表等），同样要留空。
3. blanks 的数量、顺序必须与 content 中 <Input 出现的数量、顺序完全一致。
4. answer 必须从【参考答案】中逐字摘抄，不得改写、不得把两个空合并成一个；一个空有多个均可接受的答案时用 ## 分隔；答案里出现「见下图」的，必须把对应的 ![](images/xxx.png) 一起抄进该空的 answer。
5. 小问编号规则：参考答案用 1./2./3. 或（1）（2）编号的，content 一律写成（1）（2）（3）…，数量与参考答案的小问数量一致；只有参考答案里标了①②③的子问，content 对应处才用①②③。不要写题号「16」和分值「（12分）」。
6. 参考答案里用 ①②③ 给出多个答案的（如「① a ② c ③ d」「①②③处的抗体分别是」），题干必须**分别留三个空**（①<Input></Input> ②<Input></Input> ③<Input></Input>），不能合成一个空。
7. 每个小问都必须有答案：开放题（提出假设、说明理由、提出措施/前景、评价观点、补充实验结果、绘图）也要留一个空并把参考答案写进 blanks。
8. **表格必须原样输出成 GFM Markdown 表格**（绝不允许拍平成一行文字）：
   - 第一行是表头，第二行是 `| --- | --- | …` 分隔行（列数必须与表头一致），之后每行一个数据行；
   - 每一行的单元格数必须与表头列数**完全一致**：合并单元格/空位用「空单元格」（两个竖线之间什么都不写）占位，
     既不要把相邻单元格合并，也不要丢掉空列；
   - 单元格里的数字与单位原样照抄（如 `47.41`、`43 %`、`ng / g`），顺序不许打乱、不许四舍五入；
   - 单元格里的公式仍写成 LaTeX（$\mathrm{CO_2}$ 这类），下标上标不丢；
   - 表里已经填好的数据（如 ＋＋、▲▲、/ 等）必须原样保留，不要把它们变成空；行内不要写 <br>。
9. 选择题式的选项（A．B．C．D．或 a．b．c．）必须**每项单独一行**，不要挤在同一行里。
10. 画图/补图/绘图类小问（「在图 2 中补充…」「绘制…」「完善…机制图」）：
   answer 里只写一句「见下图」即可，不要编造图形内容（脚本会用教师版 PDF 的答案图填这个空）。
11. 图片引用原样保留原题里的 ![](images/xxx.png)，不要编造任何素材。
12. 只输出 JSON，不要任何解释。

【原题】
{BODY}

【参考答案】
{ANS}
"""

REPAIR_PROMPT = r"""下面是一道生物非选择题的【原题】、【参考答案】，以及上一轮整理的结果（content 里的 <Input></Input>/<InputLong></InputLong> 是答题空，blanks 是每空的答案）。

上一轮有两个问题：{PROBLEM}
请逐条对照【参考答案】修正，重新输出完整 JSON（格式同前）：
{
  "category": "遗传与进化 / 稳态与调节 / 生物技术与工程 / 实验探究 四选一",
  "content": "修正后的完整大题",
  "blanks": [{"answer": "该空的参考答案（逐字摘抄）"}]
}
要求：
- content 里 <Input 的个数必须与 blanks 条数**严格相等**，顺序一一对应；
- 参考答案里每一个小问、每一个①/②/③、每一次「说明理由/提出假设/补充结果/绘图」都要有对应的空；
- answer 逐字摘抄，多个可接受答案用 ## 分隔。

【原题】
{BODY}

【参考答案】
{ANS}

【上一轮 content】
{CONTENT}

【上一轮 blanks】
{BLANKS}
"""

CAT_PROMPT = r"""下面是北京高三生物二模试卷的若干道非选择题（题号 + 题干摘要）。请为每题判断所属模块，只能选一个：
遗传与进化 / 稳态与调节 / 生物技术与工程 / 实验探究

判定要点：
- 遗传与进化：遗传规律、基因表达与调控、变异与育种、生物进化；
- 稳态与调节：内环境与稳态、神经/体液/免疫调节、植物激素、种群群落与生态系统（生态学内容归此类）；
- 生物技术与工程：基因工程、细胞工程、发酵工程、微生物培养与菌株改造、载体/PCR/转基因、人工构建的生物系统；
- 实验探究：细胞结构/代谢、生理机制一类的探究题（如线粒体功能、细胞增殖与衰老、自噬、光合呼吸机制），
  以实验设计、变量控制、结果推断为主线，且题面没有基因工程/细胞工程操作。
只输出 JSON：{{"16":"模块","17":"模块"}}
题目：
{ITEMS}
"""


# ──────────────────────────────────────────────────────────────────
# 通用小工具
# ──────────────────────────────────────────────────────────────────

def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def latexify(text: str) -> str:
    """Word 转换残留的 <sub>/<sup> → LaTeX（$\\mathrm{CO_2}$ 这类）。"""
    if not text:
        return text
    if "<sub>" in text or "<sup>" in text:
        def repl(m):
            base = m.group(1) or ""
            parts = re.findall(r"<su([bp])>(.*?)</su[bp]>", m.group(2), re.S)
            idx = "".join(("_{%s}" if k == "b" else "^{%s}") % v.strip() for k, v in parts)
            return f"$\\mathrm{{{base}{idx}}}$"

        text = LATEX_SUBSUP.sub(repl, text)
    while ADJ_MATH.search(text):                 # $\\mathrm{X}$$\\mathrm{Y}$ → 合并成一个
        text = ADJ_MATH.sub(r"$\\mathrm{\1\2}$", text)
    return DASH.sub("——", text)                 # "--依次为" → "——依次为"


def reflow_cjk(text: str) -> str:
    """把 Word 硬换行拼回段落：中文断行处直接接上，中英之间留一个空格。

    表格行、图片行、`注：`、`（1）`、`①`、`A．`、`**标题**`、`图 1`、以及很短的行
    （图注标签之类）一律各自成段，不参与拼接。
    """
    if not text:
        return text
    paras, buf = [], ""

    def closed_para(s: str) -> bool:
        if PARA_START.match(s) or s.startswith("|") or s.startswith("!["):
            return True
        if len(s) < 15:                      # 图注标签、单句短语
            return True
        if s.startswith("**") and s.endswith("**"):
            return True
        return s.endswith(("。", "！", "？", "；", "："))

    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if not buf:
            buf = s
            continue
        if closed_para(buf) or PARA_START.match(s) or buf.startswith("![") \
                or buf.endswith("|") or s.startswith("|"):
            paras.append(buf)
            buf = s
            continue
        buf += s if (CJK_CH.match(buf[-1]) or CJK_CH.match(s[0])) else " " + s
    if buf:
        paras.append(buf)
    out = "\n\n".join(paras)
    return re.sub(r"\|\s*\n\n\s*\|", "|\n|", out)     # 表格行之间不能空行，否则渲染不成表格


def clean_answer(ans: str) -> str:
    """答案出口规范化：去小问编号前缀、合并空白、多个可接受答案用 ## 分隔。"""
    ans = (ans or "").strip()
    if not ans:
        return ""
    out = []
    for part in ans.split("##"):
        p = part.strip()
        m = LEAD_LABEL.match(p)
        if m and len(p) > m.end():          # 「① a」→ 去掉编号；答案本身就是「③」时保留
            rest = re.sub(r"^\s*[.．、]\s*", "", p[m.end():])
            p = rest if rest.strip() else p[m.end():]
        p = re.sub(r"[ \t\u3000\u00a0]+", " ", p).strip()
        if p:
            out.append(p)
    return "##".join(out)


def is_short_answer(ans: str) -> bool:
    first = (ans or "").split("##")[0]
    return len(_strip_ws(first)) <= SHORT_MAX


def split_scores(total: int, tags: list[str], ans_text: str, review: list[str], label: str) -> list[int]:
    """把大题满分分到各空上：**长句空优先吃高分**（长句权重 2 / 短空权重 1）。

    - 按权重比例算出理想的每空分值（total·wᵢ/Σw），最大余数法（四舍五入）取整；
    - 每空至少 1 分；取整后总额与满分不等时，多退少补（补分按小数部分降序、长句优先、
      再按题序；退分按小数部分升序、短空优先）；
    - 最后做单调修正：任何长句空的分不得低于任何短空（保证「长句优先吃高分」）；
    - 保证 Σ == total，且完全由 tags 决定（不看时间/随机数），可复现。
    """
    n = len(tags)
    if n <= 0:
        return []
    total = int(total or 0)
    if total <= 0:
        review.append(f"{label}：大题满分为 0，{n} 空全部记 0 分（需人工核对卷面分值）")
        return [0] * n

    longs = [i for i, t in enumerate(tags) if t == "long"]
    shorts = [i for i, t in enumerate(tags) if t != "long"]
    per = re.search(r"每空\s*(\d+)\s*分", ans_text or "")
    if total < n:                                   # 满分比空数还少：做不到「每空至少 1 分」
        order = sorted(range(n), key=lambda i: (tags[i] != "long", i))
        scores = [0] * n
        for i in order[:total]:
            scores[i] = 1
        review.append(f"{label}：满分 {total} 分少于 {n} 个空，无法每空至少 1 分，"
                      f"改为长句优先各给 1 分 → {scores}（需人工核对卷面分值）")
        return scores

    w = [2 if t == "long" else 1 for t in tags]
    W = sum(w)
    ideal = [total * wi / W for wi in w]
    scores = [max(1, int(math.floor(x))) for x in ideal]      # 每空至少 1 分
    R = total - sum(scores)
    if R > 0:                                       # 假分不够：按小数部分降序补（长句优先、再按题序）
        order = sorted(range(n), key=lambda i: (-(ideal[i] - math.floor(ideal[i])),
                                                tags[i] != "long", i))
        for k in range(min(R, n)):
            scores[order[k]] += 1
        R = total - sum(scores)
        while R > 0:                                # 极端情况下继续按题序补
            for i in range(n):
                if R <= 0:
                    break
                scores[i] += 1
                R -= 1
    elif R < 0:                                     # 取整后超了：按小数部分升序退（短空优先、倒序）
        order = sorted(range(n), key=lambda i: (ideal[i] - math.floor(ideal[i]),
                                                tags[i] == "long", -i))
        k = 0
        while R < 0 and k < 20 * n:
            i = order[k % n]
            if scores[i] > 1:
                scores[i] -= 1
                R += 1
            k += 1
    for _ in range(100):                            # 单调修正：长句分 ≥ 短空分
        if not longs or not shorts:
            break
        hi = max(scores[i] for i in shorts)
        lo_i = min(longs, key=lambda i: (scores[i], i))
        if scores[lo_i] >= hi:
            break
        donor = max(shorts, key=lambda i: (scores[i], -i))
        if scores[donor] <= 1:
            break
        scores[donor] -= 1
        scores[lo_i] += 1

    seq = " ".join(f"{'长' if t == 'long' else '短'}{s}" for t, s in zip(tags, scores))
    note = f"（参考答案标注「每空 {per.group(1)} 分」，按长句优先规则重算）" if per else ""
    review.append(f"{label}：{total} 分 / {n} 空（长句 {len(longs)} · 短空 {len(shorts)}）"
                  f"长句权重2/短空权重1 → {seq}（Σ={sum(scores)}）{note}")
    if sum(scores) != total:                        # 兜底：必须配平
        scores[-1] += total - sum(scores)
        review.append(f"{label}：分值兜底配平，最后一空调整为 {scores[-1]} 分")
    return scores


def blank_tags(content: str) -> list[str]:
    """按 content 里占位符出现的顺序，给出每空 'long' / 'short'。"""
    return ["long" if m.group(0).startswith("<InputLong") else "short"
            for m in re.finditer(r"<InputLong></InputLong>|<Input></Input>", content or "")]


# ──────────────────────────────────────────────────────────────────
# 卷面切分
# ──────────────────────────────────────────────────────────────────

def split_marks(text: str, lo: int, hi: int) -> list[dict]:
    """按题号行切段；lo..hi 限定题号范围（避开 16 题内部的 1./2. 列表编号）。

    题号行前面常粘着上一段落的图片（w2m 会把 `图片` 与 `7．` 挤在同一行的开头）：
    - 题干区里这张图属于**下一题**（`lead` 单独取出，拼到下一题开头）；
    - 答案区里这张图属于**上一题的答案**（`text_full` 把它留在上一段末尾）。
    """
    marks = [m for m in QMARK.finditer(text) if lo <= int(m.group(2)) <= hi]
    out = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        out.append({
            "no": int(m.group(2)),
            "score": int(m.group(3)) if m.group(3) else None,
            "lead": (m.group(1) or "").strip(),      # 紧贴题号前的图片（属于本题题干）
            "text": text[m.end():(nxt.start() if nxt else len(text))],
            "text_full": text[m.end():(nxt.start(2) if nxt else len(text))].rstrip(" \t*\n"),
        })
    return out


FIGREF = re.compile(r"如图|如下图|见图|结果如图|图\s*\d|图[一二三四]|绘制|图中")


def rebalance_leads(items: list[dict]) -> list[dict]:
    """题号前粘着的图片到底属于谁：看下一题开头有没有「如图/图 N」这种引用。

    引用得到 → 属于下一题（西城 7 题那种）；引用不到 → 属于上一题的最后一个小问
    （海淀 21 题前面那张其实是 20 题图 3）。题干只会被拼进一处，图片不重不漏。
    """
    for i, it in enumerate(items):
        if not it.get("lead"):
            continue
        if FIGREF.search(it["text"][:180]):
            continue
        if i > 0:
            items[i - 1]["text"] = items[i - 1]["text"].rstrip() + "\n\n" + it["lead"] + "\n\n"
            it["lead"] = ""
    return items


def cut_tail(seg: str) -> str:
    """砍掉段尾混进来的「第二部分/参考答案/第X页」等噪声。"""
    cut = len(seg)
    for pat in (re.compile(r"(?m)^[ \t]*\**[ \t]*第[二2]部分"),
                re.compile(r"(?m)^[ \t]*\**[ \t]*参考答案"),
                re.compile(r"(?m)^[ \t]*\**[ \t]*本部分共\s*\d+\s*题"),
                re.compile(r"(?m)^[ \t]*第\s*\d+\s*页")):
        m = pat.search(seg)
        if m and m.start() < cut:
            cut = m.start()
    return seg[:cut]


def first_big_mark(body: str) -> int:
    """第二部分第一道大题（16 题）在原文里的位置；在这之前是选择题区。

    生物卷大题内部的小问编号常是 Word 自动列表（1. 2. 3.），会与选择题题号撞车，
    所以必须先把「选择题区 / 大题区」切开，再各自按题号切。
    """
    for m in QMARK.finditer(body):
        if int(m.group(2)) > N_CHOICE:
            return m.start()
    return len(body)


# 大题题号 + 分值行（Word 自动编号会把「16.」写成「1.」，分值是最可靠的信号）
BIGMARK = re.compile(r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、]\s*"
                     r"[（(]\s*(\d+)\s*分\s*[）)]")


def score_marks(text: str) -> list[re.Match]:
    return [m for m in BIGMARK.finditer(text) if int(m.group(3)) >= 5]


def split_marks_by_score(text: str, ans_order: list[int]) -> list[dict] | None:
    """按「（N分）」标记切大题，题号按参考答案区的题号顺序重编（对付 Word 自动编号）。"""
    marks = score_marks(text)
    if len(marks) < 3:
        return None
    out = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        seg = text[m.end():(nxt.start() if nxt else len(text))]
        out.append({
            "no": ans_order[i] if i < len(ans_order) else int(m.group(2)),
            "score": int(m.group(3)),
            "lead": (m.group(1) or "").strip(),
            "text": seg,
            "text_full": seg.rstrip(" \t*\n"),
        })
    return out


# 选项段里可能一行塞两个选项（「1. xxx」+「B. yyy」），按行首编号/字母拆开
OPT_SPLIT = re.compile(r"(?m)^[ \t]*(?:[1-9]|[A-D])\s*[．.、]\s*")


def _looks_stem(txt: str) -> bool:
    """像不像题干（用来区分「Word 自动编号写小的题号」和「自动编号的选项段」）。"""
    t = (txt or "").strip()
    if not t or re.match(r"^[A-D]\s*[．.、]", t):
        return False
    return len(t) >= 40 and bool(re.search(r"(的是|？|（\s*）|\(\s*\)|正确|错误|不合理|下列)", t))


def regroup_choice_segments(segs: list[dict], declared: int | None,
                            review: list[str] | None = None) -> list[dict]:
    """把按题号切出来的碎片重新归组成「题」：一个题干段 + 若干「选项段」。

    有的卷子把选项写成 Word 自动编号（1./2./3./4.），会被当成 4 道小题切出来；
    有的卷子又把某道题的题号自动编号成了「1.」。统一处理：
      - 段号 == 期望题号、且（够长 或 这是第一段）→ 新题；
      - 段号比期望题号小、但明显是题干 → 也当新题（题号被 Word 写小了），期望题号往后顺延；
      - 其余一律当成当前题的候选选项段（后面按 A–D 顺序补位）。
    """
    out: list[dict] = []
    expect = 1
    extra = 0
    for s in segs:
        txt = s["text"]
        is_stem = False
        if s["no"] == expect and (len(txt.strip()) >= 40 or not out):
            is_stem = True
        elif s["no"] < expect and _looks_stem(txt):
            is_stem = True
        if is_stem:
            out.append(dict(s, _opt_extra=[]))
            expect = max(expect, s["no"]) + 1
        elif out:
            out[-1]["_opt_extra"].append(txt.strip())
            extra += 1
        else:
            out.append(dict(s, _opt_extra=[]))
            expect = s["no"] + 1
    if extra and review is not None:
        review.append(f"选择题：{extra} 段「自动编号的选项」已并回所属题目")
    return out


def parse_choice_questions(body: str, table_opt: list[int] | None = None,
                           choice_notes: list[str] | None = None,
                           declared: int | None = None) -> list[dict]:
    """第一部分 15 道单选（选项可能一行一个，也可能一行多个 A.… B.…）"""
    segs = rebalance_leads(split_marks(body, 1, MAX_CHOICE))
    segs = regroup_choice_segments(segs, declared, choice_notes)
    table_opt = table_opt if table_opt is not None else []
    choice_notes = choice_notes if choice_notes is not None else []
    out = []
    for s in segs:
        seg = cut_tail(s["text"]).strip()
        if s["lead"]:
            seg = s["lead"] + "\n" + seg
        found = {}
        for m in OPT_MARK.finditer(seg):
            k = m.group(1)
            if k not in found:
                found[k] = m  # 每个字母取第一次出现
        opts, content = [], seg
        tbl_opts, tbl_content, tbl_mode = options_from_table(seg)
        if tbl_mode == "cell_prefix":                # 选项排成 2×2 表格（纯排版）→ 取文字做选项
            opts, content = tbl_opts, tbl_content
            log(f"    第{s['no']}题选项是 2×2 表格排版，已取文字做 A–D 选项")
        elif tbl_mode == "first_col":                # 表格就是选项内容 → 表格留题干，选项只给字母
            opts, content = tbl_opts, seg
            # 有的卷子在表格后还多一行「A. A B. B C. C D. D」的字母占位，纯噪声，去掉
            content = re.sub(r"(?m)^[ \t]*(?:[A-D]\s*[.．、]\s*[A-D]\s*)+\)?[ \t]*$", "", content)
            table_opt.append(s["no"])
            log(f"    第{s['no']}题选项是一张表（首列 A–D），表格留在题干，options 只给字母")
        elif all(k in found for k in "ABCD"):
            first = found["A"].start(1)          # 选项字母的位置（不是前导空格/右括号）
            content = seg[:first].strip()
            order = ["A", "B", "C", "D"]
            for i, k in enumerate(order):
                start = found[k].end()
                end = found[order[i + 1]].start(1) if i + 1 < len(order) else len(seg)
                opts.append({"id": k, "label": seg[start:end].strip()})
        else:
            pass
        if len(opts) < 4:                            # 自动编号的选项段按 A–D 顺序补位
            for t in (s.get("_opt_extra") or []):
                for lab in OPT_SPLIT.split(t):
                    if len(opts) >= 4:
                        break
                    lab = lab.strip()
                    if lab:
                        opts.append({"id": "ABCD"[len(opts)], "label": lab})
        if len(opts) < 4:
            # 选项全是图、只用「A B C D」标在图片下面：把四张图留在题干，options 只给字母
            m_abcd = re.search(r"A[ \t]*B[ \t]*C[ \t]*D[ \t]*$", (content or "").strip())
            if m_abcd and IMG_REF.search(content or ""):
                opts = [{"id": k, "label": k} for k in "ABCD"]
                content = content.strip()[:m_abcd.start()].rstrip()
                table_opt.append(s["no"])
                log(f"    第{s['no']}题选项是四张图（只有 A B C D 标注），图留在题干，options 只给字母")
        if len(opts) < 4:
            log(f"    ⚠ 第{s['no']}题选项只取到 {len(opts)} 个（标记 {sorted(found)}）")
        content = strip_table_remnant(content)
        out.append({"id": s["no"], "questionType": "single", "content": content,
                    "options": opts, "answer": "", "analysis": "", "score": CHOICE_SCORE,
                    "_printed": s["no"], "_qscore": s["score"]})
    # 选择题题号一律按出现顺序重编 1..N：Word 自动编号常把个别题号写错（重复/跳号），
    # 而答案表（1–15）是按位置给的，按位置编才对应得上。
    nos = [q["id"] for q in out]
    if nos != list(range(1, len(out) + 1)):
        log(f"    选择题题号不规整 {nos} → 按出现顺序重编 1–{len(out)}")
    for i, q in enumerate(out, 1):
        q["id"] = i
    return out


def options_from_table(seg: str) -> tuple[list[dict], str, str]:
    """选项被排成表格的选择题，两种版式分开处理（返回 (options, 新题干, 版式)）。

    - 版式 `cell_prefix`：每个单元格自带字母前缀（`| A．xxx | B．yyy |`，2×2 排布常见）——
      这只是**排版**，选项是正常文字：取文字做 label，表格从题干里去掉；
    - 版式 `first_col`：表头 + 首列 A/B/C/D、其余列是真正的内容（如「选项｜研究结果｜推测或推论」）——
      **尊重原卷**：表格完整留在题干（markdown 表），options 只给字母 A–D，
      因为选项内容就是表格里的行，拆成 label 会丢掉表头语义。
    """
    lines = seg.split("\n")
    blocks, cur = [], []
    for i, line in enumerate(lines):
        if TBL_SEP.match(line):
            cur.append(i)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    for idxs in blocks:
        rows = [_cell_split(lines[i]) for i in idxs]
        rows = [r for r in rows if not _is_sep_row(r)]
        if len(rows) < 2:
            continue
        # 版式一：单元格自带「A．」前缀（纯排版）
        cand: dict[str, str] = {}
        for r in rows:
            for c in r:
                m = re.match(r"^\**([A-D])\**\s*[．.、]\s*(.+)$", c.strip())
                if m and m.group(1) not in cand:
                    cand[m.group(1)] = _clean_cell(m.group(2))
        if len(cand) == 4:
            keep = [l for i, l in enumerate(lines) if i not in set(idxs)]
            return [{"id": k, "label": cand[k]} for k in "ABCD"], "\n".join(keep).strip(), "cell_prefix"
        # 版式二：首列是 A/B/C/D（表格就是选项内容）
        got = {re.sub(r"[^A-Da-d]", "", (r[0] or "").strip()).upper() for r in rows[1:]}
        if {"A", "B", "C", "D"} <= got:
            # 表格留在题干（只做规范化，不删行）
            return [{"id": k, "label": k} for k in "ABCD"], seg, "first_col"
    return [], seg, ""


def strip_table_remnant(text: str) -> str:
    """去掉题干尾部残留的半截表格（Word 表格被识别成选项后常留一行「|」）。"""
    lines = (text or "").split("\n")
    while lines:
        t = lines[-1].strip()
        if t == "" or re.fullmatch(r"\|[\s|]*", t) or (t.startswith("|") and not t.endswith("|")):
            lines.pop()
        else:
            break
    return "\n".join(lines).rstrip()


def parse_choice_answers(ans: str) -> dict[int, str]:
    """选择题答案：能吃「题号/答案」表格、「一行题号 + 一行答案」表格、「1-5 DADCA」区间行、
    行内「1.C 2.D」、以及逐题「1．【答案】C」五种写法。"""
    out: dict[int, str] = {}
    pending: list[int] = []

    # ① 表格（可多段：1–10 一行、11–15 一行）
    for line in ans.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        head = re.sub(r"[\s*]", "", cells[0])
        if head.startswith("题号"):
            pending = [int(x) for x in (re.sub(r"\D", "", c) for c in cells[1:]) if x]
            continue
        if head.startswith("答案") and pending:
            last = 0
            for n, c in zip(pending, cells[1:]):
                v = re.sub(r"[^A-Da-d]", "", c).upper()
                if not v:
                    continue
                if n in out or n <= last:          # 卷面题号写错（如把 15 写成 5）→ 按位置顺延
                    n = last + 1
                out.setdefault(n, v)
                last = n
            pending = []
            continue
        vals = [c.replace("*", "").strip() for c in cells]
        if all(v == "" or v.isdigit() for v in vals):
            nums = [int(v) for v in vals if v.isdigit()]
            if len(nums) >= 3:
                pending = nums
            continue
        if pending and all(not v or re.fullmatch(r"[A-Da-d]", v) for v in vals):
            last = 0
            for n, v in zip(pending, [v.upper() for v in vals if v]):
                if n in out or n <= last:
                    n = last + 1
                out.setdefault(n, v)
                last = n
            pending = []

    # ② 区间行「1-5 DADCA 6-10 DABCC」
    for m in re.finditer(r"(\d{1,2})\s*[-–—~至]\s*(\d{1,2})\s*[:：]?\s*([A-Da-d]{2,})", ans):
        a, b, seq = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if b - a + 1 == len(seq):
            for i, ch in enumerate(seq):
                out.setdefault(a + i, ch)

    # ③ 行内「1.C 2.D」
    for m in re.finditer(r"(\d{1,2})\s*\\?[.．、]\s*([A-Da-d])(?![A-Za-z])", ans):
        out.setdefault(int(m.group(1)), m.group(2).upper())

    # ④ 逐题「1．【答案】C」
    for m in re.finditer(r"(?m)^\s*\**\s*(\d{1,2})\s*\\?\s*[.．、]\s*【答案】\s*\**\s*([A-Da-d])\b", ans):
        out.setdefault(int(m.group(1)), m.group(2).upper())

    # ⑤ 整行裸字母串（如「ACDAD CDBCC BDDBA」）→ 按顺序对应 1..N
    for m in re.finditer(r"(?m)^[ \t]*([A-D](?:[ \t]*[A-D]){9,})[ \t]*$", ans):
        seq = re.sub(r"[^A-D]", "", m.group(1))
        if len(seq) >= 10 and not out:
            for i, ch in enumerate(seq, 1):
                out.setdefault(i, ch)
    return out


def ensure_web_images(md: Path, review: list[str]) -> None:
    """nginx 只放行 png/jpg/jpeg/gif/webp：emf/wmf 先转 png（复用现有脚本）。"""
    txt = md.read_text(encoding="utf-8")
    refs = set(IMG_REF.findall(txt))
    emf = [r for r in refs if Path(r).suffix.lower() == ".emf"
           and not (md.parent / r).with_suffix(".png").exists()]
    if emf:
        repo = Path(__file__).resolve().parent
        try:
            subprocess.run([str(repo / "venv" / "bin" / "python3"), str(repo / "convert_emf_to_png.py"),
                            str(md.parent / "images")], capture_output=True, timeout=1800)
        except Exception as e:
            review.append(f"EMF 转 PNG 失败：{str(e)[:60]}")
    wmf = [r for r in refs if Path(r).suffix.lower() == ".wmf"
           and not (md.parent / r).with_suffix(".png").exists()]
    if wmf:
        try:
            from convert_chemistry_md import wmf_to_png
            wmf_to_png([md.parent / r for r in wmf])
        except Exception as e:
            review.append(f"WMF 转 PNG 失败：{str(e)[:60]}")


def ai_json(prompt: str, max_tokens: int = 8000) -> dict:
    try:
        got = chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt}], max_tokens)
    except Exception as e:
        log(f"    AI 失败：{str(e)[:120]}")
        return {}
    return got if isinstance(got, dict) else {}


def fix_placeholders(content: str, answers: list[str], review: list[str], label: str) -> str:
    """占位符按答案顺序重排：数量对齐、长短按答案字数判定（导入端只认这两种写法）。"""
    tokens = list(PH.finditer(content or ""))
    if len(tokens) != len(answers):
        review.append(f"{label}：content 里 <Input> 出现 {len(tokens)} 次，空数 {len(answers)}，"
                      f"按较少的一侧对齐（需人工复核题干与答案对应关系）")
    pieces, last = [], 0
    for i, m in enumerate(tokens):
        ans = answers[i] if i < len(answers) else ""
        tag = "<Input></Input>" if is_short_answer(ans) else "<InputLong></InputLong>"
        pieces.append(content[last:m.start()])
        pieces.append(tag)
        last = m.end()
    pieces.append(content[last:])
    return "".join(pieces)


def drop_empty_blanks(content: str, answers: list[str], review: list[str],
                      label: str) -> tuple[str, list[str]]:
    """兜底：仍然没有参考答案的空，直接去掉这个占位（该处按无需作答处理），保证「每空都有答案」。

    只在「模型返修两轮后还有空答案」时才会走到，会在 review 里逐条记下来供人工补。
    """
    toks = list(PH.finditer(content))
    if not toks:
        return content, answers
    if len(answers) >= len(toks) and all((a or "").strip() for a in answers[:len(toks)]):
        return content, answers
    pieces, out_ans, last = [], [], 0
    for i, m in enumerate(toks):
        ans = answers[i] if i < len(answers) else ""
        pieces.append(content[last:m.start()])
        last = m.end()
        if not (ans or "").strip():
            review.append(f"{label}：第{i + 1}空没有参考答案（模型返修后仍缺），已去掉该空占位，"
                          f"该处按无需作答处理，需人工核对")
            continue
        pieces.append("<Input></Input>" if is_short_answer(ans) else "<InputLong></InputLong>")
        out_ans.append(ans)
    pieces.append(content[last:])
    return "".join(pieces), out_ans


def strip_stem_prefix(content: str, no: int) -> str:
    """题干不要带题号与分值。"""
    c = content.strip()
    c = re.sub(rf"^(?:第\s*{no}\s*题|{no}\s*[．.、])\s*", "", c)
    c = re.sub(r"^[（(]\s*共?\s*\d+\s*分\s*[）)]\s*", "", c)
    return c.strip()


def strip_ph_spaces(content: str) -> str:
    """中文里紧贴答题空的空格去掉（原卷的空是空白字符，转换后会多出空格）。"""
    content = re.sub(r"(?<=[\u4e00-\u9fff，。；：、！？])(?:[ \t\u3000\u00a0]+)(?=<Input)", "", content)
    content = re.sub(r"(</Input>|</InputLong>)[ \t\u3000\u00a0]+(?=[\u4e00-\u9fff，。；：、！？])", r"\1", content)
    return content


def _norm_cmp(s: str) -> str:
    return re.sub(r"[\s\u3000，。；：、,.;:!?！？（）()【】\[\]\"'“”‘’…—\-－]", "", s or "")


# 理综（北京理综里的生物部分）口径：选择题 5 道 × 6 分 = 30，非选择题 3 道共 50，生物部分 80 分
ZONGHE_CHOICE_SCORE = 6
ZONGHE_ESSAY_TOTAL = 50

ANSWER_MARK = re.compile(r"(?m)^[ \t]*(\d{1,2})\s*\\?\s*[．.]\s*\**\s*")
ANS_LETTER = re.compile(r"(?:故选|故答案为|答案为|选择|答案)\s*[:：]?\s*\**\s*([A-D])(?![A-Za-z])")


def declared_count(text: str) -> int | None:
    """卷面声明的题数：「本部分共 15 题」「共 5 小题」。"""
    m = DECLARED_N.search(text or "")
    try:
        n = int(m.group(1))
        return n if 1 <= n <= 40 else None
    except Exception:
        return None


def explained_answer_blocks(ans_text: str, max_no: int = 12) -> dict[int, tuple[str | None, str]]:
    """「解析版」参考答案：按题号切成块 → {题号: (选择题字母 or None, 整块文字)}。

    只认行首的「N．」/「N.」（不认「N、」，那是解析正文里的序号），避免把解析小节当成题目。
    """
    marks = [m for m in ANSWER_MARK.finditer(ans_text or "") if int(m.group(1)) <= max_no]
    out: dict[int, tuple[str | None, str]] = {}
    for i, m in enumerate(marks):
        no = int(m.group(1))
        if no in out:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(ans_text)
        text = (ans_text[m.end():end] or "").strip()
        letter = ANS_LETTER.search(text)
        out[no] = (letter.group(1) if letter else None, text)
    return out


CHOICE_PDF_PROMPT = """这是一张北京高中生物试卷的页面图像（第一部分 选择题）。

请**只**转写本页的第 {nos} 题（别的题不要），只输出 JSON：
{{"questions": [{{"no": 14, "stem": "题干", "options": [{{"id": "A", "label": "…"}}, {{"id": "B", "label": "…"}}, {{"id": "C", "label": "…"}}, {{"id": "D", "label": "…"}}]}}]}}

要求：
1. no 用卷面题号；只给 {nos} 这几个题号；
2. stem 只放题干（不要把选项写进题干），填空下划线/空格照抄，公式用 $…$，有图就写 <图片>；
3. 四个选项必须齐全、逐字照抄，不要改写、不要合并；
4. 本页没有该题就返回空数组；只输出 JSON，不要解释。
"""


def pdf_of(md: Path, type_dir: str, title: str) -> Path | None:
    """教师版 PDF 的路径（MD 目录名可能带/不带「（教师版）」）。"""
    for cand in (GAOZHONG / type_dir / SUBJECT / f"{md.parent.name}.pdf",
                 GAOZHONG / type_dir / SUBJECT / f"{title}（教师版）.pdf",
                 GAOZHONG / type_dir / SUBJECT / f"{title}.pdf"):
        if cand.exists():
            return cand
    return None


def transcribe_choices_from_pdf(pdf: Path, nos: list[int], review: list[str],
                                label: str) -> dict[int, dict]:
    """从教师版 PDF 的题干页转写指定的几道选择题（docx 转丢/串题时补救）。

    一页只问一次、且只要指定的题号，避免整页转写把输出撑爆。
    """
    pages = _answer_pages_text(pdf)
    if not pages:
        return {}
    stop = next((i for i, p in enumerate(pages) if "参考答案" in p), len(pages))
    page_of: dict[int, int] = {}
    for no in nos:
        pat = re.compile(rf"(?<![\d.]){no}\s*[．.]")
        page = next((i for i in range(stop) if pat.search(pages[i])), None)
        if page is not None:
            page_of[no] = page
    if not page_of:
        review.append(f"{label}：PDF 里没找到第 {'、'.join(map(str, nos))} 题的题干页")
        return {}
    got: dict[int, dict] = {}
    buckets: dict[int, list[int]] = {}
    for no, page in page_of.items():
        buckets.setdefault(page, []).append(no)
    for page, wants in sorted(buckets.items()):
        tmp = Path(tempfile.mkdtemp(prefix="biopdf_"))
        try:
            shots = [p for p in (_render_page(pdf, page + 1, tmp),
                                 _render_page(pdf, page + 2, tmp) if page + 1 < stop else None)
                     if p is not None]
            if not shots:
                continue
            content = [{"type": "text", "text": CHOICE_PDF_PROMPT.format(
                nos="、".join(str(x) for x in sorted(wants))) + (
                "\n（图 1 是第 %d 页，图 2 是紧随其后的第 %d 页；题干和选项可能分跨两页，"
                "两页都看后再作答。）" % (page + 1, page + 2) if len(shots) > 1 else "")}]
            for k, sh in enumerate(shots, 1):
                content.append({"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(sh.read_bytes()).decode()}})
            res = _vision_bbox([
                {"role": "system", "content": "你是试卷录入助手，只输出 JSON。"},
                {"role": "user", "content": content},
            ], VISION_MODEL, 2500)
            for q in (res.get("questions") or []):
                try:
                    n = int(re.sub(r"\D", "", str(q.get("no"))))
                except Exception:
                    continue
                opts = [{"id": str(o.get("id") or "ABCD"[k]).upper()[:1],
                         "label": str(o.get("label") or "").strip()}
                        for k, o in enumerate(q.get("options") or [])]
                stem = str(q.get("stem") or "").strip()
                if n in wants and stem and len(opts) == 4:
                    got[n] = {"content": stem, "options": opts}
            review.append(f"{label}：从 PDF 第 {page + 1} 页转写第 "
                          f"{'、'.join(map(str, sorted(wants)))} 题 → "
                          + ("成功" if any(n in got for n in wants) else "失败"))
        except Exception as e:
            review.append(f"{label}：PDF 转写第 {page + 1} 页异常（{str(e)[:60]}）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return got


SOLVE_PROMPT = r"""下面是北京高中生物试卷的若干道单项选择题（每道给了题干和 A–D 四个选项）。请逐题独立作答。

只输出 JSON：
{"answers": [{"no": 1, "answer": "C", "reason": "一句话理由"}, ...]}

要求：
1. answer 只能是 A、B、C、D 之一；reason 不超过 45 字，说明选它的依据；
2. 题目用【第 k 题】编号（k 从 1 开始，**与卷面题号无关**），answers 的 no 必须对应这个 k；
3. 给出的每道题都要作答，一道都不能漏；
4. 只输出 JSON，不要解释。

题目：
{ITEMS}
"""


def ai_solve_choice(qs: list[dict], batch: int = 8) -> dict[int, tuple[str, str]]:
    """让模型逐题作答（用来给「答案表条数 ≠ 题干块数」的卷子做内容级对齐）。"""
    out: dict[int, tuple[str, str]] = {}
    for i in range(0, len(qs), batch):
        part = qs[i:i + batch]
        items = []
        for k, q in enumerate(part, 1):
            opts = "\n".join(f"{o['id']}．{o['label']}" for o in (q.get("options") or []))
            items.append(f"【第{k}题】{q['content']}\n{opts}")
        got = ai_json(SOLVE_PROMPT.replace("{ITEMS}", "\n\n".join(items)), max_tokens=3000)
        for a in (got.get("answers") or []):
            try:
                n = int(re.sub(r"\D", "", str(a.get("no"))))
            except Exception:
                continue
            v = str(a.get("answer") or "").strip().upper()[:1]
            if 1 <= n <= len(part) and v in "ABCD":
                out[part[n - 1]["id"]] = (v, str(a.get("reason") or "").strip()[:120])
    return out


def align_choice_answers(choice_qs: list[dict], key: dict[int, str],
                         derived: dict[int, tuple[str, str]],
                         review: list[str]) -> dict[int, str]:
    """答案表的条数比题干块数少时（题号重复导致），按**内容**对齐，而不是按位置硬套。

    做法：枚举「哪一块是没有答案表条目的那一块」（N 个假设），每个假设把答案表的
    15 个字母依次对到其余 15 块上，用两把尺子打分：
      - 主要：模型逐题推导的答案与答案表字母的吻合数；
      - 次要：块上印的题号与答案表题号一致的个数（帮助在打平时做判断）。
    得分最高的假设胜出；被打掉的那一块用模型推导的答案补，并标注「AI 推导，原卷答案表缺失」。
    """
    n = len(choice_qs)
    kvals = [key[k] for k in sorted(key)]
    if not kvals or n <= len(kvals):
        return dict(key)
    ids = [q["id"] for q in choice_qs]
    printed = [q.get("_printed") or q["id"] for q in choice_qs]
    best = None
    for skip in range(n):
        blocks = [i for i in range(n) if i != skip]
        if len(blocks) != len(kvals):
            continue
        hit = sum(1 for b, kv in zip(blocks, kvals) if derived.get(ids[b], ("",))[0] == kv)
        num_ok = sum(1 for b, num in zip(blocks, sorted(key)) if printed[b] == num)
        cand = (hit, num_ok, -abs(skip - (n - 1)), -skip, skip)
        if best is None or cand > best:
            best = cand
    if best is None:
        return dict(key)
    skip = best[4]
    blocks = [i for i in range(n) if i != skip]
    mapping: dict[int, str] = {}
    for b, kv in zip(blocks, kvals):
        mapping[ids[b]] = kv
    dropped = choice_qs[skip]
    d_ans, d_why = derived.get(ids[skip], ("", ""))
    review.append(f"选择题对齐：答案表 {len(kvals)} 个、题干 {n} 块 → 按内容对齐"
                  f"（模型推导与答案表吻合 {best[0]}/{len(kvals)}、题号一致 {best[1]}/{len(kvals)}），"
                  f"跳过第 {skip + 1} 块（印的题号 {printed[skip]}：「{dropped['content'][:16]}…」）")
    if d_ans:
        mapping[ids[skip]] = d_ans
        review.append(f"第{ids[skip]}题（原卷题号 {printed[skip]}）：原卷答案表缺失该题答案，"
                      f"按模型推导填 {d_ans}（理由：{d_why or '—'}）——AI 推导，原卷答案表缺失")
        dropped["analysis"] = f"AI 推导，原卷答案表缺失：{d_why}" if d_why else "AI 推导，原卷答案表缺失"
    else:
        review.append(f"第{ids[skip]}题（原卷题号 {printed[skip]}）：原卷答案表缺失且模型没给出推导，"
                      f"答案留空，需人工补")
    return mapping


# ──────────────────────────────────────────────────────────────────
# markdown 表格：规范化（表头 / 列数对齐 / 数字单位不乱序 / 公式仍 LaTeX）
# ──────────────────────────────────────────────────────────────────

TBL_SEP = re.compile(r"^\s*\|")
TBL_CELL = re.compile(r"(?<!\\)\|")


def _cell_split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in TBL_CELL.split(s)]


def _is_sep_row(cells: list[str]) -> bool:
    """`| --- | :---: |` 这种分隔行（Word 合并单元格会掺进空列，也算”。"""
    body = [c.replace(" ", "") for c in cells if c.strip()]
    return bool(body) and all(re.fullmatch(r":?-+:?", c) for c in body)


def _clean_cell(cell: str) -> str:
    """单元格清洗：公式（$…$）与 LaTeX 原样保留，只收拾 Word 转出来的空格残留。"""
    c = cell.strip()
    if not c:
        return ""
    c = c.replace("\\_\\_", "_").replace("\\.", ".").replace("\\-", "-")
    # 数字被拆开：`10. 00` → `10.00`、`2. 13` → `2.13`（只在纯数字上下文里动手）
    c = re.sub(r"(?<![\d.])(\d+(?:\.\s?\d+)+)", lambda m: re.sub(r"\.\s+", ".", m.group(1)), c)
    c = re.sub(r"(?<=\d)\s+%", "%", c)                       # `43 %` → `43%`
    c = re.sub(r"(?<=\d)\s+(?=[a-zA-Z]{1,4}\b)", " ", c)     # `12.5 ng` 保持一个空格
    c = re.sub(r"\(\s*([a-zA-Z]+)\s*/\s*([a-zA-Z]+)\s*\)", r"(\1/\2)", c)   # `(ng / g)` → `(ng/g)`
    c = re.sub(r"<br\s*/?>(?:\s*<br\s*/?>)*", " ", c, flags=re.I)             # 单元格内换行 → 空格
    c = re.sub(r"[ \t\u3000\u00a0]{2,}", " ", c)
    return c.strip()


def normalize_md_tables(text: str) -> str:
    """把 Markdown 表格规范成 remark-gfm 一定认的形态。

    Word 转出来的表常带「合并单元格」的残留空列、重复/错位的分隔行、行内单元格数不齐，
    GFM 遇到这些会整表退化成一行文字。这里统一：
      - 表头 = 表格块第一行；列数 = 各行单元格数的最大值；
      - 只保留一行分隔行 `| --- | …`，列数与表头一致；
      - 每行补空/截断到同一列数；单元格数字与单位只清洗空格残留，公式原样；
      - 表格块前后各留一个空行。
    """
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if not TBL_SEP.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip():
            if not TBL_SEP.match(lines[i]):      # 表格块中间不该有非表格行，遇到就断开
                break
            block.append(lines[i])
            i += 1
        rows = [_cell_split(l) for l in block if not _is_sep_row(_cell_split(l))]
        rows = [[_clean_cell(c) for c in r] for r in rows]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:                        # 只有表头没有内容 → 保留原样
            out.extend(block)
            continue
        ncol = max(len(r) for r in rows)
        ncol = max(ncol, 2)
        fixed = []
        for k, r in enumerate(rows):
            r = r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
            fixed.append(r)
        head = fixed[0]
        body = fixed[1:]
        out.append("")
        out.append("| " + " | ".join(head) + " |")
        out.append("| " + " | ".join(["---"] * ncol) + " |")
        for r in body:
            out.append("| " + " | ".join(r) + " |")
        out.append("")
    res = "\n".join(out)
    res = re.sub(r"\n{3,}", "\n\n", res)
    return res.strip("\n") if text.strip() else res


def count_md_tables(text: str) -> int:
    """数 markdown 表格块（连续以 | 开头的行算一块）。"""
    n, prev = 0, False
    for line in (text or "").split("\n"):
        cur = bool(TBL_SEP.match(line))
        if cur and not prev:
            n += 1
        prev = cur
    return n


def _raw_table_blocks(text: str) -> list[str]:
    """抽出原文里的 markdown 表格块（原样，未清洗）。"""
    blocks, cur = [], []
    for line in (text or "").split("\n"):
        if TBL_SEP.match(line):
            cur.append(line)
        else:
            if len(cur) >= 2:
                blocks.append("\n".join(cur))
            cur = []
    if len(cur) >= 2:
        blocks.append("\n".join(cur))
    return blocks


def restore_raw_tables(content: str, seg: str, review: list[str], label: str) -> str:
    """AI 把表格拍平成一行文字时，按【原题】把表格补回来（表格内容以原文为准）。"""
    blocks = _raw_table_blocks(latexify(seg))
    if not blocks:
        return content
    where = []
    for tb in blocks:
        tbn = normalize_md_tables(tb)
        if not tbn.strip():
            continue
        anchor = None
        for mm in re.finditer(r"[^\n]*表[^\n]*", content):
            if re.search(r"下表|如表|见下表|表中|上表|如下表|结果见|见表", mm.group(0)):
                anchor = mm
                break
        if anchor is not None:
            content = content[:anchor.end()] + "\n\n" + tbn + "\n\n" + content[anchor.end():]
            where.append("接在「…表」那句之后")
        else:
            content = content.rstrip() + "\n\n" + tbn + "\n"
            where.append("追加到题干末尾")
    review.append(f"{label}：AI 把 {len(blocks)} 张数据表拍平成了文字，已按原题补回（{'、'.join(where)}）")
    return content


def table_shape_ok(text: str) -> list[str]:
    """表格可被简单解析：分隔行存在且唯一、每行列数与表头一致、列数 ≥2。"""
    errs, i = [], 0
    lines = (text or "").split("\n")
    ti = 0
    while i < len(lines):
        if not TBL_SEP.match(lines[i]):
            i += 1
            continue
        ti += 1
        block = []
        while i < len(lines) and TBL_SEP.match(lines[i]):
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


FIG_NUM = re.compile(r"图\s*([0-9一二三四五六七八九])")


def note_missing_figures(content: str, review: list[str], label: str) -> None:
    """题干引用了「图 N」但图片张数明显不够 → 原卷（docx）本身缺图，记一条待人工确认。"""
    refs = sorted(set(FIG_NUM.findall(content or "")))
    n_img = len(IMG_REF.findall(content or "")) + len(EXAM_REF.findall(content or ""))
    if refs and n_img < len(refs):
        review.append(f"{label}：题干引用了「图{'/'.join(refs)}」（{len(refs)} 处编号），"
                      f"但只找到 {n_img} 张图 —— 原卷 docx 里可能本来就没有这几张图，需人工确认")


def verify_answers(answers: list[str], atext: str, review: list[str], label: str) -> None:
    """逐空核对：答案是否真能在参考答案原文里找到（防止模型改写）。"""
    hay = _norm_cmp(atext)
    if not hay:
        return
    for i, a in enumerate(answers, 1):
        for part in (a or "").split("##"):
            p = _norm_cmp(part.split("\n")[0])
            if not p or p in hay:
                continue
            if IMG_REF.search(part) or PH_ANSWER.match(part):
                continue
            review.append(f"{label} 第{i}空：答案未能在参考答案原文里逐字命中，需人工核对：{part[:40]}")


def attach_answer_images(answers: list[str], atext: str, review: list[str], label: str) -> list[str]:
    """参考答案里「见下图/补充结果」这类空的配图，按顺序挂到对应答案后面。"""
    used = {r for a in answers for r in IMG_REF.findall(a or "")}
    queue = [r for r in IMG_REF.findall(atext or "") if r not in used]
    if not queue:
        return answers
    out = list(answers)
    for i, a in enumerate(out):
        if "![" in a or not queue:
            continue
        if re.search(r"见下图|如图|绘图|绘制|作图|补充.{0,8}(结果|图|方案)|结果如图", a):
            out[i] = a + f"\n![]({queue.pop(0)})"
    if queue:
        review.append(f"{label}：参考答案里的图片 {queue} 没能对上具体的空，需人工挂接")
    return out


def classify(body: str, essays: list[dict], ans_blocks: dict) -> dict[int, str]:
    """一次 AI 批量判断每道大题所属模块（带参考答案摘要，判断更稳）。"""
    items = []
    for e in essays:
        ab = ans_blocks.get(e["no"])
        tail = _strip_ws(re.sub(r"[!\[\]()]", " ", ab["text"]))[:70] if ab else ""
        items.append(f"- {e['no']}｜{_strip_ws(re.sub(r'[!\[\]()]', ' ', e['text']))[:110]}"
                     + (f"｜参考答案：{tail}" if tail else ""))
    got = ai_json(CAT_PROMPT.replace("{ITEMS}", "\n".join(items)), max_tokens=2000)
    out = {}
    for k, v in (got or {}).items():
        try:
            n = int(re.sub(r"\D", "", str(k)))
        except Exception:
            continue
        v = str(v).strip()
        out[n] = v if v in CATS else "实验探究"
    return out


# ──────────────────────────────────────────────────────────────────
# 画图/补图题的参考答案用图片（含从教师版 PDF 截图）
# ──────────────────────────────────────────────────────────────────

EXAM_REF = re.compile(r"!\[[^\]]*\]\((/exam-images/[^)]+)\)")
# 参考答案里的「待补图」占位写法：这类答案还没有真答案，要走 PDF 截图
PH_ANSWER = re.compile(r"^(?:见下图|如下图|见图|如图|答案如图|见解析|略|绘图|作图|见图示|见附图)[。.]?$")
VISION_MODEL = os.getenv("BIO_VISION_MODEL") or os.getenv("MATH_VISION_MODEL") or "deepseek-v4.1-flash"
MAX_PDF_CROPS = int(os.getenv("BIO_MAX_PDF_CROPS", "4"))     # 每卷最多从 PDF 截几张答案图

# 「这一空要学生画图/补图」的题面特征
DRAW_ASK = re.compile(
    r"(?:在图|图中|图上|如图)[^。；]{0,30}(?:补充|补画|绘制|画出|完善|标出|完成|填入|画出)"
    r"|绘制|补画|画出|作图|完善[^。；]{0,12}图|机制图|示意图|补充[^。；]{0,10}(?:实验方案|实验结果|结果|曲线|数据)"
    r"|预期实验结果"
)

FIG_PROMPT = """这是一张北京高中生物试卷【教师版参考答案】的第 {page} 页图像。

请在第 {no} 题第（{sub}）小问的答案里，找出**作为参考答案的那张图**（柱状图 / 曲线图 / 机制流程图 /
示意图 / 遗传图解 / 表格图形），给出它的矩形外框。

要求：
- 只要该小问的答案图本身（含坐标轴、图注、方框与箭头里的全部文字），不要带「（3）」「（4）」这类小问编号；
- **宁大勿小**：左/右/上/下边界都要留出余量，最左侧和最右侧的方框或文字必须完整包进框内，
  绝不能切掉方框的一半或半行字；
- 不要把相邻小问（上一问的柱状图、下一题的正文）算进来；
- 这一页没有该小问的图，就 found=false；
- bbox 用 0–1 归一化坐标 [x0, y0, x1, y1]（相对整页，左上角为原点，右下角为 (1,1)）；
- 只输出 JSON：{{"found": true, "bbox": [x0, y0, x1, y1], "desc": "这张图画的是什么"}}
"""

REFINE_PROMPT = """图 1 是试卷参考答案整页，图 2 是按你上一次给的框裁出来的局部图。

问题：图 2 里这张图的边缘被切掉了（方框被切一半、文字被切掉一半）。
请重新给出第 {no} 题第（{sub}）小问**答案图**的完整外接矩形（相对图 1 整页的 0–1 归一化坐标），
务必把最左侧、最右侧、最上方、最下方的方框与文字都完整包进来。

只输出 JSON：{{"found": true, "bbox": [x0, y0, x1, y1], "desc": "这张图画的是什么"}}
"""


def _img_size(p: Path) -> tuple[int, int]:
    out = subprocess.run(["identify", "-format", "%w %h", str(p)],
                         capture_output=True, text=True, timeout=60)
    try:
        w, h = (out.stdout or "").split()[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def _is_blank_image(p: Path) -> bool:
    """整张图几乎是空白（纯白）→ 认为裁歪了。"""
    out = subprocess.run(["convert", str(p), "-format", "%[fx:standard_deviation]", "info:"],
                         capture_output=True, text=True, timeout=60)
    try:
        return float((out.stdout or "1").strip()) < 0.008
    except Exception:
        return False


def _touches_edges(p: Path) -> tuple[bool, bool, bool, bool]:
    """裁出来的图四边是否贴着墨迹（贴着说明可能被切掉内容）。"""
    w, h = _img_size(p)
    if w < 8 or h < 8:
        return True, True, True, True

    def dark(geo: str) -> bool:
        """这条边上有没有墨迹（用最暗像素判断，细线条也能抓到）。"""
        out = subprocess.run(["convert", str(p), "-crop", geo, "+repage",
                              "-format", "%[fx:minima]", "info:"],
                             capture_output=True, text=True, timeout=60)
        try:
            return float((out.stdout or "1").strip()) < 0.85
        except Exception:
            return False

    return (dark(f"4x{h}+0+0"), dark(f"4x{h}+{w - 4}+0"),
            dark(f"{w}x4+0+0"), dark(f"{w}x4+0+{h - 4}"))


def absolute_img_path(ref: str) -> Path:
    return OUTPUT_DOCX_TO_MD / ref[len("/exam-images/"):]


def drop_missing_answer_images(ans: str, review: list[str], label: str, idx: int) -> str:
    """答案里的图片引用必须真实存在；不存在的直接去掉并留痕（避免前端裂图）。"""
    for ref in EXAM_REF.findall(ans or ""):
        if absolute_img_path(ref).exists():
            continue
        review.append(f"{label} 第{idx}空：答案图片不存在，已去掉引用：{ref}")
        ans = re.sub(r"!\[[^\]]*\]\(" + re.escape(ref) + r"\)", "", ans).strip()
    return ans


def _answer_pages_text(pdf: Path) -> list[str]:
    try:
        r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                           capture_output=True, text=True, timeout=600)
        return (r.stdout or "").split("\f")
    except Exception:
        return []


def _answer_page_of(pages: list[str], no: int, sub: int | None) -> int | None:
    """在「参考答案」区里定位第 no 题（第 sub 小问）所在页，1-based。"""
    start = next((i for i, p in enumerate(pages) if "参考答案" in p), 0)
    qre = re.compile(rf"(?m)^\s*{no}\s*[.．、]")
    qpage = next((i for i in range(start, len(pages)) if qre.search(pages[i])), None)
    if qpage is None:
        return None
    if not sub:
        return qpage + 1
    subre = re.compile(rf"[（(]\s*{sub}\s*[）)]")
    for i in range(qpage, len(pages)):
        if i > qpage and qre.search(pages[i]):
            break
        if subre.search(pages[i]):
            return i + 1
    return qpage + 1


def _gray_profile(shot: Path, crop_geo: str, horizontal: bool) -> list[int]:
    """crop_geo 区域内的灰度剖面：横向返回每列平均灰度，纵向返回每行平均灰度。"""
    m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", crop_geo)
    if not m:
        return []
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return []
    size = f"{w}x1!" if horizontal else f"1x{h}!"
    # 先二值化再压缩：得到的值是「该列/行里墨迹像素的占比」，细线条、稀疏文字都不会被平均掉
    out = subprocess.run(["convert", str(shot), "-crop", crop_geo, "+repage",
                          "-colorspace", "gray", "-threshold", "72%",
                          "-resize", size, "-depth", "8", "gray:-"],
                         capture_output=True, timeout=120)
    return list(out.stdout or b"")


def _expand_left(prof: list[int], x: int, ink_thr: int = 250,
                 gap_frac: float = 0.008, lim_frac: float = 0.18) -> int:
    """从 x 往左走，跨过图内的窄空白，停在大片空白（图的边界）之后。"""
    n = len(prof)
    if n <= 0:
        return x
    gap = max(5, int(gap_frac * n)); lim = max(16, int(lim_frac * n))
    i, white = min(x, n - 1), 0
    while i > 0 and (x - i) < lim:
        i -= 1
        if prof[i] < ink_thr:
            white = 0
        else:
            white += 1
            if white >= gap:
                return min(x, i + white)
    return max(0, i)


def _expand_right(prof: list[int], x: int, ink_thr: int = 250,
                  gap_frac: float = 0.008, lim_frac: float = 0.18) -> int:
    n = len(prof)
    if n <= 0:
        return x
    gap = max(5, int(gap_frac * n)); lim = max(16, int(lim_frac * n))
    i, white = max(0, min(x, n - 1)), 0
    while i < n - 1 and (i - x) < lim:
        i += 1
        if prof[i] < ink_thr:
            white = 0
        else:
            white += 1
            if white >= gap:
                return max(x, i - white + 1)
    return min(n, i + 1)


def _snap_bbox(shot: Path, bbox: list[float], touch: tuple[bool, bool, bool, bool]) -> list[float]:
    """只把「被裁到的边」吸附到真正的墨迹边界（先横向、再纵向），别的边信模型给的框。"""
    W, H = _img_size(shot)
    if W <= 0 or H <= 0:
        return bbox
    x0 = max(0, min(W - 2, int(bbox[0] * W))); x1 = max(x0 + 2, min(W, int(bbox[2] * W)))
    y0 = max(0, min(H - 2, int(bbox[1] * H))); y1 = max(y0 + 2, min(H, int(bbox[3] * H)))
    if touch[0] or touch[1]:
        col = _gray_profile(shot, f"{W}x{max(1, y1 - y0)}+0+{y0}", horizontal=True)
        if len(col) >= W:
            if touch[0]:
                x0 = _expand_left(col, x0)
            if touch[1]:
                x1 = _expand_right(col, x1)
    if touch[2] or touch[3]:
        row = _gray_profile(shot, f"{max(1, x1 - x0)}x{H}+{x0}+0", horizontal=False)
        if len(row) >= H:
            if touch[2]:
                y0 = _expand_left(row, y0, gap_frac=0.004, lim_frac=0.10)
            if touch[3]:
                y1 = _expand_right(row, y1, gap_frac=0.004, lim_frac=0.10)
    return [x0 / W, y0 / H, x1 / W, y1 / H]


def _crop_bbox(shot: Path, bbox, dst: Path, pad_x: float, pad_y: float) -> tuple[bool, str]:
    """按归一化 bbox 裁一张图；返回 (裁出来了, 几何串)。"""
    W, H = _img_size(shot)
    if W <= 0 or H <= 0:
        return False, ""
    x0, x1 = sorted((max(0.0, min(1.0, bbox[0])), max(0.0, min(1.0, bbox[2]))))
    y0, y1 = sorted((max(0.0, min(1.0, bbox[1])), max(0.0, min(1.0, bbox[3]))))
    X0 = max(0, int((x0 - pad_x) * W)); Y0 = max(0, int((y0 - pad_y) * H))
    X1 = min(W, int((x1 + pad_x) * W)); Y1 = min(H, int((y1 + pad_y) * H))
    w, h = X1 - X0, Y1 - Y0
    if w < 80 or h < 60:
        return False, ""
    geo = f"{w}x{h}+{X0}+{Y0}"
    subprocess.run(["convert", str(shot), "-crop", geo, "+repage", str(dst)],
                   capture_output=True, timeout=180)
    if not dst.exists() or dst.stat().st_size < 2000 or _is_blank_image(dst):
        dst.unlink(missing_ok=True)
        return False, geo
    return True, geo


def _vision_bbox(messages: list, model: str, max_tokens: int = 1500) -> dict:
    try:
        got = vision_json(messages, model, max_tokens)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _render_page(pdf: Path, page: int, tmp: Path) -> Path | None:
    subprocess.run(["pdftoppm", "-r", str(200), "-f", str(page), "-l", str(page),
                    "-png", str(pdf), str(tmp / f"p{page}")],
                   capture_output=True, timeout=300)
    cands = sorted(tmp.glob(f"p{page}*.png"))
    return cands[0] if cands else None


def _ask_bbox(shot: Path, page: int, no: int, sub: int) -> list[float] | None:
    b64 = base64.b64encode(shot.read_bytes()).decode()
    got = _vision_bbox([
        {"role": "system", "content": "你是试卷图形定位助手，只输出 JSON。"},
        {"role": "user", "content": [
            {"type": "text", "text": FIG_PROMPT.format(page=page, no=no, sub=sub)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ], VISION_MODEL)
    if not got.get("found"):
        return None
    try:
        bbox = [float(v) for v in got["bbox"]]
    except Exception:
        return None
    return bbox if len(bbox) == 4 else None


def crop_answer_figure(pdf: Path, page: int, no: int, sub: int, dst: Path,
                       review: list[str], label: str) -> bool:
    """把教师版 PDF 的某一页渲染出来，让多模态模型给 bbox，再裁成答案图。

    - 先在算出来的那一页找；找不到就顺延试相邻页（答案图跨页/答案块起页错位很常见）；
    - 裁之前把 bbox 吸附到真正的墨迹边界（防切掉方框或半行字）；
    - 还贴边就带上裁图让模型重算一次。
    """
    tmp = Path(tempfile.mkdtemp(prefix="biocrop_"))
    try:
        bbox, shot, used_page = None, None, page
        for cand in [page, page + 1, page - 1]:
            if cand < 1:
                continue
            shot = _render_page(pdf, cand, tmp)
            if shot is None:
                continue
            bbox = _ask_bbox(shot, cand, no, sub)
            if bbox:
                used_page = cand
                break
        if bbox is None or shot is None:
            review.append(f"PDF 截图失败：{label} 第（{sub}）小问在 PDF 第 {page}"
                          f"（及相邻页）没定位到答案图，保留原文字答案")
            return False

        dst.parent.mkdir(parents=True, exist_ok=True)
        ok, geo = _crop_bbox(shot, bbox, dst, 0.004, 0.004)
        if not ok:
            review.append(f"PDF 截图失败：{label} 第（{sub}）小问裁出来的图是空的（bbox {bbox}），"
                          f"保留原文字答案")
            dst.unlink(missing_ok=True)
            return False
        touch = _touches_edges(dst)
        if any(touch):                             # 被裁到了 → 吸附到真正的墨迹边界重裁
            snapped = _snap_bbox(shot, bbox, touch)
            ok3, geo3 = _crop_bbox(shot, snapped, dst, 0.004, 0.004)
            if ok3:
                geo, bbox = geo3, snapped
                touch = _touches_edges(dst)
        if all(touch):                             # 四边还都贴着 → 带上裁图让模型重算一次
            b64 = base64.b64encode(shot.read_bytes()).decode()
            b64c = base64.b64encode(dst.read_bytes()).decode()
            got2 = _vision_bbox([
                {"role": "system", "content": "你是试卷图形定位助手，只输出 JSON。"},
                {"role": "user", "content": [
                    {"type": "text", "text": REFINE_PROMPT.format(page=used_page, no=no, sub=sub)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64c}"}},
                ]},
            ], VISION_MODEL)
            try:
                bbox2 = [float(v) for v in got2["bbox"]]
                assert len(bbox2) == 4 and got2.get("found", True)
                snapped2 = _snap_bbox(shot, bbox2, _touches_edges(dst))
                ok2, geo2 = _crop_bbox(shot, snapped2, dst, 0.004, 0.004)
                if ok2:
                    geo, bbox = geo2, snapped2
            except Exception:
                pass
            if any(_touches_edges(dst)):
                review.append(f"PDF 截图：{label} 第（{sub}）小问的图仍贴边（可能切到内容），"
                              f"需人工看一眼 {dst.name}（bbox {[round(x, 3) for x in bbox]}）")
        w, h = _img_size(dst)
        review.append(f"PDF 截图：{label} 第（{sub}）小问 → PDF 第{used_page}页 {w}×{h}px（{geo}）"
                      f" → {dst.name}")
        return True
    except Exception as e:
        review.append(f"PDF 截图失败：{label} 第（{sub}）小问异常（{str(e)[:80]}），保留原文字答案")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sub_of_segment(content: str, pos: int) -> int | None:
    """答题空所在的「（N）」小问序号。"""
    head = content[:pos]
    marks = list(re.finditer(r"[（(]\s*(\d{1,2})\s*[）)]", head))
    if not marks:
        return None
    return int(marks[-1].group(1))


def _segment_around(content: str, pos: int) -> str:
    """取答题空所在小问的文字（上一个小问编号 → 下一个小问编号之间）。"""
    head = content[:pos]
    marks = [m for m in re.finditer(r"[（(]\s*(\d{1,2})\s*[）)]", head)]
    start = marks[-1].start() if marks else max(0, pos - 200)
    nxt = re.search(r"[（(]\s*\d{1,2}\s*[）)]", content[pos:])
    stop = pos + nxt.start() if nxt else len(content)
    return content[start:stop]


def fill_draw_answer_images(content: str, answers: list[str], md: Path, type_dir: str,
                            title: str, review: list[str], label: str, no: int,
                            budget: list[int], allow_ai: bool = True) -> list[str]:
    """画图/补图题：答案不该是文字描述，改为 PDF 截取的答案图。返回（可能更新过的）answers。"""
    if budget[0] <= 0:
        return answers
    pdf = pdf_of(md, type_dir, title)
    pages = _answer_pages_text(pdf) if pdf else []
    tokens = list(PH.finditer(content))
    out = list(answers)
    done_sub: set[int] = set()                    # 同一小问只截一张图
    for i, m in enumerate(tokens):
        if i >= len(out) or budget[0] <= 0:
            break
        ans = out[i]
        if IMG_REF.search(ans or "") or EXAM_REF.search(ans or ""):
            continue
        seg = _segment_around(content, m.start())
        if not DRAW_ASK.search(seg):
            continue
        if ans and PH_ANSWER.match(ans):        # 「见下图」这类占位答案 = 还没有真答案
            ans = ""
        # 只给「文字描述型/占位型/空」的画图空截图；短答案（如 BD）保留文字
        if ans and is_short_answer(ans):
            continue
        sub = _sub_of_segment(content, m.start())
        if sub is None or sub in done_sub:
            continue
        if not pages:
            review.append(f"PDF 截图失败：{label} 第（{sub}）小问需要答案图，但找不到教师版 PDF，"
                          f"保留原文字答案")
            continue
        page = _answer_page_of(pages, no, sub)
        if page is None:
            review.append(f"PDF 截图失败：{label} 第（{sub}）小问在参考答案区没定位到页码，"
                          f"保留原文字答案")
            continue
        fname = f"ans-{no}.png" if len(tokens) == 1 else f"ans-{no}-{sub}.png"
        dst = OUTPUT_DOCX_TO_MD / type_dir / SUBJECT / title / "images" / fname
        if not dst.exists():                       # 已截过的直接复用（保证 --no-ai 复跑逐字节一致）
            if not allow_ai:
                review.append(f"PDF 截图失败：{label} 第（{sub}）小问需要答案图，"
                              f"--no-ai 模式不调模型未重试，保留原文字答案")
                continue
            if not crop_answer_figure(pdf, page, no, sub, dst, review, label):
                continue
        else:
            review.append(f"PDF 截图：{label} 第（{sub}）小问复用已截好的 {fname}")
        ref = f"/exam-images/{type_dir}/{SUBJECT}/{title}/images/{fname}"
        if ans:
            review.append(f"{label} 第{i + 1}空：画图/补图题，答案改用 PDF 截图（原文字答案：{ans[:60]}）")
        out[i] = f"![]({ref})"
        done_sub.add(sub)
        budget[0] -= 1
    return out



def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{dst}"

    review: list[str] = []
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    if not ans:
        raise RuntimeError("没找到「参考答案」分界")
    ensure_web_images(md, review)

    def rewrite(t: str) -> str:
        return rewrite_images(t, md.parent, title, type_dir, review, SUBJECT)

    # ---- 一、选择题 ----
    h = HDR_ESSAY.search(body)                     # 有「第二部分 / 二、非选择题（解答题）」标题就按它切
    smarks = score_marks(body)                     # 没标题时兜底：大题才写「（N分）」
    cut = first_big_mark(body)
    if h:
        cut = h.start()
    elif len(smarks) >= 3 and smarks[0].start() < cut:
        cut = smarks[0].start()
    choice_body, essay_body = body[:cut], body[cut:]
    # 卷面声明的题数只从「选择题标题区」里找（题号行之前），别把大题标题的「共3小题」当成选择题数
    _first_q = QMARK.search(choice_body)
    head_area = choice_body[:_first_q.start()] if _first_q else choice_body[:300]
    declared = declared_count(head_area)
    cache_dir = OUT["root"] / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    table_opts: list[int] = []
    ch_notes: list[str] = []
    choice_qs = parse_choice_questions(choice_body, table_opt=table_opts, choice_notes=ch_notes)
    review.extend(ch_notes)
    for no_ in table_opts:
        review.append(f"第{no_}题（选择题）：选项原卷就是一张表（首列 A–D），表格已完整留在题干，"
                      f"options 只给字母 A–D")
    # 理综口径（北京理综里的生物部分）：选择题 5 道、每题 6 分，非选择题共 50 分，生物部分 80 分
    zonghe = bool(declared and declared <= 6) or (
        bool(choice_qs) and all((q.get("_qscore") or 0) >= 5 for q in choice_qs))
    q_unit = CHOICE_SCORE
    if zonghe:
        sc = {(q.get("_qscore") or 0) for q in choice_qs if (q.get("_qscore") or 0) >= 5}
        q_unit = sc.pop() if len(sc) == 1 else ZONGHE_CHOICE_SCORE
        review.append(f"按理综口径解析：选择题 {len(choice_qs)} 道 × {q_unit} 分，"
                      f"非选择题合计 {ZONGHE_ESSAY_TOTAL} 分，生物部分满分 "
                      f"{q_unit * len(choice_qs) + ZONGHE_ESSAY_TOTAL} 分")

    # docx 把某道选择题转丢了（卷面声明 N 道、实际切到 M 道）→ 从教师版 PDF 转写补齐
    if declared and len(choice_qs) < declared:
        pdf = pdf_of(md, type_dir, title)
        have = {q.get("_printed") for q in choice_qs}
        missing = [n for n in range(1, declared + 1) if n not in have]
        ask = sorted(set(missing) | {n - 1 for n in missing if (n - 1) in have})
        pcache = cache_dir / f"{title}-pdfchoice.json"
        rec: dict[int, dict] = {}
        if pcache.exists() and (not args.ai or not args.force):
            raw = json.loads(pcache.read_text(encoding="utf-8"))
            rec = {int(k): v for k, v in raw.items()}
            if rec:
                review.append(f"{title}：复用已从 PDF 转写好的题目 "
                              f"{'、'.join(f'第{n}题' for n in sorted(rec))}")
        elif args.ai and pdf is not None and ask:
            rec = transcribe_choices_from_pdf(pdf, ask, review, title)
            if rec:
                pcache.write_text(json.dumps({str(k): v for k, v in rec.items()},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
        if rec:
            for n, r in rec.items():
                hit = next((q for q in choice_qs if q.get("_printed") == n), None)
                if hit:
                    hit["content"], hit["options"] = r["content"], r["options"]
                else:
                    choice_qs.append({"id": n, "questionType": "single", "content": r["content"],
                                      "options": r["options"], "answer": "", "analysis": "",
                                      "score": q_unit, "_printed": n, "_qscore": q_unit})
            choice_qs.sort(key=lambda q: q.get("_printed") or q["id"])
            for i, q in enumerate(choice_qs, 1):
                q["id"] = i
            if len(choice_qs) == declared:
                review.append(f"选择题已从教师版 PDF 补齐到卷面声明的 {declared} 道")
        elif not args.ai:
            review.append(f"选择题只有 {len(choice_qs)} 道（卷面声明 {declared} 道）："
                          f"--no-ai 模式不调模型补题，需 --force 重跑")
        else:
            review.append(f"选择题只有 {len(choice_qs)} 道（卷面声明 {declared} 道），"
                          f"PDF 补题没成功，需人工核对")
    if declared and len(choice_qs) != declared:
        review.append(f"选择题切到 {len(choice_qs)} 道（卷面声明 {declared} 道）")
    elif not declared and not zonghe and len(choice_qs) != N_CHOICE:
        review.append(f"选择题切到 {len(choice_qs)} 道（卷面一般 {N_CHOICE} 道）")

    blocks = explained_answer_blocks(ans, max_no=max(12, declared or 0))
    # 参考答案是「解析版」（每题带「故选：X」）时，直接按题号逐题取答案，最不容易串题
    if all(n in blocks and blocks[n][0] for n in range(1, len(choice_qs) + 1)):
        choice_ans = {n: blocks[n][0] for n in range(1, len(choice_qs) + 1)}
        review.append("选择题答案取自参考答案的逐题解析（「故选：X」），逐题对齐")
    elif len(blocks) >= len(choice_qs) and sum(1 for n in blocks if blocks[n][0]) >= len(choice_qs):
        choice_ans = {q["id"]: blocks[q["id"]][0] for q in choice_qs
                      if q["id"] in blocks and blocks[q["id"]][0]}
        review.append("选择题答案取自参考答案的逐题解析（按题号对齐）")
    else:
        choice_ans = parse_choice_answers(ans)
    # 答案表条数 ≠ 题干块数（Word 题号重复）→ 让模型逐题推导，再按**内容**对齐，
    # 绝不用位置顺序硬套（重复题号会让后面的答案整体错位）。
    if choice_ans and len(choice_ans) != len(choice_qs):
        d_cache = cache_dir / f"{title}-choice.json"
        derived: dict[int, tuple[str, str]] = {}
        if d_cache.exists() and (not args.ai or not args.force):
            raw = json.loads(d_cache.read_text(encoding="utf-8"))
            derived = {int(k): (v[0], v[1]) for k, v in raw.items()}
        elif args.ai:
            derived = ai_solve_choice(choice_qs)
            if derived:
                d_cache.write_text(json.dumps({str(k): list(v) for k, v in derived.items()},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
        if derived:
            choice_ans = align_choice_answers(choice_qs, choice_ans, derived, review)
        else:
            review.append(f"选择题 {len(choice_qs)} 道、答案表 {len(choice_ans)} 个，需要对答案做内容对齐，"
                          f"但 --no-ai 模式（或模型无输出）拿不到推导，临时按位置对齐，需人工核对")
    for q in choice_qs:
        q["answer"] = choice_ans.get(q["id"], "")
        q["score"] = q_unit
        if not q["answer"]:
            review.append(f"第{q['id']}题（选择题）缺答案")
        if len(q["options"]) != 4:
            review.append(f"第{q['id']}题（选择题）选项不足 4 个（{len(q['options'])}）")
        q["content"] = rewrite(normalize_md_tables(reflow_cjk(latexify(q["content"]))))
        for o in q["options"]:
            o["label"] = rewrite(normalize_md_tables(reflow_cjk(latexify(o["label"]))))
    for q in choice_qs:                       # 内部字段不出口
        q.pop("_printed", None)
        q.pop("_qscore", None)
    choice_section = {
        "type": "选择题", "questionType": "biology-choice", "title": "一、选择题",
        "category": "选择", "score": q_unit * len(choice_qs),
        "questions": choice_qs,
    }

    # ---- 二、非选择题（一题一个 section） ----
    if zonghe:                                     # 理综：非选择题题号是 6/7/8（或 29–31），不分大小题号段
        essays = rebalance_leads(split_marks(essay_body, 1, 99))
        ans_blocks = {e["no"]: {"no": e["no"], "score": None,
                                "text": blocks.get(e["no"], (None, ""))[1],
                                "text_full": blocks.get(e["no"], (None, ""))[1]}
                      for e in essays}
        for e in essays:
            if not ans_blocks[e["no"]]["text_full"]:
                review.append(f"第{e['no']}题：参考答案的解析里没切到这道题")
    else:
        ans_blocks = {a["no"]: a for a in split_marks(ans, N_CHOICE + 1, 99)}
        ans_order = sorted(ans_blocks)
        essays = rebalance_leads(split_marks(essay_body, N_CHOICE + 1, 99))
        alt = split_marks_by_score(essay_body, ans_order)
        if alt and len(alt) == len(ans_order) and [e["no"] for e in essays] != ans_order:
            review.append(f"题干里的大题编号不规整（Word 自动编号 → {[e['no'] for e in essays]}），"
                          f"已按「（N分）」标记切分、并对照参考答案区题号重编为 {ans_order}")
            essays = rebalance_leads(alt)
    cat_cache = cache_dir / f"{title}-cats.json"
    if cat_cache.exists() and (not args.ai or not args.force):
        cat_map = {int(k): v for k, v in json.loads(cat_cache.read_text(encoding="utf-8")).items()}
    elif args.ai:
        cat_map = classify(essay_body, essays, ans_blocks)
        cat_cache.write_text(json.dumps(cat_map, ensure_ascii=False), encoding="utf-8")
    else:
        cat_map = {}

    # 大题满分：题干/参考答案都没写分值时倒推
    #   理综 → 非选择题合计 50 分；普通北京卷 → 全卷 100 分（16 题 10 分、其余 12 分）
    essay_totals: dict[int, int] = {}
    missing: list[int] = []
    for e in essays:
        ab0 = ans_blocks.get(e["no"])
        t = e["score"] or (ab0["score"] if ab0 else None)
        essay_totals[e["no"]] = t or 0
        if not t:
            missing.append(e["no"])
    if missing:
        if zonghe:
            remaining = ZONGHE_ESSAY_TOTAL - sum(t for no_, t in essay_totals.items() if t)
            why_total = f"理综非选择题共 {ZONGHE_ESSAY_TOTAL} 分"
        else:
            remaining = 100 - q_unit * len(choice_qs) - sum(
                t for no_, t in essay_totals.items() if t)
            why_total = "全卷 100 分"
        guess = {}
        if remaining >= len(missing):
            if 16 in missing and remaining == 12 * len(missing) - 2:
                guess = {no_: (10 if no_ == 16 else 12) for no_ in missing}
                why = "按北京卷常规结构（第16题 10 分、其余 12 分）"
            else:
                q, r = divmod(remaining, len(missing))
                guess = {no_: q + (1 if i < r else 0) for i, no_ in enumerate(missing)}
                why = "按剩余分均摊"
            for no_, t in guess.items():
                essay_totals[no_] = t
            review.append(f"第{'、'.join(str(x) for x in missing)}题：题干和参考答案都没写分值，"
                          f"按 {why_total} 倒推 {why} → "
                          + "、".join(f"第{k}题 {v} 分" for k, v in guess.items()))
        else:
            review.append(f"第{'、'.join(str(x) for x in missing)}题：题干和参考答案都没写分值，"
                          f"倒推不成立（剩余 {remaining} 分），这几题分值记 0，需人工补")

    sections = [choice_section]
    crop_budget = [MAX_PDF_CROPS]                # 每卷最多从 PDF 截几张答案图
    for e in essays:
        no, total = e["no"], essay_totals.get(e["no"], 0)
        label = f"第{no}题"
        seg = cut_tail(e["text"]).strip()
        if e["lead"]:
            seg = e["lead"] + "\n" + seg
        ab = ans_blocks.get(no)
        atext = cut_tail(ab["text_full"]).strip() if ab else ""
        if not atext:
            review.append(f"{label}：参考答案段没切到")

        cache = cache_dir / f"{title}-{no}.json"
        data = {}
        # --no-ai 时永远吃缓存（复跑要逐字节一致）；--force 只在允许调模型时忽略缓存
        if cache.exists() and (not args.ai or not args.force):
            data = json.loads(cache.read_text(encoding="utf-8"))
        elif args.ai:
            data = ai_json(ESSAY_PROMPT.replace("{BODY}", seg).replace("{ANS}", atext))
        # 空数 != 答案数、或题干漏图 → 让模型对着参考答案返修（最多两轮）
        src_imgs = IMG_REF.findall(seg)
        for _ in range(2 if args.ai else 0):
            content0 = str(data.get("content") or "")
            ans0 = [ (b.get("answer") if isinstance(b, dict) else str(b)) for b in (data.get("blanks") or []) ]
            n_tok, n_ans = len(PH.findall(content0)), len(ans0)
            if not content0:
                break
            problems = []
            if n_tok != n_ans:
                problems.append(f"content 里只有 {n_tok} 个空，但参考答案对应 {n_ans} 个答案"
                                if n_tok < n_ans else
                                f"content 里有 {n_tok} 个空，但只给了 {n_ans} 个答案")
            empty_n = sum(1 for a in ans0 if not str(a).strip())
            if empty_n:
                problems.append(f"有 {empty_n} 个空没有答案（空字符串）；请对照【参考答案】"
                                f"把这几个空的答案逐字补上，一个都不许空")
            n_mark = len(re.findall(r"[①②③④⑤⑥⑦⑧]", atext or ""))
            if n_mark >= 2 and n_ans < n_mark:
                problems.append(f"参考答案里有 {n_mark} 个 ①②③ 标记（说明有 {n_mark} 个空），"
                                f"但只给了 {n_ans} 个答案；请把这些子空分别留成独立的空并逐个摘抄答案")
            lack = [m for m in src_imgs if m not in content0]
            if lack:
                problems.append(f"content 漏掉了原题里的图片 {['![](' + m + ')' for m in lack]}，"
                                f"必须原样补回到它在题干中的位置")
            if not problems:
                break
            problem = "；".join(problems)
            log(f"    ↻ {label} 返修：{problem}")
            fixed = ai_json(REPAIR_PROMPT.replace("{BODY}", seg).replace("{ANS}", atext)
                            .replace("{PROBLEM}", problem + "。")
                            .replace("{CONTENT}", content0)
                            .replace("{BLANKS}", json.dumps(ans0, ensure_ascii=False)))
            if not fixed.get("content"):
                break
            data = fixed
        if data and args.ai:
            cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        content = str(data.get("content") or "").strip()
        raw_blanks = data.get("blanks") or []
        answers = [clean_answer(b.get("answer") if isinstance(b, dict) else str(b)) for b in raw_blanks]
        if not content:
            review.append(f"{label}：AI 没返回题干，本次跳过该题")
            continue

        verify_answers(answers, atext, review, label)
        content = reflow_cjk(strip_stem_prefix(latexify(content), no))
        answers = attach_answer_images(answers, atext, review, label)
        content = fix_placeholders(content, answers, review, label)
        content = strip_ph_spaces(content)
        n_blank = len(PH.findall(content))
        if len(answers) > n_blank:
            review.append(f"{label}：答案条数 {len(answers)} 多于题干空位 {n_blank}，多余答案被丢弃："
                          f"{answers[n_blank:]}")
            answers = answers[:n_blank]
        if any(not (a or "").strip() for a in answers) or len(answers) < n_blank:
            # 兜底：没答案的空去掉占位（保证「每空都有答案」）
            content, answers = drop_empty_blanks(content, answers, review, label)
            n_blank = len(PH.findall(content))
        answers += [""] * (n_blank - len(answers))
        for i, a in enumerate(answers, 1):
            if not a:
                review.append(f"{label} 第{i}空：答案为空（需人工补）")

        # ---- 题干里的数据表：AI 拍平了就按原文补回，再把表格规范成 GFM ----
        n_raw_tbl = count_md_tables(seg)
        n_ai_tbl = count_md_tables(content)
        if n_raw_tbl and not n_ai_tbl:
            content = restore_raw_tables(content, seg, review, label)
        elif n_raw_tbl > n_ai_tbl:
            review.append(f"{label}：原题有 {n_raw_tbl} 张表，AI 只保留了 {n_ai_tbl} 张，需人工核对")
        content = normalize_md_tables(content)
        note_missing_figures(content, review, label)

        # ---- 画图/补图题：docx 里没有的答案图，从教师版 PDF 截取 ----
        before = list(answers)
        answers = fill_draw_answer_images(content, answers, md, type_dir, title,
                                          review, label, no, crop_budget, allow_ai=args.ai)
        if answers != before:                    # 答案换成图片 → 该空按长句空重新定档
            content = fix_placeholders(content, answers, review, label)
            content = normalize_md_tables(strip_ph_spaces(content))
        tags = blank_tags(content)
        scores = split_scores(total, tags, atext, review, label)
        qs = []
        for i, a in enumerate(answers, 1):
            ans_text = drop_missing_answer_images(rewrite(latexify(a)), review, label, i)
            qs.append({"id": no * 100 + i, "questionType": "input", "content": "",
                       "answer": ans_text, "analysis": "",
                       "score": scores[i - 1] if i - 1 < len(scores) else 0})
        sections.append({
            "type": "非选择题", "questionType": "biology-fill",
            "title": f"二、非选择题（第{no}题）",
            "category": cat_map.get(no) or "实验探究",
            "score": sum(q["score"] for q in qs),
            "content": rewrite(content),
            "questions": qs,
        })

    year = re.search(r"(20\d{2})", title)
    total_score = sum(s["score"] for s in sections)
    paper_total = 80 if zonghe else 100
    if total_score != paper_total:
        if not zonghe and total_score > 100 and len(choice_qs) > N_CHOICE:
            paper_total = total_score
            review.append(f"原卷选择题题号重复，选择题实为 {len(choice_qs)} 道"
                          f"（{CHOICE_SCORE * len(choice_qs)} 分）+ 非选择题 "
                          f"{total_score - CHOICE_SCORE * len(choice_qs)} 分 = {total_score} 分，"
                          f"paper.totalScore 按 {paper_total} 写")
        else:
            review.append(f"全卷分值合计 {total_score} 分（应为 {paper_total} 分），请核对")
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": paper_total, "duration": 90,
        "tags": ([SUBJECT, args.grade, type_dir] + (["理综"] if zonghe else [])
                 + ([year.group(1)] if year else [])),
    }
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)   # 出口约定：选择题答案与选项 id 一律大写
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text("\n".join(review), encoding="utf-8")
    n_essay = len(sections) - 1
    n_blank = sum(len(s["questions"]) for s in sections[1:])
    return (f"✓ {title}：选择 {len(choice_qs)} 题 / 大题 {n_essay} 道 / 空 {n_blank} 个"
            f" / 卷面 {total_score} 分 → {dst.name}")


# ──────────────────────────────────────────────────────────────────
# 自查
# ──────────────────────────────────────────────────────────────────

def _source_md_of(jf: Path) -> Path | None:
    """由 JSON 路径反查原始 MD（output_docx_to_md/<类型>/生物/<卷>（教师版）/….md）。"""
    d = OUTPUT_DOCX_TO_MD / jf.parent.name / SUBJECT / f"{jf.stem}（教师版）"
    md = d / f"{jf.stem}（教师版）.md"
    return md if md.exists() else None


ANS_BLOCK = re.compile(r"(?m)^\s*\**\s*(\d{1,2})\s*\\?\s*[.．、]\s*【答案】\s*\**\s*([A-Da-d])\b")


def choice_answer_blocks(ans_text: str) -> dict[int, tuple[str, str]]:
    """参考答案区里「1．【答案】C …」这种逐题答案块（有解析的卷子才有）。"""
    out: dict[int, tuple[str, str]] = {}
    marks = list(ANS_BLOCK.finditer(ans_text or ""))
    for i, m in enumerate(marks):
        no = int(m.group(1))
        if no > N_CHOICE:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(ans_text)
        out[no] = (m.group(2).upper(), ans_text[m.end():end].strip())
    return out


def _exam_images_root() -> Path:
    """nginx: /exam-images/<类型>/<学科>/<卷>/images/x → output_docx_to_md/<类型>/<学科>/<卷>/images/x"""
    return OUTPUT_DOCX_TO_MD


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    """返回 (统计, 错误, 提示)。三类新增断言：分值长句优先 / 表格可解析 / 答案图存在。"""
    errs, notes = [], []
    raw = jf.read_text(encoding="utf-8")
    data = json.loads(raw)                       # 1) 能 json.load
    paper, sections = data["paper"], data["sections"]
    stat = {"title": paper.get("title"), "choice": 0, "essay": 0, "blanks": 0,
            "imgs": set(), "ans_imgs": set(), "tables": 0, "seqs": [], "sections": [],
            "answer_src": "-", "unit": CHOICE_SCORE}

    # 口径判定（理综：选择题 5 道 × 6 分、非选择题 50、生物部分 80；普通北京卷：15 × 2 + 70）
    zonghe = "理综" in (paper.get("tags") or [])
    exp_n = 5 if zonghe else N_CHOICE
    exp_unit = ZONGHE_CHOICE_SCORE if zonghe else CHOICE_SCORE
    exp_total = (exp_unit * exp_n + ZONGHE_ESSAY_TOTAL) if zonghe else 100
    stat["unit"] = exp_unit

    if paper.get("subject") != SUBJECT:
        errs.append(f"paper.subject 不是生物：{paper.get('subject')}")
    for k in ("title", "source", "grade", "year", "totalScore", "duration", "tags"):
        if k not in paper:
            errs.append(f"paper 缺字段 {k}")
    sec_sum = sum(s_.get("score") or 0 for s_ in sections)
    if paper.get("totalScore") != sec_sum:
        errs.append(f"paper.totalScore={paper.get('totalScore')} ≠ 各 section 分值合计 {sec_sum}")
    elif paper.get("totalScore") != exp_total:
        notes.append(f"paper.totalScore={paper.get('totalScore')}（该口径应为 {exp_total}）")

    choice_secs = [s for s in sections if s.get("type") == "选择题"]
    essay_secs = [s for s in sections if s.get("type") != "选择题"]
    if len(choice_secs) != 1:
        errs.append(f"选择题 section 应恰好 1 个，实际 {len(choice_secs)}")
    for s in choice_secs:
        qs = s.get("questions") or []
        stat["choice"] = len(qs)
        if s.get("questionType") != "biology-choice":
            errs.append(f"选择题 questionType = {s.get('questionType')}（应为 biology-choice）")
        if s.get("category") != "选择":
            errs.append("选择题 category 应为 选择")
        if len(qs) < exp_n:
            errs.append(f"选择题 {len(qs)} 道（少于 {exp_n} 道）")
        elif len(qs) > exp_n:
            notes.append(f"选择题 {len(qs)} 道（卷面常规 {exp_n} 道）：原卷题号重复，"
                         f"多出来的块已按内容对齐并补答案，详见 .review.txt")
        if s.get("score") != exp_unit * len(qs):
            errs.append(f"选择题 score={s.get('score')}，与 {exp_unit}×{len(qs)} 不符")
        for q in qs:
            if q.get("score") != exp_unit:
                errs.append(f"选择题第{q.get('id')}题 score={q.get('score')}（应为 {exp_unit}）")
            if q.get("questionType") != "single":
                errs.append(f"选择题第{q.get('id')}题 questionType={q.get('questionType')}")
            opts = q.get("options") or []
            if [o.get("id") for o in opts] != ["A", "B", "C", "D"]:
                errs.append(f"选择题第{q.get('id')}题选项 id 不是 A–D 四项：{[o.get('id') for o in opts]}")
            if not re.fullmatch(r"[A-D]", str(q.get("answer") or "")):
                errs.append(f"选择题第{q.get('id')}题 answer={q.get('answer')!r}（应为单个大写字母）")
            if "analysis" not in q:
                errs.append(f"选择题第{q.get('id')}题缺 analysis")
            if not (q.get("content") or "").strip():
                errs.append(f"选择题第{q.get('id')}题 content 为空")
            # ★ 断言二（选择题题干里的表格也要能解析）
            stat["tables"] += count_md_tables(q.get("content") or "")
            for e in table_shape_ok(q.get("content") or ""):
                errs.append(f"选择题第{q.get('id')}题表格：{e}")
        stat["sections"].append(("选择题", None, len(qs), s.get("score"),
                                 sum(q.get("score") or 0 for q in qs)))

    # ★ 断言四：选择题答案要能在「该题的参考答案/解析原文」里找到依据（兜答案错位）
    src = _source_md_of(jf)
    if src is None:
        notes.append("找不到原始 MD，选择题答案依据校验跳过")
    else:
        try:
            _body, atext = split_paper(src.read_text(encoding="utf-8"))
        except Exception:
            atext = ""
        per_q = choice_answer_blocks(atext)
        key = parse_choice_answers(atext) if atext else {}
        ch_qs = (choice_secs[0].get("questions") or []) if choice_secs else []
        if per_q and len(per_q) == len(ch_qs):
            stat["answer_src"] = "逐题答案块"
            for q in ch_qs:
                want, detail = per_q.get(q.get("id"), ("", ""))
                got = str(q.get("answer") or "")
                if not want:
                    errs.append(f"选择题第{q.get('id')}题：参考答案里没有该题的逐题答案块")
                elif got != want:
                    errs.append(f"选择题第{q.get('id')}题：answer={got!r} 与参考答案原文 {want!r} 不一致"
                                f"（答案错位）")
                elif want not in detail and detail:
                    notes.append(f"选择题第{q.get('id')}题：答案 {want} 在该题解析里没出现（解析较短，可能正常）")
        elif key and len(key) == len(ch_qs):
            stat["answer_src"] = "答案表"
            for q in ch_qs:
                want = key.get(q.get("id"), "")
                got = str(q.get("answer") or "")
                if not want:
                    errs.append(f"选择题第{q.get('id')}题：答案表里没有第 {q.get('id')} 题")
                elif got != want:
                    errs.append(f"选择题第{q.get('id')}题：answer={got!r} 与答案表 {want!r} 不一致（答案错位）")
            notes.append("该卷参考答案只有答案表（无逐题解析），字母依据 = 答案表对应格")
        elif key and len(key) != len(ch_qs):
            stat["answer_src"] = "答案表（条数不符，按内容对齐）"
            notes.append(f"选择题 {len(ch_qs)} 道 ≠ 答案表 {len(key)} 个：已按内容对齐（详见 .review.txt），"
                         f"逐题字母比对跳过")
        else:
            stat["answer_src"] = "无"
            notes.append("该卷参考答案里既没有答案表也没有逐题答案块，选择题答案依据无法校验")

    if not essay_secs:
        errs.append("没有非选择题 section")
    ids = set()
    for s in essay_secs:
        qs = s.get("questions") or []
        stat["essay"] += 1
        stat["blanks"] += len(qs)
        content = s.get("content") or ""
        n_in = content.count("<Input")                   # <Input 与 <InputLong 都算
        n_close = content.count("</Input>") + content.count("</InputLong>")
        n_short = content.count("<Input></Input>")
        n_long = content.count("<InputLong></InputLong>")
        if s.get("questionType") != "biology-fill":
            errs.append(f"{s.get('title')}：questionType={s.get('questionType')}")
        if s.get("category") not in CATS:
            errs.append(f"{s.get('title')}：category={s.get('category')!r} 不在 {CATS}")
        if n_in != len(qs):
            errs.append(f"{s.get('title')}：<Input 出现 {n_in} 次，questions {len(qs)} 个")
        if n_close != n_in:
            errs.append(f"{s.get('title')}：占位符不配对（开 {n_in} / 闭 {n_close}）")
        if n_short + n_long != n_in:
            errs.append(f"{s.get('title')}：存在非标准占位符写法")
        ssum = sum(q.get("score") or 0 for q in qs)
        if ssum != s.get("score"):
            errs.append(f"{s.get('title')}：Σquestions.score={ssum} ≠ section.score={s.get('score')}")
        if s.get("score") not in (10, 12):
            notes.append(f"{s.get('title')}：section.score={s.get('score')}（常见为 10/12）")

        # ★ 断言一：每空分值符合「长句空优先吃高分」（长句权重 2 / 短空权重 1）
        tags = blank_tags(content)
        got = [q.get("score") or 0 for q in qs]
        total = s.get("score") or 0
        if len(tags) == len(got):
            exp = split_scores(total, tags, "", [], "")
            if got != exp:
                errs.append(f"{s.get('title')}：每空分值 {got} 不符合长句优先规则（应为 {exp}）")
            if total >= len(got) and any(x < 1 for x in got):
                errs.append(f"{s.get('title')}：存在 0 分空 {got}（每空至少 1 分）")
            for i, t in enumerate(tags):
                if t != "long":
                    continue
                for j, u in enumerate(tags):
                    if u != "long" and got[j] > got[i]:
                        errs.append(f"{s.get('title')}：长句空第{i + 1}空 {got[i]} 分 "
                                    f"低于短空第{j + 1}空 {got[j]} 分")
            stat["seqs"].append((s.get("title"), total,
                                 " ".join(f"{'长' if t == 'long' else '短'}{x}"
                                          for t, x in zip(tags, got))))
        else:
            stat["seqs"].append((s.get("title"), total, "空数/占位符不一致，无法核对"))

        # ★ 断言二：markdown 表格可被简单解析（分隔行唯一、行列数与表头一致）
        stat["tables"] += count_md_tables(content)
        for e in table_shape_ok(content):
            errs.append(f"{s.get('title')}：表格{e}")

        for q in qs:
            if q.get("questionType") != "input":
                errs.append(f"{s.get('title')} id={q.get('id')} questionType={q.get('questionType')}")
            if not (q.get("answer") or "").strip():
                errs.append(f"{s.get('title')} id={q.get('id')} 答案为空")
            if q.get("id") in ids:
                errs.append(f"重复 id {q.get('id')}")
            ids.add(q.get("id"))
            if q.get("content") != "":
                notes.append(f"{s.get('title')} id={q.get('id')} question.content 非空")
            # ★ 断言三：answer 里的图片路径必须真实存在
            for ref in EXAM_REF.findall(q.get("answer") or ""):
                stat["ans_imgs"].add(ref)
                if not absolute_img_path(ref).exists():
                    errs.append(f"{s.get('title')} id={q.get('id')}：答案图片不存在 {ref}")
        stat["sections"].append((s.get("title"), s.get("category"), len(qs), s.get("score"), ssum))

    # 图片逐个 os.path.exists（题干 + 答案，全卷去重；排序保证报告可复现）
    root = _exam_images_root()
    for ref in sorted(set(EXAM_IMG.findall(raw))):
        stat["imgs"].add(ref)
        if not (root / ref).exists():
            errs.append(f"图片缺失：/exam-images/{ref}")
    for ref in sorted(stat["ans_imgs"]):
        stat["imgs"].add(ref[len("/exam-images/"):] if ref.startswith("/exam-images/") else ref)
    return stat, errs, notes


def run_cross_check(args) -> int:
    """逐题让模型独立作答，与 JSON 里的答案表答案比对（只报告，不改 JSON）。

    参考答案大多只有一张字母表、没有逐题解析，所以「答案能否在解析原文里找到字母依据」
    这件事只能靠这个交叉校验来兜：模型与答案表不一致的题列出来，供人工判。
    """
    types = [t for t in (args.type or "二模").replace("，", ",").split(",") if t]
    if "all" in types:
        types = ["二模", "一模", "上期末"]
    root = Path(args.output)
    cache_dir = root / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lines, total_same, total_diff = [], 0, 0
    for type_dir in types:
        for jf in sorted((root / type_dir).glob("*.json")):
            data = json.loads(jf.read_text(encoding="utf-8"))
            ch = next((s for s in data["sections"] if s.get("type") == "选择题"), None)
            if not ch:
                continue
            qs = ch.get("questions") or []
            cc = cache_dir / f"{jf.stem}-cross.json"
            if cc.exists() and not args.force:
                raw = json.loads(cc.read_text(encoding="utf-8"))
                derived = {int(k): (v[0], v[1]) for k, v in raw.items()}
            else:
                derived = ai_solve_choice(qs)
                if derived:
                    cc.write_text(json.dumps({str(k): list(v) for k, v in derived.items()},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
            bad = [(q["id"], str(q.get("answer") or ""), derived[q["id"]][0], derived[q["id"]][1])
                   for q in qs if q["id"] in derived and derived[q["id"]][0] != str(q.get("answer") or "")]
            same = len(qs) - len(bad)
            total_same += same
            total_diff += len(bad)
            print(f"[{type_dir}] {jf.stem}：一致 {same}/{len(qs)}"
                  + (f"，不一致 {len(bad)} 题" if bad else "，全部一致"))
            for no, got, want, why in bad:
                print(f"    · 第{no}题 答案表 {got!r} / 模型推导 {want}（{why[:40]}）")
                lines.append(f"| {type_dir} | {jf.stem} | {no} | {got or '—'} | {want} | {why[:50]} |")
    print(f"\n合计：一致 {total_same} 题，不一致 {total_diff} 题")
    if args.report:
        Path(args.report).write_text(
            "| 类型 | 卷 | 题号 | 答案表 | 模型推导 | 模型理由 |\n"
            "| --- | --- | --- | --- | --- | --- |\n" + "\n".join(lines) + "\n",
            encoding="utf-8")
        print(f"清单已写入 {args.report}")
    return 0


def run_check(args) -> int:
    types = [t for t in (args.type or "二模").replace("，", ",").split(",") if t]
    if "all" in types:
        types = ["二模", "一模", "上期末"]
    root = Path(args.output)
    bad, blocks, summary = 0, [], []
    for type_dir in types:
        files = sorted((root / type_dir).glob("*.json"))
        if not files:
            print(f"没有找到 JSON：{root / type_dir}")
            continue
        for jf in files:
            stat, errs, notes = check_one(jf)
            rev = jf.with_suffix(".review.txt")
            review = [x for x in rev.read_text(encoding="utf-8").splitlines() if x.strip()] if rev.exists() else []
            manual = [x for x in review
                      if not re.match(r"第\d+题：\d+ 分 / \d+ 空（长句", x)]   # 分值分配只是说明，不算人工点
            crops_fail = [x for x in review
                          if x.startswith("PDF 截图失败") or x.startswith("PDF 截图跳过")]
            crops_ok = [x for x in review if x.startswith("PDF 截图：")]
            rows = "\n".join(f"| {t} | {c or '—'} | {n} | {sc} | {ss} |"
                             for t, c, n, sc, ss in stat["sections"])
            seqs = "\n".join(f"- `{t}`（{sc} 分）：{seq}" for t, sc, seq in stat["seqs"])
            head = (f"## {stat['title']}\n\n"
                    f"- 类型：{type_dir}　文件：`{jf}`\n"
                    f"- 选择题：**{stat['choice']} 题 × {stat['unit']} 分 = "
                    f"{stat['choice'] * stat['unit']} 分**\n"
                    f"- 非选择题：**{stat['essay']} 道大题，共 {stat['blanks']} 个空**\n"
                    f"- markdown 表格：**{stat['tables']} 张**（分隔行唯一、行列数与表头一致）\n"
                    f"- 答案图片：**{len(stat['ans_imgs'])} 张**；全卷图片引用 **{len(stat['imgs'])} 张**，"
                    f"逐张 os.path.exists 校验\n"
                    f"- PDF 截图：成功 {len(crops_ok)} 张，失败 {len(crops_fail)} 条\n"
                    f"- 选择题答案依据：**{stat['answer_src']}**（逐题与参考答案原文/答案表比对）\n\n"
                    f"**每空分值序列（长/短）**\n\n{seqs}\n")
            tbl = ("\n| section | category | 空数 | section.score | Σquestions.score |\n"
                   "| --- | --- | --- | --- | --- |\n" + rows + "\n")
            verdict = ("\n**契约校验：✗ 有错误**\n\n" + "\n".join(f"- {e}" for e in errs) + "\n"
                       if errs else
                       f"\n**契约校验：✓ 通过**（json.load / 选择题 {stat['choice']} 题 × "
                       f"{stat['unit']} 分 / options 恒 A–D / "
                       "answer 单个大写字母 / Σquestions.score == section.score / `<Input` 次数 == 空数 / "
                       "占位符成对 / 每空 answer 非空 / 每空分值符合长句优先规则 / markdown 表格可解析 / "
                       "答案里的图片全部存在）\n")
            man = ("\n**需人工核对的点**\n\n" + "\n".join(f"- {x}" for x in manual) + "\n"
                   if manual else "\n**需人工核对的点**：无\n")
            crop = ("\n**无法截图清单**\n\n" + "\n".join(f"- {x}" for x in crops_fail) + "\n"
                    if crops_fail else "\n**无法截图清单**：无\n")
            blocks.append(head + verdict + tbl + man + crop)
            summary.append((type_dir, stat["title"], stat["choice"], stat["essay"], stat["blanks"],
                            stat["tables"], len(stat["ans_imgs"]), len(errs), len(manual),
                            len(crops_fail)))
            if errs:
                bad += 1
            print(f"\n===== {type_dir}/{jf.name} =====")
            print(f"选择题 {stat['choice']} 题 / 大题 {stat['essay']} 道 / 空 {stat['blanks']} 个"
                  f" / 表格 {stat['tables']} 张 / 答案图 {len(stat['ans_imgs'])} 张"
                  f" / 截图失败 {len(crops_fail)}")
            for t, sc, seq in stat["seqs"]:
                print(f"   {t}（{sc} 分）：{seq}")
            print("  ✓ 契约校验通过" if not errs else f"  ✗ 错误 {len(errs)} 条：")
            for e in errs:
                print(f"     - {e}")
            for n in notes:
                print(f"  · 提示：{n}")
            if review:
                print(f"  · review {len(review)} 条")
    for s in summary:
        print(f"  [{s[0]}] {s[1]}：选择 {s[2]} / 大题 {s[3]} / 空 {s[4]} / 表格 {s[5]} / "
              f"答案图 {s[6]} / 错误 {s[7]} / 人工点 {s[8]} / 截图失败 {s[9]}")
    if args.report:
        top = (f"# 生物 {len(summary)} 套卷 · 解析自查报告（{'、'.join(types)}）\n\n"
               "生成方式：`venv/bin/python3 convert_biology_md.py --check -T all --report …`\n\n"
               "| 类型 | 卷 | 选择题 | 大题 | 空 | 表格 | 答案图 | 契约错误 | 人工点 | 截图失败 |\n"
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
               + "\n".join(f"| {s[0]} | {s[1]} | {s[2]} 题 | {s[3]} 道 | {s[4]} | {s[5]} | {s[6]} | "
                           f"{s[7]} | {s[8]} | {s[9]} |" for s in summary) + "\n\n")
        Path(args.report).write_text(top + "\n".join(blocks), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    print(f"\n合计 {len(summary)} 份，出错 {bad} 份")
    return 0 if bad == 0 else 2


# ──────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="生物整卷 → 题库 JSON")
    ap.add_argument("-p", "--path", help="单份 MD 路径")
    ap.add_argument("--dir", default=str(OUTPUT_DOCX_TO_MD), help="MD 根目录")
    ap.add_argument("-T", "--type", default="二模",
                    help="试卷类型（输出子目录名，如 二模 / 一模 / 上期末；--check 时可写 all 或逗号分隔多个）")
    ap.add_argument("-s", "--subject", default=SUBJECT, help="学科（默认 生物）")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--papers", nargs="*", default=None,
                    help="只处理这几套（按地名/卷名片段匹配，如 海淀 西城 朝阳）")
    ap.add_argument("--force", action="store_true", help="忽略已有 JSON 与 AI 缓存，重跑")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="不调模型（只吃缓存）")
    ap.add_argument("--check", action="store_true",
                    help="只做自查（含选择题答案与参考答案原文/答案表比对）")
    ap.add_argument("--cross-check", action="store_true",
                    help="逐题让模型独立作答并与答案表比对（只报告，改不了 JSON）")
    ap.add_argument("--report", default=None, help="自查报告输出路径（配合 --check）")
    args = ap.parse_args()

    if args.cross_check:
        raise SystemExit(run_cross_check(args))
    if args.check:
        raise SystemExit(run_check(args))

    OUT["root"] = Path(args.output)
    if args.path:
        files = [Path(args.path)]
    else:
        root = Path(args.dir) / args.type / args.subject
        files = sorted(p for d in sorted(root.iterdir()) if d.is_dir()
                       for p in sorted(d.glob("*.md"))[:1]) if root.is_dir() else []
        if args.papers:
            files = [f for f in files if any(k in f.stem for k in args.papers)]
    log(f"共 {len(files)} 份 MD → {OUT['root'] / args.type}")
    for f in files:
        t0 = time.time()
        try:
            log("  " + process(f, args.type, args) + f"（{time.time() - t0:.0f}s）")
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
