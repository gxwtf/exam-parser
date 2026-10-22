#!/usr/bin/env python3
"""化学试卷（教师版）docx→MD → JSON（只处理选择题）

和物理导入走同一套数据结构与导入脚本（output_json_math/<类型>/化学/<卷>.json → import-math-json.ts），
但文本来源换成 Word 转出的 MD：
  · 教师版 PDF 里大量「浮动插图」会压在文字上（实测 2025 西城第 2–4 题选项被图盖住），
    多模态模型读 PDF 页面会把被盖住的选项截断；docx→MD 的文字是完整的。
  · MD 里的选项是「A．…」独立成行 / 表格单元格，题号独立成行 → 题干、四个选项可以直接确定性地切出来，
    图片占位符 ⟦IMGn⟧ 留在原位置，所以插图不会挂错题、挂错选项。
  · 切好之后再让模型只做「排版转换」（HTML 上下标 → LaTeX、\\= 之类转义还原、化学式/方程式转 LaTeX），
    并且做保真校验（占位符、长度），不合格就退回原文。
  · .wmf（Word 公式/图形）先 wmf2gd 转 png，图片复制到 output_docx_to_md/<类型>/化学/<卷>/images/（nginx /exam-images 直出）。

用法：
  python3 convert_chemistry_md.py -p 2025北京西城高三二模化学       # 单份
  python3 convert_chemistry_md.py --type 二模 -w 4                 # 批量
  python3 convert_chemistry_md.py --type 二模 --force               # 覆盖已生成
"""
import argparse, base64, json, os, re, shutil, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

MD_ROOT = PROJECT_ROOT.parent / "output_docx_to_md"
JSON_ROOT = PROJECT_ROOT.parent / "output_json_math"
WMF_EX = Path("/tmp/wmfbin/ex")

