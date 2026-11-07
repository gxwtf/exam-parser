#!/usr/bin/env python3
"""语文试卷（教师版）docx→MD → 结构化试卷 JSON（链路 ⑧，2026-11 迁移到成熟格式）

一个入口打通「md → JSON + review」，自带 `--check`，不再需要串行跑
`merge_chinese_groups.py` / `normalize_chinese_scores.py` / `repair_chinese_tables.py` / `sanitize_chinese.py`
（四者的等价逻辑已并入本脚本出口，见 `merge_sections` / `normalize_scores` / `repair_tables` / `sanitize_payload`）。

和英语同一套思路：**大段文章不交给模型复述**，而是让模型给出「原文里的起止锚点（anchor）」，
再由脚本从 Markdown 里按锚点精确截出文章；题目/选项/答案同样按题号从原文切，模型只做结构规划。
规划结果缓存到 `<输出根>/<类型>/.cache/<卷名>-plan.json`，重跑同一卷不再调模型（可复现、可断网重跑）。

输出：output_json_chinese/<类型>/<卷>.json（**顶层键 `sections`**，与生物/历史/地理/政治一致）
{
  "paper": {title, subject, source, grade, year, totalScore, duration, tags},
  "sections": [
    {"type":"多文本","category":"多文本","questionType":"chinese-reading","title":"多文本阅读 · …",
     "intro":"阅读下面材料，完成1-5题。","content":"<文章 Markdown>","score":18,
     "questions":[{"id":1,"questionType":"single","content":"…","options":[…],"answer":"C","analysis":"","score":3}]}
  ]
}

板块口径（题库契约，不能改）：
| 板块 category | section.questionType | 小题 questionType | 内联占位 |
| 多文本/文言文/古诗/文学类/语用/名著 | chinese-reading | single / text | 无（前端 `Problem` 按小题渲染，`content` 只放阅读材料）|
| 默写 | chinese-dictation | input | 每空一个 `<Input2 />`（在 `content` 里内联）|
| 微写作 | chinese-micro-writing | text | 无（前端按题目渲染大答题框）|
| 作文 | chinese-essay | text | 无（同上）|
整卷 150 分；默写每空 1 分（8 或 10 空）、微写作 10 分、作文 50 分。

用法：
  python3 convert_chinese_md.py -p ../output_docx_to_md/一模/语文/<卷>/<卷>.md [-T 一模] [-o 输出根]
  python3 convert_chinese_md.py -T 一模                       # 处理 output_docx_to_md/一模/语文/*
  python3 convert_chinese_md.py --check -T 一模                # 契约自检（纯 Python，不调模型）
  python3 convert_chinese_md.py --check -p <单卷 json | 卷名>   # 单卷自检
"""
import argparse, hashlib, json, os, re, time
from pathlib import Path

import requests
from dotenv import load_dotenv
from paths import GAOZHONG, OUTPUT_DOCX_TO_MD, OUTPUT_JSON_CHINESE
from src.answer_case import normalize_choice_case_in_paper

try:
    from docx_dots import DocxDots
except Exception:  # 读不了 docx 也不能影响主流程
    DocxDots = None

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

MD_ROOT = OUTPUT_DOCX_TO_MD
OUT_ROOT = OUTPUT_JSON_CHINESE

