#!/usr/bin/env python3
"""把化学题库 JSON 里的文本做 MDX 安全化处理（不调用 AI，纯确定性）。

题库前端用 next-mdx-remote + remark-gfm + remark-math + rehype-katex 渲染：
  · 裸 `<`（如「pH <7」「a<b」）会被当成 JSX 标签起始，导致整题渲染报错 → 换成 `$<$`
  · Word 转出的 <sub>/<sup> 标签统一转成 Unicode 上下标（KaTeX 之外也能正常显示）
  · 其余 HTML 标签（<span>、<p> …）直接去掉，保留其中文字
用法：python3 sanitize_mdx.py [--dir ../output_json_math/二模/化学] [--dry-run]
"""
import argparse, json, re
from pathlib import Path

MATH = re.compile(r"(\$\$.*?\$\$|\$[^$]*\$)", re.S)
ALLOWED = re.compile(r"</?br\s*/?>", re.I)

SUB = str.maketrans("0123456789+-=()nmxaehijkloprstuv", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙₘₓₐₑₕᵢⱼₖₗₒₚᵣₛₜᵤᵥ")
SUP = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")


def _sub(m):
    inner = m.group(1)
    conv = inner.translate(SUB)
    return conv


def _sup(m):
    inner = m.group(1)
    return inner.translate(SUP)


KEEP_TAGS = ("Dot", "U", "Center", "RightAlign", "Input2", "Input", "Superscript", "Blank", "ClozeBlank")


def protect_tags(seg: str) -> tuple[str, dict]:
    import uuid
    store = {}

    def repl(m):
        key = f"@@TAG{len(store)}@@"
        store[key] = m.group(0)
        return key

    name_re = "|".join(KEEP_TAGS)
    seg = re.sub(rf"</?({name_re})\s*/?>", repl, seg)
    return seg, store


def restore_tags(seg: str, store: dict) -> str:
    for k, v in store.items():
        seg = seg.replace(k, v)
    return seg


def fix_plain(seg: str) -> str:
    seg = re.sub(r"<sub>(.*?)</sub>", _sub, seg, flags=re.S)
    seg = re.sub(r"<sup>(.*?)</sup>", _sup, seg, flags=re.S)

    # 表格行里的 <br> 要保留（换行会把整张表拆坏）；先占位，最后再还原成 <br />
    out_lines = []
    for ln in seg.split("\n"):
        if ln.lstrip().startswith("|"):
            ln = re.sub(r"<br\s*/?>", "@@BR@@", ln)
        out_lines.append(ln)
    seg = "\n".join(out_lines)

    seg = ALLOWED.sub("\n", seg)                    # 其余 <br> 当换行
    seg = re.sub(r"</?[a-zA-Z][^>]*>", "", seg)      # 其它标签去掉，文字保留
    seg = seg.replace("<", "$<$").replace(">", "$>$")
    seg = re.sub(r"\\+(?=[{}])", "", seg)              # 先抹掉旧转义（含重复加的斜杠）
    seg = seg.replace("{", "\\{").replace("}", "\\}")  # MDX 里 {…} 是表达式，必须转义
    seg = seg.replace("@@BR@@", "<br />")
    return seg


def fix(text: str) -> str:
    parts = MATH.split(text or "")
    for i in range(0, len(parts), 2):               # 偶数位是数学公式之外
        seg, store = protect_tags(parts[i])
        seg = fix_plain(seg)
        parts[i] = restore_tags(seg, store)
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent.parent / "output_json_math" / "二模" / "化学"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    n_changed = n_field = 0
    for f in sorted(Path(args.dir).glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        changed = False
        for sec in data.get("sections", []):
            for q in sec.get("questions", []):
                new = fix(q.get("content", ""))
                if new != q.get("content"):
                    q["content"] = new; changed = True; n_field += 1
                for o in q.get("options", []):
                    nl = fix(str(o.get("label", "")))
                    if nl != o.get("label"):
                        o["label"] = nl; changed = True; n_field += 1
        if changed:
            n_changed += 1
            if not args.dry_run:
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'[dry-run] ' if args.dry_run else ''}{n_changed} 份试卷 / {n_field} 个字段已做 MDX 安全化")


if __name__ == "__main__":
    main()
