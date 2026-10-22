#!/usr/bin/env python3
"""政治试卷（教师版）docx→MD → 结构化 JSON

- 选择题：题干+选项，答案取「参考答案」里的字母；解析有就抄、没有就让 AI 按答案生成
- 解答题：材料+设问整段作题干（政治不做阅读分栏），答案取答案区原文
- 每题都打模块标签：中特 / 经济社会 / 政治法治 / 哲学文化 / 国政国经 / 法律生活 / 逻辑思维
  （「综合运用所学」归到中特）

输出：output_json_politics/<类型>/<卷>.json
  {paper:{...}, groups:[{category, questionType:"politics", questions:[{id,questionType,content,options,answer,analysis,score,module}]}]}

用法：python3 convert_politics_md.py -p <md路径> [-T 二模]
"""
import argparse, json, re, time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")
from convert_chinese_md import rewrite_images, split_paper, chat, log
import convert_chinese_md as C

MD_ROOT = PROJECT_ROOT.parent / "output_docx_to_md"
OUT_ROOT = PROJECT_ROOT.parent / "output_json_politics"
OUT = {"root": OUT_ROOT}

MODULES = ["中特", "经济社会", "政治法治", "哲学文化", "国政国经", "法律生活", "逻辑思维"]

QSPLIT = re.compile(r"(?m)^[ \t\u3000\u00a0]*(?:(?:!\[[^\]]*\]\([^)]*\)|⟦IMG\d+⟧)[ \t]*)*\**[ \t]*(\d{1,2})[ \t]*[.．、][ \t]*")
OPT_LINE = re.compile(r"(?m)^[ \t]*([A-D])\s*[.．、]\s*(.+)$")
SCORE_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")

SYSTEM = "你是政治试卷录入员，只输出 JSON。"

MODULE_PROMPT = r"""下面是一份北京高三政治二模试卷的若干题目（题号｜题型｜题干摘要）。
请为**每题**判断它考查的模块，只能从这 7 个里选一个：
中特（中国特色社会主义）、经济社会（经济与社会）、政治法治（政治与法治）、
哲学文化（哲学与文化）、国政国经（当代国际政治与经济）、法律生活（法律与生活）、逻辑思维（逻辑与思维）。

注意：题目若写「综合运用所学」「运用所学知识」而看不出单一模块，归到「中特」。

只输出 JSON：{{"题号":"模块"}}（题号用字符串，模块用上面 7 个词之一）。

题目：
ITEM_PLACEHOLDER
"""

CHOICE_ANA_PROMPT = r"""下面是若干道政治选择题（题干、四个选项、正确答案）。请为每题写一段解析（80-150字，中文专业简洁）：
- 说明正确选项为什么对（点出教材原理/材料依据）；
- 简要说明其他选项错在哪。
不要复述题干。只输出 JSON：{{"题号":"解析"}}。

题目：
ITEM_PLACEHOLDER
"""


def ai_json(prompt: str, max_tokens=4000) -> dict:
    try:
        got = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], max_tokens)
    except Exception as e:
        log(f"    AI 调用失败：{str(e)[:80]}")
        return {}
    return got if isinstance(got, dict) else {}


def parse_choices(ans: str) -> dict[int, str]:
    """从答案区解析选择题答案（支持「1．A 2．D …」一行多个）"""
    out = {}
    for m in re.finditer(r"(\d{1,2})\s*[.．、]\s*([A-D]{1,4})(?=[\s\d]|$)", ans):
        out[int(m.group(1))] = m.group(2)
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


def parse_questions(body: str):
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    out = []
    for i, (s, e, no) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        seg = body[e:nxt]
        seg = re.split(r"\n\s*参考答案", seg)[0]
        # 分值
        first = seg.split("\n")[0]
        m = SCORE_RE.search(first)
        score = int(m.group(1)) if m else None
        if m:
            seg = (seg[:m.start()] + seg[m.end():]).lstrip("\n ")
        # 选择题？（选项可能一行里挤着 A．①② B．①④ C．②③ D．③④）
        omarks = [(m.start(), m.group(1).lower()) for m in re.finditer(r"(?:^|[\s\u3000])([A-D])[．.、]", seg, re.M)]
        uniq = {}
        for pos, k in omarks:
            uniq.setdefault(k, pos)
        if len(uniq) >= 3:
            ordered_marks = sorted(((pos, k) for k, pos in uniq.items()))
            head = seg[:ordered_marks[0][0]].strip()
            opts = []
            for i, (pos, k) in enumerate(ordered_marks):
                endp = ordered_marks[i + 1][0] if i + 1 < len(ordered_marks) else len(seg)
                txt = re.sub(r"^[\s\u3000]*[A-D][．.、]\s*", "", seg[pos:endp]).strip()
                opts.append({"id": k, "label": txt})
            merged = {o["id"]: o["label"] for o in opts}
            ordered = [{"id": k, "label": merged.get(k, "")} for k in "abcd"]
            out.append({"id": no, "questionType": "single", "content": head, "options": ordered,
                        "score": score or 3})
        else:
            out.append({"id": no, "questionType": "text", "content": seg.strip(), "options": [], "score": score})
    return out