BASE_URL = (os.getenv("MATH_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1")).rstrip("/")
API_KEY = os.getenv("MATH_VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("MATH_VISION_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash")

IMGREF = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")
TOKEN = re.compile(r"⟦IMG(\d+)⟧")
QSPLIT = re.compile(r"(?m)^[ \t\u3000\u00a0]*((?:(?:!\[[^\]]*\]\([^)]*\)|⟦IMG\d+⟧)[ \t]*)*)\**[ \t]*(\d{1,2})[ \t]*\\?[．.][ \t]*")
# 选项行：允许行首有图片占位/图片引用/加粗符号（w2m 常把插图放在选项前同一行）
OPT_LINE = re.compile(r"(?m)^[ \t\u3000\u00a0]*((?:(?:!\[[^\]]*\]\([^)]*\)|⟦IMG\d+⟧)[ \t]*)*)\**[ \t]*([A-D])[ \t]*\\?[．.、][ \t]*")
OPT_CELL = re.compile(r"\|[ \t]*\**[ \t]*([A-D])[ \t]*\\?[．.、][ \t]*([^|\n]*?)[ \t]*(?=\||$)")

SYSTEM_PROMPT = "你是化学试卷排版员，只输出 JSON，不要 markdown 代码块，不要任何解释。"

FORMAT_PROMPT = r"""下面是一份北京高三化学试卷里各道【选择题】的题干与四个选项（已从 Word 转出的 Markdown 里切好）。
请把每一项文本整理成题库用的 Markdown + LaTeX，**严格逐字保真：一个字的增删改写都不允许**，只做格式转换：

1. 化学式/离子/方程式写成 LaTeX：
   - 化学式 $\mathrm{H_2O}$、$\mathrm{Fe_2O_3}$、$\mathrm{CH_3CH_2OH}$
   - 离子 $\mathrm{SO_4^{2-}}$、$\mathrm{NH_4^+}$、$\mathrm{OH^-}$
   - 方程式 $\mathrm{2Na_2O_2 + 2H_2O = 4NaOH + O_2\uparrow}$；反应条件写箭头上
     （$\mathrm{CaCO_3 \xrightarrow{高温} CaO + CO_2\uparrow}$）；可逆反应用 \rightleftharpoons
   - 数字与单位也包起来：$0.1\ \mathrm{mol/L}$、$25\ \mathrm{^\circ C}$
2. HTML 标签 <sub>…</sub> / <sup>…</sup> 去掉，但内容保留（转成上面的 LaTeX 上下标，
   如 SO<sup>2−</sup><sub>4</sub> → $\mathrm{SO_4^{2-}}$；错位的上下标按化学常识摆正）。
3. 多余转义符还原：\= → =、\+ → +、\_ → _、\- → -；把 ===== 之类的一串等号还原成 =。
4. 反应条件若被挤到另一行（如单独一行「高温」「△」「80 ℃」），并入对应方程式的箭头上。
5. 图片占位符 ⟦IMGn⟧ **必须原样保留，个数、顺序都不能变**（它代表插图，另行挂载）。
6. 题干不要带题号和分值；选项中不要出现 A． 前缀；表格用 markdown 表格保留。
7. 只输出 JSON 数组，结构与输入一致：{"no":1,"stem":"...","a":"...","b":"...","c":"...","d":"..."}
   （选项键就是 a/b/c/d，不要改名、不要丢项）。

输入：
"""

ANSWER_PROMPT = """下面是一份北京高三化学二模试卷（教师版）的【参考答案与解析】原文。
请提取第 {nos} 题（选择题）的答案字母（A/B/C/D）。

只输出 JSON 对象，形如 {{"1":"A","2":"C","3":"BD"}}；多选题连写，找不到的题不要写。只输出 JSON。"""


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def chat(messages: list, max_tokens: int = 16000, want_json: bool = True, retries: int = 4):
    payload = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.0, "messages": messages}
    if os.getenv("MATH_REASONING", "none").lower() in ("none", "off", "false"):
        payload["reasoning_effort"] = "none"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers,
                              timeout=int(os.getenv("MATH_REQUEST_TIMEOUT", "600")))
            if r.status_code == 400 and "reasoning" in (r.text or "").lower():
                payload.pop("reasoning_effort", None); continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(10 * (attempt + 1)); last = f"HTTP {r.status_code}"; continue
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"].get("content") or "").strip()
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
            if not want_json:
                return txt
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                m = re.search(r"(\[.*\]|\{.*\})", txt, re.S)
                if m:
                    return json.loads(m.group(1))
                raise
        except Exception as e:
            last = str(e)[:150]; time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"调用模型失败：{last}")


def split_paper(txt: str) -> tuple[str, str]:
    for marker in ("参考答案", "答案与解析", "答案解析"):
        i = txt.find(marker)
        if i > 200:
            return txt[:i], txt[i:]
    return txt, ""


def tokenize_images(body: str) -> tuple[str, list[str]]:
    files: list[str] = []

    def repl(m):
        files.append(m.group(1))
        return f"⟦IMG{len(files)}⟧"
    return IMGREF.sub(repl, body), files


