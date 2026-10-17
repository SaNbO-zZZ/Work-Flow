"""D4 信息面板：赛程卡、轮胎策略图、积分榜、数据卡。与赛道简图同一面板（trackmap.PANEL_BOX、同一底板和字体），整帧透明 PNG。

坐标都按面板设计尺寸 trackmap.BASE_W × BASE_H 写，乘 s 换成超采样像素。
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from . import graphics, trackmap

WHITE = (255, 255, 255, 255)
GREY = trackmap.GREY + (255,)
RED = trackmap.RED + (255,)
# 倍耐力配色：硬=白、中=黄、软=红
TYRE_COLORS = {"hard": (238, 238, 240), "medium": (255, 206, 0), "soft": (222, 36, 28)}
TYRE_ZH = {"hard": "硬胎", "medium": "中性胎", "soft": "软胎"}
TYRE_MARK = {"hard": "H", "medium": "M", "soft": "S"}
MAIN_RACE = ("正赛", "冲刺赛")
MAX_STRATEGIES = 3


def _layer(base: Image.Image) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """内容画在单独的透明层上，最后 alpha_composite 到底板：直接在 RGBA 底板上画半透明填充会覆盖 alpha，
    在底板上挖出半透明的洞（亮画面透出来，字看不清）。"""
    top = Image.new("RGBA", base.size, (0, 0, 0, 0))
    return top, ImageDraw.Draw(top)


def _scale(size: tuple[int, int]) -> float:
    return trackmap.SS * size[0] / 1920 * trackmap.ZOOM


def _fit(text: str, weight: str, px: float, max_w: float, min_px: float):
    f = graphics.font(weight, round(px))
    while f.getlength(text) > max_w and px > min_px:
        px -= 1
        f = graphics.font(weight, round(px))
    return f


def _header(d: ImageDraw.ImageDraw, s: float, subtitle: str | None, title: str, tail: str = "",
            right: float = 0.0) -> None:
    """左上：红色小标题 + 大标题（tail 用细一号的灰字跟在后面）。right 是右侧要留给别的内容的宽度。"""
    x, y = 28 * s, 24 * s
    if subtitle:
        d.text((x, y), subtitle, font=graphics.font("Bold", round(18 * s)), fill=RED, anchor="la")
    y = 52 * s
    max_w = (trackmap.BASE_W - 56) * s - right
    ft = graphics.font("Medium", round(24 * s))
    fh = _fit(title, "Heavy", 42 * s, max_w - (ft.getlength(tail) if tail else 0), 26 * s)
    base = y + fh.getbbox("赛")[3]
    d.text((x, base), title, font=fh, fill=WHITE, anchor="ls")
    if tail:
        d.text((x + fh.getlength(title) + 6 * s, base), tail, font=ft, fill=GREY, anchor="ls")


def _footer(d: ImageDraw.ImageDraw, s: float, text: str) -> None:
    H = trackmap.BASE_H * s
    f = _fit(text, "Regular", 14 * s, (trackmap.BASE_W - 56) * s, 10 * s)
    d.text((28 * s, H - 22 * s), text, font=f, fill=GREY[:3] + (220,), anchor="ls")


# ---------- 赛程卡 ----------

def render_schedule(sched: dict, out_png: Path, *, size: tuple[int, int] = (1920, 1080)) -> Path:
    """sched：{"name", "format", "season", "round", "days": [{"date", "weekday", "sessions": [{"name", "time"}]}],
    "credit"}；时间都是北京时间。"""
    s = _scale(size)
    img = trackmap.panel_base(s)
    top, d = _layer(img)
    fmt = sched.get("format") or "常规"
    fl, fv = graphics.font("Medium", round(15 * s)), graphics.font("Heavy", round(40 * s))
    rx = (trackmap.BASE_W - 28) * s
    d.text((rx, 34 * s), "赛制", font=fl, fill=GREY, anchor="ra")
    d.text((rx, 102 * s), fmt, font=fv, fill=WHITE, anchor="rs")
    sub = f"{sched['season']} 赛季第 {sched['round']} 站" if sched.get("season") and sched.get("round") else None
    _header(d, s, sub, f"{sched['name']} 赛程", "（北京时间）", right=fv.getlength(fmt) + 30 * s)

    days = sched["days"]
    n = max(1, len(days))
    gap = 16
    x0, y0, y1 = 28, 132, trackmap.BASE_H - 54
    cw = (trackmap.BASE_W - 56 - gap * (n - 1)) / n
    rows = max((len(day["sessions"]) for day in days), default=1)
    head_h = 50
    rh = min(112, (y1 - y0 - head_h) / max(1, rows))
    for i, day in enumerate(days):
        cx = x0 + i * (cw + gap)
        fd = _fit(day["date"], "Bold", 24 * s, cw * s * 0.62, 14 * s)
        d.text((cx * s, (y0 + 28) * s), day["date"], font=fd, fill=WHITE, anchor="ls")
        d.text(((cx + cw) * s, (y0 + 28) * s), day.get("weekday") or "", font=graphics.font("Medium", round(18 * s)),
               fill=GREY, anchor="rs")
        d.rectangle((cx * s, (y0 + 38) * s, (cx + cw) * s, (y0 + 41) * s), fill=RED)
        for j, sess in enumerate(day["sessions"]):
            ry = y0 + head_h + j * rh
            box = (cx * s, (ry + 4) * s, (cx + cw) * s, (ry + rh - 6) * s)
            d.rounded_rectangle(box, radius=8 * s, fill=(255, 255, 255, 16))
            main = sess["name"] in MAIN_RACE
            if main:
                d.rounded_rectangle((box[0], box[1], box[0] + 5 * s, box[3]), radius=2 * s, fill=RED)
            fn = graphics.font("Medium", round(min(20, rh * 0.2) * s))
            d.text(((cx + 16) * s, (ry + 4 + rh * 0.14) * s), sess["name"], font=fn, fill=RED if main else GREY,
                   anchor="la")
            ft = _fit(sess["time"], "Heavy", min(46, rh * 0.44) * s, (cw - 28) * s, 14 * s)
            d.text(((cx + 16) * s, (ry + rh - 6 - rh * 0.12) * s), sess["time"], font=ft, fill=WHITE, anchor="ls")
    _footer(d, s, sched.get("credit") or "数据：FastF1 赛历")
    img.alpha_composite(top)
    return graphics._save_png(trackmap.place_panel(img, size), out_png)


# ---------- 轮胎策略图 ----------

def _lap_mid(text: str) -> float | None:
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    return sum(nums[:2]) / len(nums[:2]) if nums else None


def _tyre_icon(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, compound: str) -> None:
    color = TYRE_COLORS[compound] + (255,)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(22, 22, 26, 255), outline=(70, 70, 78, 255),
              width=max(1, round(r * 0.05)))
    rr = r * 0.78
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=color, width=max(1, round(r * 0.15)))
    d.text((cx, cy), TYRE_MARK[compound], font=graphics.font("Heavy", round(r * 0.72)), fill=color, anchor="mm")


def _positions(st: dict, race_laps: int | None) -> list[float]:
    """各段终点在横条上的比例；有总圈数且进站圈都读得出、递增时按圈数，否则等分。"""
    n = len(st["stints"])
    even = [(i + 1) / n for i in range(n)]
    if not race_laps:
        return even
    mids = [_lap_mid(p) for p in st["pit_laps"]]
    if any(m is None for m in mids):
        return even
    pos = [m / race_laps for m in mids] + [1.0]
    if all(a < b for a, b in zip([0.0] + pos, pos)):
        return pos
    return even


def render_tyres(info: dict, out_png: Path, *, size: tuple[int, int] = (1920, 1080)) -> Path:
    """info：{"name", "compounds": {"hard": "C1"|None, ...}, "race_laps", "strategies": [{"name", "stints": [配方],
    "pit_laps": [文字]}], "sites": [站点名]}。"""
    s = _scale(size)
    img = trackmap.panel_base(s)
    top, d = _layer(img)
    _header(d, s, info.get("name"), "轮胎配方与预计策略")

    # 上半：三种干胎配方
    W = trackmap.BASE_W
    cw = (W - 56) / 3
    yc, r = 176, 34
    for i, comp in enumerate(("hard", "medium", "soft")):
        cx = 28 + i * cw
        _tyre_icon(d, (cx + r + 4) * s, yc * s, r * s, comp)
        code = info["compounds"].get(comp) or "—"
        d.text(((cx + 2 * r + 20) * s, (yc + 8) * s), code, font=graphics.font("Heavy", round(42 * s)), fill=WHITE,
               anchor="ls")
        d.text(((cx + 2 * r + 22) * s, (yc + 32) * s), TYRE_ZH[comp], font=graphics.font("Medium", round(17 * s)),
               fill=GREY, anchor="ls")
    d.line((28 * s, 236 * s, (W - 28) * s, 236 * s), fill=(255, 255, 255, 34), width=max(1, round(s)))

    # 下半：预计策略
    fl = graphics.font("Medium", round(16 * s))
    d.text((28 * s, 262 * s), "预计策略", font=fl, fill=GREY, anchor="ls")
    laps = info.get("race_laps")
    if laps:
        d.text(((W - 28) * s, 262 * s), f"全程 {laps} 圈", font=fl, fill=GREY, anchor="rs")
    strategies = info.get("strategies")[:MAX_STRATEGIES] if info.get("strategies") else []
    y0, y1 = 274, trackmap.BASE_H - 50
    if not strategies:
        d.text((W / 2 * s, (y0 + y1) / 2 * s), "官方暂未给出预计策略", font=graphics.font("Medium", round(20 * s)),
               fill=GREY, anchor="mm")
    rh = min(56, (y1 - y0) / max(1, len(strategies)))
    bx0, bx1 = 132, W - 28
    for i, st in enumerate(strategies):
        ry = y0 + i * rh
        bh = min(24, rh * 0.42)
        by1 = ry + rh - 6
        by0 = by1 - bh
        name_f = _fit(st["name"], "Bold", 20 * s, (bx0 - 40) * s, 12 * s)
        d.text((28 * s, (by0 + by1) / 2 * s), st["name"], font=name_f, fill=WHITE, anchor="lm")
        ends = _positions(st, laps)
        a = 0.0
        for k, (comp, b) in enumerate(zip(st["stints"], ends)):
            xa, xb = bx0 + (bx1 - bx0) * a, bx0 + (bx1 - bx0) * b
            col = TYRE_COLORS[comp]
            d.rounded_rectangle(((xa + (1.5 if k else 0)) * s, by0 * s, (xb - (1.5 if k < len(ends) - 1 else 0)) * s,
                                 by1 * s), radius=4 * s, fill=col + (255,))
            dark = comp != "soft"
            seg_f = graphics.font("Bold", round(min(15, bh * 0.62) * s))
            label = (info["compounds"].get(comp) or "") + " " + TYRE_ZH[comp]
            if seg_f.getlength(label) > (xb - xa - 8) * s:
                label = TYRE_MARK[comp]
            d.text(((xa + xb) / 2 * s, (by0 + by1) / 2 * s), label.strip(), font=seg_f,
                   fill=(20, 20, 24, 255) if dark else WHITE, anchor="mm")
            if k < len(ends) - 1:
                pit = (st["pit_laps"][k] or "").strip()
                if pit:
                    pit = pit if "圈" in pit else f"第 {pit} 圈"
                    d.text((xb * s, (by0 - 4) * s), pit, font=graphics.font("Medium", round(13 * s)), fill=GREY,
                           anchor="ms")
            a = b
    sites = "、".join(info.get("sites") or [])
    _footer(d, s, f"来源：{sites}" if sites else "")
    img.alpha_composite(top)
    return graphics._save_png(trackmap.place_panel(img, size), out_png)


# ---------- 积分榜 ----------

def _pts(v: float) -> str:
    """积分去掉多余的 .0（半分制下保留 .5）。"""
    return f"{v:g}"


def render_standings(table: dict, out_png: Path, *, size: tuple[int, int] = (1920, 1080)) -> Path:
    """table：{"kind": "drivers"|"teams", "title", "subtitle", "rows": [{"pos", "name", "team", "color", "points",
    "hl"}], "gap_after": 第几行之后画省略号（前 10 名后补的点名车手）或 None, "credit"}。
    hl=True 的行是本段口播点到名的，底色提亮、名字加粗。"""
    s = _scale(size)
    img = trackmap.panel_base(s)
    top, d = _layer(img)
    _header(d, s, table.get("subtitle"), table["title"])
    W = trackmap.BASE_W
    drivers = table.get("kind", "drivers") == "drivers"
    rows = table["rows"]
    gap_after = table.get("gap_after")
    x_pos, x_bar, x_name, x_team, x_pts, x_gap = 58, 68, 82, 300, W - 116, W - 28
    fl = graphics.font("Medium", round(13 * s))
    yh = 134
    d.text((x_pos * s, yh * s), "排名", font=fl, fill=GREY, anchor="rs")
    d.text((x_name * s, yh * s), "车手" if drivers else "车队", font=fl, fill=GREY, anchor="ls")
    if drivers:
        d.text((x_team * s, yh * s), "车队", font=fl, fill=GREY, anchor="ls")
    d.text((x_pts * s, yh * s), "积分", font=fl, fill=GREY, anchor="rs")
    d.text((x_gap * s, yh * s), "与榜首差距", font=fl, fill=GREY, anchor="rs")
    d.line((28 * s, (yh + 7) * s, (W - 28) * s, (yh + 7) * s), fill=(255, 255, 255, 34), width=max(1, round(s)))
    y0, y1 = yh + 11, trackmap.BASE_H - 46
    n_eff = len(rows) + (0.6 if gap_after is not None else 0)
    rh = min(31, (y1 - y0) / max(1, n_eff))
    lead = rows[0]["points"] if rows and rows[0]["pos"] == 1 else None
    fs = min(19, rh * 0.62)
    ry = y0
    for i, r in enumerate(rows):
        if gap_after is not None and i == gap_after:
            d.text((x_name * s, (ry + rh * 0.3) * s), "……", font=graphics.font("Bold", round(fs * 0.8 * s)),
                   fill=GREY, anchor="lm")
            ry += rh * 0.6
        cy = ry + rh / 2
        if r.get("hl"):
            d.rounded_rectangle((22 * s, (ry + 1.5) * s, (W - 22) * s, (ry + rh - 1.5) * s), radius=6 * s,
                                fill=(255, 255, 255, 30))
        d.text((x_pos * s, cy * s), str(r["pos"]), font=graphics.font("Bold", round(fs * s)),
               fill=WHITE if r["pos"] <= 3 or r.get("hl") else GREY, anchor="rm")
        col = graphics._hex(r.get("color") or graphics.DEFAULT_TEAM_COLOR) + (255,)
        d.rounded_rectangle((x_bar * s, (ry + rh * 0.2) * s, (x_bar + 5) * s, (ry + rh * 0.8) * s), radius=2 * s,
                            fill=col)
        name_w = (x_team - x_name - 14) if drivers else (x_pts - x_name - 70)
        fn = _fit(r["name"], "Heavy" if r.get("hl") else "Bold", fs * s, name_w * s, 10 * s)
        d.text((x_name * s, cy * s), r["name"], font=fn, fill=WHITE, anchor="lm")
        if drivers and r.get("team"):
            ft = _fit(r["team"], "Medium", fs * 0.84 * s, (x_pts - x_team - 70) * s, 10 * s)
            d.text((x_team * s, cy * s), r["team"], font=ft, fill=GREY, anchor="lm")
        d.text((x_pts * s, cy * s), _pts(r["points"]), font=graphics.font("Heavy", round(fs * s)), fill=WHITE,
               anchor="rm")
        gap = "—" if lead is None or r["pos"] == 1 else f"-{_pts(lead - r['points'])}"
        d.text((x_gap * s, cy * s), gap, font=graphics.font("Medium", round(fs * 0.84 * s)), fill=GREY, anchor="rm")
        ry += rh
    _footer(d, s, table.get("credit") or "数据：FastF1 / Ergast")
    img.alpha_composite(top)
    return graphics._save_png(trackmap.place_panel(img, size), out_png)


# ---------- 数据卡 ----------

def render_datacard(card: dict, out_png: Path, *, size: tuple[int, int] = (1920, 1080)) -> Path:
    """card：{"title", "subtitle", "rows": [{"label", "value", "bar": 0–1 或 None, "color": 可选 #hex}], "credit"}。
    2–4 行“项目 数值”；行里有 bar 时在项目名下面画一条横条做对比（同一单位、按最大值归一）。"""
    s = _scale(size)
    img = trackmap.panel_base(s)
    top, d = _layer(img)
    _header(d, s, card.get("subtitle"), card["title"])
    W = trackmap.BASE_W
    rows = card["rows"][:4]
    y0, y1 = 122, trackmap.BASE_H - 48
    rh = (y1 - y0) / max(1, len(rows))
    fv_px = min(48, rh * 0.46)
    fonts = [_fit(r["value"], "Heavy", fv_px * s, (W - 56) * 0.55 * s, 18 * s) for r in rows]
    label_w = W - 56 - max((f.getlength(r["value"]) / s for f, r in zip(fonts, rows)), default=0) - 24   # 横条同一长度才可比
    for i, (r, fv) in enumerate(zip(rows, fonts)):
        ry = y0 + i * rh
        cy = ry + rh / 2
        d.text(((W - 28) * s, cy * s), r["value"], font=fv, fill=WHITE, anchor="rm")
        bar = r.get("bar")
        fl = _fit(r["label"], "Medium", min(22, rh * 0.24) * s, label_w * s, 12 * s)
        if bar is None:
            d.text((28 * s, cy * s), r["label"], font=fl, fill=GREY, anchor="lm")
        else:
            d.text((28 * s, (cy - 4) * s), r["label"], font=fl, fill=GREY, anchor="ls")
            bh = min(10, rh * 0.1)
            col = graphics._hex(r["color"]) + (255,) if r.get("color") else RED
            d.rounded_rectangle((28 * s, (cy + 4) * s, (28 + label_w) * s, (cy + 4 + bh) * s), radius=bh / 2 * s,
                                fill=(255, 255, 255, 22))
            d.rounded_rectangle((28 * s, (cy + 4) * s, (28 + max(bh, label_w * max(0.0, min(1.0, bar)))) * s,
                                 (cy + 4 + bh) * s), radius=bh / 2 * s, fill=col)
        if i < len(rows) - 1:
            d.line((28 * s, (ry + rh) * s, (W - 28) * s, (ry + rh) * s), fill=(255, 255, 255, 26),
                   width=max(1, round(s)))
    _footer(d, s, card.get("credit") or "")
    img.alpha_composite(top)
    return graphics._save_png(trackmap.place_panel(img, size), out_png)
