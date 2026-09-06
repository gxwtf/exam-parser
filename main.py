"""
英语试卷解析器
用法:
    python3 main.py                                    # 使用默认配置处理所有 MD 文件
    python3 main.py -i ./data/input -o ./data/output   # 指定输入输出目录
    python3 main.py -m deepseek-v4-flash               # 指定模型
    python3 main.py --force                            # 强制重新处理（覆盖已有输出）
    python3 main.py --debug                            # 开启调试模式
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
    PROJECT_ROOT, INPUT_DIR, OUTPUT_DIR, DEBUG_DIR,
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, LOG_LEVEL, LOG_FILE
)
from src.parser import ExamParser
from src.loader import normalize_paper_name

logger = logging.getLogger(__name__)


def setup_logging(debug=False):
    """Setup logging with optional debug level"""
    level = logging.DEBUG if debug else getattr(logging, LOG_LEVEL)
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )


def get_papers_to_process(input_dir: Path, output_dir: Path, force: bool = False):
    """
    Get list of papers to process

    Args:
        input_dir: Directory containing MD files
        output_dir: Directory for output JSON files
        force: If True, re-process all; if False, skip existing outputs

    Returns:
        List of base names to process
    """
    if not input_dir.exists():
        return []

    papers = set()
    for item in input_dir.rglob("*.md"):
        papers.add(normalize_paper_name(item.stem))

    if not force:
        # Skip papers that already have output
        existing = set()
        for item in output_dir.rglob("*.json"):
            existing.add(normalize_paper_name(item.stem))
        papers -= existing

    return sorted(papers)


def _print_validation_summary(result: dict, base_name: str):
    """Print a validation summary of the parsed result"""
    sections = result.get("sections", [])
    paper = result.get("paper", {})

    section_counts = {}
    for sec in sections:
        sec_type = sec.get("type", "未知")
        if sec_type not in section_counts:
            section_counts[sec_type] = []
        questions = sec.get("questions", [])
        section_counts[sec_type].append({
            "questionCount": len(questions),
            "score": sec.get("score", 0),
            "title": sec.get("title", ""),
        })

    total_questions = 0
    answered_questions = 0
    for sec in sections:
        for q in sec.get("questions", []):
            total_questions += 1
            if q.get("answer"):
                answered_questions += 1

    print(f"\n{'─'*60}")
    print(f"  校验报告: {base_name}")
    print(f"{'─'*60}")
    print(f"  年级: {paper.get('grade', '未知')}  |  年份: {paper.get('year', '未知')}  |  总分: {paper.get('totalScore', '未知')}")
    print(f"{'─'*60}")
    print(f"  {'题型':<10} {'题目数':<8} {'分值':<8} {'标题'}")
    print(f"  {'─'*50}")

    for sec_type, entries in section_counts.items():
        for entry in entries:
            qc = entry["questionCount"]
            score = entry["score"]
            title = entry["title"][:20] if entry["title"] else "-"
            print(f"  {sec_type:<10} {qc:<8} {score:<8} {title}")

    print(f"  {'─'*50}")
    print(f"  总题数: {total_questions}  |  已匹配答案: {answered_questions}/{total_questions}")
    print(f"{'─'*60}\n")


def run_validation(json_files: list, output_dir: Path):
    """Validate existing JSON files without calling AI"""
    from src.validator import FinalJSONValidator

    if not json_files:
        logger.warning("没有找到 JSON 文件")
        return

    existing = [f for f in json_files if f.exists()]
    missing = [f for f in json_files if not f.exists()]

    if missing:
        for f in missing:
            logger.warning(f"文件不存在: {f.name}")

    if not existing:
        logger.warning("没有可校验的 JSON 文件")
        return

    logger.info("=" * 60)
    logger.info(f"校验模式 — 共 {len(existing)} 个文件")
    logger.info("=" * 60)

    total_errors = 0
    total_warnings = 0

    for f in existing:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"✗ {f.name}: JSON 解析失败 — {e}")
            total_errors += 1
            continue

        validator = FinalJSONValidator("")
        is_valid, errors = validator.validate(data)

        error_count = sum(1 for e in errors if e.level == "error")
        warning_count = sum(1 for e in errors if e.level == "warning")
        total_errors += error_count
        total_warnings += warning_count

        status = "✓" if is_valid else "✗"
        logger.info(f"{status} {f.name}: 错误 {error_count}, 警告 {warning_count}")

        for e in errors:
            prefix = "✗" if e.level == "error" else "⚠"
            logger.info(f"   {prefix} [{e.code}] {e.message}")

    logger.info("=" * 60)
    logger.info(f"校验完成: {len(existing)} 文件, 错误 {total_errors}, 警告 {total_warnings}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="英语试卷解析器 — 将 MD 格式试卷转为结构化 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 main.py                              # 使用默认配置
  python3 main.py -i ./papers -o ./output      # 指定目录
  python3 main.py -m deepseek-v4-flash         # 指定模型
  python3 main.py --force --debug              # 强制重处理 + 调试
  python3 main.py -p "2019北京朝阳高三二模英语" "2016北京东城高三二模英语"  # 指定试卷
  python3 main.py --validate                                          # 校验已有 JSON（空跑）
  python3 main.py --validate -p "2019北京朝阳高三二模英语"              # 校验指定 JSON
        """
    )
    parser.add_argument("-i", "--input", default=str(INPUT_DIR),
                        help=f"输入目录（默认: {INPUT_DIR}）")
    parser.add_argument("-o", "--output", default=str(OUTPUT_DIR),
                        help=f"输出目录（默认: {OUTPUT_DIR}）")
    parser.add_argument("-m", "--model", default=None,
                        help=f"模型名称（默认: {OPENAI_MODEL}）")
    parser.add_argument("-k", "--api-key", default=None,
                        help="API Key（默认从 .env 读取）")
    parser.add_argument("--base-url", default=None,
                        help=f"API 地址（默认: {OPENAI_BASE_URL}）")
    parser.add_argument("--force", action="store_true",
                        help="强制重新处理已存在的文件")
    parser.add_argument("-p", "--papers", nargs="*", default=None,
                        help="指定要处理的试卷名（多个用空格分隔），指定后自动强制重处理")
    parser.add_argument("--validate", action="store_true",
                        help="校验模式：仅检查已有 JSON 文件合法性，不调用 AI")
    parser.add_argument("--debug", action="store_true",
                        help="开启调试模式")
    args = parser.parse_args()

    # Setup
    load_dotenv()
    setup_logging(debug=args.debug)

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Validate mode: skip AI, just check existing JSON files
    if args.validate:
        if args.papers:
            json_files = [output_dir / f"{normalize_paper_name(p)}.json" for p in args.papers]
        else:
            json_files = sorted(output_dir.rglob("*.json"))
        run_validation(json_files, output_dir)
        return

    api_key = args.api_key or os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
    base_url = args.base_url or os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL)
    model = args.model or os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    if not api_key:
        logger.error("OPENAI_API_KEY 未设置，请在 .env 中配置或用 -k 指定")
        sys.exit(1)

    # Determine mode
    if args.papers:
        force = True
        mode_info = f"指定试卷（共 {len(args.papers)} 份）"
    elif args.force:
        force = True
        mode_info = "强制重处理"
    else:
        force = False
        mode_info = None

    logger.info("=" * 80)
    logger.info("英语试卷解析器")
    logger.info(f"模型: {model}")
    logger.info(f"输入: {input_dir}")
    logger.info(f"输出: {output_dir}")
    if mode_info:
        logger.info(f"模式: {mode_info}")
    logger.info("=" * 80)

    # Initialize parser
    exam_parser = ExamParser(api_key, model, base_url)

    # Test connection
    logger.info("测试 API 连接...")
    if not exam_parser.ai_client.test_connection():
        logger.error("API 连接失败")
        sys.exit(1)

    # Collect papers
    if args.papers:
        papers = [normalize_paper_name(p) for p in args.papers]
    else:
        papers = get_papers_to_process(input_dir, output_dir, force=force)

    if not papers:
        if force:
            logger.warning("输入目录中没有找到 MD 文件")
        else:
            logger.warning("没有需要处理的文件（全部已处理，用 --force 或 -p 强制重处理）")
        return

    logger.info(f"共 {len(papers)} 份试卷待处理\n")

    # Process
    success_count = 0
    fail_count = 0
    validation_issues = {}
    interrupted = False

    try:
        for i, base_name in enumerate(papers, 1):
            try:
                logger.info(f"[{i}/{len(papers)}] 处理: {base_name}")
                logger.info("-" * 80)

                result = exam_parser.parse_from_file(base_name, input_dir)

                output_file = output_dir / f"{base_name}.json"
                exam_parser.save_result(result, output_file)

                _print_validation_summary(result, base_name)

                val_errors = exam_parser._last_validation_errors
                if val_errors:
                    validation_issues[base_name] = val_errors

                success_count += 1
                logger.info(f"✓ 完成: {base_name}\n")

            except Exception as e:
                fail_count += 1
                logger.error(f"✗ 失败: {base_name} — {e}", exc_info=args.debug)
                continue

        # Summary
        logger.info("=" * 80)
        logger.info(f"处理完成! 成功: {success_count}, 失败: {fail_count}")
        logger.info("=" * 80)
    except KeyboardInterrupt:
        interrupted = True
        logger.info("=" * 80)
        logger.info(f"用户中断! 已处理: {success_count}, 失败: {fail_count}")
        logger.info("=" * 80)
    finally:
        if validation_issues:
            issues_file = output_dir / "validation_issues.txt"
            with open(issues_file, "w", encoding="utf-8") as f:
                f.write(f"校验问题记录 ({time.strftime('%Y-%m-%d %H:%M:%S')})")
                if interrupted:
                    f.write(" [用户中断]")
                f.write("\n")
                f.write("=" * 60 + "\n\n")
                for name, errors in validation_issues.items():
                    f.write(f"文件: {name}\n")
                    for e in errors:
                        tag = "ERROR" if e.level == "error" else "WARN"
                        f.write(f"  [{tag}] {e.code}: {e.message}\n")
                    f.write("-" * 40 + "\n")
            logger.info(f"校验问题已保存: {issues_file}")

        if interrupted:
            sys.exit(1)


if __name__ == "__main__":
    main()