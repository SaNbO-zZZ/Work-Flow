import io
import json
import random
import subprocess

import numpy as np
import pytest
from PIL import Image

from hermes import db, fingerprint as fp

W, H = 320, 180
MOVER = "[m]overlay=x='mod(t*140,W)':y='H/3+sin(t*3)*H/4'"


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], check=True)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("fp")
    src = d / "orig.mp4"
    # 8 秒三个场景 + 移动色块：testsrc2 / mandelbrot / cellauto
    _ff("-f", "lavfi", "-t", "2.5", "-i", f"testsrc2=s={W}x{H}:r=25",
        "-f", "lavfi", "-t", "2.5", "-i", f"mandelbrot=s={W}x{H}:r=25:start_scale=1.5:end_scale=0.01:end_pts=60",
        "-f", "lavfi", "-t", "3", "-i", f"cellauto=s={W}x{H}:r=25:rule=110:seed=7",
        "-f", "lavfi", "-t", "8", "-i", "testsrc=s=80x80:r=25",
        "-filter_complex", f"[0][1][2]concat=n=3:v=1:a=0[c];[3]null[m];[c]{MOVER},format=yuv420p",
        "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(src))
    # 另一 UP 主转载：从 2.5 秒起截，低码率、缩小、加信箱黑边
    repost = d / "repost.mp4"
    _ff("-ss", "2.5", "-i", str(src), "-vf", "scale=256:144,pad=256:192:0:24,format=yuv420p",
        "-c:v", "libx264", "-crf", "35", "-preset", "veryfast", str(repost))
    other = d / "other.mp4"
    _ff("-f", "lavfi", "-t", "3", "-i", f"life=s={W}x{H}:r=25:seed=3:mold=10:ratio=0.3",
        "-f", "lavfi", "-t", "3", "-i", f"gradients=s={W}x{H}:r=25:seed=5:speed=0.05",
        "-filter_complex", "[0][1]concat=n=2:v=1:a=0,format=yuv420p",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", str(other))
    return {"orig": src, "repost": repost, "other": other}


def _smooth_image(seed):
    rng = np.random.default_rng(seed)
    small = (rng.random((6, 6)) * 255).astype(np.uint8)
    return Image.fromarray(small).resize((256, 256), Image.BICUBIC)


def _gray32(img):
    return np.asarray(img.convert("L").resize((32, 32), Image.BOX))


def test_phash_stable_under_compression_and_distinct_otherwise():
    img = _smooth_image(1)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=25)
    jpg = Image.open(io.BytesIO(buf.getvalue()))
    h1, h2 = fp.phash_frame(_gray32(img)), fp.phash_frame(_gray32(jpg))
    assert fp.hamming(h1, h2) <= 4
    dists = [fp.hamming(h1, fp.phash_frame(_gray32(_smooth_image(s)))) for s in range(2, 12)]
    assert min(dists) > fp.MAX_DIST
    assert fp.phash_frame(np.full((32, 32), 16)) == 0          # 纯色帧 = 无信息帧
    assert h1 >> 63 == 1


def test_dct_matches_scipy():
    scipy_fft = pytest.importorskip("scipy.fft")
    x = np.random.default_rng(0).random((32, 32))
    assert np.allclose(fp._DCT32 @ x @ fp._DCT32.T, scipy_fft.dctn(x, norm="ortho"))


def test_hamming():
    assert fp.hamming(0b1011, 0b0001) == 2
    assert fp.hamming(2**64 - 1, 0) == 64


def _noisy(hs, bits, rng):
    out = []
    for h in hs:
        for b in rng.sample(range(64), bits):
            h ^= 1 << b
        out.append(h | 1 << 63)
    return out


def _rand_seq(n, rng):
    return [rng.getrandbits(64) | 1 << 63 for _ in range(n)]


def test_match_offset_partial_and_different():
    rng = random.Random(0)
    seq = _rand_seq(40, rng)
    r = fp.match(_noisy(seq[10:30], 5, rng), seq)            # 短的是长的一部分
    assert r["same"] and r["offset"] == 10 and r["overlap_s"] == 5.0
    r = fp.match(seq[0:20], _noisy(seq[12:40], 3, rng))      # 部分重叠 2 秒
    assert r["same"] and r["offset"] == -12 and r["overlap_s"] == 2.0
    assert not fp.match(seq, _rand_seq(40, rng))["same"]
    assert not fp.match(seq[0:10], seq[7:20])["same"]        # 只重叠 3 帧 < 1 秒
    assert not fp.match([0] * 20, [0] * 20)["same"]          # 黑场不算
    part = seq[:20]
    mixed = part[:14] + _rand_seq(6, rng)                    # 70% 帧相近
    assert fp.match(part, mixed)["same"]
    assert not fp.match(part, part[:8] + _rand_seq(12, rng))["same"]


def test_match_offset_in_static_scene():
    # 每帧比前一帧多翻 1 位：相隔 14 帧内都算相近，多个偏移同分，要靠距离选对
    seq = [1 << 63 | ((1 << k) - 1) for k in range(40)]
    for start in (5, 10, 20):
        r = fp.match(seq[start:start + 8], seq)
        assert r["same"] and r["offset"] == start, (start, r)


def test_video_hashes_repost_with_letterbox(videos):
    orig = fp.video_hashes(videos["orig"])
    assert len(orig) in (32, 33)      # 8 秒 × 4 帧/秒，ffmpeg fps 滤镜可能在结尾多补一帧
    repost = fp.video_hashes(videos["repost"])
    r = fp.match(repost, orig)
    assert r["same"] and r["offset"] == 10 and r["score"] >= 0.8, r
    assert not fp.match(fp.video_hashes(videos["other"]), orig)["same"]


def _asset(conn, path, shots, index=True):
    aid = conn.execute("INSERT INTO assets (source_type, local_path, created_at) VALUES ('测试', ?, ?)",
                       (str(path) if path else None, db.now())).lastrowid
    for i, (s, e) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots (asset_id, idx, start_s, end_s) VALUES (?,?,?,?)", (aid, i, s, e))
    if index:
        assert fp.index_asset(conn, aid) == len(shots)
    return aid


@pytest.fixture
def lib(conn, videos):
    a1 = _asset(conn, videos["orig"], [(0, 2.5), (2.5, 5), (5, 8)])
    a2 = _asset(conn, videos["repost"], [(0, 2.5), (2.5, 5.5)])
    a3 = _asset(conn, videos["other"], [(0, 3), (3, 6)], index=False)
    return a1, a2, a3


def test_index_asset_writes_hex(conn, lib):
    rows = conn.execute("SELECT phash FROM asset_shots WHERE asset_id=? ORDER BY idx", (lib[0],)).fetchall()
    seqs = [json.loads(r["phash"]) for r in rows]
    assert [len(s) for s in seqs] == [10, 10, 12]
    assert all(len(h) == 16 for s in seqs for h in s)


def test_find_matches(conn, lib, videos):
    a1, a2, _ = lib
    q = fp.video_hashes(videos["repost"], 3.0, 5.0)          # 原片 5.5–7.5 秒（cellauto 场景）
    hits = fp.find_matches(conn, q, exclude_asset=a2)
    assert hits[0]["asset_id"] == a1 and hits[0]["shot_idx"] == 2
    assert abs(hits[0]["offset_s"] - 0.5) <= 0.25
    assert all(h["asset_id"] != a2 for h in hits)
    assert not fp.find_matches(conn, fp.video_hashes(videos["other"]))


def test_find_matches_candidate_filter(conn):
    rng = random.Random(1)
    fake = conn.execute("INSERT INTO assets (source_type, created_at) VALUES ('测试', ?)", (db.now(),)).lastrowid
    target = _rand_seq(12, rng)
    rows = [(fake, i, 0, 3, fp._hex(_rand_seq(12, rng))) for i in range(3000)]
    rows.append((fake, 3000, 0, 3, fp._hex(target)))
    # 与 target 各帧都共享分段但整体很远：能进候选，精比必须刷掉
    decoy = [(h & 0xFFFF) | (rng.getrandbits(48) << 16) | 1 << 63 for h in target]
    rows.append((fake, 3001, 0, 3, fp._hex(decoy)))
    conn.executemany("INSERT INTO asset_shots (asset_id, idx, start_s, end_s, phash) VALUES (?,?,?,?,?)", rows)
    idx = fp._index(conn)
    query = _noisy(target, 3, rng)   # 翻 3 位，抽屉原理保证至少一个 16 位分段完全相同
    keys = fp._band_keys(query)
    cand = {p for k in keys for p in idx.post.get(k, ())}
    assert any(idx.shots[p][1] == 3000 for p in cand) and any(idx.shots[p][1] == 3001 for p in cand)
    assert len(cand) < 300
    hits = fp.find_matches(conn, query)
    assert [h["shot_idx"] for h in hits] == [3000]


def test_timeline_duplicates(conn, lib):
    a1, a2, a3 = lib
    uses = [
        {"asset_id": a1, "src_in": 0.0, "src_out": 2.0, "timeline_at": 0},
        {"asset_id": a1, "src_in": 1.5, "src_out": 2.4, "timeline_at": 2},   # 与 0 同素材重叠
        {"asset_id": a1, "src_in": 5.2, "src_out": 7.0, "timeline_at": 3},   # 同原片不同片段：允许
        {"asset_id": a3, "src_in": 0.0, "src_out": 3.0, "timeline_at": 5},   # 未建指纹，走文件
        {"asset_id": a2, "src_in": 3.0, "src_out": 4.8, "timeline_at": 8},   # = 原片 5.5–7.3，与 2 画面相同
    ]
    got = {(d["a"], d["b"], d["kind"]) for d in fp.timeline_duplicates(conn, uses)}
    assert got == {(0, 1, "same_asset_overlap"), (2, 4, "same_footage")}
    clean = [uses[0], uses[2], uses[3], {"asset_id": a2, "src_in": 0.3, "src_out": 2.0, "timeline_at": 9}]
    assert fp.timeline_duplicates(conn, clean) == []


def test_timeline_unindexed_without_file_raises(conn):
    aid = _asset(conn, None, [], index=False)
    other = _asset(conn, None, [], index=False)
    with pytest.raises(ValueError):
        fp.timeline_duplicates(conn, [{"asset_id": aid, "src_in": 0, "src_out": 2, "timeline_at": 0},
                                      {"asset_id": other, "src_in": 0, "src_out": 2, "timeline_at": 2}])


def test_cross_episode_hits(conn, lib):
    a1, a2, a3 = lib
    now = db.now()
    e1, e2 = (conn.execute("INSERT INTO episodes (created_at, updated_at) VALUES (?, ?)", (now, now)).lastrowid
              for _ in range(2))
    conn.execute("INSERT INTO asset_uses (episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (e1, a1, 5.2, 7.0, now))
    hits = fp.cross_episode_hits(conn, e2, a2, 3.0, 4.8)     # 不同素材同一画面
    # 转载版是 256×144、CRF35 的重度劣化，1.8 秒片段分数略低于整段
    assert [(h["episode_id"], h["asset_id"]) for h in hits] == [(e1, a1)] and hits[0]["score"] >= fp.SCORE_MIN
    hits = fp.cross_episode_hits(conn, e2, a1, 6.5, 7.5)     # 同素材时间重叠
    assert [(h["episode_id"], h["score"]) for h in hits] == [(e1, 1.0)]
    assert fp.cross_episode_hits(conn, e1, a1, 5.2, 7.0) == []     # 本期自己不算
    assert fp.cross_episode_hits(conn, e2, a1, 7.0, 8.0) == []     # 紧挨着不算重叠
    assert fp.cross_episode_hits(conn, e2, a3, 0.0, 3.0) == []
    assert fp.cross_episode_hits(conn, e2, a2, 0.2, 2.2) == []     # 原片 2.7–4.7，e1 没用过
