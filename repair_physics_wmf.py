#!/usr/bin/env python3
"""物理：用 AI 重新转录含 .wmf 公式图的题（Word 公式对象，浏览器无法显示）

对每份含 .wmf 引用的卷：渲染前若干页 → 只问受影响的题「题干与四个选项（公式用 LaTeX）」，
拿 AI 的文本覆盖 JSON（这样 D 选项/表格里的公式都变成可渲染的 LaTeX）。

用法：python3 repair_physics_wmf.py --type 二模 [--dry-run]
"""
import argparse, base64, json, re, tempfile, time
from pathlib import Path
import convert_math_pdf as M
from paths import DATA_ROOT

ROOT = DATA_ROOT
JSON_ROOT = ROOT / "output_json_math"
PDF_DIR = ROOT / "gaozhong"

ASK = """这是北京高中物理试卷（或答案）的一页图像。请只处理第 {no} 题，输出 JSON：

{{"stem": "题干（公式用 $...$ LaTeX，不写题号和分值）",
  "options": [{{"id":"a","label":"$...$ 或纯文字"}},{{"id":"b","label":"..."}},{{"id":"c","label":"..."}},{{"id":"d","label":"..."}}]}}

要求：公式/单位一律用 LaTeX（如 $v$、$10\\ \\mathrm{{m/s}}$、$\\mu$），纯数字也包起来（$2$）；
选项只写内容，不带 A．/（A） 前缀；本页没有该题则输出 {{}}。只输出 JSON。"""


def ask_page(img: Path, no: int, model: str) -> dict:
    b64 = base64.b64encode(img.read_bytes()).decode()
    try:
        return M.chat_json([{"role": "user", "content": [
            {"type": "text", "text": ASK.format(no=no)},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}], model, 3000, retries=2)
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="二模")
    ap.add_argument("--model", default=M.MODEL)
    ap.add_argument("--base-url", default=M.BASE_URL)
    ap.add_argument("--dpi", type=int, default=M.DPI)
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    M.BASE_URL = args.base_url.rstrip("/")

    fixed = 0
    for jf in sorted((JSON_ROOT / args.type / "物理").glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        need = []
        for sec in data["sections"]:
            for q in sec["questions"]:
                if re.search(r"\.wmf\)", q["content"]) or any(re.search(r"\.wmf\)", str(o["label"])) for o in q["options"]):
                    need.append(q)
        if not need:
            continue
        pdfs = [p for p in (PDF_DIR / args.type / "物理").glob(jf.stem + "*.pdf")]
        if not pdfs:
            print(f"  跳过（无 PDF）：{jf.stem}"); continue
        print(f"处理 {jf.stem}：{len(need)} 题含 wmf")
        with tempfile.TemporaryDirectory() as td:
            pages = M.render_pages(pdfs[0], args.dpi, Path(td), args.max_pages)
            for q in need:
                no = q["id"]
                got_opts = got_stem = None
                for img in pages:
                    got = ask_page(img, no, args.model)
                    opts = got.get("options") or []
                    if isinstance(opts, list) and len(opts) >= 4:
                        got_opts = opts; got_stem = got.get("stem") or got_stem
                        break
                if not got_opts:
                    print(f"   第{no}题：未取得选项"); continue
                # 应用
                labels = {}
                for o in got_opts:
                    if isinstance(o, dict):
                        k = str(o.get("id", "")).lower()
                        if k in "abcd": labels[k] = M.wrap_numbers_in_text(M.wrap_plain_math(M.strip_opt_prefix(M.normalize_latex(str(o.get("label", ""))))))
                if len(labels) == 4 and all(labels.values()) and not args.dry_run:
                    for o in q["options"]:
                        o["label"] = labels[o["id"]]
                    if got_stem:
                        q["content"] = M.space_before_input(M.split_tail_sentence(M.split_circled(M.strip_number_prefix(M.normalize_latex(got_stem)))))
                    fixed += 1
                    print(f"   第{no}题 ✓ 已用 AI 转录替换")
        if not args.dry_run:
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成：修复 {fixed} 题")


if __name__ == "__main__":
    main()
