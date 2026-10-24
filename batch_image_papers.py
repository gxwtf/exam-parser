#!/usr/bin/env python3
"""批量生成化学二模「图片版」：逐卷 裁图 → paper.json。

每卷：
- 源 PDF 取 gaozhong/<类型>/<学科>/<卷>.pdf
- 答案：output_json_math/<类型>/<学科>/<卷>.json（选择题）+ output_docx_to_md/<类型>/<学科>/<卷>（教师版）/*.md（非选择题）
用法：python3 batch_image_papers.py [-T 二模] [-s 化学] [--limit N] [--only 关键词]
"""
import argparse, json, re, subprocess, sys, time
from pathlib import Path

PROJ = Path(__file__).parent
ROOT = PROJ.parent


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    src = ROOT / "gaozhong" / args.type / args.subject
    pdfs = sorted(src.glob("*.pdf"))
    out_root = ROOT / "output_image_questions" / args.type / args.subject
    serve_root = ROOT / "output_docx_to_md" / args.type / args.subject
    results = []
    if args.limit:
        pdfs = pdfs[: args.limit]

    for pdf in pdfs:
        stem = re.sub(r"（教师版）|\(教师版\)|含答案", "", pdf.stem).strip()
        if args.only and args.only not in stem:
            continue
        qdir = out_root / stem
        if args.skip_existing and (qdir / "paper.json").exists():
            results.append((stem, "skip", 0, 0, 0))
            print(f"跳过 {stem}")
            continue
        qdir.mkdir(parents=True, exist_ok=True)
        serve = serve_root / stem / "images"
        serve.mkdir(parents=True, exist_ok=True)
        t0 = time.time()

        r = run([sys.executable, str(PROJ / "crop_questions_pdf.py"), "-p", str(pdf),
                 "-o", str(qdir), "--serve-dir", str(serve), "-T", args.type, "-s", args.subject])
        found = 0
        m = re.search(r"识别题号 (\d+) 个", r.stdout or "")
        if m:
            found = int(m.group(1))
        if found == 0:
            results.append((stem, "fail-crop", 0, 0, time.time() - t0))
            print(f"✗ {stem}：没识别到题号 { (r.stderr or '')[:120] }")
            continue

        md_dir = ROOT / "output_docx_to_md" / args.type / args.subject / pdf.stem
        mds = sorted(md_dir.glob("*.md")) if md_dir.exists() else []
        choice_json = ROOT / "output_json_math" / args.type / args.subject / f"{stem}.json"
        cmd = [sys.executable, str(PROJ / "build_image_questions.py"), "-d", str(qdir)]
        if mds:
            cmd += ["--md", str(mds[0])]
        if choice_json.exists():
            cmd += ["--choice-json", str(choice_json)]
        r2 = run(cmd)
        nq = 0
        miss = 0
        m2 = re.search(r"(\d+) 题（选择 (\d+)，非选择 (\d+)），缺答案 (\d+)", r2.stdout or "")
        if m2:
            nq, miss = int(m2.group(1)), int(m2.group(4))
        results.append((stem, "ok", found, miss, time.time() - t0))
        print(f"✓ {stem}：识别 {found} 题 / 生成 {nq} 题 / 缺答案 {miss}（{time.time()-t0:.0f}s）")

    ok = sum(1 for r in results if r[1] == "ok")
    fail = [r[0] for r in results if r[1].startswith("fail")]
    (out_root / "_batch_report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完成：成功 {ok} / 失败 {len(fail)} / 共 {len(results)}")
    if fail:
        print("失败卷：", fail[:20])


if __name__ == "__main__":
    main()
