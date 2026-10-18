#!/usr/bin/env python3
"""
数学试卷 PDF → JSON（多模态大模型逐页识别）

为什么不走「PDF → MD → JSON」：
  1. mineru 的 OCR 会漏数学公式和选项（实测 2024西城一模 第1题两个选项是空的、
     第7题分段函数只识别出半句）；
  2. pdftotext 虽有完整文字层，但 ∪ ∞ ∩ √ π 这些符号在 PDF 里是矢量图形，
     文字层里根本不存在（实测 0 命中）。
所以直接用多模态大模型读页面图像，一次性拿到「题干 + 选项 + 填空位 + 图 + 答案」。

只处理【选择题】和【填空题】：解答题（题干含"解答应写出文字说明"之后的部分）整段跳过。

用法：
  python3 convert_math_pdf.py -p ../../gaozhong/一模/数学/2024北京西城高三一模数学（教师版）.pdf
  python3 convert_math_pdf.py --dir ../../gaozhong -T 一模 -g 高三      # 批量
  python3 convert_math_pdf.py -p xxx.pdf --pages 2                      # 只跑前 2 页（调试）
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = (os.getenv("MATH_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = os.getenv("MATH_VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")
MAX_TOKENS = int(os.getenv("MATH_MAX_TOKENS", "8000"))
DPI = int(os.getenv("MATH_DPI", "150"))
CONCURRENCY = int(os.getenv("MATH_CONCURRENCY", "1"))

SYSTEM_PROMPT = (
    "你是数学试卷录入员，负责把试卷页面的图像精确转成结构化 JSON。"
    "你必须只输出 JSON，不要 markdown 代码块，不要任何解释。"
)

PAGE_PROMPT = """这是一张北京高中数学试卷（或参考答案）的页面图像。

请只处理【选择题】和【填空题】，**忽略解答题**（解答题是"解答应写出文字说明、演算步骤或证明过程"那类大题）。

严格按下面的 JSON 结构输出（只输出 JSON）：
{
  "questions": [
    {
      "no": 1,
      "type": "single",
      "stem": "题干原文，公式用 $...$ 包裹的 LaTeX，中文照抄",
      "options": [{"id": "a", "label": "$...$"}, {"id": "b", "label": "$...$"}, {"id": "c", "label": "$...$"}, {"id": "d", "label": "$...$"}],
      "hasFigure": false,
      "note": ""
    },
    {
      "no": 11,
      "type": "fill",
      "stem": "题干原文，空位一律写成 <Input2></Input2>（有几个空就写几个）",
      "options": [],
      "hasFigure": false,
      "note": ""
    }
  ],
  "answers": [{"no": 1, "answer": "B"}],
  "figures": [{"no": 6, "bbox": [0.0, 0.0, 0.0, 0.0]}]
}

硬性要求：
1. 题干与选项**逐字照抄原图**，不要改写、不要翻译、不要补全图中没有的内容；图里印错或看不清的，照原样保留并在 note 里说明。
2. 选择题：题干要求"选出符合题目要求的一项"时 type 用 "single"；若本卷选择题说明里写了"多项符合题目要求""有多项符合题目要求"之类，则 type 用 "multiple"。
3. 选项标签里的公式只写公式本身，不含 (A) 这类前缀；选项在图中确实空缺时 label 用空字符串 ""。
4. 公式用干净的 LaTeX，不要多余空格：\\frac{1}{2}、x^{2}、\\sqrt{3}、\\log_{2}|x|、\\cup、\\infty、\\cap、\\complement_{U}B。
5. 填空题：把每个空位改写成 <Input2></Input2> 放在原位置（例如"则 A∪B=<Input2></Input2>"）；若原图用下划线表示空位，替换之；若某题看不出空位，note 里写"未识别到空位"。
6. 题干引用了图形（三视图、程序框图、向量网格图、几何图等）时 hasFigure 置 true，并在 figures 里给出该图在本页的相对坐标 bbox=[x0,y0,x1,y1]（0~1 归一化，左上角为原点）。
7. 答案页里答案可能不在单独的答案表里，而是埋在解析文字中，形如「故答案为：X」「答案为：X」「答案：X」「故填：X」——请把 X 提取为该题 answer（多空答案连原样保留，如「①…②…」）。
8. 如果本页开头有**延续上一页的题干文字**（本页第一个题号之前、没有题号的那几行，例如「若 PA=6，则 PC=____．」），
   请单独作为一条 question 输出：no 取「本页第一个题号 - 1」，题干原样照抄（空位同样写成 <Input2></Input2>），
   并在 note 里写「跨页续写」。
