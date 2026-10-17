import json
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from hermes import voice_post as vp
from hermes.assets import FFMPEG

SR_IN = 24000

UNITS = [
    {"say": "本周巴林大奖赛，", "show": "本周巴林大奖赛，", "para": 1},
    {"say": "导航得搜马来西亚！", "show": "导航得搜马来西亚！", "para": 1},
    {"say": "一圈五点五四三公里，", "show": "一圈5.543公里，", "para": 2},
    {"say": "两条长直道加上高速连续弯和低速回头弯。", "show": "两条长直道加上高速连续弯和低速回头弯。", "para": 2},
]
# 文件名故意乱序；第 4 个单元被剪映在句中切成两段
ASR = {
    "q7.wav": "本周巴林大奖赛,导航得搜马来西亚!",
    "b2.wav": "一圈5.543公里。",
    "z1.wav": "两条长直道加上",
    "a9.wav": "高速连续弯和低速回头弯。",
}
# (类型, 秒)：t=正弦音，s=静音；原速
SHAPES = {
    "q7.wav": [("s", 0.2), ("t", 0.7), ("s", 0.5), ("t", 0.8), ("s", 0.4)],
    "b2.wav": [("s", 0.1), ("t", 0.5), ("s", 0.25), ("t", 0.5), ("s", 0.05)],
    "z1.wav": [("s", 0.05), ("t", 0.6), ("s", 0.2)],
    "a9.wav": [("s", 0.2), ("t", 2.5), ("s", 0.3)],
}


def _write(path: Path, shape, mp3=False):
    parts = []
    for kind, d in shape:
        n = int(round(d * SR_IN))
        x = np.sin(2 * np.pi * 440 * np.arange(n) / SR_IN) * 0.5 if kind == "t" else np.zeros(n)
        parts.append(x)
    pcm = (np.concatenate(parts) * 32767).astype("<i2")
    tmp = path.with_suffix(".tmp.wav") if mp3 else path
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR_IN)
        w.writeframes(pcm.tobytes())
    if mp3:  # 剪映导出的 .wav 实际是 mp3
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(tmp), "-f", "mp3", str(path)], check=True)
        tmp.unlink()


def fake_transcribe(path, prompt=""):
    return ASR[Path(path).name]


@pytest.fixture
def voice_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for name, shape in SHAPES.items():
        _write(raw / name, shape, mp3=(name == "a9.wav"))
    (raw / "._q7.wav").write_bytes(b"junk")
    (tmp_path / "配音稿.json").write_text(json.dumps({"units": UNITS, "warnings": []}, ensure_ascii=False))
    return tmp_path


def test_spoken_numbers():
    assert vp.spoken("2017年") == "二零一七年"
    assert vp.spoken("5.543公里") == "五点五四三公里"
    assert vp.spoken("1分30秒076") == "一分三十秒零七六"
    assert vp.spoken("56圈") == "五十六圈"
    assert vp.spoken("10位") == "十位"
    assert vp.spoken("1103字") == "一千一百零三字"
    assert vp.spoken("W08") == "W零八"
    assert vp.spoken("20005") == "二万零五"
    assert vp.norm("一圈5.543公里。") == vp.norm("一圈五点五四三公里，")
    assert vp.count_chars("F1重返雪邦。") == 6


def test_order_sorts_and_groups(voice_dir):
    files = sorted((voice_dir / "raw").glob("[!.]*.wav"))
    clips = vp.order(files, UNITS, transcribe=fake_transcribe)
    assert [c["file"] for c in clips] == ["q7.wav", "b2.wav", "z1.wav+a9.wav"]
    assert [(c["u0"], c["u1"]) for c in clips] == [(0, 2), (2, 3), (3, 4)]
    assert clips[1]["match"] == 1.0
    assert clips[1]["show"] == "一圈5.543公里，"
    assert all(c["match"] >= vp.MATCH_MIN for c in clips)


def test_order_keeps_misread_clip():
    asr = dict(ASR, **{"b2.wav": "一圈乱七八糟的东西公里"})
    clips = vp.order([Path(n) for n in asr], UNITS, transcribe=lambda p, prompt="": asr[Path(p).name])
    assert [c["file"] for c in clips] == ["q7.wav", "b2.wav", "z1.wav+a9.wav"]
    assert clips[1]["match"] < vp.MATCH_MIN



def test_order_english_name_heard_short():
    """2026-10-16 第 21 期：剪映切出的“斯特拉向The Race 网站表示，”被识别成“德瑞斯”，原先排到了前面的“斯特拉表示”处。"""
    units = [{"say": s, "show": s, "para": 1} for s in (
        "据Motorsport 网报道，", "斯特拉表示，新加坡采集的数据已经排除了巴库升级把赛车变慢的疑虑。",
        "更关键的是，升级能改善多少，又能解决什么。斯特拉向The Race 网站表示，", "要改善赛车那些顽固的特性，")]
    asr = {"a.wav": "据MotoSport网报道", "b.wav": "斯特拉表示,新加坡采集的数据已经排除了巴枯升级把赛车变慢的疑虑",
           "c.wav": "更关键的是 升级能改善多少又能解决什么", "d.wav": "斯特拉向德瑞斯网站表示：",
           "e.wav": "要改善赛车那些顽固的特性。"}
    clips = vp.order([Path(n) for n in asr], units, transcribe=lambda p, prompt="": asr[Path(p).name])
    assert [c["file"] for c in clips] == ["a.wav", "b.wav", "c.wav+d.wav", "e.wav"]

