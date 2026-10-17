import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from hermes import assets, config, db, glossary

SEG = 2.0


def _make_video(out: Path, sources: list[str], *, audio: bool) -> Path:
    args = ["ffmpeg", "-v", "error", "-y"]
    for src in sources:
        sep = ":" if "=" in src else "="
        args += ["-f", "lavfi", "-i", f"{src}{sep}s=320x180:r=25:d={SEG}"]
    n = len(sources)
    graph = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={SEG * n}"]
    args += ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        args += ["-map", f"{n}:a", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(args, check=True)
    return out


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("m4-videos")
    # 第一段全黑，用来验证缩略图跳过黑镜头
    three = _make_video(d / "three.mp4", ["color=c=black", "testsrc2", "smptebars"], audio=True)
    single = _make_video(d / "single.mp4", ["color=c=red"], audio=False)
    return {"three": three, "single": single}


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "素材库"
    monkeypatch.setattr(config, "LIBRARY_DIR", lib)
    return lib


def _copy(src: Path, tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copyfile(src, dest)
    return dest


def test_probe_and_sha(videos):
    info = assets.probe(videos["three"])
    assert info["duration"] == pytest.approx(6.0, abs=0.1)
    assert (info["width"], info["height"]) == (320, 180)
    assert info["fps"] == pytest.approx(25)
    assert info["vcodec"] == "h264" and info["has_audio"] is True
    assert assets.probe(videos["single"])["has_audio"] is False
    digest = assets.sha256_file(videos["three"])
    assert len(digest) == 64 and digest == assets.sha256_file(videos["three"])
    assert digest != assets.sha256_file(videos["single"])


def test_detect_shots(videos):
    shots = assets.detect_shots(videos["three"], 6.0)
    assert len(shots) == 3
    for (a, b), (ea, eb) in zip(shots, [(0, 2), (2, 4), (4, 6)]):
        assert abs(a - ea) <= 0.2 and abs(b - eb) <= 0.2
    assert assets.detect_shots(videos["single"], 2.0) == [(0.0, 2.0)]


def test_segments_merge_short():
    # 0.3 秒的闪切并到前一个；开头的短镜头并到后一个；结尾不足 0.5 秒并到前一个
    assert assets._segments([2.0, 2.3, 5.0], 8.0) == [(0.0, 2.3), (2.3, 5.0), (5.0, 8.0)]
    assert assets._segments([0.2, 3.0], 6.0) == [(0.0, 3.0), (3.0, 6.0)]
    assert assets._segments([3.0, 5.8], 6.0) == [(0.0, 3.0), (3.0, 6.0)]
    assert assets._segments([], 4.0) == [(0.0, 4.0)]


def test_ingest_files_and_rows(conn, videos, library, tmp_path):
    src = _copy(videos["three"], tmp_path, "in.MP4")
    aid = assets.ingest(conn, src, source_type="用户链接", source_url="https://example.com/v",
                        title="测试", uploader="某UP", credit="画面：某UP")
    assert not src.exists()
    folder = library / str(aid)
    assert (folder / "source.mp4").exists()
    assert sorted(p.name for p in (folder / "shots").iterdir()) == ["000.jpg", "001.jpg", "002.jpg"]
    assert [p.name for p in (folder / "sheets").iterdir()] == ["sheet_1.jpg"]
    with Image.open(folder / "shots" / "000.jpg") as im:
        assert im.width == 640
    with Image.open(folder / "thumb.jpg") as im:
        assert im.width == 480
        # 第 0 个镜头是全黑，缩略图应取第 1 个
        assert sum(im.convert("L").getdata()) / (im.width * im.height) > assets.BLACK_LUMA

    row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    assert row["state"] == "ready" and row["local_path"] == str(folder / "source.mp4")
    assert row["duration"] == pytest.approx(6.0, abs=0.1) and (row["width"], row["height"]) == (320, 180)
    thumbs = json.loads(row["thumbs"])
    assert thumbs == {"thumb": str(folder / "thumb.jpg"), "sheets": [str(folder / "sheets" / "sheet_1.jpg")]}
    shots = conn.execute("SELECT * FROM asset_shots WHERE asset_id=? ORDER BY idx", (aid,)).fetchall()
    assert [s["idx"] for s in shots] == [0, 1, 2]
    assert shots[1]["keyframe"] == str(folder / "shots" / "001.jpg")
    assert assets.credit_of(conn, aid) == "画面：某UP"

    dup = _copy(videos["three"], tmp_path, "dup.mp4")
    assert assets.ingest(conn, dup, source_type="脚本") == aid
    assert not dup.exists()
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1

    keep = _copy(videos["single"], tmp_path, "keep.mp4")
    aid2 = assets.ingest(conn, keep, source_type="脚本", uploader="频道B", move=False)
    assert aid2 != aid and keep.exists() and (library / str(aid2) / "source.mp4").exists()
    assert conn.execute("SELECT COUNT(*) FROM asset_shots WHERE asset_id=?", (aid2,)).fetchone()[0] == 1
    assert assets.credit_of(conn, aid2) == "频道B"


def test_ingest_failure_marks_failed(conn, library, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_text("不是视频")
    with pytest.raises(RuntimeError):
        assets.ingest(conn, bad, source_type="脚本")
    assert conn.execute("SELECT state FROM assets").fetchone()["state"] == "failed"


FAKE_REPLY = """```json
{"shots": [
  {"idx": 0, "teams": [], "drivers": [], "circuit": null, "session": "其他", "main": "数据图表", "sub": [],
   "shot_type": "全景",
   "has_subtitles": false, "has_logo": false, "logo_note": "不该留下"},
  {"idx": 1, "teams": ["Red Bull Racing"], "drivers": ["Max Verstappen", "Verstappen"], "circuit": "Suzuka",
   "session": "排位", "main": "比赛中", "sub": ["缠斗", "乱写", "缠斗"], "shot_type": "车载",
   "has_subtitles": false, "has_logo": true,
   "logo_note": " 右上角白色“某某体育”文字 "},
  {"idx": 2, "teams": ["红牛"], "drivers": [], "circuit": null, "session": "胡编", "main": "胡编", "sub": [],
   "shot_type": "特写",
   "has_subtitles": true, "has_logo": false, "logo_note": ""},
  {"idx": 99, "teams": ["法拉利"], "drivers": [], "circuit": null, "session": "正赛", "main": "航拍风景",
   "sub": [], "shot_type": "航拍",
   "has_subtitles": true, "has_logo": true, "logo_note": "越界"}
 ]}
```"""


def test_tag_asset_list_and_card(conn, videos, library, tmp_path):
    glossary.add_manual(conn, "车手", "Max Verstappen", "维斯塔潘")
    glossary.add_manual(conn, "车队", "Red Bull Racing", "红牛")
    glossary.add_manual(conn, "赛道", "Suzuka", "铃鹿")
    aid = assets.ingest(conn, _copy(videos["three"], tmp_path, "a.mp4"), source_type="脚本", title="日本站排位")
    other = assets.ingest(conn, _copy(videos["single"], tmp_path, "b.mp4"), source_type="脚本")
    seen = {}

    async def ask(prompt, images):
        seen.update(prompt=prompt, images=images)
        return FAKE_REPLY

    out = asyncio.run(assets.tag_asset(conn, aid, ask=ask))
    assert "Max Verstappen=维斯塔潘" in seen["prompt"] and "日本站排位" in seen["prompt"]
    assert seen["images"] == [library / str(aid) / "sheets" / "sheet_1.jpg"]
    assert all(p.exists() for p in seen["images"])
    assert [s["idx"] for s in out["shots"]] == [0, 1, 2]
    assert out["shots"][1] == {"idx": 1, "teams": ["红牛"], "drivers": ["维斯塔潘"], "circuit": "铃鹿",
                               "session": "排位", "main": ["比赛中"], "sub": ["缠斗"], "shot_type": "车载",
                               "has_subtitles": False, "has_logo": True,
                               "logo_note": "右上角白色“某某体育”文字", "end_card": False}
    assert out["shots"][2]["session"] == "其他" and out["shots"][2]["main"] == []
    assert out["batches"] == 1 and out["has_subtitles"] is True and out["has_logo"] is True
    assert out["logo_note"] == "镜头 #1：右上角白色“某某体育”文字"
    assert out["tags"] == ["其他", "排位", "数据图表", "比赛中", "缠斗", "全景", "车载", "特写", "红牛", "维斯塔潘",
                           "铃鹿"]

    row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    assert json.loads(row["tags"]) == out["tags"]
    assert (row["has_subtitles"], row["has_logo"]) == (1, 1) and row["logo_note"] == out["logo_note"]
    assert row["tagged_at"]
    rows = conn.execute("SELECT tags, has_subtitles, has_logo, logo_note FROM asset_shots WHERE asset_id=? "
                        "ORDER BY idx", (aid,)).fetchall()
    assert json.loads(rows[1]["tags"]) == {"teams": ["红牛"], "drivers": ["维斯塔潘"], "circuit": "铃鹿",
                                           "session": "排位", "main": ["比赛中"], "sub": ["缠斗"],
                                           "shot_type": "车载"}
    assert [(r["has_subtitles"], r["has_logo"], r["logo_note"]) for r in rows] == [
        (0, 0, None), (0, 1, "右上角白色“某某体育”文字"), (1, 0, None)]

    assert [a["id"] for a in assets.list_assets(conn, tag="维斯塔潘")] == [aid]
    assert [a["id"] for a in assets.list_assets(conn, tag="排位")] == [aid]
    assert assets.list_assets(conn, tag="法拉利") == []
    assert [a["id"] for a in assets.list_assets(conn)] == [other, aid]
    assert assets.list_assets(conn, state="failed") == []

    ep = conn.execute("INSERT INTO episodes(code, branch, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                      ("20261001-前瞻-日本大奖赛-01", "前瞻", "日本站前瞻", db.now(), db.now())).lastrowid
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (ep, aid, 2.0, 3.5, db.now()))
    c = assets.card(conn, aid)
    assert c["id"] == aid and c["tags"] == out["tags"] and c["thumbs"]["sheets"]
    assert [s["idx"] for s in c["shots"]] == [0, 1, 2]
    assert c["shots"][1]["tags"]["circuit"] == "铃鹿" and c["shots"][1]["keyframe"].endswith("001.jpg")
    assert [(s["has_subtitles"], s["has_logo"], s["logo_note"]) for s in c["shots"]] == [
        (False, False, None), (False, True, "右上角白色“某某体育”文字"), (True, False, None)]
    assert [s["idx"] for s in assets.clean_shots(conn, aid)] == [0, 2]
    assert [s["idx"] for s in assets.clean_shots(conn, aid, allow_subtitles=False)] == [0]
    assert c["uses"][0]["episode_code"] == "20261001-前瞻-日本大奖赛-01"
    assert c["uses"][0]["src_in"] == 2.0
    with pytest.raises(ValueError):
        assets.card(conn, 9999)


def _fake_asset(conn, n_shots: int, *, sheets: int, **cols) -> int:
    """不跑 ffmpeg，直接造 ready 素材和 n 个镜头行（拼图路径是假的，假 ask 不读）。"""
    thumbs = {"thumb": "/x/thumb.jpg", "sheets": [f"/x/sheet_{i}.jpg" for i in range(1, sheets + 1)]}
    names = ", ".join(["source_type", "title", "sha256", "state", "duration", "thumbs", "created_at", *cols])
    aid = conn.execute(f"INSERT INTO assets({names}) VALUES ({', '.join('?' * (7 + len(cols)))})",
                       ("脚本", "假素材", f"sha-{n_shots}-{sheets}-{len(cols)}", "ready", n_shots * 2.0,
                        json.dumps(thumbs), db.now(), *cols.values())).lastrowid
    conn.executemany("INSERT INTO asset_shots(asset_id, idx, start_s, end_s) VALUES (?,?,?,?)",
                     [(aid, i, i * 2.0, i * 2.0 + 2) for i in range(n_shots)])
    return aid


def test_make_sheets_covers_all_shots(tmp_path):
    frame = tmp_path / "f.jpg"
    Image.new("RGB", (64, 36), (200, 30, 30)).save(frame)
    paths = assets._make_sheets([(i, i + 1.0) for i in range(50)], [frame] * 50, tmp_path / "sheets")
    assert [p.name for p in paths] == [f"sheet_{i}.jpg" for i in range(1, 6)]
    with Image.open(paths[-1]) as im:  # 最后一张只剩 #48、#49，一行
        assert im.size == (assets.SHEET_COLS * assets.CELL_W, assets.CELL_H + assets.LABEL_H)


def test_tag_asset_batches(conn):
    import re
    aid = _fake_asset(conn, 60, sheets=5)
    calls = []

    def ask(prompt, images):
        first, last = map(int, re.search(r"本次标注镜头 #(\d+) 到 #(\d+)", prompt).groups())
        calls.append((first, last, [p.name for p in images]))
        # 故意多回一个别批的序号，应被忽略
        idxs = [*range(first, last + 1), 59 if first == 0 else 0]
        return json.dumps({"shots": [
            {"idx": i, "teams": [], "drivers": [], "circuit": None, "session": "正赛", "main": "比赛中", "sub": [],
             "shot_type": "跟拍",
             "has_subtitles": i == 50, "has_logo": i in (3, 4, 5, 7, 55) or (first == 0 and i == 59),
             "logo_note": "右上角某某体育台标" if i != 55 else "左下角 B站 UP 主水印"} for i in idxs]})

    out = asyncio.run(assets.tag_asset(conn, aid, ask=ask))
    assert calls == [(0, 47, ["sheet_1.jpg", "sheet_2.jpg", "sheet_3.jpg", "sheet_4.jpg"]),
                     (48, 59, ["sheet_5.jpg"])]
    assert out["batches"] == 2 and [s["idx"] for s in out["shots"]] == list(range(60))
    assert out["logo_note"] == "镜头 #3–#5、#7：右上角某某体育台标；镜头 #55：左下角 B站 UP 主水印"
    rows = {r["idx"]: r for r in conn.execute("SELECT * FROM asset_shots WHERE asset_id=? ORDER BY idx", (aid,))}
    assert all(r["tags"] for r in rows.values())
    assert [i for i, r in rows.items() if r["has_logo"]] == [3, 4, 5, 7, 55]
    assert [i for i, r in rows.items() if r["has_subtitles"]] == [50]
    row = conn.execute("SELECT has_subtitles, has_logo FROM assets WHERE id=?", (aid,)).fetchone()
    assert tuple(row) == (1, 1)
    assert len(assets.clean_shots(conn, aid)) == 55
    assert 50 not in [s["idx"] for s in assets.clean_shots(conn, aid, allow_subtitles=False)]


def test_tag_asset_records_usage_per_batch(conn, monkeypatch):
    from hermes import codex_runner
    aid = _fake_asset(conn, 50, sheets=5)

    async def fake_run(prompt, model, effort, workdir, **kw):
        n = len(kw["images"])
        return codex_runner.CodexResult(thread_id=None, message='{"shots": []}', usage={"input_tokens": 100 * n})

    monkeypatch.setattr(codex_runner, "run", fake_run)
    out = asyncio.run(assets.tag_asset(conn, aid))
    assert out["batches"] == 2 and out["has_logo"] is False and out["logo_note"] == ""
    assert [r[0] for r in conn.execute("SELECT input_tokens FROM usage ORDER BY id")] == [400, 100]
    row = conn.execute("SELECT has_logo, logo_note, tagged_at FROM assets WHERE id=?", (aid,)).fetchone()
    assert row["has_logo"] == 0 and row["logo_note"] is None and row["tagged_at"]


def test_old_data_without_shot_flags(conn):
    logo = _fake_asset(conn, 3, sheets=1, has_subtitles=0, has_logo=1, logo_note="右上角台标")
    subs = _fake_asset(conn, 2, sheets=1, has_subtitles=1, has_logo=0)
    untagged = _fake_asset(conn, 2, sheets=1)
    c = assets.card(conn, logo)
    assert [(s["has_subtitles"], s["has_logo"], s["logo_note"]) for s in c["shots"]] == [(None, None, None)] * 3
    assert c["has_logo"] == 1 and c["logo_note"] == "右上角台标"
    assert assets.clean_shots(conn, logo) == []
    assert [s["idx"] for s in assets.clean_shots(conn, subs)] == [0, 1]
    assert assets.clean_shots(conn, subs, allow_subtitles=False) == []
    assert [s["idx"] for s in assets.clean_shots(conn, untagged, allow_subtitles=False)] == [0, 1]
    # 逐镜头标记优先于全片标记
    conn.execute("UPDATE asset_shots SET has_logo=0, has_subtitles=0 WHERE asset_id=? AND idx=1", (logo,))
    assert [s["idx"] for s in assets.clean_shots(conn, logo)] == [1]
