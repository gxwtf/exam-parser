#!/usr/bin/env python3
"""
MinerU 批量转换试卷为 Markdown
用法:
    python3 batch_convert_md.py                          # 默认转换 gaozhong 下所有 PDF
    python3 batch_convert_md.py --dir ./gaozhong          # 指定输入目录
    python3 batch_convert_md.py -p ./xxx.pdf               # 转换单个 PDF 文件
    python3 batch_convert_md.py -o ./output_md            # 指定输出目录
    python3 batch_convert_md.py -s 语文 数学               # 只转换指定学科
    python3 batch_convert_md.py -T 上期末 一模             # 只转换指定类型
    python3 batch_convert_md.py -T 上期末 -s 语文          # 上期末的语文试卷
    python3 batch_convert_md.py -w 4                      # 4 进程并行
    python3 batch_convert_md.py --force                   # 强制重新转换
    python3 batch_convert_md.py -m ocr                    # 使用 OCR 模式
    python3 batch_convert_md.py -b pipeline               # 使用 pipeline 后端（纯 CPU）
    python3 batch_convert_md.py --no-formula              # 关闭公式识别
    python3 batch_convert_md.py --no-table                # 关闭表格识别
    python3 batch_convert_md.py --dry-run                 # 仅列出文件，不转换
"""

import os
import sys
import re
import time
import shutil
import argparse
import subprocess
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from paths import GAOZHONG, OUTPUT_PDF_TO_MD

EXTENSIONS = {".pdf"}

SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]

TYPES = {
    "上期末": "上期末",
    "下期末": "下期末",
    "一模": "一模",
    "二模": "二模",
    "真题": "真题",
}

# 区名正则（用于跳过非城区试卷，可选）
DISTRICTS = [
    "海淀", "西城", "东城", "朝阳", "丰台", "石景山",
    "通州", "大兴", "昌平", "顺义", "房山", "门头沟",
    "平谷", "怀柔", "密云", "延庆",
]
_DISTRICT_NAMES = "|".join(DISTRICTS)
DISTRICT_RE = re.compile(rf"(?:{_DISTRICT_NAMES})区?(?:初|高)[一二三]")


def check_mineru():
    """检查 mineru 是否可用"""
    if shutil.which("mineru"):
        return True
    # 尝试 magic-pdf 旧命令
    if shutil.which("magic-pdf"):
        return True
    return False


def get_mineru_cmd():
    """获取 mineru 命令名"""
    if shutil.which("mineru"):
        return "mineru"
    return "magic-pdf"


def collect_files(input_dir):
    """递归收集所有 PDF/DOCX 文件，返回 (相对路径, 绝对路径) 列表"""
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


def output_exists(rel_path, output_dir, method):
    """检查输出是否已存在（保持输入目录结构）"""
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    rel_dir = os.path.dirname(rel_path)
    out_folder = os.path.join(output_dir, rel_dir, stem)
    md_file = os.path.join(out_folder, method, f"{stem}.md")
    return os.path.isfile(md_file)


def convert_one(args_tuple):
    """转换单个文件（在子进程中运行）"""
    rel_path, abs_path, output_dir, cmd_name, method, backend, formula, table, lang, force = args_tuple

    stem = os.path.splitext(os.path.basename(rel_path))[0]
    rel_dir = os.path.dirname(rel_path)
    # 保持输入目录结构: output_dir/{rel_dir}/{stem}/{method}/{stem}.md
    file_output_dir = os.path.join(output_dir, rel_dir)
    out_folder = os.path.join(file_output_dir, stem)
    md_file = os.path.join(out_folder, method, f"{stem}.md")

    # 跳过已存在
    if not force and os.path.isfile(md_file):
        return (rel_path, True, "已存在，跳过")

    os.makedirs(file_output_dir, exist_ok=True)

    cmd = [
        cmd_name,
        "-p", abs_path,
        "-o", file_output_dir,
        "-m", method,
        "-b", backend,
    ]
    if formula is not None:
        cmd.extend(["-f", str(formula).lower()])
    if table is not None:
        cmd.extend(["-t", str(table).lower()])
    if lang:
        cmd.extend(["-l", lang])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时
        )
        if result.returncode == 0 and os.path.isfile(md_file):
            return (rel_path, True, None)
        else:
            error = result.stderr.strip()[-300:] if result.stderr else f"返回码 {result.returncode}"
            return (rel_path, False, error)
    except subprocess.TimeoutExpired:
        return (rel_path, False, "超时（>10分钟）")
    except Exception as e:
        return (rel_path, False, str(e))


