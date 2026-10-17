"""画面指纹查重（M4-3）。

每秒抽 FPS 帧，每帧算 64 位 pHash，得到哈希序列；两段序列在最佳对齐下大部分帧相近，即判为同一段画面。
哈希 0 留作"无信息帧"（纯色/全黑），不参与比对，避免黑场、转场互相误判。
"""
import json
import math
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque

import numpy as np
from PIL import Image

# 服务由 .app 拉起时 PATH 里没有 /opt/homebrew/bin（2026-10-12 素材 16–18 因此后处理失败）
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# 4 帧/秒：最短镜头 1.2 秒仍有 4 帧可比，对齐误差 ≤0.125 秒；再高解码和比对成本线性上升，收益很小
FPS = 4
MAX_DIST = 14          # 单帧汉明距离 ≤14/64 视为相近：同片重编码/缩放/调色多在 0–8，低码率和裁 5% 的高频画面在 10–16；
                       # 不同画面实测中位 24–34（随机两帧期望 32、标准差 4，≤14 的概率约百万分之三）
SCORE_MIN = 0.6        # 重叠帧中相近帧比例
MIN_SECONDS = 1.0      # 至少 1 秒相近帧才算，防止两三帧巧合
RANGE_EPS = 0.1        # 同素材时间范围重叠容差（秒），吸收剪辑点的浮点误差
FOOTAGE_MIN_OVERLAP = 0.5  # 指纹推算的范围精度约 ±0.25 秒，重叠要超过这个才算用过

_PROBE = 64            # 先缩到 64×64 找黑边，再裁剪、缩到 32×32
_SUB = 4               # 每个哈希帧用 4 倍帧率解码后做时间平滑
# 哈希前裁掉的上、下、左右边比例（去黑边之后）。实测角标哪怕只伸进保留区 2~3%，亮块也会翻转 8~20 位，
# 所以边距要把常见台标位置（距边 ≤15%）和字幕条（底部 ≤22%）整块让出去
_MARGIN = (0.15, 0.22, 0.15)
_BAR_LUMA = 32         # 黑边：全程每个像素都不超过此亮度（有限范围黑色是 16）
_FLAT_STD = 2.0
_BANDS = 4             # 64 位拆 4 段各 16 位做倒排索引
_CANDIDATES = 200


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    x = np.arange(n)[None, :]
    m = np.sqrt(2 / n) * np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0] /= np.sqrt(2)
    return m


_DCT32 = _dct_matrix(32)
_SHIFTS = np.arange(63, -1, -1, dtype=np.uint64)


def phash_frame(gray: np.ndarray) -> int:
    g = np.asarray(gray, dtype=np.float64)
    if g.shape != (32, 32):
        g = np.asarray(Image.fromarray(g.astype(np.uint8)).resize((32, 32), Image.BOX), dtype=np.float64)
    if g.std() < _FLAT_STD:
        return 0
    low = (_DCT32 @ g @ _DCT32.T)[:8, :8].ravel()
    bits = low > np.median(low[1:])
    bits[0] = True  # DC 位恒为 1，保证有效帧哈希不为 0
    return int((bits.astype(np.uint64) << _SHIFTS).sum())


def _crop_box(peak: np.ndarray) -> tuple[int, int, int, int]:
    """找上下/左右对称的黑边（信箱、柱状黑边），返回 (top, bottom, left, right) 要裁的行列数。
    peak：全程逐像素最大亮度。"""
    n = peak.shape[0]

    def run(lines):
        c = 0
        for line in lines:
            if line.max() > _BAR_LUMA:
                break
            c += 1
        return c

    tb = min(run(peak), run(peak[::-1]))
    lr = min(run(peak.T), run(peak.T[::-1]))
    # 只认对称黑边：单侧暗（夜空、暗角）不裁，避免同一画面在不同时间范围裁法不一致
    tb = tb if 2 <= tb <= n // 4 else 0
    lr = lr if 2 <= lr <= n // 4 else 0
    return tb, tb, lr, lr


def _hash_frames(frames: list[np.ndarray], peak: np.ndarray) -> list[int]:
    if not frames:
        return []
    t, b, l, r = _crop_box(peak)
    h, w = peak.shape[0] - t - b, peak.shape[1] - l - r
    # 只取画面中部：台标、字幕条、滚动条几乎都在四周，转载者加的东西会整体改掉 pHash
    t, b = t + round(h * _MARGIN[0]), b + round(h * _MARGIN[1])
    l, r = l + round(w * _MARGIN[2]), r + round(w * _MARGIN[2])
    out = []
    for f in frames:
        f = f[t:f.shape[0] - b, l:f.shape[1] - r]
        small = np.asarray(Image.fromarray(f).resize((32, 32), Image.BOX))
        out.append(phash_frame(small))
    return out


