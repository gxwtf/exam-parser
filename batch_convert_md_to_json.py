#!/usr/bin/env python3
"""
批量将 MD 试卷解析为结构化 JSON
用法:
    python3 batch_convert_md_to_json.py                          # 默认转换 output_docx_to_md 下所有 MD
    python3 batch_convert_md_to_json.py --dir ./output_docx_to_md # 指定输入目录
    python3 batch_convert_md_to_json.py -p ./xxx.md              # 转换单个 MD 文件
    python3 batch_convert_md_to_json.py -o ./output_json         # 指定输出目录
    python3 batch_convert_md_to_json.py -s 语文 数学              # 只转换指定学科
    python3 batch_convert_md_to_json.py -T 上期末 一模            # 只转换指定类型
    python3 batch_convert_md_to_json.py -T 上期末 -s 语文         # 上期末的语文试卷
    python3 batch_convert_md_to_json.py --force                  # 强制重新转换
    python3 batch_convert_md_to_json.py --dry-run                # 仅列出文件，不转换
"""

import os
import sys
import json
import time
import argparse
import logging
from pathlib import Path
from dotenv import load_dotenv

from config import (
    PROJECT_ROOT, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, LOG_LEVEL, LOG_FILE
)
from src.parser import ExamParser

EXTENSIONS = {".md"}

SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]

TYPES = {
    "上期末": "上期末",
    "下期末": "下期末",
    "一模": "一模",
    "二模": "二模",
    "真题": "真题",
}

logger = logging.getLogger(__name__)


def clean_stem(name):
    """去掉文件名中的 （教师版）(教师版) 等后缀"""
    name = name.replace("（教师版）", "").replace("(教师版)", "").strip()
    return name


def setup_logging(debug=False):
    level = logging.DEBUG if debug else getattr(logging, LOG_LEVEL)
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )


def collect_files(input_dir):
    """递归收集所有 MD 文件，返回 (相对路径, 绝对路径) 列表"""
    files = []
    input_dir = os.path.abspath(input_dir)
    for dirpath, dirnames, filenames in os.walk(input_dir):
        for f in sorted(filenames):
            ext = os.path.splitext(f)[1].lower()
            if ext in EXTENSIONS:
                abs_path = os.path.join(dirpath, f)
                rel_path = os.path.relpath(abs_path, input_dir)
                files.append((rel_path, abs_path))
    return files


def output_exists(rel_path, output_dir):
    """检查 JSON 输出是否已存在"""
    stem = clean_stem(os.path.splitext(os.path.basename(rel_path))[0])
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) >= 3:
        rel_dir = os.path.join(parts[0], parts[1])
    else:
        rel_dir = os.path.dirname(rel_path)
    json_file = os.path.join(output_dir, rel_dir, f"{stem}.json")
    return os.path.isfile(json_file)


def convert_one(rel_path, abs_path, output_dir, parser, force):
    """转换单个 MD 文件为 JSON，返回 (rel_path, success, error, validation_errors)"""
    raw_stem = os.path.splitext(os.path.basename(rel_path))[0]
    stem = clean_stem(raw_stem)
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) >= 3:
        rel_dir = os.path.join(parts[0], parts[1])
    else:
        rel_dir = os.path.dirname(rel_path)

    json_file = os.path.join(output_dir, rel_dir, f"{stem}.json")

    if not force and os.path.isfile(json_file):
        return (rel_path, True, "已存在，跳过", [])

    os.makedirs(os.path.dirname(json_file), exist_ok=True)

    try:
        md_dir = os.path.dirname(abs_path)
        result = parser.parse_from_file(raw_stem, Path(md_dir))

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        validation_errors = parser._last_validation_errors
        return (rel_path, True, None, validation_errors)
    except Exception as e:
        return (rel_path, False, str(e), [])


