#!/usr/bin/env python3
"""把「选项排成两栏/表格」的文言文选择题，用 AI 读 PDF 原页重新格式化。

背景：语文老卷里「下列各组语句中，加点词的意义和用法相同的一组是」「下列对句中加点词语的解释，
不正确的一项是」这类题，A–D 选项在卷面上排成两栏（或表格），解析时切不出来，被当成主观题渲染成
带答题框的大题。这里按产品口径**统一成一种格式**：

    「意义和用法相同/不同的一组是」类：A. 句一 / 句二   （两句都保留加点标记）
    「加点词(语)解释」类：           A. 原句 词：释义

做法：pdftotext -layout 定位题号所在页 → 150dpi 渲染该页 → 视觉模型只输出 JSON →
回填 JSON 文件（questionType=single、options、answer），并写 review 留痕。

用法：
    python3 fix_pairs_choice.py --scan                     # 列出所有待修题（JSON 目录）
    python3 fix_pairs_choice.py --apply <类型>/<卷>.json [--only 7]   # 修单卷（可限题号）
    python3 fix_pairs_choice.py --apply-all                # 修扫描到的全部
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

DATA_ROOT = Path(os.getenv("EXAM_DATA_ROOT", Path.home() / "jingshibang"))
JSON_ROOT = DATA_ROOT / "output_json_chinese"
MD_ROOT = DATA_ROOT / "output_docx_to_md"
PDF_ROOT = DATA_ROOT / "gaozhong"

def _load_env() -> None:
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
BASE_URL = (os.getenv("MATH_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = os.getenv("MATH_VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")

PAIR_STEM = re.compile(r"(意义和用法(都)?(相同|不同)的一组是|意义和用法(都)?相同的(一项|一组)是|用法(都)?相同的一组|意义和用法(都)?相同的一项是)")
EXPLAIN_STEM = re.compile(r"加点词(语)?的解释|加点词语的解说|加点词的解释")

PROMPT_STRICT = """再核对一遍：这道题有 4 个选项，**每个选项必须包含两句**（中间用「 / 」分隔），缺一句就是错的。
请回到图片逐个选项核对左右两栏，把缺失的那一句补上。
题目：{stem}

只输出 JSON：
{{"found": true, "stem": "…", "options": [{{"id": "A", "label": "句1 / 句2"}}, {{"id": "B", "label": "句1 / 句2"}}, {{"id": "C", "label": "句1 / 句2"}}, {{"id": "D", "label": "句1 / 句2"}}]}}
加点字用 <Dot>字</Dot> 包裹。
"""

PROMPT = """你在帮我把语文试卷里一道「两栏排版」的选择题整理成结构化数据。
图片是一整页试卷，请找到这道题：{stem}

【这道题的版式说明（很重要）】
- 选项分成左右两栏；**每个选项由两句话组成**（左栏两句 = A 的两句，右栏两句 = B 的两句，下一组是 C）。
- 卷面上 A．/B．/C．/D． 的标记可能单独占一行，夹在句子中间；不要把标记当句子。
- 原句里的「加点字」在 PDF 里常表现为字符下面一个点（抽取时会变成单独一行「．某」）。请把加点字用 <Dot>字</Dot> 包裹。

【输出格式】只输出 JSON，不要任何解释：
{{"found": true, "stem": "题干原文（不含选项）", "options": [
  {{"id": "A", "label": "第一句 / 第二句"}},
  {{"id": "B", "label": "第一句 / 第二句"}},
  {{"id": "C", "label": "第一句 / 第二句"}},
  {{"id": "D", "label": "第一句 / 第二句"}}]}}

