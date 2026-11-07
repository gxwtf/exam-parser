#!/usr/bin/env python3
"""化学：把指定卷别的 docx 顺序转成 MD（逐文件调用 w2m，不用多进程）

为什么不用 batch_convert_docx_to_md.py 的 ProcessPool：本机沙箱不允许创建进程池信号量
（PermissionError: [Errno 13] Permission denied / SemLock），所以这里改成单进程顺序跑，
复用它的 convert_one()，产物路径与它一致（output_docx_to_md/<类型>/<学科>/<卷>/<卷>.md）。

用法：
  python3 convert_md_chem.py --type 一模 --year-from 2021 --grade 高三
  python3 convert_md_chem.py --type 真题 --year-from 2011 --must 北京
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_convert_docx_to_md as conv  # noqa: E402
from paths import GAOZHONG, OUTPUT_DOCX_TO_MD  # noqa: E402

SUBJECT = "化学"


def norm_stem(name: str) -> str:
    return re.sub(r"（教师版）|\(教师版\)|含答案|教师版", "", name).strip()


def pick(args):
    chosen: dict[tuple[str, str], Path] = {}
    for t in args.type:
        src = Path(GAOZHONG) / t / SUBJECT
        if not src.is_dir():
            print(f"⚠ 没有目录：{src}")
            continue
        for f in sorted(list(src.glob("*.docx")) + list(src.glob("*.doc"))):
            m = re.search(r"(20\d{2})", f.stem)
            if not m:
                continue
            y = int(m.group(1))
            if not (args.year_from <= y <= args.year_to):
                continue
            if args.grade and args.grade not in f.stem:
                continue
            if args.must and args.must not in f.stem:
                continue
            stem = norm_stem(f.stem)
            key = (t, stem)
            # 同一卷有多个 docx 时优先「教师版」（答案在里面）
            if key not in chosen or "教师版" in f.stem:
                chosen[key] = f
    return sorted(chosen.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", action="append", required=True)
    ap.add_argument("--year-from", type=int, default=2011)
    ap.add_argument("--year-to", type=int, default=2030)
    ap.add_argument("--grade", default=None)
    ap.add_argument("--must", default=None)
    args = ap.parse_args()

    items = pick(args)
    print(f"待转换 {len(items)} 卷（{args.type}，{args.year_from}-{args.year_to}"
          f"{f'，{args.grade}' if args.grade else ''}{f'，含 {args.must}' if args.must else ''}）", flush=True)

    ok = skip = fail = 0
    fails = []
    t0 = time.time()
    for i, ((t, stem), docx) in enumerate(items, 1):
        rel = str(docx.relative_to(Path(GAOZHONG)))
        try:
            rel_path, success, msg = conv.convert_one((rel, str(docx), str(OUTPUT_DOCX_TO_MD), False))
        except Exception as e:  # 单个失败不影响整批
            ok_, rel_path, success, msg = False, rel, False, str(e)[:160]
        if success and msg and "跳过" in str(msg):
            skip += 1
            print(f"[{i}/{len(items)}] — {t} / {stem}（已存在）", flush=True)
        elif success:
            ok += 1
            print(f"[{i}/{len(items)}] ✓ {t} / {stem}", flush=True)
        else:
            fail += 1
            fails.append(f"{t}/{stem}: {msg}")
            print(f"[{i}/{len(items)}] ✗ {t} / {stem}：{msg}", flush=True)

    print(f"\n完成：成功 {ok} / 跳过 {skip} / 失败 {fail}，用时 {int(time.time()-t0)}s", flush=True)
    for f in fails[:30]:
        print("  -", f, flush=True)


if __name__ == "__main__":
    main()
