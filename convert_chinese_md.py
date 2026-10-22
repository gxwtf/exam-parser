#!/usr/bin/env python3
"""语文试卷（教师版）docx→MD → 结构化试卷 JSON

和英语同一套思路：**大段文章不交给模型复述**，而是让模型给出「原文里的起止锚点（anchor）」，
再由脚本从 Markdown 里按锚点精确截出文章；题目/选项/答案同样按题号从原文切，模型只做结构规划。

输出：output_json_chinese/<类型>/<卷>.json
{
  "paper": {title, subject, source, grade, year, totalScore, duration, tags},
  "groups": [
    {"category":"多文本","questionType":"chinese-reading","title":"…","intro":"阅读下面材料，完成1-5题。",
     "content":"<文章 Markdown>","score":18,
     "questions":[{"id":1,"questionType":"single","content":"…","options":[…],"answer":"C","score":3}]}
  ]
}

用法：
  python3 convert_chinese_md.py -p ../output_docx_to_md/二模/语文/xxx/xxx.md
  python3 convert_chinese_md.py --dir ../output_docx_to_md -T 二模 -w 3
"""
import argparse, json, os, re, time
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from docx_dots import DocxDots
except Exception:  # 读不了 docx 也不能影响主流程
    DocxDots = None

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

MD_ROOT = PROJECT_ROOT.parent / "output_docx_to_md"
OUT_ROOT = PROJECT_ROOT.parent / "output_json_chinese"

BASE_URL = (os.getenv("MATH_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = os.getenv("MATH_VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")

QSPLIT = re.compile(r"(?m)^[ \t]*(\d{1,2})[、.．](?!\d)\s*")
SEC_SPLIT = re.compile(r"(?m)^[ \t]*([一二三四五六七八九十])、\s*本大题共\s*(\d+)\s*小题[，,]\s*共\s*(\d+)\s*分")
OPT_LINE = re.compile(r"(?m)^[ \t]*([A-D])[．.、]\s*(.+)$")
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
   "article_end":"阅读材料的结尾 10-30 个字（原文逐字）"}
]}

规则：
1. 按题型拆组：一个大题里可能混着多个题型（例如第 14 题是默写、第 15 题是名著），必须拆开成两组。
2. 只有「有阅读材料」的题组才给 article_start/article_end；默写/微写作/作文没有材料，写空字符串。
3. questionType 映射：多文本/文言文/古诗/文学类/名著/语用 → chinese-reading；默写 → chinese-dictation；
   微写作 → chinese-micro-writing；作文 → chinese-essay。
4. article_start / article_end 必须是原文里**逐字出现**的片段（用于脚本精确截取），不要概括、不要加标点。
5. 题号用数字数组，按原文题号。
6. 只输出 JSON。

