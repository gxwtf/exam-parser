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

# 个别卷的参考答案在教师版里只有扫描图（无文字层）→ 人工转录的答案放这里，按卷名加载
MANUAL_ANS_DIR = Path(__file__).resolve().parent / "manual_answers"


def load_manual_answers(title: str):
    f = MANUAL_ANS_DIR / f"{title}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


SUBJECT = "历史"
OUT_ROOT = OUTPUT_JSON_HISTORY
OUT = {"root": OUT_ROOT}
MD_ROOT = OUTPUT_DOCX_TO_MD

N_CHOICE = 15          # 默认档位（北京高三常规卷 15 题×3 分）；实际按卷面自适应
CHOICE_SCORE = 3       # 同上
N_ESSAY = 5
CATS = ["观点论证", "史料分析", "修改补写"]

# 兜底分类用到的关键词（确定性规则，见 classify_essays）
KW_FIX = re.compile(r"修改|补写|订正|改正|添补|补充完整|将.{0,12}补充")
KW_ARGUE = re.compile(r"阐述|论述|评析|评论|点评|阐释|加以论证")
KW_VIEW = re.compile(r"谈谈.{0,6}(?:看法|认识|理解)|你的(?:看法|认识|理解)|如何认识")
KW_SOURCE = re.compile(r"考证|史料|文献|辨析|依据材料|据材料|材料[一二三四1234]|归纳|概括"
                       r"|指出|比较|说明|分析|简述|列举|划分")

QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\**\s*\\?[．.、]\s*\**\s*"
    r"(?:[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)])?[ \t]*"
)
# 第一/第二部分：覆盖 2026 各区期末、二模、以及 2011–2026 高考真题的各种写法
#   「第一部分」「第一部分.本部分共12小题」「一、选择题」「一.本部分共15题」「一、单选题」
PART1 = re.compile(
    r"(?m)^[ \t]*\**\s*(?:第[ \t]*[一1][ \t]*部[ \t]*分"
    r"|一[、.．]\s*(?:第[ \t]*[一1][ \t]*部[ \t]*分)?\s*(?:单选题?|选择题|本部分))"
)
#   「第二部分」「第二部分（非选择题共160分）」「二部分」「二、非选择题」「二、材料题」「二、解答题」
PART2 = re.compile(
    r"(?m)^[ \t]*\**\s*(?:第[ \t]*[二2][ \t]*部[ \t]*分|二[ \t]*部[ \t]*分"
    r"|二[、.．]\s*(?:第[ \t]*[二2][ \t]*部[ \t]*分)?\s*(?:非选择题|材料题|解答题|本部分))"
)
OPT_MARK = re.compile(r"(?:^|(?<=[\s\u3000\u00a0>)]))([A-D])(?:[ \t]*[．.、][ \t]*)", re.M)
SUBQ = re.compile(r"(?m)^[ \t]*[（(]\s*([1-9])\s*[）)]")
# 材料标题（行首，允许 ** 加粗与空格）：材料一/材料二/…
MAT_MARK = re.compile(r"(?m)^[ \t]*(?:\**\s*|!\[[^\]]*\]\([^)]*\)[ \t]*)*材料[一二三四1234]")
SUB_ANS = re.compile(r"[（(]\s*([1-9])\s*[）)]")
SCORE_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]")
INLINE_ANS = re.compile(r"(?<![\dA-Za-z])(\d{1,2})\s*\**\s*\\?[.．、]\s*\**\s*([A-D])\**(?![A-Za-z])")
# 答案块题头：16 题 / 16.(10分) / 16．（12分）参考答案要点： / 16\. 【答案】… / 16．【分析】… / 21.（10分）
ANS_HDR = re.compile(
    r"(?m)^[ \t]*\**\s*(\d{1,2})\s*(?:\**\s*题(?![一二三四五六七八九十])"
    r"|\\?[．.、]\s*\**\s*(?:[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]|【|参考答案|答案|评分|$))"
)
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
# 卷名 → [(原题号, 切分正则, 新题号, 说明)]：原卷某题的题号/题干漏印，选项直接跟在前一题后面
SPLIT_TAIL_CHOICE: dict[str, list[tuple[int, str, int, str]]] = {
    "2025北京门头沟高三一模历史": [
        (9, r"(?m)^\|\s*XX\s*社区", 10,
         "第10题的题号与题干在教师版 PDF 里就没有印（表格直接接在第9题选项后面），"
         "已按表格起头把「表格+①②③④+选项」切出来作为第10题；题干缺失需人工补"),
    ],
}


# 卷名 → [(题号, 图片相对路径, 说明)]：该题的 A–D 四个选项在同一张图里（等分裁成四份当选项）
OPTION_IMAGE_SPLIT: dict[str, list[tuple[int, list[tuple[str, int]], str]]] = {
    "2023北京朝阳高三一模历史": [
        (4, [("images/image4.png", 4)], "第4题的四个选项是同一张图里的四幅图（A–D 依次横排），已等分裁切"),
        (6, [("images/image5.png", 4)], "第6题的四个选项是同一张图里的四幅图（A–D 依次横排），已等分裁切"),
    ],
    "2025北京朝阳高三（上）期末历史": [
        (4, [("images/image1.png", 4)], "第4题的四个女神像在同一张图里（A–D 依次横排），已等分裁切"),
    ],
    "2021北京丰台高三一模历史": [
        (9, [("images/image3.png", 2), ("images/image4.png", 2)],
         "第9题的 A/B 是一张图里的两幅、C/D 是另一张图里的两幅，已按等分裁成四个选项图"),
    ],
    "2021北京房山高三二模历史": [
        (15, [("images/image5.png", 4)], "第15题的四个选项是同一张图里的四本封面（A–D 依次横排），已等分裁切"),
    ],
}

# 卷名 → {题号: [A,B,C,D 选项文字]}：选项是一整张表格图时，人工按图转录成文字
OPTION_TEXT_OVERRIDE: dict[str, dict[int, list[str]]] = {
    # 选项是「同一张图里的 A/B/C/D 标注位置」（2021 通州期末第 13 题：一张欧洲地图标了四个点）
    "2021北京通州高三（上）期末历史": {
        13: ["图4中标注A的国家", "图4中标注B的国家", "图4中标注C的国家", "图4中标注D的国家"],
    },
    "2021北京平谷高三一模历史": {
        12: [
            "史实：罗马曾三次征服世界，但唯有法律征服世界是最为持久的征服；结论：罗马法有效地维护了罗马的统治",
            "史实：1723年沃波尔拒绝了英王授予的贵族爵位，因为他接受封爵就必须放弃在下院的席位；结论：说明此时下院的权力已经超过上院",
            "史实：1847年正义者同盟改为共产主义者同盟，并提出全世界无产者联合起来的战斗口号；结论：表明国际工人运动开始有更为科学的理论指导",
            "史实：1910年德国将当时所实行的各种保险法汇成《工人保险法典》；结论：德国社会保障体系趋于完善",
        ],
    },
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


def _tidy_prose(t: str) -> str:
    """非表格文字：图片独占一段、①②③④ 条目断行。"""
    t = re.sub(r"\)[ \t]*(?=!\[)", ")\n\n", t)          # 相邻两张图拆成两段
    t = re.sub(r"(?<=[。！？；：」）])(?=!\[)", "\n\n", t)
    t = re.sub(r"(?<=[^\n])[ \t]*(?=!\[[^\]]*\]\(images/)", "\n\n", t)        # 图前断段
    t = re.sub(r"(!\[[^\]]*\]\(images/[^)]*\))[ \t]*(?=[^\n])", r"\1\n\n", t)  # 图后断段
    # ①②③④ 组合式条目被 docx 挤成一行 → 只在「项标记 + 汉字/引号」处断行（`①②③` 这种组合不拆）
    t = re.sub(r"(?<=[^\s\n①-⑳])(?=[②③④⑤⑥⑦⑧⑨⑩][\u4e00-\u9fff（(“\"'《])", "\n\n", t)
    return t


def tidy(text: str) -> str:
    """段落规整：压空行、图片独占一段、去掉行首行尾空白。

    **GFM 表格行必须整行保留**：2026 期末/二模卷的表格单元格里常内嵌图片，
    按「图前/图后断段」把图片拎成独立段落会把表格拆成两截（分隔行错位）。
    """
    if not text:
        return ""
    t = text.replace("\r", "")
    t = re.sub(r"[ \t\u3000]+(?=\n)", "", t)
    segs: list[list[str]] = []            # [is_table, 行列表]
    for ln in t.split("\n"):
        is_tab = ln.lstrip().startswith("|")
        if segs and segs[-1][0] == is_tab:
            segs[-1][1].append(ln)
        else:
            segs.append([is_tab, [ln]])
    out_parts = []
    for is_tab, lines in segs:
        seg = "\n".join(lines)
        out_parts.append(seg if is_tab else _tidy_prose(seg))
    t = "\n".join(out_parts)
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


STEM_SECTION_RESIDUE = re.compile(
    r"(?m)^[ \t]*(?:\*\*|__)?[ \t]*"
    r"(?:[一二三四1234][、.．]\s*)?(?:选择题|非选择题|材料题|解答题|单选题|判断题)"
    r"[ \t]*(?:\*\*|__)?[ \t]*$")


def strip_stem_residue(text: str) -> str:
    """题干开头独占的分区标题（`**一、选择题**` 之类）是 docx 串行残留 → 从题干里剔掉。
    只处理**开头独占行**，正文中间不动。"""
    t = text or ""
    hit = False
    for _ in range(3):
        lines = t.split("\n")
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and STEM_SECTION_RESIDUE.match(lines[i]):
            del lines[i]
            t = "\n".join(lines)
            hit = True
            continue
        break
    t = t.strip()
    if hit:
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


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
# 卷面参数自适应（答案区切分 / 题数 / 每题分值）
# ──────────────────────────────────────────────────────────────────
DECL_EACH = re.compile(r"每[题小]\s*题?\s*([\d.]+)\s*分")
DECL_N = re.compile(r"共\s*(\d{1,2})\s*[题小]")
DECL_TOTAL = re.compile(r"共\s*([\d.]+)\s*分")
ESC_DOT = re.compile(r"\\([.．、（）()])")            # docx 导出把点转义成 `\.`


FULLWIDTH_TRANS = {0xFF10 + i: 0x30 + i for i in range(10)}       # ０-９
FULLWIDTH_TRANS.update({0xFF21 + i: 0x41 + i for i in range(26)})  # Ａ-Ｚ
FULLWIDTH_TRANS.update({0xFF41 + i: 0x61 + i for i in range(26)})  # ａ-ｚ


SPACED_NUM = re.compile(r"(?m)^([ \t]*)(\d)\s+(\d)\s*([．.、])")


def join_spaced_numbers(text: str) -> str:
    """行首题号被 docx 拆成「1 8 .」→ 合回「18.」（2021 平谷一模第 18 题）。只动行首。"""
    return SPACED_NUM.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}", text or "")


