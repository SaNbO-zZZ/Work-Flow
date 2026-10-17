"""M4-6 视频图形：截图叠层 + 马克笔、FastF1 数据图、排名塔、花字标题卡、出处小字。

所有叠层都是透明底，按 1920×1080 设计，传入其他尺寸时整体等比缩放。
版面约定（1920×1080 下）：
- 水印固定在左上角 (28,28)-(188,188)，任何叠层都不进这个区域。
- 字幕区预留在 y ≥ 880（M5 定稿前的假设），卡片类叠层只用 CARD_BOX。
- 出处小字在右下角，宽度上限 CREDIT_MAX_W，避免压到居中的字幕。
"""
from __future__ import annotations

import builtins
import functools
import io
import json
import logging
import math
import random
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
FPS = 30

# 2026 车队代表色：取自 FastF1 正赛成绩的 TeamColor 字段（F1 官方计时数据，第 15 站阿塞拜疆核对）。
# 存疑：Cadillac 官方计时给的是中性灰 #909090，与 Haas #9C9FA2 很接近，同屏时只能靠文字区分。
TEAM_COLORS: dict[str, str] = {
    "Mercedes": "#00D7B6",
    "Red Bull Racing": "#4781D7",
    "Ferrari": "#ED1131",
    "McLaren": "#F47600",
    "Aston Martin": "#229971",
    "Alpine": "#00A1E8",
    "Williams": "#1868DB",
    "Racing Bulls": "#6C98FF",
    "Haas F1 Team": "#9C9FA2",
    "Audi": "#F50537",
    "Cadillac": "#909090",
}
DEFAULT_TEAM_COLOR = "#9AA0A6"

YELLOW = (255, 212, 0)
F1_RED = (225, 6, 0)
PANEL = (17, 19, 24)
# 以下坐标均为 1920×1080 基准
WATERMARK_BOX = (28, 28, 188, 188)
SUBTITLE_TOP = 880
CARD_BOX = (240, 72, 1680, 860)
MARGIN = 28
# 出处胶囊最宽 440px（x ≥ 1452），尽量不和居中字幕横向重叠
CREDIT_MAX_W = 640

FONT_WEIGHTS = ("Regular", "Medium", "Bold", "Heavy")


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def team_color(team: str | None) -> str:
    return TEAM_COLORS.get(team or "", DEFAULT_TEAM_COLOR)


def _k(size: tuple[int, int]) -> float:
    return size[0] / 1920


def _box(size: tuple[int, int], box: tuple[int, ...]) -> tuple[int, ...]:
    k = _k(size)
    return tuple(round(v * k) for v in box)


def _font_path(weight: str) -> Path:
    if weight not in FONT_WEIGHTS:
        raise ValueError(f"未知字重：{weight}")
    return config.FONTS_DIR / f"SourceHanSansSC-{weight}.otf"


@functools.lru_cache(maxsize=64)
def font(weight: str = "Bold", size: int = 48) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path(weight)), max(1, int(size)))


