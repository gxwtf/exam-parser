#!/usr/bin/env python3
"""化学图片版：批量裁图（只裁图，不动答案/入库）

按「卷别 + 年份 + 年级 + 关键词」挑出原卷 PDF，逐卷调用 crop_questions_pdf.py 裁出
qNN.png（写到 output_docx_to_md/<类型>/化学/<卷>/images/）与 qNN.json/manifest.json
（写到 output_image_questions/<类型>/化学/<卷>/）。

用法（在 exam-parser 目录，或任意目录用绝对路径）：
  python3 crop_batch_chem.py --type 一模 --type 上期末 --year-from 2021 --grade 高三
  python3 crop_batch_chem.py --type 真题 --year-from 2011 --must 北京
  python3 crop_batch_chem.py --type 一模 --year-from 2021 --grade 高三 --limit 2 --dry-run

选项：
  --type      卷别（可重复）：一模/二模/上期末/下期末/真题/上期中…
  --year-from/--year-to   年份区间（含端点，默认 2011~2030）
  --grade     文件名必须包含的年级词，如 高三
  --must      文件名必须包含的关键词，如 北京（真题用来排除外省卷）
  --only      只做这些卷名（精确匹配，可多个）
  --skip-existing  已有 manifest.json 的卷跳过（默认跳）
  --dry-run   只打印将要处理的清单
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DATA_ROOT = Path("/home/gxwtf/jingshibang")
PROJ = Path(__file__).resolve().parent
SUBJECT = "化学"


def norm_stem(p: Path) -> str:
    return re.sub(r"（教师版）|\(教师版\)|含答案|教师版", "", p.stem).strip()


def pick(args) -> list[tuple[str, Path]]:
    picked: dict[tuple[str, str], Path] = {}
    for t in args.type:
        src = DATA_ROOT / "gaozhong" / t / SUBJECT
        if not src.is_dir():
            print(f"⚠ 没有目录：{src}")
            continue
        for pdf in sorted(src.glob("*.pdf")):
            name = pdf.stem
            m = re.search(r"(20\d{2})", name)
            if not m:
                continue
            year = int(m.group(1))
            if not (args.year_from <= year <= args.year_to):
                continue
            if args.grade and args.grade not in name:
                continue
            if args.must and args.must not in name:
                continue
            stem = norm_stem(pdf)
            if args.only and stem not in args.only:
                continue
            key = (t, stem)
            # 同一卷有多个 PDF 时优先「教师版」
            if key not in picked or "教师版" in name:
                picked[key] = pdf
    return sorted(picked.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", action="append", required=True)
    ap.add_argument("--year-from", type=int, default=2011)
    ap.add_argument("--year-to", type=int, default=2030)
    ap.add_argument("--grade", default=None)
    ap.add_argument("--must", default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--force", action="store_true", help="已裁过也重裁")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    items = pick(args)
    print(f"待处理 {len(items)} 卷（类型 {args.type}，年份 {args.year_from}-{args.year_to}"
          f"{f'，年级 {args.grade}' if args.grade else ''}{f'，含 {args.must}' if args.must else ''}）", flush=True)

    ok = skip = fail = 0
    fails: list[str] = []
    t_all = time.time()
    for i, ((t, stem), pdf) in enumerate(items, 1):
        out_dir = DATA_ROOT / "output_image_questions" / t / SUBJECT / stem
        serve = DATA_ROOT / "output_docx_to_md" / t / SUBJECT / stem / "images"
        if args.skip_existing and not args.force and (out_dir / "manifest.json").exists():
            skip += 1
            print(f"[{i}/{len(items)}] — 跳过（已裁过）{stem}", flush=True)
            continue
        if args.dry_run:
            print(f"[{i}/{len(items)}] [dry] {t} / {stem}", flush=True)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        serve.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, str(PROJ / "crop_questions_pdf.py"),
             "-p", str(pdf), "-o", str(out_dir), "--serve-dir", str(serve),
             "-T", t, "-s", SUBJECT],
            capture_output=True, text=True, timeout=args.timeout,
        )
        found = 0
        m = re.search(r"识别题号 (\d+) 个", r.stdout or "")
        if m:
            found = int(m.group(1))
        if r.returncode != 0 or found == 0:
            fail += 1
            reason = (r.stderr or r.stdout or "").strip().replace("\n", " ")[-120:]
            fails.append(f"{t}/{stem}: {reason}")
            print(f"[{i}/{len(items)}] ✗ {stem}（{reason}）", flush=True)
            continue
        ok += 1
        print(f"[{i}/{len(items)}] ✓ {t} / {stem}：{found} 题，{time.time()-t0:.0f}s", flush=True)

    print(f"\n完成：成功 {ok} / 跳过 {skip} / 失败 {fail}，用时 {int(time.time()-t_all)}s", flush=True)
    if fails:
        print("失败清单：", flush=True)
        for f in fails[:30]:
            print("  -", f, flush=True)
    Path("/tmp/crop_batch_chem_last.json").write_text(
        json.dumps({"ok": ok, "skip": skip, "fail": fail, "fails": fails}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
