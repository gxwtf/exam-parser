#!/usr/bin/env python3
"""图片版化学题：给每题 JSON 补上题型/答案/选项（选择题 4 个空选项），并生成整卷 JSON。

数据来源：
- <单题目录>/manifest.json（split_chem_docx.py 生成）
- 答案：优先复用已有解析 JSON（选择题），非选择题从 MD 答案区解析
- 非选择题的题型与分值：chem_category.py 按原卷 MD 判（结构/原理/有机/流程/探究 + （N分））
用法：python3 build_image_questions.py -d <单题目录> -T 二模
"""
import argparse, json, re
from pathlib import Path
from PIL import Image

from convert_chemistry_full import parse_essay_answers, QSPLIT
from convert_chinese_md import split_paper
from chem_category import analyze_paper
from src.answer_case import normalize_choice_case_in_paper

PROJ = Path(__file__).parent



def declared_choice_count(body: str) -> int | None:
    """从 MD 正文里读「本部分共 N 题」「本题包括 N 小题」「选择题（共 N 小题）」里的选择题数。
    用它定选择题边界，比「答案表里最大的题号」可靠（答案常常是图片，抽不全就会把选择题当大题）。"""
    m = re.search(r"本题包括\s*(\d{1,2})\s*小?题", body)
    if m:
        return int(m.group(1))
    m = re.search(r"选择题[（(][^）)]{0,10}共\s*(\d{1,2})\s*小?题", body)
    if m:
        return int(m.group(1))
    heads = re.findall(r"本部分共\s*(\d{1,2})\s*题", body)
    if len(heads) >= 2:
        return int(heads[0])
    return None

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
    cat_info: dict[int, dict] = {}
    n_choice_declared: int | None = None
    if args.md and Path(args.md).exists():
        body, ans = split_paper(Path(args.md).read_text(encoding="utf-8"))
        essay_answers = parse_essay_answers(ans)
        n_choice_declared = declared_choice_count(body)
        try:
            # 非选择题题型 + 分值（结构/原理/有机/流程/探究；（N分）取不到时 chem_category 兜底 13）
            rep = analyze_paper(Path(args.md))
            cat_info = {int(k): v for k, v in rep["questions"].items()}
        except Exception as e:
            print(f"⚠ 题型分类失败（非选择题 category 留空）：{str(e)[:120]}")

    questions = []
    for no_str in sorted(manifest, key=lambda x: int(x)):
        no = int(no_str)
        info = manifest[no_str]
        # 选择题边界：有答案就以「答案里出现的最大题号」为准（老国标卷只有 7 道选择，
        # 第 8 题起是大题；以前写死 no<=14 会把大题当选择题、还留空答案），没答案才退回 14
        # 边界优先级：MD 里声明的选择题数 > 答案表里最大题号 > 14
        if n_choice_declared:
            is_choice = no <= n_choice_declared
        elif choice_answers:
            max_choice = max(choice_answers)
            is_choice = no in choice_answers or no <= max_choice
        else:
            is_choice = no <= 14
        ci = None if is_choice else cat_info.get(no)
        q = {
            "no": no,
            "type": "single" if is_choice else "text",
            "image": info["image"],
            "options": [{"id": k.upper(), "label": k.upper()} for k in "abcd"] if is_choice else [],
            "answer": choice_answers.get(no, "") if is_choice else essay_answers.get(no, ""),
            "score": 3 if is_choice else (ci["score"] if ci else None),
        }
        if ci:                                    # 非选择题才有题型
            q["category"] = ci["category"]
        img_path = qdir / f"q{no}.png"
        try:
            wpx = Image.open(img_path).size[0]
        except Exception:
            wpx = None
        q["px"] = wpx
        q["content"] = f'<QImg src="{q["image"]}" px="{wpx}" />' if wpx else f'![]({q["image"]})'
        questions.append(q)
        q_payload = {
            "paper": stem, "subject": "化学", "no": no, "type": q["type"],
            "image": q["image"], "answer": q["answer"], "score": q["score"],
            "options": q["options"], "px": q.get("px"), "content": q["content"],
        }
        if ci:
            q_payload["category"] = ci["category"]
        normalize_choice_case_in_paper(q_payload)  # 出口约定：选择题答案与选项 id 一律大写
        (qdir / f"q{no}.json").write_text(json.dumps(q_payload, ensure_ascii=False, indent=1), encoding="utf-8")

    paper = {
        "title": stem, "subject": "化学", "source": stem, "grade": "高三",
        "year": int(re.search(r"(20\d{2})", stem).group(1)) if re.search(r"(20\d{2})", stem) else None,
        "tags": ["化学", "高三", args.type, "图片题"],
        "questions": questions,
    }
    out = qdir / "paper.json"
    normalize_choice_case_in_paper(paper, log=print)  # 出口约定：选择题答案与选项 id 一律大写
    out.write_text(json.dumps(paper, ensure_ascii=False, indent=1), encoding="utf-8")
    n_choice = sum(1 for q in questions if q["type"] == "single")
    print(f"生成 {out.name}：{len(questions)} 题（选择 {n_choice}，非选择 {len(questions)-n_choice}），缺答案 {sum(1 for q in questions if not q['answer'])}")
    texts = [q for q in questions if q["type"] == "text"]
    if texts:
        dist = {}
        for q in texts:
            dist[q.get("category") or "未分类"] = dist.get(q.get("category") or "未分类", 0) + 1
        print("  非选择题题型：" + "  ".join(f"{k} {v}" for k, v in dist.items())
              + "｜分值 " + "/".join(str(q["score"]) for q in texts))


if __name__ == "__main__":
    main()
