#!/usr/bin/env python3
"""把语文 JSON 的分值规整到北京卷标准总分（150），避免个别卷因解析不齐出现 8 分/329 分。

- 题组总分按比例缩放到 150（保留 1 位）
- 组内按题数平均分配；默写（每空 1 分）保持
用法：python3 normalize_chinese_scores.py [--dir ...] [--target 150]
"""
import argparse, json
from pathlib import Path
from paths import DATA_ROOT

DEFAULT = {"多文本": 18, "文言文": 18, "古诗": 12, "默写": 8, "名著": 10,
           "文学类": 18, "语用": 6, "微写作": 10, "作文": 50}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DATA_ROOT / "output_json_chinese" / "二模"))
    ap.add_argument("--target", type=int, default=150)
    args = ap.parse_args()
    changed = 0
    for f in sorted(Path(args.dir).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        groups = d.get("groups") or []
        if not groups:
            continue
        total = sum(g.get("score") or 0 for g in groups)
        if total == args.target:
            continue
        # 1) 先决定每个题组的目标分
        if total <= 0:
            targets = [DEFAULT.get(g["category"], 0) for g in groups]
            s = sum(targets)
            if s == 0:
                continue
            targets = [round(t * args.target / s) for t in targets]
        else:
            targets = [round((g.get("score") or 0) * args.target / total) for g in groups]
        # 2) 误差补到最后一个非零题组
        diff = args.target - sum(targets)
        if diff and targets:
            targets[-1] += diff
        # 3) 组内分配
        for g, t in zip(groups, targets):
            qs = g["questions"]
            if not qs:
                g["score"] = 0
                continue
            if g["category"] == "默写":
                per = 1
                for q in qs:
                    q["score"] = per
                g["score"] = sum(q["score"] for q in qs)
                continue
            per = max(1, round(t / len(qs)))
            for q in qs:
                q["score"] = per
            g["score"] = sum(q["score"] for q in qs)
            # 组内误差补到最后一题，保证题组总分与目标一致
            diff = t - g["score"]
            if diff and qs[-1]["score"] + diff > 0:
                qs[-1]["score"] += diff
                g["score"] = sum(q["score"] for q in qs)
        d["paper"]["totalScore"] = sum(g["score"] for g in groups)
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        changed += 1
    print(f"规整分值：{changed} 份")


if __name__ == "__main__":
    main()
