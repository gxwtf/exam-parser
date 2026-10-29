#!/usr/bin/env python3
"""把某学科 JSON 收敛成「只要选择题」：按编号口径剔除实验题/计算题，并把选择题分值统一。

编号口径（题库只收选择题）：
  - 新单科卷（2021 起）：选择题为 1-14 题 → 只保留 id ≤ 14；
  - 理综卷（2015-2018）：物理选择题为 13-20 题 → 只保留 13 ≤ id ≤ 20；
  - 旧单科卷（2014 及以前）：选择题为 1-8 题 → 只保留 id ≤ 8。
判卷方式：先剔除「题干为空」的垃圾题，再用剩下的 id 推断属于哪种口径
（最小 id ≥ 9 → 理综；否则最大 id > 8 → 新单科；否则旧单科）。

用法：
    python3 keep_choice_only.py --type 二模 --subject 物理            # 只看不写
    python3 keep_choice_only.py --type 二模 --subject 物理 --apply    # 真改 JSON
"""
import argparse, json
from pathlib import Path

from paths import OUTPUT_JSON_MATH

CHOICE_SCORE = 3


def keep_rule(ids: list[int]):
    if min(ids) >= 9:
        return (lambda i: 13 <= i <= 20), "理综(13-20)"
    if max(ids) > 8:
        return (lambda i: 1 <= i <= 14), "新单科(1-14)"
    return (lambda i: 1 <= i <= 8), "旧单科(1-8)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--subject", default="物理")
    ap.add_argument("--score", type=float, default=CHOICE_SCORE)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    jdir = OUTPUT_JSON_MATH / args.type / args.subject
    files = sorted(jdir.glob("*.json"))
    if not files:
        raise SystemExit(f"没有 JSON：{jdir}")

    total_drop = total_keep = 0
    for jf in files:
        data = json.loads(jf.read_text(encoding="utf-8"))
        all_q = [q for sec in data["sections"] for q in sec["questions"]]
        junk = {id(q) for q in all_q if not str(q.get("content") or "").strip()}
        alive = [q for q in all_q if id(q) not in junk]
        if not alive:
            print(f"⚠ {jf.stem}：没有有效题目，跳过")
            continue
        ok, style = keep_rule([q["id"] for q in alive])
        dropped = [q["id"] for q in all_q if id(q) in junk or not ok(q["id"])]
        kept = 0
        for sec in data["sections"]:
            keep = []
            for q in sec["questions"]:
                if id(q) in junk or not ok(q["id"]):
                    continue
                q["score"] = args.score
                keep.append(q)
            sec["questions"] = keep
            sec["score"] = args.score * len(keep)
            kept += len(keep)
        total_drop += len(dropped)
        total_keep += kept
        if dropped:
            print(f"{jf.stem}｜{style}｜保留 {kept}，剔除 {dropped}")
        if args.apply and dropped:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        elif args.apply:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'' if args.apply else '[dry-run] '}共 {len(files)} 卷：保留选择题 {total_keep} 道，剔除 {total_drop} 道")


if __name__ == "__main__":
    main()
