#!/usr/bin/env python3
"""物理：把「公式图片」转成 LaTeX，只保留真正的图形/示意图。

做法：对题干/选项里的每张 ![](/exam-images/...) 图片调用多模态模型判断：
- 是公式 → 输出 LaTeX（替换成 $...$）
- 是图形/示意图/表格 → 原样保留（KEEP）

用法：python3 fix_physics_formula_images.py [-T 二模] [--workers 4] [--only 关键词] [--limit N] [--dry-run]
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
MD_ROOT = PROJ.parent / "output_docx_to_md"

PROMPT = """这是一道物理试题里的一小段内容截图。请判断它是什么：
- 如果是**数学/物理公式或表达式**（如 24√2 V、0.5 A、1:4、48 W、v=at 等），把它转成 LaTeX 公式（**不要带 $ 符号**），
- 如果是**图形、示意图、坐标图、电路图、表格**，只输出 KEEP。

只输出 LaTeX 或 KEEP，不要任何解释。若公式里有中文，用 \\text{中文} 包裹。
"""

IMG_RE = re.compile(r"!\[[^\]]*\]\(((?:/exam-images/|images/)[^)\s]+)\)")


def ask(img: Path) -> str:
    b64 = base64.b64encode(img.read_bytes()).decode()
    payload = {
        "model": MODEL, "max_tokens": 300, "temperature": 0.0, "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": "你是物理/数学公式识别助手，只输出 LaTeX 或 KEEP。"},
            {"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]},
        ],
    }
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {API_KEY}"}, timeout=120)
        r.raise_for_status()
        out = (r.json()["choices"][0]["message"].get("content") or "").strip()
        out = out.strip("`$ \n")
        return out
    except Exception:
        return ""


CUR = {"stem": ""}


def local_path(url: str) -> Path:
    if url.startswith("/exam-images/"):
        return MD_ROOT / url[len("/exam-images/"):]
    # 相对路径 images/x.png → 到该卷的 MD 图片目录找
    stem = CUR["stem"]
    for d in (MD_ROOT / "二模" / "物理").glob(f"{stem}*"):
        cand = d / "images" / url.split("/")[-1]
        if cand.exists():
            return cand
    return Path("/nonexistent")


def fix_text(txt: str, cache: dict) -> tuple[str, int]:
    changed = 0
    def repl(m):
        nonlocal changed
        url = m.group(1)
        if url in cache:
            latex = cache[url]
        else:
            p = local_path(url)
            latex = ask(p) if p.exists() else ""
            cache[url] = latex
        if not latex or latex.upper() == "KEEP":
            return m.group(0)
        changed += 1
        return f"${latex}$"
    out = IMG_RE.sub(repl, txt or "")
    return out, changed


def do_paper(path: Path, dry: bool) -> tuple[str, int, int]:
    CUR["stem"] = path.stem
    d = json.loads(path.read_text(encoding="utf-8"))
    cache, total = {}, 0
    for sec in d.get("sections", []):
        for q in sec.get("questions", []):
            if q.get("content"):
                new, n = fix_text(q["content"], cache)
                if n:
                    q["content"] = new
                    total += n
            for o in q.get("options") or []:
                if o.get("label"):
                    new, n = fix_text(str(o["label"]), cache)
                    if n:
                        o["label"] = new
                        total += n
    if total and not dry:
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.stem, total, len(cache)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="物理")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted((PROJ.parent / "output_json_math" / args.type / args.subject).glob("*.json"))
    if args.only:
        files = [f for f in files if args.only in f.stem]
    if args.limit:
        files = files[: args.limit]
    print(f"共 {len(files)} 卷，worker={args.workers}", flush=True)
    tot = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(do_paper, f, args.dry_run): f for f in files}
        for fut in as_completed(futs):
            try:
                name, n, imgs = fut.result()
                tot += n
                if n:
                    print(f"  ✓ {name}：{n} 个公式转 LaTeX（看了 {imgs} 张图）", flush=True)
            except Exception as e:
                print(f"  ✗ {futs[fut].stem}: {str(e)[:100]}", flush=True)
    print(f"完成：共 {tot} 个公式图片转成 LaTeX", flush=True)


if __name__ == "__main__":
    main()