def clean_raw(t: str) -> str:
    """确定性清洗：转义还原 + 去掉表格分隔线 / 纯 <br> 行"""
    t = re.sub(r"\\([=+\-_\[\]()])", r"\1", t or "")
    t = re.sub(r"={3,}", "=", t)
    lines = []
    for ln in (t or "").split("\n"):
        s = ln.strip()
        if re.fullmatch(r"\|(\s*:?-{2,}:?\s*\|)+", s):
            continue
        if s in ("<br>", "<br/>", "<br />", ""):
            if not lines or lines[-1] != "":
                lines.append("")
            continue
        lines.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_questions(body: str) -> list[dict]:
    """确定性切题：先确定「哪些行是题号」，再按相邻题号切段（避免把 1.3 MPa 这类小数误当题号后又把后续内容切丢）。"""
    marks = [(m.start(), m.end(), int(m.group(2)), m.group(1)) for m in QSPLIT.finditer(body)]
    if not marks:
        return []

    # 声明题数 N
    n = None
    i1 = body.find("选择题")
    if i1 >= 0:
        m = re.search(r"共\s*(\d+)\s*[小]?\s*题", body[i1:i1 + 80])
        if m:
            n = int(m.group(1))
    if n is None:
        i0 = body.find("第一部分")
        if i0 >= 0:
            m = re.search(r"本部分共\s*(\d+)\s*题", body[i0:i0 + 400])
            if m:
                n = int(m.group(1))
    if n is None:
        tot = re.search(r"选择题[^）\n]{0,10}?共\s*(\d+)\s*分", body)
        per = re.search(r"每\s*小?\s*题\s*(\d+)\s*分", body)
        if tot and per and int(per.group(1)) > 0 and int(tot.group(1)) % int(per.group(1)) == 0:
            n = int(tot.group(1)) // int(per.group(1))

    first_no = marks[0][2]
    limit_no = first_no + n - 1 if n else None
    m2 = re.search(r"(?m)^[ \t#*]*\s*(?:第二部分|二、|非选择题)", body)
    part2 = m2.start() if m2 else -1

    # 第一遍：挑出真正的题号行
    chosen: list[tuple[int, int, int, str]] = []   # (start, end, no, prefix)
    last = 0
    for idx, (start, e, no, prefix) in enumerate(marks):
        if no <= last and no == 1 and idx + 1 < len(marks) and marks[idx + 1][2] == last + 2:
            no = last + 1                    # Word 把「10．」转成了编号列表「1.」
        if no <= last:
            continue
        if limit_no is not None and no > limit_no:
            break
        if part2 > 0 and start > part2:
            break
        if chosen and no - last > 3:
            break
        chosen.append((start, e, no, prefix))
        last = no

    out: list[dict] = []
    for i, (start, e, no, prefix) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(body)
        seg = (prefix or "") + body[e:end]
        seg = re.split(r"(?m)^[ \t]*第[二2]部分", seg)[0]
        stem, opts = split_options(seg)
        filled = [k for k, v in opts.items() if v.strip() or TOKEN.search(v)]
        if len(filled) < 2:
            if filled or not TOKEN.search(seg):
                continue
            opts = {}
        imgs_here = [int(x) for x in TOKEN.findall(seg)]
        out.append({"no": no, "stem": stem, "opts": opts, "img_idx": imgs_here})

    # 末尾连续「没有 A—D 选项、只有插图」的题是非选择题
    k = 0
    while k < len(out) and not out[-1 - k]["opts"]:
        k += 1
    if k >= 2:
        out = out[: len(out) - k]
    if n and 0 < n <= len(out):
        out = out[:n]
    if out:
        miss = [x for x in range(out[0]["no"], out[-1]["no"] + 1) if x not in {o["no"] for o in out}]
        if miss:
            out[0]["_missing"] = miss
    return out


