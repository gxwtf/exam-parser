#!/usr/bin/env python3
"""校验试卷 JSON 的结构合法性（不调用 AI）

用法（需用 exam-parser 的 venv）:
    venv/bin/python3 validate_json.py <a.json> [b.json ...]
    venv/bin/python3 validate_json.py output_json/真题/英语/*.json

退出码：0 全部通过；1 有错误或缺文件。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.validator import FinalJSONValidator  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    validator = FinalJSONValidator("")
    failed = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.is_file():
            print(f"✗ 文件不存在：{p}")
            failed += 1
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"✗ {p.name}: JSON 解析失败 — {e}")
            failed += 1
            continue
        valid, errors = validator.validate(data)
        errs = [e for e in errors if e.level == "error"]
        warns = [e for e in errors if e.level == "warning"]
        if not errs and not warns:
            print(f"✓ {p.name}: 通过")
            continue
        print(f"{'✓' if valid else '✗'} {p.name}: 错误 {len(errs)}，警告 {len(warns)}")
        for e in (errs + warns)[:10]:
            print(f"    [{e.level}] [{e.code}] {e.message}")
        if errs:
            failed += 1

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
