#!/usr/bin/env python3
"""化学：直接从原 PDF 按题裁图（保原版式，不重排 → 不会出现浮动图重叠）。

做法：
1. pdftoppm 把每页渲染成 PNG；
2. pdftotext -bbox 拿到每个词的坐标，定位「N．」题号（左侧）与下一条题号/大题标题（第二部分、本部分共…）；
3. 按题号到下一条题号之间的纵向区间裁图，跨页则首尾拼接；
4. 用白块盖掉被裁进来的题号本身。

用法：python3 crop_questions_pdf.py -p <pdf> -o <单题目录> --serve-dir <nginx目录> [-T 二模] [-s 化学]
"""
import argparse, json, re, subprocess, tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

LINE_Q = re.compile(r"^(\d{1,2})\s*[.．]")
HEAD_RE = re.compile(r"^(第[一二]部分|本部分共|参考答案)")
FOOT_RE = re.compile(r"^第\s*\d+\s*页\s*[/／]\s*共\s*\d+\s*页")


def page_words(pdf: Path):
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "w.xml"
        subprocess.run(["pdftotext", "-bbox", "-q", str(pdf), str(out)], check=True)
        root = ET.parse(out).getroot()
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    pages = []
    for pg in root.iter("{http://www.w3.org/1999/xhtml}page"):
        w = float(pg.get("width")); h = float(pg.get("height"))
        words = []
        for wd in pg.iter("{http://www.w3.org/1999/xhtml}word"):
            txt = (wd.text or "").strip()
            if not txt:
                continue
            words.append({
                "t": txt,
                "x0": float(wd.get("xMin")), "y0": float(wd.get("yMin")),
                "x1": float(wd.get("xMax")), "y1": float(wd.get("yMax")),
            })
        pages.append({"w": w, "h": h, "words": words})
    return pages


def build_lines(pg):
    """把同 y 的词合成一行，返回 [{t,x0,y0,y1}]（按 y 排序）"""
    rows = {}
    for wd in pg["words"]:
        key = round(wd["y0"] / 3.0)
        rows.setdefault(key, []).append(wd)
    lines = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w["x0"])
        lines.append({
            "t": "".join(w["t"] for w in ws),
            "x0": min(w["x0"] for w in ws),
            "x1": max(w["x1"] for w in ws),
            "y0": min(w["y0"] for w in ws),
            "y1": max(w["y1"] for w in ws),
        })
    return lines


def find_markers(pages, max_q=25):
    """题号行：「N．…」或单独一个数字（且靠左）。返回 {题号: (页码, y0, 行, 数字串)}"""
    marks = {}
    expect = 1
    for pi, pg in enumerate(pages):
        for ln in build_lines(pg):
            if ln["x0"] > pg["w"] * 0.2:
                continue
            m = re.match(r"^(\d{1,2})\s*[.．]?", ln["t"])
            if not m:
                continue
            n = int(m.group(1))
            if n != expect:
                continue
            marks[n] = (pi, ln["y0"], ln, m.group(1))
            expect += 1
            if expect > max_q:
                return marks
    return marks


def find_heading_cut(pages, start_pi, start_y, end_pi, end_y):
    """在区间内找「第二部分/本部分共/参考答案」标题行，作为提前结束点"""
    for pi in range(start_pi, min(end_pi, len(pages) - 1) + 1):
        pg = pages[pi]
        y_from = start_y if pi == start_pi else 0
        for wd in pg["words"]:
            if wd["y0"] < y_from - 1:
                continue
            if pi == end_pi and wd["y0"] > end_y:
                continue
            if HEAD_RE.match(wd["t"]):
                return pi, wd["y0"]
    return end_pi, end_y


def trim_vertical(png: Path, pad: int = 0, thresh: int = 235):
    """只裁上下空白、保留整页宽度：这样同一份卷里所有题的图等比一致（前端按宽度铺满时字号相同）"""
    try:
        from PIL import Image
    except Exception:
        return
    im = Image.open(png).convert("L")
    w, h = im.size
    px = im.load()
    top = None
    bot = None
    for y in range(h):
        row_has = any(px[x, y] < thresh for x in range(0, w, 3))
        if row_has:
            if top is None:
                top = y
            bot = y
    if top is None:
        return
    top = max(0, top - pad)
    bot = min(h, bot + pad + 1)
    im.crop((0, top, w, bot)).save(png)