def normalize_fullwidth(text: str) -> str:
    """全角数字/字母 → 半角（2021 通州一模题号写成 `９．`、2021 昌平期末答案写成 `1.Ｄ`）。"""
    return (text or "").translate(FULLWIDTH_TRANS)


PAREN_DIGITS = str.maketrans({"⑴": "（1）", "⑵": "（2）", "⑶": "（3）", "⑷": "（4）",
                                 "⑸": "（5）", "⑹": "（6）", "⑺": "（7）", "⑻": "（8）",
                                 "⑼": "（9）"})


def normalize_paren_digits(text: str, line_start_only: bool = False) -> str:
    """教师版常用 `⑴⑵⑶` 当小问标记（海淀 2023 一模）→ 统一成 `（1）（2）`。
    正文里只在**行首**替换，避免把材料中的「⑴」误当小问；答案区整段替换。"""
    if not text:
        return text
    if not line_start_only:
        return text.translate(PAREN_DIGITS)
    return re.sub(r"(?m)^([ \t]*)([⑴⑵⑶⑷⑸⑹⑺⑻⑼])",
                  lambda m: m.group(1) + m.group(2).translate(PAREN_DIGITS), text)


def unescape_dots(text: str) -> str:
    """`1\\. 题干` → `1. 题干`（docx 转义点；只动标点，不动图片/公式）。"""
    return ESC_DOT.sub(r"\1", text or "")


def split_paper_hist(txt: str, review: list[str] | None = None):
    """切「试题正文 / 参考答案区」。比语文版多兜两种底：

    ① 常规：独立成行的「参考答案」/「答案与解析」/「评分标准」；
    ② 教师版把整卷和答案拼在一起、答案区没有「参考答案」字样（石景山 2026 期末）：
       答案区以「| 题号 |」答案表或「【答案】」块开头；
    ③ 最后退回语文版 split_paper。
    """
    for m in re.finditer(r"(?m)^.*$", txt):
        line = m.group(0)
        probe = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)      # 去掉行内图片
        probe = probe.replace("*", "").strip()
        if re.fullmatch(r"(?:参考答案(?:与解析|及解析)?|答案与解析|评分标准)[:：]?", probe):
            if m.start() > 300:
                return txt[:m.start()], txt[m.start():]
            break
    cands = []
    # 教师版把「整卷 + 答案」拼在一起、答案区以**重复的卷首标题**开头（2021 石景山一模）
    first_line = next((ln.strip() for ln in txt.split("\n") if ln.strip()), "")
    if len(first_line) >= 8:
        for mm in re.finditer(rf"(?m)^[ \t]*{re.escape(first_line)}[ \t]*$", txt):
            if mm.start() > 300:
                cands.append(mm.start())
                break
    m = re.search(r"(?m)^[ \t]*\|\s*\**\s*题\s*号\s*\**\s*\|", txt)
    if m and m.start() > 300:
        cands.append(m.start())
    m = re.search(r"【答案】", txt)
    if m and m.start() > 300:
        cands.append(txt.rfind("\n", 0, m.start()) + 1)
    if cands:
        i = min(cands)
        if review is not None:
            review.append("复核：本卷 md 没有「参考答案」字样（教师版整卷+答案拼接），"
                          "答案区按首个答案表/【答案】块切分")
        return txt[:i], txt[i:]
    return split_paper(txt)


def int_split(total: float, k: int) -> list[float]:
    """把 total 分成 k 份**整数**（余数从后往前各加 1），避免出现 11.2 / 5.6 这种分数分。"""
    if k <= 0:
        return []
    total = float(total)
    if abs(total - round(total)) > 1e-9:      # 本身带小数（如 2.5 分/题）→ 保持小数均分
        return [round(total / k, 4)] * k
    total = int(round(total))
    base, rem = divmod(total, k)
    return [float(base + (1 if i >= k - rem else 0)) for i in range(k)]


def detect_choice_each(body1: str, ans: str, n_choice: int, per_q: list[float],
                       review: list[str]) -> float:
    """选择题每题分值：题面逐题标注 > 卷面「每题X分」> 声明总分/题数 > 默认 3 分。"""
    if per_q:
        vals = sorted(set(per_q))
        if len(vals) == 1:
            return vals[0]
        review.append(f"复核：选择题题面分值不统一 {vals}，按最常见的 {vals[-1]} 处理")
        return max(set(per_q), key=per_q.count)
    m = DECL_EACH.search(body1[:1600])
    if m:
        return float(m.group(1))
    m = DECL_TOTAL.search(body1[:1600])
    if m and n_choice:
        return round(float(m.group(1)) / n_choice, 2)
    m = re.search(r"选择题[^。\n]{0,16}共\s*([\d.]+)\s*分", ans[:2000])
    if m and n_choice:
        review.append(f"复核：卷面只在答案区写了「选择题共 {m.group(1)} 分」，"
                      f"按 {n_choice} 题均分")
        return round(float(m.group(1)) / n_choice, 2)
    review.append(f"复核：卷面没写选择题每题分值，按北京卷常规 {CHOICE_SCORE} 分/题处理")
    return float(CHOICE_SCORE)


def detect_essay_total(body2: str, ans: str, left: float, review: list[str]) -> float | None:
    """非选择题合计分值：卷面第二部分声明 > 100 − 选择题合计。"""
    m = re.search(r"(?:第二部分|非选择题)[^。\n]{0,20}?共\s*([\d.]+)\s*分", body2[:1200])
    if m:
        return float(m.group(1))
    m = re.search(r"(?:第二部分|非选择题|材料题|解答题)[^。\n]{0,20}?满分\s*([\d.]+)\s*分",
                  body2[:1200])
    if m:
        return float(m.group(1))
    if left and left > 0:
        review.append(f"复核：卷面没声明非选择题总分，按卷面满分 100 − 选择题合计 = {left:g} 分处理")
        return left
    return None


# ──────────────────────────────────────────────────────────────────
# 选择题
# ──────────────────────────────────────────────────────────────────
def _looks_like_question(text: str, m: re.Match) -> bool:
    """判断某个题号标记后面那一行像不像题干（排除被 docx 误编号的选项行/列举行）。"""
    eol = text.find("\n", m.end())
    line = text[m.end(): eol if eol > 0 else len(text)].strip()
    if len(re.findall(r"[①-⑳]", line)) >= 2:   # 选项行（`1. 铁人王喜 ②保卫…③…④…`）
        return False
    if len(line) >= 25:
        return True
    nxt = text[(eol + 1) if eol > 0 else len(text):].lstrip()
    return nxt.startswith("![") or nxt.startswith("|")   # 题干后面接图/接表也算


def renumber_choice_markers(text: str, marks) -> str:
    """把第 1 部分的题号按**出现顺序**重排成 1..N（docx 自动编号坏掉时，正文全变成 `1.`）。"""
    out, last = [], 0
    for n, m in enumerate(marks, 1):
        out.append(text[last:m.start()])
        out.append((m.group(1) or "") + f"\n\n{n}．")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def repair_glued_numbers(text: str, missing: list[int], review: list[str] | None = None) -> str:
    """docx 把下一题题号粘到上一题选项行尾（`D．②④12．13世纪的英国…`）→ 补一个换行。
    只对「已知缺号」动手，避免把材料里的序号也拆开。"""
    hit = []
    for k in missing:
        if re.search(rf"(?m)^[ \t]*\**\s*{k}\s*\**\s*\\?[．.、]", text):
            continue
        m = re.search(rf"(?<=[^\n\d])({k})[．.、](?!\d+\s*(?:分|%|％))(?=[^\n\s])", text)
        if m:
            text = text[:m.start()] + "\n\n" + text[m.start():]
            hit.append(k)
            continue
        # 题号后**漏了点**（`8下表展示了…`）→ 补出题号与换行，交给 QMARK 认
        m = re.search(rf"(?m)^[ \t]*\**\s*({k})\**\s*(?=[\u4e00-\u9fff（(①-⑳!])", text)
        if m and not re.match(rf"^{k}\s*[．.、]", text[m.start():].lstrip()):
            text = text[:m.start()] + "\n\n" + text[m.start():m.end()].rstrip() + "．" + text[m.end():]
            hit.append(f"{k}(缺题号点)")
    if hit and review is not None:
        review.append(f"复核：题号 {hit} 在 md 里粘在上一题的选项行尾（docx 串行），已补换行拆开")
    return text


def repair_tables(text: str, review: list[str] | None = None, where: str = "") -> str:
    """修 docx 导出的两类坏表（2026 朝阳期末、2026 真题）：

    ① **表头被拆成两行**：`| 年份 | 国家 | | | | |` + 错位分隔行 + `| 英国 | 美国 | … |`
       （原表是「年份」跨列 + 5 个国家）→ 合回一行表头；
    ② **纵向合并单元格丢值**：续行少了第一列（`| 长江下游 | … |`）→ 用上一行首列补齐，
       保证每行列数一致（GFM 表格不会错位）。
    """
    lines = text.split("\n")
    out: list[str] = []
    notes: list[str] = []
    i = 0

    def cells(ln: str) -> list[str]:
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        block = lines[i:j]
        i = j
        rows = [cells(ln) for ln in block]
        sep = [k for k, r in enumerate(rows)
               if all((not c) or set(c) <= set("-: ") for c in r)]
        if len(sep) != 1:
            out.extend(block)
            continue
        si = sep[0]
        pre, post = rows[:si], rows[si + 1:]
        if not pre or not post:
            out.extend(block)
            continue
        width = len(pre[-1])
        # ① 表头被拆：首行有「跨列组标签」+ 分隔行后的第一行是剩下的表头单元格
        if (post and len(post[0]) < width and len(post[0]) == width - 1
                and all(post[0]) and not any(re.search(r"\d", c) for c in post[0])):
            lead = [c for c in pre[-1] if c]
            if len(lead) >= 2:
                newhead = lead[:-1] + post[0]
                if len(newhead) == width:
                    pre = [newhead]
                    post = post[1:]
                    notes.append("表头被 docx 拆成两行，已合并回一行")
        # ② 纵向合并：续行少首列 → 用上一行首列补齐
        fixed, prev_first, filled = [], None, 0
        for r in post:
            if len(r) == width - 1 and prev_first is not None:
                r = [prev_first] + r
                filled += 1
            fixed.append(r)
            prev_first = r[0] if r else prev_first
        if filled:
            notes.append(f"{filled} 行纵向合并单元格缺首列，已用上一行首列补齐")
        newblock = ["| " + " | ".join(pre[-1]) + " |",
                    "| " + " | ".join("---" for _ in pre[-1]) + " |"]
        for r in fixed:
            if len(r) < width:
                r = r + [""] * (width - len(r))
            newblock.append("| " + " | ".join(r) + " |")
        out.extend(newblock)
    if notes and review is not None:
        for n in dict.fromkeys(notes):
            review.append(f"复核：{where} {n}")
    return "\n".join(out)


