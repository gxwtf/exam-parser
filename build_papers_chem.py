#!/usr/bin/env python3
"""化学：给已裁好图的卷生成 paper.json（选择题答案从 MD 抽 + 非选择题答案/题型从 MD 来）

逐卷做三件事：
1. extract_choice_answers.py 从 <卷>（教师版）.md 抽 1..14 的选择题答案 → 临时 choice json；
2. build_image_questions.py -d <裁图目录> --md <md> --choice-json <临时> 生成 paper.json
   （非选择题答案与题型 category 都在这一步写入）；
3. 汇报：选择题答案抽到几个、非选择题答案缺几个、paper.json 是否生成。

用法：
  python3 build_papers_chem.py --type 一模 --type 上期末 --year-from 2021 --grade 高三
  python3 build_papers_chem.py --type 真题 --year-from 2011 --must 北京
  python3 build_papers_chem.py ... --dry-run
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_choice_answers as eca  # noqa: E402

DATA = Path("/home/gxwtf/jingshibang")
PROJ = Path(__file__).resolve().parent
SUBJECT = "化学"


def norm_stem(name: str) -> str:
    return re.sub(r"（教师版）|\(教师版\)|含答案|教师版", "", name).strip()


def find_md(t: str, stem: str) -> Path | None:
    base = DATA / "output_docx_to_md" / t / SUBJECT
    for cand in (base / f"{stem}（教师版）", base / f"{stem}含答案", base / stem):
        mds = sorted(cand.glob("*.md")) if cand.is_dir() else []
        if mds:
            return mds[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", action="append", required=True)
    ap.add_argument("--year-from", type=int, default=2011)
    ap.add_argument("--year-to", type=int, default=2030)
    ap.add_argument("--grade", default=None)
    ap.add_argument("--must", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    crop_root = DATA / "output_image_questions"
    items: list[tuple[str, str]] = []
    for t in args.type:
        d = crop_root / t / SUBJECT
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_dir():
                continue
            m = re.search(r"(20\d{2})", p.name)
            if not m:
                continue
            y = int(m.group(1))
            if not (args.year_from <= y <= args.year_to):
                continue
            if args.grade and "高三" not in p.name:
                continue
            if args.must and args.must not in p.name:
                continue
            items.append((t, p.name))

    print(f"待构建 {len(items)} 卷", flush=True)
    ok = no_md = no_choice = fail = 0
    problems: list[str] = []
    t0 = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="chem-choice-"))

    for i, (t, stem) in enumerate(items, 1):
        crop_dir = crop_root / t / SUBJECT / stem
        md = find_md(t, stem)
        if md is None:
            no_md += 1
            problems.append(f"{t}/{stem}：找不到 MD（无法取答案/题型）")
            print(f"[{i}/{len(items)}] ✗ {stem} 无 MD", flush=True)
            continue
        answers = eca.extract(md)
        if len(answers) < 10:
            no_choice += 1
            problems.append(f"{t}/{stem}：选择题答案只抽到 {len(answers)} 个")
        choice_json = tmpdir / f"{t}-{stem}.json"
        choice_json.write_text(json.dumps(eca.to_choice_json(answers), ensure_ascii=False), encoding="utf-8")
        if args.dry_run:
            print(f"[{i}/{len(items)}] [dry] {t}/{stem}：选择 {len(answers)} 个答案，md={md.parent.name}", flush=True)
            continue
        r = subprocess.run(
            [sys.executable, str(PROJ / "build_image_questions.py"), "-d", str(crop_dir),
             "--md", str(md), "--choice-json", str(choice_json), "-T", t],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0 or not (crop_dir / "paper.json").exists():
            fail += 1
            problems.append(f"{t}/{stem}：build 失败 {(r.stderr or r.stdout).strip()[-120:]}")
            print(f"[{i}/{len(items)}] ✗ {stem} build 失败", flush=True)
            continue
        ok += 1
        print(f"[{i}/{len(items)}] ✓ {t} / {stem}：选择 {len(answers)} 个  {(r.stdout or '').strip().splitlines()[-1] if r.stdout else ''}", flush=True)

    print(f"\n完成：成功 {ok} / 缺 MD {no_md} / 答案不全 {no_choice} / 失败 {fail}，用时 {int(time.time()-t0)}s", flush=True)
    for p in problems[:40]:
        print("  -", p, flush=True)


if __name__ == "__main__":
    main()
