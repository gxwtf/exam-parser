#!/usr/bin/env python3
"""语文批量迁移驱动（2025/2026 一模·上期末 + 二模全量 + 北京真题）。

只做两件事，每卷按顺序走一遍，失败逐卷分类记录：
  1) docx → MD：`batch_convert_docx_to_md.py -p <docx> -o output_docx_to_md/<类型>/语文`（md 已存在就跳过）
  2) MD → JSON：`convert_chinese_md.py -p <md> -T <类型>`（出口含合并/分值/清洗 + 备份旧 JSON）

用法：
    python3 batch_chinese_migration.py                       # 跑全部（默认 3 并发）
    python3 batch_chinese_migration.py --only 二模            # 只跑某类型
    python3 batch_chinese_migration.py --workers 2
    python3 batch_chinese_migration.py --report /tmp/x.json   # 结果落盘

结果：<repo>/batch_chinese_last.json（逐卷 status/reason/耗时），stdout 打汇总。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from paths import DATA_ROOT, GAOZHONG, OUTPUT_DOCX_TO_MD, OUTPUT_JSON_CHINESE

SUBJECT = "语文"
SCOPE_FILE = Path("/tmp/chn_scope.json")
# 真题：只做北京历年整卷（scope 清单为准）；以下两份不是整卷，跳过（汇报里说明）
SKIP_TRUE = {
    "2026北京高考真题语文作文_(教师版).docx",          # 作文单题版
    '【chatGPT版】2023年北京高考作文"范文".docx',       # 范文，不是试卷
}

FAIL_PATTERNS = [
    (r"没找到「参考答案」区", "答案区对不上"),
    (r"结构规划为空", "切板块失败"),
    (r"没有规划缓存", "结构规划缺失"),
    (r"没切到|切不到", "切板块失败"),
    (r"md 没生成|转换失败|Traceback.*docx", "md 没生成"),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean_title(stem: str) -> str:
    t = re.sub(r"[（(]教师版[）)]|[（(]含答案[）)]|含答案|_", "", stem)
    return re.sub(r"\s+", "", t).strip()


def find_md(type_dir: str, title: str) -> Path | None:
    """按卷名在 output_docx_to_md/<类型>/语文/<卷>*/ 下找 md（容忍「（教师版）」变体）"""
    root = OUTPUT_DOCX_TO_MD / type_dir / SUBJECT
    if not root.exists():
        return None
    for d in sorted(root.iterdir()):
        if not d.is_dir() or clean_title(d.name) != title:
            continue
        mds = sorted(d.glob("*.md"))
        if mds:
            return mds[0]
    return None


def build_items() -> list[dict]:
    scope = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
    items: list[dict] = []
    # 一模 / 上期末 / 真题：以 docx 为准
    for type_dir in ("一模", "上期末", "真题"):
        for name in scope.get(type_dir, []):
            if type_dir == "真题" and name in SKIP_TRUE:
                continue
            if type_dir == "真题" and "北京" not in name:      # 只做北京卷
                continue
            docx = GAOZHONG / type_dir / SUBJECT / name
            title = clean_title(Path(name).stem)
            items.append({"type": type_dir, "title": title,
                          "docx": str(docx) if docx.exists() else None,
                          "md": str(find_md(type_dir, title) or "")})
    # 二模：以存量 JSON 全量为准（96 卷），md 已存在
    jroot = OUTPUT_JSON_CHINESE / "二模"
    for jf in sorted(jroot.glob("*.json")):
        if jf.name.endswith(".old-groups.json"):
            continue
        title = jf.stem
        items.append({"type": "二模", "title": title, "docx": None,
                      "md": str(find_md("二模", title) or "")})
    # 去重
    seen, out = set(), []
    for it in items:
        k = (it["type"], it["title"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def classify(out: str) -> str:
    tail = out[-4000:]
    for pat, why in FAIL_PATTERNS:
        if re.search(pat, tail):
            return why
    m = re.search(r"✗[^\n]*", tail)
    return ("运行时错误：" + (m.group(0)[:120] if m else tail.strip().splitlines()[-1][:120] if tail.strip() else "未知"))


def one(it: dict, args) -> dict:
    t0 = time.time()
    rec = dict(it, status="", reason="", secs=0.0)
    md = Path(it["md"]) if it["md"] else None
    # ① docx → md
    if md is None or not md.exists():
        if not it["docx"]:
            rec.update(status="fail", reason="md 没生成：找不到 docx 也没有现成 md", secs=round(time.time() - t0, 1))
            return rec
        rc, out = run([sys.executable, "batch_convert_docx_to_md.py", "-p", it["docx"],
                       "-o", str(OUTPUT_DOCX_TO_MD / it["type"] / SUBJECT)])
        md = find_md(it["type"], it["title"])
        if md is None or not md.exists():
            rec.update(status="fail", reason="md 没生成：" + classify(out), secs=round(time.time() - t0, 1))
            return rec
        rec["md"] = str(md)
    # ② md → json
    cmd = [sys.executable, "convert_chinese_md.py", "-p", str(md), "-T", it["type"], "--force"]
    if args.no_ai_analysis:
        cmd.append("--no-ai-analysis")
    rc, out = run(cmd)
    rec["secs"] = round(time.time() - t0, 1)
    if rc == 0:
        rec.update(status="ok")
    else:
        rec.update(status="fail", reason=classify(out))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某类型（一模/上期末/二模/真题）")
    ap.add_argument("--workers", type=int, default=3, help="并发（建议 2–4，别拉爆网关）")
    ap.add_argument("--report", default=str(Path(__file__).parent / "batch_chinese_last.json"))
    ap.add_argument("--no-ai-analysis", dest="no_ai_analysis", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--exclude", default=None, help="跳过清单 json（[{type,title}]，已导库的冻结卷）")
    args = ap.parse_args()

    items = build_items()
    if args.exclude:
        ex = {(x["type"], x["title"]) for x in json.loads(Path(args.exclude).read_text(encoding="utf-8"))}
        before = len(items)
        items = [x for x in items if (x["type"], x["title"]) not in ex]
        log(f"跳过冻结卷 {before - len(items)} 卷")
    if args.only:
        items = [x for x in items if x["type"] == args.only]
    if args.limit:
        items = items[: args.limit]
    log(f"共 {len(items)} 卷待处理（并发 {args.workers}）")

    recs: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(one, it, args): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:                                    # noqa: BLE001
                rec = dict(it, status="fail", reason=f"驱动异常：{str(e)[:150]}", secs=0.0)
            recs.append(rec)
            done += 1
            flag = "✓" if rec["status"] == "ok" else "✗"
            log(f"  [{done}/{len(items)}] {flag} {rec['type']}/{rec['title']}"
                + (f"（{rec['secs']}s）" if rec["status"] == "ok" else f"  ← {rec['reason'][:100]}"))

    Path(args.report).write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    # 汇总
    for t in ("一模", "上期末", "二模", "真题"):
        sub = [r for r in recs if r["type"] == t]
        if not sub:
            continue
        ok = [r for r in sub if r["status"] == "ok"]
        log(f"{t}：目标 {len(sub)} / 成功 {len(ok)} / 失败 {len(sub) - len(ok)}"
            + ("" if len(ok) == len(sub) else "｜失败：" + "，".join(f"{r['title']}({r['reason'][:40]})"
                                                                     for r in sub if r["status"] != "ok")))
    total_secs = sum(r["secs"] for r in recs)
    log(f"耗时合计 {total_secs / 60:.1f} 分钟（含模型调用）；结果：{args.report}")
    bad = [r for r in recs if r["status"] != "ok"]
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
