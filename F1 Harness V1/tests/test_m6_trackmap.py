"""M6-3：赛道简图（OSM 拼环线 / 渲染）、出处小字新格式、D4 叠层。全部不联网。"""
import json
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from pathlib import Path

from hermes import assets, config, edit, graphics, narration, overlays, qa_checks, trackmap

LAT0, LON0 = 3.0, 101.0
# conftest 把 HERMES_ROOT 指到临时目录；渲染要用真实字体（只读）
REAL_FONTS = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/fonts")
needs_fonts = pytest.mark.skipif(not (REAL_FONTS / "SourceHanSansSC-Heavy.otf").exists(), reason="字体盘未挂载")


@pytest.fixture(autouse=True)
def real_fonts(monkeypatch):
    monkeypatch.setattr(config, "FONTS_DIR", REAL_FONTS)


def _ll(x, y):
    return (LAT0 + y / 110_574, LON0 + x / (111_320 * math.cos(math.radians(LAT0))))


def _osm(*, oneway=True, cut=False, kart=True, pit=True):
    """1000×400 米的矩形主赛道（底边自西向东单行），底边外 30 米有维修区，远处一个更大的卡丁车环。"""
    pts = {1: (0, 0), 2: (500, 0), 3: (1000, 0), 4: (1000, 400), 5: (500, 400), 6: (0, 400), 7: (500, 200),
           21: (300, -30), 22: (700, -30),
           31: (3000, 0), 32: (4000, 0), 33: (4000, 1000), 34: (3000, 1000)}
    nodes = {k: _ll(*v) for k, v in pts.items()}
    ways = [{"id": 1, "nodes": [1, 2, 3], "tags": {"highway": "raceway", **({"oneway": "yes"} if oneway else {})}},
            {"id": 2, "nodes": [3, 4, 5, 6, 1], "tags": {"highway": "raceway"}}]
    if cut:
        ways.append({"id": 3, "nodes": [2, 7, 5], "tags": {"highway": "raceway"}})
    if pit:
        ways.append({"id": 4, "nodes": [21, 22], "tags": {"highway": "raceway", "name": "Pit Lane"}})
    if kart:
        ways.append({"id": 5, "nodes": [31, 32, 33, 34, 31], "tags": {"highway": "raceway", "sport": "karting"}})
    return ways, nodes


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "track_maps"
    monkeypatch.setattr(trackmap, "cache_dir", lambda: d)
    return d


# ---------- OSM 拼环线 ----------

def test_way_kind():
    assert trackmap._way_kind({"highway": "raceway"}) == "track"
    assert trackmap._way_kind({"name": "Pit Lane"}) == "pit"
    assert trackmap._way_kind({"name": "Boxengasse"}) == "pit"
    assert trackmap._way_kind({"raceway": "pit_lane"}) == "pit"
    assert trackmap._way_kind({"sport": "karting"}) is None
    assert trackmap._way_kind({"area": "yes"}) is None
    assert trackmap._way_kind({"surface": "gravel"}) is None


def test_build_loop_closes_skips_kart_and_starts_at_pit():
    loop = trackmap.build_loop(*_osm())
    pts = loop["xy"]
    assert loop["length_m"] == pytest.approx(2800, rel=0.01)
    assert loop["nodes"] == 6
    assert loop["direction_from"] == "oneway" and loop["start_from"] == "pit"
    lo = pts.min(axis=0)
    assert pts[0][1] - lo[1] == pytest.approx(0, abs=1)          # 起点在底边（挨着维修区）
    assert pts[0][0] - lo[0] == pytest.approx(500, abs=5)        # 维修区中段
    assert pts[1][0] > pts[0][0]                                 # 按单行方向向东
    assert trackmap._signed_area(pts) > 0                        # 逆时针


def test_build_loop_direction_from_table_and_straight_start():
    loop = trackmap.build_loop(*_osm(oneway=False, pit=False), clockwise=True)
    assert loop["direction_from"] == "table" and loop["start_from"] == "straight"
    assert trackmap._signed_area(loop["xy"]) < 0


def test_build_loop_picks_cycle_closest_to_official_length():
    ways, nodes = _osm(cut=True)
    assert trackmap.build_loop(ways, nodes)["length_m"] == pytest.approx(2800, rel=0.01)
    assert trackmap.build_loop(ways, nodes, expected_m=1800)["length_m"] == pytest.approx(1800, rel=0.01)


