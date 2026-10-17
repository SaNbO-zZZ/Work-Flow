import re
from pathlib import Path

import numpy as np
import pytest

from hermes import config, subtitles as sub

# conftest 把 HERMES_ROOT 指到临时目录；字体用真实位置（只读）
REAL_FONTS = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/fonts")

PUNCT = re.compile(r"[，。！？、：；,.!?:;“”‘’\"'《》（）()…—·]")


def seg(show, start, end):
    return {"show": show, "text": show, "start": start, "end": end}


def test_zero_punct_and_spaces():
    cues = sub.build([seg("本周巴林大奖赛，导航得搜马来西亚！", 0.0, 4.0)])
    assert [c["text"] for c in cues] == ["本周巴林大奖赛 导航得搜马来西亚"]
    cues = sub.build([seg("本周巴林大奖赛。导航得搜马来西亚！", 0.0, 4.0)])
    assert [c["text"] for c in cues] == ["本周巴林大奖赛", "导航得搜马来西亚"]
    cues = sub.build([seg("高温、高湿，车里像蒸桑拿。", 0.0, 3.0)])
    assert [c["text"] for c in cues] == ["高温 高湿 车里像蒸桑拿"]
    cues = sub.build([seg("迈克尔·舒马赫留下了著名的“Multi 21”。", 0.0, 4.0)])
    assert cues[0]["text"] == "迈克尔 舒马赫留下了著名的Multi 21"
    cues = sub.build([seg("一圈5.543公里，", 0.0, 2.0)])
    assert cues[0]["text"] == "一圈5.543公里"       # 小数点保留
    for c in sub.build([seg("，好，", 0.0, 2.0), seg("（括号）测试：结束。", 2.0, 4.0)]):
        assert not PUNCT.search(c["text"].replace("5.543", ""))
        assert c["text"] == c["text"].strip() and "  " not in c["text"]


def test_length_and_break_points():
    text = "雪邦的F1最快单圈纪录，则由汉密尔顿保持：二〇一七年，他驾驶奔驰W08在排位赛跑出一分三十秒零七六。"
    cues = sub.build([seg(text, 0.0, 10.0)])
    assert all(sub.count(c["text"]) <= 16 for c in cues)
    # 在原标点处断行
    assert cues[0]["text"] == "雪邦的F1最快单圈纪录"
    long = "复杂多变的天气情况也为车队的策略组带来了更多的困难同时也给观众们带来了这条赛道更多的可能性。"
    cues = sub.build([seg(long, 0.0, 12.0)])
    assert all(sub.count(c["text"]) <= 16 for c in cues)
    assert "".join(c["text"] for c in cues) == long[:-1]
    # 字母数字不被切开
    cues = sub.build([seg("这一段专门用来测试很长很长的英文单词Supercalifragilistic不会被切开吧。", 0, 8)])
    assert any("Supercalifragilistic" in c["text"] for c in cues)


def test_timing_proportional_contiguous():
    cues = sub.build([seg("一二三四五六七八，一二三四五六七八九十一二三四五六。", 1.0, 4.0),
                      seg("下一句话在这里。", 4.5, 6.0)])
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] == pytest.approx(2.0, abs=0.001)          # 8/24 × 3 秒
    for a, b in zip(cues, cues[1:]):
        assert a["end"] == b["start"] and a["end"] > a["start"]
    assert cues[-1]["end"] == 6.0


def test_merge_short():
    cues = sub.build([seg("对。", 0.0, 0.3), seg("这句话足够长可以单独显示。", 0.3, 3.0)])
    assert len(cues) == 1 and cues[0]["text"] == "对 这句话足够长可以单独显示"
    assert cues[0]["start"] == 0.0 and cues[0]["end"] == 3.0
    # 合并后超过 max_chars+4 就不合并
    cues = sub.build([seg("对。", 0.0, 0.3), seg("这句话足够长可以单独显示还要再长一点点。", 0.3, 3.0)])
    assert len(cues) == 2


def test_srt_and_json(tmp_path):
    cues = sub.build([seg("本周巴林大奖赛，", 0.367, 2.0), seg("导航得搜马来西亚！", 3661.5, 3663.25)])
    p = sub.write_srt(cues, tmp_path / "a.srt")
    assert p.read_text() == ("1\n00:00:00,367 --> 01:01:01,500\n本周巴林大奖赛\n\n"
                             "2\n01:01:01,500 --> 01:01:03,250\n导航得搜马来西亚\n\n")
    j = sub.write_json(cues, tmp_path / "x" / "字幕.json")
    assert j.exists() and "导航" in j.read_text()


@pytest.mark.skipif(not (REAL_FONTS / "SourceHanSansSC-Heavy.otf").exists(), reason="字体盘未挂载")
def test_render(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FONTS_DIR", REAL_FONTS)
    img = sub.render("本周巴林大奖赛")
    assert img.size == (1920, 1080) and img.mode == "RGBA"
    a = np.asarray(img)
    assert a[:800, :, 3].max() == 0                      # 上方全透明
    ys = np.where(a[..., 3].max(1) > 0)[0]
    assert ys.min() > 850 and ys.max() < 1000
    white = (a[..., :3].min(2) > 245) & (a[..., 3] == 255)
    xs = np.where(white.any(0))[0]
    assert abs((xs.min() + xs.max()) / 2 - 960) < 10     # 水平居中
    small = sub.render("本周巴林大奖赛", (960, 540))
    assert small.size == (960, 540)
    out = sub.render_all([{"start": 0, "end": 1, "text": "甲"}, {"start": 1, "end": 2, "text": "乙"},
                          {"start": 2, "end": 3, "text": "甲"}], tmp_path / "png")
    assert out[0]["path"] == out[2]["path"] != out[1]["path"]
    assert len(list((tmp_path / "png").glob("*.png"))) == 2
