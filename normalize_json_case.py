#!/usr/bin/env python3
"""把存量题库 JSON 里的「选择题答案 + 选项 id」统一迁移成大写字母。

背景：题库约定是选择题 ``answer`` 与 ``options[].id`` 一律大写（A/B/C/D…），
前端按这个大小写匹配才能把正确选项标绿。早期各学科链路的出口有的把选项 id
写成小写（数学/物理/化学/语文/政治/图片版），英语还把小写答案也落盘了。

本工具是**存量数据**的迁移入口（新数据由各链路出口的 ``src/answer_case.py`` 保证）：

    python3 normalize_json_case.py                 # 默认 dry-run，只统计不改盘
    python3 normalize_json_case.py --apply         # 先备份再原地改写
    python3 normalize_json_case.py --only output_json_math --examples 5
    python3 normalize_json_case.py --dir /path/to/data_root

- 扫描范围：``DATA_ROOT`` 下所有 ``output_json*`` 目录（含嵌套中文目录）
  ＋ ``output_image_questions``（图片版 paper.json / qN.json），递归。
- ``--apply`` 会先把原件备份到 ``DATA_ROOT/_backup_json_case_<时间戳>/``（保持相对路径），
  再原地改写；重复跑第二遍改动为 0（幂等）。
- 只动「带非空 options 且每个 option id 都是单个字母」的题；语法填空 / 选词填空 /
  阅读表达 / 作文等单词答案原样保留。规则细节见 ``src/answer_case.py``。
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from paths import DATA_ROOT  # noqa: E402
from src.answer_case import (  # noqa: E402
    iter_choice_questions,
    normalize_choice_case_in_paper,
)

DEFAULT_DIR_GLOB = "output_json*"
EXTRA_DIRS = ("output_image_questions",)


def detect_indent(text: str) -> int:
    """猜原始文件的缩进宽度（题库里常见 indent=1 或 2）。"""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        n = len(line) - len(stripped)
        return n if n in (1, 2, 4, 8) else 2
    return 2


def collect_dirs(root: Path, only: str = None) -> list:
    """DATA_ROOT 下的 output_json* + output_image_questions。"""
    dirs = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if not (p.match(DEFAULT_DIR_GLOB) or p.name in EXTRA_DIRS):
            continue
        if only and only not in (p.name, str(p)):
            continue
        dirs.append(p)
    return dirs


def describe_change(before: dict, after: dict) -> str:
    """生成「选项 id a,b,c → A,B,C；答案 'c' → 'C'」这类短文。"""
    bits = []
    b_ids = [str(o.get("id")) for o in (before.get("options") or []) if isinstance(o, dict)]
    a_ids = [str(o.get("id")) for o in (after.get("options") or []) if isinstance(o, dict)]
    if b_ids != a_ids:
        bits.append(f"选项 id {','.join(b_ids)} → {','.join(a_ids)}")
    b_ans, a_ans = before.get("answer"), after.get("answer")
    if b_ans != a_ans:
        bits.append(f"答案 {b_ans!r} → {a_ans!r}")
    return "；".join(bits)


def process_file(path: Path, root: Path, n_examples: int) -> dict:
    """在内存里处理一个 JSON 文件，返回统计（不写盘；改写由调用方先备份再落盘）。"""
    rel = path.relative_to(root)
    stat = {"file": rel, "changed": 0, "warnings": [], "examples": [], "error": None,
            "new_text": None}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        stat["error"] = f"{type(exc).__name__}: {exc}"
        return stat

    before = copy.deepcopy(data) if n_examples else None
    changed, warnings = normalize_choice_case_in_paper(data)
    stat["changed"] = changed
    stat["warnings"] = warnings

    if changed and before is not None:
        pairs = zip(iter_choice_questions(before), iter_choice_questions(data))
        for (bpath, bq), (_apath, aq) in pairs:
            if bq != aq:
                desc = describe_change(bq, aq)
                stat["examples"].append(f"{rel}：{bpath} {desc}")
                if len(stat["examples"]) >= n_examples:
                    break

    if changed:
        indent = detect_indent(text)
        trailing_nl = "\n" if text.endswith("\n") else ""
        stat["new_text"] = json.dumps(data, ensure_ascii=False, indent=indent) + trailing_nl
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(
        description="存量题库 JSON：选择题答案与选项 id 统一大写（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正改写（先备份到 _backup_json_case_<时间戳>/）")
    ap.add_argument("--dry-run", action="store_true", help="只统计（默认行为，可省略）")
    ap.add_argument("--dir", default=str(DATA_ROOT), help=f"数据根（默认 {DATA_ROOT}）")
    ap.add_argument("--only", default=None, help="只处理某个输出目录名，如 output_json_math")
    ap.add_argument("--examples", type=int, default=3, help="每个目录打印几条改动示例（0=不收集）")
    ap.add_argument("--warnings", type=int, default=5, help="每个目录最多打印几条告警")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        ap.error("--apply 与 --dry-run 不能同时使用")

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"✗ 数据根不存在：{root}")
        return 2

    dirs = collect_dirs(root, args.only)
    if not dirs:
        print(f"✗ 在 {root} 下没找到 output_json* 目录")
        return 2

    mode = "APPLY（先备份再改写）" if args.apply else "DRY-RUN（只统计，不改盘）"
    print(f"数据根：{root}")
    print(f"模式  ：{mode}")
    print(f"目录  ：{', '.join(p.name for p in dirs)}\n")

    backup_root = None
    if args.apply:
        backup_root = root / f"_backup_json_case_{time.strftime('%Y%m%d-%H%M%S')}"
        print(f"备份到：{backup_root}\n")

    grand_files = grand_changed_files = grand_q = grand_warn = 0
    grand_errors = 0

    for d in dirs:
        files = sorted(p for p in d.rglob("*.json") if p.is_file())
        n_files = n_changed_files = n_q = n_warn = 0
        errors = []
        examples = []
        warn_samples = []
        for path in files:
            n_files += 1
            st = process_file(path, root, args.examples)
            if st["error"]:
                errors.append(f"{st['file']}: {st['error']}")
                continue
            if st["changed"]:
                n_changed_files += 1
                n_q += st["changed"]
                for e in st["examples"]:
                    if len(examples) < args.examples:
                        examples.append(e)
                if args.apply:
                    dst = backup_root / st["file"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dst)  # 先备份原件，再改写
                    path.write_text(st["new_text"], encoding="utf-8")
            n_warn += len(st["warnings"])
            for w in st["warnings"]:
                if len(warn_samples) < args.warnings:
                    warn_samples.append(f"{st['file']}: {w}")

        print(f"=== {d.name}")
        print(f"  文件数：{n_files}（JSON 全量递归）")
        print(f"  待改文件：{n_changed_files} / 待改题数：{n_q}")
        print(f"  告警：{n_warn}")
        if examples:
            print("  示例：")
            for e in examples:
                print(f"    - {e}")
        if warn_samples:
            print("  告警示例：")
            for w in warn_samples:
                print(f"    - {w}")
        if errors:
            print(f"  ✗ 读取失败 {len(errors)} 个：")
            for e in errors[:5]:
                print(f"    - {e}")
        print()

        grand_files += n_files
        grand_changed_files += n_changed_files
        grand_q += n_q
        grand_warn += n_warn
        grand_errors += len(errors)

    print("=" * 56)
    print(f"合计：文件 {grand_files}，待改文件 {grand_changed_files}，待改题数 {grand_q}，告警 {grand_warn}，读取失败 {grand_errors}")
    if args.apply:
        print(f"已备份到：{backup_root}")
        print("建议复跑一次 --dry-run 确认「待改题数」为 0（幂等验证）")
    else:
        print("未改盘。确认无误后加 --apply 执行（会先备份）。")
    return 1 if grand_errors else 0


if __name__ == "__main__":
    sys.exit(main())