def _save_png(img: Image.Image, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGBA").save(out, optimize=False)
    return out


# ---------- 出处小字 ----------

def credit_tag(text: str, size: tuple[int, int] = (1920, 1080)) -> Image.Image:
    """右下角半透明深色胶囊 + 白字；水印在左上角，这里只需避开居中字幕。"""
    W, H = size
    k = _k(size)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    pad_x, pad_y = 14 * k, 7 * k
    max_w = CREDIT_MAX_W * k - 2 * pad_x
    px = 26
    while True:
        f = font("Medium", round(px * k))
        if f.getlength(text) <= max_w or px <= 18:
            break
        px -= 1
    i = text.find("《")
    if f.getlength(text) > max_w and i > 0 and text.endswith("》"):
        head, name = text[:i + 1], text[i + 1:-1].rstrip("…")
        while name and f.getlength(head + name + "…》") > max_w:
            name = name[:-1]
        if name:
            text = head + name.rstrip() + "…》"
    while len(text) > 1 and f.getlength(text) > max_w:
        text = text[:-2] + "…"
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    w, h = r - l, b - t
    x2, y2 = W - MARGIN * k, H - MARGIN * k
    x1, y1 = x2 - w - 2 * pad_x, y2 - h - 2 * pad_y
    d.rounded_rectangle((x1, y1, x2, y2), radius=(y2 - y1) / 2, fill=(0, 0, 0, 120))
    d.text((x1 + pad_x - l, y1 + pad_y - t), text, font=f, fill=(255, 255, 255, 235))
    return img


# ---------- 截图叠层 + 马克笔 ----------

def _stroke(size: tuple[int, int], pts: list[tuple[float, float]], width: float) -> np.ndarray:
    layer = Image.new("L", size, 0)
    d = ImageDraw.Draw(layer)
    w = max(1, round(width))
    d.line(pts, fill=255, width=w, joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)
    return np.asarray(layer, dtype=np.float32) / 255


def _marker_alpha(size: tuple[int, int], highlights, underline, seed: int) -> np.ndarray:
    """手绘感：每个矩形用 1–3 笔略倾斜、两端出头的粗笔涂抹，笔画重叠处更深。"""
    rnd = random.Random(seed)
    keep = np.ones(size[::-1], dtype=np.float32)
    for x1, y1, x2, y2 in highlights:
        h = max(1, y2 - y1)
        n = max(1, min(3, round(h / 34)))
        bw = h / n * 1.25
        for i in range(n):
            cy = y1 + (i + 0.5) * h / n
            ax, bx = x1 - rnd.uniform(3, 10), x2 + rnd.uniform(3, 12)
            tilt = rnd.uniform(-0.18, 0.18) * bw
            pts = [(ax + (bx - ax) * t, cy + tilt * (2 * t - 1) + rnd.uniform(-0.05, 0.05) * bw)
                   for t in np.linspace(0, 1, 9)]
            keep *= 1 - 0.6 * _stroke(size, pts, bw * rnd.uniform(0.92, 1.05))
    lw = max(5, size[0] / 150)
    for x1, x2, y in underline:
        ph = rnd.uniform(0, math.pi)
        pts = [(x1 - 4 + (x2 - x1 + 8) * t, y + math.sin(ph + t * 5) * lw * 0.35 - t * lw * 0.6)
               for t in np.linspace(0, 1, 24)]
        keep *= 1 - 0.95 * _stroke(size, pts, lw)
    a = 1 - keep
    # 横向细纹，模拟马克笔笔触不均
    streak = 1 - 0.12 * np.random.default_rng(seed).random((size[1], 1), dtype=np.float32)
    a = a * streak
    return np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8)),
                      dtype=np.float32) / 255


def _apply_marker(im: Image.Image, highlights, underline) -> Image.Image:
    if not highlights and not underline:
        return im
    seed = hash((tuple(map(tuple, highlights)), tuple(map(tuple, underline)))) & 0xFFFF
    a = _marker_alpha(im.size, highlights, underline, seed)[..., None]
    base = np.asarray(im.convert("RGB"), dtype=np.float32)
    y = np.array(YELLOW, dtype=np.float32)
    # 以正片叠底为主（字仍然清楚），混 20% 纯黄保证深色网页上也看得见
    ink = 0.8 * base * y / 255 + 0.2 * y
    return Image.fromarray(np.clip(base * (1 - a) + ink * a, 0, 255).astype(np.uint8))


def _card(canvas: Image.Image, im: Image.Image, xy: tuple[int, int], k: float) -> None:
    """圆角 + 柔和阴影 + 细白边，直接合成进 canvas。"""
    r = round(22 * k)
    x, y = xy
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=r, fill=255)
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    off = round(14 * k)
    ImageDraw.Draw(sh).rounded_rectangle((x, y + off, x + im.width, y + im.height + off), radius=r, fill=(0, 0, 0, 150))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(1, 22 * k))))
    card = im.convert("RGBA")
    card.putalpha(mask)
    canvas.alpha_composite(card, (x, y))
    ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + im.width - 1, y + im.height - 1), radius=r,
                                             outline=(255, 255, 255, 90), width=max(1, round(2 * k)))


