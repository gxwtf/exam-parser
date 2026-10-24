#!/usr/bin/env python3
"""物理后处理：① 用 MD 覆盖含公式图的选项/题干 ② wmf→png ③ 图片复制到服务目录

用法：python3 postprocess_physics.py --type 二模 [--dry-run]
"""
import argparse, json, re, shutil
from pathlib import Path
from paths import DATA_ROOT

IMGREF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
ROOT = DATA_ROOT
MD_ROOT = ROOT / "output_docx_to_md"
JROOT = ROOT / "output_json_math"


def md_segments(md: Path) -> list[str]:
    txt = md.read_text(encoding="utf-8")
    for m in ("参考答案", "答案与解析"):
        i = txt.find(m)
        if i > 0:
            txt = txt[:i]; break
    marks = [(x.start(), x.end()) for x in re.finditer(r"(?m)^\s*\d{1,2}\s*\\?[．.]\s*", txt)]
    return [txt[st:(marks[i+1][0] if i+1 < len(marks) else len(txt))] for i, (_, st) in enumerate(marks)]


def md_options(seg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"(?m)^\s*([A-Da-d])\s*\\?[．.]\s*(.+?)\s*$", seg):
        k = m.group(1).lower()
        if k not in out:
            out[k] = m.group(2).strip()
    if len(out) < 4:   # 同行多选项
        flat = re.sub(r"\s+", " ", seg)
        for m in re.finditer(r"([A-D])\s*[．.]\s*(.+?)(?=\s*[A-D]\s*[．.]|$)", flat):
            k = m.group(1).lower()
            if k not in out and m.group(2).strip():
                out[k] = m.group(2).strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    overwritten = copied = 0
    for jf in sorted((JROOT / args.type / "物理").glob("*.json")):
        stem = jf.stem
        mds = [d for d in (MD_ROOT / args.type / "物理").glob(stem + "*") if list(d.glob("*.md"))]
        if not mds:
            continue
        mddir = mds[0]
        segs = md_segments(mddir / f"{mddir.name}.md") if (mddir / f"{mddir.name}.md").exists() else []
        if not segs:
            mdfiles = sorted(mddir.glob("*.md"))
            segs = md_segments(mdfiles[0]) if mdfiles else []
        served = mddir / "images"
        dst = MD_ROOT / args.type / "物理" / stem / "images"
        data = json.loads(jf.read_text(encoding="utf-8")); changed = False

        for sec in data["sections"]:
            for pos, q in enumerate(sec["questions"]):
                if pos >= len(segs):
                    break
                seg = segs[pos]
                mopt = md_options(seg)
                for o in q["options"]:
                    src = mopt.get(o["id"], "")
                    if "![" not in src:
                        continue
                    cur = str(o["label"]).strip()
                    # AI 已经识别出文字/LaTeX 的，以 AI 为准，不要用 MD 的图片覆盖
                    if cur and "![" not in cur:
                        continue
                    lab = re.sub(r"\.wmf\)", ".png)", src)
                    if lab != o["label"]:
                        o["label"] = lab; overwritten += 1; changed = True
                # 选项里串了多张图（A/B/C/D 挤在一个 label）→ 每个选项只留自己那张
                counts = [len(IMGREF.findall(str(o["label"]))) for o in q["options"]]
                if max(counts or [0]) > 1:
                    names = [Path(m).name for o in q["options"] for m in IMGREF.findall(str(o["label"]))]
                    uniq = list(dict.fromkeys(names))
                    if len(uniq) >= 4:
                        for i, o in enumerate(q["options"]):
                            o["label"] = f"![](images/{uniq[i]})"

                # 题干里不要重复选项图
                opt_imgs = {Path(m).name for o in q["options"] for m in IMGREF.findall(str(o["label"]))}
                if opt_imgs:
                    q["content"] = re.sub(r"\n{3,}", "\n\n", IMGREF.sub(
                        lambda m: "" if Path(m.group(1)).name in opt_imgs else m.group(0), q["content"]
                    )).strip()

                # 表格里的 wmf → png
                newc = re.sub(r"\.wmf\)", ".png)", q["content"])
                if newc != q["content"]:
                    q["content"] = newc; changed = True

        # 复制引用到的图片到服务目录
        refs = set()
        for sec in data["sections"]:
            for q in sec["questions"]:
                refs |= set(re.findall(r"!\[[^\]]*\]\((images/[^)]+)\)", q["content"]))
                for o in q["options"]:
                    refs |= set(re.findall(r"!\[[^\]]*\]\((images/[^)]+)\)", str(o["label"])))
        if refs and not args.dry_run:
            dst.mkdir(parents=True, exist_ok=True)
            for ref in refs:
                name = Path(ref).name
                src = served / name
                if src.exists() and not (dst / name).exists():
                    shutil.copyfile(src, dst / name); copied += 1
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}覆盖为含图选项 {overwritten} 处｜复制图片 {copied} 张")


if __name__ == "__main__":
    main()
