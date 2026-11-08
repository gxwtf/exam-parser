#!/usr/bin/env python3
"""把 OCR 出来的选择题答案补进库里「答案为空」的选择题（只填空，不覆盖已有答案）。"""
import json, os, subprocess, sys
from pathlib import Path
ans = json.load(open(Path(__file__).with_name("chem_answers_ocr.json"), encoding="utf-8"))
DB_NAME = "gxexercise"  # 注意：不是 gxercise，少写一个 e 就连不上
env = dict(os.environ, PGPASSWORD="wddxgy_2714")
def psql(sql):
    return subprocess.run(["psql","-h","localhost","-U","postgres","-d",DB_NAME,"-t","-A","-c",sql],
                          capture_output=True, text=True, env=env).stdout
total = 0
for title, mapping in ans.items():
    ids = [i.strip() for i in psql(f'''select qq.id from "PaperItem" pi join "GroupItem" gi on gi."groupId"=pi."itemId"
        join "Question" qq on qq.id=gi."questionId" join "TestPaper" p on p.id=pi."paperId"
        where p.title='{title}' and p.subject='化学' and qq."questionType"='single' order by qq.id;''').strip().splitlines() if i.strip()]
    n = 0
    for idx, qid in enumerate(ids, start=1):
        a = mapping.get(str(idx), mapping.get(idx))
        if not a: continue
        out = psql(f'''update "Question" set answer='{a}' where id='{qid}' and coalesce(answer,'')='';''')
        if "UPDATE 1" in out: n += 1
    total += n
    if n: print(f"  {title[:40]:42s} 补 {n} 条")
print("合计补", total, "条")
