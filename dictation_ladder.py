#!/usr/bin/env python3
"""默写板块的「阶梯式」重建（md 优先，AI 兜底，最后才看 PDF）。

产品口径（2026-11-07，用户明确）：
1. **先按常见空格式提取**：md 里的转义下划线串 `\\_**\\_**\\__`、全角/半角空格串、引号/标点间单空格；
2. **题目没提出来** → 把「该大题的**题目部分 md**」交给 AI 读（让模型按卷面标 `[空]`）；
3. **答案没提出来** → 把「该大题的**答案部分 md**」（或整份答案区）交给 AI 读，**允许 AI 回答“找不到”**；
4. AI 用普通方式试 2–3 次仍找不到 → 才发 **PDF 页面图**给视觉模型。

对外只有两个入口：
    build_dictation(md_text, pdf_path=None, use_ai=True, cache_dir=None) -> Section | None
    Section = {"content": str, "questions": [{id, questionType, content, answer, analysis, score}]}
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

BLANK = "<Input2></Input2>"

HEAD = re.compile(r"(在横线处(填写|写出)作品原句|写出诗文原句|名篇名句默写|补写出下列|默写常见的名句名篇)")
STOP = re.compile(
    r"^(?:[一二三四五六七八九十]、\s*本大题|（[一二三四五]）\s*阅读|阅读下面(?:作品|文章|的)|"
    r"[三四五六]、|参考(?:答案|译文)|答案(?:与解析)?|【参考译文】)"
)
# 空位写法（按可靠性排序）
BLANK_RUN = re.compile(
    r"[\\_*]{4,}"                                                            # \_**\_**\__
    r"|(?<=[\u4e00-\u9fff，。；？！、）”》])[ \u3000]{2,}(?=[\u4e00-\u9fff，。；？！、（“《])"
    r"|(?<=[\u201c\u201d，。；：？！\"])[ \u3000]{1,3}(?=[\u201c\u201d，。；：？！、\"])"
)
ITEM_MARK = re.compile(r"^([①-⑩]|（\d{1,2}）|\(\d{1,2}\))")
GROUP_MARK = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")


# ─────────────────────────── 1. md 常规提取 ───────────────────────────
def split_question_answer(md: str) -> tuple[str, str]:
    """把 md 切「题目部分 / 答案部分」。"""
    cut = -1
    for pat in ("参考答案", "答案与解析", "【参考译文】", "\n答案\n"):
        i = md.find(pat)
        if i > 0:
            cut = i
            break
    return (md[:cut], md[cut:]) if cut > 0 else (md, "")


def question_block(qmd: str) -> str | None:
    lines = qmd.split("\n")
    start = None
    for i, l in enumerate(lines):
        if HEAD.search(l):
            start = i
            break
    if start is None:
        return None
    out = []
    for l in lines[start:start + 40]:
        if out and STOP.match(l.strip()):
            break
        out.append(l)
    return "\n".join(out)


def md_items(block: str) -> list[str]:
    items: list[str] = []
    cur: str | None = None
    for raw in block.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if ITEM_MARK.match(line):
            if cur:
                items.append(cur)
            cur = line
        elif cur is not None:
            cur += line
    if cur:
        items.append(cur)
    return items


def blank_out(text: str) -> tuple[str, int]:
    n = 0

    def sub(_m):
        nonlocal n
        n += 1
        return BLANK

    return BLANK_RUN.sub(sub, text), n


def extract_blanks(qmd: str) -> tuple[str, int]:
    """常规提取：返回 (带空位的正文, 空位数)。"""
    block = question_block(qmd)
    if not block:
        return "", 0
    texts, total = [], 0
    for it in md_items(block):
        t, n = blank_out(it)
        if n:
            texts.append(t)
            total += n
    return "\n\n".join(texts), total


ANSWER_GROUP = re.compile(r"(?m)^\s*(?:[①②③④⑤⑥⑦⑧⑨⑩]|（\d{1,2}）|\(\d{1,2}\)|\d{1,2}\s*[．.])\s*")


BAD_ANSWER_HINTS = ("答案要点", "评分标准", "解析", "本题考查", "方面", "作用", "影响", "启示", "分）", "示例", "概括", "赏析")
# 只挡「明显是分析/答案要点」的说法；单个虚词不作判据（默写原句里也可能有「可以」）
MODERN_HINTS = ("我们", "他们", "应该", "通过", "进行", "其中", "因此", "并且", "方面", "机制")


def looks_like_dictation(answers: list[str]) -> bool:
    """默写答案应当是短的诗文片段；出现分析性文字/明显重复就说明取错了答案段。"""
    if not answers:
        return False
    if len(set(a.strip() for a in answers)) < len(answers) * 0.8:   # 大量重复 = 切错了
        return False
    for a in answers:
        a = str(a or "").strip()
        if not a:
            continue
        if len(a) > 20:
            return False
        if any(k in a for k in BAD_ANSWER_HINTS):
            return False
        if any(k in a for k in MODERN_HINTS):
            return False
    return True


def extract_answers(amd: str, need: int, qno: str | None = None) -> list[str]:
    """常规提取答案：按 ①②③ / （1）（2） 分组，去掉（重点字：…）等批注。"""
    if not amd or need <= 0:
        return []
    # 优先按题号锚定（如「15.（8 分）①…」），避免抓到前面文言文的答案
    if qno:
        m2 = re.search(rf"(?m)^\s*{re.escape(qno)}\s*[．.]?\s*[（(]?\s*(?:\d{{1,2}}\s*分)?\s*[）)]?\s*[①②③④⑤⑥⑦⑧⑨⑩]", amd)
        if m2:
            amd = amd[m2.start():]
    # 只保留默写题的答案段：从第一个 ①②③ 或「（N 分）」开始
    body = amd
    m = re.search(r"(?:默写|填写作品原句|写出诗文原句)", body)
    if m:
        body = body[m.start():m.start() + 1500]
    groups = [g for g in re.split(r"(?=[①②③④⑤⑥⑦⑧⑨⑩])", body) if g.strip().startswith(tuple("①②③④⑤⑥⑦⑧⑨⑩"))]
    out: list[str] = []
    for g in groups:
        g = re.sub(r"^[①-⑩]\s*", "", g)
        g = re.split(r"【|（\d+分）|\(\d+分\)|\n\n|【解析】|【分析】", g)[0]
        g = re.sub(r"[（(]\s*重点[^）)]*[）)]", "", g)
        parts = [p.strip() for p in re.split(r"[，,、；;\s\u3000]+", g) if len(p.strip()) >= 2]
        out.extend(parts)
    if len(out) < need:
        for m in re.finditer(r"[（(]\s*\d{1,2}\s*[）)]\s*([^\n（(]{2,40})", body):
            for p in re.split(r"[，,、；;]", m.group(1)):
                p = p.strip()
                if len(p) >= 2:
                    out.append(p)
    res = out[:need] if len(out) >= need else out
    if not looks_like_dictation(res):
        return []
    return res


# ─────────────────────── 2/3/4. AI 阶梯（md → md → PDF）───────────────────────
PROMPT_ITEMS = """下面是语文试卷里「默写」大题的**题目部分文本**。请把每一处需要学生填写的横线标出来。