def split_options(seg: str) -> tuple[str, dict[str, str]]:
    """把一段切题干与 a/b/c/d 选项

    兼容三种排版：
      ① 选项各自独立成行（最常见，行首可能带插图占位）
      ② 选项在表格的单元格里（| A．… | B．… | C．… | D．… |）
      ③ 选项是表格的行（首列只有 A/B/C/D，其余列是选项内容与研究性表格的表头）
    选项前的插图占位符会跟着选项走，不丢。
    """
    lines = seg.split("\n")

    # ③ 表格行形式：| A | … | … |
    row_idx = [i for i, ln in enumerate(lines) if re.match(r"^\|[ \t\u3000]*\**([A-D])\**[ \t]*\|", ln)]
    if len(row_idx) >= 2:
        hdr = row_idx[0]
        while hdr - 1 >= 0 and lines[hdr - 1].strip().startswith("|"):
            hdr -= 1
        header = [c.strip() for c in lines[hdr].strip().strip("|").split("|")]
        opts = {}
        for i in row_idx:
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            k = re.sub(r"[^A-Da-d]", "", cells[0]).lower()
            parts = []
            for j, c in enumerate(cells[1:], 1):
                if not c:
                    continue
                h = header[j] if j < len(header) else ""
                parts.append(f"{h}：{c}" if h and h != "选项" else c)
            if k:
                opts[k] = "；".join(parts)
        if len([v for v in opts.values() if v.strip()]) >= 2:
            return "\n".join(lines[:hdr]).strip(), expand_inline(opts)

    cells = [(m.start(), m.group(1).lower(), m.group(2)) for m in OPT_CELL.finditer(seg)]
    lines_m = [(m.start(), m.end(), m.group(1), m.group(2).lower()) for m in OPT_LINE.finditer(seg)]
    opts: dict[str, str] = {}
    first = None

    if cells and (not lines_m or cells[0][0] <= lines_m[0][0]):
        first = cells[0][0]
        for _, k, v in cells:
            opts.setdefault(k, v.strip())
    if lines_m and (first is None or lines_m[0][0] < first):
        first = lines_m[0][0]
        blocks = []
        for idx, (pos, mend, prefix, k) in enumerate(lines_m):
            nxt = lines_m[idx + 1][0] if idx + 1 < len(lines_m) else len(seg)
            blocks.append((pos, mend, prefix, k, seg[pos:nxt]))
        for pos, mend, prefix, k, blk in blocks:
            body_txt = blk[mend - pos:].strip()
            txt = (prefix.strip() + body_txt).strip()
            if k not in opts:
                opts[k] = txt

    # 兜底：四个选项挤在同一行（A．… B．… C．… D．…）
    if len([v for v in opts.values() if v.strip()]) < 4 and opts:
        joined = " ".join(v for v in opts.values() if v.strip())
        marks = [(m.start(), m.group(1).lower()) for m in re.finditer(r"(?:^|\s)([A-D])[．.、]", joined)]
        if len(marks) >= 3:
            inline = {}
            for i, (pos, k) in enumerate(marks):
                endp = marks[i + 1][0] if i + 1 < len(marks) else len(joined)
                txt = re.sub(r"^\s*[A-D][．.、]\s*", "", joined[pos:endp]).strip()
                inline.setdefault(k, txt)
            if len([v for v in inline.values() if v]) >= 3:
                opts.update(inline)

    opts = expand_inline(opts)
    stem = seg[:first] if first is not None else seg
    stem = re.sub(r"(?m)^\|[^\n]*\|\s*$\n?\Z", "", stem.strip())
    return stem.strip(), opts


TBL_HEAD = re.compile(r"(?m)^\|[ \t]*A[ \t]*\|[ \t]*B[ \t]*\|[ \t]*C[ \t]*\|[ \t]*D[ \t]*\|\s*$")


