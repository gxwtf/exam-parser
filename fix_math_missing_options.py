#!/usr/bin/env python3
"""针对已有 JSON，逐题定向补齐缺失的选择题选项（只动有问题的题，不重跑整份卷）

兼容两种输入（都能跑）：
- 旧文件：option id 是 **小写**（a/b/c/d）
- 新文件：option id 是 **大写**（A/B/C/D，225135f 之后的出口约定）

内部一律用 `option_key()`（小写）做匹配；写盘前调 `normalize_choice_case_in_paper()`
把 id 与答案转回大写。补齐时**优先复用原来的 option dict**（label 与其它字段都保留），
A-D 之外的额外选项也不会被丢掉 —— 不会再用空 label 覆盖已有选项。
"""
import base64
import glob
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_math_pdf import (REPAIR_PROMPT, SYSTEM_PROMPT, coerce_options, chat_json,
                              BASE_URL, API_KEY, MODEL, log)
from paths import DATA_ROOT
from src.answer_case import (count_nonempty_labels, normalize_choice_case_in_paper,
                             option_key, options_by_letter)

BBOX_PROMPT = """这张图是一页数学试卷。请找到第 {no} 题的各选项（A/B/C/D，选项内容本身可能是图形或公式图片），
给出每个选项内容在本页的归一化 bbox（左上角为原点，0~1，格式 [x0,y0,x1,y1]）。
只输出 JSON：{{"options":[{{"id":"a","bbox":[..]}},{{"id":"b","bbox":[..]}}]}}
注意：本页没有印出的选项就不要输出；bbox 要刚好框住该选项的内容（含图形）。"""

PDF_DIR = str(DATA_ROOT / "gaozhong/二模/数学")
JSON_DIR = str(DATA_ROOT / "output_json_math/二模/数学")
MAX_PAGES = int(os.getenv("FIX_MAX_PAGES", "6"))

LETTERS = "abcd"


# ──────────────────────────────────────────────────────────────────
# 纯逻辑（AI / 裁图可注入，便于离线自测）
# ──────────────────────────────────────────────────────────────────

def has_label(opt) -> bool:
    return bool(isinstance(opt, dict) and str(opt.get("label") or "").strip())


def all_labels_filled(cur: dict, letters: str = LETTERS) -> bool:
    return all(has_label(cur.get(i)) for i in letters)


def set_option_label(cur: dict, key: str, label: str) -> dict:
    """把 label 写进已有 option 对象（保留原 id 大小写与其它字段）；没有才新建。"""
    obj = cur.get(key)
    if not isinstance(obj, dict):
        obj = {"id": key, "label": ""}
        cur[key] = obj
    obj["label"] = label
    return obj


def rebuild_options(cur: dict, original=None, letters: str = LETTERS) -> list:
    """按 A-D 顺序重建 options：复用原对象；缺的补空 label（沿用旧行为）。

    A-D 之外的额外选项、以及重复字母的多余对象，都原样保留在末尾，避免丢字段。
    """
    out = [cur.get(i) or {"id": i, "label": ""} for i in letters]
    used = {id(o) for o in out}
    for o in original or []:
        if isinstance(o, dict) and id(o) not in used:
            out.append(o)
    return out


