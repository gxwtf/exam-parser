#!/usr/bin/env python3
"""对比「一科一卷」的成品质量指标：生产库（存量）vs 新流水线 JSON。

用途：语文 / 英语流水线迁移时，量化「新版 vs 存量」的差异。
只读，不写库。

用法：
    # 存量（生产库）
    python3 compare_paper_quality.py --db --subject 英语 --title "2026北京西城高三一模"
    # 新产出（JSON）
    python3 compare_paper_quality.py --json output_json_english/一模/2026北京西城高三一模英语.json
    # 一次跑三卷并出对照表
    python3 compare_paper_quality.py --pair --subject 英语 \
        --title "2026北京西城高三一模" --json output_json_english/一模/2026北京西城高三一模英语.json \
        [--title ... --json ...]...
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

ENV_PATH = Path.home() / "gxexercise" / ".env"


def database_url() -> str:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"找不到 DATABASE_URL：{ENV_PATH}")


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["psql", database_url(), "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"psql 失败：{out.stderr.strip()[:300]}")
    rows = []
    for line in out.stdout.splitlines():
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def db_metrics(subject: str, title: str) -> dict:
    paper = psql(
        f"""select id, "totalScore" from "TestPaper"
            where subject='{subject}' and title='{title}' limit 1;"""
    )
    if not paper:
        return {"错误": f"库里没有这套卷：{title}"}
    pid, total = paper[0][0], paper[0][1]
    cats = psql(
        f"""select g.category, g."questionType", count(distinct g.id), count(q.id),
                   count(q.id) filter (where coalesce(btrim(q.answer),'') <> ''),
                   count(q.id) filter (where coalesce(btrim(q.analysis),'') <> '')
            from "QuestionGroup" g
            join "PaperItem" pi on pi."itemId" = g.id
            join "GroupItem" gi on gi."groupId" = g.id
            join "Question" q on q.id = gi."questionId"
            where pi."paperId" = '{pid}'
            group by 1,2 order by 4 desc;"""
    )
    sections = sum(int(r[2]) for r in cats)
    questions = sum(int(r[3]) for r in cats)
    answered = sum(int(r[4]) for r in cats)
    analysed = sum(int(r[5]) for r in cats)
    return {
        "来源": "生产库",
        "卷名": title,
        "总分": total,
        "板块数": sections,
        "题数": questions,
        "有答案": answered,
        "有解析": analysed,
        "答案覆盖率": f"{answered / questions * 100:.0f}%" if questions else "—",
        "板块明细": {f"{r[0]}/{r[1]}": f"{r[2]}组/{r[3]}题" for r in cats},
    }


def json_metrics(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    paper = data.get("paper") or {}
    sections = data.get("sections") or data.get("groups") or []
    cats = Counter()
    questions = answered = analysed = 0
    for sec in sections:
        qs = sec.get("questions") or []
        key = f"{sec.get('category')}/{sec.get('questionType')}"
        cats[key] += len(qs) if qs else 1
        for q in qs:
            questions += 1
            if str(q.get("answer") or "").strip():
                answered += 1
            if str(q.get("analysis") or "").strip():
                analysed += 1
    return {
        "来源": "新 JSON",
        "卷名": paper.get("title") or Path(path).stem,
        "总分": paper.get("totalScore"),
        "板块数": len(sections),
        "题数": questions,
        "有答案": answered,
        "有解析": analysed,
        "答案覆盖率": f"{answered / questions * 100:.0f}%" if questions else "—",
        "板块明细": dict(cats),
        "顶层键": "sections" if "sections" in data else "groups",
    }


def show(m: dict) -> None:
    print(f"—— {m['来源']}：{m['卷名']}")
    for k in ("总分", "板块数", "题数", "有答案", "有解析", "答案覆盖率"):
        if k in m:
            print(f"   {k}: {m[k]}")
    if "顶层键" in m:
        print(f"   顶层键: {m['顶层键']}")
    for k, v in (m.get("板块明细") or {}).items():
        print(f"   · {k}: {v}")
    if "错误" in m:
        print(f"   ! {m['错误']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="store_true", help="只看生产库存量")
    ap.add_argument("--json", action="append", default=[], help="看新产出 JSON（可多次）")
    ap.add_argument("--subject", default="", help="--db 时必需")
    ap.add_argument("--title", action="append", default=[], help="--db 的卷名（可多次，与 --json 顺序对应）")
    ap.add_argument("--pair", action="store_true", help="按 --title/--json 顺序成对比较")
    args = ap.parse_args()

    if args.pair:
        if len(args.title) != len(args.json):
            raise SystemExit("--pair 要求 --title 与 --json 数量一致")
        for title, path in zip(args.title, args.json):
            show(db_metrics(args.subject, title))
            show(json_metrics(path))
            print()
        return

    if args.db:
        if not args.subject or not args.title:
            raise SystemExit("--db 需要 --subject 与至少一个 --title")
        for t in args.title:
            show(db_metrics(args.subject, t))
        return

    for p in args.json:
        if not os.path.exists(p):
            print(f"! 找不到文件：{p}")
            continue
        show(json_metrics(p))


if __name__ == "__main__":
    main()