def test_build_loop_errors():
    ways, nodes = _osm()
    with pytest.raises(RuntimeError, match="没有 highway=raceway"):
        trackmap.build_loop([w for w in ways if w["id"] in (4, 5)], nodes)
    with pytest.raises(RuntimeError, match="拼不成闭合环线"):
        trackmap.build_loop([ways[0]], nodes)


def test_osm_outline_builds_and_caches(cache, monkeypatch):
    monkeypatch.setattr(trackmap, "_osm_ways", lambda lat, lon, r: _osm())
    out = trackmap.osm_outline("Test Ring", LAT0, LON0, expected_km=2.8)
    assert out["source"] == "OpenStreetMap" and out["credit"] == trackmap.OSM_CREDIT
    assert len(out["points"]) == trackmap.N_POINTS
    assert out["length_m"] == pytest.approx(2800, rel=0.01)
    assert out["start_from"] == "pit" and out["direction_from"] == "oneway" and out["clockwise"] is False
    p = np.array(out["points"])
    assert p.min() >= 0 and p.max() == pytest.approx(1)
    assert (cache / "osm_test-ring.json").exists() or list(cache.glob("osm_*.json"))

    def boom(*a):
        raise AssertionError("命中缓存不该再联网")
    monkeypatch.setattr(trackmap, "_osm_ways", boom)
    assert trackmap.osm_outline("Test Ring", LAT0, LON0)["length_m"] == out["length_m"]


def test_osm_ways_all_fail_message(monkeypatch):
    def fail(method, url, **kw):
        raise RuntimeError("HTTP 504 busy")
    monkeypatch.setattr(trackmap, "_fetch", fail)
    with pytest.raises(RuntimeError) as e:
        trackmap._osm_ways(LAT0, LON0, 3000)
    assert "OpenStreetMap 赛道数据下载失败" in str(e.value) and "api.openstreetmap.org" in str(e.value)


def test_osm_ways_falls_back_to_osm_api_xml(monkeypatch):
    xml = (b'<osm><node id="1" lat="3.0" lon="101.0"/><node id="2" lat="3.001" lon="101.0"/>'
           b'<way id="9"><nd ref="1"/><nd ref="2"/><tag k="highway" v="raceway"/></way>'
           b'<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="service"/></way></osm>')

    def fetch(method, url, **kw):
        if method == "POST":
            raise RuntimeError("HTTP 429")
        return xml
    monkeypatch.setattr(trackmap, "_fetch", fetch)
    ways, nodes = trackmap._osm_ways(LAT0, LON0, 3000)
    assert [w["id"] for w in ways] == [9] and set(nodes) == {1, 2}


def test_outline_for_event_falls_back_to_osm_and_caches(cache, monkeypatch):
    calls = []
    monkeypatch.setattr(trackmap, "fastf1_event", lambda loc, before: (_ for _ in ()).throw(RuntimeError("ff1 坏了")))
    monkeypatch.setattr(trackmap, "osm_outline",
                        lambda name, **kw: calls.append((name, kw)) or {"source": "OpenStreetMap", "length_m": 1.0})
    ev = {"location": "Kuala Lumpur", "event_date": "2026-10-04"}
    assert trackmap.outline_for_event(ev)["source"] == "OpenStreetMap"
    name, kw = calls[0]
    assert "Sepang" in name and kw["expected_km"] == pytest.approx(5.543)
    assert trackmap.outline_for_event(ev)["source"] == "OpenStreetMap" and len(calls) == 1   # 第二次走缓存


def test_outline_for_event_both_fail(cache, monkeypatch):
    monkeypatch.setattr(trackmap, "fastf1_event", lambda loc, before: None)

    def osm_fail(name, **kw):
        raise RuntimeError("OSM 挂了")
    monkeypatch.setattr(trackmap, "osm_outline", osm_fail)
    with pytest.raises(RuntimeError, match="Nowhere 的赛道简图取不到：OSM 挂了"):
        trackmap.outline_for_event({"location": "Nowhere", "event_date": "2026-10-04"})


# ---------- 渲染 ----------

def _outline():
    loop = trackmap.build_loop(*_osm())
    out = trackmap._finish(loop["xy"], rotation=0.0, corners=[{"n": "1", "x": 500.0, "y": 0.0, "angle": 0.0}])
    return out