def split_options(seg: str):
    """从题干段里切出 A–D 四项（选项可能一行挤好几个、跨行、也可能带前置图）。

    有些卷把选项排成两列，导出后顺序变成 `A. … C. …` / `B. … D. …`（2021 真题第 1、9 题），
    所以严格 A→B→C→D 顺序切不出来时，退化成「按出现位置切、只要求四项齐全」。
    """
    # 选项之间没有空格（`A.阶级分化明显B.部落联盟出现`）→ 在 B/C/D 前补断行
    seg = re.sub(r"(?<=[\u4e00-\u9fff①-⑳）》」\"'”’])([B-D])(?=[．.、])", r"\n\1", seg)
    # 选项字母漏了点（行首 `B表明美国成为…`）→ 补出选项点
    seg = re.sub(r"(?m)^[ \t]*\**\s*([A-D])\**\s*(?=[\u4e00-\u9fff【《“\"(①-⑳])", r"\n\1．", seg)
    # 行内「空格 + B + ①」（`A.①②③④ B.①③②④ C③②④①`）→ 给漏点的选项补点
    seg = re.sub(r"(?<=[ \t\u3000])([A-D])(?=[①-⑳\u4e00-\u9fff])(?![\u4e00-\u9fff]{2,}[，。；])",
                 lambda m: m.group(1) + "．", seg)
    # 选项字母后直接跟内嵌图片（`A![](images/image3.wmf) 商朝内外服制…`）→ 补点，图留在 label 里
    seg = re.sub(r"(?m)^[ \t]*([A-D])(?=!\[)", r"\1．", seg)
    # 选项 A 被 docx 自动编号写成 `1. ①②③`（顺义 2024 一模第二次第 7 题）→ 还原成 A.
    if not re.search(r"(?m)^[ \t]*A\s*[．.、]", seg):
        fb = re.search(r"[ \t](B|C|D)\s*[．.、]", seg)
        if fb:
            m1 = re.search(r"(?m)^[ \t]*1\s*[．.、]\s*(?=[①-⑳\u4e00-\u9fff])", seg)
            if m1 and m1.start() < fb.start():
                seg = seg[:m1.start()] + "A．" + seg[m1.end():]
    # 四个选项被写成数字列表 `1. … 2. … 3. … 4. …`（2021 通州一模第 14 题）→ 还原成 A–D
    if not re.search(r"(?m)^[ \t]*A\s*[．.、]", seg):
        num_marks = list(re.finditer(r"(?m)^[ \t]*([1-4])\s*[．.、]\s*", seg))
        seq = [m for m in num_marks if int(m.group(1)) == len([x for x in num_marks if x.start() <= m.start()])]
        if len(num_marks) >= 4:
            picked_nums, cur = [], 1
            for m in num_marks:
                if int(m.group(1)) == cur:
                    picked_nums.append(m)
                    cur += 1
            if len(picked_nums) == 4:
                out_seg = seg[: picked_nums[0].start()]
                for i, m in enumerate(picked_nums):
                    nxt = picked_nums[i + 1].start() if i + 1 < len(picked_nums) else len(seg)
                    out_seg += "ABCD"[i] + "．" + seg[m.end():nxt].lstrip("\n") + "\n"
                seg = out_seg
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
        first = {}
        for s, e, k in marks:
            first.setdefault(k, (s, e, k))
        if sorted(first) == ["A", "B", "C", "D"]:
            best = sorted(first.values())
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


