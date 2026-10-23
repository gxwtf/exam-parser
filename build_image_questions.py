#!/usr/bin/env python3
"""图片版化学题：给每题 JSON 补上题型/答案/选项（选择题 4 个空选项），并生成整卷 JSON。

数据来源：
- <单题目录>/manifest.json（split_chem_docx.py 生成）
- 答案：优先复用已有解析 JSON（选择题），非选择题从 MD 答案区解析
用法：python3 build_image_questions.py -d <单题目录> -T 二模
"""
import argparse, json, re
from pathlib import Path
from PIL import Image

from convert_chemistry_full import parse_essay_answers, QSPLIT
from convert_chinese_md import split_paper

PROJ = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--dir", required=True)
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("--md", default=None, help="原卷 MD（取非选择题答案）")
    ap.add_argument("--choice-json", default=None, help="已有选择题解析 JSON（取选择题答案）")
    args = ap.parse_args()

    qdir = Path(args.dir)
    manifest = json.loads((qdir / "manifest.json").read_text(encoding="utf-8"))
    stem = qdir.name

    choice_answers: dict[int, str] = {}
    if args.choice_json and Path(args.choice_json).exists():
        d = json.loads(Path(args.choice_json).read_text(encoding="utf-8"))
        for sec in d.get("sections", []):
            for q in sec.get("questions", []):
                try:
                    choice_answers[int(q["id"])] = str(q.get("answer") or "").strip()
                except Exception:
                    continue

    essay_answers: dict[int, str] = {}
    if args.md and Path(args.md).exists():
        body, ans = split_paper(Path(args.md).read_text(encoding="utf-8"))
        essay_answers = parse_essay_answers(ans)

    questions = []
    for no_str in sorted(manifest, key=lambda x: int(x)):
        no = int(no_str)
        info = manifest[no_str]
        is_choice = no in choice_answers or no <= 14
        q = {
            "no": no,
            "type": "single" if is_choice else "text",
            "image": info["image"],
            "options": [{"id": k, "label": k.upper()} for k in "abcd"] if is_choice else [],
            "answer": choice_answers.get(no, "") if is_choice else essay_answers.get(no, ""),
            "score": 3 if is_choice else None,
        }
        img_path = qdir / f"q{no}.png"
        try:
            wpx = Image.open(img_path).size[0]
        except Exception:
            wpx = None
        q["px"] = wpx
        q["content"] = f'<QImg src="{q["image"]}" px="{wpx}" />' if wpx else f'![]({q["image"]})'
        questions.append(q)
        (qdir / f"q{no}.json").write_text(json.dumps({
            "paper": stem, "subject": "化学", "no": no, "type": q["type"],
            "image": q["image"], "answer": q["answer"], "score": q["score"],
            "options": q["options"], "px": q.get("px"), "content": q["content"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    paper = {
        "title": f"{stem}（图片版）", "subject": "化学", "source": stem, "grade": "高三",
        "year": int(re.search(r"(20\d{2})", stem).group(1)) if re.search(r"(20\d{2})", stem) else None,
        "tags": ["化学", "高三", "二模", "图片题"],
        "questions": questions,
    }
    out = qdir / "paper.json"
    out.write_text(json.dumps(paper, ensure_ascii=False, indent=1), encoding="utf-8")
    n_choice = sum(1 for q in questions if q["type"] == "single")
    print(f"生成 {out.name}：{len(questions)} 题（选择 {n_choice}，非选择 {len(questions)-n_choice}），缺答案 {sum(1 for q in questions if not q['answer'])}")


if __name__ == "__main__":
    main()
