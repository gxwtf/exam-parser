#!/usr/bin/env python3
"""从原卷 PDF 的文字层里抽选择题答案（MD 里答案是图片时用它兜底）。

很多「教师版」docx 的选择题答案是一张图片，docx→MD 后抽不出来；但同一套卷的 PDF
往往带文字层，`pdftotext -layout` 能拿到答案表（数字一行、字母一行）。

用法：
  python3 extract_pdf_answers.py <卷.pdf>            # 打印 题号:答案
"""
import re
import subprocess
import sys
from pathlib import Path

NUM_ROW = re.compile(r"^(?:\s*\d{1,2}\s+){2,}\s*$")
LET_ROW = re.compile(r"^(?:\s*[A-D]\s{2,}){2,}\s*$")


def pdf_text(pdf: Path) -> str:
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True)
    return r.stdout or ""


def extract(pdf: Path, expect_max: int = 20) -> dict[int, str]:
    text = pdf_text(pdf)
    if not text:
        return {}
    # 只从「参考答案 / 答案」之后找，避免把题干里的数字当答案
    idx = max(text.rfind("参考答案"), text.rfind("答案"))
    seg = text[idx:] if idx > len(text) // 3 else text
    lines = seg.splitlines()

    rows: list[tuple[list[int], list[str]]] = []
    i = 0
    while i < len(lines):
        if NUM_ROW.match(lines[i]):
            nums = [int(x) for x in re.findall(r"\d{1,2}", lines[i])]
            for j in range(i + 1, min(i + 5, len(lines))):
                if LET_ROW.match(lines[j]):
                    lets = re.findall(r"[A-D]", lines[j])
                    if len(lets) == len(nums):
                        rows.append((nums, lets))
                    break
                if NUM_ROW.match(lines[j]):
                    break
        i += 1

    out: dict[int, str] = {}
    for nums, lets in rows:
        for n, a in zip(nums, lets):
            if 1 <= n <= expect_max:
                out[n] = a
    # 兜底：单行「1 A 2 B 3 C」这种
    if len(out) < 5:
        for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[.．、:：]?\s*([A-D])(?=\s|$)", seg):
            n, a = int(m.group(1)), m.group(2)
            if 1 <= n <= expect_max:
                out.setdefault(n, a)
    return out


def main():
    pdf = Path(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    ans = extract(pdf, limit)
    print(f"{pdf.name}: {len(ans)} 个 → " + " ".join(f"{k}:{v}" for k, v in sorted(ans.items())))


if __name__ == "__main__":
    main()
