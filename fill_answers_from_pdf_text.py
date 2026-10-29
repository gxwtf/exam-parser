#!/usr/bin/env python3
"""用「教师版 PDF 的文字层 + 参考答案」补 JSON 里缺失的选择题答案（文本链路，不渲染页面）。

背景：docx 里有的小问被拆成独立选择题、或答案只写在教师版 PDF 的参考答案里，
      JSON 的 answer 会为空 → 题库里判不了分。

做法：
  1. pdftotext 取该卷教师版 PDF 全文（优先 教师版，其次原卷）；
  2. 把「全文 + 该题的题干/选项」交给模型，要求返回 answer(A/B/C/D) + 原文依据；
  3. 只接受「选项 id 里存在、confidence=high」的答案，写入 JSON（导出统一大写）。

用法：
    python3 fill_answers_from_pdf_text.py --type 二模 --subject 物理 [--dry-run] [--only 卷名]
    python3 fill_answers_from_pdf_text.py --type 二模 --subject 物理 --validate 20   # 已知答案抽检
"""
import argparse, glob, json, random, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_math_pdf as M
from paths import DATA_ROOT
from src.answer_case import normalize_choice_case_in_paper

ROOT = DATA_ROOT
JSON_ROOT = ROOT / "output_json_math"
PDF_ROOT = ROOT / "gaozhong"

ASK = """下面是一份北京高三{subject}二模试卷的教师版全文（含参考答案），以及题库里的一道题。
请判断这道题的正确答案，只输出 JSON：
{{"answer":"B","evidence":"原文依据片段（不超过 60 字）","confidence":"high|low"}}

规则：
- answer 由选项字母组成：单选题一个字母；多选题给出**全部**正确字母（如 "CD"）；
- 字母必须取自下方选项 id；若参考答案里找不到这道题、或题目有多个小问无法唯一确定，answer 留空、confidence 填 low；
- evidence 必须摘自上面的试卷全文，且尽量是「故选 B」「故答案为：AB」「参考答案 16．B」「B 正确」这类直接依据。

题目：
{stem}

选项：
{opts}
"""

ANSWER_MARKERS = ("故选", "答案为", "参考答案", "正确", "解答", "应选", "选 ")


def pdf_text(stem: str, subject: str):
    pdfs = sorted(glob.glob(str(PDF_ROOT / "二模" / subject / f"{stem}*.pdf")))
    if not pdfs:
        return None, None
    pdfs.sort(key=lambda p: ("反馈卷" in Path(p).name, "教师版" not in Path(p).name, p))  # 优先正卷教师版，避开反馈卷
    r = subprocess.run(["pdftotext", "-layout", pdfs[0], "-"], capture_output=True, text=True)
    return Path(pdfs[0]).name, r.stdout


def ask(text: str, q: dict, subject: str) -> dict:
    stem = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", q.get("content") or "")[:1500]
    opts = "\n".join(f"{o['id']}. {o['label']}" for o in (q.get("options") or []))
    msgs = [
        {"role": "system", "content": "你是物理/化学阅卷老师，只输出 JSON。"},
        {"role": "user", "content": "试卷全文：\n" + text[-20000:] + "\n\n" + ASK.format(subject=subject, stem=stem, opts=opts)},
    ]
    try:
        return M.chat_json(msgs, M.MODEL, 1200, retries=2)
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "evidence": f"调用失败：{e}", "confidence": "low"}


def pick(got: dict, ids: set[str]) -> str:
    """从模型输出里取答案字母：1~2 个字母、必须都在选项里、依据要命中答案标记、confidence=high"""
    a = re.sub(r"[^A-Z]", "", str(got.get("answer") or "").upper())
    a = "".join(sorted(dict.fromkeys(a)))
    if not a or len(a) > 2 or any(ch not in ids for ch in a):
        return ""
    if str(got.get("confidence") or "").lower() != "high":
        return ""
    ev = str(got.get("evidence") or "")
    if not any(m in ev for m in ANSWER_MARKERS):
        return ""
    return a


def ask_twice(text: str, q: dict, subject: str) -> tuple[str, dict]:
    """跑两遍，答案一致才采纳（降低单次错答风险）"""
    first = ask(text, q, subject)
    ids = {o["id"] for o in q.get("options") or []}
    a1 = pick(first, ids)
    if not a1:
        return "", first
    second = ask(text, q, subject)
    a2 = pick(second, ids)
    if a1 != a2:
        return "", {"answer": f"{a1}/{a2}", "evidence": f"两次不一致：{first.get('evidence')} ／ {second.get('evidence')}", "confidence": "low"}
    return a1, first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--subject", default="物理")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--validate", type=int, default=0, help="抽检 N 道已知答案题，报告一致率，不写盘")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    files = sorted((JSON_ROOT / args.type / args.subject).glob("*.json"))
    if args.only:
        files = [f for f in files if f.stem == args.only]
    if not files:
        raise SystemExit("没有匹配的 JSON")

    if args.validate:
        pool = []
        for f in files:
            d = json.loads(f.read_text(encoding="utf-8"))
            for sec in d["sections"]:
                for q in sec["questions"]:
                    if str(q.get("answer") or "").strip():
                        pool.append((f.stem, q))
        random.seed(args.seed)
        ok = bad = skip = 0
        for stem, q in random.sample(pool, min(args.validate, len(pool))):
            _, text = pdf_text(stem, args.subject)
            if not text:
                skip += 1
                continue
            ids = {o["id"] for o in q.get("options") or []}
            a, _got = ask_twice(text, q, args.subject)
            mark = "✓" if a == q["answer"] else "✗"
            ok += a == q["answer"]
            bad += a != q["answer"]
            print(f"{mark} {stem} q{q['id']}：期望 {q['answer']} / AI {a or '空'}")
        print(f"抽检一致 {ok}，不一致 {bad}，跳过 {skip}")
        return

    filled = failed = 0
    report = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        empties = [q for sec in data["sections"] for q in sec["questions"] if not str(q.get("answer") or "").strip()]
        if not empties:
            continue
        pdf, text = pdf_text(f.stem, args.subject)
        if not text:
            print(f"⚠ {f.stem}：缺 {len(empties)} 题答案，但找不到 PDF")
            continue
        print(f"→ {f.stem}（{pdf}）：待补 {[q['id'] for q in empties]}")
        changed = False
        for q in empties:
            ids = {o["id"] for o in q.get("options") or []}
            a, got = ask_twice(text, q, args.subject)
            ev = str(got.get("evidence") or "")[:70]
            if a:
                q["answer"] = a
                filled += 1
                changed = True
                report.append((f.stem, q["id"], a, ev))
                print(f"   q{q['id']} → {a}｜{ev}")
            else:
                failed += 1
                report.append((f.stem, q["id"], "", f"[未采纳] {str(got.get('answer'))}/{got.get('confidence')}｜{ev}"))
                print(f"   q{q['id']} → 未采纳（AI {got.get('answer')!r} / {got.get('confidence')}）：{ev}")
        if changed and not args.dry_run:
            normalize_choice_case_in_paper(data)
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'[dry-run] ' if args.dry_run else ''}补答案 {filled} 题，未采纳 {failed} 题")
    for r in report:
        if not r[2]:
            print("  待人工：", r[0], "q"+str(r[1]), r[3])


if __name__ == "__main__":
    main()
