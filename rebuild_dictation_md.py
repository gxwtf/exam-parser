#!/usr/bin/env python3
"""从 **md** 重建「默写」板块（不读图、不调 AI、不做答案反向匹配）。

卷面转换出来的 md 里，默写题的横线是稳定的 `\\_**\\_**\\_**\\__` 这类「转义下划线 + **」串，
比 PDF 文本层的空格串可靠得多。本工具：
1. 在 md 里定位默写题（「在横线处填写作品原句」/「写出诗文原句」/「名篇名句默写」），
   截到下一道大题（`四、`/`（一）阅读下面…`/`16.`）为止；
2. 按 `①…⑩` / `（1）…` / `1.` 切成小题；
3. 每个下划线串 → `<Input2></Input2>`；
4. 答案从 md 的答案区按题号取（`①…`/`（1）…`/`15.（8分）①…`），取不到就留空并写 review。

用法：
    python3 rebuild_dictation_md.py --scan
    python3 rebuild_dictation_md.py --apply "二模/2023北京丰台高三二模语文.json"
    python3 rebuild_dictation_md.py --apply-all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fix_pairs_choice as base

BLANK = "<Input2></Input2>"
MD_ROOT = base.MD_ROOT
# md 里两种空位写法：①转义下划线串 `\_**\_**\__`；②全角/半角空格串（夹在中文之间）
BLANK_RUN = re.compile(
    "[\\\\_*]{4,}"
    "|(?<=[\\u4e00-\\u9fff，。；？！、）”》])[ \\u3000]{2,}(?=[\\u4e00-\\u9fff，。；？！、（“《])"
    "|(?<=[\\u201c\\u201d，。；：？！\"])[ \\u3000]{1,3}(?=[\\u201c\\u201d，。；：？！、\"])"
)
HEAD = re.compile(r"(在横线处(填写|写出)作品原句|写出诗文原句|名篇名句默写|补写出下列)")
STOP = re.compile(r"^(?:[一二三四五六七八九十]、\s*本大题|（[一二三四五]）\s*阅读|阅读下面(?:作品|文章|的)|四、|五、|\d{1,2}\s*[．.]\s*\S)")


def md_path_for(json_path: Path) -> Path | None:
    typ = json_path.parent.name
    title = json_path.stem
    base_dir = MD_ROOT / typ / "语文"
    for cand in (base_dir / title / f"{title}.md", base_dir / f"{title}（教师版）" / f"{title}（教师版）.md"):
        if cand.exists():
            return cand
    for d in sorted(base_dir.glob(f"{title}*")):
        for f in d.glob("*.md"):
            return f
    return None


def cut_block(md: str) -> str | None:
    lines = md.split("\n")
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


def to_content(block: str) -> tuple[str, int]:
    items: list[str] = []
    cur: str | None = None
    for raw in block.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([①-⑩]|（\d{1,2}）|\(\d{1,2}\))", line)
        if m:
            if cur:
                items.append(cur)
            cur = line
        elif cur is not None:
            cur += line
    if cur:
        items.append(cur)
    n = 0
    texts = []
    for it in items:
        cnt = 0
        def sub(_m):
            nonlocal cnt
            cnt += 1
            return BLANK
        t = BLANK_RUN.sub(sub, it)
        n += cnt
        texts.append(t)
    return "\n\n".join(texts), n


def answers_from_md(md: str, n_blank: int) -> list[str]:
    """从 md 答案区取默写答案（按 ①② 或 （1） 分组）。"""
    cut = max(md.rfind("参考答案"), md.rfind("答案与解析"), 0)
    area = md[cut:] if cut else md
    found: list[str] = []
    # ①② 分组
    groups = re.split(r"(?=[①②③④⑤⑥⑦⑧⑨⑩])", area)
    for g in groups:
        if not re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩]", g):
            continue
        body = re.sub(r"^[①-⑩]\s*", "", g)
        body = re.sub(r"[（(]\s*重点[^）)]*[）)]", "", body)
        body = re.split(r"[①②③④⑤⑥⑦⑧⑨⑩]|【|（\d+分）|\(|\n\n", body)[0]
        parts = [p.strip() for p in re.split(r"[，,、；;]", body) if len(p.strip()) >= 2]
        found.extend(parts)
    if len(found) >= n_blank:
        return found[:n_blank]
    # （1）（2）分组
    found2: list[str] = []
    for m in re.finditer(r"[（(]\s*(\d{1,2})\s*[）)]\s*([^\n（(]{2,40})", area):
        for p in re.split(r"[，,、；;]", m.group(2)):
            p = p.strip()
            if len(p) >= 2:
                found2.append(p)
    if len(found2) >= n_blank:
        return found2[:n_blank]
    return (found + found2)[:n_blank]


def handle(json_path: Path, apply: bool) -> str:
    md_file = md_path_for(json_path)
    if not md_file:
        return f"{json_path.stem}：找不到 md"
    md = md_file.read_text(encoding="utf-8")
    block = cut_block(md)
    if not block:
        return f"{json_path.stem}：md 里没定位到默写题"
    content, n = to_content(block)
    if n == 0:
        return f"{json_path.stem}：md 默写块里没有下划线空位"
    answers = answers_from_md(md, n)
    qs = [{"id": i + 1, "questionType": "input", "content": "",
           "answer": answers[i] if i < len(answers) else "", "analysis": "", "score": 1} for i in range(n)]
    data = json.loads(json_path.read_text(encoding="utf-8"))
    secs = [s for s in (data.get("sections") or []) if s.get("category") == "默写"]
    old = len(secs[0].get("questions") or []) if secs else 0
    missing = sum(1 for q in qs if not q["answer"])
    msg = f"{json_path.stem}：md 读出 {n} 空 / {len(qs)} 问（原 {old} 问）｜缺答案 {missing}"
    if apply and secs:
        for s in secs:
            s["content"] = content
            s["questions"] = qs
            s["score"] = len(qs)
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        msg += " → 已写回"
    return msg


def in_scope(jf: Path) -> bool:
    typ = jf.parent.name
    title = jf.stem
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
    args = ap.parse_args()
    if args.scan:
        for f in targets_all():
            print(handle(f, apply=False))
        return 0
    targets = targets_all() if args.apply_all else [
        (Path(a) if Path(a).is_absolute() else base.JSON_ROOT / a) for a in args.apply
    ]
    for t in targets:
        print(handle(t, apply=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
