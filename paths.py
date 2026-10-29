#!/usr/bin/env python3
"""统一数据根（所有 output_* / 素材目录的落脚点）。

历史行为：脚本假定自己位于 `<数据根>/exam-parser/`，于是用
`Path(__file__).parent.parent` 拼出 `output_json_math`、`output_docx_to_md` 等目录。
这导致仓库一挪位置（例如从 ~/jingshibang/exam-parser 换到别处）所有默认路径就错。

现在统一走这里：
- 优先读环境变量 `EXAM_DATA_ROOT`（也可以写进仓库根的 .env）；
- 未设置时**退回历史行为** = 仓库的上级目录，所以本机现状（数据根
  /home/gxwtf/jingshibang）行为完全不变。

用法：
    from paths import DATA_ROOT, GAOZHONG, OUTPUT_JSON_MATH

    EXAM_DATA_ROOT=/data/exam python3 convert_math_pdf.py ...
"""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

# 数据根：默认沿用「仓库的上级目录」
DATA_ROOT = Path(
    os.getenv("EXAM_DATA_ROOT") or REPO_ROOT.parent
).expanduser().resolve()

# 素材（原始试卷）：<数据根>/gaozhong/<类型>/<学科>
GAOZHONG = DATA_ROOT / "gaozhong"

# 各学科 / 各链路的输出根
OUTPUT_DOCX_TO_MD = DATA_ROOT / "output_docx_to_md"          # docx→MD（含 images/，nginx /exam-images 直出）
OUTPUT_PDF_TO_MD = DATA_ROOT / "output_pdf_to_md"            # MinerU PDF→MD（历史存量）
OUTPUT_JSON = DATA_ROOT / "output_json"                      # 英语（main.py / batch_convert_md_to_json.py）
OUTPUT_JSON_MATH = DATA_ROOT / "output_json_math"            # 数学 / 物理 / 化学（选择题，同一套导入结构）
OUTPUT_JSON_CHINESE = DATA_ROOT / "output_json_chinese"      # 语文
OUTPUT_JSON_POLITICS = DATA_ROOT / "output_json_politics"    # 政治
OUTPUT_JSON_CHEMISTRY_FULL = DATA_ROOT / "output_json_chemistry_full"  # 化学整卷（选择+非选择）
OUTPUT_JSON_BIO = DATA_ROOT / "output_json_bio"              # 生物整卷（选择+非选择，一题一个 section）
OUTPUT_IMAGE_QUESTIONS = DATA_ROOT / "output_image_questions"          # 图片版（按题裁图）

__all__ = [
    "REPO_ROOT",
    "DATA_ROOT",
    "GAOZHONG",
    "OUTPUT_DOCX_TO_MD",
    "OUTPUT_PDF_TO_MD",
    "OUTPUT_JSON",
    "OUTPUT_JSON_MATH",
    "OUTPUT_JSON_CHINESE",
    "OUTPUT_JSON_POLITICS",
    "OUTPUT_JSON_CHEMISTRY_FULL",
    "OUTPUT_JSON_BIO",
    "OUTPUT_IMAGE_QUESTIONS",
]