def compact_blank(png: Path, thresh: int = 40, keep: int = 16):
    """把图片里连续的大段空白（页间隔）压短，避免跨页拼接出现大空洞"""
    try:
        from PIL import Image
    except Exception:
        return
    im = Image.open(png).convert("L")
    w, h = im.size
    px = im.load()
    blank = []
    for y in range(h):
        row_blank = True
        for x in range(0, w, 4):
            if px[x, y] < 235:
                row_blank = False
                break
        blank.append(row_blank)
    pieces = []
    y = 0
    while y < h:
        if blank[y]:
            y2 = y
            while y2 < h and blank[y2]:
                y2 += 1
            run = y2 - y
            pieces.append(("blank", y, y2, keep if run > thresh else run))
            y = y2
        else:
            y2 = y
            while y2 < h and not blank[y2]:
                y2 += 1
            pieces.append(("img", y, y2, None))
            y = y2
    total = sum(p[3] if p[0] == "blank" else (p[2] - p[1]) for p in pieces)
    if total == h:
        return
    out = Image.new("L", (w, total), 255)
    oy = 0
    src = Image.open(png).convert("RGB") if png.suffix.lower() in (".png", ".jpg") else im
    src = Image.open(png).convert("RGB")
    for kind, a, b, keepn in pieces:
        if kind == "blank":
            oy += keepn
        else:
            out.paste(src.crop((0, a, w, b)).convert("L"), (0, oy))
            oy += b - a
    out.save(png)


def text_column(pages, marks):
    """试题区（按题号区间）正文的左右边界（pt）。

    只统计「各题范围内、非题号行」的行：这样左边界就是题干正文的左边缘，
    题号（在更左边）会被裁掉，前端再单独显示序号 → 题干贴齐内容区最左端。
    """
    nos = sorted(marks)
    left, right = None, None
    for i, no in enumerate(nos):
        pi, y0, _ln, _d = marks[no]
        if i + 1 < len(nos):
            npi, ny0 = marks[nos[i + 1]][0], marks[nos[i + 1]][1]
        else:
            npi, ny0 = pi, pages[pi]["h"]
        for p2 in range(pi, npi + 1):
            for ln in build_lines(pages[p2]):
                if p2 == pi and ln["y0"] < y0 - 1:
                    continue
                if p2 == npi and ln["y0"] >= ny0 - 1:
                    continue
                if len(ln["t"].strip()) < 6:
                    continue
                if p2 == pi and abs(ln["y0"] - y0) < 2:      # 本题题号行
                    continue
                left = ln["x0"] if left is None else min(left, ln["x0"])
                right = ln["x1"] if right is None else max(right, ln["x1"])
    return (left or 0) - 1, (right or 0) + 2


