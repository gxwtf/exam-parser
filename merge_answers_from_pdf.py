#!/usr/bin/env python3
"""化学：有些卷的 docx 是学生版（没有答案），答案只在教师版 PDF 里。
本脚本用 convert_chemistry_pdf.py（多模态读 PDF 页面）取答案，只补 JSON 里缺的答案。

用法：python3 merge_answers_from_pdf.py [--type 二模] [--subject 化学] [--dry-run]
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
ROOT = PROJECT_ROOT.parent
JSON_ROOT = ROOT / "output_json_math"
PDF_ROOT = ROOT / "gaozhong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--subject", default="化学")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="只处理某一卷（卷名，不含 .json）")
    args = ap.parse_args()

    files = sorted((JSON_ROOT / args.type / args.subject).glob("*.json"))
    if args.only:
        files = [f for f in files if f.stem == args.only]
    filled = 0
    for jf in files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        need = [q["id"] for sec in data["sections"] for q in sec["questions"] if not str(q.get("answer") or "").strip()]
        if not need:
            continue
        pdfs = [p for p in (PDF_ROOT / args.type / args.subject).glob(jf.stem + "*.pdf")]
        if not pdfs:
            print(f"  ⚠ {jf.stem}: 缺答案 {len(need)} 题，但找不到 PDF")
            continue
        # 优先用「教师版」PDF
        pdfs.sort(key=lambda p: ("教师版" not in p.name, p.name))
        outdir = Path("/tmp/chem_ans_merge")
        cmd = [sys.executable, str(PROJECT_ROOT / "convert_chemistry_pdf.py"), "-p", str(pdfs[0]),
               "-o", str(outdir), "-w", "4", "--force"]
        print(f"  → {jf.stem}：用 {pdfs[0].name} 取答案")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        src = outdir / args.type / args.subject / f"{jf.stem}.json"
        if not src.exists():
            print(f"  ✗ 没生成 {src.name}：{r.stdout[-300:]}")
            continue
        got = {q["id"]: str(q.get("answer") or "").strip()
               for sec in json.loads(src.read_text(encoding="utf-8"))["sections"] for q in sec["questions"]}
        for sec in data["sections"]:
            for q in sec["questions"]:
                if not str(q.get("answer") or "").strip() and got.get(q["id"]):
                    q["answer"] = got[q["id"]]
                    filled += 1
        left = [q["id"] for sec in data["sections"] for q in sec["questions"] if not str(q.get("answer") or "").strip()]
        print(f"    补齐后仍缺：{left if left else '无'}")
        if not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"共补答案 {filled} 题")


if __name__ == "__main__":
    main()