@needs_fonts
def test_render_full_frame_panel_centered(tmp_path):
    png = trackmap.render(_outline(), tmp_path / "赛道图.png", title="测试赛道", subtitle="测试大奖赛", length_km=2.8)
    img = Image.open(png)
    assert img.size == (1920, 1080) and img.mode == "RGBA"
    a = np.array(img.getchannel("A"))
    x1, y1, x2, y2 = trackmap.PANEL_BOX
    assert abs(x1 - (1920 - x2)) <= 1 and abs(y1 - (1080 - y2)) <= 1 and min(x1, y1) >= 60
    assert x2 - x1 >= 728 * 1.5
    l, t, r, b = img.getchannel("A").getbbox()
    assert (l, t) == (x1, y1) and abs(r - x2) <= 1 and abs(b - y2) <= 1
    wx1, wy1, wx2, wy2 = graphics.WATERMARK_BOX
    assert a[: wy2 + 60, : wx2 + 60].max() == 0                         # 水印区
    credit = np.array(graphics.credit_tag("画面：YouTube · FORMULA 1《2025 日本大奖赛正赛集锦》").getchannel("A"))
    assert not ((a > 0) & (credit > 0)).any() and np.nonzero(credit.any(axis=1))[0].min() - y2 >= 60


@needs_fonts
def test_render_scales_with_size(tmp_path):
    img = Image.open(trackmap.render(_outline(), tmp_path / "s.png", title="测试", size=(960, 540)))
    assert img.size == (960, 540)
    a = np.array(img.getchannel("A"))
    assert a[:, : trackmap.PANEL_BOX[0] // 2 - 2].max() == 0 and a.max() > 0


@needs_fonts
def test_animate_frame_count(tmp_path, monkeypatch):
    seen = {}

    def fake_mov(frames, out, size, fps):
        seen["n"] = sum(1 for f in frames if len(f) == size[0] * size[1] * 4)
        seen["fps"] = fps
        return out
    monkeypatch.setattr(graphics, "_write_mov", fake_mov)
    trackmap.animate(_outline(), tmp_path / "a.mov", title="测试", size=(480, 270), fps=4)
    assert seen == {"n": round((0.35 + 1.8 + 1.0) * 4), "fps": 4}


# ---------- 出处小字 ----------

@pytest.mark.parametrize("url,site", [
    ("https://www.youtube.com/watch?v=x", "YouTube"), ("https://youtu.be/x", "YouTube"),
    ("https://www.bilibili.com/video/BV1", "B站"), ("https://b23.tv/abc", "B站"),
    ("https://www.formula1.com/en/video/x", "F1 官网"), ("https://www.motorsport.com/f1/", "motorsport.com"),
    ("https://news.bbc.co.uk/sport", "news.bbc.co.uk"), ("https://www.fia.com/news/x", "FIA"),
    ("https://x.com/F1/status/1", "X"), ("https://twitter.com/F1/status/1", "X"),
    ("https://www.instagram.com/reel/abc/", "Instagram"), ("https://m.weibo.cn/status/1", "微博"),
    ("https://www.douyin.com/video/1", "抖音"), ("https://media.mercedesamgf1.com/v/1", "media.mercedesamgf1.com"),
    ("", ""), (None, ""),
])
def test_site_name(url, site):
    assert assets.site_name(url) == site


def test_credit_line_formats():
    f = assets.credit_line
    assert f({"source_url": "https://youtu.be/x", "uploader": "FORMULA 1", "title": "2025 日本正赛"}) == \
        "画面：YouTube · FORMULA 1《2025 日本正赛》"
    assert f({"source_url": None, "credit": "FORMULA 1", "title": "测试素材"}) == "画面：FORMULA 1《测试素材》"
    assert f({"source_url": "https://b23.tv/x", "credit": "来源：B站 @赛车小王", "title": "集锦（1080P）"}) == \
        "画面：B站 · 赛车小王《集锦》"
    assert f({"credit": "画面：某台", "title": "排位 [高清] 【中字】"}) == "画面：某台《排位》"
    assert f({"uploader": "某UP", "title": "【全程】"}) == "画面：某UP《【全程】》"   # 全是括注就保留
    assert f({"source_url": "https://www.bilibili.com/video/BV1", "uploader": "U", "title": None}) == "画面：B站 · U"
    assert f({"source_url": "https://www.bilibili.com/video/BV1", "title": "T"}) == "画面：B站《T》"
    assert f({"credit": None, "title": None}) is None
    long = f({"uploader": "U", "title": "一二三四五六七八九十一二三四五六七八九十多出来的"})
    assert long == "画面：U《一二三四五六七八九十一二三四五六七八九十…》"


@needs_fonts
def test_credit_tag_keeps_closing_bracket(monkeypatch):
    drawn = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append(text)
        return orig(self, xy, text, *a, **kw)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    img = graphics.credit_tag("画面：B站 · 一个名字很长很长的频道《" + "超长的视频标题" * 6 + "》")
    assert drawn and drawn[-1].endswith("…》") and drawn[-1].startswith("画面：B站 · ")
    l, t, r, b = img.getchannel("A").getbbox()
    assert r - l <= graphics.CREDIT_MAX_W + 2 and r <= 1920


# ---------- D4 叠层 ----------

SCRIPT_HEADED = "## 开场\n今天聊聊这一站。\n\n## 赛道\n这条赛道很长。\n\n还有两条长直道。\n\n## 看点\n谁能赢？\n"
SCRIPT_PLAIN = ("今天聊聊这一站。\n\n先看布局，一圈五点五公里，十五个弯，两条长直道，重刹车很多。\n\n"
                "谁能赢？\n")


def test_track_paras_heading_and_keywords():
    paras, how = overlays.track_paras(SCRIPT_HEADED)
    units = narration.prepare(SCRIPT_HEADED).units
    shown = "".join(u["show"] for u in units if u["para"] in paras)
    assert "赛道很长" in shown and "长直道" in shown and "聊聊" not in shown and "谁能赢" not in shown
    assert how == "段名「赛道」"
    paras, how = overlays.track_paras(SCRIPT_PLAIN)
    assert list(paras) == [1] and how.startswith("关键词（第 2 段")
    assert overlays.track_paras("今天天气很好。\n\n明天也不错。\n") is None


def test_span_of_maps_by_characters_and_pads():
    units = [{"say": "aaaa", "para": 0}, {"say": "bbbbbb", "para": 1}, {"say": "cccccc", "para": 1},
             {"say": "dd", "para": 2}]
    align = {"duration": 20.0, "segments": [{"text": "aaaa", "start": 0, "end": 2},
                                            {"text": "bbbbbbcccccc", "start": 2.2, "end": 9.5},
                                            {"text": "dd", "start": 9.9, "end": 11}]}
    assert overlays.span_of(units, range(1, 2), align) == (2.2, 9.5)
    assert overlays.span_of(units, range(2, 3), align) == (9.9, 13.9)          # 不足 MIN_SPAN 补到 4 秒
    align["duration"] = 12.0
    assert overlays.span_of(units, range(2, 3), align) == (9.9, 12.0)
    with pytest.raises(ValueError):
        overlays.span_of(units, range(5, 6), align)



def test_panel_span_caps_at_panel_max():
    units = [{"say": "aaaa", "para": 0}, {"say": "bbbbbb", "para": 1}]
    align = {"duration": 30.0, "segments": [{"text": "aaaa", "start": 0, "end": 2},
                                            {"text": "bbbbbb", "start": 2.2, "end": 25.0}]}
    assert overlays.panel_span(units, range(1, 2), align) == (2.2, 2.2 + overlays.PANEL_MAX)
    assert overlays.panel_span(units, range(0, 1), align) == (0.0, 4.0)        # 段落短于 8 秒按段落

def _ep(tmp_path, branch="前瞻", script=SCRIPT_HEADED):
    folder = tmp_path / "ep"
    (folder / "02_文案").mkdir(parents=True)
    (folder / "03_配音").mkdir()
    if script is not None:
        (folder / "02_文案" / "逐字稿.md").write_text(script, encoding="utf-8")
        segs, t = [], 0.0
        for u in narration.prepare(script).units:
            segs.append({"text": u["say"], "show": u["show"], "start": t, "end": t + 3})
            t += 3.5
        (folder / "03_配音" / "对齐.json").write_text(json.dumps({"duration": t, "segments": segs}), encoding="utf-8")
    return {"id": 1, "folder": str(folder), "branch": branch, "gp_label": "马来西亚大奖赛", "title": "雪邦前瞻"}


@pytest.fixture
def fake_event(monkeypatch):
    ev = {"location": "Kuala Lumpur", "name_zh": "巴林大奖赛", "event_date": "2026-10-04"}
    monkeypatch.setattr(overlays.calendar, "find_event", lambda conn, text: ev if "雪邦" in text else None)
    monkeypatch.setattr(trackmap, "outline_for_event", lambda e: dict(_outline(), source="OpenStreetMap"))
    return ev


@needs_fonts
def test_build_writes_track_png_and_layers(conn, tmp_path, fake_event):
    ep = _ep(tmp_path)
    res = overlays.build(conn, ep, ["G2：赛道图挪一下"])
    folder = tmp_path / "ep" / "05_图形"
    layer = json.loads((folder / "叠层.json").read_text(encoding="utf-8"))["overlays"]
    assert res["overlays"] == 1 and not res["warnings"] and res["feedback"] == ["G2：赛道图挪一下"]
    assert layer[0]["path"] == "05_图形/赛道图.png" and layer[0]["kind"] == "赛道图"
    assert (tmp_path / "ep" / layer[0]["path"]).exists()
    assert res["section"]["how"] == "段名「赛道」" and res["section"]["start"] == 3.5
    assert layer[0]["end"] - layer[0]["start"] >= overlays.MIN_SPAN
    assert res["track"]["source"] == "OpenStreetMap" and res["track"]["length_m"] == pytest.approx(2800, rel=0.01)


@needs_fonts
def test_build_no_section_warns(conn, tmp_path, fake_event):
    res = overlays.build(conn, _ep(tmp_path, script="今天天气很好。\n\n明天也不错。\n"))
    assert res["overlays"] == 0 and "没找到介绍赛道的段落" in res["warnings"][0]
    assert (tmp_path / "ep" / "05_图形" / "赛道图.png").exists()


def test_build_other_branch_writes_empty(conn, tmp_path):
    res = overlays.build(conn, _ep(tmp_path, branch="复盘", script=None))
    assert res["note"] == "复盘分支不加赛道简图"
    assert json.loads((tmp_path / "ep" / "05_图形" / "叠层.json").read_text(encoding="utf-8")) == {"overlays": []}


def test_build_errors(conn, tmp_path, monkeypatch, fake_event):
    with pytest.raises(RuntimeError, match="先完成文案和配音"):
        overlays.build(conn, _ep(tmp_path, script=None))
    monkeypatch.setattr(overlays.calendar, "find_event", lambda conn, text: None)
    with pytest.raises(RuntimeError, match="赛历里找不到"):
        overlays.build(conn, _ep(tmp_path / "b"))


def test_qa_trackmap_item():
    ok = qa_checks._trackmap_item("前瞻", {"overlays": [{"kind": "赛道图"}]})
    bad = qa_checks._trackmap_item("前瞻", {"overlays": []})
    other = qa_checks._trackmap_item("科普", None)
    assert ok["ok"] and not bad["ok"] and "D4" in bad["detail"]
    assert other["ok"] and other["detail"] == "科普分支不查"
    assert bad["name"] == "前瞻缺赛道简图"


def test_edit_overlays_reads_layer_file(tmp_path):
    g = tmp_path / "05_图形"
    g.mkdir()
    Image.new("RGBA", (8, 8)).save(g / "赛道图.png")
    (g / "叠层.json").write_text(json.dumps({"overlays": [
        {"path": "05_图形/赛道图.png", "start": -1, "end": 99, "kind": "赛道图"},
        {"path": "05_图形/没有.png", "start": 1, "end": 2}]}), encoding="utf-8")
    warns = []
    assert edit._overlays(tmp_path, 30.0, warns) == [{"path": "05_图形/赛道图.png", "start": 0.0, "end": 30.0,
                                                      "kind": "赛道图"}]
    assert warns == ["叠层图不存在：05_图形/没有.png"]
    (g / "叠层.json").write_text("{坏", encoding="utf-8")
    assert edit._overlays(tmp_path, 30.0, warns) == [] and "读不了" in warns[-1]
    assert edit._overlays(tmp_path / "无", 30.0, []) == []
