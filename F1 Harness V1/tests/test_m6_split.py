"""长镜头按画面切细 + 同一画面只用一次（2026-10-13 用户定改法 1+3）。全部不联网。"""
import json
import random
import subprocess

import numpy as np
import pytest
from PIL import Image

from hermes import assets, config, db, fingerprint as fp, qa_checks as qa

W, H = 320, 180


def _rand_hash(rng):
    return rng.getrandbits(63) | (1 << 63)


def _noisy(h, bits, rng):
    for b in rng.sample(range(63), bits):
        h ^= 1 << b
    return h


def test_visual_cuts():
    rng = random.Random(1)
    a, b = _rand_hash(rng), _rand_hash(rng)
    seq = [_noisy(a, 4, rng) for _ in range(32)] + [_noisy(b, 4, rng) for _ in range(48)]   # 8 秒 + 12 秒
    assert assets.visual_cuts(seq) == [32]
    assert assets.visual_cuts([_noisy(a, 4, rng) for _ in range(80)]) == []                # 同一画面不切
    short = [_noisy(a, 4, rng) for _ in range(8)] + [_noisy(b, 4, rng) for _ in range(72)]  # 头 2 秒不够一段
    assert assets.visual_cuts(short) == []
    tail = [_noisy(a, 4, rng) for _ in range(72)] + [_noisy(b, 4, rng) for _ in range(8)]   # 尾 2 秒不够一段
    assert assets.visual_cuts(tail) == []
    assert assets.visual_cuts([0] * 80) == []                                               # 纯色帧不比


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    # 两张不同的平滑随机图各放 8 秒，加一点噪点（同一画面的帧之间也不完全一样）
    d = tmp_path_factory.mktemp("split")
    for name, seed in (("a.png", 1), ("b.png", 2)):
        small = (np.random.default_rng(seed).random((6, 6, 3)) * 255).astype(np.uint8)
        Image.fromarray(small).resize((W, H), Image.BICUBIC).save(d / name)
    p = d / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin",
                    "-loop", "1", "-t", "8", "-r", "25", "-i", str(d / "a.png"),
                    "-loop", "1", "-t", "8", "-r", "25", "-i", str(d / "b.png"),
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0,noise=alls=8:allf=t,format=yuv420p",
                    "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", str(p)], check=True)
    return p


def _asset(conn, path, shots):
    aid = conn.execute("INSERT INTO assets(source_type, local_path, state, created_at) VALUES ('测试', ?, 'ready', ?)",
                       (str(path), db.now())).lastrowid
    for i, (a, b) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags, has_logo, main_manual) "
                     "VALUES (?,?,?,?,?,0,?)", (aid, i, a, b, json.dumps({"main": "比赛中"}), json.dumps(["航拍风景"])))
    fp.index_asset(conn, aid)
    return aid


def test_split_long_shots(conn, video, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path)
    aid = _asset(conn, video, [(0, 16)])
    assert assets.split_long_shots(conn, aid) == 2
    rows = conn.execute("SELECT * FROM asset_shots WHERE asset_id=? ORDER BY idx", (aid,)).fetchall()
    parent, kids = rows[0], rows[1:]
    assert parent["split"] == 1 and [k["idx"] for k in kids] == [1, 2] and {k["parent_idx"] for k in kids} == {0}
    assert kids[0]["start_s"] == 0 and kids[1]["end_s"] == 16 and kids[0]["end_s"] == kids[1]["start_s"]
    assert 7 <= kids[1]["start_s"] <= 9
    assert all((tmp_path / str(aid) / "shots" / f"{k['idx']:03d}.jpg").exists() for k in kids)
    assert all(k["tags"] == parent["tags"] and k["main_manual"] == parent["main_manual"] for k in kids)
    assert len(fp._unhex(kids[0]["phash"])) + len(fp._unhex(kids[1]["phash"])) == len(fp._unhex(parent["phash"]))
    # 剪辑只看到切出来的段；再跑一次不重复切
    assert [s["idx"] for s in assets._shots(conn, aid)] == [1, 2]
    assert assets.split_long_shots(conn, aid) == 0
    # 指纹查重从切出来的段取帧，和原片直接算的一致
    assert fp._stored_hashes(conn, aid, 2.0, 10.0) is not None
    # 打标写原镜头时，切出来的段跟着改
    tag = {k: [] for k in assets.TAG_KEYS} | {"session": "正赛", "main": "航拍风景", "shot_type": "航拍",
                                               "circuit": None}
    assets.save_tags(conn, aid, {0: {**tag, "has_subtitles": False, "has_logo": True, "logo_note": "右上"}})
    kid = conn.execute("SELECT tags, has_logo, logo_note FROM asset_shots WHERE asset_id=? AND idx=2", (aid,)).fetchone()
    assert json.loads(kid["tags"])["main"] == "航拍风景" and kid["has_logo"] == 1 and kid["logo_note"] == "右上"


def test_short_shots_not_split(conn, video, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path)
    aid = _asset(conn, video, [(0, 8), (8, 16)])
    assert assets.split_long_shots(conn, aid) == 0


def _ep(conn):
    ts = db.now()
    return conn.execute("INSERT INTO episodes(branch, title, state, created_at, updated_at) VALUES ('前瞻','t','合成与QA',?,?)",
                        (ts, ts)).lastrowid


def _clip(aid, idx, a, b, start):
    return {"asset_id": aid, "shot_idx": idx, "src_in": a, "src_out": b, "start": start, "end": start + b - a}


def _tl(clips):
    return {"fps": 30, "duration": clips[-1]["end"], "video": clips}


def test_same_picture_in_timeline(conn, video):
    aid = _asset(conn, video, [(0, 4), (4, 8), (8, 16)])
    other = _asset(conn, video, [(0, 16)])
    eid = _ep(conn)
    by = lambda items, n: next(i for i in items if i["name"] == n)
    # 同素材不同镜头，但看起来一样（都是第一张图）→ 同一段画面两次
    items = qa.timeline_issues(conn, eid, _tl([_clip(aid, 0, 0.5, 3.5, 0), _clip(aid, 2, 9, 12, 3),
                                                _clip(aid, 1, 4.5, 7.5, 6)]))
    dup = by(items, "同一段画面两次")
    assert not dup["ok"] and "看起来一样" in dup["detail"] and "第1个镜头" in dup["detail"]
    assert by(items, "同一镜头用两次")["ok"]
    # 同一镜头不同时段：由“同一镜头用两次”管，不在指纹里重复报
    items = qa.timeline_issues(conn, eid, _tl([_clip(aid, 2, 8.5, 11, 0), _clip(other, 0, 0.5, 3, 2.5),
                                                _clip(aid, 2, 13, 15.5, 5)]))
    assert not by(items, "同一镜头用两次")["ok"]
    assert "第1个镜头与第3个镜头" not in by(items, "同一段画面两次")["detail"]
    # 不同素材、画面不同 → 都过
    items = qa.timeline_issues(conn, eid, _tl([_clip(aid, 0, 0.5, 3.5, 0), _clip(other, 0, 9, 12, 3)]))
    assert by(items, "同一段画面两次")["ok"] and by(items, "同一镜头用两次")["ok"]
