#!/usr/bin/env python3
"""用 AI 读 PDF 页重建「默写」板块（不做任何文本匹配）。

产品口径（2026-11-07）：默写的空位一律交给视觉模型按**卷面实际**读出，不再用空格串/下划线/答案反向匹配。
模型要返回结构化 JSON：每道小题的「题干原文（把要填的地方写成 [空]）」+ 该小题的答案列表。

用法：
    python3 ai_dictation.py --scan
    python3 ai_dictation.py --apply "二模/2023北京丰台高三二模语文.json"
    python3 ai_dictation.py --apply-all          # 范围内全部（一模/上期末 2025-2026、二模 2021-2026、真题）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fix_pairs_choice as base

BLANK = "<Input2></Input2>"
LOCK = "[空]"

PROMPT = """图片是语文试卷的一页（可能包含「默写」题）。请找到「在横线处填写作品原句」这类默写题，
把**卷面上每一处需要学生填写的横线**都标出来。

严格按下面 JSON 输出，不要任何解释：
{{"found": true, "items": [
  {{"no": "①", "text": "《桂枝香 金陵怀古》中“[空]，[空]，画图难足”几句，动静相生……（出处）"}},
  {{"no": "②", "text": "……"}}
], "answers": [["彩舟云淡","星河鹭起"], ["正道直行","谗人间之"]]}}

要求：
1. `items[].text` = 该小题的完整题干文字，**把每个需要填写的位置写成 [空]**（横线有多长不重要，一处写一个 [空]）；
2. `answers` 与 `items` 一一对应，每个小题给一个数组，按顺序列出该小题每个空的答案；答案取**试卷答案区**（本页或后几页可能印着参考答案）；
3. 题干里的书名、引号、标点保持原样；不要把下一道大题（如「（一）阅读下面作品，完成16-19题」）的内容写进 text；
4. 找不到默写题就输出 {{"found": false}}。
"""


def page_candidates(pdf: Path) -> list[int]:
    """含默写关键词的页码（最多 3 页：题干页 + 可能的答案页）。"""
    pages = base.page_texts(pdf)
    hits = []
    for i, p in enumerate(pages, start=1):
        if re.search(r"填写作品原句|写出诗文原句|名篇名句默写|补写出下列", p):
            hits.append(i)
    if not hits:
        return []
    out = hits[:1]
    # 答案区通常在后面，追加包含「【解答】」或「评分标准」且提到"分）"的页
    for i, p in enumerate(pages, start=1):
        if i in out:
            continue
        if re.search(r"【解答】|参考答案|评分标准", p) and re.search(r"\d+．?\s*[（(]\s*\d+\s*分", p):
            out.append(i)
            if len(out) >= 3:
                break
    return out


def ask_dictation(pdf: Path, pages: list[int]) -> dict:
    results = {"items": [], "answers": []}
    import tempfile
    for pg in pages:
        with tempfile.TemporaryDirectory() as td:
            img = base.render_page(pdf, pg, Path(td), dpi=160)
            if not img:
                continue
            try:
                res = base.ask_vision(img, "在横线处填写作品原句", prompt=PROMPT)
            except Exception as e:
                print(f"   ! 第 {pg} 页视觉失败：{str(e)[:60]}")
                continue
        items = res.get("items") or []
        answers = res.get("answers") or []
        if items and not results["items"]:
            results["items"] = items
        if answers:
            # 取最完整的一份答案
            if sum(len(a) for a in answers) >= sum(len(a) for a in results["answers"]):
                results["answers"] = answers
        if results["items"] and results["answers"]:
            break
    return results


def build_section(items: list, answers: list) -> tuple[str, list[dict]] | None:
    texts = []
    flat_answers: list[str] = []
    for i, it in enumerate(items):
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        n_blank = text.count(LOCK)
        if n_blank == 0:
            continue
        ans = list(answers[i]) if i < len(answers) and isinstance(answers[i], list) else []
        # 答案个数与空位数不一致 → 用文本里的顺序补齐/截断
        if len(ans) < n_blank:
            ans = ans + [""] * (n_blank - len(ans))
        ans = ans[:n_blank]
        texts.append(text.replace(LOCK, BLANK))
        flat_answers.extend(ans)
    if not texts:
        return None
    content = "\n\n".join(texts)
    qs = [
        {"id": i + 1, "questionType": "input", "content": "", "answer": a, "analysis": "", "score": 1}
        for i, a in enumerate(flat_answers)
    ]
    return content, qs


def handle(json_path: Path, apply: bool, require: int | None = None, min_blanks: int | None = None) -> str:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    secs = [s for s in (data.get("sections") or []) if s.get("category") == "默写"]
    if not secs:
        return f"{json_path.stem}：没有默写板块"
    pdf = base.locate_pdf(json_path)
    if not pdf:
        return f"{json_path.stem}：找不到 PDF"
    pages = page_candidates(pdf)
    if not pages:
        return f"{json_path.stem}：PDF 里没定位到默写题"
    res = ask_dictation(pdf, pages)
    built = build_section(res.get("items") or [], res.get("answers") or [])
    if not built:
        return f"{json_path.stem}：AI 未读出默写（pages={pages}）"
    content, qs = built
    old = len(secs[0].get("questions") or [])
    msg = (f"{json_path.stem}：AI 读出 {content.count('<Input2')} 空 / {len(qs)} 问"
           f"（原 {old} 问）｜空答案 {sum(1 for q in qs if not q['answer'])} 个")
    n_new = content.count("<Input2")
    n_old = len(secs[0].get("questions") or [])
    if require is not None and n_new != require:
        return f"{json_path.stem}：AI 读出 {n_new} 空 ≠ 卷面 {require} 分，**不写回**"
    if min_blanks is not None and n_new < min_blanks:
        return f"{json_path.stem}：AI 只读出 {n_new} 空 < 现有 {min_blanks}，**不写回**"
    if sum(1 for q in qs if not q["answer"]) > 0:
        return f"{json_path.stem}：AI 结果有 {sum(1 for q in qs if not q['answer'])} 个空缺答案，**不写回**"
    if apply:
        for s in secs:
            s["content"] = content
            s["questions"] = qs
            s["score"] = len(qs)
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        msg += " → 已写回"
    return msg


def in_scope(json_path: Path) -> bool:
    typ = json_path.parent.name
    title = json_path.stem
    m = re.match(r"(\d{4})", title)
    year = int(m.group(1)) if m else 0
    if typ == "真题":
        return "北京" in title
    if typ in ("一模", "上期末"):
        return year >= 2025
    if typ == "二模":
        return year >= 2021
    return False


def targets_all() -> list[Path]:
    return [f for f in sorted(base.JSON_ROOT.glob("*/*.json"))
            if ".old-groups" not in f.name and in_scope(f)
            and '"默写"' in f.read_text(encoding="utf-8")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", nargs="+", default=[])
    ap.add_argument("--apply-all", action="store_true")
    ap.add_argument("--require", type=int, default=None, help="只用当空位数等于该值时才写回")
    ap.add_argument("--min-blanks", type=int, default=None, help="空位数不少于该值时才写回（防 AI 读漏）")
    args = ap.parse_args()

    if args.scan:
        for f in targets_all():
            print(handle(f, apply=False))
        return 0
    targets = targets_all() if args.apply_all else [
        (Path(a) if Path(a).is_absolute() else base.JSON_ROOT / a) for a in args.apply
    ]
    for t in targets:
        print(handle(t, apply=True, require=args.require, min_blanks=args.min_blanks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
