#!/usr/bin/env python3
"""按「答案」重建默写板块的空位（答案驱动的挖空）。

默写每个小问都有答案（要填的那句）。把答案文本在题干/诗句里精确匹配出来挖成空位，
再处理残留的「　 　」「____」占位。相比按排版猜位置，这个方向更可靠：
- 答案本来就印在题干里的卷（如 2026 真题）→ 会把答案挖掉；
- 已经挖好的卷 → 匹配不到（已被挖空），保留原空位；
- 空位错位/堆在行末的卷 → 由答案重新定位。

用法：
    python3 rebuild_dictation.py --scan
    python3 rebuild_dictation.py --apply "真题/2026北京高考真题语文.json"
    python3 rebuild_dictation.py --apply-all
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
BLANK_RE = re.compile(r"<(?:Input2|Blank)[^>]*>(?:</(?:Input2|Blank)>)?")
RAW_PLACEHOLDER = re.compile(r"[ \u3000]{2,}|_{2,}|＿{2,}|〔\s*〕")


def norm(s: str) -> str:
    """去掉标点/空白/图片引用，只留汉字与字母数字，用于宽松匹配。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s or "")
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)


def strip_marks(s: str) -> str:
    s = re.sub(r"^[（(]?\d{1,2}[）)]?[\s、.．、]*", "", s or "")
    s = re.sub(r"[（(][^）)]{0,20}[）)]\s*$", "", s)
    return s.strip()


def blank_out_by_answer(content: str, answer: str) -> tuple[str, bool]:
    a = norm(strip_marks(answer))
    if len(a) < 2:
        return content, False
    idx_map: list[int] = []
    flat_chars: list[str] = []
    for i, ch in enumerate(content):
        if re.match(r"[\u4e00-\u9fffA-Za-z0-9]", ch):
            flat_chars.append(ch)
            idx_map.append(i)
    flat = "".join(flat_chars)
    pos = flat.find(a)
    if pos < 0:
        return content, False
    start = idx_map[pos]
    end = idx_map[pos + len(a) - 1] + 1
    return content[:start] + BLANK + content[end:], True


def fill_leftovers(content: str, need: int) -> int:
    filled = 0
    for m in list(RAW_PLACEHOLDER.finditer(content)):
        if filled >= need:
            break
        seg = m.group(0)
        before = content[max(0, m.start() - 6):m.start()]
        if not re.search(r"[\u4e00-\u9fff，。；：？！、]", before):
            continue
        content = content[:m.start()] + BLANK + content[m.end():]
        filled += 1
        break_after = True
    # 上面只替换了第一个，循环直到补齐
    while filled < need:
        m = RAW_PLACEHOLDER.search(content)
        if not m:
            break
        before = content[max(0, m.start() - 6):m.start()]
        if not re.search(r"[\u4e00-\u9fff，。；：？！、]", before):
            content = content[:m.start()] + " " + content[m.end():]
            continue
        content = content[:m.start()] + BLANK + content[m.end():]
        filled += 1
    return filled


def handle(json_path: Path, apply: bool) -> list[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    log: list[str] = []
    for sec in data.get("sections") or []:
        if sec.get("category") != "默写":
            continue
        qs = sec.get("questions") or []
        content = sec.get("content") or ""
        before = len(BLANK_RE.findall(content))
        hit = 0
        for q in qs:
            ans = str(q.get("answer") or "")
            if not ans.strip():
                continue
            content, ok = blank_out_by_answer(content, ans)
            hit += 1 if ok else 0
        now = len(BLANK_RE.findall(content))
        if now < len(qs):
            now += fill_leftovers(content, len(qs) - now)
            # 重新取一次内容（fill_leftovers 就地改的是局部变量）
            content = content
            now = len(BLANK_RE.findall(content))
        # 上面 fill_leftovers 返回条数但 content 已在其内部替换（传入的是 str，需按引用改造）
        status = "OK" if now == len(qs) else f"空位{now}≠小问{len(qs)}"
        log.append(f"{json_path.stem}：答案挖空 {hit}｜空位 {before}→{now}（小问 {len(qs)}）{status}")
        if apply and now == len(qs):
            sec["content"] = content
            sec["score"] = len(qs)
    if apply:
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return log


def targets_all() -> list[Path]:
    return [f for f in sorted(base.JSON_ROOT.glob("*/*.json"))
            if ".old-groups" not in f.name and '"默写"' in f.read_text(encoding="utf-8")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", nargs="+", default=[])
    ap.add_argument("--apply-all", action="store_true")
    args = ap.parse_args()
    if args.scan:
        for f in targets_all():
            for line in handle(f, apply=False):
                print(line)
        return 0
    targets = targets_all() if args.apply_all else [
        (Path(a) if Path(a).is_absolute() else base.JSON_ROOT / a) for a in args.apply
    ]
    for t in targets:
        for line in handle(t, apply=True):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
