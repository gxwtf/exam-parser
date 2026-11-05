#!/usr/bin/env python3
"""地理整卷解析（选择题 + 非选择题）→ 题库导入 JSON（`output_json_geography/<类型>/<卷名>.json`）。

链路：docx→MD（batch_convert_docx_to_md.py）→ 本脚本 → `gxexercise/scripts/import-subject-json.ts --subject 地理`

输出结构（字段名与导入脚本严格一致，**不要改名字**，与生物同构）：

    {"paper": {"title","subject","source","grade","year","totalScore","duration","tags"},
     "sections": [
       {"type":"选择题","questionType":"geography-choice","title":"一、选择题","category":"选择","score":45,
        "questions":[{"id":1,"questionType":"single","content":…,
                      "options":[{"id":"A","label":…}×4],"answer":"B","analysis":"","score":3}]},
       {"type":"非选择题","questionType":"geography-fill","title":"二、非选择题（第16题）",
        "category":"自然地理","score":14,
        "content":"材料 + 各小问设问，每问后紧跟 <InputLong></InputLong>",
        "questions":[{"id":1601,"questionType":"input","content":"（1）…","answer":…,"analysis":"","score":4}]}
     ]}

地理卷的本科特点（与生物不同，**没有可规则识别的答题空**）：

- 第一部分 15 道单选、3 分/题；**大量共享材料**——卷面「某校开展…图1(a)…读图，回答第 1、2 题」
  「图3为…读图，回答第 5、6 题」。本脚本把**材料与图复制进该组每道题的 content**（每题自洽，
  单题练习/组卷/PDF 都能独立显示）。分组不是看题号，而是看**材料块出现的位置**：
  上一题选项之后、下一题题号之前的文字＝下一组的材料；题号前面粘着上一段落的图时，
  若该题题干自带「如图为…」这类引子，则它自成一组（丰台 3 题的写法）。
- 第二部分 16–20 大题：一个大题一个 section；`content` = 全部材料（含「任务一/任务二」这类小标题）
  + 按题面顺序的各小问设问，每问**紧接着**一个占位符：长答/论述一律 `<InputLong></InputLong>`
  （地理三份卷子没有真正一词填空，`<Input></Input>` 仅保留给「填一个词/数」的情形）。
- 分值：卷面 `(1)…(4分)` 里带分值的直接取；**丰台卷第二部分与设问都没写分值**（教师版丢了），
  按北京卷 14/10/12/12/7 的常规权重分配到各小问，Σ=55，逐条记 `.review.txt`。
- `category` 只能取 自然地理 / 人文地理 / 区域发展 / 图表判读（选择题 section 恒为 `选择`），
  一次 AI 调用分类，返回不在表里落「自然地理」并记 review；`--no-ai` 走关键词兜底。
- **图片（本科重点）**：docx 导出图与 PDF 内嵌图同源时二者等价，但 docx 那条链路会二次编码、
  且浮动图常被拆散/错位/重复。本脚本对每张图给出**显式清单**（`PAPERS[卷]["assets"]`）：
    * `("pdf", 页, 序号)` —— 用 `pdftocairo -svg` 拿到该插图在版面上的**精确 bbox**，
      再在 300dpi 渲染页上 `convert -crop` 裁出来（`fig-*.png`，带卷面图注、1:1 无重采样）；
    * `("docx", "imageN.jpg")` —— 直接复制 docx 导出图（PDF 里缺这张图，或 docx 的源图分辨率
      明显更高，例如丰台卷 docx 是 PDF 内嵌图的 2~5 倍）。
  图片引用一律写成 `/exam-images/<类型>/<学科>/<卷名>/images/<文件名>`（卷名不带「（教师版）」），
  文件落到 `output_docx_to_md/<类型>/<学科>/<卷名>/images/`。
  三份卷的分布：西城 14 张全 PDF 裁图；海淀 11 张 PDF 裁图 + 图2 只有 docx 有（教师版 PDF 丢了这张图）；
  丰台 docx 导出图分辨率是 PDF 的 2~5 倍，故 20 张全用 docx 导出图。

用法：
    python3 convert_geography_md.py -T 一模                 # 处理 output_docx_to_md/一模/地理/* 全部
    python3 convert_geography_md.py -p <md 文件或卷目录> [--force] [--no-ai] [--refig]
    python3 convert_geography_md.py --check                  # 自查（纯 Python，不调模型）

`--check` 断言：选择项/答案/占位符数/分值合计/题号连续/图片落盘/标签合法/(图宽过小提示)。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from convert_chinese_md import chat, rewrite_images, split_paper, log
from paths import GAOZHONG, OUTPUT_DOCX_TO_MD, OUTPUT_JSON_GEOGRAPHY
from src.answer_case import normalize_choice_case_in_paper

SUBJECT = "地理"
OUT_ROOT = OUTPUT_JSON_GEOGRAPHY
OUT = {"root": OUT_ROOT}

N_CHOICE = 15
CHOICE_SCORE = 3
ESSAY_TOTAL = 55
PDF_DPI = 300

CATS = ["自然地理", "人文地理", "区域发展", "图表判读"]

# 题号行：可带前置图片、可带（N分）。`1\.` / `1.` / `16．` 都吃
QMARK = re.compile(
    r"(?m)^[ \t]*((?:!\[[^\]]*\]\([^)]*\)[ \t]*)*)\**\s*(\d{1,2})\s*\**\s*[\\]?[.．、·・]\s*\**\s*"
    r"(?:[（(]\s*(\d+)\s*分\s*[）)])?\s*")
HDR_ESSAY = re.compile(
    r"(?m)^[ \t]*\**[ \t]*(?:第[二2]部分|二\s*[、.．]\s*(?:第[二2]部分|非选择题|综合题))")
SUBQ = re.compile(r"(?m)^[ \t]*[（(]\s*(\d{1,2})\s*[）)][ \t]*")
# 答案区专用：设问编号（（1））可能在行首，也可能紧跟在「12．【分析】」后面（菁优网教师版）
SUBQ_ANS = re.compile(r"(?m)(?:^|(?<=[\s。．；;：:、]))[（(]\s*(\d{1,2})\s*[）)][ \t]*")
# 卷面声明的题数 / 分值：「本部分共15题」「每题3分」「共45分」
DECL_CNT = re.compile(r"本部分共\s*(\d+)\s*(?:个)?(?:小题|题|道)")
DECL_PER = re.compile(r"每(?:小)?题\s*(\d+)\s*分")
DECL_SUM = re.compile(r"(?:本部分共\s*\d+\s*(?:个)?(?:小题|题|道)[^0-9]{0,12}共\s*(\d+)\s*分"
                      r"|本部分共\s*(\d+)\s*分|选择题\s*共\s*(\d+)\s*分)")
# 题面提到「图」的措辞（图质体检用：宽 < 500px 而题面要读图 → 提示补 PDF 截图）
FIG_MENTION = re.compile(r"图\s*\d|读图|如图|下图|上图|示意|分布图|统计图|景观图|略图|图（|图\(")
SCORE_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")
IMG_REF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
BLANK_PAREN = re.compile(r"[（(][\s\u3000]*[）)]\s*$")
EMPTY_PAREN = re.compile(r"[（(][\s\u3000]{0,3}[）)]")
CJK = re.compile(r"[\u4e00-\u9fff]")
# 题干自带材料引子（丰台卷「3．如下图为…」「8．如下图为…」这种，材料写在题号行里）
SELF_MAT = re.compile(r"^\s*(?:如下图为|下图为|如图为|图为|如表为|下表为|如下图|下图|如图|表\s*\d|图\s*\d)")
BOILER = re.compile(r"[（(]\s*考生务必将答案[^）)]*[）)]")
NAME_LINE = re.compile(r"(?m)^[ \t]*\**[ \t]*姓\s*名[：:].*$")
# 卷首抬头：卷名 / 「地 理」/ 日期 / 考试须知 / 分节标题 —— 都不该进题目内容
HEAD_LINE = re.compile(
    r"^(?:20\d{2}\s*北京|20\d{2}[.．-]\d{1,2}$|地\s*理$|地\s+理$|本试卷共|考试时长|考试时间|考生务必|"
    r"试卷共|第[一1]部分|本部分共|第二部分|姓\s*名|注\s*[:：]|选择题$|非选择题$|满分|"
    r"一\s*[、.]\s*选择题|选择题\s*[，,、]\s*本部分|答在答题卡|共\s*\d+\s*页)")

# 答题框：版式与生物/历史一致（材料 + 各小问内联在 section.content 里，小问是 input 小题，
# AI 批改的 {stem} 不变），只把占位符换成英语「阅读表达」那一款的大答题框 <AnswerBox></AnswerBox>
# （不带属性，高度由前端统一给）。地理三卷没有「一词/一数」的短填空，故不输出 <Input></Input>。
ANSWER_BOX = "<AnswerBox></AnswerBox>"
PH_ANY = re.compile(r"<AnswerBox\s*/?>(?:\s*</AnswerBox>)?|<Input(?:Long(?:\s+rows=\{\d+\})?)?></Input(?:Long)?>")
PH_ANY_BOX = re.compile(r"<AnswerBox\s*/?>(?:\s*</AnswerBox>)?")
PH_OPEN = re.compile(r"<(?:Input|AnswerBox)")


def placeholder_for(score: int | None = None) -> str:
    """答题框占位符（统一，不看分值）。"""
    return ANSWER_BOX

SYSTEM = "你是北京高三地理试卷的结构化录入员，只输出 JSON。"

CAT_PROMPT = r"""下面是北京高三地理一模试卷的若干道非选择题（题号 + 材料/设问摘要）。
请为每题判断所属模块，**只能从下面 4 类里选一个**：自然地理 / 人文地理 / 区域发展 / 图表判读

判定要点（按题目主线判）：
- 自然地理：大气运动与天气气候、水循环与河流水文水系、地貌与地质作用、土壤植被、自然环境的整体性
  与差异性、自然灾害的成因与防治等**自然要素**为主线；
- 人文地理：人口、城镇与乡村、农业/工业/交通等区位与产业活动、城市内部空间结构与城市更新、
  人地关系中的社会经济活动为主线；
- 区域发展：以**某一区域**为对象，考查区域特征、区域产业结构转型、区域协同/协调发展、
  生态治理与可持续发展、区域联系与产业转移等；
- 图表判读：以**读图/绘图/统计图判读**本身为主线、且没有明显自然/人文/区域主线的题
  （只是题中某一小问要求「绘图/画框图」时，仍按整题主线归类，不要归到这里）。

注意：
1. 一题里既有自然条件又有产业活动时，看**设问落脚点**（问产业/城市/发展→人文或区域；问气候/地貌/水文→自然）；
2. 以真实区域（某省/某流域/某县/某市）为载体谈**发展、转型、区域协同、治理措施**的，优先「区域发展」；
   若区域只是背景、设问主线是**河流水文水系、地貌形成、气候变化、生态脆弱成因**等自然要素的
   规律与成因，归「自然地理」（例如「描述水系特征 / 概括产汇流规律 / 说明地貌演化过程」）；
3. 一句话写 why（≤20 字，指出判据关键词）。

只输出 JSON：{{"16": {{"category": "自然地理", "why": "河流水文与生态脆弱"}}}}
题目：
{ITEMS}
"""

# ──────────────────────────────────────────────────────────────────
# 每卷的图片清单 / 文本补丁 / 人工分值
#   assets: [(目标文件名, 来源)]；来源 = ("pdf", 页码, 该页插图序号) | ("docx", "imageN.ext")
#   imgmap: {md 里的 docx 图名: 目标文件名 或 None(丢弃重复图)}
#   patches: 解析前对「非选择题区」做的文本替换（GFM 表格复原、整页图片补文字版）
#   essay_scores: {大题号: (大题分值, [各小问分值])}，只在卷面没写分值时给
# ──────────────────────────────────────────────────────────────────
PAPERS = {
    "2026北京西城高三一模地理": {
        "pdf": "2026北京西城高三一模地理（教师版）.pdf",
        "assets": [
            ("fig-1a.png", ("pdf", 1, 0)), ("fig-1b.png", ("pdf", 1, 1)), ("fig-2.png", ("pdf", 1, 2)),
            ("fig-3.png", ("pdf", 2, 0)), ("fig-4.png", ("pdf", 2, 1)),
            ("fig-5.png", ("pdf", 3, 0)), ("fig-6.png", ("pdf", 3, 1)),
            ("fig-7.png", ("pdf", 4, 0)), ("fig-8.png", ("pdf", 4, 1)),
            ("fig-9a.png", ("pdf", 5, 0)), ("fig-9b.png", ("pdf", 5, 1)),
            ("fig-10.png", ("pdf", 6, 0)), ("fig-11.png", ("pdf", 6, 1)),
            ("fig-12.png", ("pdf", 8, 0)),
        ],
        "imgmap": {
            "image1.jpg": "fig-1a.png", "image2.jpg": "fig-1b.png", "image3.jpg": "fig-2.png",
            "image4.jpg": "fig-3.png", "image5.jpg": None,            # image5 是图4 的缩小副本
            "image6.png": "fig-4.png", "image7.png": "fig-5.png", "image8.png": "fig-6.png",
            "image9.jpg": None,                                        # image9 是图6 的缩小副本（且被塞进了 B 选项里）
            "image10.jpg": "fig-7.png", "image11.jpg": "fig-8.png",
            "image12.jpg": "fig-9a.png", "image13.jpg": "fig-9b.png",
            "image14.jpg": "fig-10.png", "image15.jpg": "fig-11.png", "image16.png": "fig-12.png",
        },
        "patches": [
            # 图12 是一整页「调研报告」的图片（原生仅 524×630，整页小字，屏幕上读不清）
            # → 保留原图，同时把报告内容逐条转录成文字，保证题目可用（review 记一条）
            ("![](images/image16.png)", """\
![](images/image16.png)

