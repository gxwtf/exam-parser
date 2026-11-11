#!/usr/bin/env python3
"""修复「题号被 docx→md 转成 Markdown 有序列表」的问题。

现象：`w2m` 把「17.（10分）」这种题号段落转成有序列表项，且在每段重新起编，
于是整份 md 里出现多个 `1. （10分）`、`1. （8分）`……下游按题号切题的逻辑识别不到新题，
把后面几道大题全并进前一道（典型：2021 顺义期末政治 16/17/18 拼成一组）。

做法：扫描 md，维护「当前题号」（来自 `^数字[.．、]` 的非列表行）；
遇到 `^1[.．] （N分）` 这种**带分值括号的列表项**就判定它是被吞掉题号的大题，
按「当前题号 + 1」重编号；遇到二级标题（`## 第二部分`、`参考答案` 等）重置计数。

用法：
    python3 fix_md_question_numbers.py --scan
    python3 fix_md_question_numbers.py --apply <md 路径> [...]
    python3 fix_md_question_numbers.py --apply-all      # 修全库（政治/地理/物理/化学/语文/英语）
"""
from __future__ import annotations

import argparse
import glob
import re
import sys

QUESTION_LINE = re.compile(r"^(\d{1,2})\s*[\.．、]\s*\S")
COLLAPSED = re.compile(r"^1\s*[\.．]\s*（\s*\d+\s*分\s*）", re.M)
RESET = re.compile(r"^\s*(##\s|参考答案|答案与解析|【参考译文】|【答案】)")


def fix_text(text: str) -> tuple[str, list[str]]:
    lines = text.split("\n")
    last: int | None = None
    changed: list[str] = []
    for i, line in enumerate(lines):
        if RESET.match(line):
            last = None
            continue
        if COLLAPSED.match(line):
            if last is not None:
                nxt = last + 1
                new = re.sub(r"^1\s*([\.．])", f"{nxt}\\1", line, count=1)
                changed.append(f"行{i + 1}: {line.strip()[:20]} → {new.strip()[:20]}")
                lines[i] = new
                last = nxt
            continue
        m = QUESTION_LINE.match(line)
        if m:
            last = int(m.group(1))
    return "\n".join(lines), changed


def affected(path: str) -> int:
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return 0
    return len(COLLAPSED.findall(text))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", nargs="*", default=[])
    ap.add_argument("--apply-all", action="store_true")
    args = ap.parse_args()

    if args.scan:
        rows = [(affected(f), f) for f in glob.glob("/home/gxwtf/jingshibang/output_docx_to_md/**/*.md", recursive=True)]
        rows = [r for r in rows if r[0] >= 2]
        print(f"受影响 md：{len(rows)}")
        for n, f in sorted(rows, reverse=True):
            print(f"  {n}×  {f}")
        return 0

    targets = args.apply or (
        [f for f in glob.glob("/home/gxwtf/jingshibang/output_docx_to_md/**/*.md", recursive=True) if affected(f) >= 2]
        if args.apply_all else []
    )
    for f in targets:
        text = open(f, encoding="utf-8").read()
        new, changed = fix_text(text)
        if not changed:
            print(f"  跳过 {f}（无需修复）")
            continue
        open(f, "w", encoding="utf-8").write(new)
        print(f"  ✓ {f}：修 {len(changed)} 处")
        for c in changed:
            print(f"      {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
