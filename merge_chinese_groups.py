#!/usr/bin/env python3
"""合并同一份卷里「同 category 的重复题组」（旧卷 AI 规划偶尔把同一题型拆成 2-3 组）。

- 题目按原顺序拼接
- 题组 content 取第一个非空
- 题组总分取合并后题目分之和
用法：python3 merge_chinese_groups.py [--dir ...]
"""
import argparse, json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent.parent / "output_json_chinese" / "二模"))
    args = ap.parse_args()
    changed = 0
    for f in sorted(Path(args.dir).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        groups = d.get("groups") or []
        if not groups:
            continue
        merged: dict[str, dict] = {}
        order = []
        for g in groups:
            cat = g["category"]
            if cat not in merged:
                merged[cat] = dict(g)
                merged[cat]["questions"] = list(g["questions"])
                order.append(cat)
            else:
                m = merged[cat]
                m["questions"].extend(g["questions"])
                if not m.get("content") and g.get("content"):
                    m["content"] = g["content"]
        if len(order) == len(groups):
            continue
        out = []
        # 题号重新编顺（保留原题号里的 "-n" 后缀形式）
        for cat in order:
            g = merged[cat]
            g["score"] = sum(q.get("score") or 0 for q in g["questions"])
            out.append(g)
        d["groups"] = out
        d["paper"]["totalScore"] = sum(g["score"] for g in out)
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        changed += 1
    print(f"合并重复题组：{changed} 份")


if __name__ == "__main__":
    main()
