"""M4-7 封面定稿样稿：B 大字冲击 × 复盘/资讯/科普 × 横竖版 = 6 张，另出 B定稿_总览.jpg。
背景用程序生成的抽象赛车感画面，只作演示；正式每期用 cover.pick_cover_frame 从该期成片里挑帧。
用法：.venv/bin/python tests/live_cover_samples.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PIL import Image, ImageChops, ImageDraw, ImageFilter  # noqa: E402

from hermes import config, cover  # noqa: E402

OUT = config.DOCS_DIR / "里程碑" / "M4_素材和图形" / "封面样稿"
W, H = 1920, 1080


def abstract_bg(path, seed, top, bottom, streaks, glow):
    """渐变底 + 左下碳纤维斜纹 + 速度线 + 发光赛道线（充当画面主体）。"""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 1, H)[:, None, None]
    arr = (np.array(top) * (1 - t) + np.array(bottom) * t).repeat(W, 1)
    yy, xx = np.mgrid[0:H, 0:W]
    cell = ((xx // 16) + (yy // 16)) % 2
    carbon = 16 + 26 * np.where(cell == 1, (xx % 16) / 16, (yy % 16) / 16)
    m = np.clip(1 - np.hypot((xx / W - 0.05) / 0.6, (yy / H - 1.0) / 0.75), 0, 1)[..., None] * 0.85
    arr = arr * (1 - m) + carbon[..., None] * m
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    lines = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(lines)
    dx, dy = np.cos(np.radians(-12)), np.sin(np.radians(-12))
    for _ in range(170):
        x0, y0 = rng.uniform(-400, W), rng.uniform(0, H * 1.2)
        length, k = rng.uniform(300, 1400), rng.uniform(0.25, 1.0)
        col = tuple(int(c * k) for c in streaks[rng.integers(len(streaks))])
        d.line([(x0, y0), (x0 + length * dx, y0 + length * dy)], fill=col, width=int(rng.integers(1, 7)))
    img = ImageChops.screen(img, lines.filter(ImageFilter.GaussianBlur(1.5)))

    th = np.linspace(0, 2 * np.pi, 900)
    ph = rng.uniform(0, 2 * np.pi, 3)
    r = 1 + 0.25 * np.sin(2 * th + ph[0]) + 0.12 * np.sin(3 * th + ph[1]) + 0.08 * np.sin(5 * th + ph[2])
    pts = list(zip(W * 0.64 + r * np.cos(th) * 400, H * 0.44 + r * np.sin(th) * 240))
    track = Image.new("RGB", (W, H))
    ImageDraw.Draw(track).line(pts, fill=glow, width=26, joint="curve")
    img = ImageChops.screen(img, track.filter(ImageFilter.GaussianBlur(22)))
    core = Image.new("RGB", (W, H))
    ImageDraw.Draw(core).line(pts, fill=(255, 255, 255), width=7, joint="curve")
    img = ImageChops.screen(img, core.filter(ImageFilter.GaussianBlur(1)))
    img.save(path)
    return path


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cover-bg-"))
    bg_rb = abstract_bg(tmp / "bg_rb.png", 15, (8, 14, 44), (2, 4, 14),
                        [(54, 113, 198), (225, 6, 0), (255, 204, 0), (190, 205, 255)], (54, 113, 198))
    bg_news = abstract_bg(tmp / "bg_news.png", 26, (40, 16, 58), (235, 110, 40),
                          [(255, 140, 0), (255, 220, 170), (200, 60, 120)], (255, 128, 0))
    bg_sci = abstract_bg(tmp / "bg_sci.png", 37, (6, 40, 38), (2, 10, 12),
                         [(0, 179, 134), (170, 255, 220), (255, 214, 0)], (0, 179, 134))
    examples = {
        "复盘": cover.CoverSpec(frame=bg_rb, title="红牛巴库双登台", subtitle="阿塞拜疆大奖赛 复盘",
                              branch="复盘", accent="#3671C6"),
        "资讯": cover.CoverSpec(frame=bg_news, title="诺里斯为何改口道歉", branch="资讯"),
        "科普": cover.CoverSpec(frame=bg_sci, title="为什么 F1 要有冲刺赛", subtitle="一次讲清冲刺赛规则",
                              branch="科普", series_no=7),
    }
    names = {"landscape": "横版", "portrait": "竖版"}
    files = {}
    for ex, spec in examples.items():
        for o, cn in names.items():
            p = cover.render(spec, out=OUT / f"B定稿_{ex}_{cn}.png", orientation=o)
            files[ex, o] = p
            print(p.name, cover.readability(p))

    font = lambda w, s: cover._font(w, s)  # noqa: E731
    # 总览：每个分支一列，上横版，下竖版 + 1:1 像素的缩略图（横 320×180、竖 180×320）
    colw, gap, head, label = 900, 40, 150, 60
    lh, pw = round(colw * 9 / 16), 380
    ph = round(pw * 16 / 9)
    n = len(examples)
    ov = Image.new("RGB", (gap + n * (colw + gap), gap + head + label + lh + 30 + ph + gap), (242, 242, 247))
    d = ImageDraw.Draw(ov)
    d.text((gap, gap), f"{cover.DEFAULT_TEMPLATE}（2026-09-30 定稿）", font=font("Heavy", 44), fill=(17, 17, 17))
    d.text((gap, gap + 66), cover.TEMPLATES[cover.DEFAULT_TEMPLATE]["desc"] + "。底图仅为演示，正式每期从成片挑帧。",
           font=font("Regular", 24), fill=(80, 80, 80))
    for c, ex in enumerate(examples):
        x, y = gap + c * (colw + gap), gap + head
        d.text((x, y), ex, font=font("Bold", 34), fill=(17, 17, 17))
        y += label
        with Image.open(files[ex, "landscape"]) as im:
            ov.paste(im.convert("RGB").resize((colw, lh), Image.LANCZOS), (x, y))
        y += lh + 30
        with Image.open(files[ex, "portrait"]) as im:
            ov.paste(im.convert("RGB").resize((pw, ph), Image.LANCZOS), (x, y))
        tx, ty = x + pw + 40, y
        for o in ("landscape", "portrait"):
            size = cover.THUMB[o]
            d.text((tx, ty), f"{names[o]}缩略图 {size[0]}×{size[1]}", font=font("Medium", 20), fill=(90, 90, 90))
            with Image.open(files[ex, o]) as im:
                ov.paste(im.convert("RGB").resize(size, Image.LANCZOS), (tx, ty + 34))
            ty += 34 + size[1] + 30
    ov.save(OUT / "B定稿_总览.jpg", quality=88)
    print("输出目录：", OUT)


if __name__ == "__main__":
    main()