def fix_table_options(stem: str, opts: dict[str, str]) -> tuple[str, dict[str, str]]:
    """选项是表格列（表头一行 A|B|C|D，下面各行是内容）时，把每列内容合成一个选项"""
    if not TBL_HEAD.search(stem):
        return stem, opts
    m = TBL_HEAD.search(stem)
    lines = stem[m.start():].split("\n")
    table, rest = [], []
    for i, ln in enumerate(lines):
        if i == 0 or ln.strip().startswith("|"):
            table.append(ln)
        else:
            rest = lines[i:]
            break
    rows = []
    for ln in table:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == 4 and all(c in ("A", "B", "C", "D") for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 1:
        return stem, opts
    new = dict(opts)
    for j in range(4):
        parts = []
        for cells in rows:
            if j < len(cells):
                c = cells[j].strip()
                if c and c not in ("A", "B", "C", "D"):
                    parts.append(c)
        if parts:
            col = "；".join(parts)
            cur = (new.get("abcd"[j]) or "").strip()
            new["abcd"[j]] = col + (cur if cur and cur not in col else "")
    stem2 = (stem[: m.start()] + "\n" + "\n".join(rest)).strip()
    stem2 = re.sub(r"\n{3,}", "\n\n", stem2)
    return stem2, new


def expand_inline(opts: dict[str, str]) -> dict[str, str]:
    """把「A．… B．…」挤在同一行的选项拆开（只在目标槽位为空时填，避免覆盖）"""
    out = dict(opts)
    for k in "abc":
        t = out.get(k) or ""
        if not t:
            continue
        hit = None
        for mm in re.finditer(r"([A-D])[^\S\n]*[．.、]", t):
            if mm.group(1).lower() > k:
                hit = mm
                break
        if not hit:
            continue
        left = t[: hit.start()].strip()
        right = t[hit.end():].strip()
        nk = hit.group(1).lower()
        out[k] = left
        if not (out.get(nk) or "").strip() and right:
            out[nk] = right
    return out


def wmf_to_png(paths: list[Path]) -> None:
    todo = [p for p in paths if p.suffix.lower() == ".wmf" and not p.with_suffix(".png").exists()]
    if not todo or not (WMF_EX / "usr/bin/wmf2gd").exists():
        return
    env = dict(os.environ, LD_LIBRARY_PATH=str(WMF_EX / "usr/lib/x86_64-linux-gnu"))
    for p in todo:
        try:
            subprocess.run([str(WMF_EX / "usr/bin/wmf2gd"), "-t", "png", "--maxwidth=1600",
                            "-o", str(p.with_suffix(".png")), str(p)],
                           capture_output=True, timeout=60, env=env)
        except Exception:
            pass


def prepare_images(md_dir: Path, stem_out: str, type_dir: str, subject: str, files: list[str]) -> dict[str, str]:
    dst_dir = MD_ROOT / type_dir / subject / stem_out / "images"
    mapping: dict[str, str] = {}
    need = []
    for ref in files:
        src = md_dir / ref
        if src.suffix.lower() == ".wmf":
            need.append(src)
    wmf_to_png(need)
    for ref in files:
        src = md_dir / ref
        if src.suffix.lower() in (".wmf", ".emf"):
            src = src.with_suffix(".png")
        if not src.exists():
            continue
        mapping[ref] = src.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        if not (dst_dir / src.name).exists():
            shutil.copyfile(src, dst_dir / src.name)
    return mapping


def md_answer_table(ans: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in re.finditer(r"\|\s*题号\s*\|([^\n]*)\n\|\s*-+[^\n]*\n\|\s*答案\s*\|([^\n]*)", ans):
        nos = [x.strip() for x in m.group(1).split("|") if x.strip()]
        vals = [x.strip() for x in m.group(2).split("|") if x.strip()]
        for n, v in zip(nos, vals):
            v = re.sub(r"[^A-D]", "", v.upper())
            if n.isdigit() and v:
                out[int(n)] = v
    return out


def md_answer_segments(ans: str) -> dict[int, str]:
    segs: dict[int, str] = {}
    marks = [(m.start(), m.end(), int(m.group(1))) for m in
             re.finditer(r"(?m)^\s*\**\s*(\d{1,2})\s*\\?[．.]\s*", ans)]
    for i, (_, e, no) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(ans)
        segs.setdefault(no, ans[e:end])
    return segs


def answers_from_md(ans: str, nos: list[int]) -> dict[int, str]:
    got = md_answer_table(ans)
    if all(n in got for n in nos):
        return got
    segs = md_answer_segments(ans)
    for n in nos:
        if n in got:
            continue
        seg = segs.get(n, "")
        m = re.search(r"故选\s*[：:]?\s*([A-Da-d][A-Da-d\s、，,]*)", seg) or \
            re.search(r"(?:正确)?答案\s*[：:]\s*([A-D]{1,4})\b", seg)
        if m:
            v = re.sub(r"[^A-D]", "", m.group(1).upper())
            if v:
                got[n] = v
    return got


def parse_choice_score(body: str) -> int:
    m = re.search(r"每\s*小?\s*题\s*(\d+)\s*分", body) or re.search(r"选择题[^。\n]{0,20}?每\s*题\s*(\d+)\s*分", body)
    try:
        return int(m.group(1))
    except Exception:
        return 3


OPT_OCR_PROMPT = r"""这是一道北京高三化学二模选择题的【选项图片】（图片里可能是 A—D 四行、2×2 四宫格或一张表格）。
请把四个选项分别转录成文字：

{"a":"...","b":"...","c":"...","d":"..."}

要求：逐字照抄，不要带 A．/B． 这类前缀；化学式/离子/方程式用 LaTeX（$\mathrm{H_2O}$、$\mathrm{SO_4^{2-}}$）；
图片里确实是空白的选项给空字符串；只输出 JSON。"""


def ocr_options(imgs: list[Path]) -> dict[str, str]:
    """选项整块是图片时，直接让模型读图转录四个选项"""
    content = [{"type": "text", "text": OPT_OCR_PROMPT}]
    for p in imgs[:2]:
        b64 = base64.b64encode(p.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}})
    try:
        got = chat([{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content}], max_tokens=3000)
    except Exception:
        return {}
    if not isinstance(got, dict):
        return {}
    out = {}
    for k in "abcd":
        v = str(got.get(k) or got.get(k.upper()) or "").strip()
        if v:
            out[k] = v
    return out