def _decode(path, start: float, end: float | None, fps: float) -> tuple[list[np.ndarray], np.ndarray]:
    """按 fps×_SUB 解码成 64×64 灰度，每 _SUB 帧取一个以该时刻为中心的三角加权平均帧。

    时间上做平滑，是为了让不同来源的抽帧相位差（最多半个抽帧间隔）、帧率不同、低码率噪点对哈希影响变小。
    返回 (平均帧列表, 全程逐像素最大亮度)。
    """
    cmd = [FFMPEG, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{max(end - start, 0):.3f}"]
    cmd += ["-an", "-sn", "-dn", "-vf", f"fps={fps * _SUB},scale={_PROBE}:{_PROBE}:flags=area,format=gray",
            "-f", "rawvideo", "-"]
    size = _PROBE * _PROBE
    weights = np.array([_SUB - abs(d) for d in range(-_SUB + 1, _SUB)], dtype=np.float32)
    win = deque(maxlen=2 * _SUB - 1)
    out = []
    peak = np.zeros((_PROBE, _PROBE), dtype=np.uint8)

    def emit(c):
        acc = np.zeros((_PROBE, _PROBE), dtype=np.float32)
        wsum = 0.0
        for j, f in win:
            if abs(j - c) < _SUB:
                acc += weights[j - c + _SUB - 1] * f
                wsum += weights[j - c + _SUB - 1]
        out.append(np.round(acc / wsum).astype(np.uint8))

    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        j = -1
        while len(buf := proc.stdout.read(size)) == size:
            j += 1
            f = np.frombuffer(buf, dtype=np.uint8).reshape(_PROBE, _PROBE)
            np.maximum(peak, f, out=peak)
            win.append((j, f.astype(np.float32)))
            c = j - (_SUB - 1)
            if c >= 0 and c % _SUB == 0:
                emit(c)
        proc.stdout.close()
        if proc.wait() != 0:
            err.seek(0)
            raise RuntimeError(f"ffmpeg 解码失败：{err.read().decode(errors='replace')[-500:]}")
    # 结尾不足半个窗口的中心点
    for c in range(len(out) * _SUB, j + 1, _SUB):
        emit(c)
    return out, peak


def video_hashes(path, start=0.0, end=None, fps=FPS) -> list[int]:
    """第 k 个哈希对应时刻 start + k/fps（前后各约 0.19 秒的加权平均画面）。"""
    return _hash_frames(*_decode(path, start, end, fps))


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _arr(h) -> np.ndarray:
    return np.asarray(list(h), dtype=np.uint64)


def match(a: list[int], b: list[int], *, max_dist=None) -> dict:
    """a[i] 与 b[i + offset] 对齐。

    对每个偏移统计重叠帧（两边都不是无信息帧）里相近帧数 m 和总数 n，取 m − (n − m) 最大的偏移
    （既要多数帧相近，又偏好更长的真实重叠，避免边缘两三帧巧合胜出）。
    同分时取平均汉明距离最小的偏移：静止/车载镜头前后几秒都"相近"，只看相近帧数会定位偏前。
    判定：score = m/n ≥ SCORE_MIN 且 m ≥ MIN_SECONDS×FPS。
    """
    max_dist = MAX_DIST if max_dist is None else max_dist
    none = {"same": False, "score": 0.0, "offset": 0, "overlap_s": 0.0}
    A, B = _arr(a), _arr(b)
    if not len(A) or not len(B):
        return none
    vb = B != 0
    size = len(A) + len(B) - 1
    n, m, d = np.zeros(size), np.zeros(size), np.zeros(size)
    # 按行分块，长片（几千帧×几千帧）也不会一次占用上 GB 内存
    for i0 in range(0, len(A), 256):
        a = A[i0:i0 + 256]
        valid = (a != 0)[:, None] & vb[None, :]
        dist = np.bitwise_count(a[:, None] ^ B[None, :])
        close = (dist <= max_dist) & valid
        diag = (np.arange(len(B))[None, :] - np.arange(i0, i0 + len(a))[:, None] + len(A) - 1).ravel()
        n += np.bincount(diag, weights=valid.ravel(), minlength=size)
        m += np.bincount(diag, weights=close.ravel(), minlength=size)
        d += np.bincount(diag, weights=(dist * valid).ravel(), minlength=size)
    obj = 2 * m - n
    tied = np.flatnonzero(obj == obj.max())
    best = int(tied[np.argmin(d[tied] / np.maximum(n[tied], 1))])
    if n[best] == 0:
        return none
    score = float(m[best] / n[best])
    return {
        "same": bool(score >= SCORE_MIN and m[best] >= MIN_SECONDS * FPS),
        "score": round(score, 3),
        "offset": best - (len(A) - 1),
        "overlap_s": round(float(n[best]) / FPS, 2),
    }


def _hex(hs: list[int]) -> str:
    return json.dumps([f"{h:016x}" for h in hs])


def _unhex(s: str) -> list[int]:
    return [int(x, 16) for x in json.loads(s)]


def _shot_t0(start_s: float) -> float:
    """整片按全局网格 k/FPS 抽帧，镜头第 0 帧是 start_s 之后的第一个网格点。"""
    return math.ceil(start_s * FPS - 1e-6) / FPS


def index_asset(conn, asset_id) -> int:
    row = conn.execute("SELECT local_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row or not row["local_path"]:
        raise ValueError(f"素材 {asset_id} 没有本地文件")
    shots = conn.execute("SELECT id, start_s, end_s FROM asset_shots WHERE asset_id=? ORDER BY idx",
                         (asset_id,)).fetchall()
    if not shots:
        return 0
    # 整片解码一次再按镜头切，比每个镜头各起一次 ffmpeg 快得多；黑边也按整片统一判断
    end = max(s["end_s"] for s in shots)
    hashes = video_hashes(row["local_path"], 0.0, end)
    for s in shots:
        k0 = round(_shot_t0(s["start_s"]) * FPS)
        k1 = math.ceil(s["end_s"] * FPS - 1e-6)
        conn.execute("UPDATE asset_shots SET phash=? WHERE id=?", (_hex(hashes[k0:k1]), s["id"]))
    _INDEX.clear()
    return len(shots)


class _Index:
    def __init__(self, conn):
        rows = conn.execute("SELECT asset_id, idx, start_s, end_s, phash FROM asset_shots "
                            "WHERE phash IS NOT NULL AND NOT split ORDER BY asset_id, idx").fetchall()
        self.shots = []
        self.post = defaultdict(list)
        for r in rows:
            hs = _unhex(r["phash"])
            if not hs:
                continue
            pos = len(self.shots)
            self.shots.append((r["asset_id"], r["idx"], r["start_s"], r["end_s"], hs))
            for key in _band_keys(hs):
                self.post[key].append(pos)


_INDEX: dict = {}


def _band_keys(hs) -> set[int]:
    keys = set()
    for h in hs:
        if h:
            for i in range(_BANDS):
                keys.add((i << 16) | ((h >> (16 * i)) & 0xFFFF))
    return keys


def _index(conn) -> _Index:
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    sig = (db,) + tuple(conn.execute(
        "SELECT count(*), coalesce(max(id), 0), coalesce(sum(length(phash)), 0) "
        "FROM asset_shots WHERE phash IS NOT NULL AND NOT split").fetchone())
    if db not in _INDEX or _INDEX[db][0] != sig:
        _INDEX[db] = (sig, _Index(conn))
    return _INDEX[db][1]


def find_matches(conn, hashes: list[int], *, exclude_asset=None, limit=10) -> list[dict]:
    """倒排索引粗筛：任一帧的任一 16 位分段完全相同就算候选，按命中分段数取前 200 个再逐个 match 精比。

    offset_s：查询第 0 帧落在该镜头第 0 帧之后多少秒（可为负）。
    """
    idx = _index(conn)
    hits = defaultdict(int)
    for key in _band_keys(hashes):
        for pos in idx.post.get(key, ()):
            hits[pos] += 1
    cands = sorted((p for p in hits if idx.shots[p][0] != exclude_asset), key=hits.get, reverse=True)
    out = []
    for pos in cands[:_CANDIDATES]:
        asset_id, shot_idx, start_s, end_s, hs = idx.shots[pos]
        r = match(hashes, hs)
        if r["same"]:
            out.append({"asset_id": asset_id, "shot_idx": shot_idx, "score": r["score"],
                        "offset_s": r["offset"] / FPS, "overlap_s": r["overlap_s"],
                        "src_at": _shot_t0(start_s) + r["offset"] / FPS, "shot": (start_s, end_s)})
    out.sort(key=lambda x: (-x["score"], -x["overlap_s"]))
    return out[:limit]


def _stored_hashes(conn, asset_id, src_in, src_out) -> list[int] | None:
    """从已存镜头指纹里按 src_in + m/FPS 网格取最近帧；有未索引或未覆盖的时段返回 None。"""
    shots = conn.execute("SELECT start_s, end_s, phash FROM asset_shots WHERE asset_id=? AND NOT split "
                         "AND end_s > ? AND start_s < ? ORDER BY start_s", (asset_id, src_in, src_out)).fetchall()
    if not shots or any(s["phash"] is None for s in shots):
        return None
    parsed = [(s["start_s"], s["end_s"], _unhex(s["phash"])) for s in shots]
    out = []
    for m in range(max(int(math.ceil((src_out - src_in) * FPS - 1e-6)), 0)):
        t = src_in + m / FPS
        shot = next((p for p in parsed if p[0] - 1e-6 <= t < p[1]), None)
        if shot is None:
            return None
        hs = shot[2]
        k = min(max(round((t - _shot_t0(shot[0])) * FPS), 0), len(hs) - 1)
        out.append(hs[k] if hs else 0)
    return out


def _use_hashes(conn, asset_id, src_in, src_out) -> list[int]:
    hs = _stored_hashes(conn, asset_id, src_in, src_out)
    if hs is not None:
        return hs
    row = conn.execute("SELECT local_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row or not row["local_path"]:
        # 查重是硬失败项，查不了必须报错，不能当作没重复
        raise ValueError(f"素材 {asset_id} 既未建指纹也没有本地文件，无法查重")
    return video_hashes(row["local_path"], src_in, src_out)


def _overlap(a0, a1, b0, b1) -> float:
    return min(a1, b1) - max(a0, b0)


def timeline_duplicates(conn, uses: list[dict], *, skip=None, missing: list | None = None) -> list[dict]:
    """一期里的重复画面：同素材时间范围重叠直接算；其余两两比指纹，同素材不同时段也比
    （车载这类同机位长镜头换个时段看起来还是一样，2026-10-13 用户定算重复）。
    skip(i, j) 为真的一对只查时间重叠、不比指纹。既没建指纹也没原片的片段比不了：默认报错（查重是硬失败项）；
    传了 missing 就把这些片段的序号放进去、跳过指纹比对。"""
    cache = {}

    def hashes(i):
        if i not in cache:
            u = uses[i]
            try:
                cache[i] = _use_hashes(conn, u["asset_id"], float(u["src_in"]), float(u["src_out"]))
            except ValueError:
                if missing is None:
                    raise
                missing.append(i)
                cache[i] = None
        return cache[i]

    out = []
    for i in range(len(uses)):
        for j in range(i + 1, len(uses)):
            a, b = uses[i], uses[j]
            if a["asset_id"] == b["asset_id"] and _overlap(float(a["src_in"]), float(a["src_out"]),
                                                           float(b["src_in"]), float(b["src_out"])) > RANGE_EPS:
                out.append({"a": i, "b": j, "kind": "same_asset_overlap", "score": 1.0})
                continue
            if skip and skip(i, j):
                continue
            ha, hb = hashes(i), hashes(j)
            if ha is None or hb is None:
                continue
            r = match(ha, hb)
            if r["same"]:
                out.append({"a": i, "b": j, "kind": "same_footage", "score": r["score"]})
    return out


def cross_episode_hits(conn, episode_id, asset_id, src_in, src_out) -> list[dict]:
    found = {}
    rows = conn.execute("SELECT id, episode_id, asset_id, src_in, src_out FROM asset_uses "
                        "WHERE asset_id=? AND episode_id != ? AND src_out > ? AND src_in < ?",
                        (asset_id, episode_id, src_in + RANGE_EPS, src_out - RANGE_EPS)).fetchall()
    for r in rows:
        found[r["id"]] = (r, 1.0)

    # 不同素材同一画面：先找匹配镜头，推算对方素材里的对应时段，再看别的期有没有用过那一段
    dur = src_out - src_in
    for m in find_matches(conn, _use_hashes(conn, asset_id, src_in, src_out),
                          exclude_asset=asset_id, limit=50):
        s0 = max(m["src_at"], m["shot"][0])
        s1 = min(m["src_at"] + dur, m["shot"][1])
        for r in conn.execute("SELECT id, episode_id, asset_id, src_in, src_out FROM asset_uses "
                              "WHERE asset_id=? AND episode_id != ?", (m["asset_id"], episode_id)):
            if _overlap(s0, s1, r["src_in"], r["src_out"]) >= FOOTAGE_MIN_OVERLAP:
                if r["id"] not in found or found[r["id"]][1] < m["score"]:
                    found[r["id"]] = (r, m["score"])
    return [{"episode_id": r["episode_id"], "asset_id": r["asset_id"], "src_in": r["src_in"],
             "src_out": r["src_out"], "score": s}
            for r, s in sorted(found.values(), key=lambda x: (-x[1], x[0]["episode_id"]))]
