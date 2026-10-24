#!/usr/bin/env python3
"""物理：从 docx→MD 里把「插图」按题号挂到题目 JSON 上（公式小图过滤掉）

MD 由 batch_convert_docx_to_md.py 生成：图片已导出到 <卷>/images/，
且在题目段落内按顺序排列。按尺寸过滤掉公式/符号小图，只保留真正的插图。

用法：python3 attach_physics_images.py --type 二模 [--dry-run]
"""
import argparse, json, re, shutil, subprocess
from pathlib import Path
from paths import DATA_ROOT

ROOT = DATA_ROOT
MD_ROOT = ROOT / "output_docx_to_md"
JSON_ROOT = ROOT / "output_json_math"

QSPLIT = re.compile(r"(?m)^\s*(\d{1,2})\s*\\?[．.、]\s*")
IMGREF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
MIN_W, MIN_H = 120, 60


def img_size(p: Path) -> tuple[int, int]:
    try:
        out = subprocess.run(["identify", "-format", "%w %h", str(p)], capture_output=True, text=True, timeout=20).stdout.split()
        return int(out[0]), int(out[1])
    except Exception:
        return 0, 0


def md_images_by_no(md_path: Path) -> dict[int, list[Path]]:
    try:
        txt = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    for marker in ("参考答案", "答案与解析"):
        i = txt.find(marker)
        if i > 0:
            txt = txt[:i]
            break
    hits = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(txt) if 1 <= int(m.group(1)) <= 20]
    out: dict[int, list[Path]] = {}
    for idx, (_, seg_start, no) in enumerate(hits):
        seg_end = hits[idx + 1][0] if idx + 1 < len(hits) else len(txt)
        for ref in IMGREF.findall(txt[seg_start:seg_end]):
            p = md_path.parent / ref
            if not p.exists():
                continue
            w, h = img_size(p)
            if w >= MIN_W and h >= MIN_H:
                out.setdefault(no, []).append(p)
    return out


def find_md(stem: str, type_dir: str) -> Path | None:
    base = MD_ROOT / type_dir / "物理"
    for d in base.glob(f"{stem}*"):
        mds = sorted(d.glob("*.md"))
        if mds:
            return mds[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    json_dir = JSON_ROOT / args.type / "物理"
    files = sorted(json_dir.glob("*.json"))
    total_q = total_img = 0
    missing = []
    for jf in files:
        stem = jf.stem
        md = find_md(stem, args.type)
        if not md:
            missing.append(stem); continue
        imgs = md_images_by_no(md)
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False
        for sec in data["sections"]:
            for q in sec["questions"]:
                no = q["id"]
                content = re.sub(r"\n*!\[\]\([^)]*\)", "", q["content"])   # 先清旧图
                srcs = imgs.get(no, [])
                if srcs:
                    # 复制到与试卷名一致的目录，保证 /exam-images/<类型>/<学科>/<卷名>/images/ 可访问
                    dst_dir = MD_ROOT / args.type / "物理" / stem / "images"
                    if not args.dry_run:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        for p in srcs:
                            if not (dst_dir / p.name).exists():
                                shutil.copyfile(p, dst_dir / p.name)
                    refs = [f"![](images/{p.name})" for p in srcs]
                    content = content.rstrip() + "\n\n" + "\n\n".join(refs)
                    total_img += len(refs)
                if content != q["content"]:
                    q["content"] = content; changed = True; total_q += 1
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}{len(files)} 份卷：{total_q} 题挂图，共 {total_img} 张")
    if missing:
        print(f"⚠ 缺 MD 的卷 {len(missing)} 份：{missing[:3]}")


if __name__ == "__main__":
    main()
