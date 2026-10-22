#!/usr/bin/env python3
"""检查化学题库 JSON 里所有 $...$ / $$...$$ 公式能否被 KaTeX 解析（前端渲染的同一套 KaTeX）。

用法：python3 check_katex.py [--dir ...] [--limit 0]
输出：前若干条失败明细 + 按文件汇总的失败数。
"""
import argparse, json, os, re, subprocess, tempfile
from pathlib import Path

MATH = re.compile(r"(\$\$.*?\$\$|\$[^$]+\$)", re.S)

NODE = r"""
const katex = require(process.env.KATEX_DIR ? process.env.KATEX_DIR : 'katex');
const chunks = [];
process.stdin.on('data', d => chunks.push(d));
process.stdin.on('end', () => {
  const items = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const bad = [];
  for (const it of items) {
    try { katex.renderToString(it.tex, { throwOnError: true, displayMode: it.display }); }
    catch (e) { bad.push({ id: it.id, tex: it.tex, err: String(e.message).slice(0, 160) }); }
  }
  process.stdout.write(JSON.stringify(bad));
});
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent.parent / "output_json_math" / "二模" / "化学"))
    ap.add_argument("--node-modules", default="/home/gxwtf/gxexercise")
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    items, owner = [], {}
    for f in sorted(Path(args.dir).glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for sec in data.get("sections", []):
            for q in sec.get("questions", []):
                texts = [("stem", q.get("content", ""))] + [(f"opt{o['id']}", str(o.get("label", ""))) for o in q.get("options", [])]
                for tag, t in texts:
                    for m in MATH.finditer(t or ""):
                        tex = m.group(1)
                        display = tex.startswith("$$")
                        tex = tex[2:-2] if display else tex[1:-1]
                        if not tex.strip():
                            continue
                        key = f"{f.name}::{q.get('id')}::{tag}"
                        owner[len(items)] = key
                        items.append({"id": len(items), "tex": tex, "display": display})

    print(f"共 {len(items)} 段公式待检查")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(NODE)
        script = fh.name
    env = dict(os.environ, NODE_PATH=str(Path(args.node_modules) / "node_modules"))
    p = subprocess.run(["node", script], input=json.dumps(items), capture_output=True, text=True,
                       cwd=args.node_modules, timeout=600, env=env)
    if p.returncode != 0:
        print("node 运行失败：", p.stderr[:500])
        return
    bad = json.loads(p.stdout or "[]")
    print(f"KaTeX 解析失败：{len(bad)} 段")
    per_file = {}
    for b in bad:
        key = owner.get(b["id"], "?")
        per_file[key.split("::")[0]] = per_file.get(key.split("::")[0], 0) + 1
        if b["id"] < args.show * 100:
            pass
    for b in bad[: args.show]:
        print(f"  ✗ {owner.get(b['id'])}: {b['err']}  <- {b['tex'][:100]}")
    print("按卷汇总：")
    for k, v in sorted(per_file.items(), key=lambda x: -x[1])[:20]:
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