def test_assemble_caps_pauses(tmp_path):
    src = tmp_path / "x.wav"
    _write(src, [("s", 0.3), ("t", 1.3), ("s", 1.3), ("t", 1.3), ("s", 0.6)])
    out = tmp_path / "out.wav"
    t = vp.assemble([{"file": str(src)}], out, speed=1.3, gap=0.3)
    with wave.open(str(out)) as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
        dur = w.getnframes() / 48000
    # 1.0 + 0.3 + 1.0，头尾静音去掉；atempo 窗口会让音头音尾各拖几十毫秒；最后补 END_PAD 秒静音
    assert dur == pytest.approx(2.3 + vp.END_PAD, abs=0.08)
    assert t[0]["start"] == 0 and t[0]["end"] == pytest.approx(dur - vp.END_PAD, abs=0.001)
    assert len(t[0]["pauses"]) == 1 and t[0]["pauses"][0]["dur"] == pytest.approx(0.3, abs=0.01)


def test_assemble_gaps_between_clips(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _write(a, [("t", 1.0), ("s", 0.05)])
    _write(b, [("s", 0.05), ("t", 1.0), ("s", 1.0)])
    t = vp.assemble([{"file": str(a)}, {"file": str(b)}], tmp_path / "o.wav", speed=1.0, gap=0.3)
    assert t[1]["start"] - t[0]["end"] == pytest.approx(0.1, abs=0.02)   # 原有停顿短于上限就保留
    t = vp.assemble([{"file": str(b)}, {"file": str(a)}], tmp_path / "o.wav", speed=1.0, gap=0.3)
    assert t[1]["start"] - t[0]["end"] == pytest.approx(0.3, abs=0.01)   # 长停顿压到上限


def test_process_outputs(voice_dir):
    res = vp.process(voice_dir, speed=1.0, gap=0.3, transcribe=fake_transcribe)
    for name in ("配音.wav", "对齐.json", "试听.mp3"):
        assert (voice_dir / name).stat().st_size > 0
    saved = json.loads((voice_dir / "对齐.json").read_text())
    assert saved == res
    assert set(res) == {"speed", "gap", "duration", "segments", "issues"}
    segs = res["segments"]
    assert [s["i"] for s in segs] == [0, 1, 2]
    assert set(segs[0]) == {"i", "file", "text", "show", "start", "end", "match", "asr"}
    assert segs[0]["text"] == "本周巴林大奖赛，导航得搜马来西亚！"
    # 0.7+0.3+0.8 | 0.3 | 0.5+0.25+0.5 | 0.1 | 0.6+0.1(剪映切开处压到 SPLICE)+2.5
    exp = [(0.0, 1.8), (2.1, 3.35), (3.45, 6.65)]
    for s, (a, b) in zip(segs, exp):
        assert s["start"] == pytest.approx(a, abs=0.03) and s["end"] == pytest.approx(b, abs=0.03)
    assert res["duration"] == pytest.approx(6.65 + vp.END_PAD, abs=0.03)
    kinds = [(i["kind"], i["segment"]) for i in res["issues"]]
    # 段 0 的 0.3 秒停顿在逗号处，不报；段 1 句中 0.25 秒要报；段 2 切开处已压到 0.1 秒，不报
    assert ("停顿", 0) not in kinds
    pauses = [i for i in res["issues"] if i["kind"] == "停顿"]
    assert [p["segment"] for p in pauses] == [1]
    assert pauses[0]["at"] == pytest.approx(2.6, abs=0.03)
    # 第 1 段 15 字 1.8 秒太快；第 2 段 28 字约 4.75 秒正常
    rate = [i for i in res["issues"] if i["kind"] == "语速"]
    assert len(rate) == 1 and rate[0]["segment"] == 0 and "第 1 段" in rate[0]["detail"]
    assert not [i for i in res["issues"] if i["kind"] == "读错"]


def test_process_reports_misread(voice_dir):
    asr = dict(ASR, **{"b2.wav": "一圈乱七八糟的东西公里"})
    res = vp.process(voice_dir, speed=1.3, transcribe=lambda p, prompt="": asr[Path(p).name])
    bad = [i for i in res["issues"] if i["kind"] == "读错"]
    assert len(bad) == 1 and bad[0]["segment"] == 1
    assert "应为「一圈五点五四三公里，」听成「一圈乱七八糟的东西公里」" in bad[0]["detail"]
    assert res["speed"] == 1.3 and res["duration"] < 6.85 + vp.END_PAD


def test_rate_low():
    units = [{"say": "慢慢说", "para": 3}]
    clips = [{"u0": 0, "u1": 1}]
    issues = vp._rate_issues(units, clips, [{"start": 0.0, "end": 3.0}])
    assert issues and "60 字/分" in issues[0]["detail"]
