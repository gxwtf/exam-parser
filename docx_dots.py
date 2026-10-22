#!/usr/bin/env python3
"""从 Word（docx）里提取「着重号（加点）」样式，并把加点字包成 <Dot>…</Dot>。

背景：文言文选择题常考「加点词语」，Word 里是字符着重号（w:em w:val="dot"），
但 docx→Markdown 会把样式丢掉，只剩文字。这里直接读 docx 的 XML，
建立「纯文本 ↔ 加点位置」的映射，再把我们切出来的题干/选项文本映射回去。

对外接口：
    dots = DocxDots("xxx.docx")
    dots.annotate("边郡略无宁岁 略；完全")  ->  "边郡<Dot>略</Dot>无宁岁 <Dot>略</Dot>；完全"
"""
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_runs(p) -> list[tuple[str, bool]]:
    """返回 [(文字, 是否加点)]（只取文字 run，忽略表格嵌套里的复杂结构）"""
    out: list[tuple[str, bool]] = []
    for r in p.iter(W + "r"):
        txt = "".join(t.text or "" for t in r.iter(W + "t"))
        if not txt:
            continue
        rpr = r.find(W + "rPr")
        dotted = False
        if rpr is not None:
            em = rpr.find(W + "em")
            if em is not None and (em.get(W + "val") or "dot") == "dot":
                dotted = True
        out.append((txt, dotted))
    return out


class DocxDots:
    def __init__(self, docx_path: str | Path):
        self.chars: list[str] = []      # 纯文本
        self.dots: list[bool] = []      # 对应位置是否加点
        with zipfile.ZipFile(docx_path) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        body = root.find(W + "body")
        if body is None:
            return
        for el in body.iter():
            if el.tag == W + "p":
                for txt, dotted in _para_runs(el):
                    for ch in txt:
                        self.chars.append(ch)
                        self.dots.append(dotted)
                self.chars.append("\n")
                self.dots.append(False)
        # 去掉表格段落重复（w:p 在表格里也会被 iter 到，这里只做去重后的正常追加，够用）

    # ---- 查询 ----
    def _compact_index(self):
        """紧凑化（去空白）后的 字符→原下标 映射"""
        idx = []
        for i, ch in enumerate(self.chars):
            if not ch.isspace():
                idx.append(i)
        return idx

    def _flags_for(self, text: str) -> list[bool] | None:
        """把 text 与文档文本做「去空白」对齐，返回每个字符（含空格）是否加点"""
        idx = self._compact_index()
        compact = "".join(self.chars[i] for i in idx)
        target = "".join(ch for ch in text if not ch.isspace())
        if not target:
            return None
        pos = compact.find(target)
        if pos < 0:
            return None
        flags: list[bool] = []
        k = pos
        for ch in text:
            if ch.isspace():
                flags.append(False)
            else:
                flags.append(self.dots[idx[k]])
                k += 1
        return flags

    def annotate(self, text: str) -> str:
        """把 text 里加点字符包成 <Dot>…</Dot>（保持原文本的空格与标点不变）"""
        flags = self._flags_for(text)
        if not flags or not any(flags):
            return text
        out, open_tag = [], False
        for ch, d in zip(text, flags):
            if d and not open_tag:
                out.append("<Dot>"); open_tag = True
            elif not d and open_tag:
                out.append("</Dot>"); open_tag = False
            out.append(ch)
        if open_tag:
            out.append("</Dot>")
        return "".join(out)

    def annotate_fragments(self, text: str) -> str:
        """按空白切段逐段标注（两栏选项里每个片段在 docx 里是连续文本，能各自命中）"""
        parts = re.split(r"(\s+)", text or "")
        out = []
        for part in parts:
            if not part:
                continue
            out.append(part if part.isspace() else self.annotate(part))
        return "".join(out)

    def marked(self, text: str) -> str:
        """用 ⟦D:x⟧ 标记加点（给模型看的版本，避免它破坏标签）"""
        flags = self._flags_for(text)
        if not flags or not any(flags):
            return text
        out, open_tag = [], False
        for ch, d in zip(text, flags):
            if d and not open_tag:
                out.append("⟦D:"); open_tag = True
            elif not d and open_tag:
                out.append("⟧"); open_tag = False
            out.append(ch)
        if open_tag:
            out.append("⟧")
        return "".join(out)

    def segment(self, stem: str, limit: int = 4000) -> str:
        """按题干定位 docx 里这一题的原文（到下一个「1．/2．」题号为止），返回带 ⟦D:x⟧ 的文本"""
        idx = self._compact_index()
        compact = "".join(self.chars[i] for i in idx)
        key = "".join(ch for ch in stem if not ch.isspace())[:24]
        if not key:
            return ""
        pos = compact.find(key)
        if pos < 0:
            return ""
        start = idx[pos]
        # 往前找段首
        while start > 0 and self.chars[start - 1] != "\n":
            start -= 1
        # 往后找「题号行」
        rest = "".join(self.chars[start:start + limit])
        m = re.search(r"\n\s*\d{1,2}\s*[、.．](?!\d)", rest[1:])
        end_txt = rest[: m.start() + 1] if m else rest
        # 生成带标记文本
        out, open_tag = [], False
        base = start
        for i, ch in enumerate(end_txt):
            d = self.dots[base + i]
            if d and not open_tag:
                out.append("⟦D:"); open_tag = True
            elif not d and open_tag:
                out.append("⟧"); open_tag = False
            out.append(ch)
        if open_tag:
            out.append("⟧")
        return "".join(out)


if __name__ == "__main__":
    import sys
    dots = DocxDots(sys.argv[1])
    for s in sys.argv[2:]:
        print(s, "->", dots.annotate(s))