def screenshot_overlay(shot: Path, out: Path, *, highlights: Iterable[tuple[int, int, int, int]] = (),
                       underline: Iterable[tuple[int, int, int]] = (), credit: str | None = None,
                       size: tuple[int, int] = (1920, 1080)) -> Path:
    """highlights / underline 用截图原始像素坐标；先在原图上画，再缩放进卡片区。"""
    im = _apply_marker(Image.open(shot).convert("RGB"), list(highlights), list(underline))
    x1, y1, x2, y2 = _box(size, CARD_BOX)
    r = min((x2 - x1) / im.width, (y2 - y1) / im.height)
    im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    _card(canvas, im, ((size[0] - im.width) // 2, y1 + (y2 - y1 - im.height) // 2), _k(size))
    if credit:
        canvas.alpha_composite(credit_tag(credit, size))
    return _save_png(canvas, out)


# ---------- 数据图 ----------

@functools.lru_cache(maxsize=1)
def _mpl_family() -> str:
    from matplotlib import font_manager
    for w in FONT_WEIGHTS:
        font_manager.fontManager.addfont(str(_font_path(w)))
    return font_manager.FontProperties(fname=str(_font_path("Regular"))).get_name()


def _fmt_laptime(v: float, _pos=None) -> str:
    m, s = divmod(v, 60)
    return f"{int(m)}:{s:04.1f}"


def _break_gaps(laps: list, times: list) -> tuple[list, list]:
    """圈号不连续处插 NaN 断线，免得把安全车/进站前后的圈连成一条长斜线。"""
    xs, ys = [], []
    for i, (x, y) in enumerate(zip(laps, times)):
        if i and x - laps[i - 1] > 1:
            xs.append(np.nan)
            ys.append(np.nan)
        xs.append(x)
        ys.append(y)
    return xs, ys


def data_chart(kind: str, data: dict, out: Path, *, title: str, credit: str | None = None,
               size: tuple[int, int] = (1920, 1080)) -> Path:
    """kind:
    - laps: {"drivers": [{"name", "team", "laps": [...], "times": [秒...]}], "note"?}
    - gap:  {"items": [{"name", "team", "value": 秒, "label"?}], "unit"?}  竖向柱，默认标 +x.xxx
    - bar:  {"items": [{"name", "value", "team"? | "color"?, "label"?}], "unit"?}  横向柱，从上到下按给定顺序
    """
    if kind not in ("laps", "gap", "bar"):
        raise ValueError(f"未知图表类型：{kind}")
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    W, H = size
    k = _k(size)
    pt = lambda px: px * k * 0.72  # noqa: E731  像素 → 磅（dpi=100）
    fg, sub, grid = "#FFFFFF", "#C4CAD3", (1, 1, 1, 0.12)
    x1, y1, x2, y2 = CARD_BOX
    fx1, fx2 = x1 / 1920, x2 / 1920
    fy1, fy2 = 1 - y2 / 1080, 1 - y1 / 1080

    # 底板和标题用 PIL 画（圆角不受坐标比例影响，字体与其他图形一致）
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bx = _box(size, CARD_BOX)
    d.rounded_rectangle(bx, radius=round(24 * k), fill=(*PANEL, 220), outline=(255, 255, 255, 26),
                        width=max(1, round(2 * k)))
    ty = bx[1] + round(56 * k)
    d.rectangle((bx[0] + round(36 * k), ty - round(20 * k), bx[0] + round(44 * k), ty + round(20 * k)), fill=F1_RED)
    d.text((bx[0] + round(62 * k), ty), title, font=font("Heavy", round(42 * k)), fill=(255, 255, 255, 255),
           anchor="lm")
    if kind == "laps" and data.get("note"):
        d.text((bx[0] + round(62 * k), bx[3] - round(22 * k)), data["note"], font=font("Regular", round(20 * k)),
               fill=(196, 202, 211, 255), anchor="ls")

    rc = {"font.family": _mpl_family(), "axes.unicode_minus": False, "font.size": pt(22),
          "text.color": fg, "axes.labelcolor": sub, "xtick.color": sub, "ytick.color": sub}
    with rc_context(rc):
        fig = Figure(figsize=(W / 100, H / 100), dpi=100)
        FigureCanvasAgg(fig)
        ax = fig.add_axes((fx1 + 130 / 1920, fy1 + 100 / 1080, (fx2 - fx1) - 190 / 1920, (fy2 - fy1) - 220 / 1080))
        ax.set_facecolor("none")
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color((1, 1, 1, 0.35))
        ax.tick_params(length=0, labelsize=pt(22), pad=pt(10))

        if kind == "laps":
            seen: dict[str, int] = {}
            for drv in data["drivers"]:
                n = seen[drv.get("team")] = seen.get(drv.get("team"), 0) + 1
                ax.plot(*_break_gaps(drv["laps"], drv["times"]), color=drv.get("color") or team_color(drv.get("team")),
                        lw=pt(4.5), ls="-" if n == 1 else (0, (4, 2)), label=drv["name"],
                        solid_capstyle="round", marker="o", ms=pt(6), mec="none")
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_laptime))
            ax.grid(axis="y", color=grid, lw=pt(1.5))
            ax.set_xlabel("圈数", fontsize=pt(22), labelpad=pt(8))
            leg = fig.legend(loc="center right", bbox_to_anchor=(fx2 - 30 / 1920, fy2 - 56 / 1080),
                             ncol=len(data["drivers"]), frameon=False, fontsize=pt(26), handlelength=1.6,
                             columnspacing=1.4)
            for t in leg.get_texts():
                t.set_color(fg)
                t.set_fontweight("bold")
        else:
            items = data["items"]
            unit = data.get("unit", "秒")
            colors = [it.get("color") or team_color(it.get("team")) for it in items]
            vals = [float(it["value"]) for it in items]
            vmax = max(vals) or 1
            if kind == "gap":
                xs = range(len(items))
                ax.bar(xs, vals, color=colors, width=0.62)
                ax.set_xticks(list(xs), [it["name"] for it in items], fontsize=pt(24), fontweight="bold", color=fg)
                ax.set_ylim(0, vmax * 1.18)
                ax.grid(axis="y", color=grid, lw=pt(1.5))
                ax.set_axisbelow(True)
                ax.set_ylabel(unit, fontsize=pt(20))
                for x, v, it in zip(xs, vals, items):
                    ax.text(x, v + vmax * 0.02, it.get("label") or f"+{v:.3f}", ha="center", va="bottom",
                            fontsize=pt(24), fontweight="bold", color=fg)
            else:
                ys = list(range(len(items)))[::-1]
                ax.barh(ys, vals, color=colors, height=0.62)
                ax.set_yticks(ys, [it["name"] for it in items], fontsize=pt(24), fontweight="bold", color=fg)
                ax.set_xlim(0, vmax * 1.15)
                ax.grid(axis="x", color=grid, lw=pt(1.5))
                ax.set_axisbelow(True)
                ax.spines["bottom"].set_visible(False)
                ax.tick_params(axis="x", labelbottom=False)
                for y, v, it in zip(ys, vals, items):
                    ax.text(v + vmax * 0.012, y, it.get("label") or f"{v:g}{unit}", va="center",
                            fontsize=pt(24), fontweight="bold", color=fg)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, transparent=True)
    chart = Image.open(buf).convert("RGBA")
    if chart.size != size:
        chart = chart.resize(size, Image.LANCZOS)
    img.alpha_composite(chart)
    return _save_png(img, out)


