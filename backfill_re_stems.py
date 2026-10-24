#!/usr/bin/env python3
"""阅读表达题干回填工具（不调用 AI，纯规则重算）

背景：exam-parser 一次重构删掉了「从 md 提取阅读表达题干」的逻辑，导致 2026-09-24 之后
生成的 JSON 里 阅读表达 的 questions[].content 全为空，落库后前端整块题干不显示。

本工具用**修复后的** src/locator.py + src/json_builder.py 代码路径，从原始 md 重新抽取
题干，回填到既有 JSON 与数据库 Question.content，不重跑 AI、不动答案和解析。

用法:
    # 只看差异（不写任何东西）
    python3 backfill_re_stems.py --papers "output_json/真题/英语/2026北京高考真题英语.json"

    # 全量演练，看成功/失败统计
    python3 backfill_re_stems.py --all --dry-run

    # 写回 JSON
    python3 backfill_re_stems.py --all --write-json

    # 写回数据库（生成 SQL 并执行）
    python3 backfill_re_stems.py --all --write-db
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from paths import DATA_ROOT

BASE = Path(__file__).resolve().parent
ROOT = DATA_ROOT
OUT_MD = ROOT / "output_docx_to_md"
OUT_JSON = ROOT / "output_json"
ENV_FILE = Path("/home/gxwtf/gxexercise/.env")

sys.path.insert(0, str(BASE))
from src.locator import ContentLocator          # noqa: E402
from src.json_builder import JSONBuilder        # noqa: E402

TYPES = ["上期末", "下期末", "一模", "二模", "真题"]
RE_TYPE = "阅读表达"


def clean_stem(name: str) -> str:
    name = name.replace("（教师版）", "").replace("(教师版)", "").strip()
    return re.sub(r"含答案$", "", name)


def resolve_md(json_path: Path):
    """由 JSON 路径反查原始 md（md 目录名保留（教师版）后缀，JSON 文件名不带）"""
    rel = json_path.relative_to(OUT_JSON)
    type_name, subject = rel.parts[0], rel.parts[1]
    stem = clean_stem(json_path.stem)
    base = OUT_MD / type_name / subject
    if not base.is_dir():
        return None, None
    for d in base.iterdir():
        if d.is_dir() and clean_stem(d.name).strip() == stem.strip():
            md = d / f"{d.name}.md"
            if md.is_file():
                return md, (type_name, subject, d.name)
    return None, None


# 全角/半角与常见连字符差异，仅用于「定位文章起点」，不影响落盘内容
_FOLD = str.maketrans({
    "﹣": "-", "－": "-", "—": "-", "–": "-", "，": ",", "。": ".", "：": ":",
    "；": ";", "！": "!", "？": "?", "（": "(", "）": ")", "　": " ",
    "’": "'", "‘": "'", "“": '"', "”": '"',
})


def norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").translate(_FOLD))


def find_article_start(md: str, article: str):
    """用 article 的首行（归一化后）在 md 里定位文章起点"""
    # JSON 里的图片地址已被改写成 nginx 直出地址，比对前还原成 md 里的相对写法
    article = re.sub(r"!\[\]\(/exam-images/[^)]*?/images/", "![](images/", article or "")
    lines = (article or "").split("\n")
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    first = next((l.strip() for l in lines if l.strip()), "")
    if not first:
        return None
    md_lines = md.split("\n")
    norm_lines = [(pos, norm(l)) for pos, l in _iter_lines(md_lines)]
    # 探针优先级：首行 → 最长的正文行（首行可能带引号/省略号，容易被改写）
    body = [l.strip() for l in lines if len(l.strip()) > 30]
    probes = [norm(first)]
    if body:
        probes.append(norm(max(body, key=len)))
    for target in probes:
        if len(target) < 8:
            continue
        # 逐级放宽：整行 → 前 60 → 前 40 → 前 25 字；按子串匹配容忍首尾差异
        for cut in (len(target), 60, 40, 25):
            probe = target[:cut]
            if len(probe) < 8:
                continue
            for pos, nl in norm_lines:
                if probe in nl:
                    return pos
    return None


def _iter_lines(md_lines):
    pos = 0
    for line in md_lines:
        yield pos, line
        pos += len(line) + 1


QNUM_RE = re.compile(r"^(?:（(\d+)）|(\d+)\\?[.．])\s*")

# 章节边界判断（与 locator._is_section_header 同一套规则）
_HDR_PATTERNS = [
    r"^参考答案", r"^\*\*第[一二三ⅠⅡⅢ]卷",
    r"^(?:\*\*)?第[一二三四五六七八九十]+部分", r"^(?:\*\*)?第[一二三]节",
    r"^\*\*[IVⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.、]\s*(?:完形填空|阅读理解|语法填空|书面表达|选词填空)",
    r"^\*\*[A-G]\*\*$", r"^[A-G]$",
]


def qnum_of(m) -> int:
    """从 QNUM_RE 的匹配里取题号（兼容「1.」与「（1）」两种写法）"""
    return int(m.group(1) or m.group(2))


def is_header(line: str) -> bool:
    return any(re.match(p, line.strip()) for p in _HDR_PATTERNS)


def split_blocks(region: str, base_pos: int):
    """把一段 md 按「行首题号」切成条目：每条 = 编号行 + 其后的非编号行。

    遇到章节标题（第二节/参考答案/…）立即收尾并停止，否则下一个 section 的标题
    和答题横线会被算进最后一题的题干里。
    """
    items, cur = [], None
    pos = base_pos
    for line in region.split("\n"):
        stripped = line.strip()
        if is_header(stripped):
            if cur:
                items.append(cur)
            break
        m = QNUM_RE.match(stripped)
        if m:
            if cur:
                items.append(cur)
            cur = {"off": pos, "num": qnum_of(m), "lines": [line]}
        else:
            if cur is not None:
                cur["lines"].append(line)
        pos += len(line) + 1
    if cur:
        items.append(cur)
    return items


def score_window(items, i, k, tail_is_header):
    """给「第 i 个条目起连续 k 条」打分：题号连续 + 后面紧跟章节边界 = 更像题目块"""
    win = items[i:i + k]
    if len(win) < k:
        return None
    nums = [w["num"] for w in win]
    score = 0
    if all(nums[j + 1] == nums[j] + 1 for j in range(k - 1)):
        score += 10
    elif all(nums[j + 1] > nums[j] for j in range(k - 1)):
        score += 6
    if tail_is_header:
        score += 5
    # 每条第一行不宜过长（题干首行一般不长），像选项行/答案行要扣分
    for w in win:
        head = w["lines"][0].strip()
        if re.match(r"^\d+\\?[.．]\s*[A-D]\\?[.．]", head):
            score -= 8
        if len(head) > 400:
            score -= 3
    return score


def extract_by_window(md: str, article_start: int, answers_pos: int, k: int):
    """在 [文章起点, 参考答案) 之间，挑一个长度 k 的连续条目窗口作为题干块"""
    end = answers_pos if answers_pos > article_start else len(md)
    region = md[article_start:end]
    items = split_blocks(region, article_start)
    if not items:
        return None
    best, best_score = None, None
    for i in range(0, len(items) - k + 1):
        tail = items[i + k]["lines"][0] if i + k < len(items) else ""
        tail_is_hdr = (not tail) or is_header(tail) or bool(QNUM_RE.match(tail.strip())) is False
        sc = score_window(items, i, k, tail_is_hdr)
        if sc is None:
            continue
        if best_score is None or sc > best_score:
            best, best_score = items[i:i + k], sc
    if not best:
        return None
    q_start = best[0]["num"]
    text = "\n".join(l for it in best for l in it["lines"]).strip()
    return {"q_start": q_start, "text": text, "score": best_score}


def find_question_range(md: str, start_pos: int, count: int):
    """从文章起点向后找「连续 count 个题号」的题目块，返回 (q_start, article_end_pos)"""
    lines = md[start_pos:].split("\n")
    offsets = []
    pos = start_pos
    for line in lines:
        offsets.append((pos, line))
        pos += len(line) + 1

    for idx, (off, line) in enumerate(offsets):
        m = QNUM_RE.match(line.strip())
        if not m:
            continue
        q_start = qnum_of(m)
        # 校验后面 count 个题号连续存在
        ok = True
        want = q_start
        for j in range(idx, len(offsets)):
            mm = QNUM_RE.match(offsets[j][1].strip())
            if not mm:
                continue
            if qnum_of(mm) == want:
                want += 1
                if want == q_start + count:
                    break
            elif qnum_of(mm) > want:
                ok = False
                break
        if ok:
            return q_start, off
    return None, None


def parse_qtext(builder, text, k):
    """解析题目块；若 md 用的是「（1）」式编号，改写成「1.」后再试一次"""
    parsed = builder._parse_question_text(text, "reading-expression")
    if len(parsed) == k:
        return parsed
    norm_text = re.sub(r"(?m)^\s*（(\d+)）\s*", r"\1. ", text)
    if norm_text != text:
        parsed2 = builder._parse_question_text(norm_text, "reading-expression")
        if len(parsed2) == k:
            return parsed2
    return parsed


def rewrite_images(content: str, type_name: str, subject: str, md_folder: str) -> str:
    """md 里的 images/x.png → nginx 直出地址 /exam-images/<类型>/<学科>/<md目录>/images/x.png"""
    prefix = f"/exam-images/{type_name}/{subject}/{md_folder}/images/"
    return re.sub(r"\]\(images/", f"]({prefix}", content)


def build_stems(json_path: Path, builder: JSONBuilder):
    """返回 (stems_by_section, 错误信息)；stems_by_section: {section_index: {...}}"""
    md_path, meta = resolve_md(json_path)
    if not md_path:
        return None, "找不到原始 md"
    type_name, subject, md_folder = meta
    md = md_path.read_text(encoding="utf-8")
    locator = ContentLocator(md)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    answers_pos = md.find("参考答案")

    out = {}
    for si, sec in enumerate(data.get("sections") or []):
        if sec.get("type") != RE_TYPE:
            continue
        questions = sec.get("questions") or []
        if not questions:
            continue
        k = len(questions)

        # ① 首选「题号区间法」：与生产线（parser.py）完全同一条代码路径
        contents, q_start, errs = None, None, []
        start_pos = find_article_start(md, sec.get("article") or "")
        if start_pos is not None:
            qa_start, article_end = find_question_range(md, start_pos, k)
            if qa_start is not None:
                qtext = locator.find_questions_after_article(article_end, qa_start, qa_start + k - 1)
                if qtext:
                    parsed = parse_qtext(builder, qtext, k)
                    if len(parsed) == k:
                        contents = [rewrite_images((p.get("content") or "").strip(),
                                                   type_name, subject, md_folder) for p in parsed]
                        q_start = qa_start
                    else:
                        errs.append(f"题号区间法解析出 {len(parsed)} 题（应为 {k}）")
                else:
                    errs.append(f"find_questions_after_article({qa_start}-{qa_start+k-1}) 返回空")
            else:
                errs.append("找不到连续题号")
        else:
            errs.append("article 首行在 md 中定位失败")

        # ② 兜底「连续题目块」定位：应付 md 题号断号（如 1,2,3,1）的卷子
        if contents is None:
            contents, q_start, werr = _stems_by_window(md, sec, k, builder,
                                                       type_name, subject, md_folder)
            if contents is None:
                return None, f"第 {si} 段：{'；'.join(errs)}；块定位：{werr}"

        if not contents or not all(contents):
            return None, f"第 {si} 段：有题干解析为空"
        # 划线题断言：插了作答提示就必须还带着陈述句（第二段非空），
        # 防止边界算错把陈述句截掉却当成成功
        for idx, c in enumerate(contents, 1):
            if "**_Write the underlined part" in c:
                if not c.split("answer section_**)", 1)[-1].strip():
                    return None, f"第 {si} 段第 {idx} 题：划线题陈述句丢失"
        out[si] = {"q_start": q_start, "contents": contents,
                   "old": [q.get("content") or "" for q in questions]}
    return out, None


def _stems_by_window(md, sec, k, builder, type_name, subject, md_folder):
    """按连续题目块抽题干，返回 (contents|None, q_start|None, 错误信息)"""
    start_pos = find_article_start(md, sec.get("article") or "")
    base = start_pos if start_pos is not None else 0
    answers_pos = md.find("参考答案", base)
    win = extract_by_window(md, base, answers_pos, k)
    if not win:
        return None, None, "找不到候选题目块"
    parsed = parse_qtext(builder, win["text"], k)
    if len(parsed) != k:
        return None, None, f"块定位解析出 {len(parsed)} 题（应为 {k}）"
    contents = [rewrite_images((p.get("content") or "").strip(), type_name, subject, md_folder)
                for p in parsed]
    return contents, win["q_start"], None


# ── 数据库 ────────────────────────────────────────────────────────────

def db_url() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL 未找到")


def psql(url: str, sql: str):
    r = subprocess.run(["psql", url, "-At", "-F", "\x1f", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql 失败: {r.stderr.strip()}")
    return [l.split("\x1f") for l in r.stdout.strip().splitlines() if l]


def sq(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def collect_db_updates(url: str, json_path: Path, stems):
    """把 JSON 里的题干映射到数据库 Question.id，返回 [(qid, new_content)]"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    paper = data["paper"]
    title, subject = paper["title"], paper.get("subject", "英语")
    rows = psql(url, f'select id from "TestPaper" where title={sq(title)} and subject={sq(subject)};')
    if not rows:
        return None, f"库里找不到套卷 {title!r}"
    if len(rows) > 1:
        return None, f"库里 {title!r} 有 {len(rows)} 条重复套卷，跳过"
    paper_id = rows[0][0]

    groups = psql(url, f'select pi."orderIndex", qg.id, qg.category from "PaperItem" pi '
                       f'join "QuestionGroup" qg on qg.id=pi."itemId" '
                       f'where pi."paperId"={sq(paper_id)} order by pi."orderIndex";')
    by_index = {int(g[0]): (g[1], g[2]) for g in groups}

    updates = []
    for si, info in stems.items():
        sec_type = data["sections"][si].get("type")
        gid, cat = by_index.get(si, (None, None))
        if gid is None:
            return None, f"第 {si} 段在库里没有对应题组（orderIndex={si}）"
        if cat != sec_type:
            return None, f"第 {si} 段错位：库里是 {cat}，JSON 是 {sec_type}"
        qrows = psql(url, f'select q.id from "GroupItem" gi join "Question" q on q.id=gi."questionId" '
                          f'where gi."groupId"={sq(gid)} order by gi."orderIndex";')
        qids = [r[0] for r in qrows]
        if len(qids) != len(info["contents"]):
            return None, f"第 {si} 段题数不一致：库里 {len(qids)}，JSON {len(info['contents'])}"
        for qid, new in zip(qids, info["contents"]):
            updates.append((qid, new))
    return updates, None


