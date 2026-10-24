#!/usr/bin/env python3
"""给已解析的化学选择题补「解析」：按卷批量调用 AI，写回 output_json_math/<类型>/化学/*.json。

用法：python3 add_chem_choice_analysis.py [-T 二模] [--limit N] [--only 关键词] [--workers 3]
"""
import argparse, json, re, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from convert_chinese_md import chat, log
from paths import DATA_ROOT

PROJ = Path(__file__).parent
SYSTEM = "你是高中化学老师，只输出 JSON。"

PROMPT = r"""下面是若干道北京高考化学选择题（题干、选项、正确答案）。请为每题写一段解析（90-160字，中文，简洁专业）：
1. 先点明正确答案的依据（涉及的化学原理/方程式/计算/实验现象）；
2. 再简要说明其它选项为什么错（能一句话点出即可）；
3. 不要复述题干，不要分点编号，直接写一段话；
4. 涉及化学式请用普通文本（如 H2SO4、Cu2+），不要用 LaTeX。

只输出 JSON：{{"题号":"解析"}}。

题目：
ITEM_PLACEHOLDER
"""


def ai_json(prompt, max_tokens=6000):
    try:
        got = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], max_tokens)
    except Exception as e:
        log(f"    AI 失败：{str(e)[:80]}")
        return {}
    return got if isinstance(got, dict) else {}


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def do_paper(path: Path, force=False) -> tuple[str, int]:
    d = json.loads(path.read_text(encoding="utf-8"))
    filled = 0
    for sec in (d.get("sections") or []):
        qs = [q for q in sec.get("questions", []) if q.get("options") and clean(q.get("answer"))]
        need = [q for q in qs if force or not clean(q.get("analysis"))]
        if not need:
            continue
        items = []
        for idx, q in enumerate(need, start=1):
            opts = "\n".join(f"{o['id'].upper()}．{o.get('label','')}" for o in q.get("options", []))
            items.append(f"【{idx}】{clean(q.get('content'))}\n{opts}\n正确答案：{clean(q.get('answer'))}")
        got = ai_json(PROMPT.replace("ITEM_PLACEHOLDER", "\n\n".join(items)[:14000]))
        for idx, q in enumerate(need, start=1):
            v = clean(got.get(str(idx)) or "")
            if v:
                q["analysis"] = v
                filled += 1
    if filled:
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.stem, filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    files = sorted((DATA_ROOT / "output_json_math" / args.type / args.subject).glob("*.json"))
    if args.only:
        files = [f for f in files if args.only in f.stem]
    if args.limit:
        files = files[: args.limit]
    log(f"共 {len(files)} 份卷，worker={args.workers}")

    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(do_paper, f, args.force): f for f in files}
        for fut in as_completed(futs):
            try:
                name, n = fut.result()
                total += n
                if n:
                    log(f"  ✓ {name}：补 {n} 道解析")
            except Exception as e:
                log(f"  ✗ {futs[fut].stem}: {str(e)[:120]}")
    log(f"完成：共补 {total} 道解析")


if __name__ == "__main__":
    main()