【图12 调研报告（节选）文字版】
城市更新是推动城市高质量发展的综合性、系统性战略行动，旨在推动城市空间复活、文脉延续等内涵式发展和建设现代化人民城市。
2025 年全国城市更新项目：改造老旧小区 2.71 万个、499 万户；加装电梯 1.4 万部；打造"口袋公园"4700 多个、城市绿道 5800 多千米；建设、改造地下管网 15.6 万千米。城市更新的方向是创新、宜居、美丽、韧性、文明、智慧。
- 零碳楼宇、"微改造"后的北京市西城区大吉巷：集中绿地、下沉街区、文保区、地铁站、地下停车场，打造集休闲、商业、办公为一体的开放城市空间。
- 工业老城，蝶变更新：包头市将包头棉纺厂旧址改造成集工业遗产展示、创意设计、艺术展览、休闲娱乐于一体的综合体，成为包棉 1958 文化街区、文旅融合新地标。
- 以智促城，以城促智：福州市拟对 30 条道路的市政通信井盖进行智能化改造，可实时监测井盖倾角、水浸状态等，并将数据上传至城市智慧管理平台。
- 中山市：改造低效工业园区，完善配套设施，建设产业适配型现代园区，打造"深圳研发＋中山制造"产业协作新常态。
- 深圳市：利用旧厂房打造直播电商创新示范基地，成为"流量街区"，让旧空间"酿"出新价值。"""),
        ],
        "review_extra": [
            "图12（20 题调研报告）原生仅 524×630 像素、整页小字，卷面即如此；已保留截图并另附逐条文字版，"
            "请复核文字版转录是否与卷面一致。",
        ],
    },
    "2026北京海淀高三一模地理": {
        "pdf": "2026北京海淀高三一模地理（教师版）.pdf",
        "assets": [
            ("fig-1.png", ("pdf", 1, 0)),
            ("fig-2.png", ("docx", "image2.jpg")),      # 教师版 PDF 里没有图2（小岛与云朵景观），只有 docx 有
            ("fig-3.png", ("pdf", 2, 0)),
            ("fig-4.png", ("pdf", 2, 1)),
            ("fig-5.png", ("pdf", 3, 0)),
            ("fig-6.png", ("pdf", 4, 0)), ("fig-7.png", ("pdf", 4, 1)),
            ("fig-8.png", ("pdf", 5, 0)), ("fig-9.png", ("pdf", 5, 1)), ("fig-10.png", ("pdf", 5, 2)),
            ("fig-11.png", ("pdf", 6, 0)),
            ("fig-20.png", ("pdf", 7, 0)),              # 20 题料卡里的「我国部分"热区"范围示意图」
        ],
        "imgmap": {
            "image1.png": "fig-1.png", "image2.jpg": "fig-2.png", "image3.jpg": "fig-3.png",
            "image4.jpg": "fig-4.png", "image5.jpg": "fig-5.png", "image6.jpg": "fig-6.png",
            "image7.jpg": "fig-7.png", "image8.jpg": "fig-8.png", "image9.jpg": "fig-9.png",
            "image10.jpg": "fig-10.png", "image11.jpg": "fig-11.png", "image12.jpg": "fig-20.png",
        },
        "patches": [
            # 20 题的材料是「料卡表格」（含一张图），docx→md 把合并单元格拍成了 5 列半截表格 → 按料卡顺序还原成列表
            ("| 热带作物种类繁多，既有芒果、香蕉、火龙果等热带水果，也有在国防、交通、医疗、制造等领域广泛应用的天然橡胶，还有作为食用油、工业用油重要原料的椰子、油棕等。 | | 1961--2024年, 中国年平均气温呈显著上升趋势，平均每10年升高0.31℃,尤其以冬季增温最为显著。 | 2026年中央一号文件提出，推动天然橡胶等产业平稳发展；<br><br>2025年中央一号文件提出，推动天然橡胶等稳产提质。 |",
             """- 热带作物种类繁多，既有芒果、香蕉、火龙果等热带水果，也有在国防、交通、医疗、制造等领域广泛应用的天然橡胶，还有作为食用油、工业用油重要原料的椰子、油棕等。
- 1961—2024 年，中国年平均气温呈显著上升趋势，平均每 10 年升高 0.31℃，尤其以冬季增温最为显著。
- 2026 年中央一号文件提出，推动天然橡胶等产业平稳发展；2025 年中央一号文件提出，推动天然橡胶等稳产提质。"""),
            (r"regex:(?m)^\|\s*---\s*\|.*\n", ""),
            ("| 横断山区河谷热量充足。四川省攀枝花自上世纪末成功引种凯特芒，成为我国重要的芒果产地。如今，攀枝花三区两县的50多万农业人口里，约一半多的人口从事芒果种植。 | ![](images/image12.jpg)<br><br>广东省《关于现代农业产业集群培育行动方案(2025 年 -2027年)》将荔枝、香蕉、菠萝等热带作物纳入百亿元级产业集群进行培育。 | | |",
             """- 横断山区河谷热量充足。四川省攀枝花自上世纪末成功引种凯特芒，成为我国重要的芒果产地。如今，攀枝花三区两县的 50 多万农业人口里，约一半多的人口从事芒果种植。
- 广东省《关于现代农业产业集群培育行动方案（2025 年—2027 年）》将荔枝、香蕉、菠萝等热带作物纳入百亿元级产业集群进行培育。
- 我国部分"热区"范围示意图（图中标注的地区为我国热带作物学会提出的"热区"范围）

![](images/image12.jpg)"""),
            ("| 油棕是产油效率最高的油料作物。经多年科技攻关，我国科研人员选育出高产耐寒的油棕品种，并在北纬25°的云南保山成功建立种植示范基地，实现亩产油200公斤的高产水平。 | | | |",
             """
- 油棕是产油效率最高的油料作物。经多年科技攻关，我国科研人员选育出高产耐寒的油棕品种，并在北纬 25° 的云南保山成功建立种植示范基地，实现亩产油 200 公斤的高产水平。"""),
        ],
        "review_extra": [
            "图2（小岛与云朵景观照片）教师版 PDF 里没有，取自 docx 导出图（232×233，卷面即小图，照片本身无标注）。",
            "20 题材料原为「料卡表格」（单元格内含一张图），GFM 无法复原合并单元格 → 已按料卡顺序还原成列表 + 图，文字逐条照抄卷面。",
            "19 题(1)是绘图题，参考答案为「绘图略」+ 绘图要求，answer 保留原文（无答案图）。",
        ],
    },
    "2026北京丰台高三一模地理": {
        "pdf": "2026北京丰台高三一模地理（教师版）.pdf",
        "assets": [
            # 丰台 docx 导出图分辨率是 PDF 内嵌图的 2~5 倍（如 image5 4552×2072 vs PDF 914×414），故全用 docx 图
            ("fig-1.png", ("docx", "image1.png")),        # 1、2 题：中国空间站位置示意
            ("fig-3-1.png", ("docx", "image2.png")),      # 3 题：海平面气压（1月9日）
            ("fig-3-2.png", ("docx", "image3.png")),      # 3 题：海平面气压（1月10日）
            ("fig-4.png", ("docx", "image4.png")),        # 4、5 题：栖息地适应度
            ("fig-6.png", ("docx", "image5.png")),        # 6、7 题：梯田四季功能
            ("fig-8.png", ("docx", "image6.png")),        # 8 题：陕西省高铁规划
            ("fig-9.png", ("docx", "image7.png")),        # 9、10 题：鼠兔生活习性
            ("fig-11.png", ("docx", "image8.png")),       # 11、12 题：凤凰新城改造规划
            ("fig-13.png", ("docx", "image9.png")),       # 13、14 题：土耳其
            ("fig-15.png", ("docx", "image10.png")),      # 15 题：黑龙江主体功能区
            ("fig-16-1.png", ("docx", "image11.png")),    # 16 题 图1 清水河流域
            ("fig-16-2.png", ("docx", "image12.png")),    # 16 题 图2 生态清洁小流域建设模式
            ("fig-16-3.png", ("docx", "image13.png")),    # 16 题 图3 封山育林治理系统模块
            ("fig-16-ans.png", ("docx", "image20.png")),  # 16(3) 参考答案图：封山育林结构框图
            ("fig-17.png", ("docx", "image14.png")),      # 17 题：秘鲁地图
            ("fig-18.png", ("docx", "image15.png")),      # 18 题：意大利冬奥会举办地 + 气候统计
            ("fig-19-1.png", ("docx", "image16.png")),    # 19 题 图1：石英砂岩峰林形成演化
            ("fig-19-2.png", ("docx", "image17.png")),    # 19 题 图2：林下木耳种植流程
            ("fig-19-3.png", ("docx", "image18.png")),    # 19 题 图3：瑶寨谷仓
            ("fig-20.png", ("docx", "image19.png")),      # 20 题料卡里的分布示意图
        ],
        "imgmap": {
            "image1.png": "fig-1.png", "image2.png": "fig-3-1.png", "image3.png": "fig-3-2.png",
            "image4.png": "fig-4.png", "image5.png": "fig-6.png", "image6.png": "fig-8.png",
            "image7.png": "fig-9.png", "image8.png": "fig-11.png", "image9.png": "fig-13.png",
            "image10.png": "fig-15.png", "image11.png": "fig-16-1.png", "image12.png": "fig-16-2.png",
            "image13.png": "fig-16-3.png", "image14.png": "fig-17.png", "image15.png": "fig-18.png",
            "image16.png": "fig-19-1.png", "image17.png": "fig-19-2.png", "image18.png": "fig-19-3.png",
            "image19.png": "fig-20.png", "image20.png": "fig-16-ans.png",
        },
        "patches": [
            # 16 题的产汇流表：表头两级、首列合并单元格，docx→md 只留下半截 5 列 → 按卷面复原成 GFM
            (r"regex:\| 前期土壤 \| 累积降雨量 \| 降雨时长/h \| \| \|\n\| --- \| --- \| --- \| \| \| --- \| --- \|\n"
             r"\| 湿润程度 \| /mm \| ＜6 \| 6～＜12 \| 12～18 \|\n"
             r"\| 一般 \| ＜45 \| 无 \| 无 \| 无 \|\n"
             r"\| 45～<50 \| 有 \| 有 \| 有 \|\n"
             r"\| ≥50 \| 多 \| 多 \| 有 \|\n"
             r"\| 干旱 \| ＜55 \| 无 \| 无 \| 无 \|\n"
             r"\| 55～＜65 \| 有 \| 有 \| 有 \|\n"
             r"\| ≥65 \| 多 \| 有 \| 有 \|",
             """| 前期土壤湿润程度 | 累积降雨量/mm | 降雨时长/h<br>＜6 | 降雨时长/h<br>6～＜12 | 降雨时长/h<br>12～18 |