SUB_MAP = str.maketrans("0123456789+-=()nmxaehijkloprstuv", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙₘₓₐₑₕᵢⱼₖₗₒₚᵣₛₜᵤᵥ")
SUP_MAP = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")


def html_fallback(t: str) -> str:
    """模型排版校验没过时的兜底：<sub>/<sup> 转 Unicode 上下标（浏览器直接能显示）"""
    def sub(m):
        inner = m.group(1).replace("−", "-").replace("－", "-").replace(" ", "")
        conv = inner.translate(SUB_MAP)
        return conv if conv != inner or all(c in "0123456789+-=()nmxaehijkloprstuv" for c in inner) else inner

    def sup(m):
        inner = m.group(1).replace("−", "-").replace("－", "-").replace(" ", "")
        return inner.translate(SUP_MAP)
    t = re.sub(r"<sub>(.*?)</sub>", sub, t or "", flags=re.S)
    t = re.sub(r"<sup>(.*?)</sup>", sup, t, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t)
    return clean_raw(t)


def trim_tail(text: str, no: int) -> str:
    """去掉选项/题干末尾被吞进来的「下一题题号」「第二部分」等（Word 转 MD 时常见）"""
    out = []
    for ln in (text or "").split("\n"):
        if re.match(r"^[ \t\u3000]*\**[ \t]*(?:第[二2]部分|二、|非选择题)", ln):
            break
        m = re.match(r"^[ \t\u3000]*\**[ \t]*(\d{1,2})[ \t]*[．.]", ln)
        if m and no < int(m.group(1)) <= no + 30:
            break
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip().strip("*").strip()


def tokens_of(s: str) -> list[str]:
    return TOKEN.findall(s or "")


def nchars(s: str) -> int:
    return len(re.sub(r"\s|⟦IMG\d+⟧|!\[[^\]]*\]\([^)]*\)|[$\\{}]", "", s or ""))


def format_pass(items: list[dict], paper_hint: str) -> list[dict]:
    """一次性把整卷的题干/选项交给模型做排版转换，返回同结构结果"""
    payload = [{"no": it["no"], "stem": it["stem"], **{k: it["opts"].get(k, "") for k in "abcd"}} for it in items]
    got = chat([{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": FORMAT_PROMPT + f"（试卷：{paper_hint}）\n"
                 + json.dumps(payload, ensure_ascii=False)}], max_tokens=20000)
    if isinstance(got, dict):
        got = got.get("questions") or got.get("data") or []
    by_no = {}
    for g in got if isinstance(got, list) else []:
        try:
            by_no[int(g.get("no"))] = g
        except Exception:
            continue
    # 保真校验：占位符个数/顺序一致、非空白字符数量不下滑
    out = []
    for it in items:
        g = by_no.get(it["no"])
        res = {"no": it["no"], "stem": it["stem"], "opts": dict(it["opts"]), "bad": [],
               "img_idx": it.get("img_idx", [])}
        if not g:
            res["bad"].append("模型漏题"); out.append(res); continue
        pairs = [("stem", it["stem"], str(g.get("stem") or ""))] + \
                [(k, it["opts"].get(k, ""), str(g.get(k) or "")) for k in "abcd"]
        for key, raw, new in pairs:
            raw_ok = tokens_of(raw) == tokens_of(new)
            len_ok = nchars(new) >= max(2, int(nchars(html_fallback(raw)) * 0.8))
            if new.strip() and raw_ok and len_ok:
                if key == "stem":
                    res["stem"] = new.strip()
                else:
                    res["opts"][key] = new.strip()
            elif raw.strip():
                res["bad"].append(key)
                fixed = html_fallback(raw)
                if key == "stem":
                    res["stem"] = fixed
                else:
                    res["opts"][key] = fixed
        out.append(res)
    return out


def convert_one(md_file: Path, type_dir: str, subject: str, args) -> str:
    stem = re.sub(r"（教师版）|\(教师版\)|含答案|（反馈卷）|反馈卷", "", md_file.stem).strip()
    stem = re.sub(r"\s+", "", stem)
    dst = JSON_ROOT / type_dir / subject / f"{stem}.json"
    review_path = dst.with_suffix(".review.txt")
    if dst.exists() and not args.force:
        return f"跳过（已存在）：{stem}"

    txt = md_file.read_text(encoding="utf-8")
    txt = re.sub(r"<su[bp]>\s*([．.、])\s*</su[bp]>", r"\1", txt)   # 标点被包进 sub/sup 的情况
    body, ans = split_paper(txt)
    body_tok, img_files = tokenize_images(body)
    items = parse_questions(body_tok)
    if not items:
        raise RuntimeError("没切出选择题")

    gap_nos = items[0].get("_missing") if items else None
    for it in items:
        it["stem"] = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]\s*", "", clean_raw(it["stem"]))
        it["stem"] = trim_tail(it["stem"], it["no"])
        it["opts"] = {k: trim_tail(v, it["no"]) for k, v in it["opts"].items()}
        it["stem"], it["opts"] = fix_table_options(it["stem"], it["opts"])
        it["opts"] = {k: clean_raw(v) for k, v in it["opts"].items()}

    bad_total = 0
    try:
        items = format_pass(items, stem)
        bad_total = sum(len(it.get("bad") or []) for it in items)
    except Exception as e:
        log(f"  {stem}: 排版转换失败，用原文兜底 {str(e)[:80]}")
        for it in items:
            it["bad"] = ["ai-failed"]

    img_map = prepare_images(md_file.parent, stem, type_dir, subject, img_files)

    def resolve(text: str) -> str:
        def repl(m):
            idx = int(m.group(1))
            if 1 <= idx <= len(img_files):
                name = img_map.get(img_files[idx - 1])
                if name:
                    return f"![](images/{name})"
            return ""
        return TOKEN.sub(repl, text)

    nos = [it["no"] for it in items]
    # 答案表常常排在「参考答案」标题的上一页（页眉切换导致），所以答案表要在全文里找
    answers = {k: v for k, v in md_answer_table(txt).items() if k in nos}
    for k, v in answers_from_md(ans, nos).items():
        answers.setdefault(k, v)
    missing = [n for n in nos if n not in answers]
    if missing and args.ai_answers and ans.strip():
        try:
            got = chat([{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": ANSWER_PROMPT.format(nos="、".join(map(str, missing)))
                         + "\n原文：\n" + ans[:40000]}], max_tokens=2000)
            if isinstance(got, dict):
                for k, v in got.items():
                    try:
                        answers[int(k)] = re.sub(r"[^A-D]", "", str(v).upper())
                    except Exception:
                        continue
        except Exception as e:
            log(f"  {stem}: AI 取答案失败 {str(e)[:80]}")

    score = parse_choice_score(body)
    tot = re.search(r"选择题[^）\n]{0,10}?共\s*(\d+)\s*分", body)
    if tot and items and int(tot.group(1)) % len(items) == 0:
        score = int(tot.group(1)) // len(items)
    review = []
    if gap_nos:
        review.append(f"缺题号（切题没切到）：{gap_nos}")
    out_qs = []
    for it in items:
        opts = [{"id": k, "label": resolve(it["opts"].get(k, "")).strip()} for k in "abcd"]
        miss = [o["id"] for o in opts if not o["label"]]
        stem_txt = resolve(it["stem"])
        if miss:
            # 选项整块是图片（Word 里贴图）→ 读图转录；仍不行就把图四等分挂上去
            cand = [img_map.get(img_files[i - 1]) for i in it.get("img_idx", []) if 1 <= i <= len(img_files)]
            cand = [n for n in cand if n]
            cand_paths = [MD_ROOT / type_dir / subject / stem / "images" / n for n in cand]
            cand_paths = [p for p in cand_paths if p.exists()]
            got_opts = ocr_options(cand_paths[-2:]) if cand_paths else {}
            filled = 0
            for o in opts:
                if not o["label"] and got_opts.get(o["id"]):
                    o["label"] = got_opts[o["id"]]
                    filled += 1
            if filled:
                # 选项已转录成文字，题干里就别再重复挂那张选项图
                used = {f"![]({'images/' + n})" for n in cand[-1:]}
                for u in used:
                    stem_txt = stem_txt.replace(u, "")
                stem_txt = re.sub(r"\n{3,}", "\n\n", stem_txt).strip()
            elif cand_paths:
                src_img = cand_paths[-1]
                dst_dir = MD_ROOT / type_dir / subject / stem / "images"
                for i, o in enumerate([x for x in opts if not x["label"]]):
                    name = f"{stem}_q{it['no']}_{o['id']}{src_img.suffix}"
                    if not getattr(args, "dry_run", False):
                        subprocess.run(["convert", str(src_img), "-crop", f"100%x25%+0+{i * 25}%", "+repage",
                                        str(dst_dir / name)], capture_output=True)
                    o["label"] = f"![](images/{name})"
                review.append(f"第{it['no']}题 选项图为整块图片，已四等分裁切（需人工核对）")
            miss = [o["id"] for o in opts if not o["label"]]
            if miss:
                review.append(f"第{it['no']}题 选项缺失：{'/'.join(miss)}")
        answer = answers.get(it["no"], "")
        if not answer:
            review.append(f"第{it['no']}题 缺答案")
        if it.get("bad"):
            review.append(f"第{it['no']}题 排版未通过校验：{'/'.join(it['bad'])}")
        out_qs.append({
            "id": it["no"], "questionType": "single", "content": stem_txt,
            "options": opts, "answer": answer, "analysis": "", "score": score,
        })

    year_m = re.search(r"(20\d{2})", stem)
    paper = {
        "title": stem, "subject": subject, "source": stem,
        "grade": args.grade, "year": int(year_m.group(1)) if year_m else None,
        "totalScore": score * len(out_qs) or None, "duration": 90,
        "tags": [subject, args.grade, "二模"] + ([year_m.group(1)] if year_m else []),
    }
    sections = [{
        "type": "选择题", "questionType": "chemistry-choice", "title": "第一部分　选择题",
        "category": "选择", "score": score * len(out_qs), "questions": out_qs,
    }]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"paper": paper, "sections": sections}, ensure_ascii=False, indent=2), encoding="utf-8")
    if review:
        review_path.write_text("\n".join(review), encoding="utf-8")
    elif review_path.exists():
        review_path.unlink()
    return f"✓ {stem}（{len(out_qs)} 题，复核 {len(review)} 处，排版校验未过 {bad_total} 项）"


def main():
    ap = argparse.ArgumentParser(description="化学试卷 MD → JSON（只处理选择题）")
    ap.add_argument("-p", "--path", help="单份：MD 目录或 MD 文件")
    ap.add_argument("--dir", default=str(MD_ROOT))
    ap.add_argument("-T", "--type", default="二模")
    ap.add_argument("-s", "--subject", default="化学")
    ap.add_argument("-g", "--grade", default="高三")
    ap.add_argument("-w", "--concurrency", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-ai-answers", dest="ai_answers", action="store_false")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.dir) / args.type / args.subject
    if args.path:
        p = Path(args.path)
        files = sorted(p.glob("*.md"))[:1] if p.is_dir() else [p]
    else:
        files = [sorted(d.glob("*.md"))[0] for d in sorted(root.iterdir())
                 if d.is_dir() and sorted(d.glob("*.md"))]
    if args.limit:
        files = files[:args.limit]
    log(f"共 {len(files)} 份 MD")

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(convert_one, f, args.type, args.subject, args): f for f in files}
        for fut in as_completed(futs):
            try:
                log("  " + fut.result())
            except Exception as e:
                log(f"  ✗ {futs[fut].parent.name}: {str(e)[:160]}")


if __name__ == "__main__":
    main()
