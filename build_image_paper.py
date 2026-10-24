#!/usr/bin/env python3
"""整卷「图片版」流水线（混合版式）：

- 选择题：先把 docx 重排（行距统一 + 浮动图内联）转 PDF 再裁图
- 非选择题：用原始 PDF 裁图（结构式多，保持原版式）
- 生成每卷 paper.json（含题号/图片/答案/选项）

用法：python3 build_image_paper.py -d <docx> [-T 二模] [--skip-reflow]
"""
import argparse, json, re, subprocess, tempfile
from pathlib import Path

PROJ = Path(__file__).parent
OUT_ROOT = PROJ.parent / "output_image_questions"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--docx", required=True)
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("--subject", default="化学")
    ap.add_argument("--skip-reflow", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    docx = Path(args.docx)
    stem = re.sub(r"（教师版）|\(教师版\)|含答案", "", docx.stem).strip()
    src_dir = docx.parent
    pdf = src_dir / f"{stem}.pdf"
    if not pdf.exists():
        pdfs = sorted(src_dir.glob(f"{stem}*.pdf"))
        pdf = pdfs[0] if pdfs else None
    if not pdf or not pdf.exists():
        raise SystemExit(f"找不到 PDF：{src_dir}")

    qdir = OUT_ROOT / args.type / args.subject / stem
    serve = PROJ.parent / "output_docx_to_md" / args.type / args.subject / stem / "images"
    qdir.mkdir(parents=True, exist_ok=True)
    serve.mkdir(parents=True, exist_ok=True)

    # 选择题题号：从已有文本版解析结果里读
    cands = [
        PROJ.parent / "output_json_math" / args.type / args.subject / f"{stem}.json",
        PROJ.parent / "output_json_math" / args.type / f"{stem}.json",
    ]
    choice_json = next((c for c in cands if c.exists()), cands[0])
    choice_ids = []
    if choice_json.exists():
        d = json.loads(choice_json.read_text(encoding="utf-8"))
        secs = d.get("sections") or []
        if isinstance(secs, dict):
            secs = [secs]
        for sec in secs:
            for q in sec.get("questions", []):
                if q.get("options"):
                    try:
                        choice_ids.append(int(q["id"]))
                    except Exception:
                        pass
    choice_ids = sorted(set(choice_ids))
    print(f"{stem}：选择题 {len(choice_ids)} 道 {choice_ids[:3]}…")

    # 1) 选择题：重排后裁图
    if choice_ids and not args.skip_reflow:
        reflow_docx = Path(tempfile.gettempdir()) / f"{stem}_reflow.docx"
        r = run(["python3", str(PROJ / "reflow_docx.py"), "-p", str(docx), "-o", str(reflow_docx)])
        print("  ", (r.stdout or r.stderr).strip()[:160])
        reflow_pdf_dir = Path(tempfile.gettempdir())
        run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(reflow_pdf_dir), str(reflow_docx)])
        reflow_pdf = reflow_pdf_dir / f"{stem}_reflow.pdf"
        if reflow_pdf.exists():
            only = ",".join(str(i) for i in choice_ids)
            r = run(["python3", str(PROJ / "crop_questions_pdf.py"), "-p", str(reflow_pdf),
                     "-o", str(qdir), "--serve-dir", str(serve), "-T", args.type, "-s", args.subject,
                     "--only", only])
            print("  ", (r.stdout or "").strip().splitlines()[-1] if r.stdout else r.stderr[:160])
        else:
            print("  ⚠ 重排 PDF 生成失败，选择题改用原版式")

    # 2) 非选择题：原 PDF 裁图
    reflow_pdf = Path(tempfile.gettempdir()) / f"{stem}_reflow.pdf"
    except_arg = []
    if choice_ids and reflow_pdf.exists():
        except_arg = ["--except", ",".join(str(i) for i in choice_ids)]
    r = run(["python3", str(PROJ / "crop_questions_pdf.py"), "-p", str(pdf),
             "-o", str(qdir), "--serve-dir", str(serve), "-T", args.type, "-s", args.subject,
             "--max-q", "30"] + except_arg)
    print("  ", (r.stdout or "").strip().splitlines()[-1] if r.stdout else r.stderr[:160])

    # 3) paper.json
    r = run(["python3", str(PROJ / "build_image_questions.py"), "-d", str(qdir),
             "--md", str(next(iter(sorted((PROJ.parent / "output_docx_to_md" / args.type / args.subject / docx.stem).glob("*.md"))), "")),
             "--choice-json", str(choice_json) if choice_json.exists() else ""])
    print("  ", (r.stdout or r.stderr).strip()[:200])


if __name__ == "__main__":
    main()