def main():
    ap = argparse.ArgumentParser(description="阅读表达题干回填")
    ap.add_argument("--papers", nargs="*", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write-json", action="store_true")
    ap.add_argument("--write-db", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true",
                    help="连已有题干也重写（用于统一历史遗留格式）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.papers:
        targets = [Path(p).resolve() for p in args.papers]
    elif args.all:
        targets = []
        for t in TYPES:
            d = OUT_JSON / t / "英语"
            if d.is_dir():
                targets.extend(sorted(d.glob("*.json")))
    else:
        ap.error("需要 --papers 或 --all")

    builder = JSONBuilder()
    url = db_url() if args.write_db else None
    ok, skipped, failed = [], [], []
    sql_lines = []

    for i, jp in enumerate(targets, 1):
        stems, err = build_stems(jp, builder)
        if err:
            failed.append((jp, err))
            print(f"[{i}/{len(targets)}] ✗ {jp.parent.name}/{jp.name}: {err}")
            continue
        # 只处理确实有空的
        if args.force:
            need = {si: v for si, v in stems.items()
                    if any((o or "").strip() != (n or "").strip()
                           for o, n in zip(v["old"], v["contents"]))}
        else:
            need = {si: v for si, v in stems.items() if any(not o.strip() for o in v["old"])}
        if not need:
            skipped.append(jp)
            continue

        if args.verbose or args.papers:
            for si, v in need.items():
                print(f"\n[{i}/{len(targets)}] {jp.name}  第{si}段 题号 {v['q_start']}-"
                      f"{v['q_start']+len(v['contents'])-1}")
                for k, (o, n) in enumerate(zip(v["old"], v["contents"]), 1):
                    print(f"   {k}) 旧: {o[:60]!r}")
                    print(f"      新: {n[:80]!r}")
        else:
            print(f"[{i}/{len(targets)}] ✓ {jp.name}")

        # 写回 JSON
        if args.write_json and not args.dry_run:
            data = json.loads(jp.read_text(encoding="utf-8"))
            for si, v in need.items():
                for q, n in zip(data["sections"][si]["questions"], v["contents"]):
                    q["content"] = n
            jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 收集数据库更新
        if args.write_db and not args.dry_run:
            updates, err2 = collect_db_updates(url, jp, need)
            if err2:
                failed.append((jp, f"DB: {err2}"))
                print(f"   ✗ DB 映射失败: {err2}")
                continue
            for qid, new in updates:
                sql_lines.append(f'update "Question" set content={sq(new)}, '
                                 f'"updatedAt"=now() where id={sq(qid)};')
        ok.append(jp)

    print(f"\n汇总：可回填 {len(ok)} 套 / 无需处理 {len(skipped)} 套 / 失败 {len(failed)} 套")
    for jp, e in failed:
        print(f"   ✗ {jp.parent.name}/{jp.name}: {e}")

    if sql_lines:
        sql_path = ROOT / "backfill_re_stems.sql"
        sql_path.write_text("begin;\n" + "\n".join(sql_lines) + "\ncommit;\n", encoding="utf-8")
        print(f"\n生成 SQL：{sql_path}（{len(sql_lines)} 条 UPDATE）")
        r = subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-f", str(sql_path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"执行失败，已整体回滚：{r.stderr.strip()}")
            sys.exit(1)
        print("数据库更新完成：", r.stdout.strip().splitlines()[-3:])


if __name__ == "__main__":
    main()