def _load_session(season: int, rnd: int, session: str, *, laps: bool):
    """先只读缓存（离线），缓存里没有再联网补（结果会写进 FastF1 缓存）。"""
    import fastf1

    config.FASTF1_CACHE.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
    logging.getLogger("fastf1").setLevel(logging.ERROR)
    last: Exception | None = None
    for offline in (True, False):
        fastf1.Cache.offline_mode(offline)
        try:
            s = fastf1.get_session(season, rnd, session)
            s.load(laps=laps, telemetry=False, weather=False, messages=False)
            if laps:
                s.laps  # noqa: B018  缓存缺圈速时这里抛 DataNotLoadedError
            if s.results is None or s.results.empty:
                raise ValueError("没有成绩数据")
            return s
        except Exception as e:  # noqa: BLE001
            last = e
        finally:
            fastf1.Cache.offline_mode(False)
    raise RuntimeError(f"FastF1 读取 {season} 第 {rnd} 站 {session} 失败：{last}") from last


def laps_from_fastf1(season: int, round: int, session: str, drivers: list[str]) -> dict:
    """drivers 用三字母缩写或车号。剔除第 1 圈、进出站圈和非绿旗圈，只留干净圈。"""
    # 参数名 round 是接口约定，遮蔽了内置 round，下面用 builtins.round
    s = _load_session(season, round, session, laps=True)
    out = []
    for code in drivers:
        info = s.get_driver(str(code))
        lp = s.laps.pick_drivers(info["Abbreviation"]).pick_wo_box().pick_track_status("1")
        lp = lp[(lp["LapNumber"] > 1) & lp["LapTime"].notna()]
        out.append({"code": info["Abbreviation"], "name": info["Abbreviation"], "full_name": info["FullName"],
                    "team": info["TeamName"], "laps": [int(n) for n in lp["LapNumber"]],
                    "times": [builtins.round(t.total_seconds(), 3) for t in lp["LapTime"]]})
    return {"drivers": out, "event": s.event["EventName"], "session": session, "source": "FastF1",
            "note": "已剔除第 1 圈 进出站圈和非绿旗圈"}


