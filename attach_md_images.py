#!/usr/bin/env python3
"""从 mineru 转出的 MD 里，把每道题号下的图片挂到对应题目的 JSON 上。

为什么不靠 AI 读 PDF 裁图：AI 给的 bbox 很不准（实测会框到半页）；
MD 里的图片是 PDF 原始图片对象，位置和归属都可靠。

用法：
  python3 attach_md_images.py --type 二模 [--dry-run]
"""
import argparse, glob, json, os, re, shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
MD_ROOT = ROOT / "output_pdf_to_md"
JSON_ROOT = ROOT / "output_json_math"
IMG_DST_ROOT = ROOT / "output_docx_to_md"

# 题号可能出现在行首（`12．`/`（12）`），也可能夹在行中间（上一题的图/文字后面紧跟 `（13）`）
QSPLIT = re.compile(r"(?:#{1,3}[ \t]*)?(?:[（(]\s*(\d{1,2})\s*[）)]|(?m:^)\s*(\d{1,2})\s*[.．、])")
IMGREF = re.compile(r"!\[\]\((images/[^)]+)\)")


def md_question_images(md_path: Path) -> dict[int, list[Path]]:
    """返回 {题号: [图片绝对路径]}（只取题目区，题号支持行首与行内两种写法）"""
    try:
        txt = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    for marker in ("# 参考答案", "## 参考答案", "# 答案", "参考答案"):
        i = txt.find(marker)
        if i > 0:
            txt = txt[:i]
            break
    hits = []
    for m in QSPLIT.finditer(txt):
        raw = m.group(1) or m.group(2)
        try:
            no = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= no <= 20:
            hits.append((m.start(), m.end(), no))
    out: dict[int, list[Path]] = {}
    for idx, (_, seg_start, no) in enumerate(hits):
        seg_end = hits[idx + 1][0] if idx + 1 < len(hits) else len(txt)
        seg = txt[seg_start:seg_end]
        imgs = [md_path.parent / p for p in IMGREF.findall(seg)]
        imgs = [p for p in imgs if p.exists()]
        if imgs:
            out.setdefault(no, []).extend(imgs)
    return out


def find_md(stem: str, type_dir: str) -> Path | None:
    base = MD_ROOT / type_dir / "数学"
    for d in base.glob(f"{stem}*"):
        cand = d / "auto"
        if cand.is_dir():
            mds = sorted(cand.glob("*.md"))
            if mds:
                return mds[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    json_dir = JSON_ROOT / args.type / "数学"
    files = sorted(json_dir.glob("*.json"))
    total_q = total_img = 0
    missing_md = []

    for jf in files:
        stem = jf.stem
        md = find_md(stem, args.type)
        if not md:
            missing_md.append(stem)
            continue
        imgs_by_no = md_question_images(md)
        data = json.loads(jf.read_text(encoding="utf-8"))
        dst_dir = IMG_DST_ROOT / args.type / "数学" / stem / "images"
        changed = False

        for sec in data["sections"]:
            for q in sec["questions"]:
                no = q["id"]
                # 去掉 AI 裁的图（不可靠）
                # 先清掉这一题之前挂过的所有图（AI 裁的 .png 与上轮 MD 的 _qN_M.jpg）
                content = re.sub(r"\n*!\[\]\(images/" + re.escape(stem) + r"_q\d+[^)]*\)", "", q["content"])
                src_imgs = imgs_by_no.get(no, [])
                if src_imgs:
                    refs = []
                    for i, src in enumerate(src_imgs, 1):
                        ext = src.suffix.lower() or ".jpg"
                        name = f"{stem}_q{no}_{i}{ext}"
                        if not args.dry_run:
                            dst_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(src, dst_dir / name)
                        refs.append(f"![](images/{name})")
                    content = content.rstrip() + "\n\n" + "\n\n".join(refs)
                    total_img += len(refs)
                if content != q["content"]:
                    q["content"] = content
                    changed = True
                    total_q += 1
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'[dry-run] ' if args.dry_run else ''}处理 {len(files)} 份卷：{total_q} 题挂上图片，共 {total_img} 张")
    if missing_md:
        print(f"⚠ 缺 MD 的卷 {len(missing_md)} 份：{missing_md[:3]}")


if __name__ == "__main__":
    main()
