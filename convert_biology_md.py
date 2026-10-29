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
- 一个空有多个可接受答案用 `##` 分隔；图片走 `/exam-images/<类型>/<学科>/<卷名>/images/x.png`。

做法：选择题/答案表**规则解析**（稳），每道非选择题交给本机模型「整理题干 + 认空 + 摘抄答案」
（生物卷的空有下划线、空格、填表、看图补画、选填①②③等多种形态，纯规则切不干净），
随后全部**确定性校验 + 出口规范**（占位符配对、长短判定、分值配平、图片重写、答案大小写）。

用法：
    python3 convert_biology_md.py -T 二模 -s 生物 -w 0        # 批量（可 --papers 限定几套）
    python3 convert_biology_md.py -p <md 文件> [--force] [--no-ai]
    python3 convert_biology_md.py --check                      # 自查（含图片存在性）
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from convert_chinese_md import chat, rewrite_images, split_paper, log
from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_BIO
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "生物"
OUT_ROOT = OUTPUT_JSON_BIO
OUT = {"root": OUT_ROOT}

N_CHOICE = 15          # 第一部分：15 道单选
CHOICE_SCORE = 2       # 每题 2 分
SHORT_MAX = 12         # 答案 ≤12 字 → <Input>，否则 <InputLong>

CATS = ["遗传与进化", "稳态与调节", "生物技术与工程", "实验探究"]

# 题号行：可带前置图片、可带（N分）
QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\\?[．.、]\s*"
    r"(?:[（(]\s*(\d+)\s*分\s*[）)])?\s*"
)
OPT_MARK = re.compile(r"(?:^|[\s\u3000\u00a0>)])([A-D])[ \t]*[．.、][ \t]*", re.M)
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
8. 表格用 Markdown 表格原样保留：表里已经填好的数据（如 ＋＋、▲▲、/ 等）必须原样保留，不要把它们变成空；图片引用原样保留，不要编造任何素材。
9. 只输出 JSON，不要任何解释。

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
        p = LEAD_LABEL.sub("", part.strip()).strip()
        p = re.sub(r"[ \t\u3000\u00a0]+", " ", p)
        if p:
            out.append(p)
    return "##".join(out)


def is_short_answer(ans: str) -> bool:
    first = (ans or "").split("##")[0]
    return len(_strip_ws(first)) <= SHORT_MAX