def render_pages(pdf: Path, dpi: int, out_dir: Path):
    prefix = out_dir / "pg"
    subprocess.run(["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(prefix)], capture_output=True)
    return sorted(out_dir.glob("pg-*.png")) or sorted(out_dir.glob("pg*.png"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--serve-dir", default=None)
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--max-q", type=int, default=25)
    args = ap.parse_args()

    pdf = Path(args.path)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    serve_dir = Path(args.serve_dir) if args.serve_dir else out_dir
    serve_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"（教师版）|\(教师版\)|含答案", "", pdf.stem).strip()

    pages = page_words(pdf)
    marks = find_markers(pages, args.max_q)
    x_left, x_right = text_column(pages, marks)
    print(f"正文区横向范围：{x_left:.1f} ~ {x_right:.1f} pt")
    print(f"识别题号 {len(marks)} 个：{sorted(marks)}")
    with tempfile.TemporaryDirectory() as td:
        imgs = render_pages(pdf, args.dpi, Path(td))
        scale = args.dpi / 72.0
        mapping = {}
        nos = sorted(marks)
        for i, no in enumerate(nos):
            pi, y0, wd, digits = marks[no]
            if i + 1 < len(nos):
                npi, ny0, _, _ = marks[nos[i + 1]]
            else:
                npi, ny0 = len(pages) - 1, pages[-1]["h"]
            # 大题标题提前截断
            npi, ny0 = find_heading_cut(pages, pi, y0, npi, ny0)
            pieces = []
            foot = {}
            for p in range(pi, npi + 1):
                for ln in build_lines(pages[p]):
                    if FOOT_RE.match(ln["t"]):
                        foot[p] = ln["y0"]
                        break
            # 每题单独的左边界：不算题号本身的占位
            q_left = None
            for p2 in range(pi, npi + 1):
                for ln2 in build_lines(pages[p2]):
                    if ln2["y0"] < (y0 if p2 == pi else 0) - 1:
                        continue
                    if p2 == npi and ln2["y0"] >= ny0 - 1:
                        continue
                    if len(ln2["t"].strip()) < 4:
                        continue
                    cand = ln2["x0"]
                    if p2 == pi and abs(ln2["y0"] - y0) < 2:      # 题号所在行
                        cand = wd["x0"] + 5.8 * len(digits) + 7
                    q_left = cand if q_left is None else min(q_left, cand)
            if q_left is None:
                q_left = x_left   # 全卷统一左边界（去页边距、题干贴左）
            q_left = x_left   # 全卷统一左边界（去页边距、题干贴左）

            starts = {}
            for p in range(pi, npi + 1):
                if p == pi:
                    continue
                top_y = 0
                for ln in build_lines(pages[p]):
                    if ln["y0"] < pages[p]["h"] * 0.05 and len(ln["t"]) <= 30:
                        top_y = ln["y1"]        # 页眉，跳过
                    else:
                        break
                starts[p] = top_y

            for p in range(pi, npi + 1):
                page_bottom = ny0 if p == npi else pages[p]["h"]
                if p in foot and foot[p] < page_bottom:
                    page_bottom = foot[p]
                im = imgs[p]
                top = int(round((y0 if p == pi else starts.get(p, 0)) * scale))
                bot = int(round(page_bottom * scale))
                if bot - top < 8:
                    continue
                pieces.append((im, top, bot))
            if not pieces:
                print(f"  ✗ 第{no}题 区间为空"); continue
            outs = []
            num_w = 5.8 * len(digits) + 7
            for k, (im, top, bot) in enumerate(pieces):
                part = Path(td) / f"c{no}_{k}.png"
                x0px = int(round(q_left * scale))
                xw = int(round((x_right - q_left) * scale))
                subprocess.run(["convert", str(im), "-crop", f"{xw}x{bot-top}+{x0px}+{top}", "+repage", str(part)],
                               capture_output=True)
                if False:
                    # 先在「未左右裁剪」的图上涂掉题号，之后再 trim
                    bx0 = (wd["x0"] - q_left) * scale - 2
                    by0 = (wd["y0"] - y0) * scale - 2
                    bx1 = (wd["x0"] + num_w - q_left) * scale
                    by1 = (wd["y1"] - y0) * scale + 2
                    subprocess.run(["convert", str(part), "-fill", "white", "-draw",
                                    f"rectangle {int(bx0)},{int(by0)} {int(bx1)},{int(by1)}", str(part)],
                                   capture_output=True)
                trim_vertical(part)
                outs.append(part)
            png = out_dir / f"q{no}.png"
            if len(outs) == 1:
                subprocess.run(["convert", str(outs[0]), str(png)], capture_output=True)
            else:
                subprocess.run(["convert"] + [str(o) for o in outs] + ["-append", str(png)], capture_output=True)
                compact_blank(png, thresh=40, keep=12)
            (serve_dir / f"q{no}.png").write_bytes(png.read_bytes())
            url = f"/exam-images/{args.type}/{args.subject}/{stem}/images/q{no}.png"
            (out_dir / f"q{no}.json").write_text(json.dumps({
                "paper": stem, "subject": args.subject, "no": no, "image": url, "image_file": f"q{no}.png",
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            mapping[no] = {"image": url}
            print(f"  ✓ 第{no}题 → q{no}.png ({png.stat().st_size//1024} KB, {len(pieces)} 段)")
    (out_dir / "manifest.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    print("输出：", out_dir)


if __name__ == "__main__":
    main()