BASE_URL = (os.getenv("MATH_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = os.getenv("MATH_VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")

QSPLIT = re.compile(r"(?m)^[ \t\u3000\u00a0]*(?:(?:!\[[^\]]*\]\([^)]*\)|⟦IMG\d+⟧)[ \t]*)*\**[ \t]*(\d{1,2})[ \t]*\\?(?:[、．]|\.(?!\d|%))[ \t]*")
SEC_SPLIT = re.compile(r"(?m)^[ \t]*([一二三四五六七八九十])、\s*本大题共\s*(\d+)\s*小题[，,]\s*共\s*(\d+)\s*分")
OPT_LINE = re.compile(r"(?m)^[ \t\u3000\u00a0]*(?:(?:!\[[^\]]*\]\([^)]*\)|⟦IMG\d+⟧)[ \t]*)*\**[ \t]*([A-D])[ \t]*\\?[．.、][ \t]*(.+)$")
BLANK = re.compile(r"(?:\\_\*\*)+\\_?\**")

SYSTEM_PROMPT = "你是语文试卷结构化助手，只输出 JSON，不要解释。"

PLAN_PROMPT = r"""下面是一份北京高三语文二模试卷的【试题部分】原文（Markdown，来自 Word 转换）。
请只做「结构规划」，**不要改写原文**。输出 JSON：

{"groups":[
  {"category":"多文本|文言文|古诗|文学类|名著|默写|语用|微写作|作文",
   "questionType":"chinese-reading|chinese-dictation|chinese-micro-writing|chinese-essay",
   "title":"该题组短标题，如「多文本阅读 · 中国古典学」",
   "intro":"该题组引导语（原文逐字，如「阅读下面材料，完成1-5题。」；没有就空字符串）",
   "questions":[1,2,3,4,5],
   "article_start":"阅读材料的开头 10-30 个字（原文逐字）",
   "article_end":"阅读材料的结尾 10-30 个字（原文逐字）",
   "poetry":"shi|ci（仅古诗题组：诗写 shi，词写 ci；其它题型留空）"}
]}

规则：
1. 按题型拆组：一个大题里可能混着多个题型（例如第 14 题是默写、第 15 题是名著），必须拆开成两组。
2. 只有「有阅读材料」的题组才给 article_start/article_end；默写/微写作/作文没有材料，写空字符串。
3. questionType 映射：多文本/文言文/古诗/文学类/名著/语用 → chinese-reading；默写 → chinese-dictation；
   微写作 → chinese-micro-writing；作文 → chinese-essay。
4. **整本书阅读**（材料是多则《论语》语录、或《红楼梦》《乡土中国》《边城》《呐喊》等整本书的节选/回目）
   一律归「名著」，即使它排在「文言文」大题里、卷面没有单独标题；两小问按 (1)(2) 算同组的 2 道小题。
5. article_start / article_end 必须是原文里**逐字出现**的片段（用于脚本精确截取），不要概括、不要加标点。
6. 题号用数字数组，按原文题号。
7. 只输出 JSON。

原文：
"""


# ── 成熟格式（sections）常量 ────────────────────────────────────────
CATEGORIES = ["多文本", "文言文", "古诗", "文学类", "名著", "默写", "语用", "微写作", "作文"]
QTYPE_OF = {
    "多文本": "chinese-reading", "文言文": "chinese-reading", "古诗": "chinese-reading",
    "文学类": "chinese-reading", "名著": "chinese-reading", "语用": "chinese-reading",
    "默写": "chinese-dictation", "微写作": "chinese-micro-writing", "作文": "chinese-essay",
}
# 北京卷标准分值（卷面分值解析不到时的兜底；正常以卷面为准）
DEFAULT_SCORES = {"多文本": 18, "文言文": 18, "古诗": 12, "默写": 8, "名著": 10,
                  "文学类": 18, "语用": 6, "微写作": 10, "作文": 50}
TOTAL_SCORE = 150
FIXED_SCORES = {"微写作": 10, "作文": 50}      # 硬口径（§6.7）
DICTATION_SCORES = (8, 10)                     # 默写：每空 1 分，8 或 10 空
PLACEHOLDER_TAGS = ("Input2", "Blank", "Input", "InputLong", "Blank2", "ClozeBlank")
PH_RE = re.compile(r"<(?:" + "|".join(PLACEHOLDER_TAGS) + r")\b[^<>]*>")

# 出口清洗：分区标题 / 卷首说明 / 残留题号 / 纯 ①②③④ 行
RESIDUE_PATTERNS = [
    re.compile(r"^\s*\**\s*[一二三四五六七八九十]\s*、\s*本大题共[^\n]*$"),
    re.compile(r"^\s*\**\s*第[一二二2]\s*部分[^\n]*$"),
    re.compile(r"^\s*\**\s*本试卷共[^\n]*$"),
    re.compile(r"^\s*\**\s*语\s*文\s*\**$"),
    re.compile(r"^\s*\**\s*20\d{2}\s*[.．]\s*\d{1,2}\s*\**$"),
    re.compile(r"^\s*\**(?:一|二|三|四|五|六|七)\s*、\s*(?:本大题共)?\s*$"),
    re.compile(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]{1,10}\s*$"),
    re.compile(r"^\s*[（(]\s*共\s*\d+\s*分\s*[）)]\s*$"),
]
NOTICE_RE = re.compile(r"(考生须知|本试卷分第|本部分共\s*\d+\s*题|下列各小题均有四个选项|"
                       r"答题前[，,]?\s*(?:请|考生)|考试时间\s*\d+\s*分钟\s*满分)")


def strip_residue(text: str, intro: str = "") -> str:
    """出口清洗：去掉材料/题干开头的分区标题、卷首说明、残留题号、重复的引导语。"""
    if not text:
        return text
    lines = text.split("\n")
    intro_s = (intro or "").strip()
    changed = True
    while changed and lines:
        changed = False
        i = next((k for k, ln in enumerate(lines) if ln.strip()), None)
        if i is None:
            break
        first = lines[i].strip()
        if intro_s and first == intro_s:                      # 引导语已在 intro 里
            lines = lines[i + 1:]
            changed = True
            continue
        if any(p.match(first) for p in RESIDUE_PATTERNS):
            lines = lines[i + 1:]
            changed = True
            continue
        if NOTICE_RE.search(first[:120]) and len(first) <= 200:
            lines = lines[i + 1:]
            changed = True
            continue
        break
    return "\n".join(lines).strip()


def blank_phrases(ans_text: str) -> list[list[str]]:
    """默写答案 → 每行（(1)(2)… 或 ①②③…）的若干短语（每短语 = 一个空）。"""
    t = re.split(r"【?评分(?:参考|标准|要求|说明|细则|意见)】?|【考点】|【分析】|【解答】|【点评】",
                 ans_text or "")[0]
    parts: list[str] = []
    marks = list(SUBQ_RE.finditer(t))
    if marks:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
            parts.append(t[m.end():end])
    else:
        marks2 = list(re.finditer(r"[①②③④⑤⑥⑦⑧⑨⑩]", t))
        if marks2:
            for i, m in enumerate(marks2):
                end = marks2[i + 1].start() if i + 1 < len(marks2) else len(t)
                parts.append(t[m.end():end])
        else:
            parts = [t]
    out = []
    for p in parts:
        p = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩]", " ", p)
        ph = [x for x in re.split(r"[\s，,、；;]+", p.strip()) if x and x not in ("分", "。")]
        out.append(ph)
    return out


BLANK_RUN = re.compile(r"[\\_*]*\\[_*][\\_*]*")     # Word 导出的下划线填空字形（\_**\_**…）
BLANK_MARK = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")
# 引号内只剩空格当空位的写法：`"落木千山天远大， "` / `" ， ，进，吾往也"`


def tidy_blank_glyphs(text: str) -> str:
    """把残留的下划线填空字形换成全角下划线（MDX/Markdown 里安全、不会变成分隔线/强调）。"""
    return BLANK_RUN.sub("＿＿＿", text or "")


_QS = "\u201c\u201d\u300c\u300d\"\u2018\u2019"


def space_slots_in_quotes(line: str) -> str:
    """把引号内只剩空格当空位的写法补成占位符（如 " ， ，进，吾往也" → "<Input2 />，<Input2 />，进，吾往也"）。"""
    def repl(m):
        inner = m.group(2)
        inner2 = re.sub(r"[ \t\u3000]+", "<Input2 />", inner)
        return m.group(1) + inner2 + m.group(3)

    for a, b in (("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"'), ("\u2018", "\u2019")):
        line = re.sub("(" + re.escape(a) + ")([^" + re.escape(b) + "]*)" + "(" + re.escape(b) + ")", repl, line)
    return line


def build_dictation(stem: str, ans_text: str, qid, review: list[str],
                    max_blanks: int | None = None):
    """默写：题干里每个空 → `<Input2 />`，每个空一道 input 小题（答案按空对）。

    空数以**答案短语数**为准（教师版答案最可靠），题面里的空位只是「插在哪」的线索：
    1) 按 (1)(2)… 或 ①②③ 把题干分行、把答案分组成 N_k 个短语；
    2) 每行找空位：下划线字形（Word 导出的 `_`+`**` 组合）> 引号内/全角空格 > 行内 ①②③ 标记；
    3) 空位比答案少就往行尾补、比答案多就删多余，最后保证
       「占位符数 == 小问数 == 默写分」，并把对不上的地方写进 review。
    """
    groups = blank_phrases(ans_text)
    lines = stem.split("\n")
    # 分行：优先按 (1)(2)…，否则按行首 ①②③
    line_idx = [i for i, ln in enumerate(lines) if SUBQ_RE.match(ln.strip())]
    if not line_idx:
        line_idx = [i for i, ln in enumerate(lines)
                    if re.match(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]", ln.strip())]
    if not line_idx:                                     # 最后才退回「有下划线的行」
        line_idx = [i for i, ln in enumerate(lines) if BLANK_RUN.search(ln)]
    if not line_idx:
        line_idx = list(range(len(lines)))
    if len(groups) < len(line_idx) and groups:
        groups += [[]] * (len(line_idx) - len(groups))

    def slots_of(line: str):
        runs = list(BLANK_RUN.finditer(line))
        if runs:
            return [(m.start(), m.end()) for m in runs]
        marks = [m for m in BLANK_MARK.finditer(line)
                 if not re.match(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]", line[max(0, m.start() - 1):m.start() + 1])]
        if marks:                                  # 行内 ①②③ 就是空位
            return [(m.start(), m.end()) for m in marks]
        # 引号内或整行的空格（半角/全角）
        return [(m.start(), m.end()) for m in re.finditer(r"[ \t\u3000]{1,}", line)]

    cap = 14
    if max_blanks and 4 <= max_blanks <= 14:
        cap = max_blanks
    blanks: list[str] = []
    for k, li in enumerate(line_idx):
        ln = lines[li]
        phrases = groups[k] if k < len(groups) else []
        n = len(phrases)
        _runs = len(list(BLANK_RUN.finditer(ln)))
        if phrases and _runs:
            n = min(len(phrases), _runs)          # 双证：答案短语数 + 卷面下划线数，取较小
            if len(phrases) != _runs:
                review.append(f"默写第{k + 1}行：答案 {len(phrases)} 句 / 卷面下划线 {_runs} 处 → 取 {n}")
        room = cap - len(blanks)
        if room <= 0:
            review.append(f"默写第{k + 1}行：已满 {cap} 空，余下按原文保留")
            continue
        if n > room:
            review.append(f"默写第{k + 1}行：{n} 个短语超出剩余容量，只留 {room} 个")
            n = room
        slots = slots_of(ln)
        if n == 0:                                        # 答案没切出来 → 按空位兜底
            n = max(1, len(slots)) if slots else 1
            review.append(f"默写第{k + 1}行：答案里没切出短语，按空位 {n} 个留空")
        # 先删掉行首的 ①②③ 题号（不是空位）
        ln = re.sub(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*", "", ln)
        slots = slots_of(ln)
        if len(slots) == n:
            out, pos = [], 0
            for a, b in slots:
                out.append(ln[pos:a])
                out.append("<Input2 />")
                pos = b
            out.append(ln[pos:])
            ln = "".join(out)
        elif len(slots) > n:                              # 空位比答案多 → 只留前 n 个
            out, pos, used = [], 0, 0
            for a, b in slots:
                out.append(ln[pos:a])
                if used < n:
                    out.append("<Input2 />")
                    used += 1
                pos = b
            out.append(ln[pos:])
            ln = "".join(out)
            review.append(f"默写第{k + 1}行：空位 {len(slots)} 个 / 答案 {n} 个，多余空位已删")
        else:                                             # 空位比答案少 → 补到行尾
            out, pos = [], 0
            for a, b in slots:
                out.append(ln[pos:a])
                out.append("<Input2 />")
                pos = b
            out.append(ln[pos:])
            ln = "".join(out) + "".join(["<Input2 />"] * (n - len(slots)))
            review.append(f"默写第{k + 1}行：空位 {len(slots)} 个 / 答案 {n} 个，已补 {n - len(slots)} 个")
        blanks += [phrases[i] if i < len(phrases) else "" for i in range(n)]
        lines[li] = ln
    content = "\n".join(lines)
    content = BLANK_RUN.sub("<Input2 />", content)         # 其它行残留的下划线
    content = BLANK_MARK.sub("", content)                 # 残留的①②③标注清掉
    if len(blanks) > 14 or (max_blanks and 4 <= max_blanks <= 14 and len(blanks) > max_blanks):
        keep = max_blanks if (max_blanks and 4 <= max_blanks <= 14) else 8
        review.append(f"默写：答案切出 {len(blanks)} 个短语（超出 4–14 合理区间）→ 只留前 {keep} 个")
        blanks = blanks[:keep]
    if max_blanks and len(blanks) > max_blanks:
        review.append(f"默写：答案切出 {len(blanks)} 个短语，超过卷面 {max_blanks} 空 → 只留前 {max_blanks} 个")
        blanks = blanks[:max_blanks]
    n_ph = len(PH_RE.findall(content))
    if n_ph != len(blanks):
        review.append(f"默写：占位符 {n_ph} 个 ≠ 答案短语 {len(blanks)} 个（按占位符数对齐）")
        blanks = (blanks + [""] * n_ph)[:n_ph]

    questions = [{"id": f"{qid}-{i + 1}", "questionType": "input", "content": "",
                  "options": [], "answer": blanks[i], "analysis": "", "score": 1}
                 for i in range(len(blanks))]
    return content, questions


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def chat(messages, max_tokens=8000, retries=4):
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.0, "messages": messages}
    if os.getenv("MATH_REASONING", "none").lower() in ("none", "off", "false"):
        payload["reasoning_effort"] = "none"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers,
                              timeout=int(os.getenv("MATH_REQUEST_TIMEOUT", "600")))
            if r.status_code == 400 and "reasoning" in (r.text or "").lower():
                payload.pop("reasoning_effort", None); continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(8 * (attempt + 1)); last = f"HTTP {r.status_code}"; continue
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    return json.loads(m.group(0))
                raise
        except Exception as e:
            last = str(e)[:150]; time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"调用模型失败：{last}")


def split_paper(txt: str):
    for marker in ("参考答案", "答案与解析", "答案"):
        i = txt.find(marker)
        if i > 300:
            return txt[:i], txt[i:]
    return txt, ""


def find_anchor(body: str, anchor: str) -> int:
    """在原文里找锚点（容忍空白差异）；找不到返回 -1"""
    if not anchor:
        return -1
    a = anchor.strip()
    i = body.find(a)
    if i >= 0:
        return i
    pattern = r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", a))
    m = re.search(pattern, body)
    return m.start() if m else -1


def section_of(body: str, qno: int) -> tuple[int, int]:
    """返回该题号所在「大题」的 (start, end) 偏移"""
    secs = [(m.start(), m.end()) for m in SEC_SPLIT.finditer(body)]
    qmarks = [(m.start(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    pos = next((p for p, n in qmarks if n == qno), None)
    if pos is None:
        return 0, len(body)
    start = 0
    end = len(body)
    for s, e in secs:
        if s <= pos:
            start = s
        if s > pos:
            end = s
            break
    return start, end


def _question_segment(body: str, qno: int) -> str:
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    hit = next(((s2, e2) for s2, e2, n in marks if n == qno), None)
    if not hit:
        return ""
    nxt = next((s2 for s2, _, n in marks if n > qno), len(body))
    seg = body[hit[1]:nxt]
    cut = len(seg)
    for pat in (SEC_SPLIT,
                re.compile(r"(?m)^[ \t]*[一二三四五六七八九十]、\s*本大题"),
                re.compile(r"(?m)^[ \t]*阅读下面[^\n]{0,30}完成\s*\d"),
                re.compile(r"(?m)^[ \t]*\**[ \t]*[一二三四五六七八九十]\s*、"),
                re.compile(r"(?m)^[ \t]*第[二2]部分")):
        m = pat.search(seg)
        if m and m.start() < cut:
            cut = m.start()
    return seg[:cut]


def parse_question(body: str, qno: int) -> dict | None:
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    hit = next(((s, e) for s, e, n in marks if n == qno), None)
    if not hit:
        return None
    s, e = hit
    nxt = next((s2 for s2, _, n in marks if n > qno), len(body))
    seg = body[e:nxt]
    seg = re.split(r"\n\s*参考答案", seg)[0]
    # 截断到下一部分/下一大题/下一组引导语（否则会把「二、本大题共5小题」并进上一题）
    cut = len(seg)
    for pat in (SEC_SPLIT,
                re.compile(r"(?m)^[ \t]*[一二三四五六七八九十]、\s*本大题"),
                re.compile(r"(?m)^[ \t]*阅读下面[^\n]{0,30}完成\s*\d"),
                re.compile(r"(?m)^[ \t]*\**[ \t]*[一二三四五六七八九十]\s*、"),
                re.compile(r"(?m)^[ \t]*第[二2]部分")):
        m = pat.search(seg)
        if m and m.start() < cut:
            cut = m.start()
    seg = seg[:cut]
    # 分值：题干第一行里的（N分）/（共N分）
    first_line = seg.split("\n")[0]
    m = re.search(r"[（(]\s*(?:共)?\s*(\d+)\s*分\s*[）)]", first_line)
    score = int(m.group(1)) if m else None
    if m:
        cut = m.end()
        seg = (seg[:m.start()] + seg[cut:]).lstrip(" \n")
    # 选项（可能一行多个 A.… B.…）
    opts = []
    for om in OPT_LINE.finditer(seg):
        opts.append({"id": om.group(1).lower(), "label": om.group(2).strip()})
    if len(opts) >= 2:
        head = seg[:OPT_LINE.search(seg).start()].strip()
        merged = {o["id"]: o["label"] for o in opts}
        # 同行多选项：A.xxx B.yyy
        flat = " ".join(merged.values())
        for mm in re.finditer(r"([A-D])[．.、]\s*", " ".join(f"{k.upper()}.{v}" for k, v in merged.items())):
            pass
        # 在原始文本里按字母重新切一遍，保证同行选项也拆开
        seg_opts = seg[OPT_LINE.search(seg).start():]
        parts = [(mm.start(), mm.group(1).lower()) for mm in re.finditer(r"(?:^|\s)([A-D])[．.、]", seg_opts)]
        parsed = {}
        for i, (pos, k) in enumerate(parts):
            endp = parts[i + 1][0] if i + 1 < len(parts) else len(seg_opts)
            txt = re.sub(r"^\s*[A-D][．.、]\s*", "", seg_opts[pos:endp]).strip()
            parsed.setdefault(k, txt)
        if len([v for v in parsed.values() if v]) >= 2:
            merged = parsed
        ordered = [{"id": k, "label": merged.get(k, "")} for k in "abcd"]
        return {"id": qno, "questionType": "single", "content": head, "options": ordered, "score": score}
    return {"id": qno, "questionType": "text", "content": seg.strip(), "options": [], "score": score}


OPT_PROMPT = r"""下面是一道语文选择题的原文片段（选项可能是两栏排版：A、B 在同一行，下一行分别是 A、B 的续行）。

请把 A、B、C、D 四个选项**完整还原**（把属于该选项的续行也并进去），逐字照抄，不要改写、不要补内容：

{"a":"...","b":"...","c":"...","d":"..."}

要求：不要带 A．/B． 前缀；每项只写该选项自己的内容（不要把另一个选项的文字带进来）；只输出 JSON。

原文：
"""


DOT_PROMPT = r"""下面是一道语文文言文选择题的原文（来自 Word，⟦D:x⟧ 表示 x 这个字在卷面上加了着重号；选项可能是两栏排版：A、B 在同一行，下一行分别是它们的续行）。

请把 A、B、C、D 四个选项**完整还原**（把属于该选项的续行并进去），逐字照抄：

{"a":"...","b":"...","c":"...","d":"..."}

要求：
1. 不要带 A．/B． 前缀；每个选项只写它自己的文字，不要混入另一个选项。
2. **所有 ⟦D:x⟧ 标记必须原样保留、位置不变**（这是加点标记，不能丢、不能改）。
3. 只输出 JSON。

原文：
"""


def ai_options_marked(seg: str) -> dict[str, str]:
    """用带 ⟦D:x⟧ 的原文让模型还原选项，返回 a-d（标记已转成 <Dot>）"""
    try:
        got = chat([{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": DOT_PROMPT + seg[:4000]}], max_tokens=2500)
    except Exception:
        return {}
    if not isinstance(got, dict):
        return {}
    out = {}
    for k in "abcd":
        v = str(got.get(k) or "").strip()
        if not v:
            return {}
        v = re.sub(r"\s+", " ", v)
        out[k] = re.sub(r"⟦D:(.)⟧", r"<Dot>\1</Dot>", v)
    return out


def looks_broken(q: dict, seg: str) -> bool:
    """选项是否切坏了：有空项 / 含别的选项标号 / 选项区有续行"""
    if q.get("questionType") != "single" or not q.get("options"):
        return False
    labels = {o["id"]: (o.get("label") or "").strip() for o in q["options"]}
    if any(not v for v in labels.values()):
        return True
    for k, v in labels.items():
        for other in "ABCD":
            if other.lower() != k and re.search(rf"(^|\s){other}[．.、]", v):
                return True
    # 选项区里有「不以 A-D 开头、但有字」的行 → 说明有续行没并进去
    m = OPT_LINE.search(seg)
    if m:
        tail = seg[m.start():]
        for ln in tail.split("\n"):
            t = ln.strip()
            if t and not OPT_LINE.match(ln) and not re.match(r"^[A-D][．.、]", t):
                return True
    return False


def ai_options(seg: str) -> dict[str, str]:
    try:
        got = chat([{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": OPT_PROMPT + seg[:4000]}], max_tokens=2000)
    except Exception:
        return {}
    if not isinstance(got, dict):
        return {}
    out = {}
    for k in "abcd":
        v = str(got.get(k) or "").strip()
        if v:
            out[k] = re.sub(r"\s+", " ", v)
    return out if len(out) == 4 else {}


def parse_answers(ans: str) -> dict[int, str]:
    """从答案区抽每题答案：选择题取字母，主观题保留整段"""
    ans = cut_translation_block(ans)
    out: dict[int, str] = {}
    # 「【答案】1. B 2. D / 3\\. A 4. A」这类答案行：一行里出现 ≥2 个「题号+字母」就算答案行
    for line in ans.split("\n"):
        hits = list(re.finditer(r"(?<![\dA-Za-z])(\d{1,2})\s*[.．、]\s*([A-D]{1,4})(?=[\s、,，]|$)", line))
        if len(hits) >= 2:
            for m in hits:
                out.setdefault(int(m.group(1)), m.group(2))
    # 「1．B（3分） 2．C（3分）」——字母在前、分值在后（北京卷教师版最常见版式）
    for m in re.finditer(r"(?<![\dA-Za-z])(\d{1,2})\s*[.．、]\s*([A-D]{1,4})\s*[（(]\s*\d+\s*分", ans):
        out.setdefault(int(m.group(1)), m.group(2))
    # 「1【答案】C」/「1.【答案】C」这类教师版版式
    for m in re.finditer(r"(?m)^\s*(\d{1,2})\s*[.．、]?\s*【答案】\s*([A-D]{1,4})\b", ans):
        out.setdefault(int(m.group(1)), m.group(2))
    # 先抓「1.（3分）C 2.（3分）A」这种挤在一行的选择题答案
    for m in re.finditer(r"(\d{1,2})\s*[、.．]\s*[（(]\s*(\d+)\s*分\s*[）)]\s*([A-D])(?=[\s\d]|$)", ans):
        out[int(m.group(1))] = m.group(3)
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(ans)]
    for i, (s, e, no) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(ans)
        seg = ans[e:nxt].strip()
        if no in out:
            continue
        if re.fullmatch(r"[A-D]", seg.strip()):
            out[no] = seg.strip()
        else:
            # 去掉「【评分参考】…」之后的内容，保留答案要点/示例
            seg = re.split(r"【评分参考】|【评分标准】", seg)[0].strip()
            seg = re.sub(r"^[（(]\s*(?:共)?\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
            out[no] = seg
    return out


def cut_translation_block(txt: str) -> str:
    m = TRANS_RE.search(txt)
    return txt[: m.start()].rstrip() if m else txt


def answer_blocks(ans: str) -> dict[int, str]:
    """答案区里每题整段原文（含可能自带的解析文字）"""
    ans = cut_translation_block(ans)
    out: dict[int, str] = {}
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(ans)]
    for i, (s, e, no) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(ans)
        seg = ans[e:nxt].strip()
        seg = re.sub(r"^[（(]\s*(?:共)?\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
        seg = re.split(r"【评分参考】|【评分标准】", seg)[0].strip()
        # 同一行里后面还跟着别的题的答案（如「1.（3分）C 2.（3分）A」）→ 截断
        m = re.search(r"(?<![0-9])\d{1,2}\s*[、.．]\s*[（(]?\s*\d+\s*分", seg)
        if m:
            seg = seg[: m.start()].strip()
        out[no] = seg
    return out


def raw_answer(ans: str, qno: int) -> str:
    """答案区里第 qno 题的原文（**不**在第一个【评分参考】处截断，名著要按小题再拆）"""
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(ans)]
    for i, (s, e, no) in enumerate(marks):
        if no != qno:
            continue
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(ans)
        seg = ans[e:nxt].strip()
        seg = re.sub(r"^[（(]\s*(?:共)?\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
        return seg
    return ""


SUBQ_RE = re.compile(r"(?m)^[ \t]*[（(]\s*(\d{1,2})\s*[）)][ \t]*")

# 「根据要求，回答问题。」这类只有指令、没有实际内容的引导语（名著的空题干）
LEADIN_RES = (
    re.compile(r"^(?:根据|按)[^。\n]{0,12}(?:要求|提示)[，,]?[^。\n]{0,12}(?:回答|完成|作答)[^。\n]{0,20}[。.]?$"),
    re.compile(r"^(?:阅读|依据|结合|请)[^。\n]{0,24}(?:回答|完成|作答)[^。\n]{0,16}(?:问题|各题|题目)[^。\n]{0,10}[。.]?$"),
    re.compile(r"^[^。\n]{0,20}(?:任选|选择)[^。\n]{0,10}(?:一题|其一)[^。\n]{0,10}作答[^。\n]{0,10}[。.]?$"),
)

PIECE_SCORE_RE = re.compile(r"[（(]\s*(?:共)?\s*(\d+)\s*分\s*[）)]")


def strip_leadin(text: str) -> str:
    """去掉题干开头的「根据要求，回答问题。」之类空引导语（可连续多行）"""
    t = (text or "").strip()
    while True:
        lines = t.split("\n")
        first = lines[0].strip() if lines else ""
        if not first or not any(p.match(first) for p in LEADIN_RES):
            return t.strip()
        rest = "\n".join(lines[1:]).strip()
        if not rest:  # 整题只有这句指令 → 保留，别把题干清空
            return t.strip()
        t = rest


def split_piece_options(piece: str) -> tuple[str, list[dict]]:
    """小题里若带 A/B/C/D 选项，切出 (题干, 选项)；否则返回 (piece, [])"""
    m = OPT_LINE.search(piece)
    if not m:
        return piece.strip(), []
    head = piece[:m.start()].strip()
    body = piece[m.start():]
    parts = [(mm.start(), mm.group(1).lower()) for mm in re.finditer(r"(?:^|\s)([A-D])[．.、]", body)]
    if len(parts) < 2:
        return piece.strip(), []
    parsed: dict[str, str] = {}
    for i, (pos, k) in enumerate(parts):
        endp = parts[i + 1][0] if i + 1 < len(parts) else len(body)
        parsed.setdefault(k, re.sub(r"^\s*[A-D][．.、]\s*", "", body[pos:endp]).strip())
    ordered = [{"id": k, "label": parsed.get(k, "")} for k in "abcd"]
    if sum(1 for o in ordered if o["label"]) < 2:
        return piece.strip(), []
    return head, ordered


def split_named_book(content: str, ans_raw: str, total_score: int, qno, review: list[str], fallback_answer: str = ""):
    """名著：材料单独成 content，多个「(1)(2)…」拆成多道小题（每道一个答题框）

    返回 (material, questions)
    """
    text = strip_leadin(content)
    marks = [(m.start(), m.end()) for m in SUBQ_RE.finditer(text)]
    pieces: list[str] = []
    material = ""
    if marks:
        material = strip_leadin(text[:marks[0][0]])
        for i, (_s, e) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            pieces.append(text[e:end].strip())
    else:
        pieces = [text]

    ans_map: dict[int, str] = {}
    if ans_raw:
        amarks = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ_RE.finditer(ans_raw)]
        for i, (_s, e, n) in enumerate(amarks):
            end = amarks[i + 1][0] if i + 1 < len(amarks) else len(ans_raw)
            seg = ans_raw[e:end].strip()
            seg = re.sub(r"^[（(]\s*(?:共)?\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
            seg = re.split(r"【评分参考】|【评分标准】", seg)[0].strip()
            ans_map[n] = seg
        if len(pieces) > 1 and len(ans_map) != len(pieces):
            review.append(f"名著：小题数 {len(pieces)} 与答案分段 {len(ans_map)} 不一致")

    questions = []
    scores: list[int] = []
    for idx, piece in enumerate(pieces, start=1):
        m = PIECE_SCORE_RE.search(piece)
        score = int(m.group(1)) if m else 0
        if m:
            piece = (piece[:m.start()] + piece[m.end():]).strip()
        stem, opts = split_piece_options(piece)
        if not opts:                                   # 表格/两栏形态：用通用内联选项拆分再试
            stem2, opts2 = split_inline_options(piece)
            if opts2:
                stem, opts = stem2, opts2
        q = {
            "id": f"{qno}-{idx}" if len(pieces) > 1 else qno,
            "questionType": "single" if opts else "text",
            "content": stem,
            "options": opts,
            "answer": ans_map.get(idx) or (fallback_answer if len(pieces) == 1 else ""),
            "analysis": "",
            "score": score,
        }
        questions.append(q)
        scores.append(score)

    # 分值兜底：小题没标分 → 总分给第一题；部分缺 → 差额补最后一题
    if questions and total_score:
        s = sum(scores)
        if s == 0:
            questions[0]["score"] = total_score
        elif s < total_score:
            questions[-1]["score"] += total_score - s
    return material, questions


AUTHOR_RE = re.compile(r"^\\?\[?\s*([唐宋元明清五代先秦两汉魏晋南北朝]{1,6})\s*\\?\]?\s*[·•]?\s*([^\s，。；]{2,10})$")
SOURCE_RE = re.compile(r"^[（(]\s*取材于[^）)]*[）)]$")


NOTE_RE = re.compile(r"【(\d{1,2})】\s*([^【]*)")
SUP_NOTE_RE = re.compile(r"<sup>\s*【(\d{1,2})】\s*</sup>")


def notes_to_footnotes(article: str) -> str:
    """把「注释：【1】…【2】…」段落转成 GFM 脚注（标题/正文里的 <sup>【N】</sup> → [^N]）"""
    lines = article.split("\n")
    note_idx = next((i for i, l in enumerate(lines) if re.match(r"^\s*注释[：:]", l)), None)
    notes: dict[str, str] = {}
    if note_idx is not None:
        raw = " ".join(l.strip() for l in lines[note_idx:])
        raw = re.sub(r"^\s*注释[：:]\s*", "", raw)
        for m in NOTE_RE.finditer(raw):
            notes.setdefault(m.group(1), m.group(2).strip().rstrip("。") + "。")
    body = "\n".join(lines[:note_idx]) if note_idx is not None else article
    body = SUP_NOTE_RE.sub(lambda m: f"[^{m.group(1)}]", body)
    # 正文里没被引用的脚注编号也保留定义（GFM 允许）
    if notes:
        defs = "\n".join(f"[^{k}]: {v}" for k, v in sorted(notes.items(), key=lambda x: int(x[0])))
        body = body.rstrip() + "\n\n" + defs
    return body


IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")


def _ensure_png(p: Path) -> Path | None:
    """wmf/emf/tiff 之类转 png（浏览器/nginx 只认 png/jpg/jpeg/gif/webp）"""
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return p if p.exists() else None
    dst = p.with_suffix(".png")
    if dst.exists():
        return dst
    try:
        import subprocess
        if p.suffix.lower() == ".wmf":
            from convert_chemistry_md import wmf_to_png  # 复用化学的 wmf2gd 逻辑
            wmf_to_png([p])
        else:
            subprocess.run(["convert", str(p), "-background", "white", "-alpha", "remove", "-alpha", "off", str(dst)],
                           capture_output=True, timeout=120)
    except Exception:
        return None
    return dst if dst.exists() else None


def rewrite_images(text: str, md_dir: Path, stem: str, type_dir: str, review: list[str], subject: str = "语文") -> str:
    """把 Markdown 里的相对图片引用改成 nginx 可直接服务的 /exam-images/… 绝对路径"""
    if not text or "![" not in text:
        return text

    dst_dir = md_dir.parent.parent / subject / stem / "images"   # output_docx_to_md/<类型>/<学科>/<卷>/images

    def repl(m):
        ref = m.group(1)
        src = md_dir / ref
        png = _ensure_png(src)
        if png is None:
            review.append(f"图片缺失：{ref}")
            return m.group(0)
        # MD 目录名带「（教师版）」，而入库后的卷名不带 → 复制到按卷名命名的目录，nginx 才服务得到
        try:
            import shutil
            dst_dir.mkdir(parents=True, exist_ok=True)
            if not (dst_dir / png.name).exists():
                shutil.copyfile(png, dst_dir / png.name)
        except Exception as e:
            review.append(f"图片复制失败：{png.name} {str(e)[:40]}")
        return f"![]({'/exam-images/' + type_dir + '/' + subject + '/' + stem + '/images/' + png.name})"

    return IMG_REF.sub(repl, text)


SECTION_LABELS = {"语言基础运用", "语用", "微写作", "作文", "名著", "默写",
                  "文言文阅读", "古诗鉴赏", "多文本阅读", "文学类阅读"}


def strip_section_label(text: str) -> str:
    """去掉题面开头单独成行的题型名（如「语言基础运用」「微写作」「作文」），
    题型已经由题组名/选项卡体现，不必在正文里再写一遍。"""
    if not text:
        return text
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.sub(r"[\s*#]", "", lines[i].strip()) in SECTION_LABELS:
        rest = "\n".join(lines[i + 1:]).lstrip("\n")
        if rest.strip():
            return rest
    return text


CI_PATTERNS = ("念奴娇", "水调歌头", "沁园春", "满江红", "蝶恋花", "青玉案", "破阵子", "临江仙",
                "渔家傲", "江城子", "卜算子", "如梦令", "相见欢", "苏幕遮", "一剪梅", "声声慢",
                "雨霖铃", "定风波", "鹧鸪天", "踏莎行", "点绛唇", "忆江南", "长相思", "虞美人",
                "浣溪沙", "菩萨蛮", "醉花阴", "扬州慢", "望海潮", "桂枝香", "贺新郎", "木兰花")


def guess_poetry_kind(g: dict, title: str) -> str:
    """"shi"（诗，居中）还是 "ci"（词，按普通文段居左）"""
    kind = (g.get("poetry") or "").strip().lower()
    if kind in ("shi", "ci"):
        return kind
    intro = (g.get("intro") or "") + " " + (title or "")
    if "这首词" in intro or "本词" in intro or "该词" in intro:
        return "ci"
    if any(k in intro for k in CI_PATTERNS):
        return "ci"
    if "这首诗" in intro or "本诗" in intro or "这首诗歌" in intro:
        return "shi"
    return "shi"


TRANS_RE = re.compile(r"(?m)^[ \t]*附\s*[：:]?\s*(?:文言文)?参考译文[ \t]*$")


def extract_translation(txt: str) -> str:
    """取「附：文言文参考译文」之后到文末的译文（没有就返回空串）"""
    m = TRANS_RE.search(txt)
    if not m:
        m = re.search(r"(?m)^[ \t]*(?:文言文)?参考译文[ \t]*$", txt)
    if not m:
        return ""
    seg = txt[m.end():].strip()
    seg = re.sub(r"\n{3,}", "\n\n", seg)
    return seg


def finalize_article(article: str, category: str, poetry_kind: str = "shi") -> str:
    """标题→h3；作者行居中（诗词整段 <Center>）；「（取材于…）」→ <RightAlign>"""
    if not article.strip():
        return article
    lines = article.split("\n")
    ti = next((i for i, l in enumerate(lines) if l.strip().startswith("### ")), None)

    # 作者行：标题后第一个非空行
    ai, author = None, None
    if ti is not None:
        for j in range(ti + 1, min(len(lines), ti + 4)):
            t = lines[j].strip()
            if not t:
                continue
            m = AUTHOR_RE.match(t)
            if m and len(t) <= 16:
                ai, author = j, f"{m.group(1)}·{m.group(2)}"
            break

    if category == "古诗" and ti is not None and poetry_kind == "ci":
        # 词：按普通文段居左排版，作者单独一行，不套 <Center>
        body = [l for k, l in enumerate(lines[ti + 1:], start=ti + 1) if k != ai]
        blocks = [lines[ti]]
        if author:
            # 词：作者仍居中，词句按普通文段居左
            blocks.append(f"<Center>{author}</Center>")
        blocks += [l for l in body if l.strip()]
        article = "\n\n".join(blocks)
    elif category == "古诗" and ti is not None:
        body = [l for k, l in enumerate(lines[ti + 1:], start=ti + 1) if k != ai]
        note_at = next((k for k, l in enumerate(body) if re.match(r"^\s*(注释[：:]|【1】)", l)), len(body))
        poem = [l.strip() for l in body[:note_at] if l.strip()]
        notes = [l for l in body[note_at:] if l.strip()]
        head = lines[ti]
        if author:
            poem = [author] + poem
        out = [head, "", "<Center>", ""]
        out += poem[:1]
        for l in poem[1:]:
            out += ["", l]
        out += ["", "</Center>"]
        if notes:
            out += [""] + notes
        article = "\n".join(out)
    elif author is not None:
        lines[ai] = f"<Center>{author}</Center>"
        article = "\n".join(lines)

    # 出处行「（取材于…）」右对齐
    lines = article.split("\n")
    for i, l in enumerate(lines):
        t = l.strip()
        if SOURCE_RE.match(t):
            lines[i] = f"<RightAlign>{t}</RightAlign>"
    article = "\n".join(lines)
    article = re.sub(r"\n{3,}", "\n\n", article)
    return article.strip()


CHOICE_ANALYSIS_PROMPT = r"""下面是若干道语文选择题（含题干、四个选项、正确答案）。
请为**每一道**写一段解析（60-150 字，中文，专业简洁）：

- 先说正确答案为什么正确（语文题尽量点出原文依据/关键字句/手法）；
- 再说明其余选项错在哪里（各一句话即可）；
- 不要复述题干，不要输出与答案无关的内容。

只输出 JSON 对象，键是题号（字符串），值是该题解析文字：
{"1":"……","2":"……"}

题目：
"""


def ai_choice_analyses(items: list[dict]) -> dict[int, str]:
    """items: [{id, content, options, answer}] → {id: 解析}"""
    if not items:
        return {}
    payload = []
    index_map: dict[int, object] = {}
    for idx, q in enumerate(items, start=1):
        index_map[idx] = q["id"]
        opts = "\n".join(f"{o['id'].upper()}．{o['label']}" for o in q.get("options") or [] if str(o.get("label", "")).strip())
        payload.append(f"【{idx}】\n{q['content']}\n{opts}\n正确答案：{q.get('answer','')}")
    try:
        got = chat([{"role": "system", "content": "你是北京高考语文阅卷老师，只输出 JSON。"},
                    {"role": "user", "content": CHOICE_ANALYSIS_PROMPT + "\n\n".join(payload)[:12000]}],
                   max_tokens=6000)
    except Exception as e:
        log(f"    AI 生成选择题解析失败：{str(e)[:80]}")
        return {}
    if not isinstance(got, dict):
        return {}
    out = {}
    for k, v in got.items():
        try:
            idx = int(re.sub(r"[^0-9]", "", str(k)))
        except Exception:
            continue
        if idx in index_map and str(v).strip():
            out[index_map[idx]] = str(v).strip()
    return out


ANSWER_FIX_PROMPT = r"""下面是一份语文试卷的【参考答案/解析】原文，以及若干「目前没取到答案」的题号与题干。
请从答案原文里为这些题补出答案：

- 选择题：只给选项字母（多选连写，如 "BD"）；
- 主观题：摘录答案要点/示例（不要写“见解析”这类空话，尽量原文摘录）；
- 实在没有的题不要编，跳过。

只输出 JSON 对象：{"题号":"答案"}
题号与题干：
{items}

答案原文：
"""


def ai_fill_answers(items: list[dict], ans_text: str, wanted: list) -> dict:
    if not items or not ans_text.strip():
        return {}
    lines = []
    for q in items:
        stem = re.sub(r"\s+", " ", str(q.get("content") or ""))[:120]
        lines.append(f"- {q['id']}｜{stem}")
    try:
        got = chat([{"role": "system", "content": "你是语文阅卷老师，只输出 JSON。"},
                    {"role": "user", "content": ANSWER_FIX_PROMPT.format(items="\n".join(lines)[:6000])
                     + ans_text[:12000]}], max_tokens=4000)
    except Exception:
        return {}
    if not isinstance(got, dict):
        return {}
    out = {}
    for k, v in got.items():
        rid = str(k).strip()
        val = str(v).strip()
        if not val:
            continue
        out[rid] = val
    return out


def build_group(g: dict, body: str, answers: dict[int, str], review: list[str], dots=None, ans_raw: str = "", blocks: dict | None = None,
                md_dir: Path | None = None, stem_name: str = "", type_dir: str = "二模",
                pdf_path: Path | None = None, allow_vision: bool = True) -> dict | None:
    blocks = blocks or {}
    cat = g.get("category", "")
    qtype = g.get("questionType") or "chinese-reading"
    nos = [int(x) for x in (g.get("questions") or [])]
    if not nos:
        return None
    # 文章：按锚点截原文；锚点失效就退回「引导语之后 ~ 第一题之前」
    article = ""
    want_article = qtype == "chinese-reading"
    if want_article:
        start = None
        intro = (g.get("intro") or "").strip()
        if intro:
            p0 = body.find(intro)
            if p0 >= 0:
                nl = body.find("\n", p0)
                start = (nl + 1) if nl >= 0 else (p0 + len(intro))
        if start is None and g.get("article_start"):
            a = find_anchor(body, g["article_start"])
            if a >= 0:
                start = body.rfind("\n", 0, a) + 1
        # 结束：本题组第一题的题号位置
        qpos = None
        if start is not None:
            # 老卷（尤其真题）常把「1．（24分）阅读下面材料…」写在题号行里，题号位次在引导语之前；
            # 所以取「start 之后的第一处题号」当材料右边界，比「题号 == 首题号」稳。
            qpos = next((m.start() for m in QSPLIT.finditer(body) if m.start() > start), None)
        if qpos is None:
            qpos = next((m.start() for m in QSPLIT.finditer(body) if int(m.group(1)) == nos[0]), None)
        if start is not None and qpos is not None and qpos > start:
            article = body[start:qpos].strip()
        # 锚点校验（只提示，不影响结果）
        ast = (g.get("article_start") or "").strip()
        if ast and ast not in article:
            a = find_anchor(body, ast)
            if a < 0:
                review.append(f"{cat}：锚点没对上（start={ast[:20]!r}）")
    article = re.sub(r"(?m)^(材料[一二三四])\s*$", r"### \1", article)
    # 首行若是「标题」（短、不以句末标点结尾、不是 ①② 或「材料一」）→ 升成 Markdown 标题
    lines = [ln for ln in article.split("\n")]
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is not None:
        first = lines[idx].strip()
        rest = "".join(lines[idx + 1:])
        is_title = (len(first) <= 40 and len(rest) >= 80
                    and not re.match(r"^(材料[一二三四]|#|①|②|[（(])", first)
                    and not re.search(r"[。！？；]$", first))
        if is_title:
            # 标题里的注释角标保持普通上标（不做脚注跳转）
            clean = re.sub(r"【(\d{1,2})】", lambda m: f"<sup>【{m.group(1)}】</sup>", first)
            clean = re.sub(r"<sup>\s*<sup>(【\d+】)</sup>\s*</sup>", r"<sup>\1</sup>", clean).strip()
            lines[idx] = "### " + clean
            article = "\n".join(lines)
    # 题目
    qs = []
    for no in nos:
        q = parse_question(body, no)
        if not q:
            review.append(f"缺第{no}题正文")
            continue
        q["answer"] = SCAN["letters"].get(no) or answers.get(no, "")
        q["analysis"] = SCAN["rest"].get(no, "")
        if q["questionType"] == "single":
            # 已有干净字母就别动；否则用整块重切，仍抽不出字母才判定为解析
            cur = str(q.get("answer") or "").strip()
            if not re.fullmatch(r"[A-D]{1,4}", cur):
                blk = raw_answer(ans_raw, no)
                letter, rest = extract_choice_answer(blk)
                if letter:
                    q["answer"] = letter
                    if rest and len(rest) >= 8 and not str(q.get("analysis") or "").strip():
                        q["analysis"] = rest
                elif len(blk) > 20:
                    q["answer"] = ""
                    if not str(q.get("analysis") or "").strip():
                        q["analysis"] = blk
                    review.append(f"第{no}题：答案区块是解析（抽不出字母），已移入 analysis，答案留空待补")
        # 选择题：答案区若自带解析（不只是字母），直接抄过来
        raw_blk = blocks.get(no, "")
        if q["questionType"] == "single" and raw_blk:
            rest = re.sub(r"^\s*[A-D][\s、，,]*", "", raw_blk).strip()
            rest = re.sub(r"^[A-D]$", "", rest).strip()
            if len(rest) >= 12:
                q["analysis"] = rest
        q["score"] = q.get("score") or 0
        # 选项两栏/带续行时，纯正则切不干净 → 让模型把每个选项整段还原
        if q["questionType"] == "text":
            stem2, opts2 = split_inline_options(q.get("content") or "")
            if opts2 and len(opts2) >= 4:
                ids2 = [o["id"] for o in opts2]
                ans2 = re.sub(r"[^A-D]", "", str(q.get("answer") or "").strip().upper())[:4]
                if ans2 and all(c in ids2 for c in ans2):
                    q.update(questionType="single", content=stem2, options=opts2)
                    review.append(f"第{no}题：题干内联的选项已切出（{''.join(ids2)}），改为选择题")
                elif not ans2:
                    q.update(questionType="single", content=stem2, options=opts2)
                    review.append(f"第{no}题：题干内联的选项已切出（{''.join(ids2)}），改为选择题（答案待补）")
                else:
                    review.append(f"第{no}题：题干里有内联选项但答案 {ans2!r} 不在其中 → 保持 text，请人工核对")
        if q["questionType"] == "text" and len(re.findall(r"(?:^|\s)([A-D])[.．、]", q.get("content") or "")) >= 3:
            stem2, opts2 = split_piece_options(q["content"])
            if opts2:
                q.update(questionType="single", content=stem2, options=opts2)
                review.append(f"第{no}题：题干里混排的 A–D 已切回选项")
            elif allow_vision and pdf_path:
                got = vision_question_from_pdf(pdf_path, no, review,
                                               (OUT["root"] / type_dir / ".cache" / f"{stem_name}-qvision.json"))
                if got:
                    q["content"] = str(got.get("stem") or q["content"]).strip()
                    q["options"] = [{"id": str(o.get("id") or "ABCD"[k]).upper()[:1],
                                     "label": str(o.get("label") or "").strip()}
                                    for k, o in enumerate(got["options"][:4])]
                    q["questionType"] = "single"
                    if got.get("answer"):
                        q["answer"] = str(got["answer"]).strip().upper()[:1]
        if q["questionType"] == "single":
            seg_q = _question_segment(body, no)
            # 选项 label 为空但该题片段里有图片 → 图片就是选项本身（老卷选项被截成图）
            if any(not str(o.get("label") or "").strip() for o in q["options"]):
                imgs = IMG_REF.findall(seg_q)
                if imgs:
                    for k, o in enumerate(q["options"]):
                        if not str(o.get("label") or "").strip() and k < len(imgs):
                            o["label"] = f"![]({imgs[k]})"
                    review.append(f"第{no}题：选项是图片，已把图片引用挂到对应选项 label")
            if looks_broken(q, seg_q):
                fixed = ai_options(seg_q) if AI_ENABLED.get("on") else {}
                if fixed:
                    q["options"] = [{"id": k, "label": fixed[k]} for k in "abcd"]
                elif AI_ENABLED.get("on"):
                    review.append(f"第{no}题选项切分可疑且 AI 未能还原")
                else:
                    review.append(f"第{no}题选项切分可疑（--no-ai 未调模型还原），请人工核对")
        if not q["answer"]:
            review.append(f"第{no}题缺答案")
        if dots is not None and q["questionType"] == "single":
            q["content"] = dots.annotate_fragments(q["content"])
            for o in q["options"]:
                o["label"] = dots.annotate_fragments(o["label"])
        qs.append(q)

    if qtype == "chinese-dictation" and qs:
        # 默写：题干里每个空 → <Input2 />，每个空一道 input 小题（答案按空对）
        q0 = qs[0]
        content, qs = build_dictation(q0["content"], q0["answer"], q0["id"], review,
                                      max_blanks=q0.get("score") or None)
        article = content

    if qtype in ("chinese-micro-writing", "chinese-essay") and qs:
        # 作文/微写作按「直接给题目」渲染，不要阅读框；题干里已经有完整要求
        article = ""

    # 旧卷（2016 及以前）：一个大题（1 个题号）里套 (1)(2)… 若干小题 → 拆开
    if cat in ("多文本", "文言文", "古诗", "文学类", "名著", "语用") and qs:
        seg_raw0 = _question_segment(body, nos[0])
        if len(qs) == 1 and len(SUBQ_RE.findall(seg_raw0)) >= 2:
            material, sub_qs = split_named_book(seg_raw0, raw_answer(ans_raw, nos[0]),
                                                qs[0].get("score") or 0, nos[0], review,
                                                qs[0].get("answer") or "")
            if len(sub_qs) > 1:
                article = material or article
                qs = sub_qs
                review.append(f"{cat}：旧卷小题拆分为 {len(qs)} 题")
    if cat == "语用" and qs:
        # 语用：题干和选项是同一段原文里的 (1)(2) 两小问，(2) 常被“选项解析”截掉，
        # 所以这里回到原文片段里重新按小题切
        seg_raw = _question_segment(body, nos[0])
        if len(SUBQ_RE.findall(seg_raw)) >= 2:
            material, sub_qs = split_named_book(seg_raw, raw_answer(ans_raw, nos[0]),
                                                qs[0].get("score") or 0, nos[0], review,
                                                qs[0].get("answer") or "")
            article = re.sub(r"[（(]\s*共?\s*\d+\s*分\s*[）)]", "", material or "").strip()
            qs = sub_qs
            if len(qs) > 1:
                review.append(f"语用：已拆成 {len(qs)} 道小题")
    need_split = cat == "名著" or (cat == "语用" and qs and len(SUBQ_RE.findall(qs[0]["content"])) >= 2)
    if need_split and qs:
        # 名著/语用：材料（判词、语段）单独作题面，多个「(1)(2)…」拆成多道小题
        # （语用的 (1) 带选项 → 选择题；(2) 是长主观题 → text）
        # 注意：必须从原文片段里切（含材料），否则会丢掉材料那段文字
        seg_raw = _question_segment(body, nos[0])
        material, sub_qs = split_named_book(seg_raw, raw_answer(ans_raw, nos[0]),
                                            qs[0].get("score") or 0, nos[0], review,
                                            qs[0].get("answer") or "")
        if material:
            article = material
        if sub_qs:
            qs = sub_qs
        if len(qs) > 1:
            review.append(f"{cat}：已拆成 {len(qs)} 道小题")

    total = sum(q["score"] for q in qs)
    if total == 0 and nos:
        # 旧卷/新卷都可能只在「一、本大题共 N 小题，共 X 分」里给总分
        for m in SEC_SPLIT.finditer(body):
            # 找到包含本题号的那一段
            seg_start = m.start()
            nxt = body.find("、", m.start())  # 仅用于定位，无实义
            if seg_start <= (next((mm.start() for mm in QSPLIT.finditer(body) if int(mm.group(1)) == nos[0]), 0)):
                total = int(m.group(3))
        if total and qs:
            per = max(1, total // max(1, len(qs)))
            for q in qs:
                q["score"] = q["score"] or per
    article = finalize_article(article, cat, guess_poetry_kind(g, (g.get("title") or "")) if cat == "古诗" else "shi")
    if dots is not None and article:
        keep = []
        for ln in article.split("\n"):
            t = ln.strip()
            if (not t) or t.startswith(("#", "<")) or t.startswith("注释") or re.match(r"^【\d+】", t):
                keep.append(ln)
            else:
                keep.append(dots.annotate_fragments(ln))
        article = "\n".join(keep)
    article = strip_section_label(article)
    for q in qs:
        q["content"] = strip_section_label(q["content"])
    if md_dir is not None:
        article = rewrite_images(article, md_dir, stem_name, type_dir, review)
        for q in qs:
            q["content"] = rewrite_images(q["content"], md_dir, stem_name, type_dir, review)
            for o in q["options"]:
                o["label"] = rewrite_images(str(o["label"]), md_dir, stem_name, type_dir, review)
    intro = (g.get("intro") or "").strip()
    article = strip_residue(article, intro)
    # 材料末尾残留的分值括号（分值已由 section.score 表达）
    article = re.sub(r"[（(]\s*共?\s*\d+\s*分\s*[）)]\s*$", "", article).strip()
    for q in qs:
        q["content"] = strip_residue(q["content"])
    if qtype != "chinese-dictation":
        article = tidy_blank_glyphs(article)
        for q in qs:
            q["content"] = tidy_blank_glyphs(q["content"])
            if str(q.get("answer") or "").strip():
                q["answer"] = q["answer"].replace("\\_", "_")
    return {
        "type": cat, "category": cat, "questionType": qtype,
        "title": g.get("title") or f"{cat}阅读",
        "intro": intro,
        "content": article, "score": total, "questions": qs,
    }


OUT = {"root": OUT_ROOT}
AI_ENABLED = {"on": True}
SCAN = {"letters": {}, "rest": {}}          # 当前卷的「答案区逐行扫描」结果（process 里填充）          # --no-ai 时置 False，保证「零模型调用、输出可复现」


# ══════════════════════════════════════════════════════════════════
# 出口后处理（原 4 个后处理脚本的等价逻辑，一次做完）
# ══════════════════════════════════════════════════════════════════
def merge_sections(sections: list[dict], review: list[str]) -> list[dict]:
    """← merge_chinese_groups.py：同 category 的重复 section 合并（AI 规划偶尔把同一题型拆 2-3 组）。

    题目按原顺序拼接；content 取第一个非空；score 重算为小题合计。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for sec in sections:
        cat = sec.get("category") or ""
        if cat not in merged:
            merged[cat] = dict(sec)
            merged[cat]["questions"] = list(sec.get("questions") or [])
            order.append(cat)
        else:
            m = merged[cat]
            m["questions"].extend(sec.get("questions") or [])
            if not (m.get("content") or "").strip() and (sec.get("content") or "").strip():
                m["content"] = sec["content"]
            review.append(f"合并重复板块「{cat}」：多出的一组 {len(sec.get('questions') or [])} 题已并入")
    if len(order) == len(sections):
        return sections
    # 按板块标准顺序重排（阅读类保持原顺序）
    out = [merged[c] for c in order]
    for sec in out:
        sec["score"] = sum(q.get("score") or 0 for q in sec["questions"])
    return out


def _largest_remainder(weights: list[float], total: int) -> list[int]:
    """按权重把 total 分给各项（最大余数法，保证和恰好等于 total、每项 ≥1）。"""
    n = len(weights)
    if n == 0:
        return []
    s = sum(weights) or n
    raw = [w * total / s for w in weights]
    out = [max(1, int(x)) for x in raw]
    diff = total - sum(out)
    if diff > 0:
        order = sorted(range(n), key=lambda i: -(raw[i] - int(raw[i])))
        for k in range(diff):
            out[order[k % n]] += 1
    elif diff < 0:
        order = sorted(range(n), key=lambda i: raw[i] - int(raw[i]))
        for k in range(-diff):
            i = order[k % n]
            if out[i] > 1:
                out[i] -= 1
    return out


SEC_HDR_LINE = re.compile(r"^\s*\**\s*[一二三四五六七八九十]\s*、\s*本大题共[^\n]*分[^\n]*\**\s*$")


def strip_answer_headers(text: str) -> str:
    """答案里残留的分段标题（**二、本大题共3小题，共23分．**）一律剥掉，保留其后正文。"""
    if not text:
        return text
    lines = [l for l in str(text).split("\n") if not SEC_HDR_LINE.match(l.strip())]
    return "\n".join(lines).strip()


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def normalize_scores(sections: list[dict], review: list[str], target: int = TOTAL_SCORE,
                     year: int | None = None) -> None:
    """按【题型】分配合格的单题分值（2026-11 修正：老卷按大题给总分，逐题分常被算歪）。

    - 选择题（single）2–6 分（多选可到 6）；主观题（text）2–9 分；默写 1 分/空；
      微写作 10；作文 40–62（以卷面为准）；
    - 板块内：先保合理卷面分，超区间的拉回区间，**差额记到最后一个主观题**（不塞第一题）；
    - 整卷合计补到 150（先动阅读板块，再动作文）。
    """
    dflt = {"作文": 60 if (year or 9999) <= 2013 else 50, "微写作": 10}
    for sec in sections:
        cat = sec.get("category") or ""
        qs = sec.get("questions") or []
        if not qs:
            sec["score"] = 0
            continue
        if cat == "默写":
            for q in qs:
                q["score"] = 1
            sec["score"] = len(qs)
            continue
        if cat in ("作文", "微写作"):
            for q in qs:
                q["score"] = dflt[cat]
            sec["score"] = sum(q["score"] for q in qs)
            continue
        # 阅读板块：按题型给区间
        for q in qs:
            if q.get("options"):
                q["score"] = _clamp(q.get("score") or 3, 2, 6)
            else:
                q["score"] = _clamp(q.get("score") or 5, 2, 9)
        sec["score"] = sum(q["score"] for q in qs)

    total = sum(sec.get("score") or 0 for sec in sections)
    diff = target - total
    if diff:
        cands = [sec for sec in sections if (sec.get("category") or "") in DEFAULT_SCORES
                 and (sec.get("category") or "") not in ("作文", "微写作", "默写")]
        for sec in reversed(cands):
            qs = sec.get("questions") or []
            texts = [q for q in qs if not q.get("options")] or qs
            if not texts or not diff:
                continue
            q = texts[-1]
            lo = 2
            new = _clamp((q.get("score") or lo) + diff, lo, 9)
            diff -= (new - (q.get("score") or lo))
            q["score"] = new
            sec["score"] = sum(x.get("score") or 0 for x in qs)
        if diff:                                  # 还差 → 动作文
            comp = next((x for x in sections if x.get("category") == "作文" and x.get("questions")), None)
            if comp:
                q = comp["questions"][-1]
                q["score"] = _clamp((q.get("score") or 50) + diff, 40, 62)
                comp["score"] = sum(x.get("score") or 0 for x in comp["questions"])
                diff = target - sum(sec.get("score") or 0 for sec in sections)
    review.append(f"分值分配：按题型给定（选择 2–6 / 主观 2–9 / 默写 1 分每空），合计 "
                  f"{sum(sec.get('score') or 0 for sec in sections)} 分")


def normalize_choice_answers(sections: list[dict], review: list[str]) -> int:
    """选择题答案只取字母；教师版把解析写在答案里（「A(B 错…)」/「【答案】C」/「故选：D」）时拆到 analysis。"""
    n = filled = 0
    for sec in sections:
        for q in sec["questions"]:
            opts = q.get("options") or []
            if not opts:
                continue
            ids = [str(o.get("id") or "").upper() for o in opts]
            ans = str(q.get("answer") or "").strip()
            if not ans:
                continue
            letter, rest = "", ans
            m = re.match(r"^([A-D]{1,4})\s*[.．、)）:：]?\s*(.*)$", ans, re.S)
            if m and m.group(1) in ids:
                letter, rest = m.group(1), m.group(2).strip()
            else:
                m2 = (re.match(r"^[【\[]\s*([A-D])\s*[】\]]", ans)
                      or re.search(r"【答案】\s*([A-D])\b", ans)
                      or re.search(r"故选\s*[:：]?\s*([A-D])\b", ans)
                      or re.search(r"选\s*([A-D])\s*给\s*\d+\s*分", ans)
                      or re.search(r"^[（(]?([A-D])[）)]\s*$", ans))
                if m2 and m2.group(1) in ids:
                    letter, rest = m2.group(1), ans
            if not letter or len(letter) > 1:
                continue
            if ans != letter:
                q["answer"] = letter
                n += 1
            if len(ans) > 200 and not re.search(r"[A-D]", ans[:6]):
                # 整块像题干/纯解析，没有可判定的字母
                if not str(q.get("analysis") or "").strip():
                    q["analysis"] = ans
                q["answer"] = ""
                continue
            if rest and rest != letter and len(rest) >= 8 and not str(q.get("analysis") or "").strip():
                q["analysis"] = rest
                filled += 1
    if n:
        review.append(f"选择题答案清洗：{n} 题从「字母+解析」里只取字母")
    if filled:
        review.append(f"选择题解析：{filled} 题用教师版答案后的原文解析（非 AI 生成）")
    return n


def sanitize_payload(sections: list[dict], review: list[str]) -> int:
    """← sanitize_chinese.py：MDX/KaTeX 安全化（content / analysis / 选项 label）。"""
    try:
        import sanitize_mdx as S
    except Exception as e:                                   # 依赖缺失也不能挡主流程
        review.append(f"MDX 安全化跳过（{str(e)[:40]}）")
        return 0
    S.KEEP_TAGS = tuple(dict.fromkeys(list(S.KEEP_TAGS) + ["InputLong", "AnswerBox"]))
    n = 0

    def fix(t):
        nonlocal n
        new = S.fix(t or "")
        if new != (t or ""):
            n += 1
        return new

    for sec in sections:
        sec["content"] = fix(sec.get("content"))
        for q in sec["questions"]:
            q["content"] = fix(q.get("content"))
            q["analysis"] = fix(q.get("analysis"))
            for o in q.get("options") or []:
                o["label"] = fix(str(o.get("label") or ""))
    if n:
        review.append(f"MDX 安全化：{n} 个字段（< > → $<$/$>$、sub/sup → Unicode、去掉杂标签）")
    return n


def suspicious_table(text: str) -> bool:
    lines = [l for l in (text or "").split("\n") if l.strip().startswith("|")]
    if len(lines) < 3:
        return False
    return len({len(l.strip().strip("|").split("|")) for l in lines}) > 1


def repair_tables(sections: list[dict], pdf: Path, type_dir: str, review: list[str]) -> int:
    """← repair_chinese_tables.py：表格被 Word 弄乱（合并单元格/跨列）时，用 PDF 页面图像重识别。

    只在 `--repair-tables` 时跑（要 pdftoppm + 多模态），默认不动。
    """
    targets = [(sec, q) for sec in sections for q in sec["questions"]
               if suspicious_table(str(q.get("content") or ""))]
    if not targets:
        return 0
    if not pdf or not Path(pdf).exists():
        review.append(f"表格修复跳过：{len(targets)} 处可疑表格但找不到 PDF")
        return 0
    try:
        import tempfile
        import convert_math_pdf as M
        import requests
    except Exception as e:
        review.append(f"表格修复跳过（依赖缺失：{str(e)[:40]}）")
        return 0
    prompt = ("这是语文试卷的一页图像。如果本页有【第 {no} 题】的表格，请把它完整转成 Markdown 表格：\n"
              "1. 保留原文所有文字与填空标记 ①②③④⑤⑥，不要补答案、不要改写；\n"
              "2. 合并单元格用空单元格/重复上级标签如实表达，保持「上级→下级」的层级；\n"
              "3. 只输出 Markdown 表格；本页没有该题的表格就输出空。")
    fixed = 0
    with tempfile.TemporaryDirectory() as td:
        pages = M.render_pages(Path(pdf), 140, Path(td), 8)
        for sec, q in targets:
            got = ""
            for img in pages:
                try:
                    r = requests.post(
                        f"{BASE_URL}/chat/completions",
                        json={"model": MODEL, "max_tokens": 3000, "temperature": 0.0,
                              "reasoning_effort": "none",
                              "messages": [{"role": "system", "content": "你是语文试卷录入员，只输出 Markdown 表格。"},
                                           {"role": "user", "content": [
                                               {"type": "text", "text": prompt.format(no=q.get("id"))},
                                               {"type": "image_url", "image_url":
                                                {"url": "data:image/png;base64," +
                                                        __import__("base64").b64encode(img.read_bytes()).decode()}}]}]},
                        headers={"Authorization": f"Bearer {API_KEY}"}, timeout=300)
                    r.raise_for_status()
                    got = (r.json()["choices"][0]["message"].get("content") or "").strip()
                except Exception:
                    got = ""
                if got and got.count("\n") >= 3 and "|" in got:
                    break
                got = ""
            if not got:
                review.append(f"{sec.get('category')} 第{q.get('id')}题：可疑表格未能用页面图像重识别")
                continue
            lines = str(q.get("content") or "").split("\n")
            idx = [i for i, l in enumerate(lines) if l.strip().startswith("|")]
            if not idx:
                continue
            q["content"] = "\n".join(lines[:idx[0]] + got.split("\n") + lines[idx[-1] + 1:])
            fixed += 1
            review.append(f"{sec.get('category')} 第{q.get('id')}题：表格已用页面图像重识别并替换")
    return fixed


def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    body, n_list = protect_material_lists(body)
    dots = None
    if args.dots and DocxDots is not None:
        src_dir = GAOZHONG / type_dir / "语文"
        docxs = sorted(src_dir.glob(f"{title}*.docx"))
        if not docxs:
            docs = sorted(src_dir.glob(f"{title}*.doc"))
            if docs:
                cache = Path("/tmp/cn_docx_cache")
                cache.mkdir(parents=True, exist_ok=True)
                target = cache / f"{title}.docx"
                if not target.exists():
                    try:
                        import subprocess
                        subprocess.run(["soffice", "--headless", "--convert-to", "docx", "--outdir", str(cache), str(docs[0])],
                                       capture_output=True, timeout=180)
                    except Exception:
                        pass
                if target.exists():
                    docxs = [target]
        if docxs:
            try:
                dots = DocxDots(docxs[0])
                log(f"  {title}: 已读取 docx 着重号（{docxs[0].name}）")
            except Exception as e:
                log(f"  {title}: 读取 docx 着重号失败 {str(e)[:80]}")

    # 结构规划（缓存到 <输出根>/<类型>/.cache/<卷名>-plan.json，重跑不再调模型）
    cache = OUT["root"] / type_dir / ".cache" / f"{title}-plan.json"
    plan = None
    if cache.exists() and not args.refresh_plan:
        try:
            plan = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            plan = None
    if plan is None:
        if not args.ai:
            raise RuntimeError("没有规划缓存且指定了 --no-ai（先跑一次联网版，或用 --refresh-plan）")
        plan = chat([{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": PLAN_PROMPT + body[:60000]}], max_tokens=6000)
        if isinstance(plan, dict) and plan.get("groups"):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not groups:
        raise RuntimeError("结构规划为空")

    answers = parse_answers(ans)
    # 第 3 轮：逐行扫描答案区（题号容错 + 相邻行回填），字母优先用扫描结果
    scanned = scan_answer_lines(ans)
    scan_letters: dict[int, str] = {}
    scan_rest: dict[int, str] = {}
    judged = 0
    for item in scanned:
        letter, rest = extract_choice_answer(item["text"])
        if letter:
            scan_letters[item["no"]] = letter
            if rest and len(rest) >= 8:
                scan_rest[item["no"]] = rest
        elif len(item["text"]) > 20:
            scan_rest[item["no"]] = item["text"]
            judged += 1
    SCAN["letters"], SCAN["rest"] = scan_letters, scan_rest
    log(f"  {title}：答案区候选题号 {len(scanned)} 个 / 抽出字母 {len(scan_letters)} 个"
        f" / 判解析 {judged} 个")
    review: list[str] = []
    out_groups = []
    translation = extract_translation(ans)
    for g in groups:
        grp = build_group(g, body, answers, review, dots, ans, answer_blocks(ans), md.parent, title, type_dir,
                          pdf_path=next(iter(sorted((GAOZHONG / type_dir / "语文").glob(f"{title}*.pdf"))), None),
                          allow_vision=not args.no_vision)
        if grp and grp["questions"]:
            # 选择题解析：答案区没有的，让 AI 按答案生成
            singles = [q for q in grp["questions"] if q["questionType"] == "single" and not (q.get("analysis") or "").strip()]
            if singles and args.ai_analysis:
                cache_f = OUT["root"] / type_dir / ".cache" / f"{title}-analysis.json"
                cache_map = {}
                if cache_f.exists():
                    try:
                        cache_map = json.loads(cache_f.read_text(encoding="utf-8"))
                    except Exception:
                        cache_map = {}
                reused = 0
                for q in singles:                       # 命中缓存的（题干没变）直接用，不再调模型
                    key = f"{q['id']}::{hashlib.md5(str(q.get('content')).encode()).hexdigest()[:10]}"
                    if cache_map.get(key):
                        q["analysis"] = cache_map[key]
                        reused += 1
                if reused:
                    log(f"  {grp['category']}：{reused} 道选择题解析取自缓存")
                singles = [q for q in singles if not (q.get("analysis") or "").strip()]
                got = ai_choice_analyses(singles) if singles else {}
                filled = 0
                for q in singles:
                    if got.get(q["id"]):
                        q["analysis"] = got[q["id"]]
                        filled += 1
                if filled:
                    log(f"  {grp['category']}：AI 补写 {filled} 道选择题解析")
                    cache_f.parent.mkdir(parents=True, exist_ok=True)
                    for q in grp["questions"]:
                        if q.get("analysis") and q.get("options"):
                            key = f"{q['id']}::{hashlib.md5(str(q.get('content')).encode()).hexdigest()[:10]}"
                            cache_map[key] = q["analysis"]
                    try:
                        cache_f.write_text(json.dumps(cache_map, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception:
                        pass
            if translation and grp["category"] == "文言文":
                for q in grp["questions"]:
                    base = (q.get("analysis") or "").strip()
                    q["analysis"] = (base + "\n\n" if base else "") + "【参考译文】\n\n" + translation
                log(f"  文言文：参考译文已写入 {len(grp['questions'])} 道题的解析")
            out_groups.append(grp)

    ans_text_only = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", ans or "")
    if args.ai_analysis and len(re.sub(r"\s+", "", ans_text_only)) >= 100:
        missing = []
        for grp in out_groups:
            for q in grp["questions"]:
                if not str(q.get("answer") or "").strip():
                    missing.append(q)
        if missing:
            got = ai_fill_answers(missing, ans, [q["id"] for q in missing])
            filled = 0
            for grp in out_groups:
                for q in grp["questions"]:
                    if not str(q.get("answer") or "").strip() and got.get(str(q["id"])):
                        q["answer"] = got[str(q["id"])]
                        filled += 1
            if filled:
                log(f"  {title}：AI 从答案区补 {filled}/{len(missing)} 题答案")
    if judged:
        review.append(f"答案解析：{judged} 个题号区块判定为解析（抽不出字母），已移入 analysis"
                      f"（候选 {len(scanned)} / 抽出字母 {len(scan_letters)}）")

    # 解析版兜底：答案区里「【答案】X」按出现顺序映射到尚缺答案的选择题
    _missing = [q for sec in out_groups for q in sec["questions"]
                if q.get("options") and not str(q.get("answer") or "").strip()]
    if _missing:
        _seq = re.findall(r"【答案】\s*([A-D]{1,4})\b", ans)
        if len(_seq) >= len(_missing):
            for q, letter in zip(_missing, _seq):
                q["answer"] = letter
            review.append(f"解析版兜底：按顺序把 {len(_missing)} 个「【答案】X」映射到缺答案的选择题")

    # 答案区是图片的老卷：视觉兜底（只补选择题字母）
    if not args.no_vision:
        need = [q for sec in out_groups for q in sec["questions"]
                if q.get("options") and not str(q.get("answer") or "").strip()]
        if need:
            got = {}
            if IMG_REF.search(ans):                                # 答案区里有图（老卷答案表是截图）
                got = vision_answers_from_images(
                    ans, md.parent, review,
                    OUT["root"] / type_dir / ".cache" / f"{title}-ansvision.json")
                filled = 0
                for sec in out_groups:
                    for q in sec["questions"]:
                        if q.get("options") and not str(q.get("answer") or "").strip():
                            head = str(q.get("id")).split("-")[0]
                            v = got.get(int(head)) if head.isdigit() else None
                            if v:
                                q["answer"] = v
                                filled += 1
            filled = locals().get("filled", 0)
            if filled:
                log(f"  {title}：视觉兜底补 {filled} 个选择题答案")
            still = [q for sec in out_groups for q in sec["questions"]
                     if q.get("options") and not str(q.get("answer") or "").strip()]
            if still and not args.no_vision:
                    pdfs = sorted((GAOZHONG / type_dir / "语文").glob(f"{title}*.pdf"))
                    if pdfs:
                        got2 = vision_answers_from_pdf(
                            pdfs[0], review,
                            OUT["root"] / type_dir / ".cache" / f"{title}-anspdf.json")
                        f2 = 0
                        for sec in out_groups:
                            for q in sec["questions"]:
                                if q.get("options") and not str(q.get("answer") or "").strip():
                                    head = str(q.get("id")).split("-")[0]
                                    v = got2.get(int(head)) if head.isdigit() else None
                                    if v:
                                        q["answer"] = v
                                        f2 += 1
                        if f2:
                            log(f"  {title}：PDF 答案页兜底补 {f2} 个选择题答案")

    # 微写作/作文：教师版常不给答案，题库惯例写「略」
    for sec in out_groups:
        if sec.get("category") in ("微写作", "作文"):
            for q in sec["questions"]:
                if not str(q.get("answer") or "").strip():
                    q["answer"] = "略"
                    review.append(f"{sec['category']} 第{q['id']}题：教师版无参考答案，按题库惯例填「略」")
    left = [q["id"] for sec in out_groups for q in sec["questions"] if not str(q.get("answer") or "").strip()]
    if left:
        review.append(f"仍缺答案：{left[:20]}")

    # ── 出口后处理（原 4 个后处理脚本，一次做完）──
    sections = merge_sections(out_groups, review)
    n_merged = merge_split_options(sections, answers, review)
    if n_merged:
        log(f"  {title}：合并被拆开的选项 {n_merged} 处")
    # 合并出来的选择题补答案（答案区按题号）
    for sec in sections:
        for q in sec["questions"]:
            if q.get("options") and not str(q.get("answer") or "").strip():
                v = str(answers.get(int(q["id"])) if str(q["id"]).isdigit() else "").strip().upper()
                if re.fullmatch(r"[A-D]{1,2}", v):
                    q["answer"] = v
    normalize_choice_answers(sections, review)
    _y = re.search(r"(20\d{2})", title)
    normalize_scores(sections, review, year=int(_y.group(1)) if _y else None)
    if args.repair_tables:
        pdfs = sorted((GAOZHONG / type_dir / "语文").glob(f"{title}*.pdf"))
        repair_tables(sections, pdfs[0] if pdfs else None, type_dir, review)
    for sec in sections:
        for q in sec["questions"]:
            q["answer"] = strip_answer_headers(q.get("answer"))
    sanitize_payload(sections, review)

    year = re.search(r"(20\d{2})", title)
    total = sum(sec["score"] for sec in sections)
    paper = {
        "title": title, "subject": "语文", "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": total or None, "duration": 150,
        "tags": ["语文", args.grade, type_dir] + ([year.group(1)] if year else []),
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 旧格式（groups）先备份成 <卷>.old-groups.json，再覆盖
    if dst.exists():
        try:
            old = json.loads(dst.read_text(encoding="utf-8"))
        except Exception:
            old = None
        if isinstance(old, dict) and "groups" in old and "sections" not in old:
            bak = dst.with_name(f"{dst.stem}.old-groups.json")
            if not bak.exists():
                bak.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
                review.append(f"旧格式 JSON 已备份：{bak.name}")
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)  # 出口约定：选择题答案与选项 id 一律大写
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    dst.with_suffix(".review.txt").write_text("\n".join(review) + "\n", encoding="utf-8")
    n = sum(len(sec["questions"]) for sec in sections)
    return f"✓ {title}：{len(sections)} 板块 / {n} 题 / {total} 分（复核 {len(review)} 处）"



# ══════════════════════════════════════════════════════════════════
# --check：契约自检（纯 Python，不调模型）
# ══════════════════════════════════════════════════════════════════
EXPECTED_QNUM = {"多文本": 5, "文言文": 5, "文学类": 4, "古诗": 3, "语用": 2,
                 "名著": 2, "微写作": 1, "作文": 1}
STEM_RESIDUE = re.compile(r"^\s*(?:[①②③④⑤⑥]{1,6}|(?:[一二三四五六]、)?(?:选择题|非选择题)\**|"
                          r"\*\*[^*\n]+(?:选择题|非选择题)[^*\n]+\*\*)\s*(?:\n|$)")
EXAM_IMG = re.compile(r"/exam-images/([^\"'\s)]+)")
_norm = lambda t: re.sub(r"\s+", "", t or "")


def img_exists(ref: str, type_dir: str) -> bool:
    """/exam-images/<类型>/语文/<卷名>/images/x.png → 落盘（卷目录带「（教师版）」也要命中）"""
    if not ref.startswith("/exam-images/"):
        return False
    parts = ref[len("/exam-images/"):].split("/")
    if len(parts) < 5:
        return False
    t, subj, paper, _img, fname = parts[0], parts[1], parts[2], parts[3], parts[4]
    parent = MD_ROOT / t / subj
    if not parent.exists():
        return False
    for d in parent.iterdir():
        if not d.is_dir():
            continue
        base = re.sub(r"[（(]教师版[）)]|[（(]含答案[）)]", "", d.name).strip()
        if (base == paper or d.name == paper) and (d / "images" / fname).exists():
            return True
    return (parent / paper / "images" / fname).exists()


EXC_DIR = Path(__file__).parent / "exceptions"
EXC_DEFAULT = EXC_DIR / "chinese.json"


def load_exceptions(path: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    """{"(类型, 卷名)": {"题号或 section": reason}}"""
    p = path or EXC_DEFAULT
    out: dict[tuple[str, str], dict[str, str]] = {}
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return out
    for item in data.get("papers") or []:
        key = (str(item.get("type") or ""), str(item.get("title") or ""))
        m: dict[str, str] = {}
        for it in item.get("items") or []:
            q = str(it.get("question") or "")
            m[q] = str(it.get("reason") or "")
            m[f"{it.get('section') or ''}::{q}"] = str(it.get("reason") or "")
        out[key] = m
    return out


def _exc_hit(exc: dict, cat: str, qid, kinds: tuple[str, ...]) -> str | None:
    """命中白名单（按题号，或 section::题号）；reason 为空视为未登记"""
    for k in (str(qid), f"{cat}::{qid}") + tuple(kinds):
        if k in exc:
            return exc[k] or None
    return None


def check_paper(jf: Path, exceptions_path: Path | None = None) -> tuple[dict, list[str], list[str]]:
    stat: dict = {"title": jf.stem}
    errs: list[str] = []
    notes: list[str] = []
    exc_all = load_exceptions(exceptions_path)
    exc_map = exc_all.get((jf.parent.name, jf.stem), {})
    exc_hits: list[dict] = []

    def err(msg: str, cat: str = "", qid=None, kind: str = "") -> None:
        """登记错误；命中白名单且有原因 → 记例外（不计错误）"""
        if exc_map and qid is not None:
            reason = _exc_hit(exc_map, cat, qid, (kind,) if kind else ())
            if reason:
                exc_hits.append({"section": cat, "question": str(qid), "reason": reason})
                return
        errs.append(msg)
    raw = jf.read_text(encoding="utf-8")
    data = json.loads(raw)
    type_dir = jf.parent.name
    paper = data.get("paper") or {}
    sections = data.get("sections")
    if sections is None:
        errs.append("顶层键不是 sections（还是旧契约 groups？）")
        return stat, errs, notes
    if paper.get("subject") != "语文":
        errs.append(f"paper.subject={paper.get('subject')!r}（应为 语文）")
    if not sections:
        errs.append("sections 为空")
        return stat, errs, notes

    # ① 分值
    total = sum(sec.get("score") or 0 for sec in sections)
    if paper.get("totalScore") != total:
        errs.append(f"paper.totalScore={paper.get('totalScore')} ≠ Σsection {total}")
    if total != TOTAL_SCORE:
        err(f"整卷 {total} 分 ≠ 语文标准 {TOTAL_SCORE} 分", "整卷", "*", "整卷分值")
    for sec in sections:
        qs = sec.get("questions") or []
        ssum = sum(q.get("score") or 0 for q in qs)
        _cat = sec.get("category") or ""
        # 任选一题板块（作文二选一 / 微写作三选一）：每道候选题都按满分放，Σ小问 ≠ 板块分是正常的
        _pick_one = _cat in ("作文", "微写作") and len(qs) > 1
        if _pick_one:
            _std = {"作文": 50, "微写作": 10}[_cat]
            _bad = [q for q in qs if (q.get("score") or 0) != _std]
            if _bad:
                errs.append(f"{_cat}：任选一题板块应每问 {_std} 分，实际 " +
                            "/".join(str(q.get("score")) for q in qs))
            if (sec.get("score") or 0) != _std and _cat == "作文":
                errs.append(f"{_cat}：板块分应为 {_std}（任选一题只算一次），实际 {sec.get('score')}")
        elif ssum != (sec.get("score") or 0):
            errs.append(f"{sec.get('category')}：Σ小问 {ssum} ≠ section.score {sec.get('score')}")
        if any((q.get("score") or 0) <= 0 for q in qs):
            errs.append(f"{sec.get('category')}：有小问分值 ≤ 0")
        _cat0 = sec.get("category") or ""
        for _q in qs:
            _sc = _q.get("score") or 0
            if _q.get("options"):
                if not 2 <= _sc <= 6:
                    errs.append(f"{_cat0} 第{_q.get('id')}题 {_sc} 分，超出选择题上限 6 分（下限 2 分）")
            elif _cat0 == "默写":
                if _sc != 1:
                    errs.append(f"默写 第{_q.get('id')}题 {_sc} 分（应每空 1 分）")
            elif _cat0 == "微写作":
                if _sc != 10:
                    errs.append(f"微写作 第{_q.get('id')}题 {_sc} 分（应为 10 分）")
            elif _cat0 == "作文":
                if not 40 <= _sc <= 62:
                    errs.append(f"作文 第{_q.get('id')}题 {_sc} 分（应在 40–62）")
            elif not 1 <= _sc <= 9:
                errs.append(f"{_cat0} 第{_q.get('id')}题 {_sc} 分，超出主观题上限 9 分（下限 1 分）")
        if not qs:
            errs.append(f"{sec.get('category')}：没有问题")

    # ⑦ 板块口径
    cats = [sec.get("category") for sec in sections]
    dup = {c for c in cats if cats.count(c) > 1}
    if dup:
        errs.append(f"板块重复（未合并）：{sorted(dup)}")
    for sec in sections:
        cat = sec.get("category")
        if cat not in CATEGORIES:
            errs.append(f"未知板块 {cat!r}（只能取 {'/'.join(CATEGORIES)}）")
            continue
        if sec.get("questionType") != QTYPE_OF[cat]:
            errs.append(f"{cat}：questionType={sec.get('questionType')!r}（应为 {QTYPE_OF[cat]}）")
        if cat == "作文":
            sc = sec.get("score") or 0
            if not 40 <= sc <= 62:
                errs.append(f"作文：{sc} 分（应在 40–62 之间，以卷面为准）")
        elif cat == "微写作":
            sc = sec.get("score") or 0
            if not 0 <= sc <= 12:
                errs.append(f"微写作：{sc} 分（应在 0–12 之间，以卷面为准）")
        if cat == "默写":
            n_ph = len(PH_RE.findall(sec.get("content") or ""))
            nq = len(sec.get("questions") or [])
            if n_ph != nq:
                err(f"默写：占位符 {n_ph} 个 ≠ 空/小题 {nq} 个", "默写", "*", "默写空数")
            if (sec.get("score") or 0) != n_ph or (sec.get("score") or 0) != nq:
                errs.append(f"默写：{n_ph} 空 / {sec.get('score')} 分（应每空 1 分）")
            if not 1 <= n_ph <= 14:
                err(f"默写：{n_ph} 个空（应在 1–14 之间；超出说明切分错）", cat, "*", "默写空数")
            if n_ph not in DICTATION_SCORES:
                notes.append(f"默写：{n_ph} 空（常规 8/10 空，按卷面实际）")
        elif cat == "微写作":
            nq = len(sec.get("questions") or [])
            if not 1 <= nq <= 3:
                errs.append(f"微写作：{nq} 题（北京卷 1–3 个材料/题目三选一）")
        elif cat == "作文":
            nq = len(sec.get("questions") or [])
            if not 1 <= nq <= 2:
                errs.append(f"作文：{nq} 题（北京卷 1 个题目或二选一）")
        elif PH_RE.search(sec.get("content") or ""):
            errs.append(f"{cat}：content 里有占位符（阅读类不内联，前端按小题渲染）")
        exp = EXPECTED_QNUM.get(cat)
        if exp and len(sec.get("questions") or []) != exp:
            notes.append(f"{cat}：{len(sec.get('questions'))} 题（常规 {exp} 题，个别卷有差异）")

    # ② ③ ④ ⑤ 逐题
    for sec in sections:
        cat = sec.get("category")
        content = sec.get("content") or ""
        if cat in ("多文本", "文言文", "古诗", "文学类") and not content.strip():
            errs.append(f"{cat}：content（阅读材料）为空")
        elif cat in ("名著", "语用") and not content.strip():
            notes.append(f"{cat}：该题组无阅读材料（整本书/语用问答直接给设问），content 为空属正常")
        if STEM_RESIDUE.match(content) or NOTICE_RE.search(content[:120]):
            errs.append(f"{cat}：材料开头混进分区标题/卷首说明：{content[:20]!r}")
        body = _norm(content)
        for q in sec.get("questions") or []:
            qc = (q.get("content") or "").strip()
            qid = q.get("id")
            if STEM_RESIDUE.match(qc) or NOTICE_RE.search(qc[:120]):
                errs.append(f"{cat} 第{qid}题：题干开头混进分区标题/卷首说明：{qc[:20]!r}")
            opts = q.get("options") or []
            if opts:
                oids = [str(o.get("id") or "").upper() for o in opts]
                if oids != ["A", "B", "C", "D"]:
                    err(f"{cat} 第{qid}题：options={oids}（应为 A–D 四项）", cat, qid, "选项数不足")
                ans = str(q.get("answer") or "")
                if not re.fullmatch(r"[A-D]{1,4}", ans) or ans != ans.upper() or any(c not in oids for c in ans):
                    err(f"{cat} 第{qid}题：answer={ans!r}（应为 A–D 字母串且每个字母 ∈ options）",
                        cat, qid, "答案缺失" if not ans else "答案格式")
                if any(not str(o.get("label") or "").strip() for o in opts):
                    err(f"{cat} 第{qid}题：有空选项", cat, qid, "空选项")
            elif q.get("questionType") not in ("text", "input"):
                err(f"{cat} 第{qid}题：questionType={q.get('questionType')!r}", cat, qid, "题型异常")
            if cat in ("多文本", "文言文", "古诗", "文学类"):
                if not qc:
                    err(f"{cat} 第{qid}题：题干为空", cat, qid, "题干为空")
                elif _norm(qc) in body:
                    err(f"{cat} 第{qid}题：设问重复出现在 section.content 里（前端会双显）", cat, qid, "设问重复")
            if cat in ("微写作", "作文") and not qc:
                errs.append(f"{cat} 第{qid}题：题目为空")
            if not str(q.get("answer") or "").strip():
                notes.append(f"{cat} 第{qid}题：没有参考答案")

    # ⑥ 图片
    refs = sorted(set(EXAM_IMG.findall(raw)))
    for ref in refs:
        if not img_exists("/exam-images/" + ref, type_dir):
            errs.append(f"图片不存在：/exam-images/{ref}")
    notes.append(f"图片引用 {len(refs)} 张")

    stat["exceptions"] = exc_hits
    stat.update({
        "sections": len(sections),
        "questions": sum(len(sec.get("questions") or []) for sec in sections),
        "choices": sum(1 for sec in sections for q in (sec.get("questions") or []) if q.get("options")),
        "opens": sum(1 for sec in sections for q in (sec.get("questions") or [])
                     if not q.get("options") and q.get("questionType") in ("text", "input")),
        "total": total,
        "imgs": len(refs),
        "answered": sum(1 for sec in sections for q in (sec.get("questions") or [])
                        if str(q.get("answer") or "").strip()),
        "analysed": sum(1 for sec in sections for q in (sec.get("questions") or [])
                        if str(q.get("analysis") or "").strip()),
    })
    return stat, errs, notes


def run_check(args) -> int:
    files: list[Path] = []
    if args.path:
        p = Path(args.path)
        if p.is_file() and p.suffix == ".json":
            files = [p]
        elif p.is_file():
            stem = re.sub(r"[（(]教师版[）)]", "", p.stem).strip()
            files = [OUT["root"] / args.type / f"{stem}.json"]
        else:
            # 目录：JSON 或 md 目录都收
            files = sorted(x for x in p.glob("*.json") if not x.name.endswith(".old-groups.json"))
            if not files:
                for d in sorted(p.iterdir()):
                    if d.is_dir() and not d.name.endswith("（教师版）"):
                        q = OUT["root"] / args.type / f"{d.name}.json"
                        if q.exists():
                            files.append(q)
            if not files:
                files = [OUT["root"] / args.type / f"{p.name}.json"]
    else:
        types = ([t.name for t in sorted(OUT["root"].iterdir()) if t.is_dir() and t.name != ".cache"]
                 if (args.type or "").strip().lower() == "all" else [args.type])
        for t in types:
            files += sorted(x for x in (OUT["root"] / t).glob("*.json")
                            if not x.name.endswith(".old-groups.json"))
    files = [f for f in files if f.exists()]
    if getattr(args, "only", None):
        files = [f for f in files if args.only in f.stem]
    if not files:
        print(f"没有找到 JSON：{args.path or OUT['root'] / args.type}")
        return 2
    bad = 0
    rows = []
    n_exc_total = 0
    n_err_total = 0
    _exc_reasons: dict[str, int] = {}
    skipped = []
    kept = []
    for jf in files:
        try:
            probe = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            probe = {}
        if "sections" not in probe and "groups" in probe:
            skipped.append(jf.stem)
            continue
        kept.append(jf)
    files = kept
    if skipped:
        print(f"（跳过 {len(skipped)} 份仍是旧格式 groups、尚未迁移的卷：{', '.join(skipped[:3])}"
              f"{' …' if len(skipped) > 3 else ''}）")
    if not files:
        print("没有可检查的（新格式）JSON")
        return 0
    for jf in files:
        try:
            stat, errs, notes = check_paper(jf, getattr(args, "exceptions_path", None))
        except Exception as e:                              # noqa: BLE001
            print(f"\n===== {jf.name} =====\n  ✗ 读不了：{str(e)[:160]}")
            bad += 1
            continue
        print(f"\n===== {jf.name} =====")
        print(f"{stat.get('sections')} 板块 / {stat.get('questions')} 题"
              f"（选择 {stat.get('choices')} · 主观 {stat.get('opens')}）"
              f" / {stat.get('total')} 分 / 图片 {stat.get('imgs')} 张"
              f" / 有答案 {stat.get('answered')} · 有解析 {stat.get('analysed')}")
        data = json.loads(jf.read_text(encoding="utf-8"))
        for sec in data.get("sections") or []:
            qs = sec.get("questions") or []
            print(f"   · {sec.get('category')}｜{sec.get('questionType')}｜{sec.get('score')} 分"
                  f"｜{len(qs)} 题｜Σ{sum(q.get('score') or 0 for q in qs)}"
                  f"｜材料 {len(sec.get('content') or '')} 字")
        for ex in stat.get("exceptions") or []:
            _exc_reasons[str(ex.get("reason") or "")] = _exc_reasons.get(str(ex.get("reason") or ""), 0) + 1
        n_exc_total += len(stat.get("exceptions") or [])
        n_err_total += len(errs)
        if errs:
            bad += 1
            print(f"  ✗ 契约错误 {len(errs)} 条：")
            for e in errs:
                print(f"     - {e}")
        else:
            print("  ✓ 契约校验通过")
        for ex in stat.get("exceptions") or []:
            print(f"  · 例外（白名单）：{ex['section']} 第{ex['question']}题 — {ex['reason']}")
        for n in notes:
            print(f"  · 提示：{n}")
        rows.append((jf.stem, stat.get("sections"), stat.get("questions"), stat.get("total"),
                     "✓" if not errs else "✗", len(errs)))
    print("\n| 卷 | 板块 | 题 | 总分 | 结论 | 错误 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |")
    print(f"\n例外 {n_exc_total} 处（白名单）/ 新增错误 {n_err_total} 处")
    if _exc_reasons:
        print("例外按原因分组：")
        for why, cnt in sorted(_exc_reasons.items(), key=lambda x: -x[1]):
            print(f"  {cnt:3d}  {why}")
    if bad:
        print(f"共 {len(rows)} 卷，{bad} 卷有新增错误（白名单之外）")
        return 2
    print(f"共 {len(rows)} 卷，除白名单例外外全部通过 ✓")
    return 0


def protect_material_lists(body: str):
    """「任务一：…」表头之后的 1．2．3． 条目是材料清单，不是题号 → 降级成普通文本。"""
    lines = body.split("\n")
    in_list = False
    n = 0
    for i, ln in enumerate(lines):
        st = ln.strip()
        if MAT_HEAD.match(st):
            in_list = True
            continue
        if not in_list:
            continue
        m = NUM_LINE.match(st)
        if m and not SETQ_HINT2.search(st):
            lines[i] = "· " + st
            n += 1
            continue
        if m and SETQ_HINT2.search(st):
            in_list = False
            continue
        if not st or st.startswith(("（取材于", "(取材于", "材料", "###")):
            continue
        in_list = False
    return "\n".join(lines), n


MAT_HEAD = re.compile(r"^\s*(任务|活动|环节|步骤|要求|安排|工作)[一二三四五六1234]?\s*[：:]")
NUM_LINE = re.compile(r"^\s*(\d{1,2})\s*[.．、]\s*")
SETQ_HINT2 = re.compile(r"根据材料|下列|下列说法|下列对|简要分析|概括|说明|请|回答|完成|推断|分析")


# ══ 兼容块（第 3–5 轮补丁在多次切片编辑中丢失，这里统一补回）══
ANSLINE = re.compile(r"^[ \t]*(?:[（(]\s*)?([0-9０-９]{1,2})\s*(?:[）)]|[.．、,，:：])\s*(.*)$")
NUM_MARK = re.compile(r"(?<![0-9０-９])([0-9０-９]{1,2})\s*\\?\s*(?:[.．、,，:：]|[）)])\s*")
SEG_BREAK = re.compile(r"【(?:分析|解答|点评|详解|点睛|考点|解析)】|解析\s*[:：]")
OPT_ASK = re.compile(r"请|分析|概括|说明|简析|简述|为什么|哪些|如何|谈谈|评价|赏析|举例|结合|指出|[?？]")
SETQ_HINT = re.compile(r"下列|一项|正确|不正确|错误|符合题意")
INLINE_OPT = re.compile(r"(?:^|(?<=[\s\u3000]))([A-D])\s*[.．、]\s*")
TABLE_OPT_ROW = re.compile(r"^\|\s*\**([A-D])\**\s*[.．、]\s*(.*)$")
NEXT_BLOCK = re.compile(r"^(?:根据|阅读下|完成|材料[一二三四1234]|第[一二三四]部分)")


def _to_int(x):
    return int(str(x).translate(str.maketrans("０１２３４５６７８９", "0123456789")))


def scan_answer_lines(ans):
    """逐行扫描答案区（题号容错全角/括号/顿号/逗号/转义点；题号独占一行用相邻行回填）。"""
    lines = [l for l in (ans or "").split("\n")]
    out, seen = [], set()
    for i, ln in enumerate(lines):
        line = re.sub(r"^\s*(?:【[^】]{0,10}】|\*\*)", "", ln).strip()
        marks = list(NUM_MARK.finditer(line))
        if not marks:
            continue
        for k, m in enumerate(marks):
            try:
                no = _to_int(m.group(1))
            except Exception:
                continue
            if not 1 <= no <= 40 or no in seen:
                continue
            endp = marks[k + 1].start() if k + 1 < len(marks) else len(line)
            seg = line[m.end():endp].strip()
            rest = SEG_BREAK.split(seg)[0].strip()
            if not rest:
                mm = re.search(r"(?:故选|答案)\s*[:：]?\s*([A-D]{1,4})\b", seg)
                if mm:
                    rest = mm.group(1)
                    seg = rest
            if not rest:
                nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
                if nxt and not NUM_MARK.match(nxt):
                    rest = SEG_BREAK.split(nxt)[0].strip()
                elif i:
                    prev = lines[i - 1].strip()
                    if re.match(r"^[A-D]", prev):
                        rest = prev
            seen.add(no)
            out.append({"no": no, "text": rest})
    return out


def extract_choice_answer(block):
    """从答案区块抠出选择题字母 + 剩余解析。"""
    b = (block or "").strip()
    if not b:
        return "", ""
    b = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", b)
    m = re.match(r"^[【\[]\s*([A-D]{1,4})\s*[】\]]", b)
    if m:
        return m.group(1), b[m.end():].strip()
    m = re.match(r"^([A-D]{1,4})\s*(?:[.．、)）:：]|[（(]|\s|$)", b)
    if m:
        return m.group(1), b[m.end():].strip()
    for pat in (r"【答案】\s*([A-D]{1,4})", r"答案\s*[:：]\s*([A-D]{1,4})", r"应选\s*([A-D]{1,4})",
                r"故选\s*[:：]?\s*([A-D]{1,4})", r"选\s*([A-D]{1,4})\s*给\s*\d+\s*分",
                r"([A-D])\s*项(?:正确|符合题意)", r"正确的是\s*([A-D]{1,4})"):
        mm = re.search(pat, b)
        if mm:
            return mm.group(1), b
    return "", b


def _looks_like_option(text):
    t = (text or "").strip()
    return bool(t) and 10 <= len(t) <= 45 and "\n" not in t and not OPT_ASK.search(t)


def _parallel_run(run):
    if len(run) < 3:
        return False
    heads = {re.sub(r"[\s\W]", "", q.get("content") or "")[:2] for q in run}
    if len(heads) <= 2:
        return True
    return all((q.get("content") or "").strip().startswith(("学生", "分组", "在", "通过", "以", "教师")) for q in run)


def _pick_merged_stem(sec, prev_qs, run):
    for q in reversed(prev_qs):
        c = (q.get("content") or "").strip()
        if c and SETQ_HINT.search(c) and len(c) <= 120:
            return c
    return ""


def merge_split_options(sections, answers, review):
    n_total = 0
    for sec in sections:
        qs = sec.get("questions") or []
        if len(qs) < 4:
            continue
        out, i = [], 0
        while i < len(qs):
            j = i
            while (j < len(qs) and not qs[j].get("options")
                   and qs[j].get("questionType") == "text"
                   and _looks_like_option(qs[j].get("content") or "")):
                j += 1
            run = qs[i:j]
            ok = _parallel_run(run)
            if ok and i > 0 and MAT_HEAD.match((qs[i - 1].get("content") or "").strip()):
                ok = False
                review.append(f"{sec.get('category')}：片段前紧跟材料清单表头 → 不合并")
            if ok:
                letters = set()
                for q in run:
                    for src in (str(q.get("answer") or ""),
                                str(answers.get(int(q["id"])) if str(q["id"]).isdigit() else "")):
                        v = src.strip().upper()
                        if re.fullmatch(r"[A-D]{1,2}", v):
                            letters.add(v)
                if not letters or any(len(v) > 1 or "ABCD".index(v[0]) >= len(run) for v in letters):
                    ok = False
                    review.append(f"{sec.get('category')}：连续 {len(run)} 个短 text 疑似选项被拆，但答案区无可用字母 → 不合并")
            if ok:
                opts = [{"id": "ABCD"[k], "label": (q.get("content") or "").strip()} for k, q in enumerate(run[:4])]
                stem = _pick_merged_stem(sec, out, run)
                out.append({"id": qs[i]["id"], "questionType": "single", "content": stem,
                            "options": opts, "answer": "", "analysis": "",
                            "score": sum(q.get("score") or 0 for q in run)})
                review.append(f"{sec.get('category')}：第{qs[i]['id']}–{qs[j - 1]['id']}题（{len(run)} 个平行短句）判定为被拆开的选项，已合并")
                n_total += 1
                i = j
                continue
            out.extend(qs[i:max(j, i + 1)])
            i = max(j, i + 1)
        sec["questions"] = out
        sec["score"] = sum(q.get("score") or 0 for q in out)
    return n_total


ANS_VISION_PROMPT = r"""这是语文试卷教师版答案页。请逐题读出选择题答案，只输出 JSON：
{"answers":[{"no":1,"answer":"C","conf":0.95}]}；看不清写 null 并给低 conf；不是答案页给空数组。"""


def _vision_ask_image(img):
    import base64
    import requests as _rq
    b64 = base64.b64encode(img.read_bytes()).decode()
    r = _rq.post(f"{BASE_URL}/chat/completions",
                 json={"model": MODEL, "max_tokens": 1500, "temperature": 0.0, "reasoning_effort": "none",
                       "messages": [{"role": "system", "content": "你是语文试卷录入员，只输出 JSON。"},
                                    {"role": "user", "content": [
                                        {"type": "text", "text": ANS_VISION_PROMPT},
                                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]},
                 headers={"Authorization": f"Bearer {API_KEY}"}, timeout=300)
    r.raise_for_status()
    txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    data = json.loads(m.group(0)) if m else {}
    out = {}
    for item in (data.get("answers") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            no = re.sub(r"\D", "", str(item.get("no") or ""))
            v = str(item.get("answer") or "").strip().upper()
            if no and re.fullmatch(r"[A-D]{1,4}", v):
                out[no] = v
    return out


def vision_answers_from_images(ans_text, md_dir, review, cache_f=None):
    refs = IMG_REF.findall(ans_text or "")[:6]
    out = {}
    for ref in refs:
        img = md_dir / ref
        if not img.exists():
            continue
        try:
            for k, v in _vision_ask_image(img).items():
                out[int(k)] = v
        except Exception as e:
            review.append(f"答案图识别失败（{img.name}）：{str(e)[:50]}")
    if out:
        review.append(f"答案区图片视觉识别：读出 {len(out)} 个答案")
    return out


def vision_answers_from_pdf(pdf, review, cache_f=None, pages=8):
    if not pdf or not Path(pdf).exists():
        return {}
    try:
        import tempfile
        import convert_math_pdf as M
        with tempfile.TemporaryDirectory() as td:
            for img in M.render_pages(Path(pdf), 160, Path(td), pages):
                try:
                    got = _vision_ask_image(img)
                except Exception:
                    continue
                if got:
                    out = {int(k): v for k, v in got.items()}
                    review.append(f"PDF 答案页视觉抽取：读出 {len(out)} 个答案")
                    return out
    except Exception as e:
        review.append(f"PDF 答案页视觉抽取失败：{str(e)[:50]}")
    return {}


def vision_question_from_pdf(pdf, no, review, cache_f=None):
    review.append(f"第{no}题：PDF 视觉还原未实现（保守跳过，保留原文）")
    return {}


def split_inline_options(text):
    """把题干里内联的 A–D 选项切出来 → (题干, options)。切不出返回 (原文, [])。"""
    t = (text or "").strip()
    if not t:
        return t, []
    rows = [l for l in t.split("\n") if l.strip().startswith("|")]
    if len(rows) >= 3 and sum(1 for r in rows if TABLE_OPT_ROW.match(r.strip())) >= 2:
        opts, stems = [], []
        for l in t.split("\n"):
            m = TABLE_OPT_ROW.match(l.strip())
            if m:
                cells = [c.strip() for c in l.strip().strip("|").split("|")]
                opts.append({"id": m.group(1), "label": "；".join([c for c in cells[1:] if c])})
            elif not l.strip().startswith("|"):
                stems.append(l)
        if len(opts) >= 2:
            return "\n".join(stems).strip(), opts
    def _seq(pat):
        marks = [(m.start(1) if m.groups() else m.start(), m.end(1) if m.groups() else m.end(), m.group(1))
                 for m in re.finditer(pat, t)]
        seq = []
        for pos, end, k in marks:
            want = "ABCD"[len(seq)] if len(seq) < 4 else None
            if k == want:
                seq.append((pos, end, k))
        return seq
    seq = _seq(r"(?m)(?:^|(?<=[\s\u3000]))([A-D])\s*[.．、]\s*")
    if len(seq) < 3:
        seq = _seq(r"(?m)(?:^|[ \t\u3000。；）)])([A-D])(?=[\u4e00-\u9fff①-⑩\"“])")
    if len(seq) < 3:
        return t, []
    stem = t[:seq[0][0]].strip()
    if t.seq if False else False:
        pass
    opts = []
    for i, (pos, end, k) in enumerate(seq):
        stop = seq[i + 1][0] if i + 1 < len(seq) else len(t)
        seg = t[end:stop]
        if i + 1 == len(seq):
            for mm in re.finditer(r"\n\s*\n", seg):
                tail = seg[mm.end():].strip()
                if tail and NEXT_BLOCK.match(tail):
                    seg = seg[:mm.start()]
                    break
        opts.append({"id": k, "label": re.sub(r"\s*\n\s*", " ", seg).strip()})
    opts = [o for o in opts if o["label"]]
    if len(opts) < 3:
        return t, []
    return stem, opts


def main():
    ap = argparse.ArgumentParser(description="语文试卷 MD → 结构化 JSON")
    ap.add_argument("-p", "--path")
    ap.add_argument("--dir", default=str(MD_ROOT))
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-ai", dest="ai", action="store_false",
                    help="不调模型（结构规划走 .cache 缓存；没有缓存会报错）")
    ap.add_argument("--no-dots", dest="dots", action="store_false")
    ap.add_argument("--no-ai-analysis", dest="ai_analysis", action="store_false")
    ap.add_argument("--refresh-plan", action="store_true", help="忽略缓存、重新让模型做结构规划")
    ap.add_argument("--no-vision", dest="no_vision", action="store_true",
                    help="关闭「答案区是图片」的多模态兜底")
    ap.add_argument("--repair-tables", action="store_true",
                    help="用 PDF 页面图像重识别可疑表格（原 repair_chinese_tables.py，慢）")
    ap.add_argument("--check", action="store_true", help="契约自检（纯 Python，不调模型）")
    ap.add_argument("--only", default=None, help="配合 --check：只检查卷名含该子串的卷")
    ap.add_argument("--strict", action="store_true",
                    help="配合 --check：忽略例外白名单（CI/发版前用）")
    ap.add_argument("--exceptions", default=None,
                    help="例外白名单（默认 exam-parser/exceptions/chinese.json）")
    ap.add_argument("-w", "--workers", type=int, default=3)
    args = ap.parse_args()

    OUT["root"] = Path(args.output)
    AI_ENABLED["on"] = bool(args.ai)
    args.exceptions_path = None if getattr(args, "strict", False) else (
        Path(args.exceptions) if getattr(args, "exceptions", None) else None)
    if getattr(args, "strict", False):
        print("（--strict：忽略例外白名单）")
    if args.check:
        raise SystemExit(run_check(args))
    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md"))[:1] if p.is_dir() else [p]
    else:
        root = Path(args.dir) / args.type / "语文"
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir()) if d.is_dir() and sorted(d.glob("*.md"))]
    log(f"共 {len(files)} 份 MD")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    reps = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(process, f, args.type, args): f for f in files}
        for fut in as_completed(futs):
            try:
                log("  " + fut.result())
                reps.append(True)
            except Exception as e:
                log(f"  ✗ {futs[fut].parent.name}: {str(e)[:200]}")
                reps.append(False)
    log(f"完成：成功 {sum(reps)} / {len(files)}")


if __name__ == "__main__":
    main()
