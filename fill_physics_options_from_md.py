#!/usr/bin/env python3
"""物理：用 docx→MD 的文本补全 AI 漏掉的选项（典型场景：题干与选项跨页）

MD 里选项通常是 A．/B．/C．/D． 文本行；若某些卷选项本身是图片，则仍补不上（会列进复核清单）。

用法：python3 fill_physics_options_from_md.py --type 二模 [--dry-run]
"""
import argparse, json, re
from pathlib import Path
import convert_math_pdf as M
from paths import DATA_ROOT

ROOT = DATA_ROOT
MD_ROOT = ROOT / "output_docx_to_md"
JSON_ROOT = ROOT / "output_json_math"

QSPLIT = re.compile(r"(?m)^\s*(\d{1,2})\s*\\?[．.、]\s*")
OPT_LINE = re.compile(r"(?m)^\s*([A-Da-d])\s*\\?[．.、]\s*(.+?)\s*$")


def md_questions(md_path: Path) -> dict[int, dict]:
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
    out: dict[int, dict] = {}
    for idx, (_, seg_start, no) in enumerate(hits):
        seg_end = hits[idx + 1][0] if idx + 1 < len(hits) else len(txt)
        seg = txt[seg_start:seg_end]
        opts = {}
        for m in OPT_LINE.finditer(seg):
            oid = m.group(1).lower()
            if oid not in opts:
                opts[oid] = m.group(2).strip()
        # 也支持「A．x B．y」挤在一行
        if len(opts) < 4:
            flat = re.sub(r"\s+", " ", seg)
            for m in re.finditer(r"([A-D])\s*[．.]\s*(.+?)(?=\s*[A-D]\s*[．.]|$)", flat):
                oid = m.group(1).lower()
                if oid not in opts and m.group(2).strip():
                    opts[oid] = m.group(2).strip()
        stem = QSPLIT.split(seg, maxsplit=1)[0]
        out[no] = {"stem": stem.strip(), "options": opts}
    return out


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
    filled = 0; still_missing = []
    for jf in files:
        md = find_md(jf.stem, args.type)
        if not md:
            continue
        mdq = md_questions(md)
        # MD 的题号可能和 PDF 不一致（PDF 13–20 vs MD 1–8）→ 按顺序对应
        md_order = [mdq[k] for k in sorted(mdq)]
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False
        for sec in data["sections"]:
            for pos, q in enumerate(sec["questions"]):
                miss = [o for o in q["options"] if not o["label"]]
                if not miss:
                    continue
                by_no = mdq.get(q["id"], {}).get("options", {})
                src = by_no if len(by_no) >= len(miss) else (md_order[pos] if pos < len(md_order) else {}).get("options", {})
                got = 0
                for o in miss:
                    raw = src.get(o["id"], "").strip()
                    if not raw:
                        continue
                    # wmf 已批量转成 png：把引用改指 png，图片保留
                    lab = re.sub(r"\.wmf\)", ".png)", raw)
                    if not lab.strip():
                        continue
                    if lab:
                        lab = M.wrap_numbers_in_text(M.wrap_plain_math(M.strip_opt_prefix(M.normalize_latex(lab))))
                        o["label"] = lab
                        got += 1
                if got:
                    filled += got; changed = True
                left = [o["id"] for o in q["options"] if not o["label"]]
                if left:
                    still_missing.append(f"{jf.stem} 第{q['id']}题 缺 {'/'.join(left)}")
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}补全选项 {filled} 个")
    if still_missing:
        print(f"仍缺选项 {len(still_missing)} 题：")
        for x in still_missing[:10]:
            print("   ", x)


if __name__ == "__main__":
    main()