【两种题型的写法】
- 题干是「加点词的意义和用法相同/不同的一组是」→ 选项 = 「第一句 / 第二句」，两句之间用「 / 」。
- 题干是「加点词(语)的解释，不正确的一项是」→ 选项 = 「原句 词：释义」。
句子里的标点、括号、通假字保持原样；两句顺序按卷面从左到右、从上到下。
如果页面上找不到这道题，输出 {{"found": false}}。
"""


def render_page(pdf: Path, page_no: int, out_dir: Path, dpi: int = 150) -> Path | None:
    prefix = out_dir / "page"
    subprocess.run(["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-r", str(dpi), "-png",
                    str(pdf), str(prefix)], check=False, capture_output=True)
    hits = sorted(out_dir.glob("page*.png"))
    return hits[0] if hits else None


def page_texts(pdf: Path) -> list[str]:
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True)
    return out.stdout.split("\f")


def find_page(pdf: Path, stem: str) -> int | None:
    flat = re.sub(r"[\s\u3000]+", "", stem)
    keys = [flat[:14], flat[:10], flat[:8]]
    pages = page_texts(pdf)
    for key in keys:
        if not key:
            continue
        for i, page in enumerate(pages, start=1):
            if key in re.sub(r"[\s\u3000]+", "", page):
                return i
    # 再退一步：取题干里最长的连续中文串去搜
    m = re.findall(r"[\u4e00-\u9fff]{6,}", flat)
    for frag in sorted(m, key=len, reverse=True)[:3]:
        for i, page in enumerate(pages, start=1):
            if frag in re.sub(r"[\s\u3000]+", "", page):
                return i
    return None


def ask_vision(img: Path, stem: str, strict: bool = False) -> dict:
    b64 = base64.b64encode(img.read_bytes()).decode()
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        json={
            "model": MODEL, "max_tokens": 1600, "temperature": 0.0, "reasoning_effort": "none",
            "messages": [
                {"role": "system", "content": "你是语文试卷录入员，只输出 JSON。"},
                {"role": "user", "content": [
                    {"type": "text", "text": (PROMPT_STRICT if strict else PROMPT).format(stem=stem)},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                ]},
            ],
        },
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=300,
    )
    resp.raise_for_status()
    txt = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {"found": False}


def needs_fix(q: dict) -> bool:
    stem = (q.get("content") or "")
    if not (PAIR_STEM.search(stem) or EXPLAIN_STEM.search(stem)):
        return False
    opts = q.get("options") or []
    if q.get("questionType") == "single" and len(opts) == 4:
        # 「解释」类：选项里既有原句又有释义才算合格
        if EXPLAIN_STEM.search(stem):
            ok = all(("：" in o.get("label", "")) or ("  " in o.get("label", "")) for o in opts)
            return not ok
        # 「相同的一组」类：每个选项必须是「第一句 / 第二句」
        ok = all("/" in o.get("label", "") for o in opts)
        return not ok
    return True


def locate_pdf(json_path: Path) -> Path | None:
    type_name = json_path.parent.name
    title = json_path.stem
    for cand in sorted((PDF_ROOT / type_name / "语文").glob(f"{title}*.pdf")):
        return cand
    return None


def fix_paper(json_path: Path, only: set[str] | None, apply: bool, force: bool = False) -> list[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    pdf = locate_pdf(json_path)
    log = []
    if not pdf:
        return [f"{json_path.stem}：找不到 PDF，跳过"]
    for sec in data.get("sections") or []:
        for q in sec.get("questions") or []:
            if only and str(q.get("id")) not in only:
                continue
            if not force and not needs_fix(q):
                continue
            stem = q.get("content") or ""
            page = find_page(pdf, stem)
            if not page:
                log.append(f"{json_path.stem}｜{sec.get('category')} {q.get('id')}：PDF 里定位不到题干，跳过")
                continue
            with tempfile.TemporaryDirectory() as td:
                img = render_page(pdf, page, Path(td))
                if not img:
                    log.append(f"{json_path.stem}｜{q.get('id')}：渲染失败，跳过")
                    continue
                try:
                    res = ask_vision(img, stem)
                except Exception as e:
                    log.append(f"{json_path.stem}｜{q.get('id')}：视觉调用失败 {str(e)[:60]}")
                    continue
            opts = res.get("options") or []
            need_pair = bool(PAIR_STEM.search(stem))
            incomplete = need_pair and (len(opts) != 4 or not all("/" in o.get("label", "") for o in opts))
            if incomplete and res.get("found"):
                with tempfile.TemporaryDirectory() as td:
                    img = render_page(pdf, page, Path(td))
                    if img:
                        try:
                            res2 = ask_vision(img, stem, strict=True)
                            if res2.get("found") and len(res2.get("options") or []) == 4:
                                res = res2
                                opts = res["options"]
                        except Exception as e:
                            log.append(f"{json_path.stem}｜{q.get('id')}：严格重试失败 {str(e)[:40]}")
            if not res.get("found") or len(opts) != 4:
                log.append(f"{json_path.stem}｜{sec.get('category')} {q.get('id')}：视觉未还原（found={res.get('found')}, opts={len(opts)}）")
                continue
            log.append(f"{json_path.stem}｜{sec.get('category')} {q.get('id')}：还原成功 → " +
                       " | ".join(f"{o['id']}.{o['label'][:18]}" for o in opts))
            if apply:
                q["questionType"] = "single"
                q["options"] = [{"id": o["id"], "label": o["label"]} for o in opts]
                q["score"] = 3
                if not str(q.get("answer") or "").strip():
                    q["answer"] = ""
    if apply:
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return log


def scan() -> list[tuple[Path, list[str]]]:
    out = []
    for f in sorted(JSON_ROOT.glob("*/*.json")):
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
    ap.add_argument("--apply", nargs="+", default=[], help="要修的 JSON（相对 JSON_ROOT 或绝对路径）")
    ap.add_argument("--only", nargs="+", default=None, help="只修这些题号")
    ap.add_argument("--apply-all", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略题型判定，强制重做 --only 指定的题")
    args = ap.parse_args()

    if args.scan:
        rows = scan()
        print(f"待修 {len(rows)} 卷：")
        for f, hit in rows:
            print(f"  · {f.parent.name}/{f.name}：{', '.join(hit)}")
        return 0

    targets: list[Path] = []
    if args.apply_all:
        targets = [f for f, _ in scan()]
    for a in args.apply:
        p = Path(a)
        targets.append(p if p.is_absolute() else JSON_ROOT / a)

    only = set(args.only) if args.only else None
    for t in targets:
        for line in fix_paper(t, only, apply=True, force=args.force):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
