"""M4-6 图形：小尺寸、少帧数，整套控制在 20 秒内。"""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from hermes import config, graphics as g

SIZE = (480, 270)
# conftest 把 HERMES_ROOT/CORE 指到临时目录；字体、水印、FastF1 缓存要用真实位置（只读）
REAL_ROOT = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1")
REAL_CACHE = Path("/Volumes/HermesCore/cache/fastf1")
FAST = pytest.mark.skipif(not (REAL_ROOT / "assets/fonts/SourceHanSansSC-Heavy.otf").exists(), reason="字体盘未挂载")


@pytest.fixture(autouse=True)
def real_assets(monkeypatch):
    monkeypatch.setattr(config, "FONTS_DIR", REAL_ROOT / "assets" / "fonts")
    monkeypatch.setattr(config, "BRAND_DIR", REAL_ROOT / "assets" / "brand")
    monkeypatch.setattr(config, "FASTF1_CACHE", REAL_CACHE)


def rgba(path):
    im = Image.open(path)
    assert im.mode == "RGBA"
    return im


def visible(im, box=None):
    a = np.asarray(im.crop(box) if box else im)[..., 3]
    return int((a > 16).sum())


def bright(im):
    px = np.asarray(im).astype(int)
    return int(((px[..., :3].min(axis=2) > 200) & (px[..., 3] > 200)).sum())


def yellow(im):
    px = np.asarray(im).astype(int)
    r, gg, b, a = px[..., 0], px[..., 1], px[..., 2], px[..., 3]
    return int(((r > 200) & (gg > 160) & (b < 150) & (a > 200)).sum())


@FAST
def test_font_renders_chinese_not_tofu():
    def draw(ch):
        im = Image.new("L", (80, 80), 0)
        ImageDraw.Draw(im).text((4, 4), ch, font=g.font("Bold", 64), fill=255)
        return np.asarray(im)
    a, b = draw("红"), draw("牛")
    assert (a > 0).sum() > 100 and (b > 0).sum() > 100
    assert (a != b).sum() > 200  # 豆腐块会画成同一个方框


def test_team_colors():
    for team in ("Mercedes", "Red Bull Racing", "Ferrari", "McLaren", "Aston Martin", "Alpine", "Williams",
                 "Racing Bulls", "Haas F1 Team", "Audi", "Cadillac"):
        assert g.TEAM_COLORS[team].startswith("#") and len(g.TEAM_COLORS[team]) == 7
    assert g.team_color("不存在") == g.DEFAULT_TEAM_COLOR


