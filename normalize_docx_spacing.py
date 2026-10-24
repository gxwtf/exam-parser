#!/usr/bin/env python3
"""把试卷 docx 里所有正文段落的行距/段间距统一成「第一题所在段落」的设置。

为什么要做：教师版 Word 里各题段落行距不一致（有的 1.0、有的 1.5），
转成图片后看起来「行距不统一」。这里以第 1 题段落为基准，统一全文段落间距。

用法：python3 normalize_docx_spacing.py -p <docx> -o <输出docx>
"""
import argparse, copy, re, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def para_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--q", type=int, default=1, help="以第几题段落为基准（默认第1题）")
    args = ap.parse_args()

    src = Path(args.path); dst = Path(args.out)
    with zipfile.ZipFile(src) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    paras = [el for el in body.iter(W + "p")]

    # 找基准段落：题号行
    base_spacing = None
    base_ind = None
    for p in paras:
        if re.match(rf"^{args.q}\s*[．.]", para_text(p).strip()):
            ppr = p.find(W + "pPr")
            if ppr is not None:
                base_spacing = ppr.find(W + "spacing")
                base_ind = ppr.find(W + "ind")
            break
    if base_spacing is None:
        base_spacing = ET.Element(W + "spacing", {W + "line": "360", W + "lineRule": "auto"})

    changed = 0
    for p in paras:
        txt = para_text(p).strip()
        if not txt:
            continue
        ppr = p.find(W + "pPr")
        if ppr is None:
            ppr = ET.SubElement(p, W + "pPr")
        old = ppr.find(W + "spacing")
        if old is not None:
            ppr.remove(old)
        sp = copy.deepcopy(base_spacing)
        # 插到 pPr 靠前的位置（保证 schema 顺序大致合理）
        ppr.insert(0, sp)
        changed += 1

    ET.register_namespace("w", W[1:-1])
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml_bytes
            zout.writestr(item, data)
    print(f"统一 {changed} 个段落的行距/段距（基准：第 {args.q} 题）")


if __name__ == "__main__":
    main()
