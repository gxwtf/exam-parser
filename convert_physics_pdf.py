#!/usr/bin/env python3
"""物理试卷 PDF → JSON（只处理选择题）

- 文字/选项/答案：多模态模型逐页读 PDF（Word 里公式/选项常是图片，MD 会丢）
- 题型判定：让模型读卷首题型说明（单选/多选/不定项/混合）后逐题标注 single/multiple
- 图片：由 attach_md_images.py / attach_pdf_images.py 后处理挂载

用法：
  python3 convert_physics_pdf.py -p ../gaozhong/二模/物理/xxx.pdf
  python3 convert_physics_pdf.py --type 二模 -g 高三 -w 8
"""
import argparse, json, os, re, tempfile, time
from pathlib import Path

import convert_math_pdf as M
from paths import GAOZHONG, OUTPUT_JSON_MATH

PROJECT_ROOT = Path(__file__).parent
JSON_ROOT = OUTPUT_JSON_MATH

SYSTEM_PROMPT = (
    "你是物理试卷录入员，负责把试卷页面的图像精确转成结构化 JSON。"
    "你必须只输出 JSON，不要 markdown 代码块，不要任何解释。"
)

PAGE_PROMPT = """这是一张北京高中物理试卷（或参考答案）的页面图像。

请只处理【选择题】（含单选、多选、不定项选择），**忽略实验题和非选择题**。

先读本页（或试卷开头）的题型说明，判断本卷选择题属于哪种：
- 若写「只有一个选项符合题目要求」「选出最符合题目要求的一项」→ 各题 type = "single"
- 若写「有多项符合题目要求」「全部选对的得X分，选对但不全的得Y分」→ type = "multiple"
- 若写「有的小题只有一个选项正确，有的小题有多个选项正确」→ 逐题判断，
  只有一个正确选项用 "single"，有多个用 "multiple"

严格按下面 JSON 输出（只输出 JSON）：
{
  "choice_type": "single" | "multiple" | "mixed",
  "questions": [
    {
      "no": 13,
      "type": "single" | "multiple",
      "stem": "题干原文，公式/单位用 $...$ 包起来的 LaTeX，中文照抄；题干里不要写题号和分数",
      "options": [{"id": "a", "label": "$...$"}, {"id": "b", "label": "$...$"},
                  {"id": "c", "label": "$...$"}, {"id": "d", "label": "$...$"}],
      "hasFigure": false,
      "note": ""
    }
  ],
  "answers": [{"no": 13, "answer": "B"}],
  "figures": []
}

硬性要求：
1. 题干与选项**逐字照抄原图**，不要改写、不要翻译、不要补全图中没有的内容。
2. 选项 label 只写内容本身，**不要带 A．/（A）这类前缀**；选项内容在图中确实空缺时 label 用空字符串 ""。
3. 公式与单位用干净 LaTeX：$v=\\dfrac{x}{t}$、$10\\ \\mathrm{m/s^{2}}$、$\\mu$、$\\theta$；纯数字也包起来（如 $2$）。
4. 题干里**不要写题号**（如「13．」）和分值（如「（6分）」）。
5. **选项可能跨页**：若本页顶部出现不带题号的孤立选项行（上一页题目的选项），
   请把它们归到「本页第一个题号 - 1」那道题上，作为该题的 options 一起输出，
   并在 note 里写「选项跨页」。
6. 题干引用图形时 hasFigure 置 true；本页没有选择题就把 questions 设为 []。
6. 答案页里答案可能在解析中，形如「故选：B」「故选 B」，或答案表——都提取为 answer（多选用 "BD" 这种连写）。
"""


