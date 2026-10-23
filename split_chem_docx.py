#!/usr/bin/env python3
"""化学：把 Word 试卷按「题」拆成单独的 docx，再转成图片（每题一张，跨页自动上下拼接）。

为什么：Word 里一题常跨页/带浮动图，转 MD 再解析会丢格式；直接把每道题渲染成图最忠实。

用法：
  python3 split_chem_docx.py -p <docx> -T 二模 -o <输出根>/<类型>/<学科>/<卷名>/images
输出：q1.png / q2.png … 以及 qmap.json（题号 → 图片文件名）
"""
import argparse, json, re, shutil, subprocess, tempfile, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = {"w": W[1:-1]}


def para_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def split_questions(docx: Path, max_q: int = 30):
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    children = list(body)
    sect = body.find(W + "sectPr")

    starts = []
    expected = 1
    for idx, el in enumerate(children):
        if el.tag != W + "p":
            continue
        txt = para_text(el)
        m = re.search(rf"(?<![0-9]){expected}\s*[．.]", txt)
        if m and m.start() <= 14:
            starts.append((idx, expected))
            expected += 1
            if expected > max_q:
                break
    if not starts:
        return [], root, children, sect

    groups = []
    FOOTER = re.compile(r"^\s*第\s*\d+\s*页\s*[/／]\s*共\s*\d+\s*页\s*$")
    for i, (idx, no) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(children)
        keep = [el for el in children[idx:end]
                if not (el.tag == W + "p" and FOOTER.match(para_text(el)))]
        groups.append((no, keep))
    return groups, root, children, sect


def write_docx(src: Path, root, children, sect, keep, dst: Path):
    for el in list(root.find(W + "body")):
        root.find(W + "body").remove(el)
    body = root.find(W + "body")
    for el in keep:
        body.append(el)
    if sect is not None:
        import copy
        sect2 = copy.deepcopy(sect)
        for tag in ("footerReference", "headerReference"):
            for el in sect2.findall(W + tag):
                sect2.remove(el)
        body.append(sect2)
    ET.register_namespace("w", W[1:-1])
    for prefix, uri in (("w", W[1:-1]),):
        ET.register_namespace(prefix, uri)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml_bytes
            zout.writestr(item, data)


def to_png(docx: Path, out_png: Path, dpi: int = 160):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, str(docx)],
                       capture_output=True, timeout=300)
        pdfs = list(Path(td).glob("*.pdf"))
        if not pdfs:
            return False
        prefix = Path(td) / "p"
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), str(pdfs[0]), str(prefix)], capture_output=True, timeout=300)
        pages = sorted(Path(td).glob("p-*.png")) or sorted(Path(td).glob("p*.png"))
        if not pages:
            return False
        if len(pages) == 1:
            subprocess.run(["convert", str(pages[0]), "-trim", "+repage", "+repage", str(out_png)], capture_output=True)
        else:
            subprocess.run(["convert"] + [str(p) for p in pages] + ["-append", "-trim", "+repage", str(out_png)],
                           capture_output=True)
        return out_png.exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path", required=True)
    ap.add_argument("-o", "--out", required=True, help="单题输出目录（每题 qN.docx / qN.png / qN.json）")
    ap.add_argument("--serve-dir", default=None, help="nginx 服务的图片目录（默认 <输出根>/<类型>/<学科>/<卷>/images）")
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--max-q", type=int, default=25)
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    args = ap.parse_args()

    docx = Path(args.path)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    serve_dir = Path(args.serve_dir) if args.serve_dir else out_dir
    serve_dir.mkdir(parents=True, exist_ok=True)
    groups, root, children, sect = split_questions(docx, args.max_q)
    print(f"识别到 {len(groups)} 题：{[g[0] for g in groups]}")
    mapping = {}
    tmp = Path(tempfile.mkdtemp(prefix="qsplit-"))
    stem = re.sub(r"（教师版）|\(教师版\)", "", docx.stem).strip()
    for no, keep in groups:
        d = out_dir / f"q{no}.docx"
        write_docx(docx, root, children, sect, keep, d)
        png_tmp = tmp / f"q{no}.png"
        if not to_png(d, png_tmp, args.dpi):
            print(f"  ✗ 第{no}题 转图失败"); continue
        (out_dir / f"q{no}.png").write_bytes(png_tmp.read_bytes())
        (serve_dir / f"q{no}.png").write_bytes(png_tmp.read_bytes())
        url = f"/exam-images/{args.type}/{args.subject}/{stem}/images/q{no}.png"
        # 单题 JSON
        is_choice = None
        (out_dir / f"q{no}.json").write_text(json.dumps({
            "paper": stem, "subject": args.subject, "no": no,
            "image": url, "image_file": f"q{no}.png",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        mapping[no] = {"image": url, "docx": f"q{no}.docx"}
        print(f"  ✓ 第{no}题 → q{no}.png ({png_tmp.stat().st_size//1024} KB)")
    (out_dir / "manifest.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print("输出目录:", out_dir)


if __name__ == "__main__":
    main()
