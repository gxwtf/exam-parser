#!/usr/bin/env python3
"""跨学科「AI 检查 + 兜底修复」质量脚手架（生物以外 6 科通用）。

生物链路（`convert_biology_md.py --check/--cross-check/--reclassify`）的跨学科通用版：
不复制生物专用逻辑，而是把各学科的产物结构归一成同一个模型

    Paper（一卷）→ items/units（groups | sections | 图片版 paper.json）→ questions

再做三类事：**确定性检查**（不调模型）、**AI 抽查**（花钱，带缓存）、**兜底修复**（默认 dry-run）。

用法
----
    python3 qa_scaffold.py --subject 英语 --type 二模 --check
    python3 qa_scaffold.py --subject 数学 --type 二模 --check --report qa/数学.md
    python3 qa_scaffold.py --subject 英语 --type 二模 --cross-check 10     # 每卷最多 10 题
    python3 qa_scaffold.py --subject 语文 --type 二模 --ai-audit 10       # 学科知识/自洽性抽查
    python3 qa_scaffold.py --subject 物理 --type 二模 --repair            # 默认 dry-run
    python3 qa_scaffold.py --subject 物理 --type 二模 --repair --apply    # 真正写盘
    python3 qa_scaffold.py --subject 化学 --type 二模 --check --image     # 只查图片版（确定性）

学科：英语 / 语文 / 数学 / 物理 / 化学 / 政治。类型：`--type 二模`、`--type 二模,一模`、`--type all`。

产物
----
- 报告：`<数据根>/qa/<学科>.md`（问题计数 + 逐条清单 + 修复建议）
- 汇总：`<数据根>/qa/README.md`（各科汇总表，自动生成）
- 状态：`<数据根>/qa/<学科>.json`（分节累积：check / cross_check / ai_audit / repair 各跑各的都能合并进同一份报告）
- 缓存：`<数据根>/qa/.cache/<学科>/<prompt-sha>.json`（`--no-ai` 只吃缓存，复跑逐字节一致）
- 留痕：每卷 `<卷>.review.txt`（只增 `qa修复：` 行，重复跑幂等）

铁律
----
1. **不改教师答案**。唯一例外：答案为空、或答案根本不在选项内，且「AI 两次独立作答一致 + 格式合法」
   才补/换，并在 `analysis` 追加「AI 补答案（原卷缺失）」、在 `<卷>.review.txt` 留痕。
2. 开放题（作文/微写作/阅读表达/长篇简答）**不参与 AI 兜底**，只报告。
3. 只读不改的检查（含 `--check`）绝不写 JSON；`--repair` 默认 dry-run，`--apply` 才落盘。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import time
import json
import os
import re
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paths import (
    DATA_ROOT,
    OUTPUT_DOCX_TO_MD,
    OUTPUT_IMAGE_QUESTIONS,
    OUTPUT_JSON,
    OUTPUT_JSON_CHINESE,
    OUTPUT_JSON_POLITICS,
    OUTPUT_JSON_MATH,
)

# ──────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────

SUBJECTS = ("英语", "语文", "数学", "物理", "化学", "政治")
ALL_TYPES = ("一模", "二模", "上期末", "下期末", "真题")
QA_DIR = DATA_ROOT / "qa"
CACHE_DIR = QA_DIR / ".cache"
REVIEW_PREFIX = "qa修复："

WEB_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# 填空题占位符：完形 / 七选五 / 语法填空 / 数理填空
BLANK_TAGS = ("InputLong", "Input2", "Input", "Blank2", "Blank", "ClozeBlank")
BLANK_RE = re.compile(r"<(?:" + "|".join(BLANK_TAGS) + r")\b")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s\"']+)")
TAG_IMG_RE = re.compile(r"<(?:QImg|img)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)")
IMG_EXAM_PREFIX = "/exam-images/"

# 可做 AI 兜底的填空类题型（机器可判、答案短）
FILL_TYPES = {"input", "input2", "inputlong", "grammar", "cloze", "blank",
              "cloze-blank", "fill", "single-blank"}
# 开放题：答案不可机器判，永不参与 AI 兜底
OPEN_TYPES = {"text", "essay", "writing", "en-writing", "chinese-essay",
              "chinese-micro-writing", "reading-expression", "micro-writing"}

# 只收录「选择 + 填空」的链路：totalScore 差额通常是非选择题部分
PARTIAL_SUBJECTS = {"数学", "物理", "化学"}

MODEL = None  # 延迟从 .env 读

SUGGEST = {
    "E_EMPTY_CHOICE": "选择题答案缺失 → 可 `--repair --apply` 用模型双跑一致兜底；否则人工补。",
    "W_EMPTY_FILL": "填空题答案缺失 → 同上（模型双跑一致才补）。",
    "I_EMPTY_OPEN": "开放题（作文/简答）无答案属正常存量缺口，不入库判分即可，需人工或另行建答案库。",
    "E_ANSWER_NOT_IN_OPTIONS": "答案不在选项内：多为解析串入 answer 字段。核对原卷；确认后 `--repair --apply` 可替换为模型双跑一致且在选项内的字母。",
    "W_ANSWER_MULTI": "选择题出现多字母：若原卷为多选题可保留（questionType 应为 multiple）；否则按错答案处理。",
    "W_ANSWER_SEPARATOR": "答案带 / 、 等分隔符：判题端不认，需人工确认后规范为单个字母。",
    "W_ANSWER_FORMAT": "大小写/全角/首尾空白不规范 → `--repair --apply` 可自动归一（不改语义）。",
    "E_DUP_OPTION_ID": "选项 id 重复 → 重新切选项，不能用 AI 兜。",
    "E_FEW_OPTIONS": "选项不足 2 项 → 多半是切题截断，需重跑该卷的选择题链路。",
    "W_EMPTY_OPTION_LABEL": "选项 label 为空 → 需回原卷/PDF 补，不入库。",
    "E_SCORE_LE0": "题分值为 0/缺失 → `--repair --apply` 在题组还有剩余分值时可均分兜底；若题组分值已被某个小问占满（如 1-1=24、其余 0）则无法自动兜底，需按卷面分值表人工重分。",
    "W_UNIT_SCORE_MISMATCH": "题组分值与小题合计不符 → 先修 0 分题，再人工核对分值表。",
    "W_TOTAL_SCORE_MISMATCH": "paper.totalScore 与各题组分值合计不符 → 人工核对卷面满分。",
    "I_TOTAL_SCORE_PARTIAL": "本链路只收录选择/填空题，totalScore 差额属非选择题部分（可忽略）。",
    "E_MISSING_IMAGE": "图片引用指向的文件不存在 → 若同目录/同卷有同名替身可自动改引用，否则必须补图（nginx 直出 404）。",
    "W_IMAGE_NONWEB_EXT": "图片扩展名 nginx 不放行（.wmf/.emf/.tiff）→ 需转 png 后再引用。",
    "W_BLANK_COUNT": "占位符数量与小题数不一致 → 重跑占位符重排（生物/英语链路有对应修复），否则答题端错位。",
    "W_DUP_STEM": "同卷题干完全重复（去空白/图片后）→ 多为题号重复切出，需人工删重。",
    "W_PAPER_SUBJECT": "paper.subject 与学科不符 → 入库会串科。",
    "W_UNIT_SCORE_MISSING": "题组缺 score → 无法做分值与均分兜底。",
    "E_IMAGE_PNG_MISSING": "图片版缺 qN.png → 前端无题面，必须重裁。",
    "W_IMAGE_JSON_MISMATCH": "图片版 qN.json 与 paper.json 不一致 → 以 paper.json 为准重生成 qN.json。",
    "I_IMG_TEXT_QUESTION": "化学图片版只做选择题：非选择题无文字题干/分值/答案，属链路现状，需另建文本链路。",
    "W_ANSWER_IS_STEM": "答案区被错切（answer 装的是题干）→ 该卷 answer/analysis 整体错位，建议重跑该卷转换；AI 兜底只能补字母，解析仍需人工。",
    "W_ANSWER_EMBEDDED": "answer 里串进了解析文本 → 同上，重跑该卷；`--repair` 只会在模型双跑一致时换成干净字母。",
    "W_ANALYSIS_IS_STEM": "解析字段是题干原文/别题题干 → 题面与解析错位，必须人工或重跑该卷，不能靠 AI 兜底。",
}

SEV_ZH = {"error": "错误", "warn": "警告", "info": "提示"}
SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


# ──────────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────────

def fmt(x: Any) -> str:
    """数字格式化：1.0 → 1，1.50 → 1.5。"""
    if x is None:
        return "—"
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, (int, float)):
        f = float(x)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    return str(x)


def norm_num(x: Any) -> float | None:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def norm_letters(s: Any) -> str:
    """答案字母归一：去空白（含全角空格）、全角→半角、转大写。"""
    t = nfkc(str(s if s is not None else ""))
    t = t.replace("\u3000", " ")
    t = re.sub(r"\s+", "", t)
    return t.upper()


def norm_fill(s: Any) -> str:
    """填空题答案归一：小写、去首尾标点、压缩空白。"""
    t = nfkc(str(s if s is not None else "")).strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t，。．.,;；:：!！?？\"“”'‘’（）()、")


def norm_stem(s: Any) -> str:
    """题干归一（判重）：去图片/HTML 标签与全部空白。"""
    t = str(s if s is not None else "")
    t = MD_IMG_RE.sub(" ", t)
    t = re.sub(r"<[^>]{0,80}>", " ", t)
    t = re.sub(r"\s+", "", t)
    return t


def opt_ids(q: dict) -> list[str]:
    out = []
    for o in (q.get("options") or []):
        if isinstance(o, dict):
            i = o.get("id")
            if i is not None and str(i).strip():
                out.append(norm_letters(i))
    return out


def is_choice(q: dict) -> bool:
    return bool(opt_ids(q))


def qtype(q: dict) -> str:
    return str(q.get("questionType") or "").strip().lower()


def is_open(q: dict) -> bool:
    return (not is_choice(q)) and qtype(q) in OPEN_TYPES


def answer_alts(ans: Any) -> list[str]:
    """答案的候选写法（`##` 分隔的等价写法）。"""
    raw = str(ans if ans is not None else "")
    parts = [p for p in re.split(r"##", raw)]
    return [p for p in parts if p.strip()] or ([raw] if raw.strip() else [])


def truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


# ──────────────────────────────────────────────────────────────────
# 链路（学科 → 产物路径）
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Layout:
    subject: str
    kind: str            # json | image

    def paper_files(self, type_name: str) -> list[Path]:
        s = self.subject
        if self.kind == "image":
            d = OUTPUT_IMAGE_QUESTIONS / type_name / s
            return sorted(d.glob("*/paper.json")) if d.exists() else []
        if s == "英语":
            d = OUTPUT_JSON / type_name / "英语"
        elif s == "语文":
            d = OUTPUT_JSON_CHINESE / type_name
        elif s == "政治":
            d = OUTPUT_JSON_POLITICS / type_name
        else:
            d = OUTPUT_JSON_MATH / type_name / s
        return sorted(d.glob("*.json")) if d.exists() else []

    def all_types(self) -> list[str]:
        return [t for t in ALL_TYPES if self.paper_files(t)]


def layouts_for(subject: str, image: bool) -> list[Layout]:
    if subject == "化学" and image:
        return [Layout(subject, "image")]
    return [Layout(subject, "json")]


# ──────────────────────────────────────────────────────────────────
# 归一模型
# ──────────────────────────────────────────────────────────────────

@dataclass
class Unit:
    idx: int
    raw: dict
    title: str
    category: str
    qtype: str
    score: float | None
    text: str            # content + article + intro（题干所在题面）
    questions: list[dict]

    @property
    def label(self) -> str:
        return self.title or self.category or f"题组{self.idx + 1}"


@dataclass
class Paper:
    subject: str
    type_name: str
    kind: str
    path: Path
    stem: str
    data: dict
    units: list[Unit]

    @property
    def paper_meta(self) -> dict:
        return self.data.get("paper") or {}

    @property
    def title(self) -> str:
        return str(self.paper_meta.get("title") or
                   (self.data.get("title") if self.kind == "image" else "") or self.stem)

    @property
    def total_score(self) -> float | None:
        return norm_num(self.paper_meta.get("totalScore"))

    @property
    def questions(self) -> list[tuple[Unit, dict]]:
        return [(u, q) for u in self.units for q in u.questions]

    @property
    def tag(self) -> str:
        return f"{self.type_name}/{self.stem}"

    def review_path(self) -> Path:
        if self.kind == "image":
            return self.path.parent / f"{self.stem}.review.txt"
        return self.path.with_suffix(".review.txt")


def _unit_text(raw: dict) -> str:
    parts = []
    for k in ("article", "content", "intro"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return "\n".join(parts)


def load_paper(subject: str, type_name: str, path: Path) -> Paper:
    data = json.loads(path.read_text(encoding="utf-8"))
    if path.name == "paper.json":                        # 化学图片版
        qs = data.get("questions") or []
        raws = [{"title": "图片版选择题", "category": "选择",
                 "questionType": "image-choice",
                 "score": sum(norm_num(q.get("score")) or 0 for q in qs) or None,
                 "questions": qs}]
        stem = path.parent.name
        kind = "image"
    else:
        raws = data.get("groups") or data.get("sections") or []
        stem = path.stem
        kind = "json"
    units = []
    for i, raw in enumerate(raws):
        qs = raw.get("questions") or []
        units.append(Unit(
            idx=i, raw=raw,
            title=str(raw.get("title") or "").strip(),
            category=str(raw.get("category") or "").strip(),
            qtype=str(raw.get("questionType") or "").strip().lower(),
            score=norm_num(raw.get("score")),
            text=_unit_text(raw),
            questions=[q for q in qs if isinstance(q, dict)],
        ))
    return Paper(subject=subject, type_name=type_name, kind=kind,
                 path=path, stem=stem, data=data, units=units)


def collect_papers(layout: Layout, types: list[str], limit: int = 0,
                   only: list[str] | None = None) -> list[Paper]:
    out = []
    for t in types:
        for f in layout.paper_files(t):
            stem = f.parent.name if layout.kind == "image" else f.stem
            if only and not any(k in stem for k in only):
                continue
            try:
                out.append(load_paper(layout.subject, t, f))
            except Exception as e:                        # noqa: BLE001
                print(f"  ✗ 读不了 {f}：{str(e)[:100]}", file=sys.stderr)
            if limit and len(out) >= limit:
                return out
    return out


# ──────────────────────────────────────────────────────────────────
# 图片引用解析
# ──────────────────────────────────────────────────────────────────

@dataclass
class ImgProbe:
    ref: str
    ok: bool
    path: Path | None = None
    alt_ref: str | None = None      # 找到同名替身时的建议引用
    where: str = ""
    note: str = ""


def _splits(d: str) -> list[str]:
    """同一份卷在 output_docx_to_md 下可能的目录名变体（教师版/含答案…）。"""
    cands = [d]
    for suf in ("（教师版）", "(教师版)", "（学生版）", "含答案", "（含答案）"):
        cands.append(d + suf)
        if d.endswith(suf):
            cands.append(d[: -len(suf)])
    cands.append(d.replace("（教师版）", "").replace("(教师版)", ""))
    return list(dict.fromkeys(c for c in cands if c))


def _json_rel_bases(paper: Paper) -> list[Path]:
    """相对引用 `images/x.png` 的解析基目录（按优先级）。"""
    bases = [paper.path.parent]                       # JSON 同级
    md = OUTPUT_DOCX_TO_MD / paper.type_name / paper.subject
    for d in _splits(paper.stem):
        bases.append(md / d)
    if paper.subject in ("数学", "物理", "化学"):
        bases.append(OUTPUT_JSON_MATH)                # `images/<卷>_qN.png` 汇总目录
    return bases


def _exam_to_path(rest: str) -> Path:
    return OUTPUT_DOCX_TO_MD / rest


def _with_ext(path: Path, ext: str) -> Path:
    return Path(path).with_suffix(ext)


def _split_query(ref: str) -> tuple[str, str]:
    raw = ref.split("#")[0]
    i = raw.find("?")
    if i < 0:
        return ref.split("#")[0], ""
    return raw[:i], ref[i:]


def _swap_ext_ref(ref: str, ext: str) -> str:
    """同目录换扩展名（保留目录与 ?v= 查询串）。"""
    raw, query = _split_query(ref)
    return str(Path(raw).with_suffix(ext)) + query


def web_sibling(path: Path) -> Path | None:
    """同名、nginx 放行扩展名的替身文件。"""
    for ext in WEB_EXTS:
        q = _with_ext(path, ext)
        if q != path and q.exists():
            return q
    return None


def _dir_variant_rests(rest: str) -> list[str]:
    """/exam-images 之后的部分：把卷目录名换成 教师版/含答案 等变体。"""
    parts = Path(rest).parts
    out = []
    if len(parts) >= 2:
        idx = 2 if len(parts) > 2 else 1
        for d in _splits(parts[idx]):
            if d == parts[idx]:
                continue
            out.append("/".join(list(parts[:idx]) + [d] + list(parts[idx + 1:])))
    return out


def probe_image(paper: Paper, ref: str, where: str = "") -> ImgProbe:
    raw, query = _split_query(ref)
    if raw.startswith(IMG_EXAM_PREFIX):
        rest = raw[len(IMG_EXAM_PREFIX):]
        p = _exam_to_path(rest)
        if p.exists():
            return ImgProbe(ref, True, p, None, where)
        # ① 同目录换扩展名
        sib = web_sibling(p)
        if sib is not None:
            alt = IMG_EXAM_PREFIX + str(sib.relative_to(OUTPUT_DOCX_TO_MD)) + query
            return ImgProbe(ref, False, None, alt, where, f"同目录有 {sib.suffix} 替身")
        # ② 卷目录名变体（教师版 / 含答案）
        for new_rest in _dir_variant_rests(rest):
            q = _exam_to_path(new_rest)
            if q.exists():
                return ImgProbe(ref, False, None, IMG_EXAM_PREFIX + new_rest + query,
                                where, "卷目录名变体命中")
        for new_rest in _dir_variant_rests(rest):
            sib = web_sibling(_exam_to_path(new_rest))
            if sib is not None:
                alt = (IMG_EXAM_PREFIX + str(sib.relative_to(OUTPUT_DOCX_TO_MD)) + query)
                return ImgProbe(ref, False, None, alt, where,
                                "卷目录名变体 + 扩展名替身命中")
        return ImgProbe(ref, False, None, None, where, "文件不存在")
    # 相对引用
    for base in _json_rel_bases(paper):
        p = base / raw
        if p.exists():
            return ImgProbe(ref, True, p, None, where)
        sib = web_sibling(p)
        if sib is not None:
            alt = str(Path(raw).parent / sib.name) if str(Path(raw).parent) != "." else sib.name
            return ImgProbe(ref, False, None, alt + query, where, f"同目录有 {sib.suffix} 替身")
    # 同卷目录名变体（相对引用）
    for base in _json_rel_bases(paper):
        parent = base.name
        for d in _splits(parent):
            p = base.parent / d / raw
            if p.exists():
                alt = f"{d}/{raw}" if str(Path(raw).parent) == "." else \
                    str(Path(raw).parent.parent / d / Path(raw))
                return ImgProbe(ref, False, None, alt + query, where, "卷目录名变体命中")
    # 只按 basename 再找一次（跨目录兜底不自动改引用，只提示）
    for base in _json_rel_bases(paper):
        for ext in WEB_EXTS:
            cand = base / "images" / _with_ext(Path(raw).name, ext).name
            if cand.exists():
                return ImgProbe(ref, False, None, None, where,
                                f"同名图存在于 {cand.parent}（需人工决定）")
    return ImgProbe(ref, False, None, None, where, "文件不存在")


def paper_image_refs(paper: Paper) -> list[tuple[str, str]]:
    """全卷图片引用（去重，保留首个出现位置）。"""
    seen: dict[str, str] = {}

    def add(text: str, where: str):
        for rx in (MD_IMG_RE, TAG_IMG_RE):
            for ref in rx.findall(text or ""):
                if ref in seen:
                    continue
                if not ref.startswith(IMG_EXAM_PREFIX) and "/" not in ref and "." not in ref:
                    continue
                seen[ref] = where

    if paper.kind == "image":
        for q in (paper.data.get("questions") or []):
            add(str(q.get("content") or ""), f"第{q.get('no') or q.get('id')}题")
            add(str(q.get("analysis") or ""), f"第{q.get('no') or q.get('id')}题解析")
    for u in paper.units:
        for k in ("article", "content", "intro"):
            add(str(u.raw.get(k) or ""), f"题组「{u.label}」题面")
        for q in u.questions:
            w = f"题组「{u.label}」第{q.get('id')}题"
            add(str(q.get("content") or ""), w + "题干")
            add(str(q.get("answer") or ""), w + "答案")
            add(str(q.get("analysis") or ""), w + "解析")
            for o in (q.get("options") or []):
                if isinstance(o, dict):
                    add(str(o.get("label") or ""), w + "选项")
    return list(seen.items())


# ──────────────────────────────────────────────────────────────────
# 确定性检查
# ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    code: str
    severity: str        # error | warn | info
    text: str
    paper: str = ""
    unit: str = ""
    qid: Any = None
    patch: dict = field(default_factory=dict)   # 供 --repair 使用的线索

    def as_dict(self) -> dict:
        return {"c": self.code, "s": self.severity, "t": self.text,
                "p": self.paper, "u": self.unit, "q": self.qid}


class Checker:
    def __init__(self, paper: Paper):
        self.paper = paper
        self.out: list[Finding] = []

    def add(self, code: str, severity: str, text: str,
            unit: Unit | None = None, qid: Any = None, **patch):
        self.out.append(Finding(code, severity, text, self.paper.tag,
                                unit.label if unit else "", qid, patch))

    # ── 1/2/8：答案 ──
    def check_answers(self):
        for u in self.paper.units:
            for q in u.questions:
                qid = q.get("id")
                ans = str(q.get("answer") if q.get("answer") is not None else "")
                ids = opt_ids(q)
                img_text = (self.paper.kind == "image" and not ids)
                if not ans.strip():
                    if ids:
                        self.add("E_EMPTY_CHOICE", "error", "选择题答案为空", u, qid)
                    elif img_text:
                        self.add("I_IMG_TEXT_QUESTION", "info",
                                 "图片版非选择题未解析（本链路只做选择题：无文字题干、无分值、无答案）",
                                 u, qid)
                    elif is_open(q):
                        self.add("I_EMPTY_OPEN", "info",
                                 f"开放题无答案（{qtype(q) or '未知题型'}，不参与 AI 兜底）", u, qid)
                    else:
                        self.add("W_EMPTY_FILL", "warn",
                                 f"填空题答案为空（{qtype(q) or '未知题型'}）", u, qid)
                    continue
                if ids:
                    n = norm_letters(ans)
                    if n in ids:
                        if n != ans:
                            why = []
                            if ans != ans.strip():
                                why.append("首尾空白")
                            if re.search(r"[Ａ-Ｚａ-ｚ\u3000]", ans):
                                why.append("全角")
                            if ans != ans.upper() and n == ans.upper():
                                why.append("小写")
                            self.add("W_ANSWER_FORMAT", "warn",
                                     f"答案 {ans!r} 需归一为 {n!r}（{'、'.join(why) or '空白'}）",
                                     u, qid, ans=ans, new=n)
                        continue
                    if len(n) > 1 and all(ch in ids for ch in n):
                        sev = "info" if qtype(q) in ("multiple", "multi") else "warn"
                        self.add("W_ANSWER_MULTI", sev,
                                 f"选择题答案 {ans!r} 为多字母", u, qid)
                        continue
                    parts = [p for p in re.split(r"[／/、,，;；]", n) if p]
                    if len(parts) > 1 and all(p in ids for p in parts):
                        self.add("W_ANSWER_SEPARATOR", "warn",
                                 f"答案 {ans!r} 用分隔符写了多个选项", u, qid)
                        continue
                    self.add("E_ANSWER_NOT_IN_OPTIONS", "error",
                             f"答案 {truncate(ans, 40)!r} 不在选项 {ids} 内", u, qid,
                             ans=ans, ids=ids)

    # ── 3：选项 ──
    def check_options(self):
        for u in self.paper.units:
            for q in u.questions:
                opts = q.get("options") or []
                ids = [norm_letters(o.get("id")) for o in opts if isinstance(o, dict)]
                if not ids:
                    continue
                dup = sorted({i for i in ids if ids.count(i) > 1})
                if dup:
                    self.add("E_DUP_OPTION_ID", "error",
                             f"选项 id 重复：{dup}（全部 {ids}）", u, q.get("id"))
                if len(ids) < 2:
                    self.add("E_FEW_OPTIONS", "error",
                             f"选项只有 {len(ids)} 项：{ids}", u, q.get("id"))
                blank = [o.get("id") for o in opts
                         if isinstance(o, dict) and not str(o.get("label") or "").strip()]
                if blank:
                    self.add("W_EMPTY_OPTION_LABEL", "warn",
                             f"选项 {blank} 的 label 为空", u, q.get("id"))

    # ── 4：分值 ──
    def check_scores(self):
        p = self.paper
        for u in p.units:
            qs = u.questions
            if u.score is None and qs:
                self.add("W_UNIT_SCORE_MISSING", "warn",
                         f"题组「{u.label}」缺 score（小题合计 {fmt(sum(norm_num(q.get('score')) or 0 for q in qs))}）", u)
            for q in qs:
                s = norm_num(q.get("score"))
                if s is None or s <= 0:
                    if self.paper.kind == "image" and not is_choice(q):
                        self.add("I_IMG_TEXT_QUESTION", "info",
                                 "图片版非选择题未做分值（本链路只做选择题）", u, q.get("id"))
                        continue
                    self.add("E_SCORE_LE0", "error",
                             f"分值为 {fmt(s) if s is not None else '缺失'}", u, q.get("id"),
                             unit_score=u.score)
            if u.score is not None and qs:
                ssum = sum(norm_num(q.get("score")) or 0 for q in qs)
                if abs(ssum - u.score) > 1e-6:
                    self.add("W_UNIT_SCORE_MISMATCH", "warn",
                             f"题组分值 {fmt(u.score)} ≠ 小题合计 {fmt(ssum)}", u,
                             unit_score=u.score, q_sum=ssum)
        total = p.total_score
        if total is not None and p.units:
            us = sum(u.score or 0 for u in p.units)
            if abs(us - total) > 1e-6:
                partial = (p.subject in PARTIAL_SUBJECTS and total > us)
                self.add("I_TOTAL_SCORE_PARTIAL" if partial else "W_TOTAL_SCORE_MISMATCH",
                         "info" if partial else "warn",
                         f"paper.totalScore={fmt(total)} ≠ 各题组分值合计 {fmt(us)}")

    # ── 5：图片 ──
    def check_images(self):
        p = self.paper
        for ref, where in paper_image_refs(p):
            pr = probe_image(p, ref, where)
            ctx = f"（{where}）" if where else ""
            if not pr.ok:
                self.add("E_MISSING_IMAGE", "error",
                         f"{ctx}图片不存在：{ref}（{pr.note}）"
                         + (f"｜建议改为 {pr.alt_ref}" if pr.alt_ref else ""),
                         ref=ref, alt=pr.alt_ref)
            if Path(ref.split("?")[0]).suffix.lower() not in WEB_EXTS:
                sib = web_sibling(pr.path) if (pr.ok and pr.path) else None
                tip = (f"｜同目录有 {sib.suffix} 替身：{_swap_ext_ref(ref, sib.suffix)}"
                       if sib is not None else "｜无同名 web 图，需先转 png")
                self.add("W_IMAGE_NONWEB_EXT", "warn",
                         f"{ctx}图片扩展名 {Path(ref.split('?')[0]).suffix} nginx 不放行：{ref}{tip}",
                         ref=ref)
        if p.kind == "image":
            d = p.path.parent
            for q in (p.data.get("questions") or []):
                no = q.get("no") or q.get("id")
                if not (d / f"q{no}.png").exists():
                    self.add("E_IMAGE_PNG_MISSING", "error", f"缺 q{no}.png", qid=no)
                qf = d / f"q{no}.json"
                if not qf.exists():
                    self.add("W_IMAGE_JSON_MISMATCH", "warn", f"缺 q{no}.json", qid=no)
                else:
                    try:
                        single = json.loads(qf.read_text(encoding="utf-8"))
                        keys = ("answer", "score", "type", "image")
                        diff = [k for k in keys if str(single.get(k)) != str(q.get(k))]
                        if diff:
                            self.add("W_IMAGE_JSON_MISMATCH", "warn",
                                     f"q{no}.json 与 paper.json 不一致：{diff}", qid=no)
                    except Exception as e:                 # noqa: BLE001
                        self.add("W_IMAGE_JSON_MISMATCH", "warn",
                                 f"q{no}.json 读取失败：{str(e)[:60]}", qid=no)

    # ── 6：占位符 vs 小题数 ──
    def check_blanks(self):
        for u in self.paper.units:
            qs = u.questions
            if not qs:
                continue
            nb = len(BLANK_RE.findall(u.text))
            if nb and nb != len(qs):
                self.add("W_BLANK_COUNT", "warn",
                         f"占位符 {nb} 个 ≠ 小题 {len(qs)} 个", u)

    # ── 7：重复题干 ──
    def check_dup_stems(self):
        """同卷重复题干：同题组内必报；跨题组还要求题面原文也相同
        （否则 "What can we infer from the passage?" 这类通用问句会误报）。"""
        seen: dict[str, tuple[Unit, Any, str]] = {}
        utext = {u.idx: norm_stem(u.text) for u in self.paper.units}
        for u in self.paper.units:
            for q in u.questions:
                key = norm_stem(q.get("content"))
                if len(key) < 12:
                    continue
                if key in seen:
                    u0, q0, t0 = seen[key]
                    same_unit = u0.idx == u.idx
                    same_text = utext.get(u.idx) and utext.get(u.idx) == t0
                    if same_unit:
                        self.add("W_DUP_STEM", "warn",
                                 f"题干与「{u0.label}」第{q0}题完全相同（同题组）", u, q.get("id"))
                    elif same_text:
                        self.add("W_DUP_STEM", "warn",
                                 f"题干与「{u0.label}」第{q0}题完全相同（题面原文也相同）", u, q.get("id"))
                else:
                    seen[key] = (u, q.get("id"), utext.get(u.idx))

    # ── 8b：answer / analysis 字段串位 ──
    def check_misaligned_fields(self):
        """识别「answer 里装了题干」「analysis 是别的题的题干」这类整卷错位。"""
        stems: dict[str, str] = {}
        for u in self.paper.units:
            for q in u.questions:
                st = norm_stem(q.get("content"))
                if len(st) >= 20:
                    stems.setdefault(st, f"「{u.label}」第{q.get('id')}题")
        for u in self.paper.units:
            for q in u.questions:
                qid = q.get("id")
                st = norm_stem(q.get("content"))
                ans = norm_stem(q.get("answer"))
                an = norm_stem(q.get("analysis"))
                if len(st) >= 20 and ans.startswith(st[:20]):
                    self.add("W_ANSWER_IS_STEM", "warn",
                             "answer 字段装的是题干（答案区被错切）", u, qid)
                if "【答案】" in str(q.get("answer") or "") or \
                        "【解析】" in str(q.get("answer") or ""):
                    self.add("W_ANSWER_EMBEDDED", "warn",
                             "answer 字段里带【答案】/【解析】标记（解析串进了答案）", u, qid)
                if len(an) >= 20:
                    if an == st:
                        self.add("W_ANALYSIS_IS_STEM", "warn",
                                 "analysis 与本题题干完全相同（未真正解析）", u, qid)
                    elif st and an.startswith(st) and len(an) > len(st) + 15:
                        self.add("W_ANALYSIS_IS_STEM", "warn",
                                 "analysis 是「题干+选项」原文（解析区被错切，未真正解析）", u, qid)
                    elif an in stems and stems[an] != f"「{u.label}」第{qid}题":
                        self.add("W_ANALYSIS_IS_STEM", "warn",
                                 f"analysis 是{stems[an]}的题干（题面/解析整体错位）", u, qid)

    # ── 元信息 ──
    def check_meta(self):
        p = self.paper
        if p.kind == "image":
            return
        subj = str(p.paper_meta.get("subject") or "")
        if subj != p.subject:
            self.add("W_PAPER_SUBJECT", "warn", f"paper.subject={subj!r}（应为 {p.subject}）")

    def run(self) -> list[Finding]:
        self.check_answers()
        self.check_options()
        self.check_scores()
        self.check_images()
        self.check_blanks()
        self.check_dup_stems()
        self.check_misaligned_fields()
        self.check_meta()
        return self.out


# ──────────────────────────────────────────────────────────────────
# AI 层：缓存 + --no-ai + 并发
# ──────────────────────────────────────────────────────────────────

SOLVE_PROMPT = """你是北京高中{subject}的阅卷老师。下面每道题**只给题干和选项，没有答案**，请逐题独立作答。

