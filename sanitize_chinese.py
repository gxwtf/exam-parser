#!/usr/bin/env python3
"""语文 JSON（groups 结构）的 MDX 安全化：裸 < > 转 $<$，sub/sup 转 Unicode，
保留 <Dot>/<U>/<Center>/<RightAlign>/<Input2> 等自定义组件标签。
用法：python3 sanitize_chinese.py [--dir ...]
"""
import argparse, json
from pathlib import Path
import sanitize_mdx as S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent.parent / "output_json_chinese" / "二模"))
    args = ap.parse_args()
    nf = nfields = 0
    for f in sorted(Path(args.dir).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        changed = False
        for g in d.get("groups", []):
            new = S.fix(g.get("content") or "")
            if new != (g.get("content") or ""):
                g["content"] = new; changed = True; nfields += 1
            for q in g.get("questions", []):
                for key in ("content", "analysis"):
                    new = S.fix(q.get(key) or "")
                    if new != (q.get(key) or ""):
                        q[key] = new; changed = True; nfields += 1
                for o in q.get("options") or []:
                    new = S.fix(str(o.get("label") or ""))
                    if new != str(o.get("label") or ""):
                        o["label"] = new; changed = True; nfields += 1
        if changed:
            nf += 1
            f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"语文 MDX 安全化：{nf} 份 / {nfields} 字段")


if __name__ == "__main__":
    main()
