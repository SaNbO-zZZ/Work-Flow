"""封面「米白拼贴」版式（用户 2026-10-16 定，照原 Codex F1 智能体做的封面）：
横版 4:3（1920×1440）、竖版 3:4（1440×1920）；米白纸底、主角车队色刷痕、真人头肩抠图；
文字全由程序用思源黑体排：小标签 + 车队色横线、主标题（一行车队色）、副标题；
底部前瞻、复盘放“本期概要”面板，资讯、科普放“ALPHA PULSE F1 · 日期”细条；右上角放频道圆标。

底图两种来源：AI 封面让图像模型画不带字的拼贴底图（aicover）；模板封面由本模块用一张真实画面拼出同风格的底图。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from PIL.PngImagePlugin import PngInfo

from . import config, cover, episodes

SIZES = {"landscape": (1920, 1440), "portrait": (1440, 1920)}
CHANNEL = "ALPHA PULSE F1"
PAPER = (239, 233, 220)
INK = (20, 32, 38)
PANEL = (16, 28, 34)
CREAM = (251, 246, 233)
MUTED = (214, 220, 224)
DEFAULT_ACCENT = "#E10600"
# 封面用的车队色：在米白纸上当大字要看得清，比计时色深一些（红牛按样张用红橙，梅奔用深青绿）
TEAM_ACCENT = {
    "红牛": "#E5400F", "梅赛德斯": "#0E8F86", "法拉利": "#D8141F", "迈凯伦": "#E05E00",
    "阿斯顿·马丁": "#1D7A5C", "Alpine": "#0A78C2", "威廉姆斯": "#1660D0", "Racing Bulls": "#2F55D4",
    "哈斯": "#C8102E", "奥迪": "#C40D2E", "凯迪拉克": "#A0822D",
}
# 认车队的说法（长的在前，“小红牛”要先于“红牛”）
TEAM_ALIASES = [
    ("Racing Bulls", "Racing Bulls"), ("小红牛", "Racing Bulls"), ("RB", "Racing Bulls"),
    ("阿斯顿·马丁", "阿斯顿·马丁"), ("阿斯顿马丁", "阿斯顿·马丁"), ("阿斯顿", "阿斯顿·马丁"),
    ("梅赛德斯", "梅赛德斯"), ("梅奔", "梅赛德斯"), ("红牛", "红牛"), ("法拉利", "法拉利"), ("迈凯伦", "迈凯伦"),
    ("Alpine", "Alpine"), ("阿尔派", "Alpine"), ("威廉姆斯", "威廉姆斯"), ("哈斯", "哈斯"),
    ("奥迪", "奥迪"), ("索伯", "奥迪"), ("凯迪拉克", "凯迪拉克"),
]
# 2026 赛季车手 → 车队（标题里常只写车手名）
DRIVER_TEAM = {
    "维斯塔潘": "红牛", "维斯塔彭": "红牛", "哈贾尔": "红牛", "拉塞尔": "梅赛德斯", "安东内利": "梅赛德斯",
    "Kimi": "梅赛德斯", "勒克莱尔": "法拉利", "汉密尔顿": "法拉利", "诺里斯": "迈凯伦", "皮亚斯特里": "迈凯伦",
    "阿隆索": "阿斯顿·马丁", "斯特罗尔": "阿斯顿·马丁", "加斯利": "Alpine", "科拉皮托": "Alpine",
    "塞恩斯": "威廉姆斯", "阿尔本": "威廉姆斯", "劳森": "Racing Bulls", "林德布拉德": "Racing Bulls",
    "奥康": "哈斯", "贝尔曼": "哈斯", "霍肯伯格": "奥迪", "博托莱托": "奥迪", "佩雷兹": "凯迪拉克", "博塔斯": "凯迪拉克",
}
SUMMARY_BRANCHES = ("前瞻", "复盘")
BJ = timezone(timedelta(hours=8))

# 坐标按成品尺寸（照原智能体 cover-layout.json）：边距、文字栏宽、标题区底线、字号上下限
LAYOUT = {
    "landscape": {"x": 80, "label_y": 92, "label": 44, "col": 760, "title_top": 250, "title_max": 177,
                  "title_min": 88, "text_bottom": None, "sub": 56, "logo": 150, "bar": 108, "panel": 272,
                  "summary_x": 80},
    "portrait": {"x": 80, "label_y": 76, "label": 60, "col": 1150, "title_top": 232, "title_max": 200,
                 "title_min": 96, "text_bottom": 790, "sub": 50, "logo": 150, "bar": 108, "panel": 272,
                 "summary_x": 80},
}


@dataclass
class Copy:
    """封面文字。lines 是主标题各行，accent 是哪一行用车队色（负数从后数）。"""
    label: str
    lines: list[str]
    accent: int = -1
    sub: list[str] = field(default_factory=list)
    summary: dict | None = None      # {"head": "红牛冲连胜 · 梅奔调升级", "points": ["赛道特点", ...]}
    date: str | None = None          # 底栏日期，如“10月15日”
    team: str | None = None
    branch: str = "资讯"


# ---------- 文字 ----------

def team_of(text: str) -> str | None:
    """标题里最先提到的车队（车手名也算）；同一位置长的说法优先（“小红牛”不认成“红牛”）。"""
    text = text or ""
    hits = [(text.find(a), -len(a), t) for a, t in [*TEAM_ALIASES, *DRIVER_TEAM.items()] if a in text]
    return min(hits)[2] if hits else None


def accent_of(team: str | None) -> tuple[int, int, int]:
    return cover._rgb(TEAM_ACCENT.get(team or "", DEFAULT_ACCENT))


def today() -> str:
    d = datetime.now(BJ)
    return f"{d.month}月{d.day}日"


def label_for(branch: str, topic: str, year: int | None = None) -> str:
    """前瞻、复盘：“2026 F1 · 奥斯汀前瞻”；资讯、科普：“F1资讯 / 红牛官宣”。"""
    year = year or datetime.now(BJ).year
    if branch in SUMMARY_BRANCHES:
        return f"{year} F1 · {topic}" if topic else f"{year} F1 · {branch}"
    return f"F1{branch} / {topic}" if topic else f"F1{branch}"


def copy_from_title(branch: str | None, publish_title: str, gp: str | None = None, *,
                    date: str | None = None) -> Copy:
    """没有 AI 文案时按发布标题排：“丨”或第一个空格前是主标题、后面是副标题。"""
    branch = branch or "资讯"
    t = re.sub(r"\s*[丨｜|]\s*", "丨", publish_title.strip())
    head, _, tail = t.partition(" ") if "丨" not in t else t.partition("丨")
    gp_short = (gp or "").removesuffix("大奖赛")
    topic = f"{gp_short}{branch}" if branch in SUMMARY_BRANCHES and gp_short else (gp or "")
    sub = [s for s in re.split(r"\s+", tail.replace("丨", " ").strip()) if s][:2]
    summary = {"head": " · ".join(sub), "points": []} if branch in SUMMARY_BRANCHES and sub else None
    return Copy(label=label_for(branch, topic), lines=[head.strip()], sub=[] if summary else sub, summary=summary,
                date=date or today(), team=team_of(publish_title), branch=branch)


# ---------- 底图 ----------

def _rng(key: str) -> random.Random:
    return random.Random(int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16))


def paper(size, rng: random.Random) -> Image.Image:
    """米白纸：底色 + 细颗粒 + 大块浅色斑。"""
    W, H = size
    np_rng = np.random.default_rng(rng.randrange(1 << 30))
    base = np.empty((H, W, 3), dtype=np.float32)
    base[:] = PAPER
    blot = np_rng.normal(0, 1, (max(2, H // 160), max(2, W // 160)))
    blot = np.asarray(Image.fromarray(((blot - blot.min()) / (np.ptp(blot) or 1) * 255).astype(np.uint8))
                      .resize((W, H), Image.BICUBIC), dtype=np.float32) / 255 - 0.5
    grain = np_rng.normal(0, 4.0, (H, W))
    base += (blot * 10 + grain)[..., None]
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def _rough_band(size, p0, p1, width, rng: random.Random, jag=0.18) -> Image.Image:
    """沿 p0→p1 的刷痕蒙版：两条边随机抖动，再撒些飞白点。"""
    W, H = size
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    (x0, y0), (x1, y1) = p0, p1
    L = math.hypot(x1 - x0, y1 - y0) or 1
    nx, ny = -(y1 - y0) / L, (x1 - x0) / L
    n = 48
    top, bot = [], []
    for i in range(n + 1):
        t = i / n
        cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        taper = 0.35 + 0.65 * math.sin(math.pi * min(1, t * 1.1)) if t < 0.95 else 0.2
        w1 = width / 2 * taper * (1 + rng.uniform(-jag, jag))
        w2 = width / 2 * taper * (1 + rng.uniform(-jag, jag))
        top.append((cx + nx * w1, cy + ny * w1))
        bot.append((cx - nx * w2, cy - ny * w2))
    d.polygon(top + bot[::-1], fill=255)
    for _ in range(int(L / 6)):
        t = rng.random()
        off = rng.uniform(-1, 1) * width * 0.75
        cx, cy = x0 + (x1 - x0) * t + nx * off, y0 + (y1 - y0) * t + ny * off
        r = rng.uniform(1, 5)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255 if abs(off) < width * 0.6 else 180)
    # 顺着笔向的细纹（干刷感）
    arr = np.asarray(m, dtype=np.float32)
    noise = np.random.default_rng(rng.randrange(1 << 30)).normal(128, 60, (H, W)).clip(0, 255).astype(np.uint8)
    streak = np.asarray(Image.fromarray(noise, "L").filter(ImageFilter.BoxBlur(1)), dtype=np.float32)
    arr *= np.clip(0.55 + streak / 255, 0, 1)
    return Image.fromarray(arr.astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.2))


def strokes(img: Image.Image, accent, region, rng: random.Random, n: int = 5) -> None:
    """在 region（x0,y0,x1,y1）里画几道左下到右上的车队色、炭黑刷痕。"""
    x0, y0, x1, y1 = region
    W, H = img.size
    colors = [accent, INK, accent, tuple(min(255, c + 40) for c in accent), INK]
    for i in range(n):
        c = colors[i % len(colors)]
        ax = rng.uniform(x0 - 0.1 * (x1 - x0), x0 + 0.35 * (x1 - x0))
        ay = rng.uniform(y0 + 0.55 * (y1 - y0), y1 + 0.1 * (y1 - y0))
        bx = rng.uniform(x0 + 0.7 * (x1 - x0), x1 + 0.2 * (x1 - x0))
        by = rng.uniform(y0 - 0.1 * (y1 - y0), y0 + 0.45 * (y1 - y0))
        w = rng.uniform(0.06, 0.16) * min(W, H)
        m = _rough_band((W, H), (ax, ay), (bx, by), w, rng)
        img.paste(c, (0, 0), m.point(lambda v: v * (0.92 if c == INK else 0.88)))


def frame_cutout(img: Image.Image, frame: Image.Image, orientation: str, accent, rng: random.Random,
                 footer_top: int) -> None:
    """模板封面：真实画面放在人物区（横版右侧、竖版下半），边缘撕纸状，周围加刷痕。"""
    W, H = img.size
    if orientation == "landscape":
        box = (int(W * 0.45), 0, W, footer_top)
        target = (0.5, 0.45)
    else:
        box = (0, int(H * 0.40), W, footer_top)
        target = (0.5, 0.45)
    bw, bh = box[2] - box[0], box[3] - box[1]
    strokes(img, accent, (box[0] - bw * 0.15, box[1], box[2], box[3]), rng, n=4)
    pic = cover._crop(frame, cover._focus(frame), (bw, bh), target, 1.0)
    m = Image.new("L", (bw, bh), 0)
    d = ImageDraw.Draw(m)
    pts = []
    if orientation == "landscape":   # 左边是斜着撕开的毛边
        for i in range(41):
            y = bh * i / 40
            pts.append((bw * (0.22 - 0.16 * i / 40) + rng.uniform(-14, 14), y))
        d.polygon(pts + [(bw, bh), (bw, 0)], fill=255)
    else:                             # 上边是斜着撕开的毛边
        for i in range(41):
            x = bw * i / 40
            pts.append((x, bh * (0.16 - 0.12 * i / 40) + rng.uniform(-14, 14)))
        d.polygon(pts + [(bw, bh), (0, bh)], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(3))
    img.paste(pic, box[:2], m)
    strokes(img, accent, (box[0], box[1] + bh * 0.78, box[2], box[3] + bh * 0.05), rng, n=2)


def fit_background(src: Image.Image, orientation: str) -> Image.Image:
    """AI 底图居中裁成 4:3 / 3:4 再缩放到成品尺寸。"""
    W, H = SIZES[orientation]
    im = src.convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


# ---------- 排字 ----------

class _Canvas:
    def __init__(self, img: Image.Image, orientation: str):
        self.img, self.o = img, orientation
        self.W, self.H = img.size
        self.items: list[dict] = []

    @property
    def d(self) -> ImageDraw.ImageDraw:
        return ImageDraw.Draw(self.img, "RGBA")

    def text(self, x, y, s, weight, size, fill, kind) -> list[int]:
        f = cover._font(weight, size)
        xy = (round(x), round(y + size * cover.ASC))
        self.d.text(xy, s, font=f, fill=fill, anchor="ls")
        bb = [round(v) for v in self.d.textbbox(xy, s, font=f, anchor="ls")]
        self.items.append({"kind": kind, "text": s, "bbox": bb, "size": size, "fill": list(fill[:3])})
        return bb

    def wash(self, mask: Image.Image):
        """文字区铺一层半透明米白，底图花时也看得清字。"""
        self.img.paste(PAPER, (0, 0), mask)


def _title_sizes(lines: list[str], max_w: float, max_h: float, smax: int, smin: int,
                 lead: float) -> list[int]:
    """每行单独定字号：首行最大；后面的行比首行长时降到约四分之三（样张“小Kimi / 首个冠军赛点”）。"""
    def fit(l, cap):
        for size in range(int(cap), smin - 1, -2):
            if cover._font("Heavy", size).getlength(l) <= max_w:
                return size
        return smin
    scale = 1.0
    while True:
        first = fit(lines[0], smax * scale)
        sizes = [first] + [fit(l, first if len(l) <= len(lines[0]) else first * 0.76) for l in lines[1:]]
        if sum(sizes) * lead - sizes[-1] * (lead - 1) <= max_h or first <= smin:
            return sizes
        scale *= 0.94


def _title_lines(c: Copy, max_w: float, max_h: float, smax: int, smin: int, lead: float) -> tuple[list[int], list[str]]:
    lines = [l.strip() for l in c.lines if l and l.strip()][:3]
    if len(lines) == 1:   # 只有一整句：按 1–3 行切，取字最大的
        size, out = cover._fit(lines[0], "Heavy", max_w, 3, smax, smin, max_h=max_h, h_fn=cover._block_h(lead))
        return [size] * len(out), out
    sizes = _title_sizes(lines, max_w, max_h, smax, smin, lead)
    out, out_sizes = [], []
    for l, z in zip(lines, sizes):   # 最小字号还放不下的行逐字折开
        f = cover._font("Heavy", z)
        parts = cover._greedy(l, f, max_w, 2) if f.getlength(l) > max_w else [l]
        out += parts
        out_sizes += [z] * len(parts)
    return out_sizes[:4], out[:4]


def _fit_one(s: str, weight: str, max_w: float, smax: int, smin: int) -> tuple[int, str]:
    for size in range(smax, smin - 1, -2):
        if cover._font(weight, size).getlength(s) <= max_w:
            return size, s
    return smin, cover._greedy(s, cover._font(weight, smin), max_w, 1)[0]


def _logo(cv: _Canvas, size: int) -> list[int]:
    p = config.BRAND_DIR / "watermark.png"
    if not p.exists():
        return []
    im = Image.open(p).convert("RGBA")
    im = im.resize((size, round(im.height * size / im.width)), Image.LANCZOS)
    m = 40
    x, y = cv.W - m - im.width, m
    cv.img.paste(im, (x, y), im)
    box = [x, y, x + im.width, y + im.height]
    cv.items.append({"kind": "logo", "bbox": box})
    return box


def _footer(cv: _Canvas, c: Copy, accent, L: dict) -> int:
    """画底部面板，返回面板顶边 y。"""
    W, H = cv.W, cv.H
    x = L["summary_x"]
    if c.branch in SUMMARY_BRANCHES and c.summary and c.summary.get("head"):
        top = H - L["panel"]
        cv.d.rectangle((0, top, W, H), fill=PANEL)
        light = tuple(round(v + (255 - v) * 0.35) for v in accent)
        cv.text(x, top + 34, "本期概要", "Bold", 40, light, "summary_label")
        size, head = _fit_one(c.summary["head"], "Heavy", W - 2 * x, 64, 40)
        cv.text(x, top + 94, head, "Heavy", size, CREAM, "summary_head")
        pts = [p for p in (c.summary.get("points") or []) if p][:4]
        if pts:
            size, s = _fit_one("  /  ".join(pts), "Bold", W - 2 * x, 40, 28)
            cv.text(x, top + 94 + 64 + 34, s, "Bold", size, MUTED, "summary_points")
        return top
    top = H - L["bar"]
    cv.d.rectangle((0, top, W, H), fill=PANEL)
    s = CHANNEL + (f"  ·  {c.date}" if c.date else "")
    cv.text(x, top + (L["bar"] - 40) / 2 - 2, s, "Bold", 40, CREAM, "channel")
    return top


def _plan(c: Copy, L: dict, W: int, footer_top: int, logo: list[int], accent) -> tuple[list[tuple], int]:
    """算出标签、标题、副标题的位置（先不画），返回绘制清单和文字区底边。"""
    ops = []
    x, col = L["x"], L["col"]
    lsize, label = _fit_one(c.label, "Heavy", min(col, (logo[0] if logo else W) - x - 40), L["label"], 30)
    lw = cover._font("Heavy", lsize).getlength(label)
    ops.append(("text", x + 4, L["label_y"], label, "Heavy", lsize, INK, "label"))
    ly = round(L["label_y"] + lsize * 1.15 + lsize * 0.55)
    ops.append(("rule", x, ly, x + max(round(lw) + 84, round(col * 0.72)), ly + 9, accent))
    bottom = L["text_bottom"] or footer_top - 60
    sub = [s for s in c.sub if s][:2]
    ssz = L["sub"]
    sub_h = (len(sub) * ssz * 1.35 + ssz * 0.5) if sub else 0
    lead = 1.12
    top = max(L["title_top"], ly + 50)
    sizes, lines = _title_lines(c, col, bottom - top - sub_h, L["title_max"], L["title_min"], lead)
    n_src = len([l for l in c.lines if l and l.strip()])
    acc = c.accent if c.accent is not None else -1
    acc_i = -1 if n_src == 1 and len(lines) == 1 else (acc if acc >= 0 else len(lines) + acc)
    y = top
    for i, (l, z) in enumerate(zip(lines, sizes)):
        ops.append(("text", x, y, l, "Heavy", z, accent if i == acc_i else INK, "title"))
        y += z * lead
    y += ssz * 0.5 - sizes[-1] * (lead - 1)
    for s in sub:
        sz, s1 = _fit_one(s, "Heavy", col, ssz, 32)
        ops.append(("text", x + 4, y, s1, "Heavy", sz, INK, "subtitle"))
        y += ssz * 1.35
    return ops, round(y)


def compose(c: Copy, background: Image.Image, orientation: str, *, wash: bool = True) -> tuple[Image.Image, dict]:
    """在底图上排字：返回成品和各元素位置。"""
    L = LAYOUT[orientation]
    W, H = SIZES[orientation]
    img = background.convert("RGB").resize((W, H), Image.LANCZOS) if background.size != (W, H) \
        else background.convert("RGB").copy()
    cv = _Canvas(img, orientation)
    accent = accent_of(c.team)
    has_panel = c.branch in SUMMARY_BRANCHES and c.summary and c.summary.get("head")
    footer_top = H - (L["panel"] if has_panel else L["bar"])
    logo_box = [W - 40 - L["logo"], 40, W - 40, 40 + L["logo"]]
    ops, text_bottom = _plan(c, L, W, footer_top, logo_box, accent)
    if wash:   # 只提亮文字所在的区域：横版左侧、竖版上部，往下淡出，不盖到人脸
        if orientation == "landscape":
            m = np.asarray(cover._ramp((W, H), 1, int(W * 0.36), int(W * 0.46), 120, 0), dtype=np.float32)
        else:
            m = np.full((H, W), 120, dtype=np.float32)
        m *= np.asarray(cover._ramp((W, H), 0, text_bottom, text_bottom + 90, 255, 0), dtype=np.float32) / 255
        cv.wash(Image.fromarray(m.astype(np.uint8), "L"))
    _footer(cv, c, accent, L)
    _logo(cv, L["logo"])
    for op in ops:
        if op[0] == "rule":
            cv.d.rectangle(op[1:5], fill=op[5])
        else:
            cv.text(*op[1:])
    meta = {"layout": "米白拼贴", "orientation": orientation, "size": [W, H], "team": c.team,
            "accent": "#%02X%02X%02X" % accent, "footer_top": footer_top, "text_bottom": text_bottom,
            "items": cv.items}
    return cv.img, meta


def template_background(c: Copy, frame: Image.Image, orientation: str) -> Image.Image:
    """模板封面的底图：米白纸 + 刷痕 + 真实画面撕纸拼贴（文字区不放画面）。"""
    W, H = SIZES[orientation]
    L = LAYOUT[orientation]
    rng = _rng(f"{c.label}|{''.join(c.lines)}|{orientation}")
    img = paper((W, H), rng)
    has_panel = c.branch in SUMMARY_BRANCHES and c.summary and c.summary.get("head")
    footer_top = H - (L["panel"] if has_panel else L["bar"])
    frame_cutout(img, frame, orientation, accent_of(c.team), rng, footer_top)
    return img


def save(img: Image.Image, meta: dict, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    info = PngInfo()
    info.add_itxt(cover.META_KEY, json.dumps(meta, ensure_ascii=False))
    img.save(out, pnginfo=info)
    return out


def render_template(c: Copy, frame_path: Path, orientation: str, out: Path) -> Path:
    with Image.open(frame_path) as im:
        frame = im.convert("RGB")
    img, meta = compose(c, template_background(c, frame, orientation), orientation)
    return save(img, {**meta, "background": "模板", "frame": str(frame_path)}, out)


def prefix(branch: str | None) -> str:
    return episodes.TITLE_PREFIX.get(branch or "", branch or "")
