#!/usr/bin/env python3
"""化学题库 JSON 的图片收尾：
  1. 引用里的 .tiff/.emf/.wmf → 转成 .png 并把引用改成 .png（nginx 只放行 png/jpg/jpeg/gif/webp）
  2. 引用指向不存在文件的，尽量在同目录里找同名 .tiff/.tif 转过去
用法：python3 fix_images.py [--dir ...] [--dry-run]
"""
import argparse, json, re, subprocess
from pathlib import Path
from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_MATH

MD_ROOT = OUTPUT_DOCX_TO_MD
REF = re.compile(r"images/([^)\s]+)")


def to_png(src: Path) -> Path | None:
    dst = src.with_suffix(".png")
    if dst.exists():
        return dst
    try:
        subprocess.run(["convert", str(src), "-background", "white", "-alpha", "remove", "-alpha", "off", str(dst)],
                       capture_output=True, timeout=120)
    except Exception:
        return None
    return dst if dst.exists() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(OUTPUT_JSON_MATH / "二模" / "化学"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    jdir = Path(args.dir)
    converted = rewritten = missing = 0
    for jf in sorted(jdir.glob("*.json")):
        stem = jf.stem
        img_dir = MD_ROOT / "二模" / "化学" / stem / "images"
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False

        def fix(text: str) -> str:
            nonlocal converted, rewritten, missing
            out = text
            for ref in set(REF.findall(text)):
                p = img_dir / ref
                ext = p.suffix.lower()
                if not p.exists():
                    base = p.with_suffix("")
                    for cand in (base.with_suffix(".tiff"), base.with_suffix(".tif")):
                        if cand.exists():
                            p = to_png(cand) or p
                            break
                if not p.exists():
                    missing += 1
                    continue
                if ext in (".tiff", ".tif", ".emf", ".wmf") or p.suffix.lower() in (".tiff", ".tif", ".emf", ".wmf"):
                    png = to_png(p) if p.suffix.lower() not in (".emf", ".wmf") else p.with_suffix(".png")
                    if png and png.exists():
                        converted += 1
                        out = out.replace(f"images/{ref}", f"images/{png.name}")
            return out

        for sec in data.get("sections", []):
            for q in sec.get("questions", []):
                new = fix(q.get("content", ""))
                if new != q.get("content"):
                    q["content"] = new; changed = True; rewritten += 1
                for o in q.get("options", []):
                    nl = fix(str(o.get("label", "")))
                    if nl != o.get("label"):
                        o["label"] = nl; changed = True; rewritten += 1
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}转 png {converted} 张｜改写字段 {rewritten} 个｜仍缺引用 {missing} 处")


if __name__ == "__main__":
    main()
