#!/usr/bin/env python3
"""给「图片版」化学选择题补解析：把每题的截图交给多模态模型，生成 100-160 字解析。

写回 <卷>/paper.json（并同步 qN.json 的 analysis 字段）。
用法：python3 add_image_choice_analysis.py [-T 二模] [--workers 4] [--only 关键词] [--limit N]
"""
import argparse, base64, json, os, re, sys
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

PROMPT = """这是北京高考化学二模的一道选择题截图。正确答案是 {ans}。
请写一段 100-160 字的中文解析：
1. 说明正确答案为什么对（涉及的反应/原理/计算/实验现象说清楚）；
2. 简要说明其它选项错在哪（一句话即可）；
3. 不要重复题干原文，不要分点编号，直接一段话；
4. 化学式用普通文本（H2SO4、Cu2+ 这类），不要用 LaTeX。
只输出解析正文，不要任何前缀或解释。
"""


def ask(img: Path, ans: str) -> str:
    b64 = base64.b64encode(img.read_bytes()).decode()
    payload = {
        "model": MODEL, "max_tokens": 800, "temperature": 0.2, "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": "你是高中化学老师，只输出解析正文。"},
            {"role": "user", "content": [
                {"type": "text", "text": PROMPT.format(ans=ans)},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]},
        ],
    }
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {API_KEY}"}, timeout=180)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        print(f"    AI 失败：{str(e)[:80]}", flush=True)
        return ""


def do_paper(d: Path, force=False) -> tuple[str, int]:
    pj = d / "paper.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    filled = 0
    for q in data["questions"]:
        if q.get("type") != "single":
            continue
        if not str(q.get("answer") or "").strip():
            continue
        if q.get("analysis") and not force:
            continue
        img = d / f"q{q['no']}.png"
        if not img.exists():
            continue
        txt = ask(img, str(q["answer"]).strip())
        if txt:
            q["analysis"] = re.sub(r"\s+", " ", txt)
            filled += 1
            qj = d / f"q{q['no']}.json"
            if qj.exists():
                try:
                    qd = json.loads(qj.read_text(encoding="utf-8"))
                    qd["analysis"] = q["analysis"]
                    qj.write_text(json.dumps(qd, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
    if filled:
        pj.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return d.name, filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dirs = [d for d in sorted((ROOT / args.type / args.subject).iterdir()) if (d / "paper.json").exists()]
    if args.only:
        dirs = [d for d in dirs if args.only in d.name]
    if args.limit:
        dirs = dirs[: args.limit]
    print(f"共 {len(dirs)} 卷，worker={args.workers}", flush=True)
    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(do_paper, d, args.force): d for d in dirs}
        for fut in as_completed(futs):
            try:
                name, n = fut.result()
                total += n
                print(f"  ✓ {name}：{n} 道解析", flush=True)
            except Exception as e:
                print(f"  ✗ {futs[fut].name}: {str(e)[:120]}", flush=True)
    print(f"完成：共生成 {total} 道解析", flush=True)


if __name__ == "__main__":
    main()