# ---------- 视频输出 ----------

def _write_mov(frames: Iterator[bytes], out: Path, size: tuple[int, int], fps: int) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size[0]}x{size[1]}",
           "-r", str(fps), "-i", "-", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
           "-an", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for fr in frames:
            p.stdin.write(fr)
    finally:
        p.stdin.close()
        err = p.stderr.read().decode(errors="replace")
        p.wait()
    if p.returncode:
        raise RuntimeError(f"ffmpeg 输出 {out.name} 失败：{err[-500:]}")
    return out


def _ease_out(t: float) -> float:
    return 1 - (1 - min(max(t, 0), 1)) ** 3


def _ease_back(t: float) -> float:
    t = min(max(t, 0), 1)
    c = 1.9
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def _fade(img: Image.Image, a: float) -> Image.Image:
    if a >= 1:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * a)))
    return out


# ---------- 排名塔 ----------

def _tower_rows(rows: list[dict], title: str | None, k: float) -> list[Image.Image]:
    w, rh, hh = round(420 * k), round(46 * k), round(54 * k)
    r = max(1, round(6 * k))
    imgs = []
    if title:
        im = Image.new("RGBA", (w, hh), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w - 1, hh - 1), radius=r, fill=(*PANEL, 235))
        d.rectangle((0, 0, round(8 * k), hh - 1), fill=F1_RED)
        f = font("Heavy", round(26 * k))
        d.text((round(24 * k), hh / 2), title, font=f, fill=(255, 255, 255, 255), anchor="lm")
        imgs.append(im)
    fp, fn, fg = font("Bold", round(24 * k)), font("Bold", round(26 * k)), font("Medium", round(22 * k))
    for i, row in enumerate(rows):
        im = Image.new("RGBA", (w, rh), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w - 1, rh - 1), radius=r, fill=(*PANEL, 215 if i % 2 else 200))
        pos_w = round(56 * k)
        d.text((pos_w / 2, rh / 2), str(row["pos"]), font=fp, fill=(255, 255, 255, 255), anchor="mm")
        cx = pos_w
        d.rectangle((cx, round(9 * k), cx + max(2, round(6 * k)), rh - round(9 * k)),
                    fill=_hex(row.get("color") or team_color(row.get("team"))))
        d.text((cx + round(20 * k), rh / 2), row["name"], font=fn, fill=(255, 255, 255, 255), anchor="lm")
        if row.get("gap"):
            d.text((w - round(16 * k), rh / 2), str(row["gap"]), font=fg, fill=(200, 206, 214, 255), anchor="rm")
        imgs.append(im)
    return imgs