def options_from_table(seg: str):
    """有的题把选项做成表格：`| 选项 | 史实 | 结论 |` + `| A | … | … |`（房山 2026 期末第 15 题）。
    返回 (options, 去掉表格后的题干)。"""
    lines = seg.split("\n")
    rows = [(i, ln) for i, ln in enumerate(lines) if ln.strip().startswith("|")]
    if len(rows) < 5:
        return None, seg
    opt: dict[str, list[str]] = {}
    tab_idx = set()
    for i, ln in rows:
        cells = [c.strip().strip("*").strip() for c in ln.strip().strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if re.fullmatch(r"[A-D]", first):
            opt.setdefault(first, cells[1:])
            tab_idx.add(i)
        elif first in ("选项", "题号") or all((not c) or set(c) <= set("-: ") for c in cells):
            tab_idx.add(i)
    if sorted(opt) != ["A", "B", "C", "D"]:
        return None, seg
    out = []
    for k in "ABCD":
        parts = [p for p in opt[k] if p]
        out.append({"id": k, "label": "；".join(parts)})
    rest = "\n".join(ln for i, ln in enumerate(lines) if i not in tab_idx)
    return out, rest


def parse_choices(text: str, review: list[str], each: float | None = None,
                  explicit: dict[int, float] | None = None, title: str = ""):
    """第一部分（选择题）。题号以卷面为准（老卷/文综卷可能不从 1 开始、题数也不是 15），
    分值由调用方按卷面算出后传入（`--check` 只要求同 section 内一致）。"""
    explicit = explicit if explicit is not None else {}
    each = float(each) if each else float(CHOICE_SCORE)
    marks = [(m.start(), m.end(), int(m.group(2)), m.group(1), m.group(3))
             for m in QMARK.finditer(text)]
    marks = [x for x in marks if 1 <= x[2] <= 60]
    picked, seen = [], set()
    for s, e, no, prefix, sc in marks:
        if no in seen:
            continue
        seen.add(no)
        picked.append((s, e, no, prefix, sc))
    picked.sort()
    raw = []
    for i, (s, e, no, prefix, sc) in enumerate(picked):
        nxt = picked[i + 1][0] if i + 1 < len(picked) else len(text)
        seg = (prefix or "") + text[e:nxt]
        raw.append([no, seg, sc])

    # 原卷某题题号漏印（门头沟 2025 一模第 10 题）→ 按卷面锚点把前一题的段尾切成新题
    for frm, pat, newno, why in SPLIT_TAIL_CHOICE.get(title, []):
        for i, item in enumerate(raw):
            if item[0] != frm:
                continue
            mm = re.search(pat, item[1])
            if not mm:
                continue
            head, tail = item[1][: mm.start()], item[1][mm.start():]
            if any(r[0] == newno for r in raw):
                break
            raw[i][1] = head
            raw.insert(i + 1, [newno, tail, None])
            review.append(f"复核：{why}")
            break

    # 表格跑到题号前面（丰台第 6 题「右表展示了…」）→ 前移给引用它的那题。
    # 但若这张表本身就是本题的「选项表」（| A | 史实 | 结论 |），绝不能搬走。
    for i in range(len(raw) - 1):
        if options_from_table(raw[i][1])[0]:
            continue
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
    for no, seg, sc in raw:
        opts, head = split_options(seg)
        if opts:
            opts = sorted(opts, key=lambda o: "ABCD".index(o["id"]))
        else:
            opts, head = options_from_table(seg)
            if opts:
                review.append(f"复核：第{no}题的 A–D 选项是表格（选项/史实/结论…），"
                              f"已按行转成 options（多列用「；」连接），请人工核对")
        content = strip_stem_residue(tidy(head))
        content = re.sub(r"^(?:[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)])", "", content).strip()
        if not content:
            review.append(f"复核：第{no}题题干为空（可能整段是 residue/图片），请人工核对")
        item = {"id": no, "questionType": "single", "content": content,
                "options": [], "answer": "", "analysis": "",
                "score": float(sc) if sc else explicit.get(no, each)}
        if opts:
            item["options"] = [{"id": o["id"], "label": tidy(o["label"])} for o in opts]
        else:
            review.append(f"复核：第{no}题没切出 A–D 四个选项，请人工核对原卷")
        out.append(item)
    return out


# ──────────────────────────────────────────────────────────────────
# 非选择题
# ──────────────────────────────────────────────────────────────────
SUBQ_LINE = re.compile(r"(?m)^[ \t]*([1-9])\s*[．.、]")
SUBQ_HINT = re.compile(r"阅读材料|结合所学|依据材料|根据材料|概括|分析|说明|简述|指出|评价|阐述|谈谈|"
                       r"归纳|比较|列举|为什么|如何")


def normalize_sub_numbers(text: str) -> str:
    """大题里的小问被 docx 自动编号写成 `1.`/`2.`（通州 2024 期末、延庆 2025 一模）→ 还原成 `（1）（2）`。
    只对「带分值」或「带设问动词」的行动手，免得把材料里的序号也改了。"""
    def repl(m):
        eol = text.find("\n", m.end())
        line = text[m.start(): eol if eol > 0 else len(text)]
        if SCORE_RE.search(line) or SUBQ_HINT.search(line):
            return f"（{m.group(1)}）"
        return m.group(0)
    return SUBQ_LINE.sub(repl, text)


def split_subs(text: str):
    """材料 + 各小问。**按行首标记切片段**：`（N）` 是设问、`材料[一二三四]` 是新材料。

    不少卷（2016/2020 真题、石景山 2026 二模）把材料和小问**交替排列**
    （材料一 → （1）设问 → 材料二 → （2）设问），旧写法只按 (1)(2) 切，
    会把「材料二」整段塞进第（1）问的 content 里。这里返回有序片段，
    由调用方按原顺序拼 section.content，设问只取自己的那一段。

    返回 (material, subs, segments)；没有 (1)(2) 时 subs 为 None。
    """
    marks: list[tuple[int, int, str, int | None]] = []
    for m in SUBQ.finditer(text):
        marks.append((m.start(), m.end(), "sub", int(m.group(1))))
    for m in MAT_MARK.finditer(text):
        marks.append((m.start(), m.start(), "mat", None))
    if not any(k == "sub" for _, _, k, _ in marks):
        return None, None, None
    marks.sort()
    # 材料里的编号列表（`1. 你是否同意…`、`（2）请解释…`）会被误当小问 → 只认**最后一个从 1 起连续**的序列
    smarks = [(i, m) for i, m in enumerate(marks) if m[2] == "sub"]
    keep_idx: set[int] = set()
    if smarks:
        best: list[int] = []
        for j, (idx, (_pos, _end, _kind, no)) in enumerate(smarks):
            if no != 1:
                continue
            seq, cur = [idx], 2
            for idx2, (_p2, _e2, _k2, n2) in smarks[j + 1:]:
                if n2 == cur:
                    seq.append(idx2)
                    cur += 1
            best = seq                      # 保留最后一个以 1 开头的连续序列
        keep_idx = set(best)
        if keep_idx:
            marks = [(pos, end, kind, no) for i, (pos, end, kind, no) in enumerate(marks)
                     if kind != "sub" or i in keep_idx]
    segs: list[tuple[str, int | None, str]] = []
    if marks and marks[0][0] > 0:
        head = text[: marks[0][0]].strip()
        if head:
            segs.append(("body", None, head))
    for i, (s, e, kind, no) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if kind == "sub":
            body = text[e:end].strip()
            lead = text[s:e]
            segs.append(("sub", no, (lead + body).strip()))
        else:
            segs.append(("mat", None, text[s:end].strip()))
    material = "\n\n".join(t for k, _, t in segs if k in ("mat", "body")).strip()
    subs = []
    for k, no, t in segs:
        if k != "sub":
            continue
        mm = SUBQ.match(t)
        stext = t[mm.end():].strip() if mm else t
        subs.append({"no": no, "text": stext})
    return material, subs, segs


def split_single_prompt(text: str):
    """没有 (1)(2) 的大题：带分值的那一段就是设问（含其后的「要求：…」）；
    卷面没标分值时退化为「最后一段非图表文字」。"""
    lines = text.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("|"):
            continue
        if SCORE_RE.search(ln) and len(ln.strip()) > 8:
            idx = i
    if idx is not None:
        return "\n".join(lines[:idx]).strip(), "\n".join(lines[idx:]).strip()
    paras = text.split("\n\n")
    for p in reversed(paras):
        s = p.strip()
        if not s or s.startswith("|") or s.startswith("![") or s.startswith("--") \
                or s.startswith("#"):
            continue
        if re.fullmatch(r"材料[一二三四1234]\s*[:：]?", s):
            continue
        if len(s) < 6:
            continue
        i = text.rfind(p)
        return text[:i].strip(), text[i:].strip()
    return text.strip(), ""


# 卷面「阅读材料，回答问题」这类套话不是题名；题名在下一行的短语（节日／征文／长安城…）
BOILER_NAME = re.compile(r"^(?:(?:阅读|根据|依据)?材料[，,]?(?:并)?(?:结合所学)?[，,]?)?"
                         r"(?:回答|完成)?(?:下列)?(?:问题|要求)?[。．.]?$|^非选择题$|^材料题$")


def essay_name(head: str, rest: str) -> tuple[str, str]:
    """大题题名：题号后那一行若是短语就用作题名；若是「阅读材料，回答问题。」这类套话，
    就往下找材料区第一行的短语（东城/丰台 2026 期末卷的题名单独占一行），并从材料里摘掉。"""
    name = re.sub(r"\s+", "", strip_score(head))
    name = re.sub(r"^[（(].*?[）)]", "", name).strip()
    name = name.strip("*").strip()
    if name and not BOILER_NAME.match(name) and len(name) <= 30:
        return name, rest
    lines = rest.split("\n")
    for i, ln in enumerate(lines[:6]):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^(?:材料[一二三四1234]|注\s*[:：]|图\s*\d|表\s*\d|[（(]\s*\d|!\[|\||--)", s):
            return "", rest
        if len(s) <= 18 and not re.search(r"[。；，,：:]$", s):
            return s, "\n".join(lines[:i] + lines[i + 1:]).strip()
        return "", rest
    return "", rest


MATERIALISH = re.compile(r"--\s*摘编自|--\s*据|^\s*材料|\n\s*\||\n\s*!\[|^\s*\d{4}\s*年")


def sub_prompt_only(txt: str) -> str:
    """小问的「题干」只取第一段；第一段之后若像材料（摘编/表格/图/年份开头），就不并进题干。
    section.content 仍保留全部内容（材料只出现一次仍在原位置）。"""
    parts = (txt or "").split("\n\n")
    if len(parts) <= 1:
        return txt
    rest = "\n\n".join(parts[1:])
    if MATERIALISH.search(rest) or len(rest) > 200:
        return parts[0]
    return txt


def parse_essays(text: str, review: list[str], min_no: int = 16,
                 ans_blocks: dict[int, str] | None = None,
                 ans_scores: dict[int, float] | None = None,
                 ans_sub_scores: dict[int, list[float]] | None = None,
                 essay_total: float | None = None):
    """第二部分（非选择题）。**一个大题一个 section**。

    大题分值来源（依次）：题面「16．（N分）」> 答案区块头「16.(N分)」> 均分（100−选择题）
    小问分值来源（依次）：题面各小问「（N分）」> 答案区各小问「…（N分）」> 大题分均分
    """
    ans_blocks = ans_blocks or {}
    ans_scores = ans_scores or {}
    ans_sub_scores = ans_sub_scores or {}
    marks = [(m.start(), m.end(), int(m.group(2)), m.group(3), m.group(1))
             for m in QMARK.finditer(text)]
    marks = [x for x in marks if x[2] >= min_no]
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
        name, rest = essay_name(head, rest.strip())
        # 题名没取到、而题号后那一行还是一整段文字（题干导语/材料）→ 那就是材料，别丢
        head_prose = strip_score(head).strip().strip("*").strip()
        if not name and len(re.sub(r"\s+", "", head_prose)) > 12:
            rest = head_prose + "\n\n" + rest.strip()
        rest = rest.strip()
        rest = normalize_sub_numbers(rest)
        material, subs, segs = split_subs(rest)
        big_score = float(sc) if sc else None
        if subs is None:
            material, prompt = split_single_prompt(rest)
            if not prompt.strip():                 # 整题只有一行设问（2018 真题第 14 题）
                prompt = strip_score(head).strip()
                material = ""
            subs = [{"no": None, "text": prompt}]
            segs = None
        else:
            prompt = None
        # 各小问分值：题面 > 答案区
        scores: list[float | None] = []
        for sb in subs:
            m = SCORE_RE.search(sb["text"])
            scores.append(float(m.group(1)) if m else None)
        if all(x is None for x in scores) and ans_sub_scores.get(no):
            cand = ans_sub_scores[no]
            if len(cand) == len(scores):
                scores = list(cand)
                review.append(f"复核：第{no}题小问分值题面未标，取自答案区小问末尾的（N分）："
                              f"{[int(x) if x == int(x) else x for x in scores]}")
        total = big_score
        if total is None and no in ans_scores:
            total = ans_scores[no]
            review.append(f"复核：第{no}题题面没标总分值，取自答案区块头「{no}.(N分)」= {total:g} 分")
        if total is None and essay_total and len(picked):
            plan = int_split(essay_total, len(picked))
            total = plan[i]
            review.append(f"复核：第{no}题题面与答案区都没标分值，按第二部分 {essay_total:g} 分 "
                          f"在 {len(picked)} 题间整数均分 = {[int(x) for x in plan]} 分")
        if total is None:
            total = 10.0
            review.append(f"复核：第{no}题题面没标总分值，按 10 分处理")
        known = [x for x in scores if x]
        if None in scores:
            lack = total - sum(known)
            left = scores.count(None)
            plan = int_split(lack, left) if (left and lack > 0) else \
                [(known[0] if known else 1)] * left
            fill = plan or [1]
            scores = [x if x else fill.pop(0) for x in scores]
            if len(subs) > 1:
                review.append(f"复核：第{no}题有小问未标分值，按大题 {total:g} 分补足："
                              f"{[int(x) if x == int(x) else x for x in scores]}")
        diff = total - sum(scores)
        if abs(diff) > 1e-6:
            review.append(f"复核：第{no}题小问分值 {[int(x) if x == int(x) else x for x in scores]} "
                          f"合计 {sum(scores):g} ≠ 大题 {total:g} 分，差额记到最后一问")
            scores[-1] = max(0.5, scores[-1] + diff)
        out.append({"no": no, "name": name, "material": material, "subs": subs,
                    "segments": segs, "scores": scores, "score": total,
                    "single": prompt is not None})
    return out


# ──────────────────────────────────────────────────────────────────
# 参考答案
# ──────────────────────────────────────────────────────────────────
def letters_from_plain_rows(ans: str) -> dict[int, str]:
    """非 GFM 表格的答案表：`题号 1 2 3 …` 一行 + `答案 D D C …` 一行（空格分隔，可折行）。"""
    got: dict[int, str] = {}
    lines = [ln.strip() for ln in ans.split("\n")]
    for i, ln in enumerate(lines):
        if not re.match(r"^(?:题|题\s*号|题号)[\s:：]*\d", ln):
            continue
        nums = [int(x) for x in re.findall(r"\d{1,2}", re.sub(r"^[^\d]*", "", ln))]
        if not nums:
            continue
        letters: list[str] = []
        j = i + 1
        while j < len(lines) and len(letters) < len(nums):
            nxt = lines[j]
            if not nxt:                      # 行间的空行不能打断
                j += 1
                continue
            if re.match(r"^答\s*案[\s:：]*[A-D]", nxt):
                letters += re.findall(r"[A-D]", re.sub(r"^答\s*案", "", nxt))
                j += 1
                continue
            if letters and re.fullmatch(r"(?:[A-D][\s、,，]*)+", nxt):
                letters += re.findall(r"[A-D]", nxt)
                j += 1
                continue
            break
        if len(letters) >= len(nums):
            for n, L in zip(nums, letters):
                got.setdefault(n, L)
    return got


def letters_from_range_run(ans: str) -> dict[int, str]:
    """「1-15DCDBB DBACA CACBD」/「1－15 DCDBBDBACACACBD」这种区间字母串（昌平期末卷）。"""
    got: dict[int, str] = {}
    for m in re.finditer(r"(?m)^[ \t]*(\d{1,2})\s*[-—－~]\s*(\d{1,2})\s*[:：]?[ \t]*"
                         r"((?:[A-D][ \t]*){4,})", ans):
        a, b = int(m.group(1)), int(m.group(2))
        letters = re.findall(r"[A-D]", m.group(3))
        for k, L in enumerate(letters):
            if a + k <= b:
                got.setdefault(a + k, L)
    return got


def parse_choice_letters(ans_text: str, review: list[str], n_choice: int = N_CHOICE,
                         choice_ids: list[int] | None = None) -> dict[int, str]:
    """选择题字母答案。北京卷这几种写法都要认：

    ① 答案表（`| 题号 | 1 | …` + `| 答案 | A | …`）；
    ② 表格单元格写成 `| 1．C | 2．B |`（昌平/西城 2026 二模、2026 真题）；
    ③ 行内字母串 `1.B 2.D 3.C`（2021/2023 真题）、`1．C` 逐行（2026 真题）；
    ④ 区间字母串 `1-15DCDBB DBACA CACBD`（昌平 2026 期末）；
    ⑤ 逐题解析块里的 `【答案】A` / `故选：A`（2011–2020 真题、海淀/石景山 2026）。
    """
    ok_ids = set(choice_ids) if choice_ids else None

    def keep(no: int) -> bool:
        return (ok_ids is not None and no in ok_ids) or (ok_ids is None and 1 <= no <= n_choice)

    got: dict[int, str] = {}
    srcs: list[str] = []
    tab = parse_answer_tables(ans_text)
    for k, v in tab.items():
        if keep(k):
            got.setdefault(k, v)
    if got:
        srcs.append("答案表")
    # ②/③ 数字＋字母（`1．C` / `1.B 2.D`）
    for mm in INLINE_ANS.finditer(ans_text):
        no, letter = int(mm.group(1)), mm.group(2).upper()
        if keep(no):
            got.setdefault(no, letter)
    if got:
        srcs.append("字母串")
    # ④ 区间字母串 / 纯文本答案表
    before = len(got)
    for k, v in letters_from_range_run(ans_text).items():
        if keep(k):
            got.setdefault(k, v)
    for k, v in letters_from_plain_rows(ans_text).items():
        if keep(k):
            got.setdefault(k, v)
    if len(got) > before:
        srcs.append("区间字母串")
    # ⑤ 逐题解析块
    before = len(got)
    ids = sorted(ok_ids) if ok_ids else list(range(1, n_choice + 1))
    for no, blk in split_answer_blocks(ans_text, expected=ids).items():
        if not keep(no) or no in got:
            continue
        head = blk[:400]
        m = (re.search(r"【答案】\s*\**\s*([A-D])(?![A-Za-z])", head)
             or re.search(r"故\s*选\s*[:：]?\s*\**\s*([A-D])(?![A-Za-z])", blk)
             or re.search(r"答\s*案\s*[:：]\s*\**\s*([A-D])(?![A-Za-z])", head))
        if m:
            got[no] = m.group(1).upper()
    if len(got) > before:
        srcs.append("解析块【答案】/故选")
    if got:
        review.append(f"复核：选择题答案取自参考答案区（{'＋'.join(dict.fromkeys(srcs))}），"
                      f"共 {len(got)} 个字母")
    return got


def parse_answer_tables(text: str) -> dict[int, str]:
    """答案表：`| 题号 | 1 |` + `| 答案 | A |`，也认单元格写成 `1．C` 的形式。"""
    rows = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip().strip("*").strip() for c in s.strip("|").split("|")]
        if all((not c) or set(c) <= set("-: ") for c in cells):
            continue                      # 分隔行
        rows.append(cells)
    got: dict[int, str] = {}
    for cells in rows:
        for c in cells:                   # 单元格自带题号：`1．C`
            m = re.fullmatch(r"(\d{1,2})\s*[．.、]?\s*([A-D])", c)
            if m:
                got.setdefault(int(m.group(1)), m.group(2))
    i = 0
    while i < len(rows) - 1:
        nums = [int(c) for c in rows[i] if re.fullmatch(r"\d{1,2}", c)]
        lets = [c for c in rows[i + 1] if re.fullmatch(r"[A-D]", c)]
        if nums and lets and len(lets) <= len(nums):
            # 有的卷「题号 11–20」一行但字母只到 15（后 5 格空着）→ 只配前 len(lets) 对
            for n, L in zip(nums[:len(lets)], lets):
                got.setdefault(n, L)
            i += 2
        else:
            i += 1
    return got


def _ans_block_start_re(no: int) -> re.Pattern:
    r"""答案块题头：`16 题` / `16.` / `16．` / `16\.`（行首、可带 ** 加粗、转义点已还原）。"""
    return re.compile(rf"(?m)^[ \t]*\**\s*{no}\s*(?:\**\s*题(?![一二三四五六七八九十])|\\?[．.、\-—–]"
                      rf"|\s+(?=【)|(?=[（(]\s*\d+(?:\.\d+)?\s*\**\s*分)|(?=!\[))")


def split_answer_blocks(ans: str, expected: list[int] | None = None) -> dict[int, str]:
    """切参考答案块。**按期望题号顺序逐块定位**（答案里 `16.`、`19.评析：`、`16.(1)…`、
    `16．【分析】` 混着出现，宽松正则会把正文里的序号当块头，所以只认已知题号且必须递增）。"""
    if expected is None:
        expected = sorted({int(m.group(1)) for m in ANS_HDR.finditer(ans)})
    starts: list[tuple[int, int, int]] = []
    pos = 0
    for k in expected:
        m = _ans_block_start_re(k).search(ans, pos)
        if not m:
            continue
        starts.append((m.start(), m.end(), k))
        pos = m.end()
    blocks: dict[int, str] = {}
    for i, (s, e, no) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(ans)
        blocks[no] = ans[s:end].strip()      # **含块头行**（`16.【答案】A` 的字母在块头里）
    return blocks


# 教师版解析脚手架（菁优网/菁师帮：「【分析】【解答】【点评】【详解】【小问N详解】」）
SCAFFOLD = re.compile(r"【(?:分析|解析|解答|点评|详解|小问\s*\d*\s*详解|点睛|考点|专题|难度|答案|"
                      r"命题立意|知识拓展|思路点拨|试题解析|参考答案)】")


def split_answer_blocks_sequential(ans: str, expected: list[int]) -> dict[int, str]:
    """答案区的大题题号也被 docx 自动编号写坏成 `1. 2. 3. …`（延庆 2025 一模、昌平 2024 二模）：
    取「第二部分」声明之后的 1..k 连续题号，按顺序映射回 expected（16..20）。"""
    m = re.search(r"(?m)^[ \t]*\**\s*(?:第二部分|二[、.．]\s*(?:非选择题|材料题|解答题|本部分))", ans)
    if not m:
        return {}
    seg = ans[m.end():]
    picked, cur = [], 1
    for mm in re.finditer(r"(?m)^[ \t]*\**\s*(\d{1,2})\s*[．.、]", seg):
        if int(mm.group(1)) == cur:
            picked.append((mm.start(), mm.end(), cur))
            cur += 1
    if not picked:
        return {}
    out: dict[int, str] = {}
    for i, (st, en, k) in enumerate(picked):
        if i >= len(expected):
            break
        end = picked[i + 1][0] if i + 1 < len(picked) else len(seg)
        out[expected[i]] = seg[st:end].strip()
    return out


def first_misnumbered_block(ans: str) -> str:
    """答案区只有第一道大题被 docx 自动编号写成 `1.（N分）`（延庆 2025 一模 16 题）→ 取出该块正文。"""
    m = re.search(r"(?m)^[ \t]*\**\s*(?:第二部分|二[、.．]\s*(?:非选择题|材料题|解答题|本部分))", ans)
    seg = ans[m.end():] if m else ans
    mm = re.search(r"(?m)^[ \t]*\**\s*1\s*[．.、]\s*[（(]\s*\d+", seg)
    if not mm:
        return ""
    rest = seg[mm.start():]
    nxt = re.search(r"(?m)^[ \t]*\**\s*(?:1[6-9]|2[0-9])\s*[．.、]", rest[10:])
    return rest[: (nxt.start() + 10) if nxt else len(rest)].strip()


def fill_missing_bare_ones(blocks: dict[int, str], essay_nos: list[int], ans: str) -> int:
    """答案区里有几道大题被 docx 自动编号写成只有 `1.` 一行（丰台 2025 一模 17/18/19）：
    取所有「裸 1. + 【答案】」块，按缺失题号的顺序回填。"""
    missing = [k for k in essay_nos if k not in blocks]
    if not missing:
        return 0
    marks = [m for m in re.finditer(r"(?m)^[ \t]*1\s*[．.、]\s*$", ans)]
    cands = []
    for m in marks:
        nxt = re.search(r"(?m)^[ \t]*1\s*[．.、]\s*$", ans[m.end():])
        end = m.end() + nxt.start() if nxt else len(ans)
        seg = ans[m.end():end].strip()
        if "【答案】" in seg[:30]:
            cands.append(seg)
    n = 0
    for k, seg in zip(missing, cands):
        blocks[k] = seg
        n += 1
    return n


def fill_missing_by_gap(blocks: dict[int, str], essay_nos: list[int], ans: str) -> int:
    """有大题的题头在 md 里彻底丢了（通州 2024 期末第 19 题）→ 用「上一块结束 ~ 下一块题头」之间的
    未归属文本补上。只在两侧都有已定位块、且中间文本够长时才补，避免张冠李戴。"""
    pos = {}
    for k in essay_nos:
        m = _ans_block_start_re(k).search(ans)
        if m:
            pos[k] = m.start()
    if len(pos) < 2:
        return 0
    order = sorted(pos.items(), key=lambda x: x[1])
    filled = 0
    for idx, k in enumerate(essay_nos):
        if k in blocks or k in pos:
            continue
        prev_i = max((i for i, (k2, _) in enumerate(order) if k2 < k), default=None)
        next_i = min((i for i, (k2, _) in enumerate(order) if k2 > k), default=None)
        if prev_i is None or next_i is None:
            continue
        start = order[prev_i][1]
        end = order[next_i][1]
        # 上一块的正文结束点 = 上一块之后第一个题头（即 start 之后的下一个 pos 之前）→ 用 order[prev_i+1]
        if prev_i + 1 < len(order):
            start = order[prev_i][1] if order[prev_i + 1][0] not in (k,) else start
        seg = ans[start:end]
        # 去掉上一块自己的题头行与正文，只保留“中间那段”
        m2 = _ans_block_start_re(order[prev_i][0]).search(seg)
        if m2:
            eol = seg.find("\n", m2.end())
            # 上一块正文到下一个题头之间：这里用简单办法——取 seg 里最后一个空行之后的部分
            tail = seg.rsplit("\n\n", 1)
            if len(tail) == 2 and len(tail[1].strip()) > 30:
                seg = tail[1]
        seg = seg.strip()
        if len(seg) > 30:
            blocks[k] = seg
            filled += 1
    return filled


def fill_missing_loose(blocks: dict[int, str], essay_nos: list[int], ans: str) -> int:
    """答案区里某道大题的题头既不在行首、也不成行（`…体现学科素养。19.(1)…`，朝阳 2025 一模）：
    按「题号 + 句点 + （/【」在全文里找，范围到下一道大题题头为止。"""
    missing = [k for k in essay_nos if k not in blocks]
    if not missing:
        return 0
    filled = 0
    for k in missing:
        m = re.search(rf"(?<![\d]){k}\s*[．.、]\s*(?=[（(【参考答评])", ans)
        if not m:
            continue
        rest = ans[m.start():]
        end = len(rest)
        for k2 in essay_nos:
            if k2 <= k:
                continue
            m2 = re.search(rf"(?<![\d]){k2}\s*[．.、]\s*(?=[（(【参考答评])", rest[1:])
            if m2:
                end = min(end, m2.start() + 1)
        seg = rest[:end].strip()
        if len(seg) > 20:
            blocks[k] = seg
            filled += 1
    return filled


def clean_ans_block(blk: str) -> str:
    """答案块去掉块头题号与解析脚手架，只留标准答案：

    ① 有「故答案为：」→ 取其后（菁优网把分析、答案、点评堆在一块）；
    ② 有「【答案】」→ 取其后（apex 教师版）；
    ③ 有「【解答】」→ 取其后；
    ④ 再砍到下一个脚手架标记（【详解】/【点评】/【小问N详解】）之前。
    """
    t = (blk or "").strip()
    if not t:
        return ""
    # 块头：`16.` / `16．(10分)` / `16\. 【答案】`
    t = re.sub(r"^\**\s*\d{1,2}\s*\**\s*(?:题(?![一二三四五六七八九十])|\\?[．.、])\s*\**\s*", "", t)
    t = re.sub(r"^[（(]\s*\d{1,2}\s*分\s*[）)]\s*", "", t)
    t = re.sub(r"^\s*\d{1,2}\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*", "", t)
    m = re.search(r"故答案为\s*[:：]?", t)
    if m:
        t = t[m.end():]
    else:
        m = re.search(r"【答案】", t)
        if m:
            t = t[m.end():]
        else:
            m = re.search(r"【解答】", t)
            if m:
                t = t[m.end():]
    # 砍掉后续脚手架段（【详解】/【点评】/【小问N详解】…）
    cut = None
    for m in SCAFFOLD.finditer(t):
        cut = m.start()
        break
    if cut is not None and cut > 0:
        t = t[:cut]
    for _ in range(3):                      # `** 参考答案：**（1…` 这类左侧残留
        t2 = re.sub(r"^\**\s*", "", t.strip())
        t2 = re.sub(r"^(?:参考答案|答案与解析|答案示例|评分标准|答案)\s*[:：]?\s*\**\s*", "", t2)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"^(?:本题|本小题)?(?:参考)?答案\s*[:：]\s*", "", t.strip())
    return t.strip()


def ans_block_scores(ans: str) -> tuple[dict[int, float], dict[int, str]]:
    """从答案区块头读大题分值：`16.(10分)` / `17．（12分）参考答案要点：` / `19.（12 分）`。"""
    scores: dict[int, float] = {}
    raw: dict[int, str] = {}
    for m in ANS_HDR.finditer(ans):
        no = int(m.group(1))
        if not (1 <= no <= 60):
            continue
        eol = ans.find("\n", m.end())
        line = ans[m.start(): (eol if eol >= 0 else len(ans))]
        raw.setdefault(no, line.strip())
        mm = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]", line)
        if mm and no not in scores:
            scores[no] = float(mm.group(1))
    return scores, raw


