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


def parse_question(body: str, qno: int) -> dict | None:
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    hit = next(((s, e) for s, e, n in marks if n == qno), None)
    if not hit:
        return None
    s, e = hit
    nxt = next((s2 for s2, _, n in marks if n > qno), len(body))
    seg = body[e:nxt]
    seg = re.split(r"\n\s*参考答案", seg)[0]
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


def build_group(g: dict, body: str, answers: dict[int, str], review: list[str]) -> dict | None:
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
        if not q["answer"]:
            review.append(f"第{no}题缺答案")
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

    total = sum(q["score"] for q in qs)
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
        grp = build_group(g, body, answers, review)
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
