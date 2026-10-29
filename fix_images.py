#!/usr/bin/env python3
"""题库 JSON 的图片收尾（任意学科）：
  1. 引用里的 .tiff/.emf/.wmf → 转成 .png 并把引用改成 .png（nginx 只放行 png/jpg/jpeg/gif/webp）
  2. 引用指向不存在文件的，尽量在同目录里找同名 .tiff/.tif 转过去
用法：
    python3 fix_images.py --type 二模 --subject 物理 [--dry-run]
    python3 fix_images.py --dir output_json_math/二模/化学 [--dry-run]   # 学科/类型从路径推断
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


def resolve_dirs(args) -> tuple[Path, Path, str]:
    """返回 (JSON 目录, 图片根目录 <data>/output_docx_to_md/<类型>/<学科>, 学科名)"""
    if args.dir:
        jdir = Path(args.dir).expanduser().resolve()
        try:
            type_dir, subject = jdir.relative_to(OUTPUT_JSON_MATH.resolve()).parts[:2]
        except ValueError:
            raise SystemExit(f"--dir 必须位于 {OUTPUT_JSON_MATH} 下的 <类型>/<学科>：{jdir}")
    else:
        type_dir, subject = args.type, args.subject
        jdir = OUTPUT_JSON_MATH / type_dir / subject
    return jdir, MD_ROOT / type_dir / subject, subject


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="JSON 目录（含 <类型>/<学科>）")
    ap.add_argument("--type", default="二模", help="试卷类型，如 二模")
    ap.add_argument("--subject", default="化学", help="学科，如 化学/物理/数学")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    jdir, img_root, subject = resolve_dirs(args)
    if not jdir.is_dir():
        raise SystemExit(f"JSON 目录不存在：{jdir}")
    print(f"学科 {subject}｜JSON {jdir}｜图片 {img_root}")
    converted = rewritten = missing = 0
    missing_refs: dict[str, str] = {}
    for jf in sorted(jdir.glob("*.json")):
        stem = jf.stem
        img_dir = img_root / stem / "images"
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
                    missing_refs[f"{stem}/{ref}"] = "文件不存在"
                    continue
                if ext in (".tiff", ".tif", ".emf", ".wmf") or p.suffix.lower() in (".tiff", ".tif", ".emf", ".wmf"):
                    png = to_png(p) if p.suffix.lower() not in (".emf", ".wmf") else p.with_suffix(".png")
                    if png and png.exists():
                        converted += 1
                        out = out.replace(f"images/{ref}", f"images/{png.name}")
                    else:
                        missing_refs[f"{stem}/{ref}"] = "转 png 失败"
                elif ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    missing_refs[f"{stem}/{ref}"] = "非 web 格式"
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
    for k, why in list(missing_refs.items())[:20]:
        print(f"  ⚠ {k}（{why}）")


if __name__ == "__main__":
    main()
