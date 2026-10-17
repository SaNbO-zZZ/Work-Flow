"""M4-7 统一封面模板：横版 1920×1080（B 站）与竖版 1080×1920（抖音）。
用户 2026-09-30 定稿"B 大字冲击"为统一模板；A、C 两套代码留着备换。
另含 pick_cover_frame：从成片里初筛封面候选帧（之后由 clip + GPT 精选，M5 接入）。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from PIL.PngImagePlugin import PngInfo

from . import config

SIZES = {"landscape": (1920, 1080), "portrait": (1080, 1920)}
# 横版底部避开 B 站缩略图的播放量/时长浮层；竖版文字收进中间 3:4 区（抖音主页网格按 3:4 居中裁）
SAFE = {"landscape": (96, 60, 1824, 910), "portrait": (72, 270, 1008, 1650)}
THUMB = {"landscape": (320, 180), "portrait": (180, 320)}
WM_WIDTH, WM_MARGIN = 160, 28          # 与成片水印规范一致
BRANCH_ACCENT = {"资讯": "#FF6B00", "前瞻": "#00A3E0", "复盘": "#E10600", "科普": "#00B386"}
DEFAULT_ACCENT = "#E10600"
MIN_CONTRAST, MIN_TEXT_PX = 4.5, 12
META_KEY = "hermes_cover"
WHITE, INK, YELLOW = (255, 255, 255), (17, 17, 17), (255, 214, 0)
ASC = 0.88                             # 思源黑体表意字框顶到基线的比例
_NO_HEAD = set("，。！？、；：,.!?;:）)》」』”’…")


@dataclass
class CoverSpec:
    frame: Path
    title: str
    subtitle: str | None = None
    branch: str = "资讯"
    gp: str | None = None
    series_no: int | None = None
    accent: str | None = None


# ---------- 颜色 ----------

def _rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lin(v):
    v = np.asarray(v, dtype=np.float64) / 255
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _lum(rgb) -> np.ndarray:
    """WCAG 相对亮度，最后一维是 RGB。"""
    l = _lin(rgb)
    return l[..., 0] * 0.2126 + l[..., 1] * 0.7152 + l[..., 2] * 0.0722


def contrast_ratio(a, b) -> float:
    la, lb = float(_lum(a)), float(_lum(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _on(bg) -> tuple[int, int, int]:
    return WHITE if contrast_ratio(WHITE, bg) >= contrast_ratio(INK, bg) else INK


# ---------- 字体与排字 ----------

@lru_cache(maxsize=256)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return _load_font(str(config.FONTS_DIR / f"SourceHanSansSC-{weight}.otf"), int(size))


def _tokens(text: str) -> list[str]:
    # 英文单词和数字整体不拆
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9.'\-]*|\s+|.", text)


def _split(text: str, n: int, font) -> list[str]:
    """把标题切成 n 行，使最长一行最短（同等时首行略长）。"""
    toks = _tokens(text.strip())
    if n == 1 or len(toks) < n:
        return [text.strip()]
    acc = [0.0]
    for t in toks:
        acc.append(acc[-1] + font.getlength(t))
    best = None
    for cuts in combinations(range(1, len(toks)), n - 1):
        if any(toks[c][0] in _NO_HEAD for c in cuts):
            continue
        b = (0, *cuts, len(toks))
        ws = [acc[b[i + 1]] - acc[b[i]] for i in range(n)]
        key = (max(ws), -ws[0])
        if best is None or key < best[0]:
            best = (key, b)
    if best is None:
        return [text.strip()]
    b = best[1]
    return ["".join(toks[b[i]:b[i + 1]]).strip() for i in range(n)]


def _greedy(text: str, font, max_w: float, max_lines: int) -> list[str]:
    """最小字号仍放不下时的兜底：逐字填满，末行截断加省略号。"""
    lines, cur = [], ""
    for t in _tokens(text.strip()):
        for piece in ([t] if font.getlength(t) <= max_w else list(t)):
            if cur and font.getlength(cur + piece) > max_w:
                lines.append(cur.strip())
                cur = piece.lstrip()
            else:
                cur += piece
    lines.append(cur.strip())
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and font.getlength(last + "…") > max_w:
            last = last[:-1]
        lines = lines[:max_lines - 1] + [last + "…"]
    return lines


def _block_h(lead: float = 1.14):
    return lambda n, s: s + (n - 1) * s * lead


def _fit(text, weight, max_w, max_lines, smax, smin, max_h=None, h_fn=None):
    """在宽高限制内取最大字号；返回 (字号, 行列表)。文本里的换行符视为指定断行。"""
    h_fn = h_fn or _block_h()
    max_h = max_h if max_h is not None else float("inf")
    ref = _font(weight, 100)
    if "\n" in text:
        splits = [[l.strip() for l in text.splitlines() if l.strip()][:max_lines]]
    else:
        splits = [_split(text, n, ref) for n in range(1, max_lines + 1)]
    best = []
    for lines in splits:
        if any(lines == b[1] for b in best):
            continue
        w100 = max(ref.getlength(l) for l in lines)
        for size in range(int(smax), int(smin) - 1, -2):
            # 先按参考字号线性估算，命中后再用真实字号复核
            if h_fn(len(lines), size) <= max_h and w100 * size / 100 <= max_w:
                if max(_font(weight, size).getlength(l) for l in lines) <= max_w:
                    best.append((size, lines))
                    break
    if best:
        # 没有分词，多切一行容易把词切断：字号差不到 15% 就取行数少的
        top = max(s for s, _ in best)
        return next((s, l) for s, l in best if s >= top * 0.85)
    return int(smin), _greedy(text.replace("\n", ""), _font(weight, smin), max_w, max_lines)


# ---------- 背景帧 ----------

def _focus(img: Image.Image) -> tuple[float, float]:
    """按边缘能量找画面主体重心（0~1）。"""
    w = 192
    h = max(1, round(w * img.height / img.width))
    g = np.asarray(img.convert("L").resize((w, h), Image.BILINEAR), dtype=np.float32)
    e = np.zeros_like(g)
    e[:, 1:] += np.abs(np.diff(g, axis=1))
    e[1:, :] += np.abs(np.diff(g, axis=0))
    e = np.maximum(e - np.percentile(e, 70), 0)   # 扣掉噪点/纹理底噪，只留显著细节
    yy, xx = np.mgrid[0:h, 0:w]
    e *= np.exp(-(((xx / w - 0.5) / 0.45) ** 2 + ((yy / h - 0.5) / 0.45) ** 2) / 2)
    e[: int(h * 0.2), : int(w * 0.12)] = 0   # 成片左上角的水印不算主体
    s = e.sum()
    if s <= 0:
        return 0.5, 0.5
    return float((e * xx).sum() / s / w), float((e * yy).sum() / s / h)


def _crop(img, focus, size, target=(0.5, 0.5), zoom=1.0) -> Image.Image:
    """裁出 size 比例的画面，并尽量让主体落在画布的 target 位置。"""
    W, H = size
    iw, ih = img.size
    scale = max(W / iw, H / ih) * zoom
    cw, ch = W / scale, H / scale
    x0 = min(max(focus[0] * iw - target[0] * cw, 0), iw - cw)
    y0 = min(max(focus[1] * ih - target[1] * ch, 0), ih - ch)
    return img.resize((W, H), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))


def _ramp(size, axis, start, end, a0, a1) -> Image.Image:
    """沿 axis（0=竖直，1=水平）从 start 到 end 平滑过渡的灰度蒙版。"""
    W, H = size
    n = H if axis == 0 else W
    t = np.clip((np.arange(n) - start) / max(end - start, 1), 0, 1)
    a = a0 + (a1 - a0) * t * t * (3 - 2 * t)
    arr = np.broadcast_to(a[:, None] if axis == 0 else a[None, :], (H, W))
    return Image.fromarray(np.ascontiguousarray(arr).astype(np.uint8), "L")


def _vignette(size, strength=170) -> Image.Image:
    W, H = size
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot((xx / W - 0.5) * 2, (yy / H - 0.5) * 2) / np.sqrt(2)
    a = np.clip((d - 0.45) / 0.55, 0, 1) ** 1.5 * strength
    return Image.fromarray(a.astype(np.uint8), "L")


@lru_cache(maxsize=4)
def _watermark(path: str) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    return im.resize((WM_WIDTH, round(im.height * WM_WIDTH / im.width)), Image.LANCZOS)


# ---------- 画布 ----------

class _Cv:
    def __init__(self, spec: CoverSpec, orientation: str, frame: Image.Image):
        self.spec, self.o, self.frame = spec, orientation, frame
        self.W, self.H = SIZES[orientation]
        self.land = orientation == "landscape"
        self.safe = SAFE[orientation]
        self.accent_hex = spec.accent or BRANCH_ACCENT.get(spec.branch, DEFAULT_ACCENT)
        self.accent = _rgb(self.accent_hex)
        self.on_accent = _on(self.accent)
        self.series = (f"第 {spec.series_no} 期"
                       if spec.branch == "科普" and spec.series_no is not None else None)
        self.focus = _focus(frame)
        self.img = Image.new("RGB", (self.W, self.H))
        self.items: list[dict] = []
        self.title_lines: list[dict] = []
        self.title_size = self.title_glyph = 0

    @property
    def d(self) -> ImageDraw.ImageDraw:
        return ImageDraw.Draw(self.img, "RGBA")

    def bg(self, size=None, target=(0.5, 0.5), zoom=1.0) -> Image.Image:
        return _crop(self.frame, self.focus, size or (self.W, self.H), target, zoom)

    def shade(self, mask: Image.Image, color=(0, 0, 0)):
        self.img.paste(color, (0, 0), mask)

    def calm(self, box, max_l=0.15):
        """白字区域太亮时逐步压暗，直到 90% 像素亮度不高于 max_l。"""
        box = tuple(int(v) for v in box)
        for _ in range(6):
            arr = np.asarray(self.img.crop(box), dtype=np.float32)
            if np.percentile(_lum(arr), 90) <= max_l:
                return
            m = Image.new("L", self.img.size, 0)
            ImageDraw.Draw(m).rectangle((box[0] - 40, box[1] - 40, box[2] + 40, box[3] + 40), fill=70)
            self.shade(m.filter(ImageFilter.GaussianBlur(30)))

    def fit_title(self, max_w, max_lines, smax, smin, max_h=None, h_fn=None):
        size, lines = _fit(self.spec.title, "Heavy", max_w, max_lines, smax, smin, max_h, h_fn)
        l, t, r, b = _font("Heavy", size).getbbox("国", anchor="ls")
        self.title_size, self.title_glyph = size, b - t
        return size, lines

    def text(self, x, y, s, weight, size, fill, kind, **kw):
        """(x, y) 是字框左上角；返回墨迹包围盒。"""
        f = _font(weight, size)
        xy = (round(x), round(y + size * ASC))
        self.d.text(xy, s, font=f, fill=fill, anchor="ls", **kw)
        bb = [round(v) for v in self.d.textbbox(xy, s, font=f, anchor="ls",
                                                stroke_width=kw.get("stroke_width", 0))]
        self.items.append({"kind": kind, "text": s, "bbox": bb})
        if kind == "title":
            self.title_lines.append({"text": s, "bbox": bb, "fill": list(fill[:3])})
        return bb

    def shadow(self, lines, weight, size, offset=(0, 6), blur=10, alpha=170):
        m = Image.new("L", self.img.size, 0)
        dm, f = ImageDraw.Draw(m), _font(weight, size)
        for x, y, s in lines:
            dm.text((round(x + offset[0]), round(y + size * ASC + offset[1])), s, font=f, fill=alpha, anchor="ls")
        self.shade(m.filter(ImageFilter.GaussianBlur(blur)))

    def glass(self, box, radius=40, amount=0.78):
        """磨砂玻璃卡片：背景模糊后叠白。"""
        x0, y0, x1, y1 = (int(v) for v in box)
        sh = Image.new("L", self.img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle((x0, y0 + 16, x1, y1 + 16), radius, fill=120)
        self.shade(sh.filter(ImageFilter.GaussianBlur(30)))
        region = self.img.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(40))
        region = Image.blend(region, Image.new("RGB", region.size, WHITE), amount)
        m = Image.new("L", region.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius, fill=255)
        self.img.paste(region, (x0, y0), m)

    def meta_row(self, x, y, size, fg, max_w, gp_fill=None) -> int:
        """分支胶囊 + 系列编号（仅科普）+ 大奖赛名，返回行高。"""
        h = round(size * 1.7)
        pw = _font("Bold", size).getlength(self.spec.branch) + size * 1.1
        self.d.rounded_rectangle((x, y, x + pw, y + h), h / 2, fill=self.accent)
        self.text(x + size * 0.55, y + (h - size) / 2, self.spec.branch, "Bold", size, self.on_accent, "branch")
        cur = x + pw + size * 0.6
        if self.series:
            self.text(cur, y + (h - size) / 2, self.series, "Bold", size, fg, "series")
            cur += _font("Bold", size).getlength(self.series) + size * 0.6
        room = x + max_w - cur
        if self.spec.gp and room >= size * 2:
            gsz, gl = _fit(self.spec.gp, "Medium", room, 1, size, max(20, size * 0.7))
            self.text(cur, y + (h - gsz) / 2, gl[0], "Medium", gsz, gp_fill or fg, "gp")
        return h

    def finish(self):
        wm = _watermark(str(config.BRAND_DIR / "watermark.png"))
        self.img.paste(wm, (WM_MARGIN, WM_MARGIN), wm)

    def meta(self, template: str) -> dict:
        return {"template": template, "orientation": self.o, "size": [self.W, self.H],
                "safe": list(self.safe), "accent": self.accent_hex, "title_size": self.title_size,
                "title_glyph_px": self.title_glyph, "title_lines": self.title_lines, "items": self.items,
                "watermark": [WM_MARGIN, WM_MARGIN, WM_MARGIN + WM_WIDTH, WM_MARGIN + WM_WIDTH]}


# ---------- 模板 A：杂志风（落选，备用） ----------

def _draw_a(cv: _Cv):
    W, H = cv.W, cv.H
    sx0, sy0, sx1, sy1 = cv.safe
    if cv.land:
        cv.img = cv.bg(target=(0.64, 0.42))
        cv.shade(_ramp((W, H), 0, int(H * 0.32), H, 0, 225))
        cv.shade(_ramp((W, H), 1, 0, int(W * 0.62), 150, 0))
        tw, nl, smax, smin, ssz, msz = int(W * 0.66), 2, 150, 64, 46, 36
    else:
        cv.img = cv.bg().filter(ImageFilter.GaussianBlur(40))
        cv.shade(Image.new("L", (W, H), 120))
        top_h = 1300
        cv.img.paste(cv.bg((W, top_h), target=(0.5, 0.45)), (0, 0), _ramp((W, top_h), 0, 880, top_h, 255, 0))
        cv.shade(_ramp((W, H), 0, 900, H, 0, 200))
        tw, nl, smax, smin, ssz, msz = sx1 - sx0 - 34, 3, 168, 60, 46, 36
    x, lead = sx0 + 34, 1.14
    sub = _fit(cv.spec.subtitle, "Medium", tw, 1, ssz, 28) if cv.spec.subtitle else None
    size, lines = cv.fit_title(tw, nl, smax, smin, max_h=smax * (1 + (nl - 1) * lead))
    th = _block_h(lead)(len(lines), size)
    y_title = (sy1 - sub[0] - round(sub[0] * 0.7) if sub else sy1) - th
    meta_h = round(msz * 1.7)
    y_meta = y_title - round(size * 0.3) - meta_h
    cv.calm((sx0, y_meta, x + tw, sy1))
    cv.d.rectangle((sx0, y_title + size * 0.06, sx0 + 9, y_title + th - size * 0.02), fill=cv.accent)
    cv.meta_row(sx0, y_meta, msz, WHITE, tw + 34, gp_fill=(235, 235, 235))
    rows = [(x, y_title + i * size * lead, l) for i, l in enumerate(lines)]
    cv.shadow(rows, "Heavy", size)
    for rx, ry, l in rows:
        cv.text(rx, ry, l, "Heavy", size, WHITE, "title")
    if sub:
        cv.text(x, sy1 - sub[0], sub[1][0], "Medium", sub[0], (235, 235, 235), "subtitle")


# ---------- 模板 B：大字冲击（2026-09-30 定稿的统一模板） ----------

def _slant_box(cv, x, y, w, h, slant, fill):
    cv.d.polygon([(x + slant, y), (x + w, y), (x + w - slant, y + h), (x, y + h)], fill=fill)


def _draw_b(cv: _Cv):
    W, H = cv.W, cv.H
    sx0, sy0, sx1, sy1 = cv.safe
    if cv.land:
        cv.img = cv.bg(target=(0.66, 0.45), zoom=1.06)
        tw, nl, smax, smin, msz = int(W * 0.74), 2, 172, 72, 44
    else:
        cv.img = cv.bg(target=(0.5, 0.40))
        tw, nl, smax, smin, msz = sx1 - sx0, 4, 170, 72, 44
    cv.img = ImageEnhance.Color(ImageEnhance.Contrast(cv.img).enhance(1.12)).enhance(1.2)
    cv.shade(Image.new("L", (W, H), 60))
    cv.shade(_vignette((W, H)))

    # 右上角分支角标，科普编号放在角标下
    tsz = 64
    th_, tpad, tsl = round(tsz * 1.5), tsz * 0.5, tsz * 0.35
    tl = _font("Heavy", tsz).getlength(cv.spec.branch)
    tx0, ty0 = sx1 - tl - 2 * tpad - tsl, sy0 + (10 if cv.land else 0)
    _slant_box(cv, tx0, ty0, sx1 - tx0, th_, tsl, cv.accent)
    cv.text(tx0 + tsl / 2 + tpad, ty0 + (th_ - tsz) / 2, cv.spec.branch, "Heavy", tsz, cv.on_accent, "branch")
    if cv.series:
        ssz = round(tsz * 0.75)
        sl = _font("Heavy", ssz).getlength(cv.series)
        cv.text(sx1 - 6 - sl, ty0 + th_ + 18, cv.series, "Heavy", ssz, WHITE, "series",
                stroke_width=5, stroke_fill=(0, 0, 0))

    # 标题：每行压在一块斜切色块上，强调色与黄色交替
    px, py, sl, gap = 36, 18, 40, 16
    h_fn = lambda n, s: n * (s + 2 * py) + (n - 1) * gap  # noqa: E731
    bar_h = round(msz * 1.6)
    below = bar_h + 16 if cv.spec.subtitle else 0
    above = bar_h + 16 if cv.spec.gp else 0
    max_h = (sy1 - below) - (ty0 + th_ + 120 + above)
    size, lines = cv.fit_title(tw - 2 * px - sl, nl, smax, smin, max_h=max_h, h_fn=h_fn)
    bh = size + 2 * py
    y = sy1 - below - h_fn(len(lines), size)

    def bar(yy, s, kind):
        sz, ls = _fit(s, "Bold", tw - 2 * px - sl, 1, msz, 28)
        w = _font("Bold", sz).getlength(ls[0]) + 2 * px + sl
        _slant_box(cv, sx0, yy, w, bar_h, sl * 0.6, (0, 0, 0, 215))
        cv.text(sx0 + sl * 0.3 + px, yy + (bar_h - sz) / 2, ls[0], "Bold", sz, WHITE, kind)

    if cv.spec.gp:
        bar(y - above, cv.spec.gp, "gp")
    for i, l in enumerate(lines):
        fill = cv.accent if i % 2 == 0 else YELLOW
        w = _font("Heavy", size).getlength(l) + 2 * px + sl
        _slant_box(cv, sx0, y, w, bh, sl, fill)
        cv.text(sx0 + sl / 2 + px, y + py, l, "Heavy", size, _on(fill), "title")
        y += bh + gap
    if cv.spec.subtitle:
        bar(sy1 - bar_h, cv.spec.subtitle, "subtitle")


# ---------- 模板 C：玻璃卡片（落选，备用） ----------

def _draw_c(cv: _Cv):
    sx0, sy0, sx1, sy1 = cv.safe
    if cv.land:
        cv.img = cv.bg(target=(0.30, 0.5), zoom=1.15)
        cx0, cx1, nl, smax, smin = 1030, sx1, 4, 120, 56
    else:
        cv.img = cv.bg(target=(0.5, 0.36))
        cx0, cx1, nl, smax, smin = sx0 - 12, sx1 + 12, 3, 124, 60
    pad, msz, lead = 56, 34, 1.14
    inner = cx1 - cx0 - 2 * pad
    meta_h = round(msz * 1.7)
    sub = _fit(cv.spec.subtitle, "Medium", inner, 2, 42, 28) if cv.spec.subtitle else None
    sub_h = _block_h(1.3)(len(sub[1]), sub[0]) + 64 if sub else 0
    head = 10 + 30 + meta_h + 40
    size, lines = cv.fit_title(inner, nl, smax, smin, max_h=(sy1 - sy0) - 2 * pad - head - sub_h)
    th = _block_h(lead)(len(lines), size)
    card_h = 2 * pad + head + th + sub_h
    y0 = max(sy0, (sy0 + sy1) / 2 - card_h / 2) if cv.land else sy1 - card_h
    cv.glass((cx0, y0, cx1, y0 + card_h))

    x, y = cx0 + pad, y0 + pad
    cv.d.rounded_rectangle((x, y, x + 72, y + 10), 5, fill=cv.accent)
    y += 40
    cv.meta_row(x, y, msz, INK, inner, gp_fill=(85, 85, 85))
    y += meta_h + 40
    for i, l in enumerate(lines):
        cv.text(x, y + i * size * lead, l, "Heavy", size, INK, "title")
    y += th
    if sub:
        y += 32
        cv.d.line((x, y, x + inner, y), fill=(0, 0, 0, 40), width=2)
        y += 32
        for i, l in enumerate(sub[1]):
            cv.text(x, y + i * sub[0] * 1.3, l, "Medium", sub[0], (60, 60, 60), "subtitle")


TEMPLATES: dict[str, dict] = {
    "A 杂志风": {"draw": _draw_a, "desc": "整幅画面 + 底部渐暗，左下白色大标题配强调色竖条，像杂志封面，克制干净",
                 "fits": "复盘、前瞻这类画面本身好看的期"},
    "B 大字冲击": {"draw": _draw_b, "desc": "画面压暗加暗角，标题逐行压在斜切色块上（强调色/黄交替），右上大角标，缩略图最抢眼",
                   "fits": "资讯、冲突类、需要在信息流里抢点击的期"},
    "C 玻璃卡片": {"draw": _draw_c, "desc": "一侧磨砂白卡片承载分支、赛事、标题、副标题，画面主体留在另一侧，Apple 式信息卡",
                   "fits": "科普、规则讲解这类信息量大、要显得专业的期"},
}
DEFAULT_TEMPLATE = "B 大字冲击"


def _template_key(name: str) -> str:
    if name in TEMPLATES:
        return name
    hits = [k for k in TEMPLATES if k.split()[0] == name or name in k]
    if len(hits) != 1:
        raise KeyError(f"未知封面模板：{name}")
    return hits[0]


def render(spec: CoverSpec, template: str | None = None, out: Path | None = None, *,
           orientation: str = "landscape") -> Path:
    """template 缺省用定稿的 DEFAULT_TEMPLATE；保留 (spec, 模板, 输出) 的旧位置参数写法。"""
    if out is None:
        raise TypeError("render() 缺少输出路径 out")
    if orientation not in SIZES:
        raise ValueError(f"orientation 只能是 {list(SIZES)}")
    key = _template_key(template or DEFAULT_TEMPLATE)
    with Image.open(spec.frame) as im:
        frame = im.convert("RGB")
    cv = _Cv(spec, orientation, frame)
    TEMPLATES[key]["draw"](cv)
    cv.finish()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps(cv.meta(key), ensure_ascii=False)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        cv.img.save(out, quality=92, comment=meta.encode("utf-8"))
    else:
        info = PngInfo()
        info.add_itxt(META_KEY, meta)
        cv.img.save(out, pnginfo=info)
    return out


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    H, W = m.shape
    p = np.pad(m, r)
    a = np.zeros((H, W + 2 * r), dtype=bool)
    for k in range(2 * r + 1):
        a |= p[k:k + H, :]
    b = np.zeros((H, W), dtype=bool)
    for k in range(2 * r + 1):
        b |= a[:, k:k + W]
    return b


def readability(img_path: Path) -> dict:
    """标题逐行取字形紧邻一圈背景算 WCAG 对比度（取 10% 分位的最差值），并换算缩略图下的字高。"""
    with Image.open(img_path) as im:
        raw = im.info.get(META_KEY) or im.info.get("comment")
        arr = np.asarray(im.convert("RGB"), dtype=np.float32)
    if not raw:
        raise ValueError(f"{img_path} 不是 cover.render 生成的封面（缺排版元数据）")
    meta = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    H, W = arr.shape[:2]
    lum = _lum(arr)
    worst = float("inf")
    for ln in meta["title_lines"]:
        x0, y0, x1, y1 = ln["bbox"]
        x0, y0, x1, y1 = max(0, x0 - 10), max(0, y0 - 10), min(W, x1 + 10), min(H, y1 + 10)
        sub = arr[y0:y1, x0:x1]
        fill = np.array(ln["fill"], dtype=np.float32)
        core = np.abs(sub - fill).max(-1) < 40
        ring = _dilate(core, 7) & ~_dilate(core, 2)
        if core.sum() < 20 or ring.sum() < 20:
            worst = 1.0
            continue
        tl, bl = float(_lum(fill)), lum[y0:y1, x0:x1][ring]
        c = (np.maximum(tl, bl) + 0.05) / (np.minimum(tl, bl) + 0.05)
        worst = min(worst, float(np.percentile(c, 10)))
    if worst == float("inf"):
        worst = 1.0
    min_px = meta["title_glyph_px"] * THUMB[meta["orientation"]][0] / meta["size"][0]
    return {"contrast": round(worst, 2), "min_text_px": round(min_px, 1),
            "ok": worst >= MIN_CONTRAST and min_px >= MIN_TEXT_PX}


# ---------- 封面候选帧初筛 ----------

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
EDGE_SKIP = 0.05          # 片头片尾各跳过 5%（片头卡、结尾 CTA）
OVERSAMPLE = 3            # 先取候选数 3 倍的帧，筛掉坏帧后再均匀挑
DARK_LUMA = 28            # 平均亮度（0~255）低于此算过暗：黑场、转场
BLUR_ABS, BLUR_REL = 15.0, 0.25   # 拉普拉斯方差低于 max(绝对值, 相对全片中位数) 算过糊
_PROBE_W = 640            # 指标统一在 640 宽上算，不受成片分辨率影响


def _duration(video: Path) -> float:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise ValueError(f"读不出时长：{video}（{r.stderr.strip()[:200]}）") from None


def _frame_stats(path: Path) -> tuple[float, float]:
    """返回（平均亮度, 拉普拉斯方差）。"""
    with Image.open(path) as im:
        g = im.convert("L")
        g = g.resize((_PROBE_W, max(1, round(g.height * _PROBE_W / g.width))), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    lap = 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(a.mean()), float(lap.var())


def pick_cover_frame(video: Path, *, candidates: int = 12, out_dir: Path | None = None) -> list[Path]:
    """B7 第一步：程序初筛。均匀取帧、跳过片头片尾和过暗/过糊的帧，返回按时间排序的候选帧路径。
    文件名带时间点（候选_03_42.50s.png）；好帧不够时只返回好帧，可能少于 candidates。"""
    video = Path(video)
    if candidates < 1:
        raise ValueError("candidates 至少为 1")
    dur = _duration(video)
    out_dir = Path(out_dir) if out_dir else video.parent / f"{video.stem}_封面候选"
    out_dir.mkdir(parents=True, exist_ok=True)
    times = np.linspace(dur * EDGE_SKIP, dur * (1 - EDGE_SKIP), candidates * OVERSAMPLE)
    with tempfile.TemporaryDirectory(prefix="cover-pick-") as tmp:
        shots = []
        for i, t in enumerate(times):
            f = Path(tmp) / f"{i:03d}.png"
            subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                            "-frames:v", "1", str(f)], capture_output=True, timeout=120)
            if f.exists():
                shots.append((float(t), f, *_frame_stats(f)))
        lit = [s for s in shots if s[2] >= DARK_LUMA]
        if not lit:
            return []
        blur_min = max(BLUR_ABS, BLUR_REL * float(np.median([s[3] for s in lit])))
        good = [s for s in lit if s[3] >= blur_min]
        if len(good) > candidates:
            good = [good[i] for i in np.round(np.linspace(0, len(good) - 1, candidates)).astype(int)]
        result = []
        for n, (t, f, _, _) in enumerate(good):
            dst = out_dir / f"候选_{n:02d}_{t:.2f}s.png"
            shutil.move(str(f), dst)
            result.append(dst)
    return result
