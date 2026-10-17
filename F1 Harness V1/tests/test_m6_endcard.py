"""片尾卡：YouTube 最后 20 秒不用、打标 end_card 顺手标不可用、合成只播到 src_out、时间线镜头总览图。"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from hermes import assets, compose, config, db

REAL_FONTS = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/fonts")


def test_usable_end():
    yt = {"source_url": "https://www.youtube.com/watch?v=abc", "duration": 300.0}
    assert assets.usable_end(yt) == 280.0
    assert assets.usable_end(yt | {"source_url": "https://youtu.be/abc"}) == 280.0
    assert assets.usable_end(yt | {"duration": 50.0}) == 50.0              # 太短不扣
    assert assets.usable_end({"source_url": "https://x.com/a/status/1", "duration": 300}) == 300.0
    assert assets.usable_end({"source_url": None, "duration": 90}) == 90.0
    assert assets.usable_end({"source_url": "https://youtu.be/abc"}) is None   # 没有时长键
    assert assets.usable_end({"duration": None}) is None


def test_usable_end_row(conn):
    aid = conn.execute("INSERT INTO assets(source_type, source_url, duration, state, created_at) "
                       "VALUES ('测试', 'https://www.youtube.com/watch?v=x', 120, 'ready', ?)", (db.now(),)).lastrowid
    row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    assert assets.usable_end(row) == 100.0


def test_schema_requires_end_card():
    item = assets.TAG_SCHEMA["properties"]["shots"]["items"]
    assert "end_card" in item["required"] and item["properties"]["end_card"] == {"type": "boolean"}
    assert "- end_card：" in assets.TAG_PROMPT and "SUBSCRIBE" in assets.TAG_PROMPT


def _tag(**kw):
    return {k: [] for k in assets.TAG_KEYS} | {"session": "正赛", "main": ["比赛中"], "shot_type": "全景",
                                              "circuit": None, "has_subtitles": False, "has_logo": False,
                                              "logo_note": ""} | kw


def _asset_with_shots(conn, n=3):
    aid = conn.execute("INSERT INTO assets(source_type, state, created_at) VALUES ('测试', 'ready', ?)",
                       (db.now(),)).lastrowid
    for i in range(n):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s) VALUES (?,?,?,?)", (aid, i, i * 5, i * 5 + 5))
    # #3 是 #2 切细出来的段
    conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, parent_idx) VALUES (?,?,?,?,2)",
                 (aid, n, 12, 15))
    return aid


def _unusable(conn, aid):
    return {r["idx"]: r["unusable"] for r in conn.execute("SELECT idx, unusable FROM asset_shots WHERE asset_id=?",
                                                          (aid,))}


def test_save_tags_end_card_marks_unusable(conn):
    aid = _asset_with_shots(conn)
    assets.set_unusable(conn, aid, 0, True)                 # 用户手标的
    res = assets.save_tags(conn, aid, {0: _tag(end_card=False), 1: _tag(), 2: _tag(end_card=True)})
    assert _unusable(conn, aid) == {0: 1, 1: 0, 2: 1, 3: 1}  # 手标保留；片尾卡连同切细段标上
    assert [s["end_card"] for s in res["shots"] if "end_card" in s] == [False, True]
    assert "end_card" not in json.loads(conn.execute("SELECT tags FROM asset_shots WHERE asset_id=? AND idx=2",
                                                     (aid,)).fetchone()["tags"])
    assert [s["idx"] for s in assets.clean_shots(conn, aid)] == [1]


def test_tag_asset_passes_end_card(conn, tmp_path):
    aid = _asset_with_shots(conn, 2)
    conn.execute("UPDATE assets SET duration=10, thumbs=? WHERE id=?",
                 (json.dumps({"sheets": [str(tmp_path / "sheet_1.jpg")]}), aid))
    base = {"teams": [], "drivers": [], "circuit": None, "session": "正赛", "main": "比赛中", "sub": [],
            "shot_type": "全景", "has_subtitles": False, "has_logo": False, "logo_note": ""}
    reply = json.dumps({"shots": [base | {"idx": 0, "end_card": False}, base | {"idx": 1, "end_card": True}]})
    res = asyncio.run(assets.tag_asset(conn, aid, ask=lambda prompt, images: reply))
    assert [s["end_card"] for s in res["shots"]] == [False, True]
    assert _unusable(conn, aid) == {0: 0, 1: 1, 2: 0}


def test_cut_stops_at_src_out(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compose, "probe", lambda p: {"duration": 600.0})
    monkeypatch.setattr(compose, "_run", lambda args: calls.append([str(a) for a in args]))
    clip = {"src_in": 100.0, "src_out": 102.5, "crop": None}
    compose._cut(tmp_path / "s.mp4", clip, 150, (960, 540), 30, tmp_path / "o.mp4", [])
    args = calls[0]
    assert args[args.index("-ss") + 1] == "100.000" and args[args.index("-t") + 1] == "2.500"
    assert args.index("-t") < args.index("-i")              # 输入选项：读到 src_out 就停
    calls.clear()
    compose._cut(tmp_path / "s.mp4", {"src_in": 5.0, "src_out": 5.0}, 1, (960, 540), 30, tmp_path / "o.mp4", [])
    assert calls[0][calls[0].index("-t") + 1] == "0.033"     # 至少一帧


def test_cut_real_does_not_play_past_src_out(tmp_path):
    ff = compose.FFMPEG
    if not Path(ff).exists() and not shutil.which("ffmpeg"):
        pytest.skip("没有 ffmpeg")
    src = tmp_path / "src.mp4"
    # 前 2 秒红、后 2 秒绿；只要 0.5–1.5 秒，槽位给 2 秒，后面应定格红色而不是播到绿色
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=160x90:rate=25:duration=2",
                    "-f", "lavfi", "-i", "color=lime:size=160x90:rate=25:duration=2", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
                   check=True, capture_output=True)
    out = tmp_path / "seg.mp4"
    compose._cut(src, {"src_in": 0.5, "src_out": 1.5}, 50, (160, 90), 25, out, compose.PREVIEW_X264)
    assert compose.probe(out)["duration"] == pytest.approx(2.0, abs=0.05)
    last = tmp_path / "last.png"
    subprocess.run([ff, "-v", "error", "-y", "-sseof", "-0.1", "-i", str(out), "-frames:v", "1", str(last)],
                   check=True, capture_output=True)
    r, g, _ = Image.open(last).convert("RGB").getpixel((80, 45))
    assert r > 150 and g < 100


def test_contact_sheet(conn, tmp_path, monkeypatch):
    ff = compose.FFMPEG
    if not Path(ff).exists() and not shutil.which("ffmpeg"):
        pytest.skip("没有 ffmpeg")
    if (REAL_FONTS / "SourceHanSansSC-Medium.otf").exists():
        monkeypatch.setattr(config, "FONTS_DIR", REAL_FONTS)
    src = tmp_path / "source.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    aid = conn.execute("INSERT INTO assets(source_type, local_path, state, created_at) VALUES ('测试', ?, 'ready', ?)",
                       (str(src), db.now())).lastrowid
    video = [{"asset_id": aid, "shot_idx": i, "src_in": 0.2 * i, "src_out": 0.2 * i + 0.5, "start": 1.5 * i,
              "end": 1.5 * i + 1.5, "beat": i % 2, "crop": {"x": 0, "y": 0, "w": 160, "h": 180} if i == 1 else None}
             for i in range(7)]
    video.append({"asset_id": 999, "shot_idx": 0, "src_in": 0, "src_out": 1, "start": 20, "end": 21, "segment": 3})
    timeline = {"video": video, "beats": [{"i": 0, "teams": ["法拉利", "迈凯伦"]}, {"i": 1, "teams": []}]}
    out = compose.contact_sheet(tmp_path, timeline, "总览.jpg", conn=conn)
    assert out == tmp_path / "总览.jpg" and out.exists()
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (6 * 320, 2 * (180 + compose.SHEET_LABEL_H))
        assert im.convert("L").crop((0, 0, 320, 180)).getextrema()[1] > 100     # 第一格有画面