def build_json(pdf: Path, pages: list[Path], model: str, stem: str, args) -> tuple[dict, list[str]]:
    review: list[str] = []
    selected = None
    if getattr(args, "smart_pages", True):
        selected = M.select_pages(pdf, len(pages))
        if selected and len(selected) < len(pages):
            M.log(f"  预筛：只送 {len(selected)}/{len(pages)} 页")
    todo = [(i, p) for i, p in enumerate(pages, 1) if selected is None or i in selected]

    results: dict[int, dict] = {}
    def one(idx, page):
        t0 = time.time()
        try:
            data = M.chat_json(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": [
                     {"type": "text", "text": PAGE_PROMPT},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + __import__("base64").b64encode(page.read_bytes()).decode()}}]}],
                model, M.MAX_TOKENS)
            M.log(f"  第 {idx}/{len(pages)} 页：{len(data.get('questions') or [])} 题、{len(data.get('answers') or [])} 个答案（{time.time()-t0:.1f}s）")
            return idx, data, None
        except Exception as e:
            return idx, None, str(e)[:120]

    workers = max(1, int(getattr(args, "concurrency", 1) or 1))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(one, i, p) for i, p in todo]):
                idx, data, err = fut.result()
                if err: review.append(f"第 {idx} 页识别失败：{err}")
                else: results[idx] = data
    else:
        for i, p in todo:
            idx, data, err = one(i, p)
            if err: review.append(f"第 {idx} 页识别失败：{err}")
            else: results[idx] = data

    all_q: dict[int, dict] = {}
    answers: dict[int, str] = {}
    for idx in sorted(results):
        data = results[idx]
        for q in data.get("questions") or []:
            try:
                no = int(q.get("no"))
            except Exception:
                continue
            norm = M.normalize_question(q, "choice")
            if not norm:
                continue
            if norm["no"] in all_q:
                prev = all_q[norm["no"]]
                if len(norm["content"]) > len(prev["content"]):
                    all_q[norm["no"]] = norm
                continue
            all_q[norm["no"]] = norm
        for a in data.get("answers") or []:
            try:
                answers[int(a.get("no"))] = re.sub(r"\s+", "", str(a.get("answer") or "")).upper()
            except Exception:
                continue

    for no in sorted(all_q):
        if not all_q[no].get("answer") and answers.get(no):
            all_q[no]["answer"] = answers[no]

    qs = []
    for no in sorted(all_q):
        q = all_q[no]
        miss = [o["id"] for o in q["options"] if not o["label"]]
        if miss:
            review.append(f"第{no}题 选项缺失：{'/'.join(miss)}")
        if not q.get("answer"):
            review.append(f"第{no}题 缺答案")
        qs.append({
            "id": no,
            "questionType": q["questionType"],
            "content": q["content"],
            "options": q["options"],
            "answer": q.get("answer", ""),
            "analysis": "",
            "score": args.choice_score,
        })

    paper = {
        "title": stem, "subject": "物理", "source": stem,
        "grade": args.grade or "高三",
        "year": int(m.group(1)) if (m := re.search(r"(20\d{2})", stem)) else None,
        "totalScore": args.choice_score * len(qs) or None,
        "duration": args.duration,
        "tags": [t for t in ["物理", args.grade or "高三", "二模"] if t] + ([m.group(1)] if (m := re.search(r"(20\d{2})", stem)) else []),
    }
    sections = [{
        "type": "选择题", "questionType": "physics-choice", "title": "一、选择题",
        "category": "选择", "score": args.choice_score * len(qs), "questions": qs,
    }] if qs else []
    return {"paper": paper, "sections": sections}, review


def process(pdf: Path, out_root: Path, args):
    stem = re.sub(r"（教师版）|\(教师版\)|含答案|（反馈卷）|反馈卷", "", pdf.stem).strip()
    dst = out_root / args.type / "物理" / f"{stem}.json"
    if dst.exists() and not args.force:
        M.log(f"跳过（已存在）：{stem}"); return
    M.log(f"处理：{pdf.name}")
    with tempfile.TemporaryDirectory() as tmp:
        pages = M.render_pages(pdf, args.dpi, Path(tmp), args.pages)
        M.log(f"  共 {len(pages)} 页（{args.dpi} dpi）")
        dst.parent.mkdir(parents=True, exist_ok=True)
        data, review = build_json(pdf, pages, args.model, stem, args)
    n = sum(len(s["questions"]) for s in data["sections"])
    if n == 0:
        M.log("  ✗ 没识别到选择题"); return
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    M.log(f"  ✓ {dst.name}（{n} 题）")
    if review:
        dst.with_suffix(".review.txt").write_text("\n".join(review), encoding="utf-8")
        M.log(f"  ⚠ 复核 {len(review)} 处")


def main():
    ap = argparse.ArgumentParser(description="物理试卷 PDF → JSON（只处理选择题）")
    ap.add_argument("-p", "--path")
    ap.add_argument("--dir", default=str(GAOZHONG))
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(JSON_ROOT))
    ap.add_argument("-m", "--model", default=M.MODEL)
    ap.add_argument("--base-url", default=M.BASE_URL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("-w", "--concurrency", type=int, default=M.CONCURRENCY)
    ap.add_argument("--dpi", type=int, default=M.DPI)
    ap.add_argument("--pages", type=int, default=None)
    ap.add_argument("--choice-score", type=int, default=6)
    ap.add_argument("--duration", type=int, default=90)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-smart-pages", dest="smart_pages", action="store_false")
    args = ap.parse_args()

    M.BASE_URL = args.base_url.rstrip("/")
    if args.api_key:
        M.API_KEY = args.api_key
    out_root = Path(args.output)

    if args.path:
        process(Path(args.path), out_root, args); return
    root = Path(args.dir) / args.type / "物理"
    pdfs = sorted(root.glob("*.pdf"))
    if args.grade:
        pdfs = [p for p in pdfs if args.grade in p.name and "教师版" in p.name]
    M.log(f"共 {len(pdfs)} 份 PDF")
    for p in pdfs:
        try:
            process(p, out_root, args)
        except Exception as e:
            M.log(f"  ✗ {p.name}: {str(e)[:150]}")


if __name__ == "__main__":
    main()
