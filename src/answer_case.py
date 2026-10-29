#!/usr/bin/env python3
"""选择题答案 / 选项 id 大小写统一（各学科链路共用的「出口」规范化）。

题库约定（gxexercise 前端按这个匹配才能把正确选项标绿）：

- **选择题**：``answer`` 与 ``options[].id`` 一律**大写字母**（A/B/C/D…）；
- **非选择题**（语法填空、选词填空、阅读表达、作文、化学/政治解答题）：
  答案是单词或句子，**原样保留大小写**（"faced"、"With" 都不能动）。

各科链路的解析/合并逻辑内部按小写 key 做匹配是正常的，**不要改**；只在
「最终写 JSON」的地方调用一次本模块，让出口统一：

    from src.answer_case import normalize_choice_case_in_paper
    payload = {"paper": paper, "sections": sections}
    normalize_choice_case_in_paper(payload, log=log)   # 就地修改
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

处理规则（只动「带非空 options 且每个 option id 都是单个字母」的题）：

1. 选项 id 统一 ``.upper()``；
2. answer 为空 → 只改选项 id，answer 不动；
3. answer 的**字母部分**统一大写，其余内容原样保留：
   - 按 ``##`` 与分隔符（``, ， ; ； / 、``）切段，纯字母段（1~2 个字母）大写；
   - 括号里的单个字母（``【c】`` / ``[c]`` / ``（c）`` / ``(c)``）大写；
   - 行首单字母 + 标点（``"b。（…）"``）大写首字母；
   - 单词/短语/整句（不是选项字母）不动；
4. 答案字母不在 options 里时**照样大写**，同时返回 warning（调用方打印，不静默）。

幂等：对已经规范化过的数据再跑一次，改动题数为 0。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List, Tuple

__all__ = [
    "normalize_choice_case_in_paper",
    "iter_questions_with_options",
    "iter_choice_questions",
    "scan_choice_case_issues",
]

# 选项 id：单个 ASCII 字母（A-Za-z）
_OPTION_ID_RE = re.compile(r"^[A-Za-z]$")

# answer 的分段分隔符：## 备选 + 常见多选/备选分隔符
_ANSWER_SEP_RE = re.compile(r"(##|[,，;；/、])")
# answer 里的纯选项字母段："a"、"A"、"ab"（多选）。3 个及以上字母视为单词，不动。
_ANSWER_TOKEN_RE = re.compile(r"^[A-Za-z]{1,2}$")
# 括号里包着的单个字母：【c】［c］[c]（c）(c)
_BRACKET_LETTER_RE = re.compile(r"([【［\[])\s*([A-Za-z])\s*([】］\]])")
_PAREN_LETTER_RE = re.compile(r"([（(])\s*([A-Za-z])\s*([）)])")
# 行首单字母 + 标点/空白（如语文答案 "b。（…）"）
_LEADING_LETTER_RE = re.compile(r"^[A-Za-z](?=[。．.、：:）)，,;；\s])")


# ──────────────────────────────────────────────────────────────────
# 遍历
# ──────────────────────────────────────────────────────────────────

def _all_single_letter_ids(options: List[Any]) -> bool:
    """options 里每一项都是 dict 且 id 是单个字母。"""
    if not options:
        return False
    for opt in options:
        if not isinstance(opt, dict):
            return False
        oid = opt.get("id")
        if not isinstance(oid, str) or not _OPTION_ID_RE.match(oid.strip()):
            return False
    return True


def iter_questions_with_options(node: Any, path: str = "") -> Iterator[Tuple[str, Dict[str, Any]]]:
    """遍历所有「带非空 options」的题目 dict（不要求 id 是单个字母，供校验用）。

    兼容三种结构：
    - ``{"sections"|"groups"|"questions": [...]}``（paper.json 的常见形态）；
    - 顶层就是 questions 列表；
    - 顶层就是单道题（图片版 ``q{n}.json``）。
    """
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from iter_questions_with_options(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    options = node.get("options")
    if isinstance(options, list) and options:
        yield (path or "question"), node
    for key in ("questions", "sections", "groups"):
        sub = node.get(key)
        if isinstance(sub, list):
            base = f"{path}.{key}" if path else key
            yield from iter_questions_with_options(sub, base)


def iter_choice_questions(node: Any, path: str = "") -> Iterator[Tuple[str, Dict[str, Any]]]:
    """遍历「可安全规范化」的选择题（options 非空且每个 id 都是单个字母）。"""
    for qpath, q in iter_questions_with_options(node, path):
        if _all_single_letter_ids(q.get("options") or []):
            yield qpath, q


# ──────────────────────────────────────────────────────────────────
# answer 的字母大写
# ──────────────────────────────────────────────────────────────────

def _normalize_answer_letters(
    answer: Any, option_ids: List[str]
) -> Tuple[Any, bool, set]:
    """把答案里的选项字母大写。

    Returns:
        (新答案, 是否有改动, 不在选项里的字母集合)
    """
    if not isinstance(answer, str) or not answer.strip():
        return answer, False, set()

    upper_ids = {str(i).strip().upper() for i in option_ids}
    hits: set = set()
    changed = False

    # 1) 按 ## / 分隔符切段，纯字母段（1~2 个字母）大写
    out: List[str] = []
    for part in _ANSWER_SEP_RE.split(answer):
        if _ANSWER_SEP_RE.fullmatch(part):
            out.append(part)
            continue
        token = part.strip()
        if token and _ANSWER_TOKEN_RE.fullmatch(token):
            hits.update(token.upper())
            if token != token.upper():
                changed = True
                part = part.replace(token, token.upper(), 1)
        out.append(part)
    answer = "".join(out)

    # 2) 括号里的单个字母
    def _up_letter(m: "re.Match[str]") -> str:
        nonlocal changed
        letter = m.group(2)
        hits.add(letter.upper())
        if letter != letter.upper():
            changed = True
            return f"{m.group(1)}{letter.upper()}{m.group(3)}"
        return m.group(0)

    answer = _BRACKET_LETTER_RE.sub(_up_letter, answer)
    answer = _PAREN_LETTER_RE.sub(_up_letter, answer)

    # 3) 行首单字母 + 标点（"b。（…）" → "B。（…）"）
    if answer and _LEADING_LETTER_RE.match(answer):
        letter = answer[0]
        hits.add(letter.upper())
        if letter.islower():
            answer = letter.upper() + answer[1:]
            changed = True

    missing = {h for h in hits if h not in upper_ids}
    return answer, changed, missing


# ──────────────────────────────────────────────────────────────────
# 单题 / 整卷规范化
# ──────────────────────────────────────────────────────────────────

def _normalize_question(q: Dict[str, Any]) -> Tuple[bool, List[str], str, str]:
    """就地规范化一道选择题。

    Returns:
        (是否改动, warnings, 改动前后的简短描述)
    """
    options = q.get("options") or []
    before_ids = [str(o.get("id")).strip() for o in options]

    changed = False
    for opt, oid in zip(options, before_ids):
        up = oid.upper()
        if opt.get("id") != up:
            opt["id"] = up
            changed = True

    before_answer = q.get("answer", "")
    new_answer, ans_changed, missing = _normalize_answer_letters(before_answer, before_ids)
    if ans_changed:
        q["answer"] = new_answer
        changed = True

    warnings: List[str] = []
    if missing:
        warnings.append(
            f"答案字母 {'/'.join(sorted(missing))} 不在选项 "
            f"{'/'.join(sorted({i.upper() for i in before_ids}))} 里（已照样大写）"
        )

    detail = ""
    if changed:
        bits = []
        if before_ids and before_ids != [o.get("id") for o in options]:
            bits.append(f"选项 id {','.join(before_ids)} → {','.join(str(o.get('id')) for o in options)}")
        if ans_changed:
            bits.append(f"答案 {before_answer!r} → {new_answer!r}")
        detail = "；".join(bits)
    return changed, warnings, detail, new_answer


def normalize_choice_case_in_paper(
    data: Any,
    log: Callable[[str], Any] = None,
) -> Tuple[int, List[str]]:
    """就地规范化整卷 / 单题 / 题目列表里的选择题大小写。

    Args:
        data: 可以是整卷 dict（``sections`` / ``groups`` / 顶层 ``questions``）、
              题目列表，或图片版 ``q{n}.json`` 这种单题 dict。
        log: 可选日志函数（脚本传自己的 ``log`` / ``logging.info`` / ``print``）。

    Returns:
        (改动题数, 告警列表)。已规范化的数据再跑一次改动题数为 0（幂等）。
    """
    changed_total = 0
    warnings: List[str] = []
    details: List[str] = []

    for path, q in iter_choice_questions(data):
        changed, warns, detail, _ = _normalize_question(q)
        if changed:
            changed_total += 1
            if detail:
                details.append(f"{path}: {detail}")
        for w in warns:
            warnings.append(f"{path}: {w}")

    if log:
        if changed_total:
            log(f"  ✓ 选择题答案/选项 id 统一大写：{changed_total} 题")
            for d in details[:3]:
                log(f"    · {d}")
            if len(details) > 3:
                log(f"    · …（另有 {len(details) - 3} 题同类改动）")
        for w in warnings:
            log(f"  ⚠ 大小写规范化：{w}")
    return changed_total, warnings


# ──────────────────────────────────────────────────────────────────
# 校验（validate_json.py 用）
# ──────────────────────────────────────────────────────────────────

def scan_choice_case_issues(data: Any) -> List[str]:
    """检查大小写一致性，返回问题清单（供 validate_json.py 报 warning）。

    报告的情况：
    - 选项 id 不是单个字母（无法统一成 A/B/C…）；
    - 选项 id 不是大写；
    - 答案里的字母是小写（与选项 id 大小写不一致）；
    - 答案字母不在选项里。
    """
    issues: List[str] = []
    for path, q in iter_questions_with_options(data):
        options = q.get("options") or []
        ids = [str(o.get("id")) if isinstance(o, dict) else o for o in options]
        bad_letter = [str(i) for i in ids if not _OPTION_ID_RE.match(str(i).strip())]
        if bad_letter:
            issues.append(f"{path}：选项 id 不是单个字母 {bad_letter}，无法统一大写")
            continue
        not_upper = [str(i) for i in ids if str(i).strip() != str(i).strip().upper()]
        if not_upper:
            issues.append(f"{path}：选项 id 不是大写 {not_upper}")
        answer = q.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            continue
        new_answer, ans_changed, missing = _normalize_answer_letters(answer, [str(i) for i in ids])
        if ans_changed:
            issues.append(f"{path}：答案字母大小写与选项 id 不一致 {answer!r}（应为 {new_answer!r}）")
        if missing:
            issues.append(
                f"{path}：答案字母 {'/'.join(sorted(missing))} 不在选项 "
                f"{'/'.join(sorted({str(i).strip().upper() for i in ids}))} 里（{answer!r}）"
            )
    return issues


if __name__ == "__main__":  # 极简自测：python3 -m src.answer_case
    _paper = {
        "sections": [
            {"questions": [
                {"id": 1, "answer": "c",
                 "options": [{"id": "a", "label": "x"}, {"id": "b", "label": "y"},
                             {"id": "c", "label": "z"}]},
                {"id": 2, "answer": "a##b",
                 "options": [{"id": "a", "label": "x"}, {"id": "b", "label": "y"}]},
            ]},
            {"questions": [
                {"id": 1, "answer": "faced", "options": []},
                {"id": 2, "answer": "With", "options": []},
            ]},
        ]
    }
    n1, w1 = normalize_choice_case_in_paper(_paper)
    n2, w2 = normalize_choice_case_in_paper(_paper)
    assert _paper["sections"][0]["questions"][0]["answer"] == "C"
    assert _paper["sections"][0]["questions"][0]["options"][0]["id"] == "A"
    assert _paper["sections"][0]["questions"][1]["answer"] == "A##B"
    assert _paper["sections"][1]["questions"][0]["answer"] == "faced"
    assert _paper["sections"][1]["questions"][1]["answer"] == "With"
    assert (n1, n2) == (2, 0), (n1, n2)
    assert not w1 and not w2
    print(f"answer_case 自测通过：首次改动 {n1} 题，二次改动 {n2} 题（幂等）")