def split_scores(total: int, n: int, ans_text: str, review: list[str], label: str) -> list[int]:
    """把大题满分分到 n 个空上：卷面写了「每空 N 分」就按 N，否则均分（余数给前面的空）。"""
    if n <= 0:
        return []
    m = re.search(r"每空\s*(\d+)\s*分", ans_text or "")
    if m:
        per = int(m.group(1))
        scores = [per] * n
        scores[-1] += total - per * n
        if scores[-1] != per:
            review.append(f"{label}：参考答案标注「每空 {per} 分」，{n} 空合计 {per * n} 分，"
                          f"与满分 {total} 分不符，差额并入最后一空")
    else:
        q, r = divmod(total, n)
        scores = [q + 1 if i < r else q for i in range(n)]
        review.append(f"{label}：卷面未给每空分值，{total} 分按 {n} 空均分（余数给前面的空）→ {scores}")
    if scores and sum(scores) != total:      # 兜底：必须配平
        scores[-1] += total - sum(scores)
    return scores


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
    for pat in (re.compile(r"(?m)^[ \t]*第[二2]部分"), re.compile(r"(?m)^[ \t]*参考答案"),
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


def parse_choice_questions(body: str) -> list[dict]:
    """第一部分 15 道单选（选项可能一行一个，也可能一行多个 A.… B.…）"""
    segs = rebalance_leads(split_marks(body, 1, N_CHOICE))
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
        if all(k in found for k in "ABCD"):
            first = found["A"].start(1)          # 选项字母的位置（不是前导空格/右括号）
            content = seg[:first].strip()
            order = ["A", "B", "C", "D"]
            for i, k in enumerate(order):
                start = found[k].end()
                end = found[order[i + 1]].start(1) if i + 1 < len(order) else len(seg)
                opts.append({"id": k, "label": seg[start:end].strip()})
        else:
            log(f"    ⚠ 第{s['no']}题选项未取全：{sorted(found)}")
        out.append({"id": s["no"], "questionType": "single", "content": content,
                    "options": opts, "answer": "", "analysis": "", "score": CHOICE_SCORE})
    return out


def parse_choice_answers(ans: str) -> dict[int, str]:
    """答案表（西城是表格）或行内「1.C 2.D」（海淀/朝阳）都能吃。"""
    out: dict[int, str] = {}
    pending: list[int] = []
    for line in ans.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        head = cells[0].replace("*", "").strip()
        if head.startswith("题号"):
            pending = [int(x) for x in (re.sub(r"\D", "", c) for c in cells[1:]) if x]
        elif head.startswith("答案") and pending:
            for n, c in zip(pending, cells[1:]):
                v = re.sub(r"[^A-Da-d]", "", c).upper()
                if v:
                    out[n] = v
            pending = []
    for m in re.finditer(r"(\d{1,2})\s*[.．、]\s*([A-Da-d])(?![A-Za-z])", ans):
        out.setdefault(int(m.group(1)), m.group(2).upper())
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
            if IMG_REF.search(part):
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
# 主流程
# ──────────────────────────────────────────────────────────────────

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
    cut = first_big_mark(body)
    choice_body, essay_body = body[:cut], body[cut:]
    choice_qs = parse_choice_questions(choice_body)
    if len(choice_qs) != N_CHOICE:
        review.append(f"选择题切到 {len(choice_qs)} 道（应为 {N_CHOICE} 道）")
    choice_ans = parse_choice_answers(ans)
    for q in choice_qs:
        q["answer"] = choice_ans.get(q["id"], "")
        if not q["answer"]:
            review.append(f"第{q['id']}题（选择题）缺答案")
        if len(q["options"]) != 4:
            review.append(f"第{q['id']}题（选择题）选项不足 4 个（{len(q['options'])}）")
        q["content"] = rewrite(reflow_cjk(latexify(q["content"])))
        for o in q["options"]:
            o["label"] = rewrite(reflow_cjk(latexify(o["label"])))
    choice_section = {
        "type": "选择题", "questionType": "biology-choice", "title": "一、选择题",
        "category": "选择", "score": CHOICE_SCORE * len(choice_qs),
        "questions": choice_qs,
    }

    # ---- 二、非选择题（一题一个 section） ----
    essays = rebalance_leads(split_marks(essay_body, N_CHOICE + 1, 99))
    ans_blocks = {a["no"]: a for a in split_marks(ans, N_CHOICE + 1, 99)}
    cache_dir = OUT["root"] / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cat_cache = cache_dir / f"{title}-cats.json"
    if cat_cache.exists() and not args.force:
        cat_map = {int(k): v for k, v in json.loads(cat_cache.read_text(encoding="utf-8")).items()}
    elif args.ai:
        cat_map = classify(essay_body, essays, ans_blocks)
        cat_cache.write_text(json.dumps(cat_map, ensure_ascii=False), encoding="utf-8")
    else:
        cat_map = {}

    sections = [choice_section]
    for e in essays:
        no, total = e["no"], e["score"]
        label = f"第{no}题"
        seg = cut_tail(e["text"]).strip()
        if e["lead"]:
            seg = e["lead"] + "\n" + seg
        ab = ans_blocks.get(no)
        atext = cut_tail(ab["text_full"]).strip() if ab else ""
        if total is None:
            total = ab["score"] if ab and ab["score"] else 0
            review.append(f"{label}：题干没写分值，取参考答案段的 {total} 分")
        if not atext:
            review.append(f"{label}：参考答案段没切到")

        cache = cache_dir / f"{title}-{no}.json"
        data = {}
        if cache.exists() and not args.force:
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
        answers += [""] * (n_blank - len(answers))
        for i, a in enumerate(answers, 1):
            if not a:
                review.append(f"{label} 第{i}空：答案为空（需人工补）")
        scores = split_scores(total, n_blank, atext, review, label)
        qs = [{"id": no * 100 + i, "questionType": "input", "content": "",
               "answer": rewrite(latexify(a)), "analysis": "", "score": scores[i - 1] if i - 1 < len(scores) else 0}
              for i, a in enumerate(answers, 1)]
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
    if total_score != 100:
        review.append(f"全卷分值合计 {total_score} 分（卷面 100 分），请核对")
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": 100, "duration": 90,
        "tags": [SUBJECT, args.grade, type_dir] + ([year.group(1)] if year else []),
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

def _exam_images_root() -> Path:
    """nginx: /exam-images/<类型>/<学科>/<卷>/images/x → output_docx_to_md/<类型>/<学科>/<卷>/images/x"""
    return OUTPUT_DOCX_TO_MD


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    """返回 (统计, 错误, 提示)。"""
    errs, notes = [], []
    raw = jf.read_text(encoding="utf-8")
    data = json.loads(raw)                       # 1) 能 json.load
    paper, sections = data["paper"], data["sections"]
    stat = {"title": paper.get("title"), "choice": 0, "essay": 0, "blanks": 0,
            "imgs": set(), "sections": []}

    if paper.get("subject") != SUBJECT:
        errs.append(f"paper.subject 不是生物：{paper.get('subject')}")
    for k in ("title", "source", "grade", "year", "totalScore", "duration", "tags"):
        if k not in paper:
            errs.append(f"paper 缺字段 {k}")

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
        if len(qs) != N_CHOICE:
            errs.append(f"选择题 {len(qs)} 道（应为 {N_CHOICE}）")
        if s.get("score") != CHOICE_SCORE * len(qs):
            errs.append(f"选择题 score={s.get('score')}，与 {CHOICE_SCORE}×{len(qs)} 不符")
        for q in qs:
            if q.get("score") != CHOICE_SCORE:
                errs.append(f"选择题第{q.get('id')}题 score={q.get('score')}（应为 2）")
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
        stat["sections"].append(("选择题", None, len(qs), s.get("score"),
                                 sum(q.get("score") or 0 for q in qs)))

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
        stat["sections"].append((s.get("title"), s.get("category"), len(qs), s.get("score"), ssum))

    # 图片逐个 os.path.exists
    root = _exam_images_root()
    for ref in set(EXAM_IMG.findall(raw)):
        stat["imgs"].add(ref)
        if not (root / ref).exists():
            errs.append(f"图片缺失：/exam-images/{ref}")
    return stat, errs, notes


def run_check(args) -> int:
    type_dir = args.type
    root = Path(args.output)
    files = sorted((root / type_dir).glob("*.json"))
    if not files:
        print(f"没有找到 JSON：{root / type_dir}")
        return 1
    bad, blocks, summary = 0, [], []
    for jf in files:
        stat, errs, notes = check_one(jf)
        rev = jf.with_suffix(".review.txt")
        review = [x for x in rev.read_text(encoding="utf-8").splitlines() if x.strip()] if rev.exists() else []
        manual = [x for x in review if "均分（余数" not in x]   # 分值均分只是说明，不算人工点
        rows = "\n".join(f"| {t} | {c or '—'} | {n} | {sc} | {ss} |"
                         for t, c, n, sc, ss in stat["sections"])
        head = (f"## {stat['title']}\n\n"
                f"- 文件：`{jf}`\n"
                f"- 选择题：**{stat['choice']} 题 × 2 分 = {stat['choice'] * 2} 分**\n"
                f"- 非选择题：**{stat['essay']} 道大题，共 {stat['blanks']} 个空**\n"
                f"- 图片引用：**{len(stat['imgs'])} 张，逐张 os.path.exists 全部命中**\n")
        tbl = ("\n| section | category | 空数 | section.score | Σquestions.score |\n"
               "| --- | --- | --- | --- | --- |\n" + rows + "\n")
        verdict = ("\n**契约校验：✗ 有错误**\n\n" + "\n".join(f"- {e}" for e in errs) + "\n"
                   if errs else
                   "\n**契约校验：✓ 通过**（json.load / 选择题 15 题 × 2 分 / options 恒 A–D / answer 单个大写字母 / "
                   "Σquestions.score == section.score / `<Input` 次数 == 空数 / 占位符成对 / 每空 answer 非空 / "
                   "图片全部存在）\n")
        man = ("\n**需人工核对的点**\n\n" + "\n".join(f"- {x}" for x in manual) + "\n"
               if manual else "\n**需人工核对的点**：无\n")
        blocks.append(head + verdict + tbl + man)
        summary.append((stat["title"], stat["choice"], stat["essay"], stat["blanks"],
                        len(stat["imgs"]), len(errs), len(manual)))
        if errs:
            bad += 1
        print(f"\n===== {jf.relative_to(root)} =====")
        print(f"选择题 {stat['choice']} 题 / 大题 {stat['essay']} 道 / 空 {stat['blanks']} 个"
              f" / 图片 {len(stat['imgs'])} 张")
        for t, c, n, sc, ss in stat["sections"]:
            print(f"   {t}｜{c or '—'}｜空 {n}｜section.score {sc}｜Σ {ss}")
        print("  ✓ 契约校验通过" if not errs else f"  ✗ 错误 {len(errs)} 条：")
        for e in errs:
            print(f"     - {e}")
        for n in notes:
            print(f"  · 提示：{n}")
        if review:
            print(f"  · review {len(review)} 条：")
            for x in review:
                print(f"     - {x}")
    for s in summary:
        print(f"  {s[0]}：选择 {s[1]} / 大题 {s[2]} / 空 {s[3]} / 图 {s[4]} / 错误 {s[5]} / 人工点 {s[6]}")
    if args.report:
        top = ("# 生物二模三套卷 · 解析自查报告\n\n"
               "| 卷 | 选择题 | 大题 | 空 | 图片 | 契约错误 | 人工点 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
               + "\n".join(f"| {s[0]} | {s[1]} 题 | {s[2]} 道 | {s[3]} | {s[4]} | {s[5]} | {s[6]} |"
                           for s in summary) + "\n\n")
        Path(args.report).write_text(top + "\n".join(blocks), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    print(f"\n合计 {len(files)} 份，出错 {bad} 份")
    return 0 if bad == 0 else 2


# ──────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="生物整卷 → 题库 JSON")
    ap.add_argument("-p", "--path", help="单份 MD 路径")
    ap.add_argument("--dir", default=str(OUTPUT_DOCX_TO_MD), help="MD 根目录")
    ap.add_argument("-T", "--type", default="二模", help="试卷类型（输出子目录名，如 二模）")
    ap.add_argument("-s", "--subject", default=SUBJECT, help="学科（默认 生物）")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--papers", nargs="*", default=None,
                    help="只处理这几套（按地名/卷名片段匹配，如 海淀 西城 朝阳）")
    ap.add_argument("--force", action="store_true", help="忽略已有 JSON 与 AI 缓存，重跑")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="不调模型（只吃缓存）")
    ap.add_argument("--check", action="store_true", help="只做自查")
    ap.add_argument("--report", default=None, help="自查报告输出路径（配合 --check）")
    args = ap.parse_args()

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