def ans_sub_scores(block: str, n: int) -> list[float] | None:
    """答案区里各小问末尾的（N分）→ 小问分值（上期末/二模 常见写法）。"""
    if not block or n <= 0:
        return None
    parts = split_answer_subs(block, n) if n > 1 else [block]
    if not parts:
        return None
    out: list[float] = []
    for part in parts:
        hits = SCORE_RE.findall(part or "")
        out.append(float(hits[-1]) if hits else None)
    if any(x is None for x in out):
        return None
    return out


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


def renumber_bare_sub_markers(text: str) -> str:
    """答案块里小问被 docx 自动编号写坏：`（1）…` 后面第二问变成 `1. …`（通州 2024 期末 19 题）
    → 按顺序还原成 `（2）`。"""
    out, cur = [], 0
    for ln in text.split("\n"):
        m = re.match(r"^[ \t]*[（(]\s*([1-9])\s*[）)]", ln)
        if m:
            cur = int(m.group(1))
        else:
            m2 = re.match(r"^[ \t]*(\d{1,2})\s*[．.、]\s*", ln)
            if m2 and cur >= 1 and int(m2.group(1)) == 1:
                ln = f"（{cur + 1}）" + ln[m2.end():]
                cur += 1
        out.append(ln)
    return "\n".join(out)


