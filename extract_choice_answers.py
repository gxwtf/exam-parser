#!/usr/bin/env python3
"""从化学 MD 的「参考答案」区抽选择题答案，输出 build_image_questions.py 能吃的 JSON。

用法：
  python3 extract_choice_answers.py <卷.md> [-o out.json]      # 打印/写出 {sections:[{questions:[{id,answer}]}]}
  python3 extract_choice_answers.py <卷.md> --text             # 只打印「题号:答案」方便核对

为什么需要它：图片版链路的选择题答案原本来自 output_json_math/<类型>/化学/<卷>.json，
而 一模 / 上期末 / 真题 没有这个 JSON，只能从 MD 的答案表里抽。
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROW_Q = re.compile(r"^\|\s*题号\s*\|")
ROW_A = re.compile(r"^\|\s*答案\s*\|")
NUM_RE = re.compile(r"\d{1,2}")


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def from_tables(lines: list[str]) -> dict[int, str]:
    """`| 题号 | 1 | 2 | ...` + `| 答案 | A | B | ...` 交替出现的表格"""
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        if not ROW_Q.match(line):
            continue
        # 往下找最近的一行「答案」
        for j in range(i + 1, min(i + 4, len(lines))):
            if ROW_A.match(lines[j]):
                nums, ans = cells(line), cells(lines[j])
                for k in range(1, min(len(nums), len(ans))):
                    m = NUM_RE.fullmatch(nums[k])
                    a = ans[k].strip().upper()
                    if m and re.fullmatch(r"[A-D]", a):
                        out[int(m.group())] = a
                break
    return out


def is_sep(cells_row: list[str]) -> bool:
    return bool(cells_row) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells_row if c)


def from_bare_table(lines: list[str]) -> dict[int, str]:
    """无表头的答案表：`| 1 | 2 | … | 14 |` 紧跟 `| --- | … |` 和 `| B | A | … |`"""
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        head = cells(line)
        if len(head) < 5 or not all(NUM_RE.fullmatch(c) for c in head if c):
            continue
        if not head or head[0] != "1":
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            if not lines[j].strip().startswith("|"):
                break
            row = cells(lines[j])
            if is_sep(row):
                continue
            if len(row) >= len(head) and all(re.fullmatch(r"[A-D]", c.upper()) for c in row if c):
                for n, a in zip(head, row):
                    if NUM_RE.fullmatch(n) and re.fullmatch(r"[A-D]", a.upper()):
                        out[int(n)] = a.upper()
                break
        if len(out) >= 5:
            break
    return out


def from_inline(text: str) -> dict[int, str]:
    """兜底：`1.A 2.B` / `1．A` / `1-B`；以及 `1-5 ABCDD` 这种区间写法"""
    out: dict[int, str] = {}
    # 区间：1-5 ABCDD / 6~10 BCDAB
    for m in re.finditer(r"(\d{1,2})\s*[-~－—]\s*(\d{1,2})\s*[：:．.、]?\s*([A-D]{2,})", text):
        start, end, letters = int(m.group(1)), int(m.group(2)), m.group(3)
        if end - start + 1 == len(letters):
            for k, ch in enumerate(letters):
                out[start + k] = ch
    # 逐题：1.A / 1．A / 1、A / 1-A
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[．.、:：\-]\s*([A-D])(?![A-Za-z])", text):
        n, a = int(m.group(1)), m.group(2)
        if 1 <= n <= 20:
            out.setdefault(n, a)
    return out


def from_prose(text: str) -> dict[int, str]:
    """解析式答案：`1．【分析】… 故选：B。` / `12．【解答】…故答案为…故选：D`"""
    out: dict[int, str] = {}
    cur = None
    for line in text.splitlines():
        # 题型一：`1\. 【答案】B`（2021+ 的卷子常用）
        m0 = re.match(r"^\s*(\d{1,2})\s*\\?\s*[．.、]?\s*【答案】\s*[：:]?\s*([A-D])", line)
        if m0:
            n = int(m0.group(1))
            if 1 <= n <= 20:
                out.setdefault(n, m0.group(2))
            cur = n
            continue
        # 题型二：`1．（3分）…` / `1．【分析】…` 开新题
        m = re.match(r"^\s*(\d{1,2})\s*\\?[．.、](?!\d)", line)
        if m:
            cur = int(m.group(1))
        m2 = re.search(r"故选\s*[：:]\s*([A-D])", line)
        if m2 and cur and 1 <= cur <= 20:
            out.setdefault(cur, m2.group(1))
        # 题型三：`【答案】` 单独一行（题干与答案分开写的卷子）
        m3 = re.match(r"^\s*【答案】\s*[：:]?\s*([A-D])\b", line)
        if m3 and cur and 1 <= cur <= 20:
            out.setdefault(cur, m3.group(1))
    return out


def extract(md_path: Path) -> dict[int, str]:
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    idx = text.find("参考答案")
    if idx < 0:
        idx = text.find("答案与解析")
    ans_text = text[idx:] if idx >= 0 else text
    lines = ans_text.splitlines()
    answers = from_tables(lines)
    if len(answers) < 10:
        for k, v in from_bare_table(lines).items():
            answers.setdefault(k, v)
    if len(answers) < 10:  # 表格抽不全就依次用「解析式答案」「逐题正则」补
        for k, v in from_prose(ans_text).items():
            answers.setdefault(k, v)
        for k, v in from_inline(ans_text).items():
            answers.setdefault(k, v)
    # 只保留 1..20 的选择题答案
    return {k: v for k, v in sorted(answers.items()) if 1 <= k <= 20}


def to_choice_json(answers: dict[int, str]) -> dict:
    return {"sections": [{"questions": [{"id": n, "answer": a} for n, a in answers.items()]}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--text", action="store_true")
    args = ap.parse_args()
    md = Path(args.md)
    answers = extract(md)
    if args.text:
        print(" ".join(f"{n}:{a}" for n, a in answers.items()) or "(没抽到)")
        return
    data = to_choice_json(answers)
    if args.out:
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{md.name}: {len(answers)} 个答案 → {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
