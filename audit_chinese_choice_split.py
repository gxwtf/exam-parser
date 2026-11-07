#!/usr/bin/env python3
"""体检：语文卷里「一道选择题被拆成若干主观小题」的结构错位。

背景：部分卷面把选择题的 A–D 选项排成独立段落，解析时被当成多道 `text` 小题，
线上表现为「选择题变成带答题框的大题」。本脚本只做**只读体检**，输出可疑清单供人工/转换器修。

判据（三条硬门槛，避免把名著/文学类里真正的多道简答题误判）：
1. 语气：片段不含设问/请求语气（请/分析/概括/说明/简析/为什么/哪些/如何/简述/谈谈/探究/?/？）；
2. 平行：连续 ≥3 个短片段（≤45 字）、无选项、长度彼此接近（极差 ≤ 15 字）；
3. 错位证据：这串片段**紧后面**跟一道 `single`（说明选项整体后移了），或该板块里存在 single 且其题干包含这串片段的文字。

用法：
    python3 audit_chinese_choice_split.py                      # 直接连生产库（只读）
    python3 audit_chinese_choice_split.py --tsv dump.tsv       # 用已导出的 TSV
    python3 audit_chinese_choice_split.py --json-root ../output_json_chinese   # 扫 JSON 目录
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ENV_PATH = Path.home() / "gxexercise" / ".env"

ASK_WORDS = ("请", "分析", "概括", "说明", "简析", "为什么", "哪些", "如何", "简述", "谈谈",
             "探究", "理解", "赏析", "评价", "?", "？", "分别", "结合")
MAX_LEN = 45
MIN_RUN = 3
MAX_SPREAD = 15


def database_url() -> str:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"找不到 DATABASE_URL：{ENV_PATH}")


def load_from_db() -> dict[tuple[str, str], list[dict]]:
    sql = """
    select replace(replace(p.title, chr(10), ' '), chr(9), ' '),
           replace(replace(g.category, chr(10), ' '), chr(9), ' '),
           q."questionType",
           replace(replace(coalesce(q.content,''), chr(10), ' '), chr(9), ' '),
           coalesce(jsonb_array_length(q.options),0)
    from "TestPaper" p
    join "PaperItem" pi on pi."paperId"=p.id
    join "QuestionGroup" g on g.id=pi."itemId"
    join "GroupItem" gi on gi."groupId"=g.id
    join "Question" q on q.id=gi."questionId"
    where p.subject='语文'
    order by p.title, g.category, gi."orderIndex";
    """
    out = subprocess.run(["psql", database_url(), "-t", "-A", "-F", "\x1f", "-c", sql],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(out.stderr.strip()[:300])
    groups: dict[tuple[str, str], list[dict]] = {}
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        title, category, qtype, content, nopt = parts[:5]
        groups.setdefault((title, category), []).append(
            {"qtype": qtype, "content": content, "nopts": int(nopt)}
        )
    return groups


def load_from_tsv(path: str) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        title, category, _gid, _qid, qtype, _length, nopt = parts[:7]
        groups.setdefault((title, category), []).append(
            {"qtype": qtype, "content": "", "nopts": int(nopt or 0)}
        )
    return groups


def load_from_json(root: str) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in sorted(Path(root).glob("*/*.json")):
        if ".old-groups" in f.name or f.name.endswith(".no-analysis.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sec in data.get("sections") or data.get("groups") or []:
            key = (f.stem, sec.get("category") or "")
            groups[key] = [
                {
                    "qtype": q.get("questionType") or "",
                    "content": q.get("content") or "",
                    "nopts": len(q.get("options") or []),
                }
                for q in (sec.get("questions") or [])
            ]
    return groups


def is_plain_fragment(q: dict) -> bool:
    if q["qtype"] != "text" or q["nopts"] > 0:
        return False
    text = (q["content"] or "").strip()
    if not text or len(text) > MAX_LEN:
        return False
    return not any(w in text for w in ASK_WORDS)


def extra_findings(groups: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """其它结构错位：选项写在题干里的 text 题、选项数不足的 single、答案非字母的 single。"""
    out = []
    import re
    opt_in_stem = re.compile(r"[A-D][.．、]")
    # 选择式设问：题干以「…的一项是 / …的一组是 / 正确的是」这类收尾，但题型是 text → 说明选项没解析出来
    choice_stem = re.compile(r"(下列[^。]{0,20}(一项|一组)是|正确的是|不正确的是|正确的一项是|推断正确|表述不正确|理解不正确|都相同的一组)")
    for (title, category), qs in groups.items():
        for idx, q in enumerate(qs, start=1):
            text = q["content"] or ""
            if q["qtype"] == "text" and choice_stem.search(text):
                out.append({"卷": title, "板块": category, "起": idx, "类型": "选择式设问却是 text（选项没解析出来）",
                            "样例": text[:50]})
            elif q["qtype"] == "text" and len(opt_in_stem.findall(text)) >= 2:
                out.append({"卷": title, "板块": category, "起": idx, "类型": "选项写在题干里（应拆成 single）",
                            "样例": text[:40]})
            if q["qtype"] == "single" and q["nopts"] not in (3, 4, 6, 7):
                out.append({"卷": title, "板块": category, "起": idx, "类型": f"single 但选项数={q['nopts']}",
                            "样例": text[:40]})
    return out


def audit(groups: dict[tuple[str, str], list[dict]]) -> list[dict]:
    findings = []
    for (title, category), qs in groups.items():
        i = 0
        while i < len(qs):
            if not is_plain_fragment(qs[i]):
                i += 1
                continue
            j = i
            while j < len(qs) and is_plain_fragment(qs[j]):
                j += 1
            run = qs[i:j]
            if len(run) >= MIN_RUN:
                lens = [len((x["content"] or "").strip()) for x in run]
                spread = max(lens) - min(lens)
                followed_by_single = j < len(qs) and qs[j]["qtype"] == "single" and qs[j]["nopts"] > 0
                last_text = (run[-1]["content"] or "").strip()
                merged_into_next = False
                if j < len(qs):
                    nxt = (qs[j]["content"] or "")
                    if last_text and last_text[:8] and last_text[:8] in nxt:
                        merged_into_next = True
                if spread <= MAX_SPREAD and (followed_by_single or merged_into_next or len(run) >= 4):
                    findings.append({
                        "卷": title,
                        "板块": category,
                        "起": i + 1,
                        "片段数": len(run),
                        "长度": lens,
                        "证据": ("后接 single" if followed_by_single else "")
                                  + ("；末片混入下一题" if merged_into_next else ""),
                        "样例": (run[0]["content"] or "").strip()[:36],
                    })
            i = j if j > i else i + 1
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv")
    ap.add_argument("--json-root")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    if args.tsv:
        groups = load_from_tsv(args.tsv)
        src = f"TSV {args.tsv}"
    elif args.json_root:
        groups = load_from_json(args.json_root)
        src = f"JSON {args.json_root}"
    else:
        groups = load_from_db()
        src = "生产库"

    findings = audit(groups)
    extra = extra_findings(groups)
    print(f"数据源：{src}｜板块 {len(groups)} 个｜拆选项可疑 {len(findings)} 处｜其它结构可疑 {len(extra)} 处")
    for e in extra[:40]:
        print(f"  ! {e['卷']} [{e['板块']}] 第 {e['起']} 题：{e['类型']}｜{e['样例']}")
    for f in findings[: args.limit]:
        print(f"  ✗ {f['卷']} [{f['板块']}] 第 {f['起']} 题起 {f['片段数']} 个片段 "
              f"长度{f['长度']} {f['证据']}\n      {f['样例']}")
    if len(findings) > args.limit:
        print(f"  …（还有 {len(findings) - args.limit} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
