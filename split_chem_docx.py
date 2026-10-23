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


HEADING = re.compile(r"^\s*(?:第[一二]部分.*|本部分共.*题.*|第[一二]部分（.*|参考答案.*|[一二三四五六七八九十]\s*[、.．].*(?:题|分).*)$")


def strip_question_number(el, no: int):
    """把首段里的「N．」去掉（题号可能被拆到多个 run 里；保留分值等其余文字）"""
    runs = [t for t in el.iter(W + "t")]
    joined = "".join(t.text or "" for t in runs)
    m = re.search(rf"(?<![0-9]){no}\s*[．.]\s*", joined)
    if not m:
        return el
    start, end = m.start(), m.end()
    pos = 0
    for t in runs:
        txt = t.text or ""
        t_start, t_end = pos, pos + len(txt)
        pos = t_end
        if t_end <= start or t_start >= end:
            continue
        cut_from = max(start, t_start) - t_start
        cut_to = min(end, t_end) - t_start
        t.text = txt[:cut_from] + txt[cut_to:]
    return el


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
        if m and m.start() <= 30:
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
        keep = []
        first = True
        for el in children[idx:end]:
            if el.tag != W + "p":
                keep.append(el)
                continue
            txt = para_text(el).strip()
            if FOOTER.match(txt):
                continue
            if HEADING.match(txt):          # 「第二部分」「本部分共5题」这类标题不要
                continue
            if first:
                strip_question_number(el, no)
                first = False
            if not para_text(el).strip() and keep:   # 去掉尾部空段
                continue
            keep.append(el)
        # 去掉末尾的空段落
        while len(keep) > 1 and keep[-1].tag == W + "p" and not para_text(keep[-1]).strip():
            keep.pop()
        groups.append((no, keep))
    return groups, root, children, sect


def inline_anchors(root):
    """把浮动对象改成内联/去浮动，避免拆题重排后和文字重叠：
    1) wp:anchor → wp:inline
    2) 段落 w:framePr（文本框/带框段落）去掉
    3) VML 图形 w:pict/v:shape 里如果有 style 的 position:absolute，去掉定位
    """
    for anchor in root.iter():
        if anchor.tag.endswith("}anchor"):
            anchor.tag = anchor.tag.replace("}anchor", "}inline")
    for ppr in root.iter(W + "pPr"):
        for fr in ppr.findall(W + "framePr"):
            ppr.remove(fr)
    for el in root.iter():
        if el.tag.endswith("}shape") or el.tag.endswith("}rect"):
            st = el.get("style")
            if st:
                el.set("style", ";".join(x for x in st.split(";") if "position:" not in x and "margin-left" not in x and "margin-top" not in x))


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
    inline_anchors(root)
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