class ProgressBar:
    """终端进度条"""

    def __init__(self, total, desc="进度"):
        self.total = total
        self.current = 0
        self.desc = desc
        self.lock = threading.Lock()
        self.current_item = ""
        self.finished = False
        self._need_redraw = True

    def update(self, n=1, item=""):
        with self.lock:
            self.current += n
            if item:
                self.current_item = item
            self._render()

    def error(self, item, error_msg):
        """实时输出失败信息"""
        with self.lock:
            # 先清掉进度条
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            # 输出错误信息
            sys.stdout.write(f"\n  ✗ {item}: {error_msg}\n")
            sys.stdout.flush()
            self._need_redraw = True

    def _render(self):
        if self.finished:
            return
        bar_width = 30
        if self.total > 0:
            pct = self.current / self.total
            filled = int(bar_width * pct)
            bar = "█" * filled + "░" * (bar_width - filled)
        else:
            pct = 0
            bar = "░" * bar_width

        line = f"\r\033[K{self.desc} |{bar}| {self.current}/{self.total} ({pct*100:.1f}%)"
        if self.current_item:
            line += f"  {self.current_item}"
        sys.stdout.write(line)
        sys.stdout.flush()

    def close(self):
        self.finished = True
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(
        description="MinerU 批量转换试卷为 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 batch_convert_md.py                           # 默认转换 gaozhong 目录
  python3 batch_convert_md.py -s 语文 数学               # 只转换语文和数学
  python3 batch_convert_md.py -T 上期末 一模             # 只转换上期末和一模
  python3 batch_convert_md.py -T 上期末 -s 语文          # 上期末的语文试卷
  python3 batch_convert_md.py -w 4 -m ocr               # 4进程 OCR 模式
  python3 batch_convert_md.py --no-formula               # 关闭公式识别
  python3 batch_convert_md.py --force                    # 强制重新转换
  python3 batch_convert_md.py --dry-run                  # 仅列出待转换文件
        """,
    )
    parser.add_argument("--dir", default=None, help="输入目录（默认 $EXAM_DATA_ROOT/gaozhong）")
    parser.add_argument("-p", "--path", default=None, help="单文件模式：指定单个 PDF 文件路径")
    parser.add_argument("-o", "--output", default=None, help="输出目录（默认输入目录旁的 output_md）")
    parser.add_argument("-w", "--workers", type=int, default=1, help="并行进程数（默认 1）")
    parser.add_argument("-m", "--method", default="auto", choices=["auto", "txt", "ocr"],
                        help="解析方式: auto=自动, txt=文本型PDF, ocr=扫描型PDF（默认 auto）")
    parser.add_argument("-b", "--backend", default="hybrid-engine",
                        choices=["pipeline", "vlm-engine", "hybrid-engine",
                                 "vlm-http-client", "hybrid-http-client"],
                        help="解析后端（默认 hybrid-engine，纯CPU用 pipeline）")
    parser.add_argument("--formula", dest="formula", default=True,
                        action="store_true", help="启用公式识别（默认）")
    parser.add_argument("--no-formula", dest="formula", action="store_false",
                        help="关闭公式识别")
    parser.add_argument("--table", dest="table", default=True,
                        action="store_true", help="启用表格识别（默认）")
    parser.add_argument("--no-table", dest="table", action="store_false",
                        help="关闭表格识别")
    parser.add_argument("-l", "--lang", default="ch", help="文档语言（默认 ch，支持中英文）")
    parser.add_argument("-s", "--subjects", nargs="*", default=SUBJECTS,
                        help=f"要转换的学科（默认全部: {' '.join(SUBJECTS)}）")
    parser.add_argument("-T", "--types", nargs="*", default=list(TYPES.keys()),
                        help=f"要转换的类型（默认全部: {' '.join(TYPES.keys())}）")
    parser.add_argument("--force", action="store_true", help="强制重新转换已存在的文件")
    parser.add_argument("--dry-run", action="store_true", help="仅列出待转换文件，不实际转换")
    args = parser.parse_args()

    if not check_mineru():
        print("错误: 未找到 mineru 命令")
        print("  请先安装: pip install mineru")
        sys.exit(1)

    cmd_name = get_mineru_cmd()
    print(f"使用命令: {cmd_name}")

    # ---- 单文件模式 ----
    if args.path:
        single_path = os.path.abspath(args.path)
        if not os.path.isfile(single_path):
            print(f"错误: 文件不存在 - {single_path}")
            sys.exit(1)
        output_dir = args.output if args.output else os.path.join(
            os.path.dirname(single_path), "output_md"
        )
        print(f"单文件: {single_path}")
        print(f"输出目录: {output_dir}")
        print(f"解析方式: {args.method}")
        print(f"解析后端: {args.backend}")
        print()

        stem = os.path.splitext(os.path.basename(single_path))[0]
        md_file = os.path.join(output_dir, stem, args.method, f"{stem}.md")
        if not args.force and os.path.isfile(md_file):
            print(f"已转换，跳过: {md_file}")
            return

        rel_path, success, error = convert_one((
            os.path.basename(single_path), single_path, output_dir,
            cmd_name, args.method, args.backend,
            args.formula, args.table, args.lang, args.force,
        ))
        if success:
            print(f"转换成功: {md_file}")
        else:
            print(f"转换失败: {error}")
            sys.exit(1)
        return

    # ---- 批量模式 ----
    if args.dir:
        input_dir = os.path.abspath(args.dir)
    else:
        input_dir = str(GAOZHONG)

    if not os.path.isdir(input_dir):
        print(f"错误: 输入目录不存在 - {input_dir}")
        sys.exit(1)

    if args.output:
        output_dir = os.path.abspath(args.output)
    else:
        output_dir = str(OUTPUT_PDF_TO_MD)

    # 构建 type 映射：支持 key 也支持 value 来匹配
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
    print(f"类型: {' '.join(all_types.keys())}")
    print(f"学科: {' '.join(subjects)}")
    print(f"解析方式: {args.method}")
    print(f"解析后端: {args.backend}")
    print(f"公式识别: {'开启' if args.formula else '关闭'}")
    print(f"表格识别: {'开启' if args.table else '关闭'}")
    print(f"文档语言: {args.lang}")
    print(f"并行数: {args.workers}")
    print()

    # 收集文件并按类型/学科过滤
    all_files = collect_files(input_dir)
    files = []
    for rel_path, abs_path in all_files:
        parts = rel_path.replace("\\", "/").split("/")
        if len(parts) < 3:
            continue
        file_type = parts[0]   # 第一级目录：上期末/下期末/一模/二模/真题
        file_subject = parts[1]  # 第二级目录：语文/数学/...
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

    # 过滤已存在的
    pending = []
    skipped = 0
    for rel_path, abs_path in files:
        if not args.force and output_exists(rel_path, output_dir, args.method):
            skipped += 1
        else:
            pending.append((rel_path, abs_path))

    if skipped:
        print(f"跳过已转换: {skipped} 个")
    print(f"待转换: {len(pending)} 个\n")

    if not pending:
        print("所有文件已转换完毕。")
        return

    # 构建参数列表
    task_args = [
        (rel, abs_path, output_dir, cmd_name, args.method, args.backend,
         args.formula, args.table, args.lang, args.force)
        for rel, abs_path in pending
    ]

    progress = ProgressBar(len(pending), desc="转换进度")
    failed_list = []
    failed_lock = threading.Lock()
    completed = 0

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(convert_one, ta): ta[0] for ta in task_args}

        for future in as_completed(futures):
            rel_path = futures[future]
            try:
                rel_path, success, error = future.result()
            except Exception as e:
                success, error = False, str(e)

            if not success:
                with failed_lock:
                    failed_list.append((rel_path, error))
                progress.error(os.path.basename(rel_path), error)

            completed += 1
            progress.update(item=os.path.basename(rel_path))

    progress.close()

    success_count = len(pending) - len(failed_list)
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


if __name__ == "__main__":
    main()