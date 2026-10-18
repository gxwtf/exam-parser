#!/usr/bin/env python3
"""把 manual_tables/<卷名>_q<题号>.md 里的表格写进对应题（替换题内所有表格）

文件名约定：<试卷stem>_q<题号>.md

用法：python3 apply_manual_tables.py --type 二模 [--dry-run]
"""
import argparse, json, re
from pathlib import Path

ROOT = Path(__file__).parent.parent
JSON_ROOT = ROOT / "output_json_math"
TBL_DIR = Path(__file__).parent / "manual_tables"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--subject", default="物理")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    n = 0
    for tf in sorted(TBL_DIR.glob(f"*_q*.md")):
        m = re.match(r"(.+)_q(\d+)\.md$", tf.name)
        if not m:
            continue
        stem, no = m.group(1), int(m.group(2))
        jf = JSON_ROOT / args.type / args.subject / f"{stem}.json"
        if not jf.exists():
            print(f"  跳过（无 JSON）：{stem}"); continue
        table = tf.read_text(encoding="utf-8").strip()
        data = json.loads(jf.read_text(encoding="utf-8")); changed = False
        for sec in data["sections"]:
            for q in sec["questions"]:
                if q["id"] != no:
                    continue
                lines = [ln for ln in q["content"].split("\n") if not ln.startswith("|")]
                content = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
                q["content"] = content + "\n\n" + table
                changed = True; n += 1
                print(f"  已应用手工表格：{stem} 第{no}题")
        if changed and not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}应用手工表格 {n} 处")


if __name__ == "__main__":
    main()