9. 本页没有题目就把 questions 设为 []；本页不是答案页就把 answers 设为 []。
"""


REPAIR_PROMPT = """这张图是数学试卷（或答案）的一页。请只处理第 {no} 题：
- 若本页印有该题的四个选项，输出 {{"options": [{{"id": "a", "label": "$...$"}}, {{"id": "b", "label": "$...$"}}, {{"id": "c", "label": "$...$"}}, {{"id": "d", "label": "$...$"}}]}}
- 若本页印有该题的答案，输出 {{"answer": "X"}}。答案可能不在答案表里，而在解析末尾，形如「故选 A．」「故答案为：X」「故填：X」——这些都算该题答案（多空答案用「；」分隔并全部保留）
- 两者都有就都输出；都没有则输出 {{}}
只输出 JSON，不要解释。"""


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def chat_json(messages: list, model: str, max_tokens: int, retries: int = 4) -> dict:
    """调用多模态接口并解析 JSON（带 429/5xx 重试）"""
    payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.1, "messages": messages}
    if os.getenv("MATH_REASONING", "none").lower() in ("none", "off", "false"):
        payload["reasoning_effort"] = "none"   # 本地/OpenAI 兼容端点：关掉思考，直接输出 JSON
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if "openrouter" in BASE_URL:
        headers.update({"HTTP-Referer": "https://ti.gxwtf.cn", "X-Title": "gxexercise-math-parser"})
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers,
                                 timeout=int(os.getenv("MATH_REQUEST_TIMEOUT", "240")))
            if resp.status_code == 400 and "reasoning" in (resp.text or "").lower() and "reasoning_effort" in payload:
                payload.pop("reasoning_effort", None)
                log("    该端点不支持 reasoning_effort，去掉后重试")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 15 * (attempt + 1)
                log(f"    HTTP {resp.status_code}，{wait}s 后重试（{attempt+1}/{retries}）")
                time.sleep(wait)
                last_err = f"HTTP {resp.status_code}"
                continue
            resp.raise_for_status()
            data = resp.json()
            ch = data["choices"][0]
            text = (ch["message"].get("content") or "").strip()
            if ch.get("finish_reason") == "length" and len(text) < 200:
                # 推理模型被长度截断且内容太少 → 加倍重试
                payload["max_tokens"] = min(max_tokens * 2, 32000)
                log(f"    输出被截断（finish=length），把 max_tokens 提到 {payload['max_tokens']} 重试")
                last_err = "finish=length"
                continue
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
            if not text:
                last_err = "空内容"
                time.sleep(5)
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.S) or re.search(r"\[.*\]", text, re.S)
                if m:
                    return json.loads(m.group(0))
                raise
        except Exception as e:
            last_err = str(e)[:120]
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"调用模型失败：{last_err}")


def select_pages(pdf: Path, n_pages: int) -> set[int]:
    """用 PDF 文字层预筛：只保留可能含选择题/填空题或其答案的页，解答题页直接跳过
    （实测解答题页约占一半，这些页送模型纯属浪费）"""
    keep: set[int] = set()
    ans_pages: list[int] = []
    for i in range(1, n_pages + 1):
        try:
            txt = subprocess.run(
                ["pdftotext", "-f", str(i), "-l", str(i), str(pdf), "-"],
                capture_output=True, text=True, timeout=30,
            ).stdout or ""
        except Exception:
            keep.add(i)          # 拿不到文字层就保守处理，照常送模型
            continue
        flat = txt.replace(" ", "")
        if "参考答案" in flat or "答案与解析" in flat or "答案解析" in flat:
            ans_pages.append(i)
        if "选择题" in flat or "填空题" in flat:
            keep.add(i)
        elif re.search(r"[（(]\s*([1-9]|1[0-5])\s*[）)]", txt):
            keep.add(i)
        elif "故答案为" in flat or "故选" in flat:
            keep.add(i)
        elif re.search(r"^\s*(1[0-5]|[1-9])\s*[.．、]", txt, re.M):
            keep.add(i)
    # 答案区前 2 页一定保留（选择题答案表 / 填空题答案通常在最前面）
    for p in ans_pages[:1]:
        keep.update({p, p + 1})
    return {p for p in keep if 1 <= p <= n_pages}


def render_pages(pdf: Path, dpi: int, out_dir: Path, max_pages: int | None = None) -> list[Path]:
    """PDF → 每页 PNG"""
    prefix = out_dir / "page"
    cmd = ["pdftoppm", "-png", "-r", str(dpi)]
    if max_pages:
        cmd += ["-f", "1", "-l", str(max_pages)]
    cmd += [str(pdf), str(prefix)]
    subprocess.run(cmd, check=True, capture_output=True)
    pages = sorted(out_dir.glob("page-*.png")) + sorted(out_dir.glob("page*.png"))
    # pdftoppm 命名可能是 page-01.png / page-1.png
    pages = sorted(set(pages), key=lambda p: p.name)
    return pages


def ask_page(image: Path, model: str) -> dict:
    """调多模态模型识别一页"""
    b64 = base64.b64encode(image.read_bytes()).decode()
    return chat_json(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": PAGE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        model, MAX_TOKENS,
    )


def crop_figures(image: Path, figures: list[dict], out_img_dir: Path, stem: str) -> dict[int, str]:
    """按归一化 bbox 裁剪图形，返回 {题号: 相对图片路径}"""
    saved = {}
    if not figures:
        return saved
    for fig in figures:
        try:
            no = int(fig.get("no"))
            x0, y0, x1, y1 = [float(v) for v in fig["bbox"]]
        except Exception:
            continue
        w, h = x0 * 0 + (x1 - x0), (y1 - y0)
        if w <= 0.02 or h <= 0.02:
            continue
        # 用 ImageMagick 裁剪（venv 里没有 PIL）
        out_img_dir.mkdir(parents=True, exist_ok=True)
        dst = out_img_dir / f"{stem}_q{no}.png"
        geo = f"{int(w * 1000)}%x{int(h * 1000)}%+{int(x0 * 100)}%+{int(y0 * 100)}%"
        try:
            subprocess.run(
                ["convert", str(image), "-crop", geo, "+repage", str(dst)],
                check=True, capture_output=True,
            )
            saved[no] = f"images/{dst.name}"
        except Exception as e:
            log(f"    图形裁剪失败（第{no}题）：{e}")
    return saved


def normalize_latex(text: str) -> str:
    r"""把模型输出的 \(...\) / \[...\] 统一成 $...$ / $$...$$（题库用 remark-math 渲染）"""
    t = text or ""
    if "\\(" in t or "\\[" in t:
        t = t.replace("\\[", "$$").replace("\\]", "$$").replace("\\(", "$").replace("\\)", "$")
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def classify(q: dict, choice_max: int) -> str:
    """判定选择题/填空题：优先用模型给的 type，缺失时按是否有选项 + 题号兜底
    （北京卷选择题数量因年份而异：2014 年是 8 题、2024 年是 10 题）"""
    t = (q.get("type") or "").strip().lower()
    if t.startswith(("fill", "input", "blank")):
        return "fill"
    if t.startswith(("single", "multi", "choice", "select")):
        return "choice"
    if q.get("options"):
        return "choice"
    try:
        return "choice" if int(q.get("no")) <= choice_max else "fill"
    except Exception:
        return "fill"


def normalize_question(q: dict, section: str) -> dict | None:
    """整理单题：字段补全 + 清洗"""
    try:
        no = int(q.get("no"))
    except Exception:
        return None
    qtype = "fill" if section == "fill" else "single"
    raw_type = (q.get("type") or "").strip().lower()
    if section == "choice":
        qtype = "multiple" if raw_type.startswith("multi") else "single"

    stem = normalize_latex(q.get("stem") or "")

    options = []
    for o in (q.get("options") or []):
        oid = str(o.get("id") or "").strip().lower()
        label = normalize_latex(o.get("label") or "")
        if oid in {"a", "b", "c", "d"}:
            options.append({"id": oid, "label": label})
    if section == "choice":
        # 保证 a-d 四个槽位都在
        have = {o["id"] for o in options}
        options = [o for o in options if o["id"] in have] + [
            {"id": x, "label": ""} for x in "abcd" if x not in have
        ]
        options.sort(key=lambda o: "abcd".index(o["id"]))

    return {
        "no": no,
        "questionType": "input2" if section == "fill" else qtype,
        "content": stem,
        "options": options,
        "hasFigure": bool(q.get("hasFigure")),
        "note": (q.get("note") or "").strip(),
    }


def repair_question(image: Path, no: int, model: str) -> dict:
    """针对单题定向补漏：只问四选项 / 答案"""
    b64 = base64.b64encode(image.read_bytes()).decode()
    try:
        return chat_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": REPAIR_PROMPT.format(no=no)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ],
            model, 4000, retries=2,
        )
    except Exception:
        return {}


def build_json(pdf: Path, pages: list[Path], model: str, out_root: Path, stem: str, args) -> tuple[dict, list[str]]:
    """逐页识别 → 合并成 exam-parser 风格的 JSON"""
    all_q: dict[int, dict] = {}
    answers: dict[int, str] = {}
    q_page: dict[int, int] = {}
    answer_pages: list[int] = []
    review: list[str] = []

    # ① 预筛页面：跳过解答题页（约占一半）
    selected = None
    if getattr(args, "smart_pages", True):
        selected = select_pages(pdf, len(pages))
        skipped = [i for i in range(1, len(pages) + 1) if i not in selected]
        if skipped:
            log(f"  预筛：跳过 {len(skipped)}/{len(pages)} 页（解答题页），只送 {len(selected)} 页")
    todo = [(i, p) for i, p in enumerate(pages, 1) if selected is None or i in selected]

    # ② 逐页识别（页级并发，结果按页序合并）
    page_results: dict[int, dict] = {}

    def _one(idx: int, page: Path):
        t0 = time.time()
        try:
            data = ask_page(page, model)
            log(f"  第 {idx}/{len(pages)} 页：{len(data.get('questions') or [])} 题、"
                f"{len(data.get('answers') or [])} 个答案（{time.time()-t0:.1f}s）")
            return idx, data, None
        except Exception as e:
            msg = str(e)[:120]
            log(f"  第 {idx}/{len(pages)} 页失败：{msg}")
            return idx, None, msg

    workers = max(1, int(getattr(args, "concurrency", 1) or 1))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(_one, i, p) for i, p in todo]):
                idx, data, err = fut.result()
                if err:
                    review.append(f"第 {idx} 页识别失败：{err}")
                else:
                    page_results[idx] = data
    else:
        for i, p in todo:
            idx, data, err = _one(i, p)
            if err:
                review.append(f"第 {idx} 页识别失败：{err}")
            else:
                page_results[idx] = data

    # ③ 按页序合并
    for idx in sorted(page_results):
        page = pages[idx - 1]
        data = page_results[idx]
        qs = data.get("questions") or []
        ans = data.get("answers") or []
        figs = data.get("figures") or []

        # 图形裁剪需要知道每题属于哪类（本页题号即可判断：题号 > 选择题上限的算填空）
        for q in qs:
            try:
                no = int(q.get("no"))
            except Exception:
                continue
            section = classify(q, args.choice_max)
            norm = normalize_question(q, section)
            if not norm:
                continue
            if norm["no"] in all_q:
                # 同一题跨页出现（题干被页边界截断）：按页序拼接，并去掉重复行
                prev = all_q[norm["no"]]
                pc, nc = prev["content"], norm["content"]
                if nc and nc not in pc:
                    if pc and pc in nc:
                        merged = nc
                    else:
                        seen = set(l.strip() for l in pc.split("\n") if l.strip())
                        add = [l for l in nc.split("\n") if l.strip() and l.strip() not in seen]
                        merged = pc + ("\n\n" + "\n".join(add) if add else "")
                    prev["content"] = merged
                    prev["note"] = (prev.get("note") or "") or (norm.get("note") or "")
                if norm.get("figurePath") and not prev.get("figurePath"):
                    prev["figurePath"] = norm["figurePath"]
                if len(norm.get("options") or []) > len(prev.get("options") or []):
                    prev["options"] = norm["options"]
                continue
            # 图形
            if norm["hasFigure"] and figs:
                paths = crop_figures(page, [f for f in figs if str(f.get("no")) == str(no)],
                                     out_root / "images", stem)
                if norm["no"] in paths:
                    norm["figurePath"] = paths[norm["no"]]
            all_q[norm["no"]] = norm
            q_page.setdefault(norm["no"], idx)
        if ans:
            answer_pages.append(idx)
        for a in ans:
            try:
                answers[int(a.get("no"))] = str(a.get("answer") or "").strip()
            except Exception:
                continue

    # ── 定向补漏：选项空缺的选择题、缺答案的题，去原页与相邻页/答案页再问一次
    if getattr(args, "repair", True):
        need_opt = [no for no, q in all_q.items()
                    if q["questionType"] != "input2" and any(not o["label"] for o in q["options"])]
        need_ans = [no for no in all_q if not answers.get(no)]
        for no in sorted(set(need_opt) | set(need_ans)):
            cand = []
            if no in q_page:
                cand += [q_page[no], q_page[no] + 1]
            cand += answer_pages
            # 缺答案时：候选页不够就扩到全卷（答案常藏在被预筛跳过的解析页里）
            if no in need_ans:
                extra = [i for i in range(1, len(pages) + 1) if i not in cand]
                cand += extra
            cand = [p for p in dict.fromkeys(cand) if 1 <= p <= len(pages)]
            for pno in cand:
                try:
                    got = repair_question(pages[pno - 1], no, model)
                except Exception:
                    continue
                opts = got.get("options") or []
                if no in need_opt and opts:
                    fixed = [{"id": str(o.get("id", "")).lower(), "label": (o.get("label") or "").strip()}
                             for o in opts if str(o.get("id", "")).lower() in "abcd"]
                    if len(fixed) == 4 and all(o["label"] for o in fixed):
                        fixed.sort(key=lambda o: "abcd".index(o["id"]))
                        all_q[no]["options"] = fixed
                        log(f"  补漏：第{no}题选项（第{pno}页）")
                        need_opt.remove(no)
                ansv = (got.get("answer") or "").strip()
                if no in need_ans and ansv:
                    answers[no] = ansv
                    log(f"  补漏：第{no}题答案（第{pno}页）")
                    need_ans.remove(no)
                if no not in need_opt and no not in need_ans:
                    break

    # ── 组装 sections
    choice_qs, fill_qs = [], []
    for no in sorted(all_q):
        q = all_q[no]
        if q["questionType"] == "input2":
            q["score"] = args.fill_score
            fill_qs.append(q)
        else:
            q["score"] = args.choice_score
            choice_qs.append(q)

    def pack(qs, fmt):
        out = []
        for q in qs:
            item = {
                "id": q["no"],
                "questionType": q["questionType"],
                "content": q["content"],
                "options": [] if fmt == "fill" else q["options"],
                "answer": answers.get(q["no"], ""),
                "analysis": "",
                "score": q["score"],
            }
            if q.get("figurePath"):
                item["content"] = item["content"] + f"\n\n![]({q['figurePath']})"
            if q.get("note"):
                item["note"] = q["note"]
            out.append(item)
        return out

    sections = []
    if choice_qs:
        sections.append({
            "type": "选择题", "questionType": "math-choice", "title": "一、选择题", "category": "选择",
            "score": sum(q["score"] for q in choice_qs), "questions": pack(choice_qs, "choice"),
        })
    if fill_qs:
        sections.append({
            "type": "填空题", "questionType": "math-fill", "title": "二、填空题", "category": "填空",
            "score": sum(q["score"] for q in fill_qs), "questions": pack(fill_qs, "fill"),
        })

    # ── 复核清单
    for q in choice_qs:
        empty = [o["id"] for o in q["options"] if not o["label"]]
        if empty:
            review.append(f"第{q['no']}题 选项为空：{empty}")
    for q in fill_qs:
        n = q["content"].count("<Input2>")
        if n == 0:
            review.append(f"第{q['no']}题 没有识别到空位")
        else:
            # 多空题：答案里的分段数应不少于空数，否则很可能丢了一半答案
            ans = answers.get(q["no"], "")
            parts = [p for p in re.split(r"[；;，,、]|\s{2,}", ans) if p.strip()]
            if n > 1 and len(parts) < n:
                review.append(f"第{q['no']}题 有 {n} 个空但答案只有 {len(parts)} 段（{ans[:40]}），请核对")
    for q in choice_qs + fill_qs:
        if not answers.get(q["no"]):
            review.append(f"第{q['no']}题 缺答案")
        if q.get("note"):
            review.append(f"第{q['no']}题 模型备注：{q['note']}")

    paper = {
        "title": stem,
        "subject": "数学",
        "source": stem,
        "grade": args.grade or "高三",
        "year": int(m.group(1)) if (m := re.search(r"(20\d{2})", stem)) else None,
        "totalScore": sum(s["score"] for s in sections) or None,
        "duration": args.duration,
        "tags": [t for t in ["数学", args.grade or "高三"] + ["一模", "二模", "真题"] if t in stem] + (
            [m.group(1)] if (m := re.search(r"(20\d{2})", stem)) else []
        ),
    }
    return {"paper": paper, "sections": sections}, review


def process_pdf(pdf: Path, out_root: Path, args) -> None:
    stem = re.sub(r"（教师版）|\(教师版\)|含答案", "", pdf.stem).strip()
    subj_dir = out_root / args.type / "数学"
    dst = subj_dir / f"{stem}.json"
    if dst.exists() and not args.force:
        log(f"跳过（已存在）：{stem}")
        return

    log(f"处理：{pdf.name}")
    with tempfile.TemporaryDirectory() as tmp:
        pages = render_pages(pdf, args.dpi, Path(tmp), args.pages)
        log(f"  共 {len(pages)} 页（{args.dpi} dpi）")
        subj_dir.mkdir(parents=True, exist_ok=True)
        data, review = build_json(pdf, pages, args.model, out_root, stem, args)

    n_q = sum(len(s["questions"]) for s in data["sections"])
    if n_q == 0:
        log("  ✗ 没有识别到题目，跳过写入")
        return
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  ✓ {dst}（{n_q} 题）")
    if review:
        rp = dst.with_suffix(".review.txt")
        rp.write_text("\n".join(review), encoding="utf-8")
        log(f"  ⚠ 需人工复核 {len(review)} 处 → {rp.name}")


def main():
    global BASE_URL, API_KEY
    ap = argparse.ArgumentParser(description="数学试卷 PDF → JSON（多模态模型）")
    ap.add_argument("-p", "--path", help="单个 PDF 路径")
    ap.add_argument("--dir", default=str(PROJECT_ROOT.parent / "gaozhong"), help="批量模式：素材根目录")
    ap.add_argument("-T", "--type", default="一模", help="试卷类型目录（一模/二模/真题…）")
    ap.add_argument("-s", "--subject", default="数学", help="学科（目前只支持数学）")
    ap.add_argument("-g", "--grade", default="高三", help="年级（用于 tag / paper.grade）")
    ap.add_argument("-o", "--output", default=str(PROJECT_ROOT.parent / "output_json_math"), help="输出根目录")
    ap.add_argument("-m", "--model", default=MODEL, help=f"多模态模型（默认 {MODEL}）")
    ap.add_argument("--base-url", default=BASE_URL, help=f"接口地址（默认 {BASE_URL}）")
    ap.add_argument("--api-key", default=None, help="接口密钥（默认取 MATH_VISION_API_KEY / OPENAI_API_KEY）")
    ap.add_argument("-w", "--concurrency", type=int, default=CONCURRENCY, help="页级并发数（默认 1）")
    ap.add_argument("--dpi", type=int, default=DPI)
    ap.add_argument("--pages", type=int, default=None, help="只处理前 N 页（调试用）")
    ap.add_argument("--choice-max", type=int, default=10, help="选择题最大题号（默认 10）")
    ap.add_argument("--choice-score", type=int, default=4, help="选择题每题分值（默认 4）")
    ap.add_argument("--fill-score", type=int, default=5, help="填空题每题分值（默认 5）")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--force", action="store_true", help="覆盖已有 JSON")
    ap.add_argument("--no-repair", dest="repair", action="store_false", help="关闭定向补漏")
    ap.add_argument("--no-smart-pages", dest="smart_pages", action="store_false", help="关闭页面预筛（所有页都送模型）")
    args = ap.parse_args()

    BASE_URL = args.base_url.rstrip("/")
    if args.api_key:
        API_KEY = args.api_key

    out_root = Path(args.output)
    if args.path:
        process_pdf(Path(args.path), out_root, args)
        return

    root = Path(args.dir) / args.type / args.subject
    pdfs = sorted(root.glob("*.pdf"))
    if args.grade:
        pdfs = [p for p in pdfs if args.grade in p.name]
    log(f"共 {len(pdfs)} 份 PDF（{args.type}/{args.subject}，年级含「{args.grade}」）")
    for p in pdfs:
        try:
            process_pdf(p, out_root, args)
        except Exception as e:
            log(f"  ✗ 失败 {p.name}: {str(e)[:150]}")


if __name__ == "__main__":
    main()