@FAST
def test_credit_tag_bottom_right_and_clear_of_watermark():
    im = g.credit_tag("来源：FIA 官方文件", SIZE)
    assert im.size == SIZE and im.mode == "RGBA"
    W, H = SIZE
    assert visible(im, (W // 2, H * 3 // 4, W, H)) > 50
    assert visible(im) == visible(im, (W // 2, H * 3 // 4, W, H))
    assert visible(im, g._box(SIZE, g.WATERMARK_BOX)) == 0
    # 超长出处也不越过右半屏，不压居中字幕
    long = g.credit_tag("来源：" + "非常长的出处说明" * 10, SIZE)
    xs = np.nonzero(np.asarray(long)[..., 3].any(axis=0))[0]
    assert xs.min() >= W - (g.CREDIT_MAX_W + g.MARGIN) * W / 1920 - 2


@FAST
def test_screenshot_overlay_marker(tmp_path):
    shot = tmp_path / "shot.png"
    im = Image.new("RGB", (640, 400), "white")
    d = ImageDraw.Draw(im)
    for y in range(40, 380, 30):
        d.text((30, y), "FIA DOCUMENT 文件 " * 3, font=g.font("Regular", 18), fill="black")
    im.save(shot)
    plain = rgba(g.screenshot_overlay(shot, tmp_path / "plain.png", size=SIZE))
    out = rgba(g.screenshot_overlay(shot, tmp_path / "o.png", highlights=[(100, 100, 400, 140)],
                                    underline=[(80, 360, 300)], credit="来源：FIA 官网", size=SIZE))
    assert out.size == SIZE
    assert np.asarray(out)[0, 0, 3] == 0  # 卡片外透明
    assert visible(out, g._box(SIZE, g.WATERMARK_BOX)) == 0
    assert yellow(plain) == 0 and yellow(out) > 200


@FAST
@pytest.mark.parametrize("kind,data", [
    ("laps", {"drivers": [{"name": "拉塞尔", "team": "Mercedes", "laps": [2, 3, 4], "times": [107.1, 106.9, 107.4]},
                          {"name": "安东内利", "team": "Mercedes", "laps": [2, 3, 4], "times": [107.5, 107.2, 107.0]}],
              "note": "已剔除进出站圈"}),
    ("gap", {"items": [{"name": "维斯塔潘", "team": "Red Bull Racing", "value": 0.196},
                       {"name": "哈贾尔", "team": "Red Bull Racing", "value": 10.704}]}),
    ("bar", {"items": [{"name": "梅奔", "team": "Mercedes", "value": 25}, {"name": "红牛", "value": 18}],
             "unit": "分"}),
])
def test_data_chart(tmp_path, kind, data):
    a = rgba(g.data_chart(kind, data, tmp_path / "a.png", title="巴库圈速对比", credit="来源：FastF1", size=SIZE))
    b = rgba(g.data_chart(kind, data, tmp_path / "b.png", title="", size=SIZE))
    assert a.size == SIZE
    assert np.asarray(a)[0, 0, 3] == 0
    assert visible(a, g._box(SIZE, g.WATERMARK_BOX)) == 0
    assert bright(a) > bright(b) + 50  # 标题和出处的中文真的画出来了


def test_data_chart_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        g.data_chart("pie", {}, tmp_path / "x.png", title="x")


def test_laps_from_fastf1_cached():
    try:
        data = g.laps_from_fastf1(2026, 15, "R", ["RUS", "VER"])
    except RuntimeError as e:
        pytest.skip(f"缓存里没有阿塞拜疆圈速：{e}")
    assert [d["code"] for d in data["drivers"]] == ["RUS", "VER"]
    d = data["drivers"][0]
    assert d["team"] == "Mercedes" and len(d["laps"]) == len(d["times"]) > 20
    assert 1 not in d["laps"] and all(90 < t < 130 for t in d["times"])


@FAST
def test_ranking_tower(tmp_path):
    rows = [{"pos": i, "name": n, "team": t, "gap": gp} for i, (n, t, gp) in enumerate(
        [("拉塞尔", "Mercedes", "1:38:02.143"), ("维斯塔潘", "Red Bull Racing", "+0.196"),
         ("哈贾尔", "Red Bull Racing", "+10.704")], 1)]
    still = tmp_path / "t.png"
    mov = g.ranking_tower(rows, tmp_path / "t.mov", seconds=3, fps=10, title="正赛前三", size=SIZE, still=still)
    p = g._probe(mov)
    assert (p["width"], p["height"]) == SIZE
    assert "yuva" in p["pix_fmt"]
    assert int(p["nb_read_frames"]) == 30
    im = rgba(still)
    assert visible(im) > 500
    assert visible(im, g._box(SIZE, g.WATERMARK_BOX)) == 0
    assert visible(im, (0, round(g.SUBTITLE_TOP * SIZE[1] / 1080), SIZE[0], SIZE[1])) == 0


@FAST
def test_title_card_png_and_mov(tmp_path):
    im = rgba(g.title_card("红牛巴库双登台", tmp_path / "t.png", sub="阿塞拜疆大奖赛复盘", size=SIZE))
    assert im.size == SIZE and visible(im) > 2000
    assert np.asarray(im)[0, 0, 3] == 0
    other = rgba(g.title_card("梅奔巴库夺冠", tmp_path / "t2.png", size=SIZE))
    assert (np.asarray(im)[..., 3] != np.asarray(other)[..., 3]).sum() > 1000
    mov = g.title_card("红牛巴库双登台", tmp_path / "t.mov", sub="复盘", seconds=1, size=SIZE)
    p = g._probe(mov)
    assert (p["width"], p["height"]) == SIZE and "yuva" in p["pix_fmt"]
    assert int(p["nb_read_frames"]) == 30


@FAST
def test_preview_mp4(tmp_path):
    png = g.title_card("巴库", tmp_path / "t.png", size=SIZE)
    mp4 = g.preview_mp4(png, tmp_path / "p.mp4", seconds=1)
    p = g._probe(mp4)
    assert p["codec_name"] == "h264" and (p["width"], p["height"]) == SIZE
    assert abs(float(p["duration"]) - 1) < 0.1
    mov = g.title_card("巴库", tmp_path / "t.mov", seconds=1, size=SIZE)
    bg = tmp_path / "bg.jpg"
    Image.new("RGB", (300, 300), (90, 30, 30)).save(bg)
    p2 = g._probe(g.preview_mp4(mov, tmp_path / "p2.mp4", background=bg, seconds=1))
    assert (p2["width"], p2["height"]) == SIZE