def repair_question_options(q: dict, pages, ask_options, ask_bbox, crop_option,
                            image_dir: pathlib.Path, stem: str, log=log,
                            letters: str = LETTERS):
    """给一道题补选项。返回 (是否补全, 新补的字母列表[大写])。

    Args:
        ask_options: f(img_path, no) -> {"options": ...}（文本模型读选项）
        ask_bbox:    f(img_path, no) -> {"options": [{"id","bbox"}]}（选项是图时）
        crop_option: f(src_img, dst_path, geometry) -> bool（裁图成功与否）
    """
    no = q["id"]
    original = list(q.get("options") or [])
    cur = options_by_letter(original)
    added = []

    def fill(key: str, label: str, img_name: str):
        set_option_label(cur, key, label)
        added.append(key.upper())
        log(f"  {stem[:24]} 第{no}题 → 补 {key.upper()}（{img_name}）")

    # ① 文本模型直接读选项文字/LaTeX
    for img in pages:
        if all_labels_filled(cur, letters):
            break
        try:
            got = ask_options(img, no)
        except Exception:
            continue
        om = coerce_options(got.get("options"))
        for k, v in om.items():
            if k in letters and v and not has_label(cur.get(k)):
                fill(k, v, pathlib.Path(img).name)

    # ② 选项本身是图形/公式图片 → 让模型给 bbox，裁图当选项
    if not all_labels_filled(cur, letters):
        for img in pages:
            if all_labels_filled(cur, letters):
                break
            try:
                got = ask_bbox(img, no)
            except Exception:
                continue
            for item in (got.get("options") or []):
                if not isinstance(item, dict):
                    continue
                oid = option_key(item.get("id"))
                bb = item.get("bbox") or []
                if oid not in letters or len(bb) != 4 or has_label(cur.get(oid)):
                    continue
                try:
                    x0, y0, x1, y1 = [float(v) for v in bb]
                except (TypeError, ValueError):
                    continue
                if (x1 - x0) <= 0.02 or (y1 - y0) <= 0.01:
                    continue
                dst = pathlib.Path(image_dir) / f"{stem}_q{no}_{oid}.png"
                geo = f"{int((x1-x0)*1000)}%x{int((y1-y0)*1000)}%+{int(x0*100)}%+{int(y0*100)}"
                if not crop_option(img, dst, geo):
                    continue
                fill(oid, f"![](images/{dst.name})", pathlib.Path(img).name)

    complete = all_labels_filled(cur, letters)
    q["options"] = rebuild_options(cur, original, letters)
    return complete, added


# ──────────────────────────────────────────────────────────────────
# 真实 IO（仅 main 用）
# ──────────────────────────────────────────────────────────────────

def _ask_options(img, no):
    return chat_json(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": [
             {"type": "text", "text": REPAIR_PROMPT.format(no=no)},
             {"type": "image_url", "image_url": {
                 "url": "data:image/png;base64," + base64.b64encode(pathlib.Path(img).read_bytes()).decode()}}]}],
        MODEL, 3000, retries=2)


def _ask_bbox(img, no):
    return chat_json([{"role": "user", "content": [
        {"type": "text", "text": BBOX_PROMPT.format(no=no)},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(pathlib.Path(img).read_bytes()).decode()}}]}],
        MODEL, 2000, retries=2)


def _crop_option(src_img, dst_path: pathlib.Path, geo: str) -> bool:
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["convert", str(src_img), "-crop", geo, "+repage", str(dst_path)],
                       check=True, capture_output=True)
        return True
    except Exception:
        return False


def main():
    fixed_total = 0
    for jf in sorted(glob.glob(os.path.join(JSON_DIR, "*.json"))):
        stem = os.path.basename(jf)[:-5]
        with open(jf, encoding="utf-8") as f:
            d = json.load(f)
        labels_before = count_nonempty_labels(d)
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
                complete, added = repair_question_options(
                    q, pages, _ask_options, _ask_bbox, _crop_option,
                    image_dir=pathlib.Path(JSON_DIR) / "images", stem=stem)
                if added:
                    changed = True
                if complete:
                    fixed_total += 1
                else:
                    miss = [i for i in LETTERS if not has_label(options_by_letter(q["options"]).get(i))]
                    log(f"  {stem[:24]} 第{q['id']}题 仍缺 {'/'.join(miss)}")
        if changed:
            normalize_choice_case_in_paper(d)  # 导出统一大写（answer 与 option id）
            lost = labels_before - count_nonempty_labels(d)
            if lost > 0:  # 理论上不可能：补齐只会变多
                log(f"  ✗ {stem}：选项 label 非空数减少 {lost} 个，已跳过写盘")
                continue
            with open(jf, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
    log(f"完成：补齐 {fixed_total} 道题")


if __name__ == "__main__":
    main()
