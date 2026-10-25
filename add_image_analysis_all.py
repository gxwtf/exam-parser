#!/usr/bin/env python3
"""给化学「图片版」所有题目补解析：选择题（含原本没答案的）+ 非选择题。

- 选择题有答案 → 只写解析
- 选择题无答案 → 让模型先定答案（单个字母）再写解析
- 非选择题 → 写解题思路 + 参考答案要点（作为解析）
结果写回 paper.json / qN.json。
用法：python3 add_image_analysis_all.py [--workers 6] [--only 关键词] [--limit N]
"""
import argparse, base64, json, os, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJ = Path(__file__).parent
load_dotenv(PROJ / ".env")
BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1").rstrip("/")
API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")
ROOT = PROJ.parent / "output_image_questions"

SYS = "你是高中化学老师，只输出要求的 JSON 或文本，不要解释。"

P_CHOICE = """这是北京高考化学二模的一道选择题截图，正确答案是 {ans}。请写 100-160 字中文解析：先说明正确答案为什么对，再简要说明其它选项错在哪。化学式用普通文本（H2SO4、Cu2+），不要 LaTeX，不要分点编号。只输出解析正文。"""

P_CHOICE_NOANS = """这是北京高考化学二模的一道选择题截图。请先判断正确答案，再写解析。
只输出 JSON：{{"answer":"A","analysis":"100-160字解析（正确项为什么对、其它选项错在哪，化学式用普通文本，不要 LaTeX）"}}"""

P_TEXT = """这是北京高考化学二模的一道非选择题截图。请写 120-200 字解析：说明解题思路与关键步骤，并给出参考答案要点（化学式用普通文本，如 H2SO4、Cu2+）。不要 LaTeX，不要分点编号。只输出解析正文。"""


def ask(img: Path, prompt: str, max_tokens=900) -> str:
    b64 = base64.b64encode(img.read_bytes()).decode()
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.2, "reasoning_effort": "none",
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": [{"type": "text", "text": prompt},
                                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {API_KEY}"}, timeout=180)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        print(f"    AI 失败：{str(e)[:60]}", flush=True)
        return ""


def do_paper(d: Path, force=False) -> tuple[str, int, int]:
    pj = d / "paper.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    filled = 0
    answers = 0
    for q in data["questions"]:
        no = q["no"]
        img = d / f"q{no}.png"
        if not img.exists():
            continue
        is_choice = q.get("type") == "single"
        ans = str(q.get("answer") or "").strip()
        if is_choice and ans:
            if q.get("analysis") and not force:
                continue
            txt = ask(img, P_CHOICE.format(ans=ans))
            if txt:
                q["analysis"] = re.sub(r"\s+", " ", txt)
                filled += 1
        elif is_choice and not ans:
            out = ask(img, P_CHOICE_NOANS)
            m = re.search(r"\{[\s\S]*\}", out)
            if not m:
                continue
            try:
                got = json.loads(m.group(0))
            except Exception:
                continue
            a = str(got.get("answer") or "").strip().upper()[:4]
            an = re.sub(r"\s+", " ", str(got.get("analysis") or "")).strip()
            if a:
                q["answer"] = a
                answers += 1
            if an:
                q["analysis"] = an
                filled += 1
        else:
            if q.get("analysis") and not force:
                continue
            txt = ask(img, P_TEXT)
            if txt:
                q["analysis"] = re.sub(r"\s+", " ", txt)
                filled += 1
        if q.get("analysis"):
            qj = d / f"q{no}.json"
            if qj.exists():
                try:
                    qd = json.loads(qj.read_text(encoding="utf-8"))
                    qd["analysis"] = q["analysis"]
                    if q.get("answer"):
                        qd["answer"] = q["answer"]
                    qj.write_text(json.dumps(qd, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
    if filled or answers:
        pj.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return d.name, filled, answers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    dirs = [d for d in sorted((ROOT / "二模" / "化学").iterdir()) if (d / "paper.json").exists()]
    if args.only:
        dirs = [d for d in dirs if args.only in d.name]
    if args.limit:
        dirs = dirs[: args.limit]
    print(f"共 {len(dirs)} 卷，worker={args.workers}", flush=True)
    tot = tot_a = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(do_paper, d, args.force): d for d in dirs}
        for fut in as_completed(futs):
            try:
                name, n, a = fut.result()
                tot += n; tot_a += a
                if n or a:
                    print(f"  ✓ {name}：解析 +{n}，补答案 +{a}", flush=True)
            except Exception as e:
                print(f"  ✗ {futs[fut].name}: {str(e)[:100]}", flush=True)
    print(f"完成：解析 +{tot}，补答案 +{tot_a}", flush=True)


if __name__ == "__main__":
    main()