def ranking_tower(rows: list[dict], out: Path, *, seconds: float = 4.0, fps: int = FPS, title: str | None = None,
                  size: tuple[int, int] = (1920, 1080), side: str = "left", still: Path | None = None) -> Path:
    """side=left 放水印正下方（与水印左对齐）；side=right 放右上。逐行滑入 → 停留 → 最后 0.35 秒淡出。"""
    if not 3 <= seconds <= 5:
        logging.getLogger("hermes.graphics").warning("排名塔时长 %.1f 秒超出规范 3–5 秒", seconds)
    k = _k(size)
    parts = _tower_rows(rows, title, k)
    gap = max(1, round(4 * k))
    w = parts[0].width
    x0 = round(MARGIN * k) if side == "left" else size[0] - round(MARGIN * k) - w
    y = round((WATERMARK_BOX[3] + 24) * k) if side == "left" else round(CARD_BOX[1] * k)
    slots = []
    for im in parts:
        slots.append(y)
        y += im.height + gap
    travel = (x0 + w) if side == "left" else (size[0] - x0)
    sign = -1 if side == "left" else 1
    n = round(seconds * fps)
    stagger, slide, fade_t = 0.07, 0.35, 0.35

    def frame(t: float) -> Image.Image:
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        out_a = 1 - _ease_out((t - (seconds - fade_t)) / fade_t) if t > seconds - fade_t else 1
        for i, (im, yy) in enumerate(zip(parts, slots)):
            p = _ease_out((t - i * stagger) / slide)
            if p <= 0:
                continue
            a = min(1, p * 1.5) * out_a
            if a <= 0:
                continue
            canvas.alpha_composite(_fade(im, a), (x0 + round(sign * travel * (1 - p)), yy))
        return canvas

    if still:
        _save_png(frame(seconds - fade_t - 1e-3), still)
    return _write_mov((frame(i / fps).tobytes() for i in range(n)), out, size, fps)


# ---------- 花字标题卡 ----------