| --- | --- | --- | --- | --- |
| 一般 | ＜45 | 无 | 无 | 无 |
| 一般 | 45～＜50 | 有 | 有 | 有 |
| 一般 | ≥50 | 多 | 多 | 有 |
| 干旱 | ＜55 | 无 | 无 | 无 |
| 干旱 | 55～＜65 | 有 | 有 | 有 |
| 干旱 | ≥65 | 多 | 有 | 有 |"""),
            # 20 题的料卡表格（两列卡片 + 一张分布图）→ 按卷面顺序还原成列表 + 图
            (r"regex:(?m)^\| 黑龙江大庆市.*$",
             "- 黑龙江大庆市是我国最大的石油生产基地，重点发展石化产业，生产多种石化产品。"
             "其中高密度聚乙烯是设施农业管材的核心原料，可用于农田滴灌设施的建设。"),
            (r"regex:(?m)^\| --- \| \| --- \|$", ""),
            (r"regex:(?m)^\| !\[菁师帮（北京题库）\]\(images/image19\.png\) \| 山东是我国海洋大省.*$",
             "- 山东是我国海洋大省，海域面积近 16 万平方千米，海岸线 3505 千米，重点开发海底的金矿、"
             "煤矿、砂矿、油气等资源。渤海海域已形成海上平台钻井、海底管道输运组成的完整海上油气生产体系。\n\n"
             "- 我国部分地区战略性矿产资源分布示意图（图中标出的省区即材料所述地区）\n\n"
             "![](images/image19.png)"),
            (r"regex:(?m)^\| 贵州福泉市.*$",
             "- 贵州福泉市探明磷矿储量约 14.43 亿吨，是新能源电池材料重要承载区。2026 年春，"
             "贵州某化肥重点企业，满负荷运行磷肥生产线，为春耕生产筑牢“肥力”基础。"),
            (r"regex:(?m)^\| 四川冕宁县.*\| 广西河池市.*$",
             "- 四川冕宁县稀土储量居全球第二，且易采、易选、易冶炼。稀土因强磁性、耐高温、发光性能优异等属性，"
             "成为电动汽车、风力发电机、机器人等高端制造以及尖端国防技术的核心材料。\n\n"
             "- 广西河池市的锡、锑、铟等关键金属储量居全国前列，曾长期粗放开采，出现了上百家“小散乱”工矿企业。"
             "2025 年，河池市建设关键金属高质量发展综合试验区，从锡矿中提炼锡以及综合回收铟、锑等伴生金属，"
             "产生的废渣用于生产建材。到 2027 年，试验区内绿色矿山占比将达到 100%。"),
        ],
        # 丰台教师版第二部分的题号行和设问都没有分值 → 按北京卷常规权重分配（Σ=55）
        "essay_scores": {
            16: (14, [4, 5, 5]),
            17: (10, [4, 3, 3]),
            18: (12, [4, 4, 4]),
            19: (12, [4, 4, 4]),
            20: (7, [7]),
        },
        "review_extra": [
            "丰台教师版第二部分与各小问均未标分值（题号行、设问、答案区都没有「(N分)」），"
            "已按北京卷常规权重 14/10/12/12/7 分配到大题、再按设问难度分到小问（Σ=55），需人工核对。",
            "16 题产汇流表卷面只有「一般」「干旱」两组（docx 与教师版 PDF 完全一致，疑似原卷缺「湿润」组），"
            "已按原样转录成 GFM 表格（首列合并单元格按行补齐）。",
            "20 题材料原为「料卡表格」（单元格内含一张分布图），已按卷面顺序还原成列表 + 图，文字逐条照抄。",
            "分类：一次 AI 调用按「材料 + 全部设问」判定（设问是主要判据），结果见各 section 的 category；"
            "如某题口径不合意可手工改，脚本不覆盖已入手工值以外的东西。",
        ],
    },
}

# ──────────────────────────────────────────────────────────────────
# 通用兜底（PAPERS 里没有的卷）
#
# 34 卷（上期末/二模/真题）不可能逐卷手写 assets，故：
#   1. 图源 = **docx 导出图**（md 里的 images/*.jpg|png），imgmap 取恒等映射，
#      由 `rewrite_images()` 复制到扁平的 `/exam-images/<类型>/地理/<卷名>/images/`；
#   2. **不裁 PDF、不做 bbox**（老卷 PDF 内嵌图分辨率参差，统一以后续人工补截图）；
#   3. 解析后做**图质体检**（见 `figure_audit`）：被引用的图逐张记像素宽高，
#      「宽 < 500 且题面写了图/读图/如图」或「题面提到图但该题没有图」→ 写 `复核：` 行；
#      某卷一张图都没有却又提到「图1/读图/示意图」→ 同样写 `复核：`（可能是 docx 丢图）；
#   4. 题数 / 分值以卷面标注为准（`本部分共N题`/`每题N分`/`共N分`、题号行 `（N分）`），
#      卷面没标的按北京卷常规（选择 3 分/题、大题 14/10/12/12/7 之类）分配并留痕。
# ──────────────────────────────────────────────────────────────────
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
            ".tif", ".tiff", ".wmf", ".emf")   # tiff/wmf 由 rewrite_images→_ensure_png 转 png


def generic_cfg(src_md: Path, review: list[str] | None = None) -> dict:
    """没在 PAPERS 里登记的卷：图源一律 docx 导出图（恒等 imgmap），无补丁、无人工分值。"""
    img_dir = src_md.parent / "images"
    names: list[str] = []
    if img_dir.is_dir():
        names = sorted(p.name for p in img_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in IMG_EXTS)
    if review is not None:
        if names:
            review.append(f"通用兜底：图源取 docx 导出图 {len(names)} 张（未裁 PDF），"
                          f"如某图看不清请后续补 PDF 截图")
            odd = [n for n in names if Path(n).suffix.lower() in (".wmf", ".emf")]
            if odd:
                review.append(f"图质：该卷有 {len(odd)} 张图是 docx 内嵌 WMF（{', '.join(odd[:6])}"
                              f"{'…' if len(odd) > 6 else ''}），WMF→PNG 转换后文字层/内嵌位图可能错乱"
                              f"（出现黑块或文字重叠），已肉眼抽查，可疑的见下条，必要时补 PDF 截图")
        else:
            review.append("通用兜底：该卷 docx 目录下没有任何导出图（images/ 为空）")
    return {"generic": True, "assets": [], "imgmap": {n: n for n in names},
            "patches": [], "essay_scores": {}, "review_extra": [], "img_dir": img_dir}


def declared_choice_total(text: str) -> tuple[int | None, int | None, int | None]:
    """选择题区声明的（题数, 每题分值, 总分）——卷面写乱时可能只有部分。"""
    head = text[:1600]
    cnt = per = tot = None
    m = DECL_CNT.search(head)
    if m:
        cnt = int(m.group(1))
    m = DECL_PER.search(head)
    if m and int(m.group(1)) > 0:
        per = int(m.group(1))
    # 「本部分共15题，每题3分，共45分」/「一、本部分共8小题，每小题8分，共44分」
    # → 声明行里「共N分」取**最后一个**（前面可能还有「每小题N分」）
    for line in head.split("\n"):
        if "共" not in line or "分" not in line:
            continue
        if "本部分" in line or "选择题" in line or "满分" in line:
            nums = re.findall(r"共\s*(\d+)\s*分", line)
            if nums:
                tot = int(nums[-1])
                break
    return cnt, per, tot


def declared_essay_total(text: str) -> int | None:
    """非选择题区声明的总分（「本部分共5题，共55分」「满分56分」）。"""
    head = text[:600]
    m = DECL_SUM.search(head)
    if m:
        return int(next(g for g in m.groups() if g))
    m = re.search(r"满分\s*(\d+)\s*分", head)
    return int(m.group(1)) if m else None


# 北京卷常规大题权重（5 个大题、共 55 分）
ESSAY_WEIGHTS = [14, 10, 12, 12, 7]


def essay_totals(scripts: list[dict], cfg: dict, decl_total: int | None,
                 review: list[str]) -> dict[int, int]:
    """每个大题的总分：卷面题号行的（N分）> 人工配置 > 本部分声明总分按常规权重分配。"""
    man = cfg.get("essay_scores") or {}
    out: dict[int, int] = {}
    unknown = []
    for sc in scripts:
        no = sc["no"]
        if no in man:
            out[no] = man[no][0]
        elif sc["score"]:
            out[no] = sc["score"]
        else:
            unknown.append(no)
    if unknown and decl_total:
        rest = decl_total - sum(out.values())
        if len(unknown) == 5 and rest == sum(ESSAY_WEIGHTS):
            for no, w in zip(unknown, ESSAY_WEIGHTS):
                out[no] = w
            review.append(f"第 {'/'.join(map(str, unknown))} 题卷面没写大题分值，"
                          f"按北京卷常规权重 14/10/12/12/7 分配（Σ={rest}）")
        elif rest >= len(unknown):
            base, rem = divmod(rest, len(unknown))
            for j, no in enumerate(unknown):
                out[no] = base + (rem if j == len(unknown) - 1 else 0)
            review.append(f"第 {'/'.join(map(str, unknown))} 题卷面没写大题分值，"
                          f"按本部分声明总分 {decl_total} 分均分（各 {base} 分），需人工核对")
    return out


def choice_per_score(choice_text: str, qs: list[dict], review: list[str]) -> int:
    """选择题「每题分值」：优先卷面显式声明，其次按总分均分，最后用北京卷常规 3 分。"""
    n = len(qs)
    cnt, per, tot = declared_choice_total(choice_text)
    marked = [q["score"] for q in qs if q.get("score")]
    if cnt and n and cnt != n:
        review.append(f"卷面声明「本部分共{cnt}题」，实际切到 {n} 题（以实际切题为准）")
    if per and (tot is None or per * n == tot):
        return per
    if per and tot and per * n != tot:
        review.append(f"卷面「每题{per}分 × {n}题 = {per * n}分」与声明总分 {tot} 分不符，改用总分均分")
    if tot and n and tot % n == 0:
        return tot // n
    if marked and len(marked) * 2 >= n:
        vals = sorted(marked)
        return vals[len(vals) // 2]
    review.append(f"选择题分值无卷面依据，按北京卷常规 {CHOICE_SCORE} 分/题记录")
    return CHOICE_SCORE


# 个别题的四个选项在 docx 里被拍成**一张**拼图（A/B/C/D 四幅图并排）→ 已从原图裁成 4 张，在这里登记。
# key = 卷名，value = {题号: [A图, B图, C图, D图]}（文件名在 images/ 下）
MANUAL_OPTION_IMAGES = {
    "2024北京西城高三一模地理": {4: [f"opt-q4-{x}.png" for x in "abcd"]},
    "2025北京西城高三（上）期末地理": {4: [f"opt-q4-{x}.png" for x in "abcd"]},
}


# 逐卷留痕（补丁/人工处理过的卷在 review 里写一行，说明为什么这么做）
MANUAL_NOTES = {
    "2025北京海淀高三一模地理": [
        "第16(2)答案解析开头原有一张 EMF 图，转 PNG 后是空白（1×1），原卷该处无可读图形，已去掉引用，需人工核对答案图。",
    ],
    "2025北京西城高三一模地理": [
        "第20题：教师版 PDF 在 20 题表格后直接翻页，**卷面设问整句缺失**；暂以导入语「实施乡村振兴战略是…读图，回答下列问题。」作设问（未臆造），需人工补真实设问。",
    ],
    "2024北京西城高三一模地理": [
        "第4题四个选项在 docx 里是一张拼图（image3.png 下半部分），已按原图裁成 opt-q4-a~d.png 四张图片选项，已肉眼核对。",
    ],
    "2025北京西城高三（上）期末地理": [
        "第4题四个选项在 docx 里是一张拼图（image2.png），已按原图裁成 opt-q4-a~d.png 四张图片选项，已肉眼核对。",
    ],
    "2025北京延庆高三一模地理": [
        "docx 自动编号把多个题号/选项字母吃掉或重排（4、10、13 写成「1.」；A、C 选项写成「1.」「2.」；"
        "第一/第二部分标题写成「1. （选择题…/综合题…）」），已按原卷逐处纠正（见 review 里对应补丁未命中的提示）。",
    ],
}


# 逐卷文本补丁：docx→md 把某段（多为「设问」或整块版式）弄丢/弄成图片时，按卷补回原文。
# key = 卷名（去掉（教师版）），value = [(旧片段, 新片段), ...]（旧片段必须命中，否则记 review）
MANUAL_TEXT_PATCHES = {
    # 2025 西城一模 20 题：教师版 PDF 在 20 题表格后直接翻页，**卷面设问整句缺失**；
    # 这里把导入语挪到设问位置（不臆造设问），review 里注明需人工补。
    "2025北京西城高三一模地理": [
        ("实施乡村振兴战略是建设美丽中国的关键举措。图12示意吉林省不同区域乡村振兴的探索和实践。读图，回答下列问题。\n\n| 镇赉县",
         "| 镇赉县"),
        ("图12\n\n参考答案",
         "图12\n\n实施乡村振兴战略是建设美丽中国的关键举措。图12示意吉林省不同区域乡村振兴的探索和实践。读图，回答下列问题。\n\n参考答案"),
    ],
    # 2025 海淀一模 16(2)：答案解析开头挂着一张 EMF 图（转 PNG 后是空白 1×1，原卷也没有可读内容）→ 去掉
    "2025北京海淀高三一模地理": [
        ("![](images/image12.emf)", ""),
    ],
    # 2025 延庆一模：docx 自动编号把多个题号吃掉/重排（4、10、13 写成 1；A/C 选项写成 1、2；
    # 第一部分/第二部分标题被写成「1. （选择题…/综合题…）」），按原卷逐处纠正。
    "2025北京延庆高三一模地理": [
        ("1. （选择题 共45分）", "第一部分 （选择题 共45分）"),
        ("1. （综合题 共55分）", "第二部分 （综合题 共55分）"),
        ("\n1. 该区域\n", "\n4. 该区域\n"),
        ("\n1. 图5（a）中", "\n10. 图5（a）中"),
        ("\n1. 辛克雷水电站的建设", "\n13. 辛克雷水电站的建设"),
        ("1. 福州受低气压影响，气温升高明显 B.", "A. 福州受低气压影响，气温升高明显 B."),
        ("2. 昆明北部高压脊影响，天空云量少 D.", "C. 昆明北部高压脊影响，天空云量少 D."),
        ("1. 说明这些具体措施对推进区域水资源利用与保护的作用。", "（2）说明这些具体措施对推进区域水资源利用与保护的作用。"),
        ("1. （9分）阅读图文资料，回答下列问题。", "17.（9分）阅读图文资料，回答下列问题。"),
        ("1. 塔斯马尼亚岛西部降水多，东部降水少，分析原因。", "（1）塔斯马尼亚岛西部降水多，东部降水少，分析原因。"),
        ("\n1. 概述塔斯马尼亚岛混合农业的主要优点。", "\n（2）概述塔斯马尼亚岛混合农业的主要优点。"),
        ("\n2. 简述在该地建设大面积自然保护区的意义。", "\n（3）简述在该地建设大面积自然保护区的意义。"),
        ("\n1. 概括冷湖镇发展过程及其成因。", "\n（1）概括冷湖镇发展过程及其成因。"),
        ("\n2. 分析冷湖镇成为", "\n（2）分析冷湖镇成为"),
        ("\n3. 为打造世界级天文观测活动基地", "\n（3）为打造世界级天文观测活动基地"),
    ],
}


# 个别卷的选择题答案在教师版里只以**图片**给出（整张答案表是截图）→ 人工 read_image 转录后写在这里。
# key = 卷名（去掉（教师版）），value = {题号: 字母}
MANUAL_CHOICE_ANSWERS = {
    # 2023 东城高三二模：答案表是 image13.png（688×153），逐题转录
    "2023北京东城高三二模地理": {1: "C", 2: "C", 3: "A", 4: "C", 5: "D", 6: "C", 7: "B", 8: "A",
                                 9: "B", 10: "B", 11: "D", 12: "A", 13: "D", 14: "D", 15: "A"},
}


# 题号 → 卷名 之外的通用兜底（AI 不可用时按关键词猜 category）
CAT_RULES = [
    ("自然地理", ("河流", "水系", "流域", "气候", "气温", "降水", "地貌", "地质", "植被", "土壤", "生态脆弱",
                  "自然灾害", "天气", "气压")),
    ("人文地理", ("人口", "城镇", "城市", "乡村", "产业", "农业", "工业", "交通", "城市更新", "区位")),
    ("区域发展", ("区域", "转型", "协同", "辐射", "产业结构", "开发", "治理", "国家安全", "资源")),
]


# ──────────────────────────────────────────────────────────────────
# PDF 插图精确裁切（pdftocairo -svg 给出 bbox，300dpi 渲染页上切）
# ──────────────────────────────────────────────────────────────────

def pdf_page_boxes(pdf: Path, page: int) -> list[dict]:
    tmp = Path("/tmp/.geo_svg_%d.svg" % page)
    subprocess.run(["pdftocairo", "-svg", "-f", str(page), "-l", str(page), str(pdf), str(tmp)],
                   check=True, capture_output=True)
    txt = re.sub(r'xlink:href="data:[^"]*"', 'xlink:href="DATA"',
                 tmp.read_text(encoding="utf-8", errors="replace"))
    tmp.unlink(missing_ok=True)
    nat = {m.group(1): (float(m.group(2)), float(m.group(3)))
           for m in re.finditer(r'<image id="(source-\d+)" x="0" y="0" width="([\d.]+)" height="([\d.]+)"', txt)}
    out = []
    for m in re.finditer(r'<use xlink:href="#(source-\d+)" transform="matrix\(([^)]*)\)"', txt):
        vals = [float(v) for v in m.group(2).split(",")]
        a, b, c, d, e, f = vals
        w, h = nat.get(m.group(1), (0.0, 0.0))
        xs = [e + a * x + c * y for x in (0, w) for y in (0, h)]
        ys = [f + b * x + d * y for x in (0, w) for y in (0, h)]
        out.append({"nat": (int(w), int(h)), "box": (min(xs), min(ys), max(xs), max(ys))})
    return out


def pdf_cut(pdf: Path, page: int, idx: int, dst: Path, dpi=PDF_DPI, margin=3.0) -> tuple[int, int]:
    boxes = pdf_page_boxes(pdf, page)
    if idx >= len(boxes):
        raise RuntimeError(f"第 {page} 页只有 {len(boxes)} 张插图，取不到第 {idx} 张")
    x0, y0, x1, y1 = boxes[idx]["box"]
    scale = dpi / 72.0
    pg = Path("/tmp/.geo_pg_%02d.png" % page)
    if not pg.exists():
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-f", str(page), "-l", str(page),
                        str(pdf), "/tmp/.geo_pg"], check=True, capture_output=True)
        src = Path("/tmp/.geo_pg-%02d.png" % page)
        if not src.exists():
            src = Path("/tmp/.geo_pg-%d.png" % page)
        shutil.move(str(src), pg)
    geo = "%dx%d+%d+%d" % (int(round((x1 - x0 + 2 * margin) * scale)),
                           int(round((y1 - y0 + 2 * margin) * scale)),
                           int(round(max(0.0, (x0 - margin)) * scale)),
                           int(round(max(0.0, (y0 - margin)) * scale)))
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["convert", str(pg), "-crop", geo, "+repage", str(dst)], check=True, capture_output=True)
    return boxes[idx]["nat"]


def real_format(p: Path) -> str:
    """文件真实格式（不认扩展名）——扩展名叫 .png 其实是 jpeg 的情况要纠。"""
    try:
        return subprocess.run(["identify", "-format", "%m", str(p)],
                              capture_output=True, text=True, timeout=30).stdout.strip().upper()
    except Exception:
        return ""


def build_assets(title: str, cfg: dict, src_md: Path, refig: bool, review: list[str]) -> dict:
    """把该卷所有插图落到 **md 同目录的 images/**（跟 docx 导出图同层），

    随后由 `rewrite_images` 复制到 `/exam-images/...` 对应的扁平卷目录
    （`output_docx_to_md/<类型>/<学科>/<卷名>/images/`）——与生物/政治同一套落盘方式，
    绝不自己拼目录、绝不产生「（教师版）/卷名/」这种嵌套层。
    """
    md_dir = src_md.parent
    dst_dir = md_dir / "images"
    dst_dir.mkdir(parents=True, exist_ok=True)
    pdf = GAOZHONG / "一模" / SUBJECT / cfg["pdf"] if cfg.get("pdf") else None
    info = {}
    for name, src in cfg["assets"]:
        dst = dst_dir / name
        try:
            if src[0] == "pdf":
                if refig or not dst.exists():
                    nat = pdf_cut(pdf, src[1], src[2], dst)
                else:
                    nat = pdf_page_boxes(pdf, src[1])[src[2]]["nat"]
                review.append(f"图片 {name}：PDF 第 {src[1]} 页第 {src[2] + 1} 张插图裁图"
                              f"（原生 {nat[0]}×{nat[1]}，{PDF_DPI}dpi 渲染后裁切）")
            else:
                s = md_dir / "images" / src[1]
                if not s.exists():
                    raise RuntimeError(f"docx 图 {src[1]} 不存在")
                stale = (refig or not dst.exists() or dst.stat().st_mtime < s.stat().st_mtime
                         or (dst.suffix.lower() == ".png" and real_format(dst) not in ("PNG", "")))
                if stale:
                    if dst.suffix.lower() == s.suffix.lower():
                        shutil.copyfile(s, dst)
                    else:                                   # 目标名是 .png 而源是 .jpg → 真转成 PNG
                        subprocess.run(["convert", str(s), "-strip", str(dst)],
                                       check=True, capture_output=True)
                review.append(f"图片 {name}：沿用 docx 导出图 {src[1]}（该图 PDF 里没有或分辨率更低）")
        except Exception as e:                                   # 裁图失败不该中断整卷
            review.append(f"图片 {name} 生成失败：{str(e)[:80]}")
            continue
        wh = img_size(dst)
        if wh:
            info[name] = wh
    return info


def img_size(p: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run(["identify", "-format", "%w %h", str(p)],
                             capture_output=True, text=True, timeout=60).stdout.split()
        return int(out[0]), int(out[1])
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────
# 文本工具
# ──────────────────────────────────────────────────────────────────

def is_fragment(cfg: dict, tgt: str, review: list[str] | None = None) -> bool:
    """通用卷里「碎片图」（宽或高 < 40px 的符号/公式/空白小图）不算插图，直接丢。"""
    if not cfg.get("generic"):
        return False
    base = cfg.get("img_dir", Path("."))
    wh = img_size(base / tgt) or img_size(base / Path(tgt).with_suffix(".png"))
    if wh and (wh[0] < 40 or wh[1] < 40):
        if review is not None:
            review.append(f"图质：{tgt} 只有 {wh[0]}×{wh[1]}px，判定为符号/公式/空白碎片，已丢弃")
        return True
    return False


def sub_images(text: str, cfg: dict, title: str, type_dir: str, seen: dict, review: list[str]) -> str:
    """md 里的 docx 图 → /exam-images/... 目标图；None 的（重复/缩小副本）直接丢掉。"""
    if not text:
        return text

    def repl(m):
        ref = Path(m.group(1)).name
        tgt = cfg["imgmap"].get(ref)
        if tgt is None:
            if ref in cfg["imgmap"]:
                return ""                       # 明确标记为丢弃
            review.append(f"未登记的图片引用：{ref}")
            return ""
        if tgt in seen:
            return ""                           # 同一段里重复出现，只留第一张
        seen[tgt] = True
        if is_fragment(cfg, tgt, review):        # 通用卷：<20px 的是符号/公式碎片，不是插图
            return ""
        return f"![](images/{tgt})"

    return IMG_REF.sub(repl, text)


def normalize_md(t: str) -> str:
    """段落/列表/图片各自成块：列表项与图片前后补空行（表格行内绝不动）。"""
    out = []
    for ln in (t or "").split("\n"):
        s = ln.rstrip()
        prev = out[-1] if out else ""
        if s.strip().startswith("- ") and prev.strip() and not prev.strip().startswith(("- ", "|")):
            out.append("")
        elif s.strip().startswith("![") and prev.strip() and not prev.strip().startswith("!["):
            out.append("")
        elif prev.strip().startswith("![") and s.strip() and not s.strip().startswith("!["):
            out.append("")
        out.append(s)
    t = "\n".join(out)
    # 两张图挤在同一行（`![](a)![](b)`）→ 各自成段，前端才好排版
    t = re.sub(r"(\))(?=!\[)", r"\1\n\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def tidy(text: str) -> str:
    """段落清理：去硬换行空白、合并多余空行、去掉空的图片占位行。"""
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        s = ln.strip()
        if not s:
            if not blank and out:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(s)
    t = "\n".join(out)
    # 两张图挤在同一行（`![](a)![](b)`）→ 各自成段，前端才好排版
    t = re.sub(r"(\))(?=!\[)", r"\1\n\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def strip_score(s: str) -> tuple[str, int | None]:
    m = SCORE_RE.search(s or "")
    if not m:
        return (s or "").strip(), None
    return (s[:m.start()] + s[m.end():]).strip(), int(m.group(1))


def _opt_chain(seg: str) -> list[tuple[int, str]]:
    """找「A→B→C→D」按序排列的字母+圆点链（对 A.①②B.①④ 这种挤在一起的写法有效）。"""
    # 候选：字母前面不是英文字母，后面紧跟「圆点」或「中文/圈码/占位符」（覆盖 A.①② / A![](图) ①②）
    cand = [(m.start(1), m.group(1)) for m in
            re.finditer(r"(?<![A-Za-z])([A-D])(?=[\s\u3000]{0,2}(?:[．.、]|[\u4e00-\u9fff①②③④⑤⑥⑦⑧⑨\u27e6]))", seg)]
    if len(cand) < 4:
        cand = [(m.start(1), m.group(1)) for m in re.finditer(r"([A-D])\s*[．.、]", seg)]
    for i, (pos, k) in enumerate(cand):
        if k != "A":
            continue
        seq = [(pos, "A")]
        for p2, k2 in cand[i + 1:]:
            if k2 == chr(ord(seq[-1][1]) + 1):
                seq.append((p2, k2))
                if k2 == "D":
                    return seq
    return []


def opt_split(seg: str) -> tuple[str, list[dict]]:
    """把「题干 + A．… B．… C．… D．…」切成 (题干, 四个选项)。"""
    marks = [(m.start(1), m.group(1)) for m in
             re.finditer(r"(?:^|[\s\u3000])([A-D])\s*[．.、]\s*", seg)]
    # 兜底写法①：「A 设立保护区…」（字母后有空格但没有点）
    marks += [(m.start(1), m.group(1)) for m in
              re.finditer(r"(?m)^[ \t]*([A-D])[ \t]+(?=[^\s])", seg)]
    # 兜底写法②：「…补给为主B.主要粮食作物…」（选项 A 与 B 之间没有空格）
    marks += [(m.start(1), m.group(1)) for m in
              re.finditer(r"(?<=[\u4e00-\u9fff）)])([A-D])\s*[．.、]\s*", seg)]
    # 兜底写法③：「…郊区人类活动少 B城区有绿化浇灌…」（字母前有空格、后面既无点也紧跟汉字）
    marks += [(m.start(1), m.group(1)) for m in
              re.finditer(r"(?:^|[\s\u3000])([A-D])(?=[\u4e00-\u9fff])", seg)]
    seen: dict[str, int] = {}
    for pos, k in marks:
        seen.setdefault(k, pos)
    if len(seen) < 4:
        chain = _opt_chain(seg)                 # 兜底写法④：A.①②B.①④C.②③D.③④（字母紧跟圆点即可）
        if chain:
            order = chain
        else:
            return seg.strip(), []
    else:
        order = sorted(((pos, k) for k, pos in seen.items()))
    stem = seg[:order[0][0]].strip()
    labels: dict[str, str] = {}
    for i, (pos, k) in enumerate(order):
        end = order[i + 1][0] if i + 1 < len(order) else len(seg)
        lab = re.sub(r"^[\s\u3000]*[A-D]\s*[．.、]\s*", "", seg[pos:end]).strip()
        labels[k] = lab
    return stem, [{"id": k, "label": labels.get(k, "")} for k in "ABCD"]


# ──────────────────────────────────────────────────────────────────
# 选择题区
# ──────────────────────────────────────────────────────────────────

DOPT = re.compile(r"(?:^|[\s\u3000])D\s*[．.、]\s*")


def split_item(body: str) -> tuple[str, list[dict], str, str]:
    """把一题的原文切成（题干+选项、四个选项、粘在选项后面属于下一组材料的尾巴）。

    尾巴的判据：D 选项所在段落之后的段落（卷面里材料总是从新段落起）。
    """
    paras = body.split("\n\n")
    k = None
    for i, p in enumerate(paras):
        if DOPT.search(IMG_REF.sub("", p)):
            k = i
            break
    if k is None:
        stem, opts = _opt_split_keep_imgs(body)
        return stem, opts, "", body
    head = "\n\n".join(paras[:k + 1])
    trail = "\n\n".join(paras[k + 1:])
    stem, opts = _opt_split_keep_imgs(head)
    return stem, opts, trail, head


def _opt_split_keep_imgs(seg: str) -> tuple[str, list[dict]]:
    """选项文字里可能夹着图片（`A.![](images/image3.png)`）：先把图换成占位符再切，切完还原。"""
    imgs: list[str] = []

    def mask(m):
        imgs.append(m.group(0))
        return f"\u27e6IMG{len(imgs)}\u27e7"

    def unmask(t: str) -> str:
        return re.sub(r"\u27e6IMG(\d+)\u27e7", lambda m: imgs[int(m.group(1)) - 1], t)

    stem, opts = opt_split(IMG_REF.sub(mask, seg))
    return re.sub(r"\u27e6IMG\d+\u27e7", "", stem).strip(), \
        [{"id": o["id"], "label": unmask(o["label"])} for o in opts]


def fix_num_then_image(text: str) -> str:
    """题号后直接跟图片 / 或用了全角冒号当分隔（「6：若图示…」）→ 统一补成点。"""
    text = re.sub(r"(?m)^([ \t]*\**\s*)(\d{1,2})(?=!\[)", r"\1\2.", text)
    # 「6：若图示…」这种题号 + 全角冒号；后面跟数字的（10：00 这种时间）不动
    return re.sub(r"(?m)^([ \t]*\**\s*)(\d{1,2})[ \t]*\**[ \t]*[：:][ \t]*(?=[^\d\s])", r"\1\2. ", text)


def fix_swallowed_option_a(text: str) -> str:
    """docx 自动编号把选项「A．」吃成「1.」时还原成 A。

    判据：该编号行**出现在题号序列之后且编号倒退/重复**（如第 3 题后面出现「1. 沙尘暴 B. 寒潮…」），
    并且它后面几行里紧跟着「B．」。正常的题号行（1. 2. 3. 递增）一律不动。
    """
    lines = text.split("\n")
    last_no = 0
    out = []
    for i, ln in enumerate(lines):
        if HEAD_LINE.match(ln.strip().strip("*").strip()):
            last_no = 0                       # 新的分节 → 题号重新从 1 开始
            out.append(ln)
            continue
        m = re.match(r"^[ \t]*\**\s*(\d{1,2})[ \t]*[\\]?[.．、][ \t]*", ln)
        if not m:
            out.append(ln)
            continue
        n = int(m.group(1))
        nxt = None
        for ln2 in lines[i + 1:i + 12]:
            m2 = re.match(r"^[ \t]*\**\s*(\d{1,2})[ \t]*[\\]?[.．、]", ln2)
            if m2:
                nxt = int(m2.group(1))
                break
        if nxt is not None and nxt - n >= 10:      # 下一题是 12 而这里是 1 → 原题号是 11，不是选项 A
            out.append(ln)
            continue
        if n <= 4 and n <= last_no:               # 只有 A–D 位置（1–4）且编号倒退才可能是被吃掉的选项 A
            ahead = ln[m.end():] + "\n" + "\n".join(lines[i + 1:i + 4])
            if re.search(r"(?:^|[\s\u3000])B[ \t]*[.．、]", ahead):
                out.append("A. " + ln[m.end():])
                continue
        last_no = max(last_no, n)
        out.append(ln)
    return "\n".join(out)


def parse_choice_region(text: str, cfg: dict, src_md: Path, title: str, type_dir: str,
                        review: list[str]) -> list[dict]:
    """返回 [{no, content, options}] —— content 已含共享材料与该组的图（每题自洽）。"""
    text = fix_num_then_image(text)
    text = fix_swallowed_option_a(text)
    marks = [m for m in QMARK.finditer(text)]
    items = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        items.append({
            "no": int(m.group(2)),
            "score": int(m.group(3)) if m.group(3) else None,
            "lead": (m.group(1) or "").strip(),
            "body": text[m.end():(nxt.start() if nxt else len(text))],
        })
    pre = text[:marks[0].start()] if marks else text
    pre = "\n".join(ln for ln in pre.split("\n")
                    if not HEAD_LINE.match(ln.strip().strip("*").strip()) and ln.strip())

    for it in items:
        it["stem"], it["options"], it["trail"], it["head"] = split_item(it["body"])

    # 开头那段「没有 A–D 选项的题号」＝卷首须知/目录（如「1.答题前，考生务必…」）→ 并进材料
    lead_drop = 0
    while lead_drop < len(items) and not items[lead_drop]["options"]:
        lead_drop += 1
    if 0 < lead_drop < len(items):
        dropped = [it["no"] for it in items[:lead_drop]]
        head_txt = "\n\n".join(f"{it['no']}. {it['body'].strip()}" for it in items[:lead_drop])
        pre = (head_txt + "\n\n" + pre).strip()
        review.append(f"选择题开头 {lead_drop} 段没有 A–D 选项的编号（{dropped}）判定为卷首须知/目录，已并入材料")
        items = items[lead_drop:]

    for it in items:
        if not it["options"]:                       # 选项与题干混排兜底
            review.append(f"选择题第{it['no']}题：没切出 A–D 四个选项，原文保留在 content 里")
            it["stem"] = it["body"]
        opt_imgs = {r for o in it["options"] for r in IMG_REF.findall(o["label"] or "")}
        it["own_refs"] = [r for r in IMG_REF.findall(it["lead"] + "\n" + it["head"]) if r not in opt_imgs]

    # 选项是拼图的题（人工裁好的 A–D 四张图）→ 直接补成 4 个图片选项
    man_opts = MANUAL_OPTION_IMAGES.get(title) or {}
    for it in items:
        names = man_opts.get(it["no"])
        if names and not it["options"]:
            it["options"] = [{"id": k, "label": f"![](images/{n})"} for k, n in zip("ABCD", names)]
            review.append(f"选择题第{it['no']}题：选项在 docx 里是拼图，已按原图裁成 A–D 四张图片选项")

    # 仍然没有选项的编号条目（材料里的「1. …」清单、分节标题）→ 并进下一题的材料，不当题目
    if items and any(not it["options"] for it in items):
        kept, pending = [], ""
        for it in items:
            if it["options"]:
                if pending:
                    it["lead"] = (pending + "\n" + it["lead"]).strip()
                    pending = ""
                kept.append(it)
            else:
                pending += "\n\n" + f"{it['no']}. {it['body'].strip()}"
                review.append(f"选择题第{it['no']}题位置没有 A–D 选项，判定为材料/标题，已并入下一题材料")
        if pending:
            review.append("选择题区末尾有不成题的编号文本，已丢弃")
        items = kept

    # 题号自愈：卷面/docx 把某个题号写错（如把「3.」写成「1.」）时，按「上一题号+1」顺延
    last_no = 0
    for it in items:
        if it["no"] <= last_no:
            old_no = it["no"]
            it["no"] = last_no + 1
            review.append(f"选择题题号 {old_no} 与前一题重复/倒退，已顺延纠正为 {it['no']}（原卷第 {it['no']} 题）")
        last_no = it["no"]

    # 分组：材料块 = 上一题选项之后、本题题号之前的文字（第一组用卷首那段）
    groups, cur = [], None
    for i, it in enumerate(items):
        mat = pre if i == 0 else items[i - 1]["trail"]
        it["material"] = mat
        starts_new = (i == 0) or bool(mat.strip()) or bool(it["lead"]) \
            or bool(it["own_refs"]) or bool(SELF_MAT.match(it["stem"]))
        if starts_new:
            cur = {"material": mat, "items": [it]}
            groups.append(cur)
        else:
            cur["items"].append(it)

    out = []
    for g in groups:
        seen: dict[str, bool] = {}
        mat_txt = sub_images(tidy(g["material"]), cfg, title, type_dir, seen, review)
        figs = re.findall(r"!\[\]\((images/[^)]+)\)", mat_txt)
        for it in g["items"]:
            for ref in it["own_refs"]:
                tgt = cfg["imgmap"].get(Path(ref).name)
                if tgt and tgt not in seen and not is_fragment(cfg, tgt, review):
                    seen[tgt] = True
                    figs.append(f"images/{tgt}")
            it["stem"] = sub_images(tidy(it["stem"]), cfg, title, type_dir, seen, review)
            for o in it["options"]:
                o["label"] = sub_images(tidy(o["label"]), cfg, title, type_dir, seen, review)
                o["label"] = rewrite_images(o["label"], src_md.parent, title, type_dir, review, SUBJECT)
        figs = list(dict.fromkeys(figs))
        body_parts = [re.sub(r"!\[\]\([^)]+\)", "", mat_txt).strip()] if mat_txt else []
        for f in figs:
            body_parts.append(f"![]({f})")
        shared = "\n\n".join(p for p in body_parts if p.strip())
        for it in g["items"]:
            stem = re.sub(r"!\[\]\([^)]+\)", "", it["stem"]).strip()
            stem = EMPTY_PAREN.sub("", stem).strip()
            content = (shared + "\n\n" + stem).strip() if shared else stem
            content = normalize_md(rewrite_images(content, src_md.parent, title, type_dir, review, SUBJECT))
            out.append({"no": it["no"], "content": content, "options": it["options"],
                        "score": it.get("score")})
    return out


# ──────────────────────────────────────────────────────────────────
# 非选择题区
# ──────────────────────────────────────────────────────────────────

Q_VERB = re.compile(r"说明|简述|简答|分析|列举|概括|评价|描述|绘制|画出|画出|阐述|阐明|比较|判断|"
                     r"推测|归纳|提出|解释|指出|说出|写出|设计|论证|谈谈|选择|补充|完成|查找|计算|绘出|作答")
REQ_START = re.compile(r"^(?:绘图要求|答题要求|要求|注\s*[:：]|[①②③④⑤⑥⑦⑧⑨])")
MAT_LEAD = re.compile(r"(?:^|[。；，])\s*(?:图|表)\s*\d+\s*[为是]|下图为|上图为|如图|图为|下表为")
HARD_MAT = re.compile(r"^(?:任务[一二三四五六七八九十\d]|材料[一二三四五六七八九十\d]?\s*[:：]|【)")


def split_prompt_paras(paras: list[str]) -> int:
    """在一个小问块里判断「设问」到第几段结束（后面的是下一问的材料）。

    规则：设问至少要吃掉第一段；随后继续吃「含设问动词」或「绘图要求/①②③」的段落，
    以及很短的过渡段；遇到「任务二…」「材料…」「【…】」这类材料/标题立刻停。
    """
    if not paras:
        return 0
    k, i = 1, 1
    while i < len(paras):
        p = paras[i].strip()
        if HARD_MAT.match(p):
            break
        nxt = paras[i + 1].strip() if i + 1 < len(paras) else ""
        if MAT_LEAD.search(p):            # 这一句是材料引子（图N为…），设问到上一段为止
            break
        if Q_VERB.search(p) or REQ_START.match(p):
            k = i + 1
        elif nxt and not HARD_MAT.match(nxt) and (Q_VERB.search(nxt) or REQ_START.match(nxt)):
            k = i + 1                     # 描述性引导段 + 下一段才是设问 → 一起算设问
        else:
            break
        i += 1
    return k


def parse_essay_region(text: str, ans_text: str, cfg: dict, src_md: Path, title: str, type_dir: str,
                       review: list[str], target_total: int | None = None) -> list[dict]:
    for pat, rep in cfg.get("patches", []):
        if pat.startswith("regex:"):
            new = re.sub(pat[len("regex:"):], rep, text)
            if new == text:
                review.append(f"正则补丁未命中（需人工核对）：{pat[len('regex:'):][:40]}…")
            text = new
        else:
            if pat not in text:
                review.append(f"文本补丁未命中（需人工核对）：{pat[:40]}…")
            text = text.replace(pat, rep)
    text = fix_num_then_image(text)
    marks = [m for m in QMARK.finditer(text)]
    scripts = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        scripts.append({
            "no": int(m.group(2)),
            "score": int(m.group(3)) if m.group(3) else None,
            "lead": (m.group(1) or "").strip(),
            "body": text[m.end():(nxt.start() if nxt else len(text))],
        })
    # 末尾材料里常有「1. 2. 3.」这种编号条目（资料卡/任务清单），不是大题 → 只保留 >= 首题号的递增段
    if scripts:
        first = scripts[0]["no"]
        keep, dropped = [], []
        for sc in scripts:
            if sc["no"] >= first and (not keep or sc["no"] > keep[-1]["no"]):
                keep.append(sc)
            else:
                dropped.append(sc["no"])
        if dropped:
            review.append(f"非选择题区里 {dropped} 这些编号不成递增序列（材料里的条目编号），已忽略")
            scripts = keep
    # 大题总分：卷面题号行的（N分）→ 人工配置 → 本部分声明总分按常规权重分配
    essay_decl = declared_essay_total(text)
    totals = essay_totals(scripts, cfg, essay_decl or target_total, review)
    out = []
    for sc in scripts:
        no = sc["no"]
        seen: dict[str, bool] = {}
        body = (sc["lead"] + "\n" + sc["body"]) if sc["lead"] else sc["body"]
        body = sub_images(tidy(body), cfg, title, type_dir, seen, review)
        subs = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ.finditer(body)]
        total, subs_scores = totals.get(no), None
        man = (cfg.get("essay_scores") or {}).get(no)
        if man:
            total, subs_scores = man
            if sc["score"] and sc["score"] != total:
                review.append(f"第{no}题：卷面分值 {sc['score']} 与人工配置 {total} 不一致，取人工配置")

        # 逐个小问：设问段 vs 后面属于下一问的材料段
        questions = []
        if not subs:                                    # 整题一个设问（20 题那种）
            paras = [p for p in body.split("\n\n") if p.strip()]
            j = len(paras) - 1                          # 设问 = 最后一个**有汉字**的段落
            while j > 0 and not CJK.search(re.sub(r"!\[[^\]]*\]\([^)]+\)", "", paras[j])):
                j -= 1
            extra = [f"![]({r})" for r in IMG_REF.findall(paras[j])]   # 设问段里夹带的图挪到材料里
            questions.append({"k": None, "prompt": paras[j].strip(), "tail": "",
                              "head": "\n\n".join(paras[:j] + extra + paras[j + 1:]).strip()})
        else:
            head = body[:subs[0][0]].strip()
            for i, (s, e, k) in enumerate(subs):
                end = subs[i + 1][0] if i + 1 < len(subs) else len(body)
                chunk = [p for p in body[s:end].split("\n\n") if p.strip()]
                cut = split_prompt_paras(chunk)
                questions.append({"k": k, "prompt": "\n\n".join(chunk[:cut]).strip(),
                                  "tail": "\n\n".join(chunk[cut:]).strip(),
                                  "head": head if i == 0 else ""})
        # 分值
        for i, q in enumerate(questions):
            prompt, ps = strip_score(q["prompt"])
            q["prompt"] = prompt.strip()
            if subs_scores and i < len(subs_scores):
                q["score"] = subs_scores[i]
            elif ps:
                q["score"] = ps
            else:
                q["score"] = None                    # 留给下面按大题总分分配
        missing = [q for q in questions if not q.get("score")]
        if missing:
            known = sum(q["score"] for q in questions if q.get("score"))
            rest = (total - known) if total else 0
            if total and rest >= len(missing):
                base, rem = divmod(rest, len(missing))
                for j, q in enumerate(missing):
                    q["score"] = base + (rem if j == len(missing) - 1 else 0)
                review.append(f"第{no}题：{len(missing)} 个小问卷面没标分值，"
                              f"按大题总分 {total} 分与已标注小问配平（各 "
                              f"{base} 分{'，最后一问 ' + str(base + rem) + ' 分' if rem else ''}），需人工核对")
            else:
                for q in missing:
                    q["score"] = total if (total and len(questions) == 1) else 1
                review.append(f"第{no}题：{len(missing)} 个小问卷面与答案都没分值、大题总分也没有，记为 1 分（需人工核对）")
        ssum = sum(q["score"] for q in questions)
        if total is None:
            total = ssum
            review.append(f"第{no}题：卷面没写大题分值，取小问分值合计 {total}")
        if ssum != total:
            delta = total - ssum
            if questions[-1]["score"] + delta > 0:
                questions[-1]["score"] += delta
                review.append(f"第{no}题：小问分值合计 {ssum} ≠ 大题 {total}，差额 {delta:+d} 并入最后一问")
            else:
                total = ssum
                review.append(f"第{no}题：小问分值合计 {ssum} 与分配的大题分值 {total - delta} 差 {delta:+d}，"
                              f"按卷面标注的分值合计 {ssum} 为准（需人工核对）")
        # section.content：材料 → 设问 → 答题框 → 下一问材料 → 设问 → 答题框 …
        # （单栏版式不变；只是占位符换成 <AnswerBox></AnswerBox>）
        blocks = []
        for q in questions:
            if q["head"]:
                blocks.append(q["head"])
            blocks.append(f"{q['prompt']}\n\n{placeholder_for(q['score'])}")
            if q["tail"]:
                blocks.append(q["tail"])
        content = "\n\n".join(b for b in blocks if b.strip())
        content = normalize_md(rewrite_images(content, src_md.parent, title, type_dir, review, SUBJECT))
        # 小问设问原文会给 AI 批改当 {stem}，里面不能带图片引用（图留在 section.content 里）
        prompts = [(q["k"], IMG_REF.sub("", q["prompt"]).strip() or q["prompt"], q["score"])
                   for q in questions]
        out.append({"no": no, "score": total, "content": content, "prompts": prompts})

    # 全卷兜底：非选择题总分应为「100 − 选择题分值」；卷面没标时差额并入最后一题的最后一问
    if target_total and out:
        delta = target_total - sum(sc["score"] for sc in out)
        if delta:
            last = out[-1]
            k, prompt, sc_last = last["prompts"][-1]
            if sc_last + delta > 0:
                last["prompts"][-1] = (k, prompt, sc_last + delta)
                last["score"] += delta
                review.append(f"非选择题合计 {sum(sc['score'] for sc in out) - delta} ≠ 目标 {target_total}"
                              f"（100−选择），差额 {delta:+d} 并入第{last['no']}题最后一问，需人工核对")
            else:
                # 最后一题塞不下（会让某问 <1 分）→ 从分值最大的小问里扣/补，仍然绝不留 0 分
                before = sum(sc["score"] for sc in out)
                for sec in sorted(out, key=lambda x: -x["score"]):
                    for i in range(len(sec["prompts"]) - 1, -1, -1):
                        k, prompt, sc_i = sec["prompts"][i]
                        take = min(abs(delta), max(0, sc_i - 1)) if delta < 0 else abs(delta)
                        if take <= 0:
                            continue
                        sec["prompts"][i] = (k, prompt, sc_i + (-take if delta < 0 else take))
                        sec["score"] += (-take if delta < 0 else take)
                        delta += take if delta < 0 else -take
                        if delta == 0:
                            break
                    if delta == 0:
                        break
                if delta == 0:
                    review.append(f"非选择题合计 {before} ≠ 目标 {target_total}（100−选择），已按比例从各题小问中配平，需人工核对")
                else:
                    review.append(f"非选择题合计 {before} 与目标 {target_total} 仍差 {delta:+d}，需人工核对")
    # 答案 + 详解（analysis）
    all_nos = [sc["no"] for sc in out]
    for sc in out:
        want = [k for k, _p, _s in sc["prompts"]]
        box, det = essay_answer(ans_text, sc["no"], review, want=want, all_nos=all_nos)
        for k, prompt, score in sc["prompts"]:
            a = (box or {}).get(k, "")
            if not (a or "").strip():
                review.append(f"第{sc['no']}题{'第' + str(k) + '问' if k else ''}："
                              f"原卷参考答案区里没有该问答案，answer 置占位说明，需人工补")
                a = "（原卷未提供该小问的参考答案）"
            a = sub_images(tidy(a), cfg, title, type_dir, {}, review) if "![" in (a or "") else a
            if "![" in (a or ""):
                a = rewrite_images(a, src_md.parent, title, type_dir, review, SUBJECT)
            sc.setdefault("answers", []).append(a)
            an = (det or {}).get(k, "")
            if not an and k is not None:
                an = (det or {}).get(None, "")
            an = sub_images(an, cfg, title, type_dir, {}, review) if "![" in (an or "") else an
            if "![" in (an or ""):
                an = rewrite_images(an, src_md.parent, title, type_dir, review, SUBJECT)
            sc.setdefault("analyses", []).append(an)
    return out


DETAIL = re.compile(r"(?m)^[ \t]*【(?:(\d{1,2})题|小问(\d{1,2}))?详解】[ \t]*")
ANY_MARK = re.compile(r"(?m)^[ \t]*【[^】]{0,8}】[ \t]*")
# 菁优网教师版把 【分析】/【解答】/【点评】 写在行中间（「12．【分析】（1）…」）
MARK_ANY = re.compile(r"【(?:分析|解答|点评|答案|详解|解析)】")
PRE_ANS = re.compile(r"(\d{1,2})\s*[\\]?\s*[.．、]\s*【答案】")
# 菁优网答案：「…故选：C。」（每题解析固定以「故选」收尾）
SELECTED = re.compile(r"故(?:选|选择)\s*[:：]?\s*([A-D])(?![A-Za-z])")


def squeeze(text: str) -> str:
    """详解正文：段落之间保留一个空行，去掉多余空白。"""
    paras = [re.sub(r"[ \t\u3000]+", " ", p.strip()) for p in text.split("\n\n")]
    return "\n\n".join(p for p in paras if p).strip()


def parse_choice_details(ans_text: str, max_no: int = N_CHOICE) -> dict[int, str]:
    """选择题的 【N题详解】 / 单题后面的 【详解】 → {题号: 解析正文}。

    丰台这种「详解卷」里，多题连排的用 `【1题详解】`，单题的写成
    `3. 【答案】D` 后面跟一个不带题号的 `【详解】`，题号要往前回溯。
    """
    out: dict[int, str] = {}
    marks = list(DETAIL.finditer(ans_text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(ans_text)
        seg = ans_text[m.end():end]
        seg = ANY_MARK.split(seg)[0]                      # 到下一个 【…】 标记为止
        # 无编号的 【详解】 若落在选择题区，后面可能直接接「**第二部分 非选择题**」和 16 题答案
        seg = re.split(r"(?m)^[ \t]*(?:\**\s*第[二2]部分|\d{1,2}\s*[\\]?\s*[.．、]\s*【答案】)", seg)[0]
        num = m.group(1)
        if num is None:
            prev = list(PRE_ANS.finditer(ans_text[:m.start()]))
            num = prev[-1].group(1) if prev else None
        if num and seg.strip() and int(num) <= max_no:
            out.setdefault(int(num), squeeze(seg))
    if not out and "【分析】" in ans_text:
        # 菁优网教师版（老卷常见）：每题一段「N．【分析】…【解答】…故选：X。【点评】…」
        qs = [m for m in QMARK.finditer(ans_text)]
        for i, m in enumerate(qs):
            no = int(m.group(2))
            if no > max_no:
                continue
            end = qs[i + 1].start() if i + 1 < len(qs) else len(ans_text)
            seg = ans_text[m.end():end]
            j = seg.find("【分析】")
            if j < 0:
                j = seg.find("【解答】")
            if j < 0:
                continue
            body = MARK_ANY.sub("", seg[j:])
            if body.strip():
                out.setdefault(no, squeeze(body))
    return out


def essay_answer(ans_text: str, no: int, review: list[str],
                 want=None, all_nos=None) -> tuple[dict, dict]:
    """在参考答案区里切出第 no 大题 → (各小问答案, 各小问详解)。

    答案按 (1)(2)(3) 切；丰台这种详解卷把首问写在「16. 【答案】（1）…」同一行，
    这里先按行内 progress 切，再按 【小问N详解】/【详解】 单独收解析正文。
    """
    marks = [m for m in QMARK.finditer(ans_text) if int(m.group(2)) == no]
    if not marks:
        # 老卷常见：最后一个大题的答案开头被 docx 自动编号吃成「1.」/「一、」，
        # 找不到题号时把它当「该大题答案 = 答案区末尾剩余部分」（只有当它是最后一题时才这么干）
        others = [m.start() for m in QMARK.finditer(ans_text)]
        tail = None
        if all_nos and no == max(all_nos) and others:
            m3 = re.search(r"(?m)^[ \t]*\d{1,2}[ \t]*[.．][ \t]*$", ans_text[others[-1]:])
            if m3:
                tail = ans_text[others[-1] + m3.start():]
        if tail:
            raw = tail
            review.append(f"第{no}题：答案区里没有「{no}．」题号（疑似 docx 自动编号丢失），"
                          f"已从「1.」那一行起取答案区末尾作该题答案，需人工核对")
        else:
            review.append(f"第{no}题：参考答案区里没找到该大题")
            return {}, {}
    else:
        start = marks[0].end()
        later = [m.start() for m in QMARK.finditer(ans_text) if int(m.group(2)) > no]
        raw = ans_text[start:(later[0] if later else len(ans_text))]
    # 大题答案区被「下一题」截断：答案区里后面的大题题号被 docx 自动编号吃成「1.」时，
    # 本题的 raw 会一路吞到卷末（把下一大题的答案也吃进来）。看到**只有编号的一行**就截断。
    if all_nos and no < max(all_nos):
        m2 = re.search(r"(?m)^[ \t]*\d{1,2}[ \t]*[.．][ \t]*$", raw)
        if m2 and m2.start() > 0:
            raw = raw[:m2.start()]
            review.append(f"第{no}题：答案区在「{m2.group(0).strip()}」处截断"
                          f"（后续大题题号被 docx 自动编号吃掉），需人工核对")
    dmarks = list(DETAIL.finditer(raw))
    details: dict = {}
    for i, m in enumerate(dmarks):
        end = dmarks[i + 1].start() if i + 1 < len(dmarks) else len(raw)
        seg = ANY_MARK.split(raw[m.end():end])[0]
        key = int(m.group(2)) if m.group(2) else None
        if not seg.strip():
            continue
        details[key] = (details.get(key, "") + "\n\n" + squeeze(seg)).strip()
    block = raw[:dmarks[0].start()] if dmarks else raw
    # 菁优网教师版（老卷/真题常见）每题三段：【分析】…【解答】…【点评】…。
    # 答案只取【解答】（没有【解答】时退回【分析】），【分析】/【点评】归 analysis，
    # 否则「本题难度较低…」这类点评会混进答案尾巴里。
    ans_block, ana_block = _split_marks(block)
    ans_block = re.sub(r"^[\s*]+", "", ans_block)    # 「**20.（7分）**」收尾的星号
    # docx 自动编号残留：答案开头单独一行的「1.」（原本是「20.」，导出时被重排成 1.）
    ans_block = re.sub(r"^(?:\d{1,2}\s*[.．]\s*\n+)+", "", ans_block).strip()
    ans_block = re.sub(r"^解[：:]\s*", "", ans_block)   # 菁优网「【解答】解：…」
    subs = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ_ANS.finditer(ans_block)]
    head = ans_block[:subs[0][0]].strip() if subs else ""
    out = {}
    if subs:
        for i, (s, e, k) in enumerate(subs):
            end = subs[i + 1][0] if i + 1 < len(subs) else len(ans_block)
            seg = ans_block[e:end]
            seg = re.sub(r"^[ \t]*[（(]\s*\d+\s*分\s*[）)]", "", seg.strip()).strip()
            seg = re.sub(r"^解[：:]\s*", "", seg)
            out[k] = seg
        out = _fill_missing_answers(out, head, want, no, review)
    else:
        out = {None: ans_block.strip()}
    # 分析：按同样的（N）切；切不出同号小问就不塞（避免把整段点评重复贴到每一问）
    if ana_block.strip():
        asubs = [(m.start(), m.end(), int(m.group(1))) for m in SUBQ_ANS.finditer(ana_block)]
        if asubs and want:
            for i, (s, e, k) in enumerate(asubs):
                if k in want:
                    end = asubs[i + 1][0] if i + 1 < len(asubs) else len(ana_block)
                    details.setdefault(k, squeeze(ana_block[e:end]))
    return out, details


MARK_ITER = re.compile(r"【(分析|解答|点评|答案|详解|解析)】")


def _split_marks(block: str) -> tuple[str, str]:
    """把大题答案区按【分析】/【解答】/【点评】/【答案】拆成（答案正文, 解析正文）。"""
    pos = [(m.start(), m.group(0), m.group(1)) for m in MARK_ITER.finditer(block)]
    if not pos:
        return block, ""
    parts: dict[str, list[str]] = {}
    head = block[:pos[0][0]]
    for i, (p, tag, name) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(block)
        parts.setdefault(name, []).append(block[p + len(tag):end])
    ans = "\n\n".join(parts.get("解答") or []) or "\n\n".join(parts.get("答案") or [])
    if not ans.strip():
        ans = "\n\n".join(parts.get("分析") or [])
    ana = "\n\n".join((parts.get("分析") or []) + (parts.get("点评") or []))
    if head.strip() and not head.strip().startswith(("【", "!")):
        ans = head.strip() + "\n\n" + ans
    return ans, ana


def _fill_missing_answers(out: dict, head: str, want, no: int,
                          review: list[str]) -> dict:
    """答案区少切了小问时尽量找补：小问首答被写在题号行（head）、或最后一问的答案是一张图。"""
    if not want:
        return out
    missing = [k for k in want if k is not None and not (out.get(k) or "").strip()]
    if not missing:
        return out
    if head and head.strip():
        k = missing.pop(0)
        out[k] = head.strip()
        review.append(f"第{no}题第{k}问：答案区把该问答案写在题号同一行（无「（{k}）」编号），已回收，需人工核对")
    # 答案是一张图（绘图题）：最后一问的答案里多出来的图挪给缺答案的那问
    keys = [k for k in want if k is not None]
    for k in keys:
        seg = out.get(k) or ""
        imgs = re.findall(r"!\[[^\]]*\]\([^)]+\)", seg)
        if not imgs or not missing:
            continue
        later = [x for x in missing if keys.index(x) > keys.index(k)]
        if not later:
            continue
        tgt = later[0]
        out[tgt] = (out.get(tgt) or "").strip() or imgs[-1]
        out[k] = seg.replace(imgs[-1], "").strip()
        missing.remove(tgt)
        review.append(f"第{no}题第{tgt}问：参考答案是一张图（绘图题），已从上一问尾部挪过来，需人工核对")
    return out


def parse_choice_answers(ans_text: str, expect: int = N_CHOICE) -> dict[int, str]:
    """选择题答案：答案表 / 【答案】锚点 / 「1．A」罗列 / 菁优网「故选：C」四种写法都吃。"""
    got: dict[int, str] = {}
    # 1) 答案表：| 题号 | 1 | 2 | … | / | 答案 | D | B | … |
    for m in re.finditer(r"(?m)^\|\s*(?:题号|题目|序号)\s*\|(.+)$", ans_text):
        nums = [x.strip() for x in m.group(1).strip().strip("|").split("|")]
        after = ans_text[m.end():]
        row = re.search(r"(?m)^\|\s*答案\s*\|(.+)$", after)
        if not row:
            continue
        vals = [re.sub(r"[*\s]", "", x).upper() for x in row.group(1).strip().strip("|").split("|")]
        for n, v in zip(nums, vals):
            if n.isdigit() and re.fullmatch(r"[A-D]", v or ""):
                got.setdefault(int(n), v)
    if len(got) >= expect:
        return got
    # 1b) 无表头的答案表：某行是 1 2 3 … N，紧跟的一行是 A B C …（朝阳二模 / 2024 真题 / 丰台二模）
    lines = ans_text.split("\n")

    def cells_of(ln: str) -> list[str]:
        if not ln.strip().startswith("|"):
            return []
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    for i, ln in enumerate(lines):
        nums = [c for c in cells_of(ln) if c]
        if len(nums) < 2 or not all(c.isdigit() for c in nums):
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            vals = [re.sub(r"[*\s]", "", c).upper() for c in cells_of(lines[j]) if c]
            if len(vals) != len(nums) or not all(re.fullmatch(r"[A-D]", v) for v in vals):
                continue
            for n, v in zip(nums, vals):
                got.setdefault(int(n), v)
            break
    if len(got) >= expect:
        return got
    # 1b1) 裸区间写法（延庆：「1-15ACDAC CDCAA DDCCB」）
    for m in re.finditer(r"(?m)^[ \t]*(\d{1,2})\s*[-–—~]\s*(\d{1,2})\s*([A-D](?:[A-D\s]*[A-D])?)[ \t]*$", ans_text):
        a, b, letters = int(m.group(1)), int(m.group(2)), re.sub(r"\s", "", m.group(3)).upper()
        if b - a + 1 == len(letters):
            for k, v in zip(range(a, b + 1), letters):
                got.setdefault(k, v)
    if len(got) >= expect:
        return got
    # 1b2) 区间写法：「题号1-10答案CDABBABDCD」「题号11-15CABCD」
    for m in re.finditer(r"题\s*号\s*(\d{1,2})\s*[-–—~至]\s*(\d{1,2})\s*答\s*案\s*[:：]?\s*([A-Da-d]{2,})", ans_text):
        a, b, letters = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        for k, v in zip(range(a, b + 1), letters):
            got.setdefault(k, v)
    if len(got) >= expect:
        return got
    # 1c) 纯文本答案表：「题号 1 2 3 … 」+ 下一行「答案 C A C …」（可有若干段）
    for i, ln in enumerate(lines):
        hm = re.match(r"^[ \t]*\**\s*题\s*号\s*[:：]?[ \t]*(.+)$", ln)
        if not hm:
            continue
        nums = re.findall(r"\d{1,2}", hm.group(1))
        if not nums:
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            am = re.match(r"^[ \t]*\**\s*答\s*案\s*[:：]?[ \t]*(.+)$", lines[j])
            if not am:
                continue
            vals = [v for v in re.findall(r"(?<![A-Za-z])([A-D])(?![A-Za-z])", am.group(1))]
            if len(vals) < len(nums):
                vals = [v for v in re.split(r"[\s,，、]+", am.group(1).strip()) if re.fullmatch(r"[A-D]", v)]
            for n, v in zip(nums, vals):
                got.setdefault(int(n), v)
            break
    if len(got) >= expect:
        return got
    # 1d) 整行连写的答案串（「BADBC CBACD DBCCB」）→ 按顺序对应 1..N
    for ln in lines[:60]:
        t = re.sub(r"[^A-Da-d]", "", ln)
        if len(t) >= expect and re.fullmatch(r"[A-D\sa-d]+", ln.strip()) and len(ln.strip()) < expect * 3 + 10:
            for k, v in enumerate(t[:expect], start=1):
                got.setdefault(k, v.upper())
            break
    if len(got) >= expect:
        return got
    # 2) 【答案】锚点：`【答案】1. B 2. C` / `3. 【答案】D`
    for m in re.finditer(r"【答案】([^\n【]*)", ans_text):
        seg = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", m.group(1))
        for k, v in re.findall(r"(\d{1,2})\s*[\\]?\s*[.．、]?\s*([A-D])(?![A-Za-z\u4e00-\u9fff])", seg):
            got.setdefault(int(k), v)
    for m in re.finditer(r"(\d{1,2})\s*[\\]?\s*[.．、]\s*【答案】\s*([A-D])", ans_text):
        got.setdefault(int(m.group(1)), m.group(2))
    if len(got) >= expect:
        return got
    # 3) 纯答案罗列（西城那种「1．C 2. C 3. B …」），只看「答案」开头那几行，避开解析正文
    head = ans_text[:2400]
    for k, v in re.findall(r"(\d{1,2})\s*[\\]?\s*[.．、]\s*([A-D])(?![A-Za-z\u4e00-\u9fff])", head):
        got.setdefault(int(k), v)
    if len(got) >= expect:
        return got
    # 4) 菁优网教师版：每题一段「N．【分析】…【解答】…故选：C。」→ 按题号段找「故选」
    qs = [m for m in QMARK.finditer(ans_text)]
    for i, m in enumerate(qs):
        no = int(m.group(2))
        if no > expect:
            continue
        end = qs[i + 1].start() if i + 1 < len(qs) else len(ans_text)
        seg = ans_text[m.end():end]
        picked = SELECTED.findall(seg)
        if picked:
            got.setdefault(no, picked[-1])
    return got


# ──────────────────────────────────────────────────────────────────
# 分类
# ──────────────────────────────────────────────────────────────────

def cat_cache_path(type_dir: str) -> Path:
    """分类缓存（文件名不带 .json，免得被入库脚本的 *.json 扫到）。"""
    return OUT["root"] / type_dir / ".classify_cache"


def load_cat_cache(type_dir: str) -> dict:
    try:
        return json.loads(cat_cache_path(type_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cat_cache(type_dir: str, data: dict) -> None:
    p_ = cat_cache_path(type_dir)
    p_.parent.mkdir(parents=True, exist_ok=True)
    p_.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def classify(essays: list[dict], use_ai: bool, review: list[str],
             title: str = "", type_dir: str = "一模") -> dict[int, str]:
    """分类：AI 一次调用；`--no-ai` 时优先吃缓存（保证复跑逐字节一致）。"""
    out: dict[int, str] = {}
    cache = load_cat_cache(type_dir)
    cached = {int(k): v for k, v in (cache.get(title) or {}).items() if v in CATS}
    if not use_ai and cached:
        out.update(cached)
        review.append(f"分类取自缓存（--no-ai）：{sorted(cached)}")
    if use_ai and essays:
        items = []
        for sc in essays:
            brief = re.sub(r"\s+", " ", re.sub(r"!\[\]\([^)]+\)", "", sc["content"]))[:260]
            items.append(f"- {sc['no']}｜{brief}")
        try:
            got = chat([{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": CAT_PROMPT.replace("{ITEMS}", "\n".join(items))}], 2000)
            if isinstance(got, dict):
                for k, v in got.items():
                    cat = (v or {}).get("category") if isinstance(v, dict) else v
                    cat = str(cat or "").strip()
                    if cat in CATS:
                        out[int(re.sub(r"\D", "", str(k)))] = cat
                    else:
                        review.append(f"第{k}题：AI 返回的标签 {cat!r} 不在 {CATS}，落到「自然地理」")
            if out:
                cache[title] = {str(k): v for k, v in sorted(out.items())}
                save_cat_cache(type_dir, cache)
        except Exception as e:
            review.append(f"分类 AI 调用失败（{str(e)[:60]}），改用缓存/关键词兜底")
    for sc in essays:
        if sc["no"] in out:
            continue
        text = re.sub(r"!\[\]\([^)]+\)", "", sc["content"])
        best, hit = "自然地理", 0
        for cat, kws in CAT_RULES:
            n = sum(text.count(k) for k in kws)
            if n > hit:
                best, hit = cat, n
        out[sc["no"]] = best
        if hit:
            review.append(f"第{sc['no']}题：分类走关键词兜底 → {best}")
    return out


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────

def paper_title(md: Path) -> str:
    t = re.sub(r"（教师版）|\(教师版\)|含答案", "", md.stem)
    return re.sub(r"\s+", "", t).strip()


def split_regions(body: str, review: list[str]) -> tuple[str, str]:
    """切「选择题区 / 非选择题区」：分节标题 → 第二个「本部分共N题」声明 → 按声明题数数题号。"""
    cut = HDR_ESSAY.search(body)
    if cut:
        return body[:cut.start()], body[cut.end():]
    decl = [(m.start(), int(m.group(1))) for m in DECL_CNT.finditer(body)]
    if len(decl) >= 2:
        review.append("没找到「第二部分」标题，按第二个「本部分共N题」声明切分选择题/非选择题区")
        i = decl[1][0]
        return body[:i], body[i:]
    marks = [m for m in QMARK.finditer(body)]
    if decl and len(marks) > decl[0][1]:
        n = decl[0][1]
        review.append(f"没找到「第二部分」标题，按卷面声明的 {n} 道选择题切分（第 {marks[n].group(2)} 题起为非选择题）")
        return body[:marks[n].start()], body[marks[n].start():]
    # 最后一招：选择题每题都有 A–D 四个选项，大题没有 → 第一道「凑不齐 A–D」的题即非选择题起点
    for i, m in enumerate(marks):
        seg = body[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(body))]
        if len(set(re.findall(r"(?:^|[\s\u3000])([A-D])\s*[.．、]", seg))) < 4:
            if i > 0:
                review.append(f"没找到「第二部分」标题、也没有题数声明，按「有没有 A–D 选项」切分："
                              f"第 {m.group(2)} 题起为非选择题")
                return body[:m.start()], body[m.start():]
            break
    review.append("⚠ 没找到选择题/非选择题的分界（既无「第二部分」标题也无题数声明），按标题切分失败")
    return body, ""


def grade_of(title: str, default: str = "高三") -> str:
    m = re.search(r"高([一二三])", title)
    if m:
        return "高" + m.group(1)
    if "高考真题" in title or "高考" in title:
        return "高三"
    return default


def process(md: Path, type_dir: str, args) -> str:
    title = paper_title(md)
    dst = OUT["root"] / type_dir / f"{title}.json"
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{title}"
    review: list[str] = []
    cfg = PAPERS.get(title)
    if cfg is None:                                  # 通用兜底：docx 导出图 + 卷面分值
        cfg = generic_cfg(md, review)
    txt = md.read_text(encoding="utf-8")
    for old_s, new_s in MANUAL_TEXT_PATCHES.get(title, []):   # 逐卷补丁（丢设问/整块版式被拍成图）
        if old_s not in txt:
            review.append(f"文本补丁未命中（需人工核对）：{old_s[:40]}…")
        txt = txt.replace(old_s, new_s)
    txt = NAME_LINE.sub("", txt)
    txt = BOILER.sub("", txt)
    body, ans = split_paper(txt)
    # 答案区里「16【答案】（1）…」这种题号后没有点号的写法（2024 真题等）→ 补一个点
    ans = re.sub(r"(?m)^[ \t]*\**\s*(\d{1,2})\s*\**\s*(?=【)", r"\1．", ans)
    # 大题题号夹在上一问答案末尾（通州卷「…减少碳排放。18.（8分）」）→ 换到行首，否则整题会漏切
    ans = re.sub(r"(?<=[\s。．；;）)])[ \t]*(\d{1,2})[ \t]*[.．][ \t]*(?=[（(]\s*\d+\s*分)", r"\n\1．", ans)

    choice_text, essay_text = split_regions(body, review)
    if not essay_text:
        raise RuntimeError("没切到非选择题区（找不到分界）")

    # 图片资产（PAPERS 里的卷走显式清单；通用卷 assets 为空，图源就是 docx 导出图）
    info = build_assets(title, cfg, md, getattr(args, "refig", False), review)

    choices = parse_choice_region(choice_text, cfg, md, title, type_dir, review)
    if not choices:
        raise RuntimeError("没切到选择题")
    answers = parse_choice_answers(ans, expect=len(choices))
    if MANUAL_CHOICE_ANSWERS.get(title):          # 答案只以图片给出的卷：人工转录后覆盖
        cfg = dict(cfg, choice_answers=MANUAL_CHOICE_ANSWERS[title])
    if cfg.get("choice_answers"):                 # 答案只以图片给出的卷：人工转录后在此覆盖
        answers.update({int(k): v for k, v in cfg["choice_answers"].items()})
        review.append(f"选择题答案取自人工转录（卷面答案表是图片）：{len(cfg['choice_answers'])} 题")
    details = parse_choice_details(ans, max_no=max(q["no"] for q in choices))
    if details:
        # 详解里常带图（多为 wmf/emf 位图）→ 与正文同一条链路落盘并改成 /exam-images/… 绝对路径
        for k, v in list(details.items()):
            if "![" in (v or ""):
                v = sub_images(tidy(v), cfg, title, type_dir, {}, review)
                details[k] = rewrite_images(v, md.parent, title, type_dir, review, SUBJECT)
        review.append(f"选择题详解 {len(details)} 题收进 analysis（{sorted(details)[:3]}…）")
    miss_ans = [q["no"] for q in choices if not (answers.get(q["no"]) or "").strip()]
    if miss_ans:
        review.append(f"选择题答案缺失：{miss_ans}（answer 置空占位，需人工补）")
    per = choice_per_score(choice_text, choices, review)
    essays = parse_essay_region(essay_text, ans, cfg, md, title, type_dir, review,
                                target_total=(100 - per * len(choices)) or None)
    if not essays:
        raise RuntimeError("没切到非选择题")
    cats = classify(essays, args.ai, review, title=title, type_dir=type_dir)

    sec_choice = {
        "type": "选择题", "questionType": "geography-choice", "title": "一、选择题",
        "category": "选择", "score": per * len(choices),
        "questions": [
            {"id": q["no"], "questionType": "single", "content": q["content"],
             "options": [{"id": o["id"], "label": o["label"]} for o in q["options"]],
             "answer": answers.get(q["no"], ""), "analysis": details.get(q["no"], ""),
             "score": per}
            for q in choices],
    }
    sections = [sec_choice]
    for sc in essays:
        sections.append({
            "type": "非选择题", "questionType": "geography-fill",
            "title": f"二、非选择题（第{sc['no']}题）", "category": cats.get(sc["no"], "自然地理"),
            "score": sc["score"], "content": sc["content"],
            "questions": [
                {"id": sc["no"] * 100 + (k if k else 1), "questionType": "input",
                 "content": prompt, "answer": a, "analysis": an, "score": score}
                for (k, prompt, score), a, an in
                zip(sc["prompts"], sc["answers"], sc.get("analyses") or [""] * len(sc["prompts"]))],
        })

    year = re.search(r"(20\d{2})", title)
    grade = grade_of(title, args.grade)
    paper = {
        "title": title, "subject": SUBJECT, "source": title, "grade": grade,
        "year": int(year.group(1)) if year else None,
        "totalScore": sum(s["score"] for s in sections), "duration": 90,
        "tags": [SUBJECT, grade, type_dir] + ([year.group(1)] if year else []),
    }
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)

    # 图质复核提示（PAPERS 里的显式清单）
    for name, (w, h) in sorted(info.items()):
        if w < 400:
            review.append(f"图质：{name} 只有 {w}×{h}（卷面原生就这么小，标注可能吃力），已肉眼复核并保留")
    # 图质体检（通用兜底：宽 < 500 且题面要读图 → 提示后续补 PDF 截图）
    if cfg.get("generic"):
        figure_audit(payload, md, title, review)
    review += cfg.get("review_extra", [])
    review += MANUAL_NOTES.get(title, [])

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    dst.with_suffix(".review.txt").write_text("\n".join(f"复核：{r}" for r in review) + "\n",
                                              encoding="utf-8")
    return (f"✓ {title}：{len(sections)} 个 section / 选择 {len(choices)} 题 "
            f"({sec_choice['score']}分) + 非选择 {len(essays)} 题 "
            f"({sum(s['score'] for s in sections[1:])}分) = {paper['totalScore']} 分；"
            f"图片 {len(info)} 张；复核 {len(review)} 条")


# ──────────────────────────────────────────────────────────────────
# --check
# ──────────────────────────────────────────────────────────────────

def img_path_of(ref: str) -> Path | None:
    """/exam-images/<类型>/<学科>/<卷>/images/x.png → 磁盘路径（兼容带「（教师版）」的目录）"""
    m = re.fullmatch(r"/exam-images/([^/]+)/([^/]+)/([^/]+)/images/([^/]+)", ref)
    if not m:
        return None
    type_dir, subject, stem, name = m.groups()
    base = OUTPUT_DOCX_TO_MD / type_dir / subject
    for cand in (base / stem / "images" / name, base / f"{stem}（教师版）" / "images" / name):
        if cand.exists():
            return cand
    return None


def figure_audit(payload: dict, src_md: Path, title: str, review: list[str]) -> None:
    """通用兜底卷的图质体检（不裁图，只**量尺寸 + 看题面是否要读图**，可疑的写 `复核：`）。

    判定规则：
      1. 被引用的图逐张量宽高；**宽 < 500px 且该题/该大题的题面提到「图/读图/如图/示意图」**
         → 记一条（提示后续补 PDF 截图）；
      2. 某题题面提到「图N/读图/如图」但**这道题一张图都没有** → 记一条（可能是 docx 丢图）；
      3. 整卷一张图都没有、但题面出现「图1/读图/示意图」→ 记一条（docx 可能整体丢图）；
      4. 同一张图被两个不同的大题共用 → 记一条（图注与实际题号可能不符）。
    """
    MIN_W = 500
    cache: dict[str, tuple[int, int] | None] = {}

    def size_of(ref: str) -> tuple[int, int] | None:
        if ref not in cache:
            p = img_path_of(ref)
            cache[ref] = img_size(p) if p and p.exists() else None
        return cache[ref]

    items: list[tuple[str, str]] = []          # (标签, 文本)
    for sec in payload["sections"]:
        if sec.get("type") == "选择题":
            for q in sec.get("questions") or []:
                items.append((f"第{q.get('id')}题", q.get("content") or ""))
        else:
            items.append((sec.get("title") or "?", sec.get("content") or ""))
            for q in sec.get("questions") or []:
                items.append((f"{sec.get('title') or '?'}小问{q.get('id')}",
                              (q.get("content") or "") + " " + (q.get("answer") or "")))

    all_refs: list[str] = []
    owners: dict[str, list[str]] = {}
    for label, text in items:
        refs = re.findall(r"!\[[^\]]*\]\((/exam-images/[^)]+)\)", text)
        for r in refs:
            if r not in all_refs:
                all_refs.append(r)
            owners.setdefault(r, []).append(label)
        if refs and FIG_MENTION.search(re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)):
            for r in dict.fromkeys(refs):
                wh = size_of(r)
                if wh and wh[0] < MIN_W:
                    review.append(f"图质：{label} 的图 {Path(r).name} 只有 {wh[0]}×{wh[1]}px，"
                                  f"题面要求读图，标注可能看不清 → 建议后续补 PDF 截图")
        elif not refs and FIG_MENTION.search(text) and len(text) > 40:
            review.append(f"图质：{label} 题面提到图，但该题没有图片（docx 可能丢图）→ 建议核对 PDF 原卷")

    if not all_refs:
        joined = " ".join(t for _l, t in items)
        if FIG_MENTION.search(joined):
            review.append("图质：整卷一张图都没有，但题面出现「图N/读图/示意图」字样"
                          "（docx 可能整体丢图）→ 必须核对 PDF 原卷并补截图")
        else:
            review.append("图质：该卷确实没有图（题面也没有读图要求）")

    # 错图形态：高 < 120px 或宽 < 200px（海淀一模 fig-3/fig-5 那种「横条/一小条文字」）
    for r in all_refs:
        wh = size_of(r)
        if wh and (wh[1] < 120 or wh[0] < 200):
            review.append(f"图质：{Path(r).name} 尺寸 {wh[0]}×{wh[1]}px 形态异常（高<120 或宽<200）"
                          f"→ 复核：疑似错图，请对照 PDF 原卷确认是不是这一题的图")

    for r, labs in owners.items():
        big = {l.split("小问")[0] for l in labs if l.startswith("二、")}
        if len(big) > 1:
            review.append(f"图质：{Path(r).name} 被 {sorted(big)} 共用，图注与实际题号可能不符 → 建议核对")

    if all_refs:
        okw = [(Path(r).name, size_of(r)) for r in all_refs if size_of(r) and size_of(r)[0] >= MIN_W]
        review.append(f"图质体检：引用图片 {len(all_refs)} 张，其中宽 ≥{MIN_W}px 的 {len(okw)} 张"
                      f"（docx 导出原图，未裁 PDF）")


def check_one(jf: Path) -> tuple[dict, list[str], list[str]]:
    errs, notes = [], []
    data = json.loads(jf.read_text(encoding="utf-8"))
    paper, sections = data["paper"], data["sections"]
    stat = {"title": paper.get("title"), "choice": 0, "essay": 0, "blanks": 0, "imgs": set()}
    if paper.get("subject") != SUBJECT:
        errs.append(f"paper.subject={paper.get('subject')!r}")
    for k in ("title", "source", "grade", "year", "totalScore", "duration", "tags"):
        if k not in paper:
            errs.append(f"paper 缺字段 {k}")
    sec_sum = sum(s.get("score") or 0 for s in sections)
    if paper.get("totalScore") != sec_sum:
        errs.append(f"totalScore={paper.get('totalScore')} ≠ Σsection.score={sec_sum}")
    if paper.get("totalScore") != 100:
        notes.append(f"totalScore={paper.get('totalScore')}（北京卷常规 100）")

    choice_secs = [s for s in sections if s.get("type") == "选择题"]
    essay_secs = [s for s in sections if s.get("type") != "选择题"]
    if len(choice_secs) != 1:
        errs.append(f"选择题 section 应恰好 1 个，实际 {len(choice_secs)}")
    for s in choice_secs:
        qs = s.get("questions") or []
        stat["choice"] = len(qs)
        if s.get("questionType") != "geography-choice":
            errs.append(f"选择题 questionType={s.get('questionType')}")
        if s.get("category") != "选择":
            errs.append("选择题 category 应为 选择")
        if not qs:
            errs.append("选择题 section 里没有题目")
        ids = [q.get("id") for q in qs]
        if ids and ids != list(range(ids[0], ids[0] + len(ids))):
            errs.append(f"选择题题号不连续：{ids}")
        # 题数 / 每题分值以卷面为准（一模 15×3；高一高二 20/25/30×2、文综 11×4 都是合法形态），
        # 这里只查**自洽**：每题分值一致且为正、section.score == 题数×每题分值。
        per = qs[0].get("score") if qs else None
        if not isinstance(per, int) or per <= 0:
            errs.append(f"选择题每题 score={per!r}（应为正整数）")
        elif any(q.get("score") != per for q in qs):
            errs.append(f"选择题每题分值不一致：{[q.get('score') for q in qs]}")
        elif s.get("score") != per * len(qs):
            errs.append(f"选择题 score={s.get('score')} ≠ {per}×{len(qs)}")
        if len(qs) != N_CHOICE or per != CHOICE_SCORE:
            notes.append(f"选择题 {len(qs)} 题 × {per} 分（非北京高三常规 {N_CHOICE}×{CHOICE_SCORE}，按卷面实际）")
        for q in qs:
            no = q.get("id")
            if q.get("score") != per:
                errs.append(f"第{no}题 score={q.get('score')}（应为 {per}）")
            if q.get("questionType") != "single":
                errs.append(f"第{no}题 questionType={q.get('questionType')}")
            opts = q.get("options") or []
            if [o.get("id") for o in opts] != ["A", "B", "C", "D"]:
                errs.append(f"第{no}题选项 id 不是 A–D：{[o.get('id') for o in opts]}")
            for o in opts:
                if not (o.get("label") or "").strip():
                    errs.append(f"第{no}题选项 {o.get('id')} 为空")
            a = str(q.get("answer") or "")
            if not re.fullmatch(r"[A-D]", a):
                errs.append(f"第{no}题 answer={a!r}（应为单个大写字母）")
            elif a not in [o.get("id") for o in opts]:
                errs.append(f"第{no}题 answer={a!r} 不在选项 id 里")
            if not (q.get("content") or "").strip():
                errs.append(f"第{no}题 content 为空")
            if "analysis" not in q:
                errs.append(f"第{no}题缺 analysis")
            for ref in re.findall(r"!\[\]\((/exam-images/[^)]+)\)", q.get("content") or ""):
                p = img_path_of(ref)
                if p is None:
                    errs.append(f"第{no}题图片找不到：{ref}")
                else:
                    stat["imgs"].add(p)

    if not essay_secs:
        errs.append("没有非选择题 section")
    ids = []
    for s in essay_secs:
        qs = s.get("questions") or []
        stat["essay"] += 1
        stat["blanks"] += len(qs)
        title = s.get("title") or "?"
        content = s.get("content") or ""
        # 形态：单栏（材料 + 设问内联在 content 里），每问后面跟一个大答题框 <AnswerBox></AnswerBox>；
        # 老的 <Input…>/<InputLong…> 一律不许残留。
        n_box = len(PH_ANY_BOX.findall(content))
        n_old = len(re.findall(r"<Input", content))
        if n_old:
            errs.append(f"{title}：section.content 里残留 {n_old} 个 <Input…> 占位符（应全部换成 <AnswerBox>）")
        if n_box != len(qs):
            errs.append(f"{title}：<AnswerBox 出现 {n_box} 次，questions {len(qs)} 个")
        if not content.strip():
            errs.append(f"{title}：section.content 为空（材料丢了）")
        if s.get("questionType") != "geography-fill":
            errs.append(f"{title}：questionType={s.get('questionType')}")
        if s.get("category") not in CATS:
            errs.append(f"{title}：category={s.get('category')!r} 不在 {CATS}")
        ssum = sum(q.get("score") or 0 for q in qs)
        if ssum != s.get("score"):
            errs.append(f"{title}：Σquestions.score={ssum} ≠ section.score={s.get('score')}")
        if not (s.get("score") or 0) > 0:
            errs.append(f"{title}：section.score={s.get('score')}")
        flat_content = re.sub(r"\s+", "", content)
        for q in qs:
            ids.append(q.get("id"))
            if q.get("questionType") != "input":
                errs.append(f"{title} 第{q.get('id')}题：questionType={q.get('questionType')!r}"
                            f"（应为 input）")
            # 单栏版式：小问设问必须**内联在** section.content 里（且只出现一次）
            qc = re.sub(r"\s+", "", q.get("content") or "")
            if qc:
                hits = flat_content.count(qc)
                if hits == 0:
                    errs.append(f"{title} 第{q.get('id')}题：设问不在 section.content 里（单栏版式要求内联）")
                elif hits > 1:
                    errs.append(f"{title} 第{q.get('id')}题：设问在 section.content 里出现 {hits} 次（材料重复）")
            if not (q.get("answer") or "").strip():
                errs.append(f"{title} 第{q.get('id')}题：answer 为空")
            if not (q.get("score") or 0) > 0:
                errs.append(f"{title} 第{q.get('id')}题：score={q.get('score')}（不能为 0）")
            if not (q.get("content") or "").strip():
                errs.append(f"{title} 第{q.get('id')}题：content 为空")
            # 小问题干不应把材料再抄一遍（政治老流水线的 bug）
            if len(q.get("content") or "") > 200:
                notes.append(f"{title} 第{q.get('id')}题：小问题干偏长（{len(q.get('content'))} 字），复核是否混入材料")
            for ref in re.findall(r"!\[\]\((/exam-images/[^)]+)\)",
                                  (q.get("answer") or "") + (q.get("content") or "")):
                p = img_path_of(ref)
                if p is None:
                    errs.append(f"{title} 第{q.get('id')}题图片找不到：{ref}")
                else:
                    stat["imgs"].add(p)
        for ref in re.findall(r"!\[\]\((/exam-images/[^)]+)\)", content):
            p = img_path_of(ref)
            if p is None:
                errs.append(f"{title}：section.content 图片找不到 {ref}")
            else:
                stat["imgs"].add(p)
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        errs.append(f"非选择题题号不连续/重复：{ids}")
    for p in sorted(stat["imgs"]):
        wh = img_size(p)
        if wh and (wh[1] < 120 or wh[0] < 200):
            notes.append(f"图片 {p.name} 尺寸 {wh[0]}×{wh[1]}px 形态异常 → 复核：疑似错图（请对照 PDF 原卷）")
    return stat, errs, notes


def do_check(type_dir: str) -> int:
    root = OUT["root"] / type_dir
    files = sorted(root.glob("*.json"))
    if not files:
        log(f"✗ {root} 下没有 JSON")
        return 1
    log(f"自查 {len(files)} 份：{root}")
    bad = 0
    rows = []
    for jf in files:
        try:
            stat, errs, notes = check_one(jf)
        except Exception as e:
            rows.append((jf.stem, "✗", f"解析失败：{str(e)[:120]}", ""))
            bad += 1
            continue
        ok = not errs
        bad += 0 if ok else 1
        rows.append((stat["title"], "✓" if ok else "✗",
                     f"选择 {stat['choice']} / 非选择 {stat['essay']}（{stat['blanks']} 问）/ 图 {len(stat['imgs'])} 张",
                     "; ".join(errs)))
        for n in notes:
            log(f"    · 提示：{n}")
    w = max(len(r[0]) for r in rows) + 2
    for title, flag, info, err in rows:
        log(f"  {title.ljust(w)}{flag}  {info}")
        if err:
            for e in err.split("; "):
                log(f"      ✗ {e}")
    log("—" * 60)
    log(("全部通过 ✓" if not bad else f"{bad} 份有问题 ✗"))
    return 0 if not bad else 1


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="地理试卷 MD → 结构化 JSON（可 --check）")
    ap.add_argument("-p", "--path", help="单份 MD 文件，或某一卷的目录")
    ap.add_argument("--dir", default=str(OUTPUT_DOCX_TO_MD), help="MD 根目录")
    ap.add_argument("-T", "--type", default="一模")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-o", "--output", default=str(OUT_ROOT))
    ap.add_argument("-w", "--workers", default="1", help="兼容参数（本脚本单进程串行）")
    ap.add_argument("--papers", nargs="*", default=None, help="只处理这几卷（卷名关键字）")
    ap.add_argument("--force", action="store_true", help="覆盖已有 JSON")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="不调模型（分类走关键词兜底）")
    ap.add_argument("--refig", action="store_true", help="重新生成 PDF 裁图（默认有就复用）")
    ap.add_argument("--check", action="store_true", help="纯 Python 自查已有 JSON")
    args = ap.parse_args()

    OUT["root"] = Path(args.output)
    if args.check:
        sys.exit(do_check(args.type))

    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md")) if p.is_dir() else [p]
    else:
        root = Path(args.dir) / args.type / SUBJECT
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir())
                 if d.is_dir() and sorted(d.glob("*.md"))]
    if args.papers:
        files = [f for f in files if any(k in f.stem for k in args.papers)]
    log(f"共 {len(files)} 份 MD")
    for f in files:
        try:
            log("  " + process(f, args.type, args))
        except Exception as e:
            log(f"  ✗ {f.parent.name}: {str(e)[:200]}")
            if str(e):
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    main()
