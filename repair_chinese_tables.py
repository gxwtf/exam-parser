#!/usr/bin/env python3
"""语文：修复被 Word→MD 弄乱（合并单元格/跨列）的表格题。

做法：定位该题所在 PDF 页，让多模态模型把表格重新转成 Markdown（保留 ①②③ 等填空标记），
再替换掉题干里那段坏表格。
用法：python3 repair_chinese_tables.py [--dir 输出目录] [--type 二模] [--dry-run] [--only 卷名]
"""
import argparse, base64, json, re, tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")
BASE_URL = (__import__("os").getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = __import__("os").getenv("OPENAI_API_KEY", "")
MODEL = __import__("os").getenv("OPENAI_MODEL", "deepseek-v4.1-flash")

import convert_math_pdf as M

PROMPT = r"""这是语文试卷的一页图像。如果本页有【第 {no} 题】的表格，请把它完整转成 Markdown 表格：
1. 保留原文所有文字与填空标记 ①②③④⑤⑥，不要补答案、不要改写；
2. 合并单元格用空单元格/重复上级标签如实表达，保持「上级→下级」的层级；
3. 只输出 Markdown 表格（不要题目文字、不要解释）；本页没有该题的表格就输出空。
"""


def ask(img: Path, no) -> str:
    b64 = base64.b64encode(img.read_bytes()).decode()
    payload = {"model": MODEL, "max_tokens": 3000, "temperature": 0.0, "reasoning_effort": "none",
               "messages": [{"role": "system", "content": "你是语文试卷录入员，只输出 Markdown 表格。"},
                            {"role": "user", "content": [{"type": "text", "text": PROMPT.format(no=no)},
                                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {API_KEY}"}, timeout=300)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


def table_region(lines: list[str]) -> tuple[int, int] | None:
    idx = [i for i, l in enumerate(lines) if l.strip().startswith("|")]
    if len(idx) < 2:
        return None
    return idx[0], idx[-1]


def suspicious(text: str) -> bool:
    lines = [l for l in (text or "").split("\n") if l.strip().startswith("|")]
    if len(lines) < 3:
        return False
    widths = {len(l.strip().strip("|").split("|")) for l in lines}
    return len(widths) > 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent.parent / "output_json_chinese" / "二模"))
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    pdf_dir = PROJECT_ROOT.parent / "gaozhong" / args.type / "语文"
    fixed = 0
    for jf in sorted(Path(args.dir).glob("*.json")):
        if args.only and jf.stem != args.only:
            continue
        data = json.loads(jf.read_text(encoding="utf-8"))
        targets = [(g, q) for g in data["groups"] for q in g["questions"] if suspicious(str(q.get("content") or ""))]
        if not targets:
            continue
        pdfs = sorted(pdf_dir.glob(jf.stem + "*.pdf"))
        if not pdfs:
            print(f"  ⚠ {jf.stem}: 有可疑表格但没有 PDF")
            continue
        with tempfile.TemporaryDirectory() as td:
            pages = M.render_pages(pdfs[0], 140, Path(td), 8)
            for g, q in targets:
                got = ""
                for img in pages:
                    got = ask(img, q["id"])
                    if got and got.count("\n") >= 3:
                        break
                    got = ""
                if not got:
                    print(f"  ⚠ {jf.stem} {g['category']} 第{q['id']}题：没读到表格")
                    continue
                lines = (q.get("content") or "").split("\n")
                region = table_region(lines)
                if not region:
                    continue
                s, e = region
                new_content = "\n".join(lines[:s] + got.split("\n") + lines[e + 1:])
                if not args.dry_run:
                    q["content"] = new_content
                fixed += 1
                print(f"  ✓ {jf.stem} {g['category']} 第{q['id']}题：表格已替换（{got.count(chr(10))+1} 行）")
        if not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"处理表格 {fixed} 处")


if __name__ == "__main__":
    main()
