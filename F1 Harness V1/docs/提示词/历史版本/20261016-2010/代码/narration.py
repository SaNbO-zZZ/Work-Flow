"""配音稿整理（M5-2）：G1 定稿逐字稿 → 粘进剪映的纯文本 + 单元列表。

剪映按标点把全文拆成多段配音，所以在每个保留标点后切单元，任何一段配音都正好等于连续若干单元。
say 是朗读文本，show 是字幕文本；读音替换只改 say，字幕不会出现同音错字。
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .jianying import MAX_CHARS

READINGS = Path(__file__).parent / "data" / "读音表.json"
PUNCT = "。！？，、"
STOPS = "。！？"
DIG = "零一二三四五六七八九"
PH = 0xE000  # 英文词占位符（私用区字符），防止数字转换和标点规整碰到英文词
# 括号里以这些词开头的算舞台提示，整段删
STAGE = re.compile(r"^\s*(画面|镜头|字幕|配图|图示|图解|叠层|叠图|音效|BGM|音乐|停顿|转场|素材|插入|切到|切换|"
                   r"特写|空镜|注[:：]|备注|此处)", re.I)
# 汉字之间的空格当逗号：写手常用空格代替逗号断句，删掉会让剪映收到无标点长句、乱切停顿
CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")
WORD = r"[A-Za-z][A-Za-z0-9]*(?:(?:[ \-]|\. ?)[A-Za-z][A-Za-z0-9]*)*"


@dataclass
class Prepared:
    text: str
    units: list[dict]
    warnings: list[str] = field(default_factory=list)


def load_readings(path: Path | None = None) -> dict:
    data = json.loads(Path(path or READINGS).read_text("utf-8"))
    return {"keep": list(data.get("keep", [])), "say": dict(data.get("say", {}))}


# ---------- 数字读法 ----------

def digits(s: str, zero: str = "零") -> str:
    return "".join(zero if c == "0" else DIG[int(c)] for c in s)


def _small(n: int) -> str:
    s, zero = "", False
    for size, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d = n // size % 10
        if d:
            s += ("零" if zero else "") + DIG[d] + unit
            zero = False
        elif s:
            zero = True
    return s


def num_int(n: int) -> str:
    if n == 0:
        return "零"
    if n >= 10 ** 12:
        return digits(str(n))
    out, prev, gap = "", None, False
    for size, unit in ((10 ** 8, "亿"), (10 ** 4, "万"), (1, "")):
        q = n // size % 10000
        if not q:
            gap = bool(out)
            continue
        if out and (q < 1000 or gap or prev % 10 == 0):
            out += "零"
        out += _small(q) + unit
        prev, gap = q, False
    return out[1:] if out.startswith("一十") else out


def num(s: str) -> str:
    whole, _, frac = s.partition(".")
    return num_int(int(whole)) + ("点" + digits(frac) if frac else "")


# 英文保护之前做（单位和 P1/FP1 本身含字母）
PRE_RULES = [
    (re.compile(r"(?<![A-Za-z])km/h(?![A-Za-z])", re.I), lambda m: "公里每小时"),
    (re.compile(r"°\s*C(?![A-Za-z])|℃|°"), lambda m: "度"),
    (re.compile(r"(?<![A-Za-z])kg(?![A-Za-z])", re.I), lambda m: "公斤"),
    (re.compile(r"(?<![A-Za-z])km(?![A-Za-z])", re.I), lambda m: "公里"),
    (re.compile(r"(?<![A-Za-z0-9])FP([1-3])(?![A-Za-z0-9])"), lambda m: "一二三"[int(m[1]) - 1] + "练"),
    (re.compile(r"(?<![A-Za-z0-9])P(\d{1,2})(?![A-Za-z0-9])"), lambda m: f"第{num_int(int(m[1]))}名"),
]

NUM_RULES = [
    # 圈速 1:30.076 / 1分30秒076
    (re.compile(r"(\d+):(\d{2})\.(\d{1,3})"), lambda m: f"{num_int(int(m[1]))}分{num_int(int(m[2]))}秒{digits(m[3])}"),
    (re.compile(r"(\d+)分(\d+)秒(\d{3})"), lambda m: f"{num_int(int(m[1]))}分{num_int(int(m[2]))}秒{digits(m[3])}"),
    # 时刻 16:00 / 16:30
    (re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?![\d.])"),
     lambda m: f"{num_int(int(m[1]))}点" + ("" if m[2] == "00" else
                                          ("零" if m[2][0] == "0" else "") + num_int(int(m[2])) + "分")),
    (re.compile(r"[+＋](?=\d[\d.]*秒)"), lambda m: ""),
    (re.compile(r"(?<!\d)(\d{4})(?=年|赛季)"), lambda m: digits(m[1], "〇")),
    (re.compile(r"[#＃](\d+)"), lambda m: num(m[1]) + "号"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[%％]"), lambda m: "百分之" + num(m[1])),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[-~～–—]\s*(?=\d)"), lambda m: num(m[1]) + "到"),
    (re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+)(?![\d,])"), lambda m: m[1].replace(",", "")),
]
GENERIC = re.compile(r"\d+(?:\.\d+)?")


def _numbers(line: str, warn) -> str:
    for rx, fn in NUM_RULES:
        line = rx.sub(fn, line)

    def generic(m):
        before = line[m.start() - 1] if m.start() else ""
        after = line[m.end()] if m.end() < len(line) else ""
        if before != "第" and not re.match(r"[\u4e00-\u9fff]", after):
            warn(f"数字读法未确认：{m[0]}")
        if m[0] == "2" and before != "第" and after and after in LIANG:
            return "两"
        return num(m[0])
    return GENERIC.sub(generic, line)


# 单独的 2 后面跟量词读"两"（两圈、两名），"第2""2号"仍读"二"
LIANG = set("圈次个位名年周天支辆套站场轮届款种家倍条台分秒小万千百")


# ---------- 译名 ----------

def _glossary(conn: sqlite3.Connection | None) -> dict[str, str | None]:
    """原名（小写）→ 已审译名，未审为 None；车手另按姓氏查。已审优先。"""
    out: dict[str, str | None] = {}
    if conn is None:
        return out

    def put(key, zh):
        if out.get(key) is None:
            out[key] = zh
    rows = conn.execute("SELECT kind, original, approved, state FROM glossary").fetchall()
    for r in rows:
        put(r["original"].strip().lower(), r["approved"] if r["state"] == "approved" and r["approved"] else None)
    for r in rows:
        if r["kind"] == "车手" and " " in r["original"].strip():
            last = r["original"].split()[-1].lower()
            put(last, r["approved"] if r["state"] == "approved" and r["approved"] else None)
    return out


# ---------- 文本清理 ----------

def _clean_line(line: str) -> str:
    line = line.strip()
    if (not line or line.startswith(("|", "```")) or re.match(r"#{1,6}(\s|$)", line)
            or re.fullmatch(r"([-*_=]\s*){3,}", line)):
        return ""
    line = re.sub(r"^(>\s*)+", "", line)
    line = re.sub(r"^([-*+•]|\d+[.、)）])\s+", "", line)
    return re.sub(r"\*\*|__|~~|[*`]", "", line)


def _parens(line: str) -> str:
    rx = re.compile(r"[（(【]([^（）()【】]*)[）)】]")
    while True:
        new = rx.sub(lambda m: "" if STAGE.match(m[1]) else m[1], line)
        if new == line:
            return line
        line = new


def _punct(line: str) -> str:
    line = re.sub(r"[「」“”‘’《》『』\"']", "", line)
    line = re.sub(r"\.{3,}|[：:；;—–…~～]+", "，", line)
    line = line.translate(str.maketrans({",": "，", "!": "！", "?": "？", ".": "。"}))
    line = re.sub(r"\s+", "", line)
    # 连续标点合并：有句末标点取第一个句末标点，否则逗号优先
    line = re.sub(r"[。！？，、]{2,}", lambda m: next((c for c in m[0] if c in STOPS), "，" if "，" in m[0] else "、"), line)
    line = line.lstrip(PUNCT)
    if line and line[-1] not in PUNCT:
        line += "。"
    return line


def prepare(script: str, conn: sqlite3.Connection | None = None) -> Prepared:
    readings = load_readings()
    keep = {k.lower() for k in readings["keep"]}
    say_map = sorted(readings["say"].items(), key=lambda kv: -len(kv[0]))
    names = _glossary(conn)
    keep_alt = "|".join(re.escape(k) for k in sorted(readings["keep"], key=len, reverse=True))
    latin = re.compile((rf"(?<![A-Za-z0-9])(?:{keep_alt})(?![A-Za-z0-9])|" if keep_alt else "")
                       + rf"(?<![A-Za-z0-9]){WORD}", re.I)
    warnings: dict[str, None] = {}
    tokens: list[str] = []

    def warn(msg):
        warnings[msg] = None

    def protect(m):
        w = m[0]
        rep = w
        if w.lower() not in keep and not re.search(r"\d", w):  # 含数字的是车型/代号，原样保留
            rep = names.get(w.lower()) or w
            if rep == w:
                warn(f"未审译名：{w}")
        tokens.append(rep)
        return chr(PH + len(tokens) - 1)

    def restore(s):
        return re.sub(r"[\ue000-\uf8ff]", lambda m: tokens[ord(m[0]) - PH], s)

    if re.search(r"\[AD\]", script, re.I):
        warn("稿中有广告位 [AD]，标记已删，请确认广告文案已写进正文")
    text = re.sub(r"<!--.*?-->", "", script, flags=re.S)
    text = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", text)   # markdown 链接留文字
    text = re.sub(r"\[[^\[\]\n]*\]", "", text)                   # [F12] [S3] [AD] 等标记

    units: list[dict] = []
    paras: list[str] = []
    for raw in text.splitlines():
        line = _parens(_clean_line(raw))
        line = CJK_GAP.sub("，", line)
        for rx, fn in PRE_RULES:
            line = rx.sub(fn, line)
        line = latin.sub(protect, line)
        line = _punct(_numbers(line, warn))
        if not line:
            continue
        para = len(paras)
        says = []
        for piece in re.findall(rf"[^{PUNCT}]+[{PUNCT}]", line):
            show = restore(piece)
            say = show
            for k, v in say_map:
                say = say.replace(k, v)
            units.append({"say": say, "show": show, "para": para})
            says.append(say)
        paras.append("".join(says))

    out = "\n".join(paras)
    if not out:
        warn("配音稿为空")
    if len(out) > MAX_CHARS:
        warn(f"配音稿 {len(out)} 字，超过剪映上限 {MAX_CHARS} 字")
    return Prepared(out, units, list(warnings))


def write(prepared: Prepared, voice_dir: Path) -> None:
    voice_dir = Path(voice_dir)
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "配音稿.txt").write_text(prepared.text, "utf-8")
    (voice_dir / "配音稿.json").write_text(
        json.dumps({"units": prepared.units, "warnings": prepared.warnings}, ensure_ascii=False, indent=2), "utf-8")