def main():
    parser = argparse.ArgumentParser(
        description="批量将 MD 试卷解析为结构化 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 batch_convert_md_to_json.py                           # 默认转换 output_docx_to_md 目录
  python3 batch_convert_md_to_json.py -s 语文 数学               # 只转换语文和数学
  python3 batch_convert_md_to_json.py -T 上期末 一模             # 只转换上期末和一模
  python3 batch_convert_md_to_json.py -T 上期末 -s 语文          # 上期末的语文试卷
  python3 batch_convert_md_to_json.py --force                   # 强制重新转换
  python3 batch_convert_md_to_json.py --dry-run                 # 仅列出待转换文件
        """,
    )
    parser.add_argument("--dir", default=None, help="输入目录（默认上级目录下的 output_docx_to_md）")
    parser.add_argument("-p", "--path", default=None, help="单文件模式：指定单个 MD 文件路径")
    parser.add_argument("-o", "--output", default=None, help="输出目录（默认输入目录旁的 output_json）")
    parser.add_argument("-m", "--model", default=None, help=f"模型名称（默认: {OPENAI_MODEL}）")
    parser.add_argument("-k", "--api-key", default=None, help="API Key（默认从 .env 读取）")
    parser.add_argument("--base-url", default=None, help=f"API 地址（默认: {OPENAI_BASE_URL}）")
    parser.add_argument("-s", "--subjects", nargs="*", default=SUBJECTS,
                        help=f"要转换的学科（默认全部: {' '.join(SUBJECTS)}）")
    parser.add_argument("-T", "--types", nargs="*", default=list(TYPES.keys()),
                        help=f"要转换的类型（默认全部: {' '.join(TYPES.keys())}）")
    parser.add_argument("--force", action="store_true", help="强制重新转换已存在的文件")
    parser.add_argument("--dry-run", action="store_true", help="仅列出待转换文件，不实际转换")
    parser.add_argument("--debug", action="store_true", help="开启调试模式")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(debug=args.debug)

    api_key = args.api_key or os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
    base_url = args.base_url or os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL)
    model = args.model or os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    if not api_key:
        logger.error("OPENAI_API_KEY 未设置，请在 .env 中配置或用 -k 指定")
        sys.exit(1)

    # ---- 单文件模式 ----
    if args.path:
        single_path = os.path.abspath(args.path)
        if not os.path.isfile(single_path):
            print(f"错误: 文件不存在 - {single_path}")
            sys.exit(1)

        output_dir = args.output if args.output else os.path.join(
            os.path.dirname(single_path), "output_json"
        )
        stem = clean_stem(os.path.splitext(os.path.basename(single_path))[0])
        json_file = os.path.join(output_dir, f"{stem}.json")

        print(f"单文件: {single_path}")
        print(f"输出目录: {output_dir}")
        print(f"模型: {model}")
        print()

        if not args.force and os.path.isfile(json_file):
            print(f"已转换，跳过: {json_file}")
            return

        exam_parser = ExamParser(api_key, model, base_url)
        if not exam_parser.ai_client.test_connection():
            print("API 连接失败")
            sys.exit(1)

        md_dir = os.path.dirname(single_path)
        try:
            result = exam_parser.parse_from_file(stem, Path(md_dir))
            os.makedirs(output_dir, exist_ok=True)
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"转换成功: {json_file}")
        except Exception as e:
            print(f"转换失败: {e}")
            sys.exit(1)
        return

    # ---- 批量模式 ----
    if args.dir:
        input_dir = os.path.abspath(args.dir)
    else:
        input_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output_docx_to_md")

    if not os.path.isdir(input_dir):
        print(f"错误: 输入目录不存在 - {input_dir}")
        sys.exit(1)

    if args.output:
        output_dir = os.path.abspath(args.output)
    else:
        output_dir = os.path.join(os.path.dirname(input_dir), "output_json")

    all_types = {}
    for arg in args.types:
        if arg in TYPES:
            all_types[arg] = TYPES[arg]
        else:
            for k, v in TYPES.items():
                if v == arg:
                    all_types[k] = v
                    break
    subjects = [s for s in args.subjects if s in SUBJECTS]

    print(f"输入目录: {input_dir}")
    print(f"输出目录: {output_dir}")
    print(f"模型: {model}")
    print(f"类型: {' '.join(all_types.keys())}")
    print(f"学科: {' '.join(subjects)}")
    print()

    all_files = collect_files(input_dir)
    files = []
    for rel_path, abs_path in all_files:
        parts = rel_path.replace("\\", "/").split("/")
        if len(parts) < 3:
            continue
        file_type = parts[0]
        file_subject = parts[1]
        if file_type in all_types and file_subject in subjects:
            files.append((rel_path, abs_path))

    print(f"共找到 {len(files)} 个文件（总 {len(all_files)} 个）")

    if args.dry_run:
        print("\n待转换文件列表:")
        for rel_path, _ in files:
            print(f"  {rel_path}")
        return

    if not files:
        print("没有需要转换的文件。")
        return

    pending = []
    skipped = 0
    for rel_path, abs_path in files:
        if not args.force and output_exists(rel_path, output_dir):
            skipped += 1
        else:
            pending.append((rel_path, abs_path))

    if skipped:
        print(f"跳过已转换: {skipped} 个")
    print(f"待转换: {len(pending)} 个\n")

    if not pending:
        print("所有文件已转换完毕。")
        return

    exam_parser = ExamParser(api_key, model, base_url)
    logger.info("测试 API 连接...")
    if not exam_parser.ai_client.test_connection():
        logger.error("API 连接失败")
        sys.exit(1)
    print("API 连接成功\n")

    success_count = 0
    failed_list = []
    validation_issues = {}

    for i, (rel_path, abs_path) in enumerate(pending, 1):
        stem = clean_stem(os.path.splitext(os.path.basename(rel_path))[0])
        print(f"[{i}/{len(pending)}] {stem} ...", end=" ", flush=True)

        rel_path_result, success, error, val_errors = convert_one(
            rel_path, abs_path, output_dir, exam_parser, args.force
        )

        if success:
            if error:
                print(f"跳过")
            else:
                if val_errors:
                    error_count = sum(1 for e in val_errors if e.level == "error")
                    warn_count = sum(1 for e in val_errors if e.level == "warning")
                    print(f"OK ({error_count}E/{warn_count}W)")
                    validation_issues[rel_path] = val_errors
                else:
                    print("OK")
            success_count += 1
        else:
            print(f"失败: {error}")
            failed_list.append((rel_path, error))

        time.sleep(0.5)

    print(f"\n完成! 成功: {success_count}, 失败: {len(failed_list)}")

    if failed_list:
        failed_file = os.path.join(output_dir, "failed_conversions.txt")
        with open(failed_file, "w", encoding="utf-8") as f:
            f.write(f"转换失败记录 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n")
            f.write("=" * 60 + "\n\n")
            for name, err in failed_list:
                f.write(f"文件: {name}\n原因: {err}\n")
                f.write("-" * 40 + "\n")
        print(f"失败记录: {failed_file}")

    if validation_issues:
        issues_file = os.path.join(output_dir, "validation_issues.txt")
        with open(issues_file, "w", encoding="utf-8") as f:
            f.write(f"校验问题记录 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n")
            f.write("=" * 60 + "\n\n")
            for name, errors in validation_issues.items():
                f.write(f"文件: {name}\n")
                for e in errors:
                    tag = "ERROR" if e.level == "error" else "WARN"
                    f.write(f"  [{tag}] {e.code}: {e.message}\n")
                f.write("-" * 40 + "\n")
        print(f"校验问题: {issues_file}")


if __name__ == "__main__":
    main()