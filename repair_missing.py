#!/usr/bin/env python3
"""化学：补「切题没切到」的题（Word 转 Markdown 时题号被吞、选项是整块图片/表格等）。

做法：读 *.review.txt 里的「缺题号」，用多模态模型逐页读该卷 PDF，把指定题号的题干+四选项转录回来，
按题号插进 JSON（答案缺失时一起补）。

用法：python3 repair_missing.py --type 二模 [--subject 化学] [--dry-run] [--only 卷名]
"""
import argparse, base64, json, re, tempfile, time
from pathlib import Path

import convert_math_pdf as M

PROJECT_ROOT = Path(__file__).parent
ROOT = PROJECT_ROOT.parent
JSON_ROOT = ROOT / "output_json_math"
PDF_ROOT = ROOT / "gaozhong"

ASK = """这是北京高三化学试卷（或答案页）的一页图像。请只转录第 {no} 题（如果本页没有这道题，输出 {{}}）：

{{"stem":"题干原文（化学式用 $\\mathrm{{H_2O}}$ 这类 LaTeX；不写题号和分值）",
  "options":[{{"id":"a","label":"..."}},{{"id":"b","label":"..."}},{{"id":"c","label":"..."}},{{"id":"d","label":"..."}}],
  "answer":"B"}}

要求：逐字照抄，不要改写；选项不要带 A． 前缀；题干里的数据表用 markdown 表格；
答案若本页看不到就留空字符串。只输出 JSON。"""


def ask_page(img: Path, no: int) -> dict:
    b64 = base64.b64encode(img.read_bytes()).decode()
    try:
        return M.chat_json([{"role": "system", "content": "你是化学试卷录入员，只输出 JSON。"},
                            {"role": "user", "content": [
                                {"type": "text", "text": ASK.format(no=no)},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
                           M.MODEL, 4000, retries=2)
    except Exception:
        return {}


def norm_opts(raw):
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            k2 = str(k).strip().lower()
            if k2 in "abcd":
                out[k2] = str(v)
    elif isinstance(raw, list):
        for i, item in enumerate(raw[:4]):
            if isinstance(item, dict):
                k2 = str(item.get("id", "")).strip().lower()
                lab = str(item.get("label", ""))
                if k2 in "abcd":
                    out[k2] = lab
                else:
                    out["abcd"[i]] = lab
            else:
                out["abcd"[i]] = str(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--subject", default="化学")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    jdir = JSON_ROOT / args.type / args.subject
    files = sorted(jdir.glob("*.json"))
    if args.only:
        files = [f for f in files if f.stem == args.only]
    total = 0
    for jf in files:
        rev = jf.with_suffix(".review.txt")
        if not rev.exists():
            continue
        m = re.search(r"缺题号[^：:\n]*[：:]\s*\[([0-9,\s]+)\]", rev.read_text(encoding="utf-8"))
        if not m:
            continue
        missing = [int(x) for x in re.findall(r"\d+", m.group(1))]
        pdfs = [p for p in (PDF_ROOT / args.type / args.subject).glob(jf.stem + "*.pdf")]
        if not pdfs:
            print(f"  ⚠ {jf.stem}: 缺 {missing}，无 PDF"); continue
        pdfs.sort(key=lambda p: ("教师版" not in p.name, p.name))
        data = json.loads(jf.read_text(encoding="utf-8"))
        qs = data["sections"][0]["questions"]
        have = {q["id"] for q in qs}
        need = [n for n in missing if n not in have]
        if not need:
            continue
        print(f"  → {jf.stem}: 补 {need}")
        with tempfile.TemporaryDirectory() as td:
            pages = M.render_pages(pdfs[0], args.dpi, Path(td), args.max_pages)
            for no in need:
                got = {}
                for img in pages:
                    cand = ask_page(img, no)
                    o = norm_opts(cand.get("options"))
                    if len([v for v in o.values() if str(v).strip()]) >= 4:
                        got = cand
                        break
                if not got:
                    print(f"     第{no}题：PDF 里没读到"); continue
                o = norm_opts(got.get("options"))
                q = {
                    "id": no, "questionType": "single",
                    "content": M.normalize_latex(str(got.get("stem") or "").strip()),
                    "options": [{"id": k, "label": M.normalize_latex(str(o.get(k, "")).strip())} for k in "abcd"],
                    "answer": re.sub(r"[^A-D]", "", str(got.get("answer") or "").upper()) or "",
                    "analysis": "", "score": qs[0].get("score", 3) if qs else 3,
                }
                qs.append(q); qs.sort(key=lambda x: x["id"]); total += 1
                print(f"     第{no}题 ✓（答案 {q['answer'] or '缺'}）")
        if not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        rev.unlink(missing_ok=True)
    print(f"补齐题目 {total} 道")


if __name__ == "__main__":
    main()