严格输出 JSON：{{"found": true, "items": ["小题1题干（把每处横线写成 [空]）", "小题2题干…"]}}
- 按卷面小题顺序（①②③ 或（1）（2）），每题一个字符串；
- **只在真的有横线的地方写 [空]**，一处横线一个 [空]；
- 找不到默写题就输出 {{"found": false}}。

题目文本：
{text}
"""

PROMPT_ANSWERS = """下面是语文试卷「默写」大题的**参考答案部分文本**。请按小题顺序列出每空的答案。

严格输出 JSON：{{"found": true, "answers": ["第1空答案", "第2空答案", …]}}
- 顺序与卷面小题一致（①②③ 或（1）（2）），一空一个字符串；
- 去掉「（重点字：…）」「【评分标准】…」这类批注；
- 找不到答案就输出 {{"found": false}}。

答案文本：
{text}
"""

PROMPT_PDF = """图片是语文试卷的一页。请找到「默写」题，按卷面把每处横线标成 [空]，并给出答案。

严格输出 JSON：{{"found": true, "items": ["题干（含 [空]）", …], "answers": ["第1空答案", …]}}
找不到就输出 {{"found": false}}。
"""


def _cache_path(cache_dir: Path | None, key: str) -> Path | None:
    if not cache_dir:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")


def _ai_json(payload_prompt: str, cache_dir: Path | None, cache_key: str, make_messages) -> dict:
    cp = _cache_path(cache_dir, cache_key)
    if cp and cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        data = make_messages(payload_prompt)
    except Exception:
        return {"found": False}
    if cp:
        try:
            cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return data


def _chat_json(user_prompt: str) -> dict:
    """纯文本调用（复用 fix_pairs_choice 的网关配置）。"""
    import requests
    import fix_pairs_choice as base

    resp = requests.post(
        f"{base.BASE_URL}/chat/completions",
        json={
            "model": base.MODEL, "max_tokens": 2000, "temperature": 0.0, "reasoning_effort": "none",
            "messages": [
                {"role": "system", "content": "你是语文试卷录入员，只输出 JSON。"},
                {"role": "user", "content": user_prompt},
            ],
        },
        headers={"Authorization": f"Bearer {base.API_KEY}"},
        timeout=300,
    )
    resp.raise_for_status()
    txt = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {"found": False}
    except Exception:
        return {"found": False}


def ai_items_from_qmd(qmd: str, cache_dir: Path | None = None) -> list[str]:
    block = question_block(qmd)
    if not block:
        return []
    data = _ai_json("", cache_dir, "items:" + block[:400], lambda _p: _chat_json(PROMPT_ITEMS.format(text=block[:3000])))
    if not data.get("found"):
        return []
    items = [str(x) for x in (data.get("items") or []) if str(x).strip()]
    return items if any("[空]" in i for i in items) else []


def ai_answers_from_amd(amd: str, cache_dir: Path | None = None) -> list[str]:
    if not amd.strip():
        return []
    # 只把「默写题号段」发给模型，避免读到别的板块答案
    _m = re.search(r"(?m)^\s*(\d{1,2})\s*[．.]\s*[（(]?\s*(?:\d{1,2}\s*分)?\s*[）)]?\s*[①②③④⑤⑥⑦⑧⑨⑩]", amd)
    if _m:
        amd = amd[_m.start():_m.start() + 800]
    text = amd[:4000]
    data = _ai_json("", cache_dir, "answers:" + text[:400], lambda _p: _chat_json(PROMPT_ANSWERS.format(text=text)))
    if not data.get("found"):
        return []
    got = [str(a).strip() for a in (data.get("answers") or []) if str(a).strip()]
    return got if looks_like_dictation(got) else []


def ai_from_pdf(pdf: Path, cache_dir: Path | None = None, tries: int = 2) -> tuple[list[str], list[str]]:
    import fix_pairs_choice as base

    if not pdf or not Path(pdf).exists():
        return [], []
    pages = base.page_texts(pdf)
    cand = [i for i, p in enumerate(pages, start=1) if HEAD.search(p)][:1]
    cand += [i for i, p in enumerate(pages, start=1)
             if re.search(r"【解答】|参考答案", p) and re.search(r"\d+\s*[．.]?\s*[（(]\s*\d+\s*分", p)][:2]
    for pg in cand[:3]:
        for _ in range(tries):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                img = base.render_page(pdf, pg, Path(td), dpi=170)
                if not img:
                    continue
                try:
                    data = base.ask_vision(img, "在横线处填写作品原句", prompt=PROMPT_PDF)
                except Exception:
                    continue
            if data.get("found"):
                items = [str(x) for x in (data.get("items") or []) if str(x).strip()]
                answers = [str(a).strip() for a in (data.get("answers") or []) if str(a).strip()]
                if items and any("[空]" in i for i in items):
                    return items, answers
    return [], []


# ─────────────────────────── 组装 + 校验 ───────────────────────────
def assemble(items: list[str], answers: list[str]) -> dict | None:
    """items：每题题干（含若干 [空]）；answers：拍平的答案列表，按顺序分配到各空。"""
    texts: list[str] = []
    flat: list[str] = []
    cursor = 0
    flat_ans = list(answers or [])
    for it in items:
        n = it.count("[空]")
        if n == 0:
            continue
        chunk = flat_ans[cursor:cursor + n]
        cursor += n
        chunk = list(chunk) + [""] * max(0, n - len(chunk))
        texts.append(it.replace("[空]", BLANK))
        flat.extend(chunk[:n])
    if not texts:
        return None
    return {
        "content": "\n\n".join(texts),
        "questions": [
            {"id": i + 1, "questionType": "input", "content": "", "answer": a, "analysis": "", "score": 1}
            for i, a in enumerate(flat)
        ],
    }


def expected_blanks(block: str) -> tuple[int | None, bool]:
    """从题干块里读「（N 分）」作为期望空数；同时判断是否「限选 N 道题」。"""
    m = re.search(r"[（(]\s*(?:共\s*)?(\d{1,2})\s*分\s*[）)]", block or "")
    limited = bool(re.search(r"限选", block or ""))
    return (int(m.group(1)) if m else None), limited


def _acceptable(sec: dict | None, expect: int | None, limited: bool) -> bool:
    if not sec:
        return False
    n = len(sec["questions"])
    if any(not q["answer"] for q in sec["questions"]):
        return False
    if expect is None:
        return n > 0
    if limited:
        return n >= expect and n % 2 == 0
    return n == expect


def question_number(block: str) -> str | None:
    m = re.search(r"(?m)^\s*(\d{1,2})\s*[．.]", block or "")
    return m.group(1) if m else None


def build_dictation(md_text: str, pdf: Path | None = None, use_ai: bool = True,
                    cache_dir: Path | None = None) -> tuple[dict | None, str]:
    """阶梯式重建，返回 (section, 采用的方式)。"""
    qmd, amd = split_question_answer(md_text)
    blk = question_block(qmd) or ""
    expect, limited = expected_blanks(blk)
    qno = question_number(blk)

    # ① md 常规提取
    content, n = extract_blanks(qmd)
    if n:
        answers = extract_answers(amd, n, qno)
        if len(answers) >= n:
            sec = assemble([c.replace(BLANK, "[空]") for c in content.split("\n\n")], answers[:n])
            if _acceptable(sec, expect, limited):
                return sec, "md-regular"
        # 答案为空 → 进 AI 读答案
        if use_ai:
            ai_ans = ai_answers_from_amd(amd, cache_dir)
            if len(ai_ans) >= n:
                sec = assemble([c.replace(BLANK, "[空]") for c in content.split("\n\n")], ai_ans)
                if _acceptable(sec, expect, limited):
                    return sec, "md-question + ai-answers"

    if not use_ai:
        return None, "no-ai"

    # ② 题目没提出来 → AI 读题目部分 md
    items = ai_items_from_qmd(qmd, cache_dir)
    answers = extract_answers(amd, sum(i.count("[空]") for i in items), qno) if items else []
    if items and len(answers) >= sum(i.count("[空]") for i in items):
        sec = assemble(items, answers)
        if _acceptable(sec, expect, limited):
            return sec, "ai-items(md) + md-answers"

    # ③ 答案没提出来 → AI 读答案部分 md
    if items:
        ai_ans = ai_answers_from_amd(amd, cache_dir)
        need = sum(i.count("[空]") for i in items)
        if len(ai_ans) >= need:
            sec = assemble(items, ai_ans)
            if _acceptable(sec, expect, limited):
                return sec, "ai-items(md) + ai-answers(md)"

    # ④ md 都不行 → PDF 视觉（AI 普通方式已试过 2–3 次）
    items2, answers2 = ai_from_pdf(pdf, cache_dir) if pdf else ([], [])
    if items2:
        sec = assemble(items2, answers2)
        if _acceptable(sec, expect, limited):
            return sec, "pdf-vision"
    return None, "failed"
