#!/usr/bin/env python3
"""历史整卷解析（选择题 + 非选择题）→ 题库导入 JSON（`output_json_history/<类型>/<卷名>.json`）。

链路：docx→MD（batch_convert_docx_to_md.py）→ 本脚本 → 题库导入脚本
（`gxexercise/scripts/import-subject-json.ts --subject 历史`）。
契约见 `DESIGN-文史地一模.md`，结构同生物（`convert_biology_md.py`），但历史卷的坑不同，
所以是**规则解析**为主，不依赖模型：

    {"paper": {"title","subject","source","grade","year","totalScore","duration","tags"},
     "sections": [
       {"type":"选择题","questionType":"history-choice","title":"一、选择题","category":"选择","score":45,
        "questions":[{"id":1,"questionType":"single","content":…,
                      "options":[{"id":"A","label":…}×4],"answer":"B","analysis":"","score":3}]},
       {"type":"非选择题","questionType":"history-fill","title":"二、非选择题（第16题）儒与医",
        "category":"史料分析","score":10,
        "content":"材料一 …\n\n材料二 …\n\n（1）设问<InputLong></InputLong>\n\n（2）设问<InputLong rows={4}></InputLong>",
        "questions":[{"id":1601,"questionType":"input","content":"（1）设问","answer":"…",
                      "analysis":"","score":4}]}
     ]}

关键约定（与 DESIGN 一致）：

- 选择题 15 题 × 3 分；**每题自洽**——共享材料/共享图复制进每题 `content`；
  ①②③④ 组合式选项原样进 `label`（`A. ①②` 不拆）；`options` 恒 A–D、`answer` 单个大写字母；
- 非选择题**一个大题一个 section**（16…20），材料只出现在 `section.content` 里**一次**
  （政治老流水线把材料复制进每个小问，是用户点名的 bug，这里绝不这么做）；
  **版式不变（单栏）**：材料 + 各小问设问仍内联在 `section.content` 里，每个设问紧接着一个
  答题框占位符 **`<AnswerBox></AnswerBox>`**（2026-11 用户口径：只换答题框，不拆成「左材料右文字」；
  高度由前端按固定档位给，不再写 `rows=…`）；`questions[i].questionType` 仍是 `input`
  （AI 批改的 `{stem}` 口径不变），`content` = 设问原文；**全文不出现 `<InputLong>` / `<Input>`**；
- `questions[i].content` = 该小问设问原文（去分值括号、不含材料），`answer` = 参考答案该小问原文
  （块尾的评分说明随之保留），`score` = 该小问分值，`Σquestions.score == section.score`
  （分不到时把差额给最后一问并写 review，绝不留 0 分）；
- `category` 只能取 观点论证 / 史料分析 / 修改补写（选择题 section 恒为 `选择`）。
  默认走**关键词规则**（确定性、可复现、不依赖网关）；规则判不出来的题才调一次模型
  （`--no-ai` 时落第一个标签并写 review）；
- 图片引用一律 `/exam-images/<类型>/历史/<卷名>/images/x.png`（卷名不带「（教师版）」），
  文件由 `rewrite_images` 复制到按卷名命名的目录，`--check` 逐张校验真实存在。

历史的**特有坑**（脚本内已按卷写死修正表，并逐条写进 `<卷名>.review.txt`）：

1. docx 浮动图错位：`![](images/image1.png)8.毛泽东曾说…`（图其实是第 9 题「结合右图」的太行山图）、
   丰台 `images/image2.jpg` 落在第 2 题选项行里（其实是第 3 题的玻璃杯）→ `IMAGE_FIX`；
2. 同一张图被导出两次（西城第 5 题花边钱 image2/image3）→ `DROP_IMAGE` 去重；
3. 表格跑在题号**前面**（丰台第 6 题「右表展示了…」的表在第 5 题段尾）→ 自动前移到引用它的那题；
4. 教师版表格串行（丰台第 19 题思想家/观点两列表，续行把引文塞进了思想家列）→ 合并回上一行的观点列；
5. 参考答案块头有三种写法（`16 题` / `17 题-参考示例(乔叔版)` / `16.（8分）`），块内再按 (1)(2) 切，
   海淀还把 `（2）` 挤在 `（4分）` 后面同一行 → 切分规则见 `split_answer_subs`。

用法：
    python3 convert_history_md.py -p <md 文件或卷目录> [-T 一模] [--force] [--no-ai]
    python3 convert_history_md.py                      # 处理 output_docx_to_md/一模/历史/*
    python3 convert_history_md.py --check [-T 一模]     # 纯 Python 自查（不调模型），全绿退出码 0
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from convert_chinese_md import rewrite_images, split_paper, log
from paths import OUTPUT_DOCX_TO_MD, OUTPUT_JSON_HISTORY
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "历史"
OUT_ROOT = OUTPUT_JSON_HISTORY
OUT = {"root": OUT_ROOT}
MD_ROOT = OUTPUT_DOCX_TO_MD

N_CHOICE = 15          # 第一部分：15 道单选
CHOICE_SCORE = 3       # 每题 3 分
N_ESSAY = 5            # 第二部分：16-20
CATS = ["观点论证", "史料分析", "修改补写"]

# 兜底分类用到的关键词（确定性规则，见 classify_essays）
KW_FIX = re.compile(r"修改|补写|订正|改正|添补|补充完整|将.{0,12}补充")
KW_ARGUE = re.compile(r"阐述|论述|评析|评论|点评|阐释|加以论证")
KW_VIEW = re.compile(r"谈谈.{0,6}(?:看法|认识|理解)|你的(?:看法|认识|理解)|如何认识")
KW_SOURCE = re.compile(r"考证|史料|文献|辨析|依据材料|据材料|材料[一二三四1234]|归纳|概括"
                       r"|指出|比较|说明|分析|简述|列举|划分")

QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\**\s*\\?[．.、]\s*\**\s*"
    r"(?:[（(]\s*(\d+)\s*分\s*[）)])?[ \t]*"
)
PART1 = re.compile(r"(?m)^[ \t]*\**\s*第[ \t]*[一1][ \t]*部[ \t]*分")
PART2 = re.compile(r"(?m)^[ \t]*\**\s*第[ \t]*[二2][ \t]*部[ \t]*分")
OPT_MARK = re.compile(r"(?:^|(?<=[\s\u3000\u00a0>)]))([A-D])(?:[ \t]*[．.、][ \t]*)", re.M)
SUBQ = re.compile(r"(?m)^[ \t]*[（(]\s*([1-9])\s*[）)]")
SUB_ANS = re.compile(r"[（(]\s*([1-9])\s*[）)]")
SCORE_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")
INLINE_ANS = re.compile(r"(?<![\dA-Za-z])(\d{1,2})\s*\**\s*\\?[.．、]\s*\**\s*([A-D])\**(?![A-Za-z])")
ANS_HDR = re.compile(
    r"(?m)^[ \t]*\**\s*(\d{1,2})\s*(?:\**\s*题(?![一二三四五六七八九十])|\\?[．.、]\s*[（(]\s*\d+\s*分)")
ANSWER_BOX = "<AnswerBox></AnswerBox>"                  # 单栏形态的答题框（高度由前端按档位给）
PH_ANY = re.compile(r"</?Input[A-Za-z0-9]*\b[^<>]*>?")  # 任何 <Input…> 写法（生成后清残留 + --check 用）
IMG_TOKEN = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
EXAM_IMG = re.compile(r"/exam-images/([^\"'\s)]+)")
HEAD_ANS = re.compile(r"^(?:参考答案(?:要点|示例)?|答案示例|答案|评分标准)\s*[:：]\s*$")

# ── 按卷写死的人工修正（每条都会写进 review.txt，供复核）───────────────
# (图片文件名, 原挂错题号, 应挂题号, 说明)
IMAGE_FIX: dict[str, list[tuple[str, int, int, str]]] = {
    "2026北京海淀高三一模历史": [
        ("images/image1.png", 8, 9,
         "docx 把「太行山示意图」导到了第 8 题题号前；第 8 题题干未提图，第 9 题写「结合右图判断」，故移到第 9 题"),
        ("images/image2.png", 14, 15,
         "docx 把「1951 年东欧漫画（马歇尔计划=带枷锁的救生圈）」导进了第 14 题 C 选项行；第 15 题写「右图是 1951 年的东欧漫画」，故移到第 15 题"),
    ],
    "2026北京丰台高三一模历史": [
        ("images/image2.jpg", 2, 3,
         "docx 把「东晋王氏家族墓地玻璃杯」导到了第 2 题 A 选项行；第 2 题右图是西汉十四州部图，第 3 题写「右图展示的是…玻璃杯」，故移到第 3 题"),
    ],
}
# (题号, 图片文件名, 说明)：同一张图被导出两次，去重
DROP_IMAGE: dict[str, list[tuple[int, str, str]]] = {
    "2026北京西城高三一模历史": [
        (5, "images/image3.jpg",
         "第 5 题同一枚「花边钱」被 docx 导出了两次（image2 与 image3 是同一张图，只是尺寸不同），去重保留 image2"),
    ],
}
# 按卷覆盖的分类结果（规则已能判对，留空表即可；需要人工定标时写这里）
CATEGORY_OVERRIDE: dict[str, dict[int, str]] = {}

# 静态复核说明（与卷面比对后的判断，直接写进 review.txt）
STATIC_NOTES: dict[str, list[str]] = {
    "2026北京西城高三一模历史": [
        "第3题「某历史时期的政权示意图」docx 导出图为 780×492（<900px 的截图门槛），但 pdfimages 显示 "
        "教师版 PDF 里嵌的就是同一张 780×492 图，200dpi 重新渲染也只会放大同一份像素、不增加可读细节，"
        "故保留 docx 导出图；标注（前秦/后秦/西燕/南燕/东晋/北魏…）肉眼可辨，如需更清晰的原图请提供高清版",
        "第5题「花边钱」在卷面是浮动图（见右图）+ 行内图两处引用同一枚币，content 里只保留一份图",
    ],
    "2026北京海淀高三一模历史": [
        "第9题太行山示意图仅 241×311（docx 导出，含察哈尔/北平/石家庄/太原等标注），图例简单、字迹可辨，"
        "未采用 PDF 截图；PDF 内嵌图分辨率同源，截图不会更清楚",
        "第17题材料三「1949-2025 年发电量示意图」+ 两张「发电能源种类构成」饼图共 3 张图，"
        "原卷就是分开的三张，未合并，请人工确认前端排版可接受",
    ],
    "2026北京丰台高三一模历史": [
        "第15题「联合国气候变化框架公约四次缔约方大会」表在题干内（题面写「下表列出了…」），"
        "原样保留；第18题就业统计表有若干空单元格（原卷即空），未补 0，请人工确认",
    ],
}


# ──────────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────────
def clean_title(stem: str) -> str:
    t = re.sub(r"[（(]教师版[）)]|[（(]含答案[）)]|含答案|教师版", "", stem)
    return re.sub(r"\s+", "", t).strip()


def tidy(text: str) -> str:
    """段落规整：压空行、图片独占一段、去掉行首行尾空白。"""
    if not text:
        return ""
    t = text.replace("\r", "")
    t = re.sub(r"[ \t\u3000]+(?=\n)", "", t)
    t = re.sub(r"\)[ \t]*(?=!\[)", ")\n\n", t)          # 相邻两张图拆成两段
    t = re.sub(r"(?<=[。！？；：」）])(?=!\[)", "\n\n", t)
    t = re.sub(r"(?<=[^\n])[ \t]*(?=!\[[^\]]*\]\(images/)", "\n\n", t)        # 图前断段
    t = re.sub(r"(!\[[^\]]*\]\(images/[^)]*\))[ \t]*(?=[^\n])", r"\1\n\n", t)  # 图后断段
    # ①②③④ 组合式条目被 docx 挤成一行 → 只在「项标记 + 汉字/引号」处断行（`①②③` 这种组合不拆）
    t = re.sub(r"(?<=[^\s\n①-⑳])(?=[②③④⑤⑥⑦⑧⑨⑩][\u4e00-\u9fff（(“\"'《])", "\n\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    lines = [ln.strip() for ln in t.split("\n")]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return join_broken_paragraphs(t)


END_PUNCT = "。！？；：…\"”）)」』】》"
ITEM_START = re.compile(r"^(?:[①-⑳]|[（(]\s*\d|材料[一二三四1234]|注\s*[:：]|图\s*\d|表\s*\d|--|—|!\[|\||#|\d{1,2}\s*[.．、])")


def join_broken_paragraphs(text: str) -> str:
    """docx 分页/文本框常把一句话切成两段（「…派出2000」↔「人的海军陆战队…」），接回去。"""
    paras = text.split("\n\n")
    out: list[str] = []
    for p in paras:
        s = p.strip()
        if not s:
            continue
        if out:
            prev = out[-1]
            if (len(prev) >= 15 and prev[-1] not in END_PUNCT and not prev.lstrip().startswith("|")
                    and not prev.lstrip().startswith("![")
                    and not ITEM_START.match(s)
                    and (s[0].isalnum() or s[0] in "\"“'‘「《" or "\u4e00" <= s[0] <= "\u9fff")):
                out[-1] = prev + s
                continue
        out.append(s)
    return "\n\n".join(out)


def strip_score(text: str) -> str:
    return SCORE_RE.sub("", text).strip()


def _norm_ws(text: str) -> str:
    """去掉所有空白（用来判断「设问是否还残留在材料里」）。"""
    return re.sub(r"\s+", "", text or "")


def finalize_content(text: str) -> str:
    """section.content 出口：清掉任何 <Input…> 残留（历史不该有）、空行收敛到最多一个、首尾 strip。"""
    t = PH_ANY.sub("", text or "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def md5_of(p: Path) -> str:
    import hashlib
    return hashlib.md5(p.read_bytes()).hexdigest()


# ──────────────────────────────────────────────────────────────────
# 选择题
# ──────────────────────────────────────────────────────────────────
def split_options(seg: str):
    """从题干段里切出 A–D 四项（选项可能一行挤好几个、跨行、也可能带前置图）。"""
    marks = [(m.start(), m.end(), m.group(1)) for m in OPT_MARK.finditer(seg)]
    best = None
    for i, (s, e, k) in enumerate(marks):
        if k != "A":
            continue
        seq = [(s, e, k)]
        j = i + 1
        while j < len(marks) and len(seq) < 4:
            if marks[j][2] == "ABCD"[len(seq)]:
                seq.append(marks[j])
            j += 1
        if len(seq) == 4:
            best = seq
            break
    if not best:
        return None, seg
    head = seg[: best[0][0]]
    opts = []
    for i, (s, e, k) in enumerate(best):
        end = best[i + 1][0] if i + 1 < len(best) else len(seg)
        opts.append({"id": k, "label": seg[e:end]})
    return opts, head


def table_tail(seg: str):
    """seg 末尾若是一整块 GFM 表格，返回该表格文本（否则 None）。"""
    lines = seg.rstrip("\n").split("\n")
    i = len(lines)
    while i > 0 and (not lines[i - 1].strip() or lines[i - 1].lstrip().startswith("|")):
        i -= 1
    block = "\n".join(lines[i:]).strip("\n")
    if "|" not in block:
        return None
    if not any(ln.lstrip().startswith("|") for ln in block.split("\n")):
        return None
    rest = "\n".join(lines[:i]).strip()
    if not rest:          # 整段都是表 → 不搬
        return None
    return block, rest


def parse_choices(text: str, review: list[str]):
    marks = [(m.start(), m.end(), int(m.group(2)), m.group(1)) for m in QMARK.finditer(text)]
    marks = [x for x in marks if 1 <= x[2] <= N_CHOICE + 5]
    picked, seen = [], set()
    for s, e, no, prefix in marks:
        if no in seen:
            continue
        seen.add(no)
        picked.append((s, e, no, prefix))
    picked.sort()
    raw = []
    for i, (s, e, no, prefix) in enumerate(picked):
        nxt = picked[i + 1][0] if i + 1 < len(picked) else len(text)
        seg = (prefix or "") + text[e:nxt]
        raw.append([no, seg])

    # 表格跑到题号前面（丰台第 6 题「右表展示了…」）→ 前移给引用它的那题
    for i in range(len(raw) - 1):
        got = table_tail(raw[i][1])
        if not got:
            continue
        block, rest = got
        if re.search(r"[右上下此]表|下表|上表", raw[i + 1][1][:80]):
            raw[i][1] = rest
            raw[i + 1][1] = block + "\n\n" + raw[i + 1][1]
            review.append(f"复核：第{raw[i + 1][0]}题的表格在 md 里排在第{raw[i][0]}题段尾"
                          f"（题面写「右表展示了…」），已按语义前移到第{raw[i + 1][0]}题 content")

    out = []
    for no, seg in raw:
        opts, head = split_options(seg)
        content = tidy(head)
        content = re.sub(r"^(?:[（(]\s*\d+\s*分\s*[）)])", "", content).strip()
        item = {"id": no, "questionType": "single", "content": content,
                "options": [], "answer": "", "analysis": "", "score": CHOICE_SCORE}
        if opts:
            item["options"] = [{"id": o["id"], "label": tidy(o["label"])} for o in opts]
        else:
            review.append(f"复核：第{no}题没切出 A–D 四个选项，请人工核对原卷")
        out.append(item)
    return out


# ──────────────────────────────────────────────────────────────────
# 非选择题
# ──────────────────────────────────────────────────────────────────
def split_subs(text: str):
    """材料 + 各小问（(1)(2) 行首）"""
    marks = list(SUBQ.finditer(text))
    if not marks:
        return None, None
    material = text[: marks[0].start()].strip()
    subs = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        subs.append({"no": int(m.group(1)), "text": text[m.end():end].strip()})
    return material, subs


def split_single_prompt(text: str):
    """没有 (1)(2) 的大题：最后一段带分值的文字就是设问（含其后的「要求：…」）。"""
    lines = text.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("|"):
            continue
        if SCORE_RE.search(ln) and len(ln.strip()) > 8:
            idx = i
    if idx is None:
        return text.strip(), ""
    return "\n".join(lines[:idx]).strip(), "\n".join(lines[idx:]).strip()


def parse_essays(text: str, review: list[str]):
    marks = [(m.start(), m.end(), int(m.group(2)), m.group(3), m.group(1))
             for m in QMARK.finditer(text)]
    marks = [x for x in marks if x[2] >= 16]
    picked, seen = [], set()
    for s, e, no, sc, prefix in marks:
        if no in seen:
            continue
        seen.add(no)
        picked.append((s, e, no, sc, prefix))
    picked.sort()

    out = []
    for i, (s, e, no, sc, prefix) in enumerate(picked):
        nxt = picked[i + 1][0] if i + 1 < len(picked) else len(text)
        seg = (prefix or "") + text[e:nxt]
        head, _, rest = seg.partition("\n")
        name = re.sub(r"\s+", "", strip_score(head))
        name = re.sub(r"^[（(].*?[）)]", "", name).strip()
        rest = rest.strip()
        material, subs = split_subs(rest)
        big_score = int(sc) if sc else None
        if subs is None:
            material, prompt = split_single_prompt(rest)
            subs = [{"no": None, "text": prompt}]
        else:
            prompt = None
        # 各小问分值
        scores = []
        for sb in subs:
            m = SCORE_RE.search(sb["text"])
            scores.append(int(m.group(1)) if m else None)
        total = big_score if big_score else (sum(x for x in scores if x) or None)
        if total is None:
            total = 10
            review.append(f"复核：第{no}题题面没标总分值，按 10 分处理")
        known = [x for x in scores if x]
        if None in scores:
            lack = total - sum(known)
            left = scores.count(None)
            fill = max(1, round(lack / left)) if left else 1
            scores = [x if x else fill for x in scores]
            review.append(f"复核：第{no}题有小问题面未标分值，按大题 {total} 分补足：{scores}")
        if sum(scores) != total:
            review.append(f"复核：第{no}题小问分值 {scores} 合计 {sum(scores)} ≠ 大题 {total} 分，"
                          f"差额记到最后一问")
            scores[-1] = max(1, scores[-1] + (total - sum(scores)))
        out.append({"no": no, "name": name, "material": material, "subs": subs,
                    "scores": scores, "score": total, "single": prompt is not None})
    return out


# ──────────────────────────────────────────────────────────────────
# 参考答案
# ──────────────────────────────────────────────────────────────────
def parse_choice_letters(ans_text: str, review: list[str]) -> dict[int, str]:
    head = ans_text
    m = ANS_HDR.search(ans_text)
    if m and len(ans_text[: m.start()].strip()) > 20:
        head = ans_text[: m.start()]
    got, srcs = {}, []
    for mm in INLINE_ANS.finditer(head):
        if int(mm.group(1)) <= N_CHOICE:
            got.setdefault(int(mm.group(1)), mm.group(2).upper())
    if got:
        srcs.append("行内字母串")
    tab = parse_answer_tables(head)
    if tab:
        before = len(got)
        for k, v in tab.items():
            if k <= N_CHOICE:
                got.setdefault(k, v)
        if len(got) > before or not srcs:
            srcs.append("答案表")
    if got:
        review.append(f"复核：选择题答案取自参考答案区（{'＋'.join(srcs)}），"
                      f"共 {len(got)} 个字母")
    return got


def parse_answer_tables(text: str) -> dict[int, str]:
    rows = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip().strip("*").strip() for c in s.strip("|").split("|")]
        if all((not c) or set(c) <= set("-: ") for c in cells):
            continue                      # 分隔行
        rows.append(cells)
    got = {}
    i = 0
    while i < len(rows) - 1:
        nums = [int(c) for c in rows[i] if re.fullmatch(r"\d{1,2}", c)]
        lets = [c for c in rows[i + 1] if re.fullmatch(r"[A-D]", c)]
        if nums and len(nums) == len(lets):
            for n, L in zip(nums, lets):
                got.setdefault(n, L)
            i += 2
        else:
            i += 1
    return got


def split_answer_blocks(ans: str) -> dict[int, str]:
    marks = [(m.start(), m.end(), int(m.group(1))) for m in ANS_HDR.finditer(ans)]
    picked, seen = [], set()
    for s, e, no in marks:
        if 16 <= no <= 40 and no not in seen:
            seen.add(no)
            picked.append((s, e, no))
    picked.sort()
    blocks = {}
    for i, (s, e, no) in enumerate(picked):
        end = picked[i + 1][0] if i + 1 < len(picked) else len(ans)
        eol = ans.find("\n", e)                       # 块头行剩下的（「-参考示例(乔叔版)」之类）丢掉
        blocks[no] = ans[(eol + 1 if 0 <= eol < end else e):end].strip()
    return blocks


# 答案块里单独成行的标题（「影响因素」「参考答案示例」…），不是答案正文
ANS_LABEL = re.compile(
    r"^(?:影响因素|发展特点|原因|表现|特点|影响|作用|意义|启示|示例[一二三四]|背景|概况|"
    r"内容|评价|方式|方法|延续|创新|观点|阐释|总结|总评|参考答案(?:要点|示例)?|答案)$")


def strip_ans_head(t: str) -> str:
    t = t.strip()
    for _ in range(3):
        lines = t.split("\n", 1)
        first = lines[0].strip().rstrip("：:")
        if HEAD_ANS.match(lines[0].strip()) or (
                len(lines) > 1 and ANS_LABEL.match(first) and "。" not in first):
            t = lines[1].lstrip() if len(lines) > 1 else ""
        else:
            break
    return t.strip()


def split_answer_subs(text: str, n: int):
    """按 (1)(2) 把答案块切成 n 段；海淀的 `（4分）（2）` 挤在同一行，也认。"""
    if n <= 1:
        return [strip_ans_head(re.sub(r"^\s*[（(]\s*1\s*[）)]\s*", "", text.strip()))]
    marks = []
    for m in SUB_ANS.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        head = text[line_start:m.start()]
        ok = not head.strip()
        if not ok and head.rstrip().endswith(("）", ")")) and len(head.strip()) <= 40:
            ok = True
        if ok:
            marks.append(m)
    seq, cur = [], 1
    for m in marks:
        if int(m.group(1)) == cur:
            seq.append(m)
            cur += 1
            if cur > n:
                break
    if len(seq) != n:
        return None
    out = []
    for i, m in enumerate(seq):
        end = seq[i + 1].start() if i + 1 < len(seq) else len(text)
        out.append(strip_ans_head(text[m.end():end]))
    return out


# ──────────────────────────────────────────────────────────────────
# 分类
# ──────────────────────────────────────────────────────────────────
def classify_rules(text: str):
    """返回 (标签, 判据)；判据为 None 表示规则没命中，需要兜底。"""
    if KW_FIX.search(text):
        return "修改补写", "设问要求修改/补写"
    if KW_ARGUE.search(text):
        return "观点论证", "设问要求阐述/评析/点评"
    if KW_VIEW.search(text) and not KW_SOURCE.search(text):
        return "观点论证", "设问要求谈看法/理解"
    if KW_SOURCE.search(text):
        return "史料分析", "设问依据材料归纳/概括/说明/分析"
    return None, None


def classify_essays(essays, args, review, cache: Path | None = None):
    """规则优先；规则判不出来的那几题才调一次模型（--no-ai 时落第一个标签）。"""
    got, low = {}, []
    for e in essays:
        text = " ".join(sb["text"] for sb in e["subs"])
        lab, why = classify_rules(text)
        if lab:
            got[e["no"]] = (lab, why)
        else:
            low.append((e, text))
    if low and not args.no_ai:
        import convert_chinese_md as C
        items = "\n".join(f"- {e['no']}｜{re.sub(r'[!\\[\\]()]', ' ', t)[:150]}" for e, t in low)
        prompt = ("下面是一份北京高三历史一模试卷的若干非选择题（题号｜设问摘要）。"
                  "请为每题判断题型标签，只能从这 3 个里选一个：\n"
                  "观点论证（要求阐述/评析/论证某个观点或谈谈看法）、"
                  "史料分析（依据材料归纳概括、解释分析、比较史实、辨析史料）、"
                  "修改补写（要求修改或补写材料/答案）。\n\n"
                  '只输出 JSON：{"题号": "标签"}\n\n题目：\n' + items)
        try:
            data = C.chat([{"role": "system", "content": "你是历史试卷录入员，只输出 JSON。"},
                           {"role": "user", "content": prompt}], 1200)
        except Exception as e:                                    # 网关不通也不能崩
            data = {}
            review.append(f"复核：分类模型调用失败（{str(e)[:60]}），规则未命中的题落默认标签")
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    no = int(re.sub(r"\D", "", str(k)))
                except Exception:
                    continue
                v = str(v).strip()
                if v in CATS:
                    got[no] = (v, "模型判定")
                else:
                    got[no] = (CATS[0], f"模型返回不合法标签「{v}」，落默认")
                    review.append(f"复核：第{no}题分类模型返回「{v}」（不在 {'/'.join(CATS)} 里），"
                                  f"已落默认标签 {CATS[0]}")
    for e in essays:
        if e["no"] not in got:
            got[e["no"]] = (CATS[0], "规则未命中，落默认标签（--no-ai 或模型不可用）")
            review.append(f"复核：第{e['no']}题分类规则未命中，落默认标签 {CATS[0]}，请人工确认")
    for no, (lab, why) in sorted(got.items()):
        if no in CATEGORY_OVERRIDE.get("", {}):
            lab, why = CATEGORY_OVERRIDE[""][no], "人工指定"
        review.append(f"复核：分类 第{no}题 = {lab}（{why}）")
    return {no: lab for no, (lab, _) in got.items()}


# ──────────────────────────────────────────────────────────────────
# 图片：按卷修正 + 改写绝对地址
# ──────────────────────────────────────────────────────────────────
def _tok_re(fname: str):
    return re.compile(r"!\[[^\]]*\]\(" + re.escape(fname) + r"\)")


def take_image(rec: dict, fname: str) -> bool:
    pat = _tok_re(fname)
    hit = False
    if pat.search(rec.get("content") or ""):
        rec["content"] = pat.sub("", rec["content"])
        hit = True
    for o in rec.get("options") or []:
        if pat.search(o.get("label") or ""):
            o["label"] = pat.sub("", o["label"]).strip()
            hit = True
    if pat.search(rec.get("answer") or ""):
        rec["answer"] = pat.sub("", rec["answer"])
        hit = True
    return hit


def apply_image_fixes(title: str, choices: list[dict], review: list[str]):
    byid = {q["id"]: q for q in choices}
    for fname, frm, to, why in IMAGE_FIX.get(title, []):
        src, dst = byid.get(frm), byid.get(to)
        if not src or not dst:
            review.append(f"复核：图片修正表里第{frm}→{to}题找不到对应题目（{fname}），请人工确认")
            continue
        if not take_image(src, fname):
            review.append(f"复核：图片修正表里 {fname} 不在第{frm}题里（可能 md 已变），请人工确认")
            continue
        dst["content"] = (f"![]({fname})\n\n" + (dst.get("content") or "")).strip()
        src["content"] = (src.get("content") or "").strip()
        review.append(f"复核：图片错位修正 {fname}：第{frm}题 → 第{to}题。{why}")
    for no, fname, why in DROP_IMAGE.get(title, []):
        q = byid.get(no)
        if q and take_image(q, fname):
            q["content"] = (q.get("content") or "").strip()
            review.append(f"复核：图片去重 第{no}题 删除 {fname}。{why}")


def fix_two_col_tables(text: str, where: str, review: list[str]) -> str:
    """教师版两列表格串行（续行把引文塞进了第一列）→ 合并回上一行第二列。"""
    lines = text.split("\n")
    out, prev_row = [], None
    changed = 0
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            out.append(ln)
            prev_row = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all((not c) or set(c) <= set("-: ") for c in cells):
            out.append(ln)
            prev_row = None
            continue
        if prev_row is not None and len(prev_row) == 2:
            add = ""
            if len(cells) == 1 and cells[0]:
                add = cells[0]                      # 整行只剩一个单元格：续行引文
            elif len(cells) == 2 and cells[0] and not cells[1]:
                add = cells[0]                      # 引文塞进了第一列、第二列空着
            if add:
                out[-1] = out[-1].rstrip().rstrip("|").rstrip() + "<br><br>" + add + " |"
                changed += 1
                continue
        out.append(ln)
        prev_row = cells
    if changed:
        review.append(f"复核：{where} 的两列表格有 {changed} 行续行把引文塞进了第一列（docx 串行），"
                      f"已合并回上一行的观点列（`<br><br>` 分隔）")
    return "\n".join(out)


def rewrite_all(recs: list[dict], md_dir: Path, title: str, type_dir: str, review: list[str]):
    for rec in recs:
        if rec.get("content"):
            rec["content"] = rewrite_images(rec["content"], md_dir, title, type_dir, review, SUBJECT)
        if rec.get("answer"):
            rec["answer"] = rewrite_images(rec["answer"], md_dir, title, type_dir, review, SUBJECT)
        for o in rec.get("options") or []:
            o["label"] = rewrite_images(o["label"], md_dir, title, type_dir, review, SUBJECT)


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────
def process(md: Path, type_dir: str, args) -> str:
    title = clean_title(md.stem)
    dst = Path(OUT["root"]) / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"

    txt = md.read_text(encoding="utf-8")
    body, ans = split_paper(txt)
    if not ans:
        raise RuntimeError("没找到「参考答案」区")

    m2 = PART2.search(body)
    if not m2:
        raise RuntimeError("没找到「第二部分」")
    m1 = PART1.search(body)
    body1 = body[(m1.start() if m1 else 0): m2.start()]
    body2 = body[m2.start():]

    review: list[str] = []
    choice_qs = parse_choices(body1, review)
    essays = parse_essays(body2, review)
    if len(choice_qs) != N_CHOICE:
        review.append(f"复核：选择题切出 {len(choice_qs)} 道（卷面声明 {N_CHOICE} 道），请人工核对")
    if not essays:
        raise RuntimeError("第二部分没切到大题")

    apply_image_fixes(title, choice_qs, review)

    # 选择题答案
    letters = parse_choice_letters(ans, review)
    for q in choice_qs:
        q["answer"] = letters.get(q["id"], "")
        if not q["answer"]:
            review.append(f"复核：第{q['id']}题在参考答案区找不到字母答案")

    # 非选择题答案
    blocks = split_answer_blocks(ans)
    cats = classify_essays(essays, args, review)
    sections_essay = []
    for e in essays:
        no = e["no"]
        blk = blocks.get(no, "")
        if not blk:
            review.append(f"复核：第{no}题在参考答案区找不到对应块")
        subs_a = split_answer_subs(blk, len(e["subs"])) if blk else None
        if subs_a is None:
            review.append(f"复核：第{no}题参考答案块里按 (1)(2) 切不开 {len(e['subs'])} 段，"
                          f"整块放进第一问，其余留空待人工补")
            subs_a = [blk] + [""] * (len(e["subs"]) - 1)
        questions = []
        for i, sb in enumerate(e["subs"]):
            prompt = strip_score(sb["text"]).strip()
            qid = int(f"{no}{i + 1:02d}") if sb["no"] is not None else int(f"{no}01")
            ctext = f"（{sb['no']}）{prompt}" if sb["no"] is not None else prompt
            questions.append({
                "id": qid, "questionType": "input",
                "content": tidy(ctext), "answer": (subs_a[i] or "").strip(),
                "analysis": "", "score": e["scores"][i],
            })
            if not (subs_a[i] or "").strip():
                review.append(f"复核：第{no}题（{sb['no'] or 1}）参考答案为空，请人工补")

        # section.content：**单栏不变**——材料 + 各小问设问内联，每个设问后紧跟一个 <AnswerBox> 答题框
        parts = []
        material = tidy(e["material"])
        if material:
            material = fix_two_col_tables(material, f"第{no}题材料", review)
            parts.append(material)
        for sb in e["subs"]:
            prompt = strip_score(sb["text"]).strip()
            lead = f"（{sb['no']}）" if sb["no"] is not None else ""
            sep = "\n\n" if "\n" in prompt else ""     # 多段设问（含「要求：…」）答题框另起一段
            parts.append(f"{lead}{prompt}{sep}{ANSWER_BOX}")
        content = finalize_content("\n\n".join(parts))
        if not material:
            review.append(f"复核：第{no}题只切到设问、没切到材料，请人工核对材料是否漏抽")

        n_img = len(IMG_TOKEN.findall(content))
        n_tab = count_tables(content)
        if n_img or n_tab:
            review.append(f"复核：第{no}题 content 含 图 {n_img} 张 / 表 {n_tab} 张，"
                          f"请人工核对图表与设问的对应关系")
        sections_essay.append({
            "type": "非选择题", "questionType": "history-fill",
            "title": f"二、非选择题（第{no}题）{e['name']}" if e["name"] else f"二、非选择题（第{no}题）",
            "category": cats.get(no, CATS[0]), "score": e["score"],
            "content": content, "questions": questions,
        })

    # 选择题 section
    sections = []
    ch_score = sum(q["score"] for q in choice_qs)
    sections.append({
        "type": "选择题", "questionType": "history-choice", "title": "一、选择题",
        "category": "选择", "score": ch_score, "questions": choice_qs,
    })
    sections += sections_essay

    # 图片改写（统一出口）
    rewrite_all(choice_qs, md.parent, title, type_dir, review)
    for s in sections_essay:
        rewrite_all(s["questions"], md.parent, title, type_dir, review)
        s["content"] = rewrite_images(s["content"], md.parent, title, type_dir, review, SUBJECT)

    # 卷面图题留给人工
    for q in choice_qs:
        if IMG_TOKEN.search(q["content"]) or any(IMG_TOKEN.search(o["label"]) for o in q["options"]):
            review.append(f"复核：第{q['id']}题含图（docx 导出图），请人工核对清晰度与标注是否可读")

    review.append("复核：答题框已换成 <AnswerBox></AnswerBox>（单栏版式不变：材料+设问仍内联在 "
                  "section.content，每个小问后跟一个答题框；小问仍是 questionType=input；"
                  "全文无 <InputLong>/<Input> 残留，高度由前端按档位给）")
    review.extend(f"复核：{x}" for x in STATIC_NOTES.get(title, []))

    total = sum(s["score"] for s in sections)
    year = re.search(r"(20\d{2})", title)
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": args.grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": total, "duration": 90,
        "tags": [SUBJECT, args.grade, type_dir] + ([year.group(1)] if year else []),
    }
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rev = dst.with_suffix(".review.txt")
    rev.write_text("\n".join(review) + "\n", encoding="utf-8")

    n_q = sum(len(s["questions"]) for s in sections)
    return (f"✓ {title}：{len(sections)} sections / {n_q} 题"
            f"（选择 {len(choice_qs)}×{CHOICE_SCORE}={ch_score}，非选择 {len(sections_essay)} 大题），"
            f"总分 {total}，review {len(review)} 条")


# ──────────────────────────────────────────────────────────────────
# 自查（纯 Python，不调模型）
# ──────────────────────────────────────────────────────────────────
def paper_dirs_for(paper: str, type_dir: str, subject: str = SUBJECT) -> list[Path]:
    parent = MD_ROOT / type_dir / subject
    if not parent.exists():
        return []
    out = []
    for d in sorted(parent.iterdir()):
        if not d.is_dir():
            continue
        base = re.sub(r"[（(]教师版[）)]|[（(]含答案[）)]", "", d.name).strip()
        if base == paper or d.name == paper:
            out.append(d)
    return out


def img_exists(ref: str) -> bool:
    """/exam-images/<类型>/历史/<卷名>/images/x.png → 落到磁盘（卷目录带「（教师版）」也要命中）"""
    if not ref.startswith("/exam-images/"):
        return False
    parts = ref[len("/exam-images/"):].split("/")
    if len(parts) < 5:
        return False
    type_dir, subject, paper, _imgdir, fname = parts[0], parts[1], parts[2], parts[3], parts[4]
    for d in paper_dirs_for(paper, type_dir, subject):
        if (d / "images" / fname).exists():
            return True
    direct = MD_ROOT / type_dir / subject / paper / "images" / fname
    return direct.exists()


def table_shape_errors(text: str) -> list[str]:
    errs = []
    lines = [ln.strip() for ln in text.split("\n") if ln.strip().startswith("|")]
    i = 0
    while i < len(lines):
        block = []
        while i < len(lines) and lines[i].startswith("|"):
            block.append(lines[i])
            i += 1
        if len(block) < 2:
            errs.append(f"表格只有 {len(block)} 行（不成表）：{block[0][:40] if block else ''}")
            continue
        cols = [len(ln.strip().strip('|').split('|')) for ln in block]
        sep = [k for k, ln in enumerate(block)
               if all((not c.strip()) or set(c.strip()) <= set("-: ") for c in ln.strip().strip("|").split("|"))]
        if len(sep) != 1:
            errs.append(f"表格分隔行 {len(sep)} 个（应为 1）：{block[0][:40]}")
            continue
        if len(set(cols)) != 1:
            errs.append(f"表格各行列数不一致 {sorted(set(cols))}：{block[0][:40]}")
    return errs


def count_tables(text: str) -> int:
    """GFM 表格张数 = 分隔行个数。"""
    n = 0
    for ln in text.split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = s.strip("|").split("|")
        if all((not c.strip()) or set(c.strip()) <= set("-: ") for c in cells):
            n += 1
    return n


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    stat, errs, notes = {}, [], []
    raw = jf.read_text(encoding="utf-8")
    data = json.loads(raw)
    p = data.get("paper") or {}
    secs = data.get("sections") or []
    stat["title"] = p.get("title") or jf.stem

    if not secs:
        errs.append("没有 sections")
        return stat, errs, notes
    if p.get("subject") != SUBJECT:
        errs.append(f"paper.subject={p.get('subject')} ≠ {SUBJECT}")
    if p.get("totalScore") != 100:
        errs.append(f"paper.totalScore={p.get('totalScore')}（历史一模应为 100）")
    if p.get("duration") != 90:
        notes.append(f"paper.duration={p.get('duration')}")

    ch = [s for s in secs if s.get("type") == "选择题"]
    es = [s for s in secs if s.get("type") != "选择题"]
    if len(ch) != 1:
        errs.append(f"选择题 section 有 {len(ch)} 个（应为 1）")
    stat["choice"] = sum(len(s.get("questions") or []) for s in ch)
    stat["essay"] = len(es)
    stat["blanks"] = sum(len(s.get("questions") or []) for s in es)

    # 选择题
    for s in ch:
        if s.get("questionType") != "history-choice":
            errs.append(f"选择题 questionType={s.get('questionType')}")
        if s.get("category") != "选择":
            errs.append(f"选择题 category={s.get('category')}（应为 选择）")
        ids = [q.get("id") for q in s["questions"]]
        if ids != list(range(1, N_CHOICE + 1)):
            errs.append(f"选择题题号不连续：{ids[:3]}…{ids[-3:] if len(ids) > 3 else ''}")
        ssum = 0
        for q in s["questions"]:
            ssum += q.get("score") or 0
            opts = q.get("options") or []
            oids = [str(o.get("id")) for o in opts]
            if oids != ["A", "B", "C", "D"]:
                errs.append(f"第{q.get('id')}题 options={oids}（应为 A–D）")
            ans = str(q.get("answer") or "")
            if len(ans) != 1 or ans != ans.upper() or ans not in oids:
                errs.append(f"第{q.get('id')}题 answer={ans!r}（应为 A–D 单大写字母且 ∈ options）")
            if not (q.get("content") or "").strip():
                errs.append(f"第{q.get('id')}题 content 为空")
            for lt in "ABCD":
                if not any(o.get("id") == lt and (o.get("label") or "").strip() for o in opts):
                    errs.append(f"第{q.get('id')}题选项 {lt} 为空")
            if (q.get("score") or 0) != CHOICE_SCORE:
                errs.append(f"第{q.get('id')}题 score={q.get('score')}（应为 {CHOICE_SCORE}）")
        if ssum != (s.get("score") or 0):
            errs.append(f"选择题 Σscore {ssum} ≠ section.score {s.get('score')}")
        if s.get("score") != N_CHOICE * CHOICE_SCORE:
            errs.append(f"选择题 section.score={s.get('score')}（应为 {N_CHOICE * CHOICE_SCORE}）")

    # 非选择题
    nos = []
    for s in es:
        m = re.search(r"第\s*(\d+)\s*题", s.get("title") or "")
        if not m:
            errs.append(f"section.title 里没有「第N题」：{s.get('title')!r}")
        else:
            nos.append(int(m.group(1)))
        if s.get("questionType") != "history-fill":
            errs.append(f"{s.get('title')} questionType={s.get('questionType')}")
        if s.get("category") not in CATS:
            errs.append(f"{s.get('title')} category={s.get('category')!r}（只能取 {'/'.join(CATS)}）")
        content = s.get("content") or ""
        qs = s.get("questions") or []
        # 大题：单栏形态——材料 + 设问内联，每个小问配一个 <AnswerBox></AnswerBox> 答题框
        n_box = content.count("<AnswerBox")
        if n_box != len(qs):
            errs.append(f"{s.get('title')} <AnswerBox 占位符 {n_box} 个 ≠ 小问 {len(qs)} 个")
        if "<Input" in content or PH_ANY.search(content):
            errs.append(f"{s.get('title')} section.content 里还有 <Input/<InputLong 残留"
                        f"（应全部换成 <AnswerBox>）")
        if content.count(ANSWER_BOX) != n_box:
            errs.append(f"{s.get('title')} 有 {n_box - content.count(ANSWER_BOX)} 个 <AnswerBox "
                        f"没写成成对形式")
        ssum = sum(q.get("score") or 0 for q in qs)
        if ssum != (s.get("score") or 0):
            errs.append(f"{s.get('title')} Σquestions.score {ssum} ≠ section.score {s.get('score')}")
        if not content.strip():
            errs.append(f"{s.get('title')} content 为空（材料没进来）")
        ids = set()
        sec_body = _norm_ws(content)
        for q in qs:
            # ① 小问仍是 input（AI 批改的 {stem} 口径不变；只有答题框换成 <AnswerBox>）
            if q.get("questionType") != "input":
                errs.append(f"{s.get('title')} id={q.get('id')} questionType={q.get('questionType')}"
                            f"（应为 input）")
            if not (q.get("answer") or "").strip():
                errs.append(f"{s.get('title')} id={q.get('id')} answer 为空")
            if (q.get("score") or 0) <= 0:
                errs.append(f"{s.get('title')} id={q.get('id')} score={q.get('score')}（不能 ≤ 0）")
            qc = (q.get("content") or "").strip()
            if not qc:
                errs.append(f"{s.get('title')} id={q.get('id')} content 为空")
            if q.get("id") in ids:
                errs.append(f"重复 id {q.get('id')}")
            ids.add(q.get("id"))
            # ② 设问在单栏里是内联的 → 必须出现在 section.content 里一次（防漏拼/重复）
            probe = _norm_ws(qc)
            if probe and sec_body.count(probe) != 1:
                errs.append(f"{s.get('title')} id={q.get('id')} 的设问在 section.content 里出现 "
                            f"{sec_body.count(probe)} 次（应为 1 次）：{qc[:24]}…")
            if "<Input" in qc or PH_ANY.search(qc):
                errs.append(f"{s.get('title')} id={q.get('id')} 的 content 里有 <Input…> 残留")
            # ③ 材料不该复制进小问（政治老流水线的 bug）
            if re.search(r"材料[一二三四][：:]|材料[一二三四]\s", qc):
                errs.append(f"{s.get('title')} id={q.get('id')} 小问 content 里混进了材料")
        for e in table_shape_errors(content):
            errs.append(f"{s.get('title')} {e}")
        for q in qs:
            for e in table_shape_errors(q.get("answer") or ""):
                errs.append(f"{s.get('title')} id={q.get('id')} 答案里 {e}")
    if nos and nos != list(range(min(nos), min(nos) + len(nos))):
        errs.append(f"大题题号不连续：{nos}")
    if nos and min(nos) != 16:
        errs.append(f"大题题号从 {min(nos)} 开始（应为 16）")

    # 分值合计
    paper_sum = sum(s.get("score") or 0 for s in secs)
    if paper_sum != (p.get("totalScore") or 0):
        errs.append(f"题组合计 {paper_sum} ≠ paper.totalScore {p.get('totalScore')}")

    # 图片逐张落盘
    refs = sorted(set(EXAM_IMG.findall(raw)))
    bad = [r for r in refs if not img_exists("/exam-images/" + r)]
    for r in bad:
        errs.append(f"图片不存在：/exam-images/{r}")
    stat["imgs"] = len(refs)

    # 未引用的图片（提示，不算错）
    pdirs = paper_dirs_for(stat["title"], "一模")
    if pdirs:
        used = {r.split("/")[-1] for r in refs}
        for f in sorted((pdirs[0] / "images").glob("*")):
            if f.name not in used:
                notes.append(f"图片未被引用：{f.name}")

    # review 存在
    if not jf.with_suffix(".review.txt").exists():
        notes.append("没有 review.txt")
    return stat, errs, notes


def run_check(args) -> int:
    types = [t.strip() for t in (args.type or "一模").replace("，", ",").split(",") if t.strip()]
    if "all" in types:
        types = ["一模"]
    root = Path(OUT["root"])
    bad, rows = 0, []
    for type_dir in types:
        files = sorted((root / type_dir).glob("*.json"))
        if not files:
            print(f"没有找到 JSON：{root / type_dir}")
            bad += 1
            continue
        for jf in files:
            try:
                stat, errs, notes = check_one(jf)
            except Exception as e:
                print(f"\n===== {type_dir}/{jf.name} =====")
                print(f"  ✗ 读取/解析失败：{str(e)[:160]}")
                bad += 1
                rows.append((type_dir, jf.stem, "—", "—", "—", "✗", 1))
                continue
            rev = jf.with_suffix(".review.txt")
            n_rev = len([x for x in rev.read_text(encoding="utf-8").splitlines() if x.strip()]) if rev.exists() else 0
            print(f"\n===== {type_dir}/{jf.name} =====")
            print(f"选择题 {stat.get('choice')} 题 / 大题 {stat.get('essay')} 道 / 小问 {stat.get('blanks')} 个"
                  f" / 图片引用 {stat.get('imgs')} 张 / review {n_rev} 条")
            secs = json.loads(jf.read_text(encoding="utf-8"))["sections"]
            for s in secs:
                qs = s.get("questions") or []
                print(f"   · {s.get('title')}｜{s.get('category')}｜{s.get('score')} 分"
                      f"｜{len(qs)} 题｜Σ{sum(q.get('score') or 0 for q in qs)}")
            if errs:
                bad += 1
                print(f"  ✗ 错误 {len(errs)} 条：")
                for e in errs:
                    print(f"     - {e}")
            else:
                print("  ✓ 契约校验通过")
            for n in notes:
                print(f"  · 提示：{n}")
            rows.append((type_dir, stat.get("title"), stat.get("choice"), stat.get("essay"),
                         stat.get("blanks"), "✓" if not errs else "✗", len(errs)))
    print("\n| 类型 | 卷 | 选择 | 大题 | 小问 | 结论 | 错误数 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |")
    print(f"\n合计 {len(rows)} 份，出错 {bad} 份")
    return 0 if bad == 0 else 1


# ──────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────
def collect_files(p: Path | None, type_dir: str) -> list[Path]:
    if p is not None:
        if p.is_dir():
            direct = sorted(p.glob("*.md"))
            if direct:
                return direct
            return [sorted(d.glob("*.md"))[0] for d in sorted(p.iterdir())
                    if d.is_dir() and sorted(d.glob("*.md"))]
        return [p]
    root = MD_ROOT / type_dir / SUBJECT
    if not root.exists():
        return []
    return [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir())
            if d.is_dir() and sorted(d.glob("*.md"))]


def main():
    ap = argparse.ArgumentParser(description="历史试卷 MD → 结构化 JSON（生物式结构）")
    ap.add_argument("-p", "--path", default=None, help="md 文件或卷目录（卷目录=含多份卷）")
    ap.add_argument("-T", "--type", default="一模", help="试卷类型（输出子目录，如 一模/二模）")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("--force", action="store_true", help="已存在也重跑")
    ap.add_argument("--no-ai", dest="no_ai", action="store_true",
                    help="不调模型（分类走规则；规则没命中的落默认标签并写 review）")
    ap.add_argument("-w", "--workers", type=int, default=1,
                    help="兼容现有 CLI；本机沙箱下恒为单进程（参数保留但忽略）")
    ap.add_argument("--check", action="store_true", help="只自查已生成的 JSON（纯 Python，不调模型）")
    args = ap.parse_args()

    OUT["root"] = Path(args.output)
    if args.check:
        raise SystemExit(run_check(args))

    files = collect_files(Path(args.path) if args.path else None, args.type)
    if not files:
        log(f"没有找到 MD：{args.path or MD_ROOT / args.type / SUBJECT}")
        raise SystemExit(1)
    log(f"共 {len(files)} 份 MD（单文件模式）")
    ok = 0
    for f in files:
        try:
            log("  " + process(f, args.type, args))
            ok += 1
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")
    log(f"完成 {ok}/{len(files)} 份 → {Path(args.output) / args.type}")
    raise SystemExit(0 if ok == len(files) else 1)


if __name__ == "__main__":
    main()
