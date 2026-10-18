#!/usr/bin/env python3
"""物理：选项内容是图片的题（MD 里没有选项文字），把该题的选项图切出来当选项

策略：在 MD 的题目区按题号取段落 → 收集段落内所有图片（按出现顺序）→
若图片数 ≥ 待补选项数，取**最后 N 张**作为选项图（选项图通常排在题干配图之后）；
否则整题跳过并记入复核清单。

用法：python3 fill_physics_option_images.py --type 二模 [--dry-run]
"""
import argparse, json, re, shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
MD_ROOT = ROOT / "output_docx_to_md"
JSON_ROOT = ROOT / "output_json_math"
IMGREF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")


def md_question_segments(md_path: Path) -> dict[int, str]:
    try:
        txt = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    for marker in ("参考答案", "答案与解析"):
        i = txt.find(marker)
        if i > 0:
            txt = txt[:i]
            break
    segs: dict[int, str] = {}
    marks = [(m.start(), m.end(), int(m.group(1))) for m in re.finditer(r"(?m)^\s*(\d{1,2})\s*[．.、]\s*", txt) if 1 <= int(m.group(1)) <= 25]
    for idx, (_, start, no) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(txt)
        segs.setdefault(no, txt[start:end])
    return segs


def find_md(stem: str, type_dir: str) -> Path | None:
    for d in (MD_ROOT / type_dir / "物理").glob(f"{stem}*"):
        mds = sorted(d.glob("*.md"))
        if mds:
            return mds[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted((JSON_ROOT / args.type / "物理").glob("*.json"))
    filled_q = filled_o = 0
    skipped = []
    quarter_split = []
    for jf in files:
        md = find_md(jf.stem, args.type)
        if not md:
            continue
        segs = md_question_segments(md)
        seg_order = [segs[k] for k in sorted(segs)]
        data = json.loads(jf.read_text(encoding="utf-8"))
        dst_dir = MD_ROOT / args.type / "物理" / jf.stem / "images"
        changed = False
        for sec in data["sections"]:
            for pos, q in enumerate(sec["questions"]):
                miss = [o for o in q["options"] if not o["label"]]
                if not miss:
                    continue
                seg_by_no = segs.get(q["id"], "")
                n_by_no = len(IMGREF.findall(seg_by_no))
                seg = seg_by_no if n_by_no >= len(miss) else (seg_order[pos] if pos < len(seg_order) else "")
                imgs = [md.parent / p for p in IMGREF.findall(seg) if (md.parent / p).exists()]
                if len(imgs) < len(miss):
                    # 兜底：若整题只有 1 张图且缺 4 个选项 → 常见于「四个子图拼成一张」，
                    # 按等宽四等分裁切，作为 a/b/c/d（会记入复核清单，供人工核对）
                    if len(imgs) == 1 and len(miss) == 4:
                        src_img = imgs[0]
                        if not args.dry_run:
                            dst_dir.mkdir(parents=True, exist_ok=True)
                        for i, o in enumerate(miss):
                            name = f"{jf.stem}_q{q['id']}_{o['id']}{src_img.suffix}"
                            if not args.dry_run:
                                import subprocess
                                subprocess.run(["convert", str(src_img), "-crop", f"25%x100%+{i*25}%+0", "+repage",
                                                str(dst_dir / name)], check=True, capture_output=True)
                            o["label"] = f"![](images/{name})"
                        filled_q += 1; filled_o += len(miss); changed = True
                        quarter_split.append(f"{jf.stem} 第{q['id']}题（等分裁切）")
                        continue
                    skipped.append(f"{jf.stem} 第{q['id']}题（图 {len(imgs)} < 缺 {len(miss)}）")
                    continue
                picked = imgs[-len(miss):]
                if not args.dry_run:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for o, src in zip(miss, picked):
                        name = f"{jf.stem}_q{q['id']}_{o['id']}{src.suffix}"
                        shutil.copyfile(src, dst_dir / name)
                        o["label"] = f"![](images/{name})"
                else:
                    for o, src in zip(miss, picked):
                        o["label"] = f"![]({src.name})"
                filled_q += 1; filled_o += len(miss); changed = True
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}用选项图补全 {filled_q} 题 / {filled_o} 个选项")
    if quarter_split:
        print(f"其中等分裁切的题 {len(quarter_split)} 道（需人工核对）：{quarter_split[:3]}")
        (JSON_ROOT / args.type / "物理" / "选项图等分裁切_复核.txt").write_text("\n".join(quarter_split), encoding="utf-8")
    if skipped:
        print(f"仍无法补（图不够）：{len(skipped)} 题")
        for x in skipped[:8]:
            print("   ", x)


if __name__ == "__main__":
    main()