def process(md: Path, type_dir: str, args) -> str:
    title = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem).strip()
    title = re.sub(r"\s+", "", title)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"
    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    body = re.sub(r"<su[bp]>\s*([.．、])\s*</su[bp]>", r"\1", body)

    qs = parse_questions(body)
    if not qs:
        raise RuntimeError("没切到题目")
    choice_ans = parse_choices(ans)
    essay_ans = parse_essay_answers(ans)

    # 1) 模块分类（一次调用）
    lines = "\n".join(f"- {q['id']}｜{'选择' if q['questionType']=='single' else '解答'}｜"
                      f"{re.sub(r'\s+', ' ', q['content'])[:120]}" for q in qs)
    modules = ai_json(MODULE_PROMPT.replace("ITEM_PLACEHOLDER", lines))
    mod_map = {}
    for k, v in (modules or {}).items():
        v2 = str(v).strip()
        try:
            mod_map[int(re.sub(r"\D", "", str(k)))] = v2 if v2 in MODULES else "中特"
        except Exception:
            continue

    # 2) 答案 + 解析
    for q in qs:
        no = q["id"]
        if q["questionType"] == "single":
            q["answer"] = choice_ans.get(no, "")
            q["analysis"] = ""
        else:
            q["answer"] = essay_ans.get(no, "")
            q["analysis"] = ""
        q["module"] = mod_map.get(no, "中特")

    # 2b) 选择题解析：答案区自带就抄，没有就 AI 生成
    need = []
    for q in qs:
        if q["questionType"] != "single":
            continue
        blk = essay_ans.get(q["id"], "")
        rest = re.sub(r"^[A-D][\s、，,]*", "", blk).strip()
        if len(rest) >= 12:
            q["analysis"] = rest
        else:
            need.append(q)
    if need and args.ai_analysis:
        items = []
        for idx, q in enumerate(need, start=1):
            opts = "\n".join(f"{o['id'].upper()}．{o['label']}" for o in q["options"] if o["label"])
            items.append(f"【{idx}】{q['content']}\n{opts}\n正确答案：{q['answer']}")
        got = ai_json(CHOICE_ANA_PROMPT.replace("ITEM_PLACEHOLDER", "\n\n".join(items)[:12000]), max_tokens=6000)
        for idx, q in enumerate(need, start=1):
            v = str(got.get(str(idx)) or "").strip()
            if v:
                q["analysis"] = v

    # 3) 图片改写（政治卷有图表）
    review = []
    for q in qs:
        q["content"] = rewrite_images(q["content"], md.parent, title, type_dir, review)
        for o in q["options"]:
            o["label"] = rewrite_images(str(o["label"]), md.parent, title, type_dir, review)

    # 4) 分组：选择题一组 + 每个模块的解答题一组
    choice_qs = [q for q in qs if q["questionType"] == "single"]
    essay_qs = [q for q in qs if q["questionType"] == "text"]
    groups = []
    if choice_qs:
        groups.append({"category": "选择", "questionType": "politics", "title": "选择题",
                       "content": "", "score": sum(q["score"] or 0 for q in choice_qs), "questions": choice_qs})
    for mod in MODULES:
        sub = [q for q in essay_qs if q["module"] == mod]
        if sub:
            groups.append({"category": mod, "questionType": "politics", "title": f"{mod}·解答题",
                           "content": "", "score": sum(q["score"] or 0 for q in sub), "questions": sub})

    year = re.search(r"(20\d{2})", title)
    paper = {
        "title": title, "subject": "政治", "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": sum(g["score"] for g in groups) or None, "duration": 90,
        "tags": ["政治", args.grade, "二模"] + ([year.group(1)] if year else []),
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"paper": paper, "groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        dst.with_suffix(".review.txt").write_text("\n".join(review), encoding="utf-8")
    n = len(qs)
    return f"✓ {title}：{len(groups)} 组 / {n} 题（选择 {len(choice_qs)}，解答 {len(essay_qs)}）"


def main():
    ap = argparse.ArgumentParser(description="政治试卷 MD → 结构化 JSON")
    ap.add_argument("-p", "--path")
    ap.add_argument("--dir", default=str(MD_ROOT))
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-ai-analysis", dest="ai_analysis", action="store_false")
    args = ap.parse_args()

    OUT["root"] = Path(args.output)
    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md"))[:1] if p.is_dir() else [p]
    else:
        root = Path(args.dir) / "二模" / "政治"
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir()) if d.is_dir() and sorted(d.glob("*.md"))]
    log(f"共 {len(files)} 份 MD")
    for f in files:
        try:
            log("  " + process(f, args.type, args))
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