只输出 JSON：
{{"answers": [{{"no": 1, "answer": "C", "reason": "不超过 30 字的理由"}}]}}

要求：
1. no 对应题目前的【第k题】编号（k 从 1 开始，与卷面题号无关）；
2. 选择题 answer 只写选项 id 原样（一个字母）；填空/默写题 answer 写答案内容，等价写法用 ## 分隔；
3. 每道题都必须作答，一道都不能漏；
4. 只输出 JSON，不要解释。

题目：
{items}
"""

AUDIT_PROMPT = """你是北京高中{subject}的教研员，正在做题库入库前的质量抽查。下面每道题都附了「已录入答案」。

请逐题判断：
① 题干、选项、答案是否自洽（答案是否真的回答了题干）？
② 有没有明显的串题（题干与选项/答案不是同一道题）？
③ 有没有明显错别字、学科知识错误、答案与解析矛盾？

只输出 JSON：
{{"issues": [{{"no": 1, "kind": "串题|错别字|学科错误|答案矛盾|其他", "detail": "不超过 40 字"}}]}}

没有问题的题不要出现在 issues 里；全部没问题就输出 {{"issues": []}}。只输出 JSON。

题目：
{items}
"""

KIND_ZH = {
    "empty_answer": "空答案-AI补答",
    "answer_from_analysis": "选项外答案-取自解析",
    "answer_letter_prefix": "选项外答案-取首字母",
    "answer_not_in_options": "选项外答案-AI替换",
    "answer_case": "答案归一",
    "image_alt": "缺图替身改引用",
    "score": "分值兜底",
}


def _model() -> str:
    return os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")


class Gateway:
    """OpenAI 兼容网关封装：缓存命中优先，`--no-ai` 只吃缓存。"""

    def __init__(self, subject: str, enabled: bool = True, force: bool = False):
        self.subject = subject
        self.enabled = enabled
        self.force = force
        self.dir = CACHE_DIR / subject
        self._lock = threading.Lock()
        self._memo: dict[str, dict] = {}
        self.calls = 0
        self.hits = 0
        self.errors = 0

    def _path(self, kind: str, prompt: str) -> Path:
        sha = hashlib.sha1((kind + "\x00" + prompt).encode("utf-8")).hexdigest()[:20]
        return self.dir / f"{kind}-{sha}.json"

    def call(self, kind: str, prompt: str, max_tokens: int = 4000) -> dict:
        path = self._path(kind, prompt)
        key = path.name
        with self._lock:
            if key in self._memo:
                self.hits += 1
                return self._memo[key]
        if not self.force and path.exists():
            try:
                got = json.loads(path.read_text(encoding="utf-8")).get("response") or {}
                with self._lock:
                    self._memo[key] = got
                    self.hits += 1
                return got
            except Exception:                             # noqa: BLE001
                pass
        if not self.enabled:
            with self._lock:
                self._memo[key] = {}
            return {}
        got = self._ask(prompt, max_tokens)
        with self._lock:
            self._memo[key] = got
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"kind": kind, "model": _model(), "response": got},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
        return got

    def _ask(self, prompt: str, max_tokens: int) -> dict:
        from convert_math_pdf import chat_json
        with self._lock:
            self.calls += 1
        try:
            got = chat_json([{"role": "user", "content": prompt}], _model(), max_tokens)
            return got if isinstance(got, dict) else {}
        except Exception as e:                            # noqa: BLE001
            with self._lock:
                self.errors += 1
            print(f"  ! 模型调用失败：{str(e)[:120]}", file=sys.stderr)
            return {}


def _ask_answers(gw: Gateway, subject: str, items: str, *,
                 reverse: bool = False, max_tokens: int = 4000) -> dict[int, tuple[str, str]]:
    head = ("第二遍独立判读：请重新读题作答，不要沿用任何既有结论。\n\n" if reverse else "")
    prompt = head + SOLVE_PROMPT.format(subject=subject, items=items)
    got = gw.call("solve", prompt, max_tokens)
    out: dict[int, tuple[str, str]] = {}
    for a in (got.get("answers") or []):
        if not isinstance(a, dict):
            continue
        try:
            no = int(re.sub(r"\D", "", str(a.get("no") or "")))
        except Exception:                                 # noqa: BLE001
            continue
        ans = str(a.get("answer") if a.get("answer") is not None else "").strip()
        if no and ans:
            out[no] = (ans, str(a.get("reason") or "").strip()[:120])
    return out


# ──────────────────────────────────────────────────────────────────
# 题面渲染 / 抽样
# ──────────────────────────────────────────────────────────────────

@dataclass
class Pick:
    unit: Unit
    q: dict
    j: int                 # 在题组内的序号（0 基）
    k: int = 0             # 批内编号（1 基）


def blank_context(unit: Unit, j: int, before: int = 420, after: int = 260) -> str:
    """把题面占位符编号后，截取第 j+1 空附近的一段（完形/语法填空/七选五用）。"""
    text = unit.text
    marks = list(BLANK_RE.finditer(text))
    if not marks or len(marks) != len(unit.questions) or j >= len(marks):
        return ""
    parts, last = [], 0
    for i, m in enumerate(marks):
        parts.append(text[last:m.start()])
        parts.append(f"【空{i + 1}】")
        last = m.end()
    parts.append(text[last:])
    marked = "".join(parts)
    pos = marked.find(f"【空{j + 1}】")
    if pos < 0:
        return ""
    lo, hi = max(0, pos - before), min(len(marked), pos + after)
    return ("…" if lo > 0 else "") + marked[lo:hi] + ("…" if hi < len(marked) else "")


def render_pick(p: Pick, with_answer: bool = False, with_analysis: bool = False) -> str:
    q = p.q
    stem = str(q.get("content") or "").strip()
    if not stem:
        stem = blank_context(p.unit, p.j) or truncate(p.unit.text, 900)
    stem = truncate(stem, 1200)
    lines = [f"【第{p.k}题】{stem}"]
    opts = q.get("options") or p.unit.raw.get("options") or []
    if opts:
        lines.append("\n".join(f"{o.get('id')}．{str(o.get('label') or '').strip()}"
                               for o in opts if isinstance(o, dict)))
    if with_answer:
        lines.append(f"（已录入答案：{str(q.get('answer') or '').strip() or '空'}）")
    if with_analysis:
        an = str(q.get("analysis") or "").strip()
        if an:
            lines.append(f"（已录入解析：{truncate(an, 200)}）")
    return "\n".join(lines)


def compare_able(q: dict) -> bool:
    ans = str(q.get("answer") if q.get("answer") is not None else "")
    if not ans.strip():
        return False
    ids = opt_ids(q)
    if ids:
        n = norm_letters(ans)
        return n in ids or (len(n) > 1 and all(c in ids for c in n))
    if qtype(q) in OPEN_TYPES:
        return False
    return len(ans) <= 200


def pick_items(paper: Paper, n: int) -> list[Pick]:
    """抽样：每卷最多 n 题。

    先让「可判题最多」的题组贡献连续前 5 题（这样 identity vs ±1/±2 平移诊断才有样本），
    剩下的名额再按题组轮转各取 1 题，保证覆盖度；最终按卷面顺序编号。
    """
    pools = [[(u, q, j) for j, q in enumerate(u.questions) if compare_able(q)]
             for u in paper.units]
    picks: list[Pick] = []
    order = sorted(range(len(pools)), key=lambda i: (-len(pools[i]), i))
    if order and pools[order[0]]:
        i0 = order[0]
        take = pools[i0][: min(5, n, len(pools[i0]))]
        picks.extend(Pick(u, q, j) for u, q, j in take)
        pools[i0] = pools[i0][len(take):]
    while len(picks) < n and any(pools):
        progressed = False
        for pool in pools:
            if pool and len(picks) < n:
                u, q, j = pool.pop(0)
                picks.append(Pick(u, q, j))
                progressed = True
        if not progressed:
            break
    picks.sort(key=lambda p: (p.unit.idx, p.j))
    for k, p in enumerate(picks, 1):
        p.k = k
    return picks


def judge(p: Pick, model_ans: str) -> str:
    """same / diff / noans"""
    ids = opt_ids(p.q)
    key = str(p.q.get("answer") if p.q.get("answer") is not None else "")
    if ids:
        m = next((ch for ch in norm_letters(model_ans) if ch in ids), None)
        if not m:
            return "noans"
        return "same" if m == norm_letters(key) else "diff"
    ma = {norm_fill(x) for x in answer_alts(model_ans)}
    kb = {norm_fill(x) for x in answer_alts(key)}
    ma.discard("")
    if not ma:
        return "noans"
    return "same" if (ma & kb) else "diff"


def _model_letter(model_ans: str, ids: list[str]) -> str:
    return next((ch for ch in norm_letters(model_ans) if ch in ids), "")


def shift_diag(picks: list[Pick], models: dict[int, str]) -> dict | None:
    """在「同一题组的连续抽样」上比 identity 与 ±1/±2 平移的吻合率。"""
    groups: dict[int, list[Pick]] = {}
    for p in picks:
        if opt_ids(p.q):
            groups.setdefault(p.unit.idx, []).append(p)
    units = []
    for idx, ps in sorted(groups.items()):
        keys, mods = [], []
        for p in ps:
            ids = opt_ids(p.q)
            keys.append(norm_letters(p.q.get("answer"))[:1])
            mods.append(_model_letter(models.get(p.k, ""), ids))
        if len(keys) < 3 or any(not k for k in keys):
            continue
        if not any(mods):
            continue
        rates = {}
        for s in (-2, -1, 0, 1, 2):
            pairs = [(keys[i], mods[i + s]) for i in range(len(keys))
                     if 0 <= i + s < len(mods)]
            pairs = [(a, b) for a, b in pairs if a and b]
            rates[s] = (sum(1 for a, b in pairs if a == b) / len(pairs)) if pairs else None
        if rates.get(0) is None:
            continue
        cand = {s: r for s, r in rates.items() if r is not None}
        best = max(cand, key=lambda s: (cand[s], -abs(s)))
        units.append({
            "unit": ps[0].unit.label,
            "n": len(keys),
            "identity": round(cand[0], 3),
            "best_shift": best,
            "best_rate": round(cand[best], 3),
            "rates": {str(s): (round(r, 3) if r is not None else None) for s, r in rates.items()},
        })
    if not units:
        return None
    dist: dict[str, int] = {}
    for u in units:
        dist[str(u["best_shift"])] = dist.get(str(u["best_shift"]), 0) + 1
    susp = [u for u in units if u["best_shift"] != 0
            and u["best_rate"] - u["identity"] >= 0.15 and u["n"] >= 5]
    return {"units": units, "best_shift_count": dict(sorted(dist.items())), "suspects": susp}


# ──────────────────────────────────────────────────────────────────
# cross-check / ai-audit
# ──────────────────────────────────────────────────────────────────

def sample_papers(papers: list[Paper], n: int) -> list[Paper]:
    by: dict[str, list[Paper]] = {}
    for p in papers:
        by.setdefault(p.type_name, []).append(p)
    keys = sorted(by)
    out: list[Paper] = []
    while len(out) < n and any(by[k] for k in keys):
        for k in keys:
            if by[k] and len(out) < n:
                out.append(by[k].pop(0))
    return out


def cross_check_paper(gw: Gateway, paper: Paper, n: int) -> dict:
    picks = pick_items(paper, n)
    if not picks:
        return {"paper": paper.tag, "title": paper.title, "n": 0, "same": 0, "diff": 0,
                "noans": 0, "choice_n": 0, "choice_same": 0, "rows": [], "shift": None}
    items = "\n\n".join(render_pick(p) for p in picks)
    got = _ask_answers(gw, paper.subject, items, max_tokens=4000)
    models = {k: v[0] for k, v in got.items()}
    rows, same, diff, noans = [], 0, 0, 0
    choice_n = choice_same = 0
    for p in picks:
        model = models.get(p.k, "")
        verdict = judge(p, model) if model else "noans"
        ids = opt_ids(p.q)
        if ids:
            choice_n += 1
            if verdict == "same":
                choice_same += 1
        if verdict == "same":
            same += 1
        elif verdict == "diff":
            diff += 1
        else:
            noans += 1
        if verdict != "same":
            rows.append({"unit": p.unit.label, "qid": p.q.get("id"),
                         "key": str(p.q.get("answer") or ""),
                         "model": truncate(model, 60) or "（模型未作答）",
                         "verdict": verdict,
                         "reason": truncate(got.get(p.k, ("", ""))[1], 60)})
    return {"paper": paper.tag, "title": paper.title, "n": len(picks), "same": same,
            "diff": diff, "noans": noans, "choice_n": choice_n,
            "choice_same": choice_same, "rows": rows,
            "shift": shift_diag(picks, models)}


def _run_papers(jobs, workers: int, fn) -> list:
    if workers <= 1 or len(jobs) <= 1:
        return [fn(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, jobs))


def do_cross_check(papers: list[Paper], n: int, npapers: int, gw: Gateway,
                   workers: int, cmd: str) -> dict:
    sample = sample_papers(list(papers), npapers)
    res = _run_papers(sample, workers, lambda p: cross_check_paper(gw, p, n))
    per = [r for r in res]
    tot = {k: sum(r[k] for r in per) for k in ("n", "same", "diff", "noans",
                                               "choice_n", "choice_same")}
    rate = (tot["same"] / tot["n"]) if tot["n"] else None
    crate = (tot["choice_same"] / tot["choice_n"]) if tot["choice_n"] else None
    shifts = [r["shift"] for r in per if r["shift"]]
    dist: dict[str, int] = {}
    for s in shifts:
        for k, v in s["best_shift_count"].items():
            dist[k] = dist.get(k, 0) + v
    suspects = [{"paper": r["paper"], **u}
                for r, s in zip(per, [x["shift"] for x in per]) if s
                for u in s["suspects"]]
    return {
        "cmd": cmd, "n_per_paper": n, "papers_sampled": len(per), **tot,
        "rate": round(rate, 4) if rate is not None else None,
        "choice_rate": round(crate, 4) if crate is not None else None,
        "papers": [{"paper": r["paper"], "title": r["title"], "n": r["n"],
                    "same": r["same"], "diff": r["diff"], "noans": r["noans"],
                    "rate": round(r["same"] / r["n"], 3) if r["n"] else None}
                   for r in per],
        "rows": [{"paper": r["paper"], **row} for r in per for row in r["rows"]],
        "shift_units": [{"paper": r["paper"], **u} for r in per
                        for u in (r["shift"]["units"] if r["shift"] else [])],
        "shift_best_count": dict(sorted(dist.items())),
        "shift_suspects": suspects,
        "no_ai": not gw.enabled,
    }


def audit_paper(gw: Gateway, paper: Paper, n: int) -> dict:
    picks = pick_items(paper, n)
    if not picks:
        return {"paper": paper.tag, "title": paper.title, "n": 0, "rows": []}
    items = "\n\n".join(render_pick(p, with_answer=True, with_analysis=True) for p in picks)
    prompt = AUDIT_PROMPT.format(subject=paper.subject, items=items)
    got = gw.call("audit", prompt, 4000)
    rows = []
    for it in (got.get("issues") or []):
        if not isinstance(it, dict):
            continue
        try:
            no = int(re.sub(r"\D", "", str(it.get("no") or "")))
        except Exception:                                 # noqa: BLE001
            continue
        pk = next((p for p in picks if p.k == no), None)
        if pk is None:
            continue
        rows.append({"paper": paper.tag, "unit": pk.unit.label, "qid": pk.q.get("id"),
                     "kind": str(it.get("kind") or "其他")[:12],
                     "detail": truncate(str(it.get("detail") or ""), 80),
                     "answer": str(pk.q.get("answer") or "")})
    return {"paper": paper.tag, "title": paper.title, "n": len(picks), "rows": rows}


def do_ai_audit(papers: list[Paper], n: int, npapers: int, gw: Gateway,
                workers: int, cmd: str) -> dict:
    sample = sample_papers(list(papers), npapers)
    res = _run_papers(sample, workers, lambda p: audit_paper(gw, p, n))
    rows = [r for x in res for r in x["rows"]]
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {"cmd": cmd, "n_per_paper": n, "papers_sampled": len(res),
            "questions": sum(x["n"] for x in res),
            "issues": len(rows), "kinds": dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
            "papers": [{"paper": x["paper"], "title": x["title"], "n": x["n"],
                        "issues": len(x["rows"])} for x in res],
            "rows": rows, "no_ai": not gw.enabled}


# ──────────────────────────────────────────────────────────────────
# 兜底修复（默认 dry-run）
# ──────────────────────────────────────────────────────────────────

AI_MARK = "AI 补答案（原卷缺失）"
AI_MARK_FIX = "AI 修正答案（原卷答案不在选项内）"
MOVE_MARK = "原答案字段余下内容（qa 迁移）"


@dataclass
class Repair:
    kind: str
    paper: str
    unit: str
    qid: Any
    field: str
    old: Any
    new: Any
    note: str
    node: dict | None = field(default=None, repr=False, compare=False)

    def line(self) -> str:
        where = f"「{self.unit}」" if self.unit else ""
        if self.qid not in (None, ""):
            where += f"第{self.qid}题"
        where = where or "全卷"
        field = "" if self.field == "image" else f" {self.field}"
        return (f"{REVIEW_PREFIX}{KIND_ZH.get(self.kind, self.kind)}{where}{field}："
                f"{fmt(self.old)!r} → {fmt(self.new)!r}（{self.note}）")

    def as_dict(self) -> dict:
        return {"kind": self.kind, "paper": self.paper, "unit": self.unit,
                "qid": self.qid, "field": self.field, "old": fmt(self.old),
                "new": fmt(self.new), "note": self.note}


def plan_case_repairs(paper: Paper) -> list[Repair]:
    out = []
    for u in paper.units:
        for q in u.questions:
            ids = opt_ids(q)
            ans = str(q.get("answer") if q.get("answer") is not None else "")
            if not ids or not ans.strip():
                continue
            n = norm_letters(ans)
            if n == ans or len(n) != 1 or n not in ids:
                continue
            out.append(Repair("answer_case", paper.tag, u.label, q.get("id"), "answer",
                              ans, n, "大小写/全角/空白归一", q))
    return out


def plan_image_repairs(paper: Paper) -> list[Repair]:
    """图片缺失 / 扩展名不放行：同目录有 web 同名替身就改引用，否则原样保留。"""
    out = []
    for ref, where in paper_image_refs(paper):
        pr = probe_image(paper, ref, where)
        if not pr.ok:
            if pr.alt_ref:
                out.append(Repair("image_alt", paper.tag, where or "全卷", "", "image",
                                  ref, pr.alt_ref, pr.note or "同名替身"))
            continue
        if Path(ref.split("?")[0]).suffix.lower() in WEB_EXTS:
            continue
        sib = web_sibling(pr.path) if pr.path else None
        if sib is not None:
            out.append(Repair("image_alt", paper.tag, where or "全卷", "", "image",
                              ref, _swap_ext_ref(ref, sib.suffix), f"同目录有 {sib.suffix} 替身"))
    return out


def plan_score_repairs(paper: Paper) -> list[Repair]:
    out = []
    for u in paper.units:
        qs = u.questions
        if not qs:
            continue
        zeros = [q for q in qs if (norm_num(q.get("score")) or 0) <= 0]
        if not zeros or u.score is None or u.score <= 0:
            continue
        fixed = sum(norm_num(q.get("score")) or 0 for q in qs if q not in zeros)
        rest = u.score - fixed
        if rest <= 0:
            continue
        share = round(rest / len(zeros), 2)
        if share <= 0:
            continue
        acc = 0.0
        for i, q in enumerate(zeros):
            val = share if i < len(zeros) - 1 else round(rest - acc, 2)
            acc = round(acc + val, 2)
            out.append(Repair("score", paper.tag, u.label, q.get("id"), "score",
                              q.get("score"), val,
                              f"题组分值 {fmt(u.score)}，{len(qs)} 小题，{len(zeros)} 道 0 分按均分兜底",
                              q))
    return out


def _valid_new_answer(q: dict, cand: str) -> str | None:
    ids = opt_ids(q)
    if ids:
        n = norm_letters(cand)
        return n if len(n) == 1 and n in ids else None
    t = str(cand or "").strip()
    if not t or len(t) > 80 or re.search(r"[。！？!?；;]", t):
        return None
    stem = norm_stem(q.get("content"))
    if stem and norm_stem(t) == stem:
        return None
    return t


ANS_SAID = re.compile(r"(?:故选|正确答案为|正确答案是|答案\s*[:：])\s*([A-H])")


def _analysis_letter(q: dict) -> str:
    """解析里明确写出的选项字母（用于给 AI 兜底结果做交叉标记，不改判）。"""
    m = ANS_SAID.search(str(q.get("analysis") or ""))
    return m.group(1) if m else ""


LEAD_LETTER = re.compile(r"^\s*[【\[（(]?\s*([A-Ha-hＡ-Ｈａ-ｈ])\s*[】\]）)]?\s*[.．、,，:：]?\s*(.*)$", re.S)


def plan_letter_prefix_repairs(paper: Paper) -> list[Repair]:
    """答案不在选项内、但以选项字母开头（如 `A(A 项，…)` / `B(3分)【解析…`）→ 取该字母。
    余下文字迁到 analysis，信息不丢；比让模型重答更保真。"""
    out = []
    for u in paper.units:
        for q in u.questions:
            ids = opt_ids(q)
            if not ids:
                continue
            ans = str(q.get("answer") if q.get("answer") is not None else "")
            if not ans.strip() or norm_letters(ans) in ids:
                continue
            # 纯字母多答案（如 AD / ABC）是「多选」，不能按「取首字母」拆——否则会被截断成单选
            if re.fullmatch(r"[A-Ha-hＡ-Ｈａ-ｈ]{2,}", ans.strip()):
                continue
            m = LEAD_LETTER.match(ans)
            if not m:
                continue
            letter = norm_letters(m.group(1))
            rest = (m.group(2) or "").strip()
            if letter not in ids:
                continue
            if norm_letters(ans) == letter:      # 本来就是干净字母，交给 case 归一
                continue
            note = f"原答案以 {letter} 开头，取该字母"
            if rest:
                note += f"；余下 {len(rest)} 字迁到 analysis"
            out.append(Repair("answer_letter_prefix", paper.tag, u.label, q.get("id"),
                              "answer", ans, letter, note, q))
    return out


def plan_analysis_answer_repairs(paper: Paper) -> list[Repair]:
    """选项外/空答案时，若「解析原文」明确写了 故选X / 正确答案为X 且 X 在选项内，先用解析原文。

    解析来自教师版原卷，依据比模型硬；解析与模型冲突时不再用模型覆盖（只报告）。
    """
    out = []
    for u in paper.units:
        for q in u.questions:
            ids = opt_ids(q)
            if not ids:
                continue
            ans = str(q.get("answer") if q.get("answer") is not None else "")
            if norm_letters(ans) in ids:
                continue
            said = _analysis_letter(q)
            if said and said in ids:
                out.append(Repair("answer_from_analysis", paper.tag, u.label, q.get("id"),
                                  "answer", ans, said, f"解析原文写明 {said}（教师版依据）", q))
    return out


def _same_answer(a: str, b: str) -> bool:
    return norm_fill(a) == norm_fill(b) and bool(norm_fill(a))


def plan_ai_answer_repairs(paper: Paper, gw: Gateway,
                           covered: set[int] | None = None) -> tuple[list[Repair], list[str]]:
    """空答案 / 选项外答案：模型对**每道题**独立作答两次，两次一致且格式合法才补。

    刻意逐题（而不是 10 题一批）调用：批量会让缓存键依赖同批的其它题，
    修完一批后重跑会重新分批、重新采样，结论不稳定（不幂等）；逐题的缓存键
    只由「这道题的题面」决定，所以 `--no-ai` 复跑必然逐字节一致。
    """
    covered = covered or set()
    cands: list[tuple[Unit, dict, str]] = []
    for u in paper.units:
        for q in u.questions:
            if id(q) in covered:
                continue
            ids = opt_ids(q)
            ans = str(q.get("answer") if q.get("answer") is not None else "")
            if not ans.strip():
                if ids or (qtype(q) in FILL_TYPES and not is_open(q)):
                    cands.append((u, q, "empty_answer"))
                continue
            if not ids:
                continue
            n = norm_letters(ans)
            if n in ids:
                continue
            if len(n) > 1 and all(c in ids for c in n):
                continue
            parts = [p for p in re.split(r"[／/、,，;；]", n) if p]
            if len(parts) > 1 and all(p in ids for p in parts):
                continue
            cands.append((u, q, "answer_not_in_options"))
    if not cands:
        return [], []
    out: list[Repair] = []
    skipped: list[str] = []
    for u, q, kind in cands:
        p = Pick(u, q, u.questions.index(q), 1)
        item = render_pick(p)
        a1 = _ask_answers(gw, paper.subject, item, max_tokens=1500).get(1)
        a2 = _ask_answers(gw, paper.subject, item, reverse=True, max_tokens=1500).get(1)
        if not a1 or not a2:
            skipped.append(f"{u.label} 第{q.get('id')}题：模型未作答")
            continue
        v1 = _valid_new_answer(q, a1[0])
        v2 = _valid_new_answer(q, a2[0])
        if not v1 or not v2 or not _same_answer(v1, v2):
            skipped.append(f"{u.label} 第{q.get('id')}题：两次不一致或格式不合法"
                           f"（{truncate(a1[0], 16)} / {truncate(a2[0], 16)}）")
            continue
        old = str(q.get("answer") or "")
        note = f"模型两次一致作答 {v1}；理由：{truncate(a1[1] or a2[1] or '—', 50)}"
        said = _analysis_letter(q)
        if said and said != v1:
            note += f"；⚠解析里写的字母是 {said}（需人工抽查）"
        out.append(Repair(kind, paper.tag, u.label, q.get("id"), "answer", old, v1, note, q))
    return out, skipped


def _append_analysis(q: dict, note: str, kind: str = "empty_answer") -> None:
    mark = AI_MARK if kind == "empty_answer" else AI_MARK_FIX
    cur = str(q.get("analysis") or "").strip()
    if mark in cur:
        return
    add = f"{mark}：{note}"
    q["analysis"] = f"{cur}\n\n{add}" if cur else add


def _deep_replace(obj: Any, old: str, new: str) -> int:
    n = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                if old in v:
                    obj[k] = v.replace(old, new)
                    n += 1
            else:
                n += _deep_replace(v, old, new)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                if old in v:
                    obj[i] = v.replace(old, new)
                    n += 1
            else:
                n += _deep_replace(v, old, new)
    return n


def write_review(paper: Paper, repairs: list[Repair]) -> Path:
    path = paper.review_path()
    old = []
    if path.exists():
        old = [ln for ln in path.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith(REVIEW_PREFIX)]
    lines = old + [r.line() for r in repairs]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def apply_repairs(paper: Paper, repairs: list[Repair], apply: bool,
                  backup_root: Path | None = None) -> int:
    if not repairs:
        return 0
    for r in repairs:
        if r.kind == "image_alt":
            _deep_replace(paper.data, str(r.old), str(r.new))
            continue
        if r.node is None:
            continue
        r.node[r.field] = r.new
        if r.kind in ("empty_answer", "answer_not_in_options"):
            _append_analysis(r.node, r.note, r.kind)
        elif r.kind == "answer_letter_prefix":
            m = LEAD_LETTER.match(str(r.old))
            rest = (m.group(2) or "").strip() if m else ""
            cur = str(r.node.get("analysis") or "").strip()
            if rest and MOVE_MARK not in cur:
                add = f"{MOVE_MARK}：{rest}"
                r.node["analysis"] = f"{cur}\n\n{add}" if cur else add
    if apply:
        if backup_root is not None:
            try:
                dst = backup_root / paper.path.relative_to(DATA_ROOT)
            except ValueError:
                dst = backup_root / paper.path.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paper.path, dst)
        write_review(paper, repairs)
        paper.path.write_text(json.dumps(paper.data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return len(repairs)


def do_repair(papers: list[Paper], gw: Gateway, apply: bool, workers: int,
              cmd: str) -> dict:
    backup_root = (DATA_ROOT / f"_backup_qa_repair_{time.strftime('%Y%m%d-%H%M%S')}"
                   if apply else None)

    def per_paper(paper: Paper) -> dict:
        reps = plan_case_repairs(paper)
        reps += plan_letter_prefix_repairs(paper)              # ① 字母前缀最保真
        reps += plan_analysis_answer_repairs(paper)            # ② 解析原文次之
        reps += plan_image_repairs(paper)
        reps += plan_score_repairs(paper)
        covered = {id(r.node) for r in reps
                   if r.kind in ("answer_from_analysis", "answer_letter_prefix",
                                 "answer_case")}
        ai_reps, skipped = plan_ai_answer_repairs(paper, gw, covered)   # ② --no-ai 时只吃缓存
        reps += ai_reps
        n = apply_repairs(paper, reps, apply, backup_root)
        return {"paper": paper.tag, "title": paper.title, "n": n,
                "items": [r.as_dict() for r in reps],
                "skipped": skipped, "review": str(paper.review_path())}

    res = _run_papers(papers, workers, per_paper)
    counts: dict[str, int] = {}
    for r in res:
        for it in r["items"]:
            counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    conflicts = sum(1 for r in res for it in r["items"] if "⚠解析里写的字母" in it["note"])
    return {"cmd": cmd, "apply": apply,
            "backup": str(backup_root) if backup_root and backup_root.exists() else "",
            "papers": [r["paper"] for r in res],
            "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "total": sum(counts.values()), "conflicts": conflicts,
            "items": [{"paper": r["paper"], **it} for r in res for it in r["items"]],
            "skipped": [{"paper": r["paper"], "text": s} for r in res for s in r["skipped"]],
            "per_paper": [{"paper": r["paper"], "title": r["title"], "n": r["n"]} for r in res]}


# ──────────────────────────────────────────────────────────────────
# 报告渲染
# ──────────────────────────────────────────────────────────────────

CODE_ZH = {
    "E_EMPTY_CHOICE": "选择题空答案",
    "W_EMPTY_FILL": "填空空答案",
    "I_EMPTY_OPEN": "开放题无答案",
    "E_ANSWER_NOT_IN_OPTIONS": "答案不在选项内",
    "W_ANSWER_MULTI": "多字母答案",
    "W_ANSWER_SEPARATOR": "答案带分隔符",
    "W_ANSWER_FORMAT": "答案大小写/全角/空白",
    "E_DUP_OPTION_ID": "选项 id 重复",
    "E_FEW_OPTIONS": "选项不足 2 项",
    "W_EMPTY_OPTION_LABEL": "选项 label 为空",
    "E_SCORE_LE0": "分值为 0/缺失",
    "W_UNIT_SCORE_MISMATCH": "题组分值 ≠ 小题合计",
    "W_UNIT_SCORE_MISSING": "题组缺 score",
    "W_TOTAL_SCORE_MISMATCH": "totalScore ≠ 合计",
    "I_TOTAL_SCORE_PARTIAL": "totalScore 差额（只收选择/填空）",
    "E_MISSING_IMAGE": "图片缺失",
    "W_IMAGE_NONWEB_EXT": "图片扩展名不放行",
    "W_BLANK_COUNT": "占位符数 ≠ 小题数",
    "W_DUP_STEM": "同卷重复题干",
    "W_PAPER_SUBJECT": "paper.subject 不符",
    "E_IMAGE_PNG_MISSING": "图片版缺 png",
    "W_IMAGE_JSON_MISMATCH": "图片版 json 不一致",
    "I_IMG_TEXT_QUESTION": "图片版非选择题（未做）",
    "W_ANSWER_IS_STEM": "answer 是题干",
    "W_ANSWER_EMBEDDED": "answer 里串入【答案】/【解析】",
    "W_ANALYSIS_IS_STEM": "解析与题干/别题相同",
}
AUTO_FIX = {"W_ANSWER_FORMAT", "E_SCORE_LE0", "E_MISSING_IMAGE",
            "answer_from_analysis", "answer_letter_prefix", "answer_not_in_options",
            "E_EMPTY_CHOICE", "W_EMPTY_FILL", "E_ANSWER_NOT_IN_OPTIONS"}
MANUAL_ONLY = {"E_DUP_OPTION_ID", "E_FEW_OPTIONS", "W_EMPTY_OPTION_LABEL",
               "W_ANSWER_IS_STEM", "W_ANSWER_EMBEDDED", "W_ANALYSIS_IS_STEM",
               "W_ANSWER_MULTI", "W_ANSWER_SEPARATOR", "W_BLANK_COUNT",
               "W_DUP_STEM", "W_UNIT_SCORE_MISMATCH", "W_TOTAL_SCORE_MISMATCH",
               "E_IMAGE_PNG_MISSING", "W_IMAGE_JSON_MISMATCH", "W_IMAGE_NONWEB_EXT",
               "W_PAPER_SUBJECT", "W_UNIT_SCORE_MISSING"}


def cell(x: Any, n: int | None = None) -> str:
    """表格单元格/列表项安全化：去换行、转义竖线、可选截断。"""
    t = str(x if x is not None else "")
    if n:
        t = truncate(t, n)
    return t.replace("\r", " ").replace("\n", " / ").replace("|", "\\|")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def do_check(papers: list[Paper], types: list[str], cmd: str) -> dict:
    findings: list[dict] = []
    per_paper = []
    totals: dict[str, dict] = {}
    paper_sets: dict[str, set] = {}
    for paper in papers:
        fs = Checker(paper).run()
        for f in fs:
            findings.append(f.as_dict())
            code = f.code
            t = totals.setdefault(code, {"sev": f.severity, "hit": 0})
            t["hit"] += 1
            paper_sets.setdefault(code, set()).add(paper.tag)
        cnt = {"error": 0, "warn": 0, "info": 0}
        for f in fs:
            cnt[f.severity] += 1
        per_paper.append({"paper": paper.tag, "title": paper.title,
                          "units": len(paper.units), "questions": len(paper.questions),
                          **cnt})
    for code, t in totals.items():
        t["papers"] = len(paper_sets.get(code, ()))
    return {"cmd": cmd, "types": types, "papers": len(papers),
            "units": sum(p["units"] for p in per_paper),
            "questions": sum(p["questions"] for p in per_paper),
            "totals": dict(sorted(totals.items(),
                                  key=lambda kv: (SEV_ORDER[kv[1]["sev"]], -kv[1]["hit"], kv[0]))),
            "per_paper": per_paper, "findings": findings}


def _render_check(L: list[str], ck: dict) -> None:
    L.append("## 2. 确定性检查（`--check`）\n")
    L.append(f"```bash\n{ck['cmd']}\n```\n")
    tot = {"error": 0, "warn": 0, "info": 0}
    for code, t in ck["totals"].items():
        tot[t["sev"]] += t["hit"]
    L.append(f"覆盖 **{ck['papers']} 卷 / {ck['units']} 题组 / {ck['questions']} 题**；"
             f"命中 **错误 {tot['error']} · 警告 {tot['warn']} · 提示 {tot['info']}**。\n")
    L.append("### 2.1 问题计数\n")
    L.append("| 代码 | 检查项 | 级别 | 命中 | 涉及卷 | 自动修复 | 修复建议 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for code, t in ck["totals"].items():
        auto = "✅ 可自动" if code in AUTO_FIX else ("❌ 需人工" if code in MANUAL_ONLY else "—")
        L.append(f"| `{code}` | {CODE_ZH.get(code, code)} | {SEV_ZH[t['sev']]} | "
                 f"{t['hit']} | {t.get('papers', 0)} | {auto} | {SUGGEST.get(code, '')} |")
    L.append("")
    # 逐卷清单：先给「有问题的卷」汇总表，再逐卷列明细（无问题的卷不展开）
    L.append("### 2.2 逐卷清单\n")
    by_paper: dict[str, list[dict]] = {}
    for f in ck["findings"]:
        by_paper.setdefault(f["p"], []).append(f)
    meta = {p["paper"]: p for p in ck["per_paper"]}
    dirty = [p for p in ck["per_paper"] if p["error"] or p["warn"]]
    L.append(f"有「错误/警告」的卷 **{len(dirty)} / {len(ck['per_paper'])}**"
             "（只有「提示」或完全干净的卷不展开，见 2.1 计数）。\n")
    if dirty:
        L.append("| 卷 | 题组 | 题 | 错误 | 警告 | 提示 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for m in dirty:
            L.append(f"| {cell(m['paper'])} | {m['units']} | {m['questions']} | "
                     f"{m['error']} | {m['warn']} | {m['info']} |")
        L.append("")
    for m in dirty:
        tag = m["paper"]
        fs = by_paper.get(tag, [])
        head = (f"#### {tag}\n\n- 题组 {m.get('units', 0)} / 题 {m.get('questions', 0)}；"
                f"错误 {m.get('error', 0)} · 警告 {m.get('warn', 0)} · 提示 {m.get('info', 0)}\n")
        lines = [f"- **[{'错误' if f['s'] == 'error' else ('警告' if f['s'] == 'warn' else '提示')}]** "
                 f"`{f['c']}`"
                 + (f"（{f['u']}）" if f.get("u") else "")
                 + (f" 题{f['q']}" if f.get("q") not in (None, "") else "")
                 + f"：{cell(f['t'])}" for f in fs]
        L.append(head + "\n" + "\n".join(lines) + "\n")


def _render_cross(L: list[str], cc: dict) -> None:
    L.append("## 3. AI 抽查\n")
    L.append("### 3.1 cross-check（模型独立作答，不给答案）\n")
    L.append(f"```bash\n{cc['cmd']}\n```\n")
    L.append(f"- 抽样：**{cc['papers_sampled']} 卷 × 每卷最多 {cc['n_per_paper']} 题**，"
             f"共 {cc['n']} 题（模型未作答 {cc['noans']} 题）")
    L.append(f"- 一致 / 不一致：**{cc['same']} / {cc['diff']}**，总体一致率 **{_pct(cc['rate'])}**")
    L.append(f"- 其中选择题：{cc['choice_n']} 题，一致 {cc['choice_same']} 题，"
             f"一致率 **{_pct(cc['choice_rate'])}**")
    L.append("")
    if cc["papers"]:
        L.append("| 卷 | 抽样题 | 一致 | 不一致 | 未作答 | 一致率 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for p in cc["papers"]:
            L.append(f"| {cell(p['paper'])} | {p['n']} | {p['same']} | {p['diff']} | "
                     f"{p['noans']} | {_pct(p['rate'])} |")
        L.append("")
    if cc.get("shift_best_count"):
        L.append(f"**平移诊断**（同一题组内 ≥3 题才计）：最优平移分布 "
                 f"{cc['shift_best_count']}（`0` = 无整体错位；±1/±2 = 疑似答案整体错位）\n")
        if cc.get("shift_suspects"):
            L.append("| 卷 | 题组 | 题数 | identity | 最优平移 | 最优吻合率 |")
            L.append("| --- | --- | --- | --- | --- | --- |")
            for s in cc["shift_suspects"]:
                L.append(f"| {cell(s['paper'])} | {cell(s['unit'])} | {s['n']} | "
                         f"{_pct(s['identity'])} | {s['best_shift']:+d} | {_pct(s['best_rate'])} |")
            L.append("")
        else:
            L.append("✓ 未发现「整体错位」迹象（无题组的 ±1/±2 平移明显优于 identity）。\n")
    if cc["rows"]:
        L.append("**分歧清单**\n")
        L.append("| 卷 | 题组 | 题号 | 原卷答案 | 模型独立答案 | 状态 | 模型理由 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in cc["rows"]:
            st = "未作答" if r["verdict"] == "noans" else "不一致"
            L.append(f"| {cell(r['paper'])} | {cell(r['unit'])} | {cell(r['qid'])} | "
                     f"{cell(r['key'], 30) or '—'} | {cell(r['model'])} | {st} | "
                     f"{cell(r['reason'])} |")
        L.append("")
    else:
        L.append("✓ 抽样题全部一致。\n")


def _render_audit(L: list[str], au: dict) -> None:
    L.append("### 3.2 ai-audit（题干-选项-答案自洽性 / 串题 / 错别字 / 学科错误）\n")
    L.append(f"```bash\n{au['cmd']}\n```\n")
    L.append(f"- 抽样：**{au['papers_sampled']} 卷 × 每卷最多 {au['n_per_paper']} 题**，"
             f"共 {au['questions']} 题；可疑 **{au['issues']}** 条")
    if au["kinds"]:
        L.append(f"- 类型分布：{'，'.join(f'{k} {v}' for k, v in au['kinds'].items())}")
    L.append("- **只报告不改**：ai-audit 的结论一律不落盘，需人工复核。\n")
    if au["rows"]:
        L.append("| 卷 | 题组 | 题号 | 已录入答案 | 类型 | 说明 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for r in au["rows"]:
            L.append(f"| {cell(r['paper'])} | {cell(r['unit'])} | {cell(r['qid'])} | "
                     f"{cell(r['answer'], 24) or '—'} | {cell(r['kind'])} | {cell(r['detail'])} |")
        L.append("")
    else:
        L.append("✓ 抽样题未发现可疑项。\n")


def _render_repair(L: list[str], rp: dict, name: str = "",
                   history: list[dict] | None = None) -> None:
    mode = "**已写盘（`--apply`）**" if rp["apply"] else "**dry-run（未写盘）**"
    L.append("## 4. 兜底修复（`--repair`）\n")
    L.append(f"```bash\n{rp['cmd']}\n```\n")
    L.append(f"模式：{mode}；本轮命中 **{rp['total']}** 处。\n")
    if history:
        hc: dict[str, int] = {}
        for it in history:
            hc[it["kind"]] = hc.get(it["kind"], 0) + 1
        L.append(f"**累计已写盘 {len(history)} 处**（跨多轮 `--apply` 累积；每处都在对应 "
                 "`<卷>.review.txt` 留痕）："
                 + "，".join(f"{KIND_ZH.get(k, k)} {v}" for k, v in
                             sorted(hc.items(), key=lambda kv: (-kv[1], kv[0]))) + "\n")
    if rp.get("conflicts"):
        L.append(f"⚠ 其中 **{rp['conflicts']}** 处「模型答案与解析里写的字母不一致」"
                 "（行尾标 `⚠解析里写的字母是 X`），必须人工抽查这几条。\n")
    if rp["counts"]:
        L.append("| 修复类别 | 条数 | 边界 |")
        L.append("| --- | --- | --- |")
        bounds = {
            "empty_answer": "模型两次独立作答一致且格式合法才补；analysis 追加「AI 补答案（原卷缺失）」",
            "answer_from_analysis": "解析原文明确写「故选X/正确答案为X」且 X 在选项内 → 取解析原文（不改语义）",
            "answer_letter_prefix": "原答案以字母开头但混了说明文字 → 取该字母，余下文字迁到 analysis（不丢信息）",
            "answer_not_in_options": "模型答案在选项内且两次一致才替换；留痕同上",
            "answer_case": "只做大小写/全角/空白归一，不改语义",
            "image_alt": "仅当同目录/同卷目录名变体存在同名替身时改引用",
            "score": "按题组分值均分兜底，只补 0 分/缺失",
        }
        for k, v in rp["counts"].items():
            L.append(f"| {KIND_ZH.get(k, k)} | {v} | {bounds.get(k, '')} |")
        L.append("")
        L.append("| 卷 | 题组 | 题号 | 字段 | 改前 | 改后 | 依据 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for it in rp["items"]:
            L.append(f"| {cell(it['paper'])} | {cell(it['unit']) or '—'} | {cell(it['qid']) or '—'} | "
                     f"{cell(it['field'])} | {cell(it['old'], 36) or '（空）'} | "
                     f"{cell(it['new'], 40)} | {cell(it['note'], 60)} |")
        L.append("")
    elif history:
        L.append("✓ 末轮复跑 0 处（幂等：结果与累计写盘一致；缓存命中，`--no-ai` 可复现）。\n")
    else:
        L.append("✓ 没有需要兜底的项。\n")
    if history:
        L.append("### 4.1 累计写盘明细\n")
        L.append("| 卷 | 题组 | 题号 | 字段 | 改前 | 改后 | 依据 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for it in history:
            L.append(f"| {cell(it['paper'])} | {cell(it.get('unit')) or '—'} | "
                     f"{cell(it.get('qid')) or '—'} | {cell(it.get('field'))} | "
                     f"{cell(it.get('old'), 36) or '（空）'} | {cell(it.get('new'), 40)} | "
                     f"{cell(it.get('note'), 60)} |")
        L.append("")
    if rp["skipped"]:
        L.append(f"**未采纳（两次不一致 / 格式不合法 / 模型未作答）{len(rp['skipped'])} 条**，"
                 "保持原样、只报告：\n")
        for s in rp["skipped"][:60]:
            L.append(f"- {cell(s['paper'])}：{cell(s['text'])}")
        if len(rp["skipped"]) > 60:
            L.append(f"- …（其余 {len(rp['skipped']) - 60} 条同类，见 `qa/{name}.json`）")
        L.append("")


def _render_manual(L: list[str], sc: dict) -> None:
    L.append("## 5. 需人工介入\n")
    ck = sc.get("check") or {}
    auto_covered = {"W_ANSWER_FORMAT", "E_SCORE_LE0", "E_EMPTY_CHOICE", "W_EMPTY_FILL",
                    "E_ANSWER_NOT_IN_OPTIONS", "E_MISSING_IMAGE"}
    repaired = set(((sc.get("repair") or {}).get("counts") or {}).keys())
    rows = [(c, t) for c, t in (ck.get("totals") or {}).items()
            if c not in ("I_EMPTY_OPEN", "I_TOTAL_SCORE_PARTIAL", "I_IMG_TEXT_QUESTION")]
    if not ck:
        L.append("（先跑 `--check` 生成清单。）\n")
        return
    if not rows:
        L.append("✓ 确定性检查未发现需要人工介入的问题。\n")
        return
    L.append("| 检查项 | 命中 | 已自动处理 | 说明 |")
    L.append("| --- | --- | --- | --- |")
    for code, t in rows:
        done = "是" if (code in repaired or (code in auto_covered and
                                            (sc.get("repair") or {}).get("apply")) ) else "否"
        note = SUGGEST.get(code, "")
        if code in AUTO_FIX and done == "是":
            note = "已按规则兜底并留痕（`<卷>.review.txt`），建议抽查 5~10 条"
        L.append(f"| {CODE_ZH.get(code, code)}（`{code}`） | {t['hit']} | {done} | {note} |")
    L.append("")


def render_subject(sc: dict) -> str:
    name = name_of(sc["subject"], sc.get("layout", "json"))
    L = [f"# {name} · 质量脚手架报告\n",
         "> 由 `exam-parser/qa_scaffold.py` 自动生成（可重复执行，逐节累积合并）。",
         f"> 数据根 `{DATA_ROOT}`；本文件路径 `<数据根>/qa/{name}.md`。\n"]
    ck = sc.get("check") or {}
    if ck.get("per_paper"):
        L.append("## 1. 覆盖面\n")
        L.append("| 类型 | 卷 | 题组 | 题 | 错误 | 警告 | 提示 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        agg: dict[str, dict] = {}
        for m in ck["per_paper"]:
            t = m["paper"].split("/")[0]
            a = agg.setdefault(t, {"papers": 0, "units": 0, "questions": 0,
                                   "error": 0, "warn": 0, "info": 0})
            for k in ("units", "questions", "error", "warn", "info"):
                a[k] += m.get(k, 0)
            a["papers"] += 1
        for t in sorted(agg):
            a = agg[t]
            L.append(f"| {t} | {a['papers']} | {a['units']} | {a['questions']} | "
                     f"{a['error']} | {a['warn']} | {a['info']} |")
        L.append("")
    if sc.get("check"):
        _render_check(L, sc["check"])
    if sc.get("cross_check"):
        _render_cross(L, sc["cross_check"])
    elif sc.get("ai_audit"):
        L.append("## 3. AI 抽查\n")
    if sc.get("ai_audit"):
        _render_audit(L, sc["ai_audit"])
    if sc.get("repair"):
        _render_repair(L, sc["repair"], name, sc.get("repair_history") or [])
    _render_manual(L, sc)
    return "\n".join(L) + "\n"


def render_readme(sidecars: list[dict]) -> str:
    L = ["# 质量脚手架汇总（qa/README.md）\n",
         "> 由 `exam-parser/qa_scaffold.py` 自动生成，**勿手改**。",
         "> 每科明细报告在同目录 `<学科>.md`；口径见 `exam-parser/PIPELINES.md` 的「质量脚手架」小节。\n",
         "| 学科 | 卷 | 题 | 确定性错误 | 警告 | 提示 | cross-check 一致率（选择） | ai-audit 可疑 | 兜底修复（模式/条数） | 报告 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for sc in sidecars:
        ck = sc.get("check") or {}
        tot = {"error": 0, "warn": 0, "info": 0}
        for code, t in (ck.get("totals") or {}).items():
            tot[t["sev"]] += t["hit"]
        cc = sc.get("cross_check") or {}
        au = sc.get("ai_audit") or {}
        rp = sc.get("repair") or {}
        mode = ("apply" if rp.get("apply") else "dry-run") if rp else "—"
        nm = name_of(sc["subject"], sc.get("layout", "json"))
        rep = (f"累计 {len(sc.get('repair_history') or [])}（末轮 {mode} {rp.get('total')}）"
               if rp else "—")
        L.append(f"| {nm} | {ck.get('papers', '—')} | {ck.get('questions', '—')} | "
                 f"{tot['error']} | {tot['warn']} | {tot['info']} | "
                 f"{_pct(cc.get('choice_rate')) if cc else '—'} | "
                 f"{au.get('issues', '—')} | {rep} | [{nm}.md]({nm}.md) |")
    L.append("")
    L.append("约定：确定性检查不花钱、可全量跑；`--cross-check` / `--ai-audit` 抽样控制成本"
             "（建议每科 20 卷 × 每卷 10 题 / 10 卷）；`--repair` 默认 dry-run，`--apply` 才写盘。\n")
    return "\n".join(L) + "\n"


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────

def resolve_types(layout: Layout, arg: str) -> list[str]:
    raw = [t.strip() for t in (arg or "二模").replace("，", ",").split(",") if t.strip()]
    if "all" in raw:
        return layout.all_types()
    return raw


def name_of(subject: str, kind: str) -> str:
    """同一学科有多条链路时报告/状态分文件（化学图片版与化学选择题 JSON 并存）。"""
    return f"{subject}（图片版）" if kind == "image" else subject


def sidecar_path(subject: str, kind: str = "json") -> Path:
    return QA_DIR / f"{name_of(subject, kind)}.json"


def report_path(subject: str, kind: str, arg: str | None) -> Path:
    name = name_of(subject, kind)
    if not arg:
        return QA_DIR / f"{name}.md"
    p = Path(arg).expanduser()
    if p.suffix.lower() == ".md":
        return p if p.is_absolute() else (Path.cwd() / p)
    d = p if p.is_absolute() else (Path.cwd() / p)
    return d / f"{name}.md"


def load_sidecar(subject: str, kind: str = "json") -> dict:
    p = sidecar_path(subject, kind)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            pass
    return {"subject": subject}


def save_reports(subject: str, kind: str, sc: dict, rp: Path) -> None:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    sidecar_path(subject, kind).write_text(json.dumps(sc, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render_subject(sc), encoding="utf-8")
    sidecars = []
    for f in sorted(QA_DIR.glob("*.json")):
        if f.name in ("README.json",):
            continue
        try:
            sidecars.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:                                 # noqa: BLE001
            continue
    (QA_DIR / "README.md").write_text(render_readme(sidecars), encoding="utf-8")


def cmdline(argv: list[str] | None = None) -> str:
    """记录进报告的命令：去掉不影响结论的开关（--no-ai / --force / --workers），
    这样 `--no-ai` 复跑能逐字节复现同一份报告。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    defaults = {"--ai-papers": "20", "--audit-papers": "10", "--limit": "0"}
    out, i = [], 0
    while i < len(raw):
        a = raw[i]
        if a in ("--no-ai", "--force"):
            i += 1
            continue
        if a == "--workers":
            i += 2
            continue
        if a.startswith("--workers="):
            i += 1
            continue
        if a in defaults:
            val = raw[i + 1] if i + 1 < len(raw) else ""
            if val == defaults[a]:
                i += 2
                continue
        if "=" in a and a.split("=", 1)[0] in defaults:
            k, v = a.split("=", 1)
            if v == defaults[k]:
                i += 1
                continue
        out.append(a)
        i += 1
    return " ".join(["python3", "qa_scaffold.py"] + out)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="跨学科质量脚手架：确定性检查 + AI 抽查 + 兜底修复（生物以外 6 科）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--subject", required=True, choices=SUBJECTS, help="学科")
    ap.add_argument("-T", "--type", default="二模",
                    help="试卷类型：二模 / 二模,一模 / all（默认 二模）")
    ap.add_argument("--check", action="store_true", help="确定性检查（默认动作，不调模型）")
    ap.add_argument("--cross-check", type=int, default=None, metavar="N",
                    help="AI 独立作答复核：每卷最多抽样 N 题")
    ap.add_argument("--ai-audit", type=int, default=None, metavar="N",
                    help="AI 自洽性抽查：每卷最多抽 N 题")
    ap.add_argument("--repair", action="store_true", help="兜底修复（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="配合 --repair 真正写盘")
    ap.add_argument("--image", action="store_true",
                    help="化学图片版（output_image_questions）：只做确定性检查")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="不调模型（只吃缓存）")
    ap.add_argument("--force", action="store_true", help="忽略 AI 缓存重跑")
    ap.add_argument("--workers", type=int, default=4, help="AI 并发（默认 4）")
    ap.add_argument("--papers", nargs="*", default=None, help="只跑卷名含这些片段的卷")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少卷（调试用）")
    ap.add_argument("--ai-papers", type=int, default=20,
                    help="cross-check 抽样卷数（默认 20）")
    ap.add_argument("--audit-papers", type=int, default=10,
                    help="ai-audit 抽样卷数（默认 10）")
    ap.add_argument("--report", default=None,
                    help="报告路径（.md 文件或缺省目录；默认 <数据根>/qa/<学科>.md）")
    args = ap.parse_args(argv)

    if args.image and args.subject != "化学":
        ap.error("--image 只用于化学图片版（output_image_questions）")
    if args.apply and not args.repair:
        ap.error("--apply 必须配合 --repair")
    do_any = bool(args.check or args.cross_check is not None
                  or args.ai_audit is not None or args.repair)
    if not do_any:
        args.check = True

    layout = layouts_for(args.subject, args.image)[0]
    types = resolve_types(layout, args.type)
    papers = collect_papers(layout, types, args.limit, args.papers)
    if not papers:
        print(f"✗ {args.subject} 没有可检查的产物（类型 {types}；link={layout.kind}）")
        return 1
    print(f"[{args.subject}] 类型 {'、'.join(types)}：{len(papers)} 卷（{layout.kind} 链路）")

    kind = layout.kind
    sc = load_sidecar(args.subject, kind)
    sc["subject"] = args.subject
    sc["types"] = types
    sc["layout"] = layout.kind
    cmd = cmdline()

    if args.check:
        ck = do_check(papers, types, cmd)
        sc["check"] = ck
        tot = {"error": 0, "warn": 0, "info": 0}
        for code, t in ck["totals"].items():
            tot[t["sev"]] += t["hit"]
        print(f"  --check：{ck['papers']} 卷 / {ck['units']} 题组 / {ck['questions']} 题；"
              f"错误 {tot['error']} · 警告 {tot['warn']} · 提示 {tot['info']}")
        for code, t in ck["totals"].items():
            print(f"     {SEV_ZH[t['sev']]} `{code}` {CODE_ZH.get(code, code)}："
                  f"{t['hit']} 处 / {t.get('papers', 0)} 卷")

    gw = Gateway(args.subject, enabled=args.ai, force=args.force)
    if args.image and (args.cross_check is not None or args.ai_audit is not None
                       or args.repair):
        print("  · 图片版题目只有截图、没有文字题干：AI 抽查/修复跳过（只做确定性检查）")
        sc.setdefault("notes", [])
        note = "图片版（--image）只有截图题面，AI 抽查与 AI 兜底不适用，仅做确定性检查"
        if note not in sc["notes"]:
            sc["notes"].append(note)

    if args.cross_check is not None and not args.image:
        print(f"  --cross-check {args.cross_check}：抽样 {args.ai_papers} 卷 × 每卷最多 "
              f"{args.cross_check} 题…")
        cc = do_cross_check(papers, args.cross_check, args.ai_papers, gw,
                            args.workers, cmd)
        sc["cross_check"] = cc
        print(f"     一致 {cc['same']} / 不一致 {cc['diff']} / 未作答 {cc['noans']}；"
              f"总体 {_pct(cc['rate'])}，选择题 {_pct(cc['choice_rate'])}；"
              f"最优平移分布 {cc['shift_best_count']}；模型调用 {gw.calls} 次（缓存命中 {gw.hits}）")

    if args.ai_audit is not None and not args.image:
        print(f"  --ai-audit {args.ai_audit}：抽样 {args.audit_papers} 卷…")
        au = do_ai_audit(papers, args.ai_audit, args.audit_papers, gw,
                         args.workers, cmd)
        sc["ai_audit"] = au
        print(f"     题 {au['questions']}，可疑 {au['issues']} 条"
              + (f"（{'，'.join(f'{k} {v}' for k, v in au['kinds'].items())}）" if au["kinds"] else "")
              + f"；模型调用 {gw.calls} 次（缓存命中 {gw.hits}）")

    if args.repair:
        print(f"  --repair {'（写盘）' if args.apply else '（dry-run）'}…")
        rp = do_repair(papers, gw, args.apply, args.workers, cmd)
        sc["repair"] = rp
        if args.apply and rp["items"]:
            hist = sc.setdefault("repair_history", [])
            seen = {(h["paper"], h.get("unit"), str(h.get("qid")), h.get("field"),
                     str(h.get("new"))) for h in hist}
            for it in rp["items"]:
                k = (it["paper"], it.get("unit"), str(it.get("qid")), it.get("field"),
                     str(it.get("new")))
                if k not in seen:
                    seen.add(k)
                    hist.append(it)
        print(f"     修复 {rp['total']} 处：" + ("，".join(
            f"{KIND_ZH.get(k, k)} {v}" for k, v in rp["counts"].items()) or "无")
            + f"；未采纳 {len(rp['skipped'])} 条；模型调用 {gw.calls} 次（缓存命中 {gw.hits}）")

    rp_path = report_path(args.subject, kind, args.report)
    save_reports(args.subject, kind, sc, rp_path)
    print(f"  报告：{rp_path}")
    print(f"  汇总：{QA_DIR / 'README.md'}；状态：{sidecar_path(args.subject, kind)}")
    if gw.errors:
        print(f"  ⚠ 模型调用失败 {gw.errors} 次（结论里会缺这些样本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())