原文：
"""


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
                re.compile(r"(?m)^[ \t]*阅读下面[^\n]{0,30}完成\s*\d")):
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
    out: dict[int, str] = {}
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


def finalize_article(article: str, category: str) -> str:
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

    if category == "古诗" and ti is not None:
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


def build_group(g: dict, body: str, answers: dict[int, str], review: list[str], dots=None, ans_raw: str = "") -> dict | None:
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
        q["answer"] = answers.get(no, "")
        q["analysis"] = ""
        q["score"] = q.get("score") or 0
        # 选项两栏/带续行时，纯正则切不干净 → 让模型把每个选项整段还原
        if q["questionType"] == "single":
            seg_q = _question_segment(body, no)
            if looks_broken(q, seg_q):
                fixed = ai_options(seg_q)
                if fixed:
                    q["options"] = [{"id": k, "label": fixed[k]} for k in "abcd"]
                else:
                    review.append(f"第{no}题选项切分可疑且 AI 未能还原")
        if not q["answer"]:
            review.append(f"第{no}题缺答案")
        if dots is not None and q["questionType"] == "single":
            q["content"] = dots.annotate_fragments(q["content"])
            for o in q["options"]:
                o["label"] = dots.annotate_fragments(o["label"])
        qs.append(q)

    if qtype == "chinese-dictation" and qs:
        # 默写：题干里的每个空 → <Input2 />，每个空一道 input 题（答案按空格拆）
        q0 = qs[0]
        stem = q0["content"]
        n_blank = len(BLANK.findall(stem))
        ans_text = q0["answer"]
        blanks = []
        for part in re.findall(r"[（(]\s*\d+\s*[）)]\s*([^（(]+)", ans_text):
            blanks += [x for x in re.split(r"\s+", part.strip()) if x]
        if not blanks:
            blanks = [x for x in re.split(r"\s+", re.sub(r"[（(]\s*\d+\s*[）)]", " ", ans_text).strip()) if x]
        if n_blank == 0:
            n_blank = len(blanks)
        content = BLANK.sub("<Input2 />", stem)
        inputs = [{"id": f"{q0['id']}-{i+1}", "questionType": "input", "content": "",
                   "options": [], "answer": blanks[i] if i < len(blanks) else "", "score": 1}
                  for i in range(n_blank)]
        if len(blanks) != n_blank:
            review.append(f"默写空数 {n_blank} 与答案数 {len(blanks)} 不一致")
        qs = inputs
        article = content

    if qtype in ("chinese-micro-writing", "chinese-essay") and qs:
        article = qs[0]["content"]

    if cat == "名著" and qs:
        # 名著：材料（判词/选段）单独作题面，多个「(1)(2)…」拆成多道小题，
        # 前端按「材料 + 每小题一个答题框」渲染（不再走左右两栏的阅读区）
        material, sub_qs = split_named_book(qs[0]["content"], raw_answer(ans_raw, nos[0]),
                                            qs[0].get("score") or 0, nos[0], review,
                                            qs[0].get("answer") or "")
        article = material
        qs = sub_qs
        if len(qs) > 1:
            review.append(f"名著：已拆成 {len(qs)} 道小题")

    total = sum(q["score"] for q in qs)
    article = finalize_article(article, cat)
    if dots is not None and article:
        keep = []
        for ln in article.split("\n"):
            t = ln.strip()
            if (not t) or t.startswith(("#", "<")) or t.startswith("注释") or re.match(r"^【\d+】", t):
                keep.append(ln)
            else:
                keep.append(dots.annotate_fragments(ln))
        article = "\n".join(keep)
    return {
        "category": cat, "questionType": qtype,
        "title": g.get("title") or f"{cat}阅读",
        "intro": g.get("intro") or "",
        "content": article, "score": total, "questions": qs,
    }


OUT = {"root": OUT_ROOT}


def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    dots = None
    if args.dots and DocxDots is not None:
        docxs = sorted((PROJECT_ROOT.parent / "gaozhong" / type_dir / "语文").glob(f"{title}*.docx"))
        if docxs:
            try:
                dots = DocxDots(docxs[0])
                log(f"  {title}: 已读取 docx 着重号（{docxs[0].name}）")
            except Exception as e:
                log(f"  {title}: 读取 docx 着重号失败 {str(e)[:80]}")

    plan = {}
    if args.ai:
        plan = chat([{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": PLAN_PROMPT + body[:60000]}], max_tokens=6000)
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not groups:
        raise RuntimeError("结构规划为空")

    answers = parse_answers(ans)
    review: list[str] = []
    out_groups = []
    for g in groups:
        grp = build_group(g, body, answers, review, dots, ans)
        if grp and grp["questions"]:
            out_groups.append(grp)

    year = re.search(r"(20\d{2})", title)
    total = sum(g["score"] for g in out_groups)
    paper = {
        "title": title, "subject": "语文", "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": total or None, "duration": 150,
        "tags": ["语文", args.grade, "二模"] + ([year.group(1)] if year else []),
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"paper": paper, "groups": out_groups}, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text("\n".join(review), encoding="utf-8")
    n = sum(len(g["questions"]) for g in out_groups)
    return f"✓ {title}：{len(out_groups)} 组 / {n} 题（复核 {len(review)} 处）"


def main():
    ap = argparse.ArgumentParser(description="语文试卷 MD → 结构化 JSON")
    ap.add_argument("-p", "--path")
    ap.add_argument("--dir", default=str(MD_ROOT))
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-ai", dest="ai", action="store_false")
    ap.add_argument("--no-dots", dest="dots", action="store_false")
    args = ap.parse_args()

    OUT["root"] = Path(args.output)
    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md"))[:1] if p.is_dir() else [p]
    else:
        root = Path(args.dir) / args.type / "语文"
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir()) if d.is_dir() and sorted(d.glob("*.md"))]
    log(f"共 {len(files)} 份 MD")
    for f in files:
        try:
            log("  " + process(f, args.type, args))
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
