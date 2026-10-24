#!/usr/bin/env python3
"""化学整卷解析（选择 + 非选择），试跑用。

- 选择题：题干+选项，答案取答案表；解析没有就让 AI 按答案生成
- 非选择题：材料+设问，按 (1)(2)… 拆成多道单题（材料复制到每题），答案按小问切分
- 非选择题按题型打分类标签：物质结构与性质 / 化学反应原理 / 有机化学基础 / 工业流程 / 实验探究
用法：python3 convert_chemistry_full.py -p <md> [--force]
"""
import argparse, json, re, time
from pathlib import Path

from convert_chinese_md import split_paper, rewrite_images, chat, log
import convert_politics_md as P
from paths import DATA_ROOT, OUTPUT_JSON_CHEMISTRY_FULL

OUT_ROOT = OUTPUT_JSON_CHEMISTRY_FULL
OUT = {"root": OUT_ROOT}

CATS = ["物质结构与性质", "化学反应原理", "有机化学基础", "工业流程", "实验探究"]
QSPLIT = re.compile(r"(?m)^[ \t]*(\d{1,2})\s*\\?(?:．|、(?!\d)|\.(?!\d|%))\s*")
SUBQ = re.compile(r"(?m)^[ \t]*[（(]\s*(\d{1,2})\s*[）)]\s*")
SCORE_RE = re.compile(r"[（(]\s*(?:共)?\s*(\d+)\s*分\s*[）)]")
SYSTEM = "你是化学试卷录入员，只输出 JSON。"

CAT_PROMPT = r"""下面是若干道高中化学非选择题的题干摘要。请为每题判断属于哪类题型，只能选一个：
物质结构与性质 / 化学反应原理 / 有机化学基础 / 工业流程 / 实验探究
只输出 JSON：{{"题号":"类别"}}。
题目：
ITEM_PLACEHOLDER
"""

ANA_PROMPT = r"""下面是若干道化学选择题（题干、选项、正确答案）。请为每题写一段解析（80-140字）：
正确答案为什么对、其它选项错在哪（涉及方程式/原理要点明）。只输出 JSON：{{"序号":"解析"}}。
题目：
ITEM_PLACEHOLDER
"""


def ai_json(prompt, max_tokens=5000):
    try:
        got = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], max_tokens)
    except Exception as e:
        log(f"    AI 失败：{str(e)[:80]}")
        return {}
    return got if isinstance(got, dict) else {}


def parse_choice_answers(ans: str) -> dict[int, str]:
    """答案表可能分两段（题号1-7 / 8-14），按「题号行 → 答案行」通用解析"""
    out: dict[int, str] = {}
    lines = [l.strip() for l in ans.split("\n")]
    pending: list[int] = []
    for l in lines:
        if not l.startswith("|"):
            continue
        cells = [c.strip() for c in l.strip("|").split("|")]
        if not cells:
            continue
        head = cells[0].replace("*", "")
        if head.startswith("题号"):
            pending = []
            for c in cells[1:]:
                c = re.sub(r"[^0-9]", "", c)
                if c:
                    pending.append(int(c))
        elif head.startswith("答案") and pending:
            vals = [re.sub(r"[^A-D]", "", c.upper()) for c in cells[1:]]
            for n, v in zip(pending, vals):
                if v:
                    out[n] = v
            pending = []
    for m in re.finditer(r"(\d{1,2})\s*[.．]\s*([A-D]{1,4})\b", ans):
        out.setdefault(int(m.group(1)), m.group(2))
    return out


def parse_essay_answers(ans: str) -> dict[int, str]:
    out = {}
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(ans)]
    for i, (s, e, no) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(ans)
        seg = ans[e:nxt].strip()
        seg = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
        out[no] = seg
    return out


def parse_qs(body: str):
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    out = []
    for i, (s, e, no) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        seg = body[e:nxt]
        seg = re.split(r"\n\s*参考答案", seg)[0]
        seg = re.split(r"(?m)^[ \t]*第[二2]部分", seg)[0]
        seg = seg.lstrip("\n \t")
        first = seg.split("\n")[0]
        m = SCORE_RE.search(first)
        score = int(m.group(1)) if m else None
        if m:
            seg = (seg[:m.start()] + seg[m.end():]).lstrip("\n ")
        omarks = [(mm.start(), mm.group(1).lower()) for mm in
                  re.finditer(r"(?:^|[\s\u3000])(?:!\[[^\]]*\]\([^)]*\)\s*)*([A-D])\s*[.．、]\s*", seg, re.M)]
        opts_head = [(mm.start(), mm.group(1)) for mm in
                     re.finditer(r"(?:^|[\s\u3000])(?:!\[[^\]]*\]\([^)]*\)\s*)*([A-D])\s*[.．、]\s*", seg, re.M)]
        uniq = {}
        for pos, k in omarks:
            uniq.setdefault(k, pos)
        if len(uniq) >= 3:
            ordered = sorted((pos, k) for k, pos in uniq.items())
            head = seg[:ordered[0][0]].strip()
            opts = []
            for j, (pos, k) in enumerate(ordered):
                endp = ordered[j + 1][0] if j + 1 < len(ordered) else len(seg)
                txt = re.sub(r"^[\s\u3000]*[A-D]\s*[.．、]\s*", "", seg[pos:endp]).strip()
                opts.append({"id": k, "label": txt})
            merged = {o["id"]: o["label"] for o in opts}
            out.append({"id": no, "questionType": "single", "content": head,
                        "options": [{"id": k, "label": merged.get(k, "")} for k in "abcd"],
                        "score": score or 3})
        else:
            out.append({"id": no, "questionType": "text", "content": seg.strip(), "options": [], "score": score})
    return out


