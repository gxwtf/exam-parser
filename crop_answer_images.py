#!/usr/bin/env python3
"""化学：把原卷 PDF 里「参考答案」区按大题裁成图片，供题库直接把答案图挂到题目上。

做法：
1. pdftotext -bbox 拿到每页每个词的坐标（复用 crop_questions_pdf 的 page_words / build_lines）；
2. 从第一个出现「参考答案」的页开始，找大题题号（`15.（11分）` / `【15题答案】` / `15．` 等写法）；
3. 每个题号裁到「下一条题号」之间（同页），跨页则到页底 / 下一页题号前；
4. 用 pdftoppm 以指定 dpi 渲染该页，PIL 按坐标裁图，存到
   output_docx_to_md/<类型>/<学科>/<卷>/answers/q<N>.png

用法：
  python3 crop_answer_images.py --type 一模 [--only 卷名] [--dpi 200]
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from crop_questions_pdf import page_words, build_lines  # noqa: E402

DATA = Path("/home/gxwtf/jingshibang")
MD_ROOT = DATA / "output_docx_to_md"
PDF_ROOT = DATA / "gaozhong"
SUBJECT = "化学"

# 大题题号写法：15．（11分） / 15. (1)… / 【15题答案】 / 15、
# 大题题号写法：15．（11分） / 15.（11分） / 15（8分） / 15（11分） / 【15题答案】 / 15、
ANS_MARK = re.compile(r"^(?:【)?(\d{1,2})\s*(?:题答案】|[．.、]|[（(]\s*\d{1,2}\s*分)")
FOOT_RE = re.compile(r"^\s*(第\s*\d+\s*页|共\s*\d+\s*页)")


def find_pdf(t: str, paper: str) -> Path | None:
    d = PDF_ROOT / t / SUBJECT
    if not d.is_dir():
        return None
    cands = [p for p in d.glob("*.pdf") if re.sub(r"（教师版）|\(教师版\)|含答案", "", p.stem).strip() == paper]
    # 优先教师版（有解析）
    for p in cands:
        if "教师版" in p.name:
            return p
    return cands[0] if cands else None


def answer_markers(pages):
    """返回 {题号: (页号, y0)}，只在「参考答案」之后的小题号里找"""
    start = None
    for i, pg in enumerate(pages):
        if any("参考答案" in ln["t"] for ln in build_lines(pg)):
            start = i
            break
    if start is None:
        return {}
    marks = {}
    for pi in range(start, len(pages)):
        w = pages[pi]["w"]
        for ln in build_lines(pages[pi]):
            t = ln["t"].strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            if FOOT_RE.match(t):
                continue
            m = ANS_MARK.match(t)
            if not m:
                continue
            no = int(m.group(1))
            if not (15 <= no <= 25):
                continue
            if ln["x0"] > w * 0.35:      # 只认左栏的题号
                continue
            if len(t) > 40 and "题答案" not in t:   # 排除「15．下列说法正确的是…」这种题干
                continue
            marks.setdefault(no, (pi, ln["y0"]))
    return marks


def render_page(pdf: Path, page_no: int, dpi: int, tmpdir: Path) -> Path:
    out = tmpdir / f"p{page_no}"
    subprocess.run(
        ["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-r", str(dpi), "-png", str(pdf), str(out)],
        capture_output=True, check=True,
    )
    got = sorted(tmpdir.glob(f"p{page_no}-*"))
    if not got:
        raise RuntimeError(f"渲染失败 p{page_no}")
    return got[0]


def crop_paper(t: str, paper: str, dpi: int) -> dict:
    pdf = find_pdf(t, paper)
    if not pdf:
        return {"paper": paper, "error": "找不到 PDF"}
    md_dir = MD_ROOT / t / SUBJECT / paper
    if not md_dir.is_dir():
        return {"paper": paper, "error": f"MD 目录不存在 {md_dir}"}
    pages = page_words(pdf)
    marks = answer_markers(pages)
    if not marks:
        return {"paper": paper, "error": "答案区没找到大题题号"}

    out_dir = md_dir / "answers"
    out_dir.mkdir(exist_ok=True)
    scale = dpi / 72.0
    nos = sorted(marks)
    saved = {}
    cache = {}
    for i, no in enumerate(nos):
        pi, y0 = marks[no]
        if i + 1 < len(nos):
            npi, ny0 = marks[nos[i + 1]]
        else:
            npi, ny0 = min(pi, len(pages) - 1), pages[pi]["h"]
        if pi not in cache:
            cache = {}  # 逐页渲染，先清缓存省内存
            cache[pi] = render_page(pdf, pi + 1, dpi, Path("/tmp/chem-ans-crop"))
        img = Image.open(cache[pi])
        pg = pages[pi]
        top = max(0.0, (y0 - 3) * scale)
        if npi == pi:
            bot = min(img.height, (ny0 - 3) * scale)
        else:
            bot = img.height
        if bot - top < 20:
            continue
        left = max(0.0, 20 * scale)
        right = min(img.width, (pg["w"] - 12) * scale)
        crop = img.crop((int(left), int(top), int(right), int(bot)))
        dst = out_dir / f"q{no}.png"
        crop.save(dst, optimize=True)
        saved[no] = str(dst.relative_to(MD_ROOT))
    return {"paper": paper, "saved": saved}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True)
    ap.add_argument("--only", default=None)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--json", default="/tmp/answer-images.json")
    args = ap.parse_args()

    d = MD_ROOT / args.type / SUBJECT
    if not d.is_dir():
        raise SystemExit(f"没有目录 {d}")
    papers = [p.name for p in sorted(d.iterdir()) if p.is_dir() and (p / "images").is_dir()]
    if args.only:
        papers = [p for p in papers if p == args.only]
    print(f"{args.type}：待处理 {len(papers)} 卷")
    result = {}
    for i, paper in enumerate(papers, 1):
        try:
            r = crop_paper(args.type, paper, args.dpi)
        except Exception as e:  # noqa: BLE001
            r = {"paper": paper, "error": str(e)[:150]}
        result[paper] = r
        n = len(r.get("saved") or {})
        flag = "✓" if n else "✗"
        print(f"[{i}/{len(papers)}] {flag} {paper[:36]}：{n} 张 {r.get('error', '')}", flush=True)
    old = {}
    p = Path(args.json)
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old.update(result)
    p.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v.get("saved") or {}) for v in result.values())
    ok = sum(1 for v in result.values() if v.get("saved"))
    print(f"\n完成：{ok} 卷 / {total} 张，明细 {p}")


if __name__ == "__main__":
    main()
