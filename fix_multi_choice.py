#!/usr/bin/env python3
"""把「五选二」题型（正确的/不正确的**两项**是）单独处理成多选。

背景：老新卷里都有「下列对文章的理解，不正确的两项是（4 分）」这类题——卷面上是 A–E 五个选项，
要选两个。旧链路一律按单选 4 选项处理，导致：选项少一个、答案只有一个字母、前端渲染成单选框。

处理：定位题号页 → 150dpi 渲染 → 视觉模型输出 {stem, options[5], answer(两个字母)} →
回填 JSON：`questionType="multiple"`、5 个选项、`answer` 用逗号连接（"A,C"，与前端 Checkbox 一致）。

用法：
    python3 fix_multi_choice.py --scan
    python3 fix_multi_choice.py --apply "二模/2014北京海淀高三二模语文.json" --only 19
    python3 fix_multi_choice.py --apply-all          # 处理扫描到的全部
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fix_pairs_choice as base  # 复用渲染/定位/调用视觉的底座

STEM_RE = re.compile(r"(两|二)项(是|）|\)|。|$)")
PROMPT = """图片是一整页语文试卷。请找到这道题：{stem}

这道题是「五选二」：卷面上有 **A、B、C、D、E 五个选项**，要选出**两个**。

只输出 JSON，不要解释：
{{"found": true, "stem": "题干原文（不含选项）", "options": [
  {{"id": "A", "label": "…"}}, {{"id": "B", "label": "…"}}, {{"id": "C", "label": "…"}},
  {{"id": "D", "label": "…"}}, {{"id": "E", "label": "…"}}], "answer": "两个字母，如 AC"}}