def split_text_qs(q, sub_answers):
    content = q["content"]
    marks = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ.finditer(content)]
    if len(marks) < 2:
        return [q]
    material = content[: marks[0][0]].strip()
    out = []
    for i, (_s, e, no) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(content)
        piece = content[e:end].strip()
        m = SCORE_RE.search(piece)
        score = int(m.group(1)) if m else None
        if m:
            piece = (piece[:m.start()] + piece[m.end():]).strip()
        out.append({
            "id": f"{q['id']}-{no}", "questionType": "text",
            "content": (material + "\n\n" + f"（{no}）" + piece) if material else f"（{no}）" + piece,
            "options": [], "score": score, "answer": sub_answers.get(no, ""),
        })
    return out


def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    qs = parse_qs(body)
    if not qs:
        raise RuntimeError("没切到题目")
    choice_ans = parse_choice_answers(ans)
    essay_ans = parse_essay_answers(ans)

    review = []
    for q in qs:
        if q["questionType"] == "single":
            q["answer"] = choice_ans.get(q["id"], "")
            q["analysis"] = ""
            if not q["answer"]:
                review.append(f"第{q['id']}题缺答案")
    # 选择题解析（AI）
    need = [q for q in qs if q["questionType"] == "single" and not q.get("answer") == ""]
    need = [q for q in qs if q["questionType"] == "single"]
    if need and args.ai:
        items = []
        for idx, q in enumerate(need, start=1):
            opts = "\n".join(f"{o['id'].upper()}．{o['label']}" for o in q["options"] if o["label"])
            items.append(f"【{idx}】{q['content']}\n{opts}\n正确答案：{q['answer']}")
        got = ai_json(ANA_PROMPT.replace("ITEM_PLACEHOLDER", "\n\n".join(items)[:12000]), max_tokens=8000)
        for idx, q in enumerate(need, start=1):
            v = str(got.get(str(idx)) or "").strip()
            if v:
                q["analysis"] = v

    # 非选择题：拆小问 + 分类
    texts = [q for q in qs if q["questionType"] == "text"]
    if texts:
        lines = "\n".join(f"- {q['id']}｜{re.sub(r'\\s+', ' ', q['content'])[:120]}" for q in texts)
        cats = ai_json(CAT_PROMPT.replace("ITEM_PLACEHOLDER", lines))
        cat_map = {}
        for k, v in (cats or {}).items():
            try:
                n = int(re.sub(r"\D", "", str(k)))
            except Exception:
                continue
            cat_map[n] = str(v).strip() if str(v).strip() in CATS else "实验探究"

    expanded = []
    for q in qs:
        if q["questionType"] == "single":
            expanded.append(q)
            continue
        raw_ans = essay_ans.get(q["id"], "")
        q["answer"] = raw_ans
        q["analysis"] = ""
        sub_marks = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ.finditer(raw_ans)]
        sub_ans = {}
        for i, (_s, e, no) in enumerate(sub_marks):
            end = sub_marks[i + 1][0] if i + 1 < len(sub_marks) else len(raw_ans)
            seg = raw_ans[e:end].strip()
            seg = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", seg).strip()
            sub_ans[no] = seg
        subs = split_text_qs(q, sub_ans)
        total_score = q.get("score") or 0
        if subs and total_score > 0:
            if all(not (sq.get("score") or 0) for sq in subs):
                per = max(1, total_score // len(subs))
                for sq in subs:
                    sq["score"] = per
            diff = total_score - sum(sq.get("score") or 0 for sq in subs)
            if diff and subs:
                subs[-1]["score"] = (subs[-1].get("score") or 0) + diff
        for sq in subs:
            sq["module"] = cat_map.get(q["id"], "实验探究")
            sq["analysis"] = ""
            if not sq["answer"]:
                sq["answer"] = raw_ans
            expanded.append(sq)
    qs = expanded

    for q in qs:
        q["content"] = rewrite_images(q["content"], md.parent, title, type_dir, review, "化学")
        for o in q["options"]:
            o["label"] = rewrite_images(str(o["label"]), md.parent, title, type_dir, review, "化学")
        if q.get("answer"):
            q["answer"] = rewrite_images(str(q["answer"]), md.parent, title, type_dir, review, "化学")

    choice_qs = [q for q in qs if q["questionType"] == "single"]
    groups = []
    if choice_qs:
        groups.append({"category": "选择", "questionType": "chemistry-full", "title": "选择题",
                       "content": "", "score": sum(q["score"] or 0 for q in choice_qs), "questions": choice_qs})
    for cat in CATS:
        sub = [q for q in qs if q["questionType"] == "text" and q["module"] == cat]
        if sub:
            groups.append({"category": cat, "questionType": "chemistry-full", "title": cat,
                           "content": "", "score": sum(q["score"] or 0 for q in sub), "questions": sub})

    year = re.search(r"(20\d{2})", title)
    paper = {"title": title, "subject": "化学", "source": title, "grade": args.grade,
             "year": int(year.group(1)) if year else None,
             "totalScore": sum(g["score"] for g in groups) or None, "duration": 90,
             "tags": ["化学", args.grade, "二模"] + ([year.group(1)] if year else [])}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"paper": paper, "groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text("\n".join(review), encoding="utf-8")
    return f"✓ {title}：{len(groups)} 组 / {len(qs)} 题（选择 {len(choice_qs)}，非选择 {len(qs)-len(choice_qs)}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path")
    ap.add_argument("--dir", default=str(DATA_ROOT / "output_docx_to_md"))
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
        root = Path(args.dir) / "二模" / "化学"
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir()) if d.is_dir() and sorted(d.glob("*.md"))]
    log(f"共 {len(files)} 份 MD")
    for f in files:
        try:
            log("  " + process(f, args.type, args))
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
