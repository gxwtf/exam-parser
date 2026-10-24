#!/usr/bin/env python3
"""重排试卷 docx：统一段落行距 + 把浮动图/文本框改成内联，供「规整版式」使用。

- 段落行距/段前段后：以第 1 题所在段落为基准，统一全文
- DrawingML 浮动图（wp:anchor）→ wp:inline（保留 extent / docPr / graphic）
- 段落 w:framePr（文本框）去掉
- VML 图形（w:pict/v:shape）去掉 position:absolute 与 margin 偏移，随文字流排

用法：python3 reflow_docx.py -p <docx> -o <out.docx> [--q 1]
"""
import argparse, copy, re, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
}
W = "{%s}" % NS["w"]
WP = "{%s}" % NS["wp"]


def para_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def anchor_to_inline(root):
    """把 wp:anchor 就地改成 wp:inline：只去掉定位/环绕子元素，保留 graphic 等原有结构
    （不少图是包在 mc:AlternateContent 里，重建 inline 会丢图）"""
    DROP = {"simplePos", "positionH", "positionV", "behindDoc", "locked",
            "layoutInCell", "allowOverlap", "wrapNone", "wrapSquare", "wrapTight",
            "wrapThrough", "wrapTopAndBottom"}
    changed = 0
    for el in list(root.iter()):
        if el.tag != WP + "anchor":
            continue
        el.tag = WP + "inline"
        for child in list(el):
            local = child.tag.split("}")[-1]
            if local in DROP:
                el.remove(child)
        changed += 1
    return changed


def move_inline_images_to_para_end(root):
    """内联图默认插在 run 的位置（常在文字前），这里把含图的 run 移到段落末尾，
    让「选项文字 + 图」按自然顺序显示"""
    n = 0
    for p in list(root.iter(W + "p")):
        runs = [r for r in p.findall(W + "r")]
        moved = []
        for r in runs:
            has_img = any(_.tag == WP + "inline" for _ in r.iter())
            has_text = any((t.text or "").strip() for t in r.iter(W + "t"))
            if has_img and not has_text:      # 只搬「纯图片」的 run，文字/公式条件不动
                moved.append(r)
        for r in moved:
            if r in list(p):
                p.remove(r)
                p.append(r)
                n += 1
    return n


def strip_frames(root):
    n = 0
    for ppr in root.iter(W + "pPr"):
        for fr in ppr.findall(W + "framePr"):
            ppr.remove(fr)
            n += 1
    return n


def vml_inline(root):
    """VML 图形处理：
    - 带文字的（反应条件「电解」「△」等文本框）：抽成普通文字「（电解）」放到段落末尾，避免错位
    - 不带文字的（化学结构式等）：去掉绝对定位，随文字流排
    """
    n = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if not child.tag.endswith("}pict"):
                continue
            txt = "".join((t.text or "") for t in child.iter(W + "t")).strip()
            if txt:
                run = ET.Element(W + "r")
                rpr = ET.SubElement(run, W + "rPr")
                size = ET.SubElement(rpr, W + "sz"); size.set(W + "val", "20")
                t = ET.SubElement(run, W + "t")
                t.text = f"（{txt}）"
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                parent.insert(list(parent).index(child), run)
                parent.remove(child)
                n += 1
    for el in root.iter():
        st = el.get("style")
        if not st:
            continue
        parts = [x for x in st.split(";") if x.strip()]
        keep = [x for x in parts if not re.match(r"\s*(position|margin-left|margin-top|mso-position-[a-z-]+)\s*:", x)]
        if len(keep) != len(parts):
            el.set("style", ";".join(keep))
    return n


def unify_spacing(root, q: int):
    paras = [el for el in root.find(W + "body").iter(W + "p")]
    base_spacing, base_ind = None, None
    for p in paras:
        if re.match(rf"^{q}\s*[．.]", para_text(p).strip()):
            ppr = p.find(W + "pPr")
            if ppr is not None:
                base_spacing = ppr.find(W + "spacing")
                base_ind = ppr.find(W + "ind")
            break
    if base_spacing is None:
        base_spacing = ET.Element(W + "spacing", {W + "line": "360", W + "lineRule": "auto"})
    n = 0
    for p in paras:
        if not para_text(p).strip():
            continue
        ppr = p.find(W + "pPr")
        if ppr is None:
            ppr = ET.Element(W + "pPr")
            p.insert(0, ppr)
        old = ppr.find(W + "spacing")
        if old is not None:
            ppr.remove(old)
        ppr.insert(0, copy.deepcopy(base_spacing))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--q", type=int, default=1)
    args = ap.parse_args()

    src, dst = Path(args.path), Path(args.out)
    with zipfile.ZipFile(src) as z:
        xml = z.read("word/document.xml")
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(xml)
    a = anchor_to_inline(root)
    m = move_inline_images_to_para_end(root)
    f = strip_frames(root)
    v = vml_inline(root)
    s = unify_spacing(root, args.q)
    out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = out
            zout.writestr(item, data)
    print(f"浮动图内联 {a} 处（其中 {m} 处移到段落末尾）/ 去文本框 {f} 处 / VML 去定位 {v} 处 / 统一段落 {s} 个")


if __name__ == "__main__":
    main()