def split_answer_subs(text: str, n: int):
    """按 (1)(2) 把答案块切成 n 段；海淀的 `（4分）（2）` 挤在同一行、昌平把 (1)(2) 全挤在一行，也认。"""
    if n <= 1:
        return [strip_ans_head(re.sub(r"^\s*[（(]\s*1\s*[）)]\s*", "", text.strip()))]

    def collect(strict: bool):
        marks = []
        for m in SUB_ANS.finditer(text):
            if strict:
                line_start = text.rfind("\n", 0, m.start()) + 1
                head = text[line_start:m.start()]
                ok = not head.strip(" \t*#")
                if not ok and head.rstrip().endswith(("）", ")")) and len(head.strip()) <= 40:
                    ok = True
                if not ok:
                    continue
            marks.append(m)
        seq, cur = [], 1
        for m in marks:
            if int(m.group(1)) == cur:
                seq.append(m)
                cur += 1
                if cur > n:
                    break
        return seq if len(seq) == n else None

    seq = collect(True) or collect(False)      # 先按行首切，切不开再放开（同行挤多个小问）
    if not seq:
        # 小问标记少了右括号（`**（1说明、时间(7分)：…`）→ 用宽松标记再切一次
        loose = []
        for m in re.finditer(r"[（(]\s*([1-9])\s*(?:[）)]|(?=[^\d）)分]))", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if not text[line_start:m.start()].strip(" \t*#"):
                loose.append(m)
        seq2, cur = [], 1
        for m in loose:
            if int(m.group(1)) == cur:
                seq2.append(m)
                cur += 1
                if cur > n:
                    break
        if len(seq2) == n:
            out = []
            for i, m in enumerate(seq2):
                end = seq2[i + 1].start() if i + 1 < len(seq2) else len(text)
                out.append(strip_ans_head(text[m.end():end]))
            if all(o.strip() for o in out):
                return out
    if not seq:
        # 没有 (1)(2) 标记，但答案里用「特征1…特征N」这类序号标了 n 条 → 按它切
        marks = []
        for kw in ("特征", "方面", "措施", "表现", "原因", "影响", "意义", "阶段"):
            mm = [m for m in re.finditer(kw + r"\s*([1-9１-９])", text)]
            if len(mm) == n:
                marks = mm
                break
        if marks:
            out = []
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
                out.append(strip_ans_head(text[m.start():end]))
            if all(o.strip() for o in out):
                return out
        # 没有 (1)(2) 标记，但答案里有 n 个「（N分）」标记 → 按分值标记切（房山 2023 一模 16 题）
        hits = [h for h in SCORE_RE.finditer(text)
                if re.match(r"\s*(?:\n\s*\n|$)", text[h.end():h.end() + 6])]
        if len(hits) >= n:                     # 多出来的分值标记是小问内部的分点，切前 n-1 个
            cut = hits[: n - 1]
            out = []
            prev_end = 0
            for h in cut:
                out.append(strip_ans_head(text[prev_end:h.end()]))
                prev_end = h.end()
            out.append(strip_ans_head(text[prev_end:]))
            if all(o.strip() for o in out):
                return out
        # 没有 (1)(2) 标记、答案本来就是若干自然段（西城 2023 一模 16 题「发展：…」「原因：…」）
        paras = [x.strip() for x in re.split(r"\n\s*\n", text.strip()) if x.strip()]
        # 先去掉「共10分」「（10分）」「参考答案要点」这类段头
        paras = [x for x in paras
                 if not re.fullmatch(r"(?:共\s*\d+(?:\.\d+)?\s*分|[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]"
                                     r"|参考答案(?:要点|示例)?[:：]?|答案(?:示例)?[:：]?)", x.strip())]
        if len(paras) == n:
            return [strip_ans_head(x) for x in paras]
        if len(paras) > n and len(paras) % n == 0:      # 每问 2 段这种 → 均匀分块
            k = len(paras) // n
            return [strip_ans_head("\n\n".join(paras[i * k:(i + 1) * k])) for i in range(n)]
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
            text = " ".join(sb["text"] for sb in e["subs"])
            lab = "观点论证" if re.search(r"谈谈|看法|认识|理解|评析|评价|观点|论证|阐释|阐述", text) \
                else "史料分析"
            got[e["no"]] = (lab, "规则未命中，按兜底规则判（--no-ai 或模型不可用）")
            review.append(f"复核：第{e['no']}题分类规则未命中，兜底判为 {lab}，请人工确认")
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


def split_tail_choices(title: str, choices: list[dict], each: float, review: list[str]):
    """原卷某题的题号+题干漏印时，从前一题的段尾把「表格+组合项+选项」切出来单列一题。"""
    rules = SPLIT_TAIL_CHOICE.get(title) or []
    if not rules:
        return
    byid = {q["id"]: q for q in choices}
    for frm, pat, new_no, why in rules:
        src = byid.get(frm)
        if not src or new_no in byid:
            review.append(f"复核：题号切分规则（第{frm}题→第{new_no}题）找不到对应题目，请人工确认")
            continue
        m = re.search(pat, src.get("content") or "")
        if not m:
            review.append(f"复核：题号切分规则（第{frm}题→第{new_no}题）没找到切分锚点，请人工确认")
            continue
        head, tail = (src["content"][: m.start()]).strip(), (src["content"][m.start():]).strip()
        opts, rest = split_options(tail)
        if opts:
            opts = sorted(opts, key=lambda o: "ABCD".index(o["id"]))
        src["content"] = head
        item = {"id": new_no, "questionType": "single", "content": tidy(rest),
                "options": [{"id": o["id"], "label": tidy(o["label"])} for o in (opts or [])],
                "answer": "", "analysis": "", "score": each}
        if not item["options"]:
            review.append(f"复核：第{new_no}题切出来后没切到 A–D 选项，请人工核对")
        choices.append(item)
        choices.sort(key=lambda q: q["id"])
        review.append(f"复核：{why}")


def split_option_images(title: str, choices: list[dict], md_dir: Path, review: list[str]):
    """有的卷把 A–D 四个选项画成图片（四幅横排、或两张图各两幅）。按等分裁开，每个选项挂一张。"""
    rules = OPTION_IMAGE_SPLIT.get(title) or []
    if not rules:
        return
    try:
        from PIL import Image
    except Exception as e:                                    # 没装 PIL 就跳过并留痕
        review.append(f"复核：需要裁切选项图但 PIL 不可用（{str(e)[:40]}），请人工补选项")
        return
    byid = {q["id"]: q for q in choices}
    for no, parts, why in rules:
        q = byid.get(no)
        if not q:
            review.append(f"复核：选项图裁切规则里第{no}题不存在，请人工确认")
            continue
        made, letter_i, ok = [], 0, True
        for rel, n in parts:
            src = md_dir / rel
            if not src.exists():
                review.append(f"复核：选项图裁切规则的图片不存在：{rel}")
                ok = False
                break
            im = Image.open(src)
            w, h = im.size
            stem = Path(rel).stem
            for k in range(n):
                letter = "ABCD"[letter_i]
                letter_i += 1
                im.crop((int(w * k / n), 0, int(w * (k + 1) / n), h)).save(
                    md_dir / "images" / f"{stem}_opt{letter}.png")
                made.append({"id": letter, "label": f"![](images/{stem}_opt{letter}.png)"})
            (md_dir / "images").mkdir(parents=True, exist_ok=True)
        if not ok or len(made) != 4:
            review.append(f"复核：第{no}题选项图裁切只得到 {len(made)} 个选项，请人工核对")
            continue
        q["options"] = made
        for rel, _n in parts:
            q["content"] = _tok_re(rel).sub("", q.get("content") or "").strip()
        review.append(f"复核：第{no}题选项是一整图，已裁成 4 张并挂到 options。{why}")


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

    txt = join_spaced_numbers(normalize_fullwidth(unescape_dots(md.read_text(encoding="utf-8"))))
    review: list[str] = []
    body, ans = split_paper_hist(txt, review)
    if not ans:
        raise RuntimeError("没找到「参考答案」区")
    ans = normalize_paren_digits(ans)

    body = normalize_paren_digits(body, line_start_only=True)
    m2 = PART2.search(body)
    m1 = PART1.search(body)
    if not m2:
        # 兜底：没有「第二部分」字样（延庆/石景山 2023 一模只写了「一、选择题」）→
        # 用「首个带材料/小问(1)/大分值(≥6分) 的题号」当大题起点
        marks = [(mm.start(), int(mm.group(2))) for mm in QMARK.finditer(body)]
        cand = None
        for i, (pos, no) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
            seg = body[pos:end]
            if (re.search(r"(?m)^[ \t]*\**\s*材料[一二三四1234]", seg)
                    or SUBQ.search(seg)
                    or any(float(x) >= 6 for x in SCORE_RE.findall(seg))):
                cand = pos
                break
        if cand is None:
            raise RuntimeError("没找到「第二部分」，也判不出大题起点")
        body1, body2 = body[(m1.start() if m1 else 0): cand], body[cand:]
        first_no = next(int(mm.group(2)) for mm in QMARK.finditer(body2))
        review.append(f"复核：本卷没有「第二部分」字样（卷面只写了「一、选择题」这类标题），"
                      f"大题起点按「首个带材料/小问/大分值的题」自动判定为第 {first_no} 题，"
                      f"请人工确认选择题与该题的分界")
    else:
        body1 = body[(m1.start() if m1 else 0): m2.start()]
        body2 = body[m2.start():]

    # ① 先扫一遍题号，确定「选择题到哪、大题从几号开始」（老卷/文综卷不从 1 开始、题数也不是 15）
    decl = DECL_N.search(body1[:1800])
    want = int(decl.group(1)) if decl else None
    occ = list(QMARK.finditer(body1))
    uniq = sorted({int(m.group(2)) for m in occ})
    if want and uniq != list(range(1, want + 1)):
        # 题号坏掉且个数对不上（有选项行也被写成「1.」）→ 只保留「像题干」的标记再重排
        cand = [m for m in occ if _looks_like_question(body1, m)]
        if len(cand) == want:
            body1 = renumber_choice_markers(body1, cand)
            review.append(f"复核：本卷 md 的选择题题号被 docx 自动编号写坏（{want} 道题的题号取值是 "
                          f"{uniq}），已按**文档顺序**重排为 1–{want}（剔除了被误编号的选项行），"
                          f"请人工核对题号与答案表是否一一对应")
            occ, uniq = [], list(range(1, want + 1))
    if want and len(occ) == want and uniq != list(range(1, want + 1)):
        # 题号整体坏掉（docx 自动编号：多题都写成「1.」）→ 按出现顺序重排 1..N
        body1 = renumber_choice_markers(body1, occ)
        review.append(f"复核：本卷 md 的选择题题号被 docx 自动编号写坏（{want} 道题的题号取值是 "
                      f"{uniq}），已按**文档出现顺序**重排为 1–{want}，"
                      f"请人工核对题号与答案表是否一一对应")
    else:
        nos1 = uniq
        gaps = [k for k in range(1, max(nos1)) if k not in nos1] if nos1 else []
        if gaps:
            body1 = repair_glued_numbers(body1, gaps, review)
        # 卷面声明了「共 N 题」但切少了（题号粘在上一题选项行尾 / 漏了点）
        if want:
            now = sorted({int(m.group(2)) for m in QMARK.finditer(body1)})
            if now and len(now) < want:
                tail = [k for k in range(1, want + 1) if k not in now]
                body1 = repair_glued_numbers(body1, tail, review)
    nos1 = sorted({int(m.group(2)) for m in QMARK.finditer(body1)})
    q_nos = nos1
    if not q_nos:
        raise RuntimeError("第一部分没有切到题号")
    n_choice = len(q_nos)
    min_essay_no = max(q_nos) + 1
    nos2 = sorted({int(m.group(2)) for m in QMARK.finditer(body2)})
    ans_nos_pre = sorted({int(m.group(1)) for m in ANS_HDR.finditer(ans)
                          if int(m.group(1)) >= min_essay_no})
    egaps = [k for k in range(min_essay_no, max(nos2)) if k not in nos2] if nos2 else []
    # 末尾还有一道大题、只是题号漏了点（`20 (8分）历史作业`）→ 只在「题号紧跟（N分）」时才认，
    # 避免把材料里的序号当成新大题
    if nos2:
        for k in range(max(nos2) + 1, max(nos2) + 3):
            if re.search(rf"(?m)^[ \t]*\**\s*{k}\s*[（(]\s*\d+(?:\.\d+)?\s*分", body2):
                egaps.append(k)
                break
    if egaps:
        body2 = repair_glued_numbers(body2, egaps, review)
        nos2 = sorted({int(m.group(2)) for m in QMARK.finditer(body2)})
    # docx 把「18.」写成了「8.」（丢了十位）：答案区有 18、正文只有 8 → 还原成 18
    _miss = [k for k in ans_nos_pre if k not in set(nos2)]
    for k in list(_miss):
        if (k - 10) < min_essay_no:
            pat = rf"(?m)^([ \t]*\**\s*){k - 10}(\s*[．.、])"
            if re.search(pat, body2):
                body2 = re.sub(pat, rf"\g<1>{k}\g<2>", body2, count=1)
                review.append(f"复核：第{k}题的题号在 md 里被 docx 写成了「{k - 10}.」（丢了十位），已还原")
    nos2 = sorted({int(m.group(2)) for m in QMARK.finditer(body2)})

    # 答案区里有、正文里却没切到的题号（`20 (8分）历史作业` 漏了点 / 粘行）→ 补
    ans_nos = sorted({int(m.group(1)) for m in ANS_HDR.finditer(ans)
                      if int(m.group(1)) >= min_essay_no})
    lack = [k for k in ans_nos if k not in set(nos2)]
    if lack:
        body2 = repair_glued_numbers(body2, lack, review)
        nos2 = sorted({int(m.group(2)) for m in QMARK.finditer(body2)})
    essay_nos = [k for k in nos2 if k >= min_essay_no]

    # ② 参考答案区：分块 + 分值
    blocks_raw = split_answer_blocks(ans, expected=essay_nos)
    blocks = {k: v for k, v in blocks_raw.items() if k >= min_essay_no}
    _missing = [k for k in essay_nos if k not in blocks]
    if _missing:
        fb = first_misnumbered_block(ans)
        if fb:
            blocks[_missing[0]] = fb
            review.append(f"复核：第{_missing[0]}题的答案块在 md 里被 docx 自动编号写成了「1.」，"
                          f"已按位置补回该题，请人工核对")
    _n = fill_missing_bare_ones(blocks, essay_nos, ans)
    if _n:
        review.append(f"复核：有 {_n} 道大题的答案块被 docx 自动编号写成裸「1.」，"
                      f"已按缺失题号顺序回填（第 {essay_nos[0]}–{essay_nos[-1]} 题），请人工核对答案归属")
    _g = fill_missing_by_gap(blocks, essay_nos, ans)
    if _g:
        review.append(f"复核：有 {_g} 道大题的题头在 md 里丢了，已用「相邻答案块之间」的文本回填，"
                      f"请人工核对答案归属")
    _l = fill_missing_loose(blocks, essay_nos, ans)
    if _l:
        review.append(f"复核：有 {_l} 道大题的题头在 md 里没有独立成行（和上一段粘在一起），"
                      f"已按题号定位回填，请人工核对答案归属")
    if essay_nos and len(blocks) < max(1, len(essay_nos) // 2):
        seq = split_answer_blocks_sequential(ans, essay_nos)
        if seq:
            blocks = seq
            review.append(f"复核：答案区的大题题号被 docx 自动编号写坏（写成 1. 2. 3.…），"
                          f"已按「第二部分」之后的出现顺序映射回第 {essay_nos[0]}–{essay_nos[-1]} 题，"
                          f"请人工核对答案与题号是否一一对应")
    ab_scores, _ab_heads = ans_block_scores(ans)

    # ③ 分值：选择题每题、非选择题合计
    per_q = [float(m.group(3)) for m in QMARK.finditer(body1) if m.group(3)]
    each = detect_choice_each(body1, ans, n_choice, per_q, review)
    ch_score = round(each * n_choice, 4)

    # ③' 选择题道数以实际切出的为准（个别卷题号漏印，靠规则补出来的题不算在 body1 数字里）
    choice_qs = parse_choices(body1, review, each=each,
                              explicit={no: each for no in q_nos}, title=title)
    n_choice = len(choice_qs)
    ch_score = round(sum(q["score"] for q in choice_qs), 4)
    essay_total = detect_essay_total(body2, ans, 100 - ch_score, review)
    if essay_total is not None and essay_total > 100 - ch_score + 1e-6:
        review.append(f"复核：卷面声明的非选择题总分 {essay_total:g} 分超过「100 − 选择题 {ch_score:g}」"
                      f"（老文综卷的声明值含政治/地理），改按 100 − 选择题 = {100 - ch_score:g} 分处理")
        essay_total = round(100 - ch_score, 4)

    # ④ 小问分值兜底（答案区各小问末尾的（N分））
    sub_scores: dict[int, list[float]] = {}
    if essay_total is not None:
        for no in sorted(blocks):
            n_sub = len(SUBQ.findall(blocks[no])) or 1
            got = ans_sub_scores(clean_ans_block(blocks[no]), n_sub)
            if got:
                sub_scores[no] = got

    essays = parse_essays(body2, review, min_no=min_essay_no, ans_blocks=blocks,
                          ans_scores=ab_scores, ans_sub_scores=sub_scores,
                          essay_total=essay_total)
    if not essays:
        raise RuntimeError("第二部分没切到大题")
    if n_choice != N_CHOICE:
        review.append(f"复核：选择题切出 {n_choice} 道（北京高三常规卷是 15 道，本卷按卷面为准），"
                      f"每题 {each:g} 分、合计 {ch_score:g} 分")
    got_sum = sum(e["score"] for e in essays)
    if essay_total is not None and abs(got_sum - essay_total) > 1e-6:
        review.append(f"复核：大题分值合计 {got_sum:g} ≠ 卷面声明非选择题 {essay_total:g} 分，"
                      f"差额记到最后一题")
        essays[-1]["score"] = essays[-1]["score"] + (essay_total - got_sum)
        essays[-1]["scores"][-1] = essays[-1]["scores"][-1] + (essay_total - got_sum)

    apply_image_fixes(title, choice_qs, review)
    split_option_images(title, choice_qs, md.parent, review)
    for _q in choice_qs:
        _ov = (OPTION_TEXT_OVERRIDE.get(title) or {}).get(_q["id"])
        if _ov:
            _q["options"] = [{"id": L, "label": t} for L, t in zip("ABCD", _ov)]
            review.append(f"复核：第{_q['id']}题的选项是一整张表格图，已按图人工转录成文字选项（请抽查）")

    # 选择题答案
    manual = load_manual_answers(title)
    letters = parse_choice_letters(ans, review, n_choice, [q["id"] for q in choice_qs])
    if manual:
        for k, v in (manual.get("choice") or {}).items():
            letters[int(k)] = v
        review.append(f"复核：本卷参考答案是扫描图（无文字层），答案由人工转录：{manual.get('note','')}")
    for q in choice_qs:
        q["answer"] = letters.get(q["id"], "")
        if not q["answer"]:
            review.append(f"复核：第{q['id']}题在参考答案区找不到字母答案")

    # 非选择题答案
    cats = classify_essays(essays, args, review)
    sections_essay = []
    for e in essays:
        no = e["no"]
        blk = blocks.get(no, "")
        blk = clean_ans_block(blk) if blk else ""
        if not blk:
            review.append(f"复核：第{no}题在参考答案区找不到对应块")
        blk = renumber_bare_sub_markers(blk)
        subs_a = split_answer_subs(blk, len(e["subs"])) if blk else None
        if subs_a is None:
            review.append(f"复核：第{no}题参考答案块里按 (1)(2) 切不开 {len(e['subs'])} 段，"
                          f"整块放进第一问，其余留空待人工补")
            subs_a = [blk] + [""] * (len(e["subs"]) - 1)
        if manual and str(no) in (manual.get("essays") or {}):
            ms = manual["essays"][str(no)]
            subs_a = (list(ms.get("subs") or []) + [""] * len(e["subs"]))[: len(e["subs"])]
            if ms.get("total"):
                e["score"] = float(ms["total"])
        questions = []
        blocks_sub: list[str] = []          # 每小问「（N）设问」段（含表格修复后的样子），供 content 复用
        for i, sb in enumerate(e["subs"]):
            prompt = strip_score(sb["text"]).strip()
            qid = int(f"{no}{i + 1:02d}") if sb["no"] is not None else int(f"{no}01")
            lead = f"（{sb['no']}）" if sb["no"] is not None else ""
            qtext = tidy(f"{lead}{prompt}")
            qtext = repair_tables(qtext, review, f"第{no}题（{sb['no']}）设问")
            _before = qtext
            qtext = strip_stem_residue(qtext)
            _short = sub_prompt_only(qtext)
            if _short != qtext:
                review.append(f"复核：第{no}题（{sb['no']}）设问后面的材料被并进了小问题干，已截掉"
                              f"（材料仍在 section.content 里原样保留一次）")
                qtext = _short
            if qtext != _before:
                review.append(f"复核：第{no}题（{sb['no']}）题干开头的分区标题被 docx 串进了正文，已剔除")
            if not qtext.strip():
                review.append(f"复核：第{no}题（{sb['no']}）题干被清洗后为空，请人工核对原卷设问")
            blocks_sub.append(qtext)
            ans_text = repair_tables((subs_a[i] or "").strip(), review, f"第{no}题（{sb['no']}）参考答案")
            # 答案开头重复了一遍设问（教师版常见「设问 + 等级描述表」）→ 把重复的题干剪掉
            _qs = re.sub(r"\s+", "", qtext)
            if len(_qs) >= 20 and re.sub(r"\s+", "", ans_text).startswith(_qs[:20]):
                _cut = ans_text.find(qtext.strip()[:20])
                if _cut >= 0:
                    ans_text = ans_text[_cut + len(qtext.strip()):].lstrip(" \n|")
                else:
                    ans_text = re.sub(r"^[^\n]*\n+", "", ans_text)
                review.append(f"复核：第{no}题（{sb['no']}）参考答案开头重复了设问，已剪掉重复部分")
            questions.append({
                "id": qid, "questionType": "input",
                "content": qtext, "answer": ans_text,
                "analysis": "", "score": e["scores"][i],
            })
            if not (subs_a[i] or "").strip():
                review.append(f"复核：第{no}题（{sb['no'] or 1}）参考答案为空，请人工补")

        # section.content：**单栏不变**——材料 + 各小问设问内联，每个设问后紧跟一个 <AnswerBox> 答题框。
        # 材料与小问交替排列的卷（材料一 → （1）→ 材料二 → （2））按**卷面原顺序**拼，不重排。
        parts = []
        segs = e.get("segments")
        if segs:
            idx = 0
            for kind, sno, txt in segs:
                if kind == "sub":
                    if idx >= len(blocks_sub):
                        break
                    block = blocks_sub[idx]
                    idx += 1
                    sep = "\n\n" if "\n" in block else ""
                    parts.append(f"{block}{sep}{ANSWER_BOX}")
                else:
                    mblock = tidy(txt)
                    mblock = fix_two_col_tables(mblock, f"第{no}题材料", review)
                    mblock = repair_tables(mblock, review, f"第{no}题材料")
                    parts.append(mblock)
        else:
            material = tidy(e["material"])
            if material:
                material = fix_two_col_tables(material, f"第{no}题材料", review)
                material = repair_tables(material, review, f"第{no}题材料")
                parts.append(material)
            for block in blocks_sub:
                sep = "\n\n" if "\n" in block else ""     # 多段设问（含「要求：…」）答题框另起一段
                parts.append(f"{block}{sep}{ANSWER_BOX}")
        content = finalize_content("\n\n".join(p for p in parts if p.strip()))
        if not e["material"]:
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
    ch_score = round(sum(q["score"] for q in choice_qs), 4)
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

    # 老卷 / 文综卷的坑：逐条留痕（供入库后人工复核）
    if n_choice == 12 and abs(each - 4) < 1e-9:
        review.append("复核：本卷是**北京文综卷的历史部分**（12 道选择题×4 分 + 3 道大题）。"
                      "md 教师版已把历史部分单独抽出并重排题号：选择题 1–12、大题 13 起"
                      "（原卷题号是 12–23 的选择题 + 37/40 两道大题，与 JSON 题号不一致，"
                      "以「卷面抽出的历史部分连续题号」入库）；答案表/解析也是重排后的口径，已交叉核对")
    if re.search(r"故答案为|【解答】|【分析】", ans):
        review.append("复核：答案区是菁优网解析版式（【分析】【解答】【点评】），入库的 answer 已剔除"
                      "解析脚手架，只保留「故答案为／【答案】」之后的标准答案；选择题字母取自 `故选：X`，"
                      "已与卷面答案逐题核对")
    if n_choice not in (15, 12):
        review.append(f"复核：本卷选择题 {n_choice} 道（非北京高三常规 15 道；海淀期末 22 道×2 分、"
                      f"西城期末 20 道×2.5 分这类按卷面声明处理），请人工确认题数与分值")
    review.append("复核：答题框已换成 <AnswerBox></AnswerBox>（单栏版式不变：材料+设问仍内联在 "
                  "section.content，每个小问后跟一个答题框；小问仍是 questionType=input；"
                  "全文无 <InputLong>/<Input> 残留，高度由前端按档位给）")
    review.extend(f"复核：{x}" for x in STATIC_NOTES.get(title, []))

    total = round(sum(s["score"] for s in sections), 4)
    if abs(total - round(total)) < 1e-9:
        total = int(round(total))
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
            f"（选择 {len(choice_qs)}×{each:g}={ch_score:g}，非选择 {len(sections_essay)} 大题），"
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
    """GFM 表格结构自查。**必须按真实相邻行分块**：材料里常有多张表，
    旧写法把所有 `|` 行压到一起，会把两张正常表格误判成「分隔行 2 个」。"""
    errs = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            block.append(lines[i].strip())
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
        if sep[0] != 1:
            errs.append(f"表格分隔行不在第 2 行（在第 {sep[0] + 1} 行）：{block[0][:40]}")
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


def check_one(jf: Path, type_dir: str = "一模") -> tuple[dict, list[str], list[str]]:
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
        errs.append(f"paper.totalScore={p.get('totalScore')}（北京历史卷满分应为 100）")
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
        if ids != list(range(1, len(ids) + 1)):
            errs.append(f"选择题题号不连续（应从 1 连续编号）：{ids[:3]}…{ids[-3:] if len(ids) > 3 else ''}")
        if len(set(ids)) != len(ids):
            errs.append("选择题题号有重复")
        each = None
        if ids:
            each = (s.get("score") or 0) / len(ids)
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
            if each is not None and abs((q.get("score") or 0) - each) > 1e-6:
                errs.append(f"第{q.get('id')}题 score={q.get('score')}（本题组应为每题 {each:g} 分）")
        if abs(ssum - (s.get("score") or 0)) > 1e-6:
            errs.append(f"选择题 Σscore {ssum} ≠ section.score {s.get('score')}")
        stat["choice_each"] = each

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
        if abs(ssum - (s.get("score") or 0)) > 1e-6:
            errs.append(f"{s.get('title')} Σquestions.score {ssum} ≠ section.score {s.get('score')}")
        if not content.strip():
            errs.append(f"{s.get('title')} content 为空（材料没进来）")
        ids = set()
        sec_body = _norm_ws(content)
        # 材料只出现一次：材料正文（去掉设问与答题框后）不允许整段重复
        body_only = sec_body
        for q in qs:
            probe = _norm_ws((q.get("content") or "").strip())
            if probe:
                body_only = body_only.replace(probe, "")
        body_only = body_only.replace(_norm_ws(ANSWER_BOX), "")
        # 按「长段落」判重：设问里提到「材料一」是正常的，只有整段材料被复制才是 bug
        seen_para: dict[str, int] = {}
        for para in content.split("\n\n"):
            pn = _norm_ws(para)
            if len(pn) < 40 or pn.startswith("|"):
                continue
            seen_para[pn] = seen_para.get(pn, 0) + 1
        for pn, c in seen_para.items():
            if c > 1:
                errs.append(f"{s.get('title')} 材料段落在 section.content 里重复了 {c} 次：{pn[:24]}…")
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
    if nos and stat.get("choice") and min(nos) <= stat["choice"]:
        errs.append(f"大题题号从 {min(nos)} 开始，与选择题 {stat['choice']} 题重叠")

    # 分值合计
    paper_sum = sum(s.get("score") or 0 for s in secs)
    if abs(paper_sum - (p.get("totalScore") or 0)) > 1e-6:
        errs.append(f"题组合计 {paper_sum} ≠ paper.totalScore {p.get('totalScore')}")

    # 图片逐张落盘
    refs = sorted(set(EXAM_IMG.findall(raw)))
    bad = [r for r in refs if not img_exists("/exam-images/" + r)]
    for r in bad:
        errs.append(f"图片不存在：/exam-images/{r}")
    stat["imgs"] = len(refs)

    # 未引用的图片（提示，不算错）
    pdirs = paper_dirs_for(stat["title"], type_dir)
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
                stat, errs, notes = check_one(jf, type_dir)
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
