#!/usr/bin/env python3
"""盘点「素材目录里有、题库里没有」的套卷，输出 txt。

用法：python3 audit_missing_papers.py [-o 输出文件]
默认输出 /home/gxwtf/missing-papers-<日期>.txt
"""
import argparse
import os
import re
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path

DATA = Path("/home/gxwtf/jingshibang/gaozhong")
ouch = ["上期中", "上期末", "下期末", "一模", "二模", "真题"]
SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "历史", "政治", "地理"]
DB = "gxexercise"
env = dict(os.environ, PGPASSWORD="wddxgy_2714")


def psql(sql: str) -> str:
    return subprocess.run(["psql", "-h", "localhost", "-U", "postgres", "-d", DB, "-t", "-A", "-c", sql],
                          capture_output=True, text=True, env=env).stdout


def norm(t: str) -> str:
    t = re.sub(r"（教师版）|\(教师版\)|含答案|教师版|（图片版）|\(图片版\)|（解析版）|\(解析版\)", "", t)
    t = re.sub(r"[\s\u3000（）()【】\[\]·・_\-—－．.]", "", t)
    return t.strip().lower()


def db_titles() -> dict[str, set[str]]:
    out = psql('select subject || chr(9) || title from "TestPaper";')
    d = defaultdict(set)
    for line in out.strip().splitlines():
        if "\t" in line:
            s, t = line.split("\t", 1)
            d[s].add(t)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    out_path = Path(args.out) if args.out else Path(f"/home/gxwtf/missing-papers-{date.today():%Y%m%d}.txt")

    dbt = db_titles()
    lines, total_missing = [], 0
    summary = []
    outside: list[tuple[str, str, str, str]] = []
    matrix: dict[str, dict[str, tuple[int, int]]] = {}
    for subject in SUBJECTS:
        dbn = {norm(t): t for t in dbt.get(subject, set())}
        rows = []
        for typ in ouch:
            d = DATA / typ / subject
            if not d.is_dir():
                continue
            stems = {}
            for f in list(d.glob("*.pdf")) + list(d.glob("*.docx")) + list(d.glob("*.doc")):
                m = re.search(r"(20\d{2})", f.stem)
                if not m:
                    continue
                stem = re.sub(r"（教师版）|\(教师版\)|含答案|教师版", "", f.stem).strip()
                key = norm(stem)
                # 同卷多份文件时优先教师版
                if key not in stems or "教师版" in f.stem:
                    stems[key] = (stem, f.name)
            for key, (stem, fname) in sorted(stems.items()):
                if typ == "真题" and "北京" not in stem:
                    outside.append((subject, typ, stem, fname))
                    continue
                hit = key in dbn or any(key == k or key in k or k in key for k in dbn)
                rows.append((typ, stem, fname, hit))
        miss = [r for r in rows if not r[3]]
        total_missing += len(miss)
        summary.append((subject, len(rows), len(rows) - len(miss), len(miss)))
        per: dict[str, list[int]] = {}
        for typ in ouch:
            sub = [r for r in rows if r[0] == typ]
            if sub:
                per[typ] = [len(sub), sum(1 for r in sub if r[3])]
        matrix[subject] = per
        if miss:
            lines.append(f"\n===== {subject}：素材 {len(rows)} 卷 / 已入库 {len(rows)-len(miss)} / 缺 {len(miss)} =====")
            cur = None
            for typ, stem, fname, _ in miss:
                if typ != cur:
                    lines.append(f"  -- {typ} --")
                    cur = typ
                lines.append(f"    [缺] {stem}")
                lines.append(f"         源文件：{typ}/{subject}/{fname}")

    head = [f"题库存量缺口盘点　{date.today():%Y-%m-%d} {os.popen('date +%H:%M').read().strip()}",
            "口径：素材目录 gaozhong/<类型>/<学科> 下的 PDF/DOCX（同名只算一份，优先教师版）；",
            "     与库里 TestPaper 标题按「去空格/括号/教师版」归一后比对（允许包含关系）。",
            "",
            f"{'学科':<4}{'素材卷数':>8}{'已入库':>8}{'缺':>8}"]
    for s, a, b, c in summary:
        head.append(f"{s:<4}{a:>10}{b:>10}{c:>8}")
    head.append(f"{'合计':<4}{sum(x[1] for x in summary):>10}{sum(x[2] for x in summary):>10}{total_missing:>8}")
    head.append("")
    head.append("按「学科 × 类型」缺口（格式：缺 / 素材）")
    head.append("学科    " + "".join(f"{t:>12}" for t in ouch))
    for subject in SUBJECTS:
        per = matrix.get(subject, {})
        if not per:
            continue
        cells = []
        for t in ouch:
            v = per.get(t)
            cells.append(f"{(v[0]-v[1])}/{v[0]}" if v else "-")
        head.append(f"{subject:<6}" + "".join(f"{c:>12}" for c in cells))
    if outside:
        head.append("")
        head.append(f"另外：真题目录里的外省卷 {len(outside)} 卷（不含「北京」，按约定不入库，未列入下方明细）")
    out_path.write_text("\n".join(head + lines) + "\n", encoding="utf-8")
    print("\n".join(head))
    print(f"\n明细已写入：{out_path}")


if __name__ == "__main__":
    main()
