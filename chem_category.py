#!/usr/bin/env python3
"""化学非选择题题型分类器：结构 / 原理 / 有机 / 流程 / 探究。

输出的 `category` 只取这 5 个**短名**，与题库（gxexercise）的题型全名一一对应：

    结构 → 物质结构与性质
    原理 → 化学反应原理
    有机 → 有机化学基础
    流程 → 工业流程
    探究 → 实验探究

（题库侧用 `src/constants/chemistry.ts` 的 CHEM_CATEGORY_LABELS 把短名映射成全名；
本模块不输出全名，也不要自创「物质结构 / 反应原理 / 工业流程题」这类变体。）

背景：化学卷的非选择题要按固定题型归档（15 结构 / 16 原理 / 17 有机 / 18 流程 / 19 探究）。
- **2021 及以后**：北京卷题型位置基本固定，按题号给位置先验（15→结构 … 19→探究），
  只有当「该位置类别的关键词命中 <2」且「得分最高类别命中 ≥3」时才按内容覆盖（reason=覆盖）。
  （海淀 2026 期中就是例外：17 题是「CH4-CO2 重整」的原理题，靠这条覆盖规则纠正。）
- **2020 及以前**：题型位置没固定（老卷甚至是 25-28 / 8-11 编号），纯关键词打分
  （reason=关键词），完全没命中才用位置先验兜底（reason=兜底）。

打分口径：强标记词 3 分、普通词 1 分，按**命中的不同关键词个数**计（同一个词重复出现只算 1 次；
同类别内长词优先并屏蔽其子串，避免「反应速率 + 速率」被重复计数）。
打平按「强标记命中数 → 总命中数 → 位置先验」。

题眼加分（只给「流程 / 探究」，其余三类保持纯加权）：这两个类别最容易被「原理」抢走——化学原理词
（平衡 / 速率 / 电极 / 转化率…）几乎每题都出现，属于「背景类别」；而「工艺 / 流程」「探究」
这类题眼只有该题型才有。判定为 +2 一项、封顶 +6：
  1) 主题眼命中（流程: `工艺|流程`；探究: `探究`）+2；
  2) 题眼成组（SIGNATURE 命中 ≥2 个，如「浸取 + 滤液 + 过程Ⅰ」「假设 + 对照 + 实验小组」）+2；
  3) 题头（材料首句前 60 字）就点明「…的流程如下 / 探究…」再 +2。
  题头之所以单独加分：小题问里常混别的考点（例如工艺流程题最后一个小问考电解），但材料首句
  最能代表整题题型；用题头加权可以让「工艺流程题被原理抢走」这类错误明显减少。

老卷（2020 及以前）另有一张 OLD_POS 位置先验（理综 25-28 / 独立命题 8-11 的固定顺序），
**只用于打平裁决**，不做位置覆盖（老卷题型位置并不严格固定）。

用法：
  python3 chem_category.py --paper 2026北京海淀高三（上）期中化学 --print
  python3 chem_category.py --paper 2023北京海淀高三二模化学 --print --json /tmp/x.json
  python3 chem_category.py --all --json /tmp/chem_categories.json
  python3 chem_category.py --all --print            # 逐卷逐题打印（很长）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from convert_chinese_md import split_paper
from convert_chemistry_full import QSPLIT, SCORE_RE
from paths import OUTPUT_DOCX_TO_MD

CATS = ["结构", "原理", "有机", "流程", "探究"]
# 题库里的题型全名（前端导航 / 筛选键，gxexercise: src/constants/subjects.ts 的
# categoryToRoute['化学']）。输出到 JSON 的 category 一律用上面的**短名**，入库时由题库侧
# 用 CHEM_CATEGORY_LABELS 映射成全名；这里只作为对照表，方便人工核对，别在输出里用全名。
CAT_FULL = {
    "结构": "物质结构与性质",
    "原理": "化学反应原理",
    "有机": "有机化学基础",
    "流程": "工业流程",
    "探究": "实验探究",
}
# 2021+ 的位置先验：15→结构 16→原理 17→有机 18→流程 19→探究
POS = {15: "结构", 16: "原理", 17: "有机", 18: "流程", 19: "探究"}
CONF_MIN_SCORE = 5                        # 内容判定门槛：最高分 ≥5
CONF_RATIO = 1.5                          # 且 最高分 ≥1.5 × 次高分
POS_FROM_YEAR = 2021                      # 仅用于打印口径说明
# 老卷（2020 及以前）的位置先验：理综 25-28 / 独立命题 8-11 的固定顺序，
# **只用来打平裁决**（老卷题型位置并不严格固定，不做位置覆盖）。
OLD_POS = {25: "有机", 26: "原理", 27: "流程", 28: "探究",
           8: "有机", 9: "原理", 10: "流程", 11: "探究",
           15: "结构", 16: "原理", 17: "有机", 18: "流程", 19: "探究"}
W_STRONG, W_NORMAL = 3, 1                 # 强标记词 / 普通词权重
BONUS_STEP, BONUS_CAP = 2, 6              # 题眼加分：主题眼 +2、题眼成组 +2、题头 +2，封顶 +6
HEAD_CHARS = 60                           # 「题头」（材料/首句）取前多少字
DEFAULT_SCORE = 13                        # 取不到分值时的兜底

# ---------------------------------------------------------------- 关键词表
# 强标记词（3 分）：一眼能定题型的「题眼」
STRONG = {
    "结构": [
        "价层电子排布", "电子排布式", "电子排布图", "核外电子排布", "原子结构示意",
        "元素周期表", "周期表", "元素周期律", "晶胞", "晶格能", "晶体类型", "杂化",
        "电离能", "电负性", "配合物", "配位键", "配位数", "空间构型", "空间结构",
        "未成对电子", "孤电子对", "分子的极性", "手性", "等电子体",
        "化学键", "键长", "键角", "σ键", "π键",
    ],
    "原理": [
        "化学平衡", "平衡常数", "平衡转化率", "平衡", "转化率", "反应速率", "速率",
        "反应热", "热化学方程式", "焓变", "活化能", "熵变", "自发",
        "原电池", "电解", "电化学", "电极反应", "电极", "燃料电池",
        "电离平衡", "电离常数", "电离", "水解", "沉淀溶解平衡", "Ksp", "溶度积",
        "能量变化", "恒温恒容", "化学平衡常数", "体积分数", "外界条件", "选择性",
    ],
    "有机": [
        "合成路线", "同分异构", "官能团", "结构简式", "反应类型", "核磁共振氢谱",
        "键线式", "芳香烃", "加聚", "缩聚", "顺反异构", "酯化反应", "消去反应",
        "银镜反应", "取代反应", "加成反应", "高分子化合物", "聚合物",
        "有机合成", "有机物", "分子式",
    ],
    "流程": [
        "工艺流程", "流程图", "流程如下", "的流程", "制备流程", "工业流程",
        "工艺", "流程", "生产流程", "制备工艺",
        "浸取", "浸出", "酸浸", "碱浸", "水浸", "浸取液",
        "滤渣", "滤液", "母液", "焙烧", "煅烧",
        "除杂", "资源化", "综合利用", "回收", "反萃取", "提纯",
        r"过程[ⅠⅡⅢⅣⅤⅥIVX]", r"步骤[ⅠⅡⅢⅣⅤⅥIVX①②③④⑤]", r"操作[①②③④⑤a-dA-D]",
    ],
    "探究": [
        "探究", "假设", "猜想", "对照组", "对照实验", "对比实验",
        "异常现象", "实验反思",
        "实验小组", "研究小组", "兴趣小组", "小组",
        r"实验[一二三四1-4]",
    ],
}

# 普通词（1 分）：常见但不独有，只做辅助
NORMAL = {
    "结构": [
        "电子式", "结构式", "配位", "离子键", "共价键", "金属键",
        "轨道", "能级", "原子半径", "核电荷数", "晶体",
    ],
    "原理": [
        "催化剂", "催化", "主反应", "副反应", "外界条件", "温度", "压强",
        r"[a-dnpq]极", r"[正负阴阳]极", "电流", "导电",
        "氧化还原", "电子转移", "物质的量", "分解率",
        "去除率", "脱除", "滴定", "尾气", "废气", "烟气",
    ],
    "有机": [
        "苯环", "羟基", "羧基", "醛基", "酯基", "氨基", "碳碳双键",
        "烃", "烯烃", "烷烃", "卤代烃", "油脂", "反应条件", "步骤",
    ],
    "流程": [
        "过滤", "洗涤", "干燥", "蒸发浓缩", "冷却结晶", "研磨", "粉碎",
        "原料", "产品", "产率", "废渣", "废液", "废屑", "分离", "沉淀率", "提取",
        "冶炼", "杂质",
        "调节", "控制pH", "控制pH范围", "利用率",
        "过程", "步骤", "工业上", "生产",
    ],
    "探究": [
        # 「实验方案 / 设计实验 / 补充实验」是**让你去设计**的设问，原理 / 流程题也常出现
        # （如「设计实验方案证明…」），所以降为普通词；真正的探究题有 探究/假设/猜想/对照 等强标记。
        "实验方案", "设计实验", "补充实验", "验证", "证实",
        "查阅资料", "实验现象", "预期现象", "实验记录", "结论", "反思",
        "排除", "证明", "试管", "实验操作", "实验目的", "研究",
    ],
}

# 题眼组合加分：只给「流程 / 探究」（原理几乎每题都沾边，其它类别关键词已足够独特）
# 口径：主题眼（PRIMARY）命中 +2；题眼（SIGNATURE）命中 ≥2 个再 +2（合计封顶 +4）。
# 理由：化学原理词（平衡 / 速率 / 电极…）几乎每题都出现，是「背景类别」；而「工艺 / 流程 /
# 探究」这类题眼只有该题型才有，给它们加分才能挡住「工艺流程题被 □原理 抢走」。
PRIMARY = {
    "流程": r"工艺|流程",
    "探究": r"探究",
}

SIGNATURE = {
    "流程": [
        r"工艺流程|流程图|流程如下|的流程|制备流程",
        r"浸取|浸出|酸浸|碱浸|水浸|浸取液",
        r"滤液|滤渣|母液",
        r"资源化|综合利用|回收|提纯",
        r"过程[ⅠⅡⅢⅣⅤⅥIVX]|步骤[ⅠⅡⅢⅣⅤⅥIVX]",
        r"焙烧|煅烧",
        r"除杂|杂质",
        r"反萃取|萃取",
    ],
    "探究": [
        r"探究",
        r"假设|猜想",
        r"实验方案|设计实验|补充实验",
        r"对照组|对照实验|对比实验",
        r"实验小组|研究小组|兴趣小组",
        r"异常现象",
    ],
}

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_WS_RE = re.compile(r"[ \t\u3000]+")


def clean_text(text: str) -> str:
    """统计前清掉 HTML/组件标签、图片语法与多余空白。"""
    t = _TAG_RE.sub(" ", text or "")
    t = _IMG_RE.sub(" ", t)
    t = t.replace("\\", "")          # 转义符（\- \_）会影响词面匹配
    t = _WS_RE.sub(" ", t)
    return t


def _count_hits(text: str, words: list[str]) -> list[str]:
    """返回命中的不同关键词（长词优先，命中后屏蔽其子串）。"""
    work = text
    hit = []
    for w in sorted(words, key=len, reverse=True):
        if re.search(w, work):
            hit.append(w)
            work = re.sub(w, "\u0000" * 2, work)   # 屏蔽，避免长短词重复计数
    return hit


def score_text(text: str) -> dict[str, dict]:
    """对一段题面打分，返回 {类别: {strong, normal, score, words, sig}}。"""
    t = clean_text(text)
    head = t[:HEAD_CHARS]          # 材料首句最能代表题型（小问里常混别的考点）
    out = {}
    for cat in CATS:
        sw = _count_hits(t, STRONG[cat])
        nw = _count_hits(t, NORMAL[cat])
        sig = [p for p in SIGNATURE.get(cat, []) if re.search(p, t)]
        bonus = 0
        if cat in PRIMARY and re.search(PRIMARY[cat], t):
            bonus += BONUS_STEP                       # 主题眼（工艺/流程、探究）
        if len(sig) >= 2:
            bonus += BONUS_STEP                       # 题眼成组出现再 +2
        if cat in PRIMARY and re.search(PRIMARY[cat], head):
            bonus += BONUS_STEP                       # 题头就点明「…的流程如下 / 探究…」再 +2
        bonus = min(bonus, BONUS_CAP)
        out[cat] = {
            "strong": len(sw),
            "normal": len(nw),
            "total": len(sw) + len(nw),
            "bonus": bonus,
            "score": W_STRONG * len(sw) + W_NORMAL * len(nw) + bonus,
            "words": list(dict.fromkeys(sw + nw)),
            "sig": sig,
        }
    return out


def _prior_order(pos_cat: str | None) -> list[str]:
    """位置先验排序：位置类别排第一，其余按标准顺序。"""
    if pos_cat in CATS:
        return [pos_cat] + [c for c in CATS if c != pos_cat]
    return list(CATS)


def _rank_key(cat: str, sc: dict[str, dict], prior: list[str]):
    """越大越优：得分 → 强标记数 → 总命中数 → 位置先验。"""
    s = sc[cat]
    return (s["score"], s["strong"], s["total"], -prior.index(cat))


def classify(text: str, no: int, year: int | None, strict_position: bool = False) -> tuple[str, dict, str]:
    """给一道非选择题定题型。

    **2021 及以后**（北京卷新高考题型位置固定，用户口径）：
      - 一律听位置：15→结构 16→原理 17→有机 18→流程 19→探究，reason="位置"；
      - 唯一覆盖条件（收紧到「强标记证据」上）：位置类别的大题文本**强标记命中数 = 0**，
        且另一个类别**强标记命中数 ≥2** → 取那个类别，reason="覆盖"。
        （海淀 2026 期中 17 题就是靠它从「有机」纠正为「原理」：有机强标记 0，原理强标记 ≥2。）

    **2020 及以前**：题型位置并不固定，内容关键词优先——
      1) 最高分类别「得分 ≥5」且「≥1.5 × 次高分」→ 取它，reason="关键词"；
      2) 否则退位置表（15-19 用 POS，理综 25-28 / 独立 8-11 用 OLD_POS），reason="位置"；
      3) 都没有位置可退 → 取最高分，reason="兜底"。

    返回 (category, hits, reason)；hits = {类别: {strong, normal, total, bonus, score, words, sig}}。
    """
    sc = score_text(text)
    pos_cat = POS.get(no)
    new_era = year is None or year >= POS_FROM_YEAR

    if new_era and pos_cat:
        if sc[pos_cat]["strong"] == 0 and not strict_position:
            others = sorted((c for c in CATS if c != pos_cat),
                            key=lambda c: (sc[c]["strong"], sc[c]["score"], sc[c]["total"]), reverse=True)
            best = others[0]
            if sc[best]["strong"] >= 2:
                return best, sc, "覆盖"
        return pos_cat, sc, "位置"

    prior = _prior_order(pos_cat or OLD_POS.get(no))
    ranked = sorted(CATS, key=lambda c: _rank_key(c, sc, prior), reverse=True)
    top, second = ranked[0], ranked[1]
    t_score, s_score = sc[top]["score"], sc[second]["score"]
    if t_score >= CONF_MIN_SCORE and t_score >= CONF_RATIO * s_score:
        return top, sc, "关键词"
    pos_or_old = pos_cat or OLD_POS.get(no)
    if pos_or_old:
        return pos_or_old, sc, "位置"
    return top, sc, "兜底"


# ---------------------------------------------------------------- 取题与分值
def _segments(body: str) -> dict[int, str]:
    """按题号切题目区，返回 {题号: 题面}。

    题号做**单调递增过滤**（同 crop_questions_pdf.find_markers 的思路）：正文里的
    「已知条件 1. / 2. / 3.」、「①…」之类被 QSPLIT 当成题号时，会让取段整体错位
    （2021 丰台二模就切出 [1..7, 1, 2, 10..19, 1, 2, 3, 19]）。只接受比上一个更大的题号。
    """
    marks = [(m.start(), m.end(), int(m.group(1))) for m in QSPLIT.finditer(body)]
    keep: list[tuple[int, int, int]] = []
    prev = -1
    for s, e, no in marks:
        if no <= prev or no > 40:          # 单调递增 + 明显异常的大题号直接丢
            continue
        keep.append((s, e, no))
        prev = no
    out: dict[int, str] = {}
    for i, (_s, e, no) in enumerate(keep):
        nxt = keep[i + 1][0] if i + 1 < len(keep) else len(body)
        seg = body[e:nxt]
        seg = re.split(r"\n\s*参考答案", seg)[0]
        seg = re.split(r"(?m)^[ \t]*第[二2]部分", seg)[0]
        out.setdefault(no, seg.lstrip("\n \t"))
    return out


def _score_of(seg: str) -> int | None:
    """题目开头的「（N分）」；只在开头 120 字内找，避免把某小问的分值当成大题分值。"""
    m = SCORE_RE.search(seg[:120])
    return int(m.group(1)) if m else None


def non_choice_numbers(nos: list[int]) -> list[int]:
    """按题号判断哪些是「非选择题」。

    - 有 15-19 的卷（2020 及以后）：就是 15-19；
    - 理综式编号（2015-2019 部分卷）：25-28；
    - 老卷独立命题（1-11）：选择题在前，取 ≥8 的题号（多于 4 个时取最后 4 个）。
    """
    uniq = sorted(set(nos))
    five = [n for n in uniq if 15 <= n <= 19]
    if len(five) >= 4:                 # 2020+ 标准编号（偶尔缺 1 题仍算）
        return five
    zh = [n for n in uniq if 25 <= n <= 28]
    if zh:                             # 理综式：25-28 是非选择
        return zh
    if len(five) >= 2:                 # 只剩 2-3 道 15-19，也按 15-19 处理
        return five
    tail = [n for n in uniq if n >= 8]
    if len(tail) > 4:
        tail = tail[-4:]
    return tail


def year_of(name: str) -> int | None:
    m = re.search(r"(20\d{2})", name)
    return int(m.group(1)) if m else None


def load_paper(md_path: Path) -> dict:
    """读一卷 MD：返回 {name, year, questions: {题号: {text, score, has_score}}}。"""
    txt = Path(md_path).read_text(encoding="utf-8")
    body, _ans = split_paper(txt)
    segs = _segments(body)
    name = re.sub(r"（教师版）|\(教师版\)|含答案", "", Path(md_path).stem).strip()
    nos = non_choice_numbers(list(segs))
    qs = {}
    for no in nos:
        seg = segs.get(no, "")
        s = _score_of(seg)
        qs[no] = {"text": seg, "score": s if s is not None else DEFAULT_SCORE,
                  "has_score": s is not None}
    return {"name": name, "year": year_of(name), "questions": qs}


def analyze_paper(md_path: Path, strict_position: bool = False) -> dict:
    """分类一卷，返回 {name, year, questions: {题号: {category, score, reason, top, hits}}}。"""
    info = load_paper(md_path)
    out = {}
    for no, q in info["questions"].items():
        cat, sc, reason = classify(q["text"], no, info["year"], strict_position)
        top = {c: sc[c]["score"] for c in
               sorted(CATS, key=lambda c: sc[c]["score"], reverse=True) if sc[c]["score"] > 0}
        out[str(no)] = {
            "category": cat,
            "score": q["score"],
            "reason": reason,
            "has_score": q["has_score"],
            "top": dict(list(top.items())[:3]),
            "hits": {c: sc[c]["words"] for c in CATS if sc[c]["words"]},
        }
    return {"name": info["name"], "year": info["year"], "questions": out}


# ---------------------------------------------------------------- 遍历 & CLI
def iter_teacher_mds(root: Path | None = None):
    """遍历 output_docx_to_md/*/化学/*（教师版）/*.md（跳过 venv）。"""
    root = Path(root or OUTPUT_DOCX_TO_MD)
    for md in sorted(root.glob("*/化学/*（教师版）/*.md")):
        if any(p == "venv" or p.startswith("venv") for p in md.parts):
            continue
        yield md


def find_paper(key: str, root: Path | None = None) -> Path:
    """按卷名（可带/不带（教师版））找 MD；先精确、再子串。"""
    key_norm = re.sub(r"（教师版）|\(教师版\)", "", key).strip()
    mds = list(iter_teacher_mds(root))
    for md in mds:
        if re.sub(r"（教师版）|\(教师版\)", "", md.stem).strip() == key_norm:
            return md
    fuzzy = [md for md in mds if key_norm in re.sub(r"（教师版）|\(教师版\)", "", md.stem)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise SystemExit(f"找不到卷：{key}")
    raise SystemExit("卷名不唯一，请写全：\n  " + "\n  ".join(m.stem for m in fuzzy))


def _fmt_hits(words: list[str]) -> str:
    return "，".join(words[:8]) if words else "—"


def _print_paper(rep: dict, position_rule: bool) -> None:
    print(f"\n【{rep['name']}】year={rep['year']} 分类口径={'位置优先(2021+)' if position_rule else '纯关键词'}")
    print("  短名 → 题库全名：" + "，".join(f"{c}={CAT_FULL[c]}" for c in CATS))
    print(f"  {'题号':<4}{'类别':<5}{'分值':<5}{'reason':<7}{'打分(前3)':<26}命中词")
    for no, q in rep["questions"].items():
        top = " ".join(f"{c}:{s}" for c, s in q["top"].items())
        score = str(q["score"]) + ("" if q["has_score"] else "(缺,兜底)")
        print(f"  {no:<6}{q['category']:<6}{score:<6}{q['reason']:<8}{top:<26}{_fmt_hits(q['hits'].get(q['category'], []))}")


def main():
    ap = argparse.ArgumentParser(description="化学非选择题题型分类（结构/原理/有机/流程/探究）")
    ap.add_argument("--paper", default=None, help="卷名（去掉（教师版））或子串")
    ap.add_argument("--all", action="store_true", help="遍历 output_docx_to_md/*/化学/*（教师版）/*.md")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("--print", dest="do_print", action="store_true", help="打印逐题分类明细")
    ap.add_argument("--root", default=None, help="output_docx_to_md 根目录（默认取 paths.py）")
    ap.add_argument("--strict-position", action="store_true",
                    help="2021+ 一律按位置，不做「覆盖」（对比口径用）")
    args = ap.parse_args()

    root = Path(args.root) if args.root else None
    if args.paper:
        mds = [find_paper(args.paper, root)]
    elif args.all:
        mds = list(iter_teacher_mds(root))
    else:
        ap.error("要么 --paper <卷名>，要么 --all")

    result: dict[str, dict] = {}
    no_score_papers: list[str] = []
    for md in mds:
        try:
            rep = analyze_paper(md, args.strict_position)
        except Exception as e:                      # 单卷失败不影响全量
            print(f"  ✗ {md.parent.name}: {str(e)[:160]}", file=sys.stderr)
            continue
        result[rep["name"]] = rep["questions"]
        if any(not q["has_score"] for q in rep["questions"].values()):
            no_score_papers.append(rep["name"])
        if args.do_print:
            _print_paper(rep, (rep["year"] or 0) >= POS_FROM_YEAR)

    # ---- 统计
    dist: dict[str, int] = {}
    reasons: dict[str, int] = {}
    total = 0
    for qs in result.values():
        for q in qs.values():
            dist[q["category"]] = dist.get(q["category"], 0) + 1
            reasons[q["reason"]] = reasons.get(q["reason"], 0) + 1
            total += 1
    if args.all:
        print(f"\n共 {len(result)} 卷 / {total} 道非选择题")
        print("类别分布：" + "  ".join(f"{c} {dist.get(c, 0)}" for c in CATS))
        print("判定来源：" + "  ".join(f"{r} {n}" for r, n in sorted(reasons.items(), key=lambda x: -x[1])))
        if no_score_papers:
            print(f"缺分值（用 {DEFAULT_SCORE} 兜底）的卷 {len(no_score_papers)} 卷：" + "、".join(no_score_papers))

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写出 {args.json}")


if __name__ == "__main__":
    main()
