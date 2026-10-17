"""M4-7 统一封面模板：定稿 B 为默认、三套模板横竖版渲染、超长标题不越界、科普"第 N 期"、可读性检查、候选帧初筛。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from hermes import config, cover

REAL_ROOT = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1")


@pytest.fixture(autouse=True)
def real_assets(monkeypatch):
    fonts, brand = REAL_ROOT / "assets" / "fonts", REAL_ROOT / "assets" / "brand"
    if not (fonts / "SourceHanSansSC-Heavy.otf").exists() or not (brand / "watermark.png").exists():
        pytest.skip("外置盘字体或水印不在")
    monkeypatch.setattr(config, "FONTS_DIR", fonts)
    monkeypatch.setattr(config, "BRAND_DIR", brand)


@pytest.fixture(scope="module")
def frame(tmp_path_factory):
    """带纹理的合成帧：左暗右亮，右侧有一块高细节"主体"。"""
    rng = np.random.default_rng(7)
    w, h = 1920, 1080
    arr = np.linspace(30, 170, w)[None, :, None].repeat(h, 0).repeat(3, 2)
    arr += rng.normal(0, 12, (h, w, 3))
    arr[300:700, 1350:1700] = rng.integers(0, 255, (400, 350, 3))
    p = tmp_path_factory.mktemp("frame") / "frame.png"
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(p)
    return p


def _meta(p):
    with Image.open(p) as im:
        return json.loads(im.info[cover.META_KEY])


def _inside(bb, box):
    return bb[0] >= box[0] and bb[1] >= box[1] and bb[2] <= box[2] and bb[3] <= box[3]


def test_three_templates_distinct():
    assert len(cover.TEMPLATES) == 3
    assert all({"draw", "desc", "fits"} <= set(t) for t in cover.TEMPLATES.values())
    assert cover._template_key("B") == "B 大字冲击"
    with pytest.raises(KeyError):
        cover._template_key("Z")


def test_default_template_is_b(tmp_path, frame):
    assert cover.DEFAULT_TEMPLATE == "B 大字冲击" and cover.DEFAULT_TEMPLATE in cover.TEMPLATES
    spec = cover.CoverSpec(frame=frame, title="巴库复盘")
    assert _meta(cover.render(spec, out=tmp_path / "d.png"))["template"] == "B 大字冲击"
    assert _meta(cover.render(spec, None, tmp_path / "e.png"))["template"] == "B 大字冲击"
    with pytest.raises(TypeError):
        cover.render(spec)


@pytest.mark.parametrize("branch", ["复盘", "资讯", "前瞻", "科普"])
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_default_template_all_chinese(tmp_path, frame, branch, orientation):
    spec = cover.CoverSpec(frame=frame, title="为什么 F1 要有冲刺赛" if branch == "科普" else "红牛巴库双登台",
                           subtitle="阿塞拜疆大奖赛", branch=branch, gp="阿塞拜疆大奖赛", series_no=7)
    m = _meta(cover.render(spec, out=tmp_path / "zh.png", orientation=orientation))
    # 只有用户给的标题里可以有英文（如 F1），模板自己加的字样必须全是中文
    for it in m["items"]:
        if it["kind"] != "title":
            assert not re.search(r"[A-Za-z]", it["text"]), it
    series = [i["text"] for i in m["items"] if i["kind"] == "series"]
    assert series == (["第 7 期"] if branch == "科普" else [])


@pytest.mark.parametrize("tpl", list(cover.TEMPLATES))
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_render_sizes_and_safe_area(tmp_path, frame, tpl, orientation):
    spec = cover.CoverSpec(frame=frame, title="红牛巴库双登台", subtitle="阿塞拜疆大奖赛 复盘",
                           branch="复盘", gp="阿塞拜疆大奖赛", accent="#3671C6")
    out = cover.render(spec, tpl, tmp_path / "c.png", orientation=orientation)
    with Image.open(out) as im:
        assert im.size == cover.SIZES[orientation]
        assert im.mode == "RGB"
    m = _meta(out)
    kinds = {i["kind"] for i in m["items"]}
    assert {"title", "subtitle", "branch", "gp"} <= kinds
    for it in m["items"]:
        assert _inside(it["bbox"], m["safe"]), it


@pytest.mark.parametrize("tpl", list(cover.TEMPLATES))
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_long_title_fits_safe_area(tmp_path, frame, tpl, orientation):
    spec = cover.CoverSpec(frame=frame, title="维斯塔潘巴库连续第三次杆位后在正赛第一圈被迫退赛引发车队内部激烈争论" * 2,
                           subtitle="一个非常非常长的副标题用来检查会不会跑出画面安全区域的边界",
                           branch="前瞻", gp="阿塞拜疆大奖赛与巴林大奖赛背靠背双站")
    out = cover.render(spec, tpl, tmp_path / "long.png", orientation=orientation)
    m = _meta(out)
    assert m["title_lines"] and m["title_lines"][-1]["text"].endswith("…")
    for it in m["items"]:
        assert _inside(it["bbox"], m["safe"]), it


def test_short_title_gets_big_font(tmp_path, frame):
    spec = cover.CoverSpec(frame=frame, title="头哥夺冠")
    m = _meta(cover.render(spec, "A", tmp_path / "s.png"))
    assert len(m["title_lines"]) == 1 and m["title_size"] >= 140


@pytest.mark.parametrize("tpl", list(cover.TEMPLATES))
def test_series_no_only_for_science(tmp_path, frame, tpl):
    def run(branch, no, name):
        spec = cover.CoverSpec(frame=frame, title="尾流到底怎么用", branch=branch, series_no=no)
        p = cover.render(spec, tpl, tmp_path / name)
        with Image.open(p) as im:
            return np.asarray(im), _meta(p)

    sci, m = run("科普", 7, "a.png")
    sci0, _ = run("科普", None, "b.png")
    assert any(i["kind"] == "series" and i["text"] == "第 7 期" for i in m["items"])
    assert not any("No." in i["text"] for i in m["items"])
    assert not np.array_equal(sci, sci0)
    news, m2 = run("资讯", 7, "c.png")
    news0, _ = run("资讯", None, "d.png")
    assert all(i["kind"] != "series" for i in m2["items"])
    assert np.array_equal(news, news0)


def test_watermark_top_left(tmp_path, frame):
    p = cover.render(cover.CoverSpec(frame=frame, title="测试"), "C", tmp_path / "w.png", orientation="portrait")
    assert _meta(p)["watermark"] == [28, 28, 188, 188]


def test_focus_finds_subject(frame):
    with Image.open(frame) as im:
        fx, fy = cover._focus(im.convert("RGB"))
    assert fx > 0.6 and 0.3 < fy < 0.7


def test_split_balanced_and_keeps_words():
    f = cover._font("Heavy", 100)
    assert cover._split("诺里斯为何改口道歉", 2, f) in (["诺里斯为何", "改口道歉"], ["诺里斯为", "何改口道歉"])
    assert any("DRS" in l for l in cover._split("新规DRS取消了", 2, f))
    assert not any(l.startswith("，") for l in cover._split("头哥赢了，木瓜哭了", 2, f))


def test_fit_prefers_fewer_lines_and_honors_newline():
    size, lines = cover._fit("诺里斯为何改口道歉", "Heavy", 824, 4, 170, 72)
    assert len(lines) == 2
    assert cover._fit("诺里斯\n为何改口道歉", "Heavy", 824, 4, 170, 72)[1] == ["诺里斯", "为何改口道歉"]


@pytest.mark.parametrize("tpl", list(cover.TEMPLATES))
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_readability_reasonable(tmp_path, frame, tpl, orientation):
    spec = cover.CoverSpec(frame=frame, title="诺里斯为何改口道歉", branch="资讯")
    r = cover.readability(cover.render(spec, tpl, tmp_path / "r.png", orientation=orientation))
    assert set(r) == {"contrast", "min_text_px", "ok"}
    assert 1 <= r["contrast"] <= 21
    assert r["contrast"] >= cover.MIN_CONTRAST and r["min_text_px"] >= cover.MIN_TEXT_PX and r["ok"]


def test_readability_flags_low_contrast(tmp_path):
    img = Image.new("RGB", (1920, 1080), (140, 140, 140))
    f = cover._font("Heavy", 60)
    d = ImageDraw.Draw(img)
    d.text((200, 500), "灰字灰底", font=f, fill=(120, 120, 120), anchor="ls")
    bb = list(d.textbbox((200, 500), "灰字灰底", font=f, anchor="ls"))
    meta = {"orientation": "landscape", "size": [1920, 1080], "title_glyph_px": 55,
            "title_lines": [{"text": "灰字灰底", "bbox": bb, "fill": [120, 120, 120]}]}
    info = PngInfo()
    info.add_itxt(cover.META_KEY, json.dumps(meta, ensure_ascii=False))
    p = tmp_path / "bad.png"
    img.save(p, pnginfo=info)
    r = cover.readability(p)
    assert r["contrast"] < 2 and r["min_text_px"] < cover.MIN_TEXT_PX and r["ok"] is False


def test_jpeg_output_keeps_meta(tmp_path, frame):
    p = cover.render(cover.CoverSpec(frame=frame, title="巴库复盘"), "B", tmp_path / "c.jpg")
    assert cover.readability(p)["contrast"] > 1


def test_bad_orientation(tmp_path, frame):
    with pytest.raises(ValueError):
        cover.render(cover.CoverSpec(frame=frame, title="x"), "A", tmp_path / "x.png", orientation="square")


# ---------- 候选帧初筛 ----------

def _synth_video(path, segments):
    """segments: [(类型, 秒数)]，类型 good=正常测试图、black=黑场、blur=重度模糊。"""
    src = {"good": "testsrc2=s=640x360:r=10:d={d}",
           "black": "color=c=black:s=640x360:r=10:d={d}",
           "blur": "testsrc2=s=640x360:r=10:d={d},gblur=sigma=25"}
    parts = [f"{src[k].format(d=d)},format=yuv420p[v{i}]" for i, (k, d) in enumerate(segments)]
    graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(len(segments))) \
        + f"concat=n={len(segments)}:v=1:a=0[out]"
    subprocess.run([cover.FFMPEG, "-v", "error", "-y", "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)
    return path


def _t(p):
    return float(re.search(r"_(\d+\.\d+)s\.png$", p.name).group(1))


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") and not Path(cover.FFMPEG).exists(), reason="没有 ffmpeg")


@needs_ffmpeg
def test_pick_cover_frame_skips_black_and_blur(tmp_path):
    # 0-10 正常，10-15 黑场，15-20 模糊，20-30 正常
    v = _synth_video(tmp_path / "v.mp4", [("good", 10), ("black", 5), ("blur", 5), ("good", 10)])
    out = cover.pick_cover_frame(v, candidates=6, out_dir=tmp_path / "picks")
    assert len(out) == 6 and all(p.exists() for p in out)
    ts = [_t(p) for p in out]
    assert ts == sorted(ts)
    for t, p in zip(ts, out):
        assert 1.5 - 1e-6 <= t <= 28.5 + 1e-6          # 片头片尾各 5% 不取
        assert not (10 <= t <= 20), t                   # 黑场和模糊段一帧都不要
        luma, sharp = cover._frame_stats(p)
        assert luma >= cover.DARK_LUMA and sharp > cover.BLUR_ABS
    assert any(t < 10 for t in ts) and any(t > 20 for t in ts)   # 前后两段都有，分布均匀
    with Image.open(out[0]) as im:
        assert im.size == (640, 360)
    assert sorted(tmp_path.joinpath("picks").iterdir()) == sorted(out)   # 落选帧不留在输出目录


@needs_ffmpeg
def test_pick_cover_frame_default_count_and_all_black(tmp_path):
    v = _synth_video(tmp_path / "g.mp4", [("good", 20)])
    out = cover.pick_cover_frame(v)
    assert len(out) == 12 and out[0].parent == tmp_path / "g_封面候选"
    black = _synth_video(tmp_path / "b.mp4", [("black", 4)])
    assert cover.pick_cover_frame(black, candidates=3) == []
