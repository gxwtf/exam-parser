#!/usr/bin/env python3
"""把 Word 的 .emf 图形（Word 里画的公式箭头/装置图/流程图）批量转 PNG。

ImageMagick 不支持 EMF、wmf2gd 只认 WMF，所以走 LibreOffice：
每张 EMF 生成一个 HTML → soffice 一次性批量转 PDF → pdftoppm 出图 → trim 白边 → 存回 `<原名>.png`。

用法：python3 convert_emf_to_png.py <搜索根目录> [--jobs 1]
"""
import argparse, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    root = Path(args.root)
    emfs = [p for p in root.rglob("*.emf") if not p.with_suffix(".png").exists()]
    print(f"待转换 EMF {len(emfs)} 张", flush=True)
    if not emfs:
        return

    tmp = Path(tempfile.mkdtemp(prefix="emf2png-"))
    html_dir = tmp / "html"
    html_dir.mkdir()
    index = {}
    for i, emf in enumerate(emfs):
        name = f"e{i:04d}"
        index[name] = emf
        (html_dir / f"{name}.html").write_text(
            f"<html><body><img src='file://{emf}'></body></html>", encoding="utf-8")

    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp)]
                   + [str(p) for p in sorted(html_dir.glob("*.html"))],
                   capture_output=True, timeout=7200)

    def one(item):
        name, emf = item
        pdf = tmp / f"{name}.pdf"
        if not pdf.exists():
            return f"✗ {emf.name}（无 PDF）"
        png = tmp / f"{name}.png"
        subprocess.run(["pdftoppm", "-png", "-r", str(args.dpi), "-singlefile", str(pdf), str(tmp / name)],
                       capture_output=True)
        if not png.exists():
            return f"✗ {emf.name}（无 PNG）"
        subprocess.run(["convert", str(png), "-trim", "+repage", str(emf.with_suffix(".png"))],
                       capture_output=True)
        return None

    ok = bad = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(one, index.items()):
            if res:
                print(res, flush=True); bad += 1
            else:
                ok += 1
    print(f"完成：成功 {ok}，失败 {bad}，临时目录 {tmp}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
