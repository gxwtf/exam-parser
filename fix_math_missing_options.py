#!/usr/bin/env python3
"""针对已有 JSON，逐题定向补齐缺失的选择题选项（只动有问题的题，不重跑整份卷）"""
import json, glob, os, re, base64, subprocess, tempfile, pathlib, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_math_pdf import (REPAIR_PROMPT, SYSTEM_PROMPT, coerce_options, chat_json,
                              BASE_URL, API_KEY, MODEL, log)

BBOX_PROMPT = """这张图是一页数学试卷。请找到第 {no} 题的各选项（A/B/C/D，选项内容本身可能是图形或公式图片），
给出每个选项内容在本页的归一化 bbox（左上角为原点，0~1，格式 [x0,y0,x1,y1]）。
只输出 JSON：{{"options":[{{"id":"a","bbox":[..]}},{{"id":"b","bbox":[..]}}]}}
注意：本页没有印出的选项就不要输出；bbox 要刚好框住该选项的内容（含图形）。"""

PDF_DIR = str(pathlib.Path(__file__).parent.parent / "gaozhong/二模/数学")
JSON_DIR = str(pathlib.Path(__file__).parent.parent / "output_json_math/二模/数学")
MAX_PAGES = int(os.getenv("FIX_MAX_PAGES", "6"))

def main():
    fixed_total = 0
    for jf in sorted(glob.glob(os.path.join(JSON_DIR, "*.json"))):
        stem = os.path.basename(jf)[:-5]
        d = json.load(open(jf, encoding="utf-8"))
        targets = [q for s in d["sections"] if s["type"] == "选择题"
                   for q in s["questions"] if any(not o["label"] for o in q["options"])]
        if not targets:
            continue
        pdfs = glob.glob(os.path.join(PDF_DIR, stem + "*.pdf"))
        if not pdfs:
            log(f"跳过（无 PDF）：{stem}")
            continue
        changed = False
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", str(MAX_PAGES),
                            pdfs[0], f"{td}/p"], check=True, capture_output=True)
            pages = sorted(pathlib.Path(td).glob("p*.png"))
            for q in targets:
                no = q["id"]
                cur = {o["id"]: o for o in q["options"]}
                for img in pages:
                    if all(cur.get(i, {}).get("label") for i in "abcd"):
                        break
                    try:
                        got = chat_json(
                            [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": [
                                 {"type": "text", "text": REPAIR_PROMPT.format(no=no)},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img.read_bytes()).decode()}}]}],
                            MODEL, 3000, retries=2)
                    except Exception:
                        continue
                    om = coerce_options(got.get("options"))
                    added = [k for k, v in om.items() if k in "abcd" and v and not cur.get(k, {}).get("label")]
                    for k in added:
                        cur[k] = {"id": k, "label": om[k]}
                    if added:
                        log(f"  {stem[:24]} 第{no}题 → 补 {'/'.join(added)}（{img.name}）")
                # 选项是图形/公式图片、文本模型转不出来 → 让模型给 bbox，裁图当选项
                if not all(cur.get(i, {}).get("label") for i in "abcd"):
                    for img in pages:
                        if all(cur.get(i, {}).get("label") for i in "abcd"):
                            break
                        try:
                            got = chat_json([{"role": "user", "content": [
                                {"type": "text", "text": BBOX_PROMPT.format(no=no)},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img.read_bytes()).decode()}}]}],
                                MODEL, 2000, retries=2)
                        except Exception:
                            continue
                        for item in (got.get("options") or []):
                            oid = str(item.get("id", "")).strip().lower()
                            bb = item.get("bbox") or []
                            if oid not in "abcd" or len(bb) != 4 or cur.get(oid, {}).get("label"):
                                continue
                            x0, y0, x1, y1 = [float(v) for v in bb]
                            if (x1 - x0) <= 0.02 or (y1 - y0) <= 0.01:
                                continue
                            img_dir = pathlib.Path(JSON_DIR) / "images"
                            img_dir.mkdir(parents=True, exist_ok=True)
                            dst = img_dir / f"{stem}_q{no}_{oid}.png"
                            geo = f"{int((x1-x0)*1000)}%x{int((y1-y0)*1000)}%+{int(x0*100)}%+{int(y0*100)}%"
                            try:
                                subprocess.run(["convert", str(img), "-crop", geo, "+repage", str(dst)], check=True, capture_output=True)
                            except Exception:
                                continue
                            cur[oid] = {"id": oid, "label": f"![](images/{dst.name})"}
                            log(f"  {stem[:24]} 第{no}题 → 裁图补 {oid}（{img.name}）")
                if all(cur.get(i, {}).get("label") for i in "abcd"):
                    q["options"] = [cur[i] for i in "abcd"]
                    changed = True
                    fixed_total += 1
                else:
                    q["options"] = [cur.get(i, {"id": i, "label": ""}) for i in "abcd"]
                    miss = [i for i in "abcd" if not cur.get(i, {}).get("label")]
                    log(f"  {stem[:24]} 第{no}题 仍缺 {'/'.join(miss)}")
        if changed:
            json.dump(d, open(jf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"完成：补齐 {fixed_total} 道题")

if __name__ == "__main__":
    main()