def _title_layer(text: str, sub: str | None, k: float) -> Image.Image:
    px = 150
    while px > 60 and font("Heavy", round(px * k)).getlength(text) > 1440 * k:
        px -= 4
    f = font("Heavy", round(px * k))
    so, si = max(2, round(16 * k)), max(1, round(6 * k))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), text, font=f, stroke_width=so)
    pad = round(40 * k)
    tw, th = r - l + 2 * pad, b - t + 2 * pad
    origin = (pad - l, pad - t)

    def mask(stroke: int) -> Image.Image:
        m = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(m).text(origin, text, font=f, fill=255, stroke_width=stroke, stroke_fill=255)
        return m

    m_out, m_in, m_fill = mask(so), mask(si), mask(0)
    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (tw, th), (0, 0, 0, 150))
    shadow.putalpha(m_out.point(lambda v: v * 150 // 255))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, 8 * k))), (round(6 * k), round(10 * k)))
    layer.paste((16, 22, 44, 255), (0, 0), m_out)
    layer.paste((255, 255, 255, 255), (0, 0), m_in)
    # 竖向渐变：亮黄 → 橙
    g = np.linspace(0, 1, th, dtype=np.float32)[:, None]
    top, bot = np.array((255, 244, 92), np.float32), np.array((255, 150, 0), np.float32)
    grad = (top * (1 - g[..., None]) + bot * g[..., None]).repeat(tw, axis=1)
    layer.paste(Image.fromarray(grad.astype(np.uint8)), (0, 0), m_fill)
    # 轻微右倾，更有动感
    sh = 0.12
    extra = round(sh * th)
    layer = layer.transform((tw + extra, th), Image.AFFINE, (1, sh, -extra, 0, 1, 0), resample=Image.BICUBIC)
    if not sub:
        return layer
    fs = font("Bold", round(52 * k))
    l2, t2, r2, b2 = probe.textbbox((0, 0), sub, font=fs)
    px2, py2 = round(30 * k), round(12 * k)
    pw, ph = r2 - l2 + 2 * px2, b2 - t2 + 2 * py2
    pill = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(pill)
    d.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=ph // 2, fill=(*F1_RED, 255))
    d.text((px2 - l2, py2 - t2), sub, font=fs, fill=(255, 255, 255, 255))
    W = max(layer.width, pw)
    out = Image.new("RGBA", (W, layer.height + ph - pad // 2), (0, 0, 0, 0))
    out.alpha_composite(layer, ((W - layer.width) // 2, 0))
    out.alpha_composite(pill, ((W - pw) // 2, layer.height - pad // 2))
    return out


def title_card(text: str, out: Path, *, sub: str | None = None, style: str = "花字", seconds: float | None = None,
               size: tuple[int, int] = (1920, 1080)) -> Path:
    """seconds=None 输出透明 PNG；否则输出弹出动画的透明 MOV（0.4 秒回弹入场，最后 0.25 秒淡出）。"""
    if style != "花字":
        raise ValueError(f"暂只支持花字样式：{style}")
    k = _k(size)
    layer = _title_layer(text, sub, k)
    # 放在字幕区之上、画面偏上居中
    cx, cy = size[0] // 2, round(470 * k)

    def place(img: Image.Image) -> Image.Image:
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        canvas.alpha_composite(img, (cx - img.width // 2, cy - img.height // 2))
        return canvas

    if seconds is None:
        return _save_png(place(layer), out)
    n = round(seconds * FPS)
    hold = place(layer).tobytes()

    def frames() -> Iterator[bytes]:
        for i in range(n):
            t = i / FPS
            if t < 0.4:
                s = max(0.05, 0.25 + 0.75 * _ease_back(t / 0.4))
                im = layer.resize((max(1, round(layer.width * s)), max(1, round(layer.height * s))), Image.BICUBIC)
                yield place(_fade(im, min(1, t / 0.12))).tobytes()
            elif seconds >= 1.2 and t > seconds - 0.25:
                yield place(_fade(layer, (seconds - t) / 0.25)).tobytes()
            else:
                yield hold

    return _write_mov(frames(), out, size, FPS)


# ---------- 预览 ----------

def _probe(path: Path) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                        "stream=width,height,pix_fmt,codec_name,nb_read_frames:format=duration", "-of", "json",
                        str(path)], capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    return {**j["streams"][0], **j.get("format", {})}


def _default_background(size: tuple[int, int]) -> Image.Image:
    """模拟素材底色的蓝灰渐变 + 暗角。"""
    W, H = size
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    t = (0.6 * y + 0.4 * x)[..., None]
    img = np.array((78, 96, 120), np.float32) * (1 - t) + np.array((28, 32, 42), np.float32) * t
    vig = 1 - 0.35 * (((x - 0.5) ** 2 + (y - 0.5) ** 2) * 2)[..., None]
    return Image.fromarray(np.clip(img * vig, 0, 255).astype(np.uint8))


def preview_mp4(overlay: Path, out: Path, *, background: Path | None = None, seconds: float = 3) -> Path:
    """叠到背景上导出 H.264 预览；顶层再压上品牌水印，方便检查与水印的避让。"""
    overlay, out = Path(overlay), Path(out)
    is_png = overlay.suffix.lower() == ".png"
    if is_png:
        size = Image.open(overlay).size
    else:
        p = _probe(overlay)
        size = (int(p["width"]), int(p["height"]))
    k = _k(size)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        bg = Path(td) / "bg.png"
        if background:
            im = Image.open(background).convert("RGB")
            r = max(size[0] / im.width, size[1] / im.height)
            im = im.resize((math.ceil(im.width * r), math.ceil(im.height * r)), Image.LANCZOS)
            l, t = (im.width - size[0]) // 2, (im.height - size[1]) // 2
            im.crop((l, t, l + size[0], t + size[1])).save(bg)
        else:
            _default_background(size).save(bg)
        cmd = [FFMPEG, "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(bg)]
        cmd += (["-loop", "1", "-framerate", str(FPS)] if is_png else []) + ["-i", str(overlay)]
        fc = "[0:v][1:v]overlay=0:0:format=auto[a]"
        last = "[a]"
        wm = config.BRAND_DIR / "watermark.png"
        if wm.exists():
            cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(wm)]
            wpx, m = round(160 * k), round(MARGIN * k)
            fc += f";[2:v]scale={wpx}:-1[w];[a][w]overlay={m}:{m}:format=auto[b]"
            last = "[b]"
        fc += f";{last}format=yuv420p[v]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-t", str(seconds), "-r", str(FPS), "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-movflags", "+faststart", "-an", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"ffmpeg 预览失败：{r.stderr[-500:]}")
    return out
