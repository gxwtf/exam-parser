#!/usr/bin/env python3
"""物理：把 MD 里的表格（题中数据表）补回题干预备

AI 逐页识别时会丢掉表格；docx→MD 里表格是 markdown 表格（| 分隔），
按题号（取不到再按顺序）取该题段落里的表格，追加到题干末尾。

用法：python3 attach_physics_tables.py --type 二模 [--dry-run]
"""
import argparse, json, re
from pathlib import Path
from paths import DATA_ROOT

ROOT = DATA_ROOT
MD_ROOT = ROOT / "output_docx_to_md"
JSON_ROOT = ROOT / "output_json_math"


def segments(md_path: Path) -> list[str]:
    try:
        txt = md_path.read_text(encoding="utf-8")
    except Exception:
        return []
    for marker in ("参考答案", "答案与解析"):
        i = txt.find(marker)
        if i > 0:
            txt = txt[:i]
            break
    marks = [(m.start(), m.end()) for m in re.finditer(r"(?m)^\s*\d{1,2}\s*\\?[．.]\s*", txt)]
    out = []
    for idx, (_, st) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(txt)
        out.append(txt[st:end])
    return out


def tables_in(seg: str) -> list[str]:
    lines = seg.split("\n")
    blocks, cur = [], []
    for ln in lines:
        if ln.strip().startswith("|"):
            cur.append(ln.rstrip())
        else:
            if len(cur) >= 2:
                blocks.append("\n".join(cur))
            cur = []
    if len(cur) >= 2:
        blocks.append("\n".join(cur))
    return blocks


def find_md(stem: str, type_dir: str) -> Path | None:
    for d in (MD_ROOT / type_dir / "物理").glob(stem + "*"):
        mds = sorted(d.glob("*.md"))
        if mds:
            return mds[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    filled = 0
    for jf in sorted((JSON_ROOT / args.type / "物理").glob("*.json")):
        md = find_md(jf.stem, args.type)
        if not md:
            continue
        segs = segments(md)
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False
        for sec in data["sections"]:
            for pos, q in enumerate(sec["questions"]):
                if pos >= len(segs):
                    break
                tbls = tables_in(segs[pos])
                if not tbls:
                    continue
                have = "\n".join(tbls)
                if have[:40] in q["content"]:
                    continue
                q["content"] = q["content"].rstrip() + "\n\n" + "\n\n".join(tbls)
                filled += 1; changed = True
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}补回表格的题：{filled}")


if __name__ == "__main__":
    main()