要求：选项文字按卷面原样（保留标点、括号）；找不到这道题就输出 {{"found": false}}。
"""


def needs_fix(q: dict) -> bool:
    stem = q.get("content") or ""
    if not STEM_RE.search(stem):
        return False
    opts = q.get("options") or []
    if q.get("questionType") == "multiple" and len(opts) == 5:
        ans = str(q.get("answer") or "")
        letters = re.sub(r"[^A-E]", "", ans.upper())
        if len(letters) == 2:
            return False
    return len(opts) < 5 or q.get("questionType") != "multiple"


def normalize_answer(raw: str) -> str:
    letters = sorted(set(re.sub(r"[^A-E]", "", (raw or "").upper())))
    return ",".join(letters)


def pdf_text(pdf: Path) -> str:
    import subprocess
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout


def parse_five_options(text: str, stem: str) -> tuple[list[dict], str]:
    """从 PDF 文本层解析出 A–E 五个选项（跨页/换行都会拼接）。"""
    flat = re.sub(r"[\s\u3000]+", "", stem)
    key = flat[:12]
    idx = text.find(key)
    if idx < 0:
        frag = re.findall(r"[\u4e00-\u9fff]{6,}", flat)
        for f in sorted(frag, key=len, reverse=True)[:3]:
            idx = text.find(f)
            if idx >= 0:
                break
    if idx < 0:
        return [], ""
    tail = text[idx:]
    # 截到下一道题（行首的 数字 + ．/.）
    stop = re.search(r"\n\s*\d{1,2}\s*[．.]\s*\S", tail[len(key):])
    block = tail[: len(key) + (stop.start() if stop else 3000)]
    block = re.sub(r"\n\s*\d+\s*/\s*\d+\s*\n", "\n", block)  # 去页码
    marks = list(re.finditer(r"(?m)(?:^|\n)\s*([A-E])\s*[．.、]\s*", block))
    if len(marks) < 5:
        return [], ""
    opts = []
    for i, m in enumerate(marks[:5]):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        seg = block[m.end():end]
        seg = re.sub(r"\s*\n\s*", "", seg).strip()
        opts.append({"id": m.group(1), "label": seg})
    return opts, block


def parse_answer(text: str, qid: str) -> str:
    """答案区里找双字母答案，兼容「19.（4 分）B E」「19．BC」「19.B、E」等写法。"""
    num = re.sub(r"\D", "", str(qid)) or ""
    if not num:
        return ""
    # 直接全文找：模式要求「题号 + （可选分值括号）+ 两个字母」，题目选项区不会出现这种行
    area = text
    pat = (rf"(?m)^\s*{num}\s*[．.、]?\s*"
           r"(?:（[^）]{0,14}分[^）]{0,6}）|\([^)]{0,14}分[^)]{0,6}\))?\s*"
           r"([A-E])\s*[、,，\s]?\s*([A-E])(?![A-E])")
    for m in re.finditer(pat, area):
        letters = sorted({m.group(1), m.group(2)})
        if len(letters) == 2:
            return ",".join(letters)
    return ""


def fix_paper(json_path: Path, only: set[str] | None, apply: bool) -> list[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    pdf = base.locate_pdf(json_path)
    log: list[str] = []
    if not pdf:
        return [f"{json_path.stem}：找不到 PDF，跳过"]
    for sec in data.get("sections") or []:
        for q in sec.get("questions") or []:
            if only and str(q.get("id")) not in only:
                continue
            if not needs_fix(q):
                continue
            stem = q.get("content") or ""
            text = pdf_text(pdf)
            opts, _block = parse_five_options(text, stem)
            ans = parse_answer(text, str(q.get("id")))
            if len(opts) != 5 or len(ans.split(",")) != 2:
                # 文本层失败 → 用视觉兜底（整页 + 严格提示词）
                page = base.find_page(pdf, stem)
                if page:
                    with tempfile.TemporaryDirectory() as td:
                        img = base.render_page(pdf, page, Path(td))
                        if img:
                            try:
                                res = base.ask_vision(img, stem, prompt=PROMPT)
                                vopts = res.get("options") or []
                                vans = normalize_answer(res.get("answer") or "")
                                if len(vopts) == 5 and len(vans.split(",")) == 2:
                                    opts, ans = vopts, vans
                            except Exception as e:
                                log.append(f"{json_path.stem}｜{q.get('id')}：视觉兜底失败 {str(e)[:50]}")
            if len(opts) != 5 or len(ans.split(",")) != 2:
                log.append(f"{json_path.stem}｜{sec.get('category')} {q.get('id')}："
                           f"未还原（opts={len(opts)}, answer={ans!r}）")
                continue
            log.append(f"{json_path.stem}｜{sec.get('category')} {q.get('id')}：五选二 → "
                       + " ".join(o["id"] for o in opts) + f" 答案 {ans}")
            if apply:
                q["questionType"] = "multiple"
                q["options"] = [{"id": o["id"], "label": o["label"]} for o in opts]
                q["answer"] = ans
                if not q.get("score"):
                    q["score"] = 4
    if apply:
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return log


def scan() -> list[tuple[Path, list[str]]]:
    out = []
    for f in sorted(base.JSON_ROOT.glob("*/*.json")):
        if ".old-groups" in f.name:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        hit = []
        for sec in data.get("sections") or []:
            for q in sec.get("questions") or []:
                if needs_fix(q):
                    hit.append(f"{sec.get('category')} {q.get('id')}")
        if hit:
            out.append((f, hit))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", nargs="+", default=[])
    ap.add_argument("--only", nargs="+", default=None)
    ap.add_argument("--apply-all", action="store_true")
    args = ap.parse_args()

    if args.scan:
        rows = scan()
        print(f"待处理「五选二」{len(rows)} 卷：")
        for f, hit in rows:
            print(f"  · {f.parent.name}/{f.name}：{', '.join(hit)}")
        return 0

    targets = [f for f, _ in scan()] if args.apply_all else [
        (Path(a) if Path(a).is_absolute() else base.JSON_ROOT / a) for a in args.apply
    ]
    only = set(args.only) if args.only else None
    for t in targets:
        for line in fix_paper(t, only, apply=True):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
