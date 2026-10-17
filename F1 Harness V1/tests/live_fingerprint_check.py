"""M4 验收①实测：换编码/缩放/裁切/调色/截取的同一段画面能否认出，负样本不误报。

用法：python tests/live_fingerprint_check.py [原片路径] [负样本路径 ...] [--max 秒数]
不给原片时用 ffmpeg 合成约 30 秒多场景视频。全部在临时目录里做，不碰正式库。
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="hermes-fp-live-"))
os.environ["HERMES_CORE"] = str(TMP / "core")
os.environ["HERMES_ROOT"] = str(TMP / "root")
os.environ["HERMES_JOBS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from hermes import db, fingerprint as fp  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *map(str, args)], check=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return float(j["format"]["duration"]), j["streams"][0]["width"], j["streams"][0]["height"]


def text_png(text, path):
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 64)
    img = Image.new("RGBA", (900, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((10, 10), text, font=font, fill="white", stroke_width=4, stroke_fill="black")
    img.save(path)
    return path


def synth(path, scenes, text):
    """若干 6~10 秒场景拼接，540p 生成后放大到 1080p，叠移动文字和移动测试图块。"""
    args, labels = [], []
    for i, (dur, src) in enumerate(scenes):
        args += ["-f", "lavfi", "-t", dur, "-i", f"{src}:r=30"]
        labels.append(f"[{i}]scale=960:540,setsar=1[s{i}]")
    n = len(scenes)
    total = sum(float(d) for d, _ in scenes)
    args += ["-loop", "1", "-t", total, "-i", text_png(text, TMP / f"{path.stem}.png"),
             "-f", "lavfi", "-t", total, "-i", "testsrc=s=160x160:r=30"]
    graph = ";".join(labels) + ";" + "".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,scale=1920:1080[c];" \
        f"[c][{n}]overlay=x='mod(t*260,W+w)-w':y='H*0.72'[t];" \
        f"[t][{n + 1}]overlay=x='W/2+sin(t*1.3)*W/3':y='H/4+cos(t*0.9)*H/6',format=yuv420p"
    ff(*args, "-filter_complex", graph, "-c:v", "libx264", "-crf", "12", "-preset", "fast", path)
    return path


ORIG_SCENES = [
    ("6", "testsrc2=s=960x540"),
    ("6", "mandelbrot=s=960x540:maxiter=300:start_scale=2:end_scale=0.002:end_pts=40"),
    ("6", "life=s=480x270:seed=11:mold=20:ratio=0.25:life_color=#ffcc33:death_color=#113355"),
    ("6", "cellauto=s=960x540:rule=110:seed=21"),
    ("6", "gradients=s=960x540:seed=8:speed=0.08:n=5"),
]
NEG_SCENES = [
    [("10", "mandelbrot=s=960x540:maxiter=300:start_x=0.28:start_y=0.008:start_scale=0.2:end_scale=0.0005:end_pts=60"),
     ("10", "life=s=480x270:seed=99:mold=5:ratio=0.4"),
     ("10", "cellauto=s=960x540:rule=30:seed=5")],
    [("10", "testsrc=s=960x540"),
     ("10", "gradients=s=960x540:seed=42:speed=0.05:n=3"),
     ("10", "smptehdbars=s=960x540")],
]


def variants(src, span, w, h):
    enc = ["-c:v", "libx264", "-crf", "20", "-preset", "fast"]
    v = [
        ("H.264 CRF18", 0, ["-c:v", "libx264", "-crf", "18", "-preset", "medium"], None, None),
        ("H.264 500k", 0, ["-c:v", "libx264", "-b:v", "500k", "-maxrate", "600k", "-bufsize", "1000k"], None, None),
        ("HEVC x265", 0, ["-c:v", "libx265", "-crf", "28", "-preset", "fast", "-x265-params", "log-level=error"],
         None, None),
        ("缩到 720p", 0, enc, "scale=-2:720", None),
        ("缩到 480p", 0, enc, "scale=-2:480", None),
        ("四周裁 5% 放大回原尺寸", 0, enc, f"crop=iw*0.9:ih*0.9,scale={w}:{h}", None),
        ("调亮度/对比度/饱和度", 0, enc, "eq=brightness=0.06:contrast=1.15:saturation=1.4", None),
        ("5 秒处截 10 秒", 5.0, enc, None, 10.0),
        ("5.13 秒处截 10 秒(非整帧)", 5.13, enc, None, 10.0),
        ("加信箱黑边 4:3", 0, enc, f"scale={w * 3 // 4 // 2 * 2}:-2,pad={w * 3 // 4 // 2 * 2}:{w * 9 // 16 // 2 * 2}:(ow-iw)/2:(oh-ih)/2", None),
        ("改 25fps", 0, enc + ["-r", "25"], None, None),
        ("加台标+底部字幕条", 0, enc,
         "drawbox=x=0:y=ih*0.84:w=iw:h=ih*0.1:color=black@0.7:t=fill,"
         "drawbox=x=iw*0.84:y=ih*0.04:w=iw*0.13:h=ih*0.09:color=white@0.85:t=fill", None),
        ("镜像翻转(不要求)", 0, enc, "hflip", None),
    ]
    out = []
    for i, (name, ss, codec, vf, t) in enumerate(v):
        p = TMP / f"v{i:02d}.mp4"
        args = []
        if ss:
            args += ["-ss", ss]
        args += ["-t", t or span, "-i", src]
        if vf:
            args += ["-vf", vf]
        ff(*args, "-an", *codec, "-pix_fmt", "yuv420p", p)
        out.append((name, p, ss))
    return out


def dist_stats(a, b, offset):
    d = [fp.hamming(x, b[i + offset]) for i, x in enumerate(a) if 0 <= i + offset < len(b) and x and b[i + offset]]
    return (int(np.median(d)), int(np.percentile(d, 90))) if d else (-1, -1)


def windows(hs, secs=2.0, step=2.0):
    n, s = int(secs * fp.FPS), int(step * fp.FPS)
    return [(i, hs[i:i + n]) for i in range(0, len(hs) - n + 1, s)]


def main():
    argv = sys.argv[1:]
    max_s = 60.0
    if "--max" in argv:
        k = argv.index("--max")
        max_s = float(argv[k + 1])
        del argv[k:k + 2]
    random.seed(0)
    t0 = time.time()
    if argv:
        src = Path(argv[0])
        dur, w, h = probe(src)
        span = min(dur, max_s)
        negs = [(f"外部负样本 {Path(p).name}", Path(p), 0, None) for p in argv[1:]]
        if dur >= span + 25:
            # 同一场比赛的其他段落：最难的负样本（同机位、同涂装）
            negs.append((f"同片其他段落 {dur - 20:.0f}–{dur - 5:.0f}s", src, dur - 20, dur - 5))
    else:
        src = synth(TMP / "orig.mp4", ORIG_SCENES, "F1 HERMES  LAP 23  P1 VER +0.412")
        dur, w, h = probe(src)
        span = dur
        negs = []
    for i, sc in enumerate(NEG_SCENES):
        negs.append((f"合成负样本 {i + 1}", synth(TMP / f"neg{i}.mp4", sc, "OTHER CHANNEL  NEWS 24/7"), 0, None))
    print(f"原片：{src}（{dur:.1f}s，{w}×{h}，本次取前 {span:.1f}s）  准备耗时 {time.time() - t0:.1f}s")

    t = time.time()
    orig = fp.video_hashes(src, 0, span)
    orig_t = time.time() - t
    vs = variants(src, span, w, h)

    rows, speeds = [], [orig_t / span * 60]
    print("\n表 1  整段比对（变体 vs 原片）")
    head = f"{'变体':<24}| same  | score | offset(帧/秒) | 预期偏移 | 距离中位/P90 | 哈希耗时 | 比对耗时"
    print(head)
    print("-" * 104)
    for name, p, ss in vs:
        vd = probe(p)[0]
        t = time.time()
        hs = fp.video_hashes(p)
        ht = time.time() - t
        speeds.append(ht / vd * 60)
        t = time.time()
        r = fp.match(hs, orig)
        mt = time.time() - t
        exp = round(ss * fp.FPS)
        med, p90 = dist_stats(hs, orig, r["offset"])
        ok_off = abs(r["offset"] - exp) <= 1
        rows.append((name, r, ok_off))
        print(f"{name:<24}| {str(r['same']):<5} | {r['score']:.3f} | {r['offset']:>4} / {r['offset'] / fp.FPS:>5.2f}s "
              f"| {exp:>3}{'  ✓' if ok_off else '  ✗'}   | {med:>3} / {p90:<3}      | {ht:6.2f}s  | {mt * 1000:6.1f}ms")
    for name, p, ss, end in negs:
        t = time.time()
        hs = fp.video_hashes(p, ss, end)
        ht = time.time() - t
        r = fp.match(hs, orig)
        med, p90 = dist_stats(hs, orig, r["offset"])
        rows.append((name, r, None))
        print(f"{name:<24}| {str(r['same']):<5} | {r['score']:.3f} | {r['offset']:>4} / {r['offset'] / fp.FPS:>5.2f}s "
              f"|  负样本  | {med:>3} / {p90:<3}      | {ht:6.2f}s  |")

    # 表 2：2 秒短窗口（接近真实镜头长度）逐段识别
    print("\n表 2  2 秒短窗口识别（每 2 秒切一段去原片里找）")
    print(f"{'样本':<24}| 窗口数 | 判同 | 偏移正确")
    print("-" * 56)
    for name, p, ss in vs:
        hs = fp.video_hashes(p)
        ws = windows(hs)
        same = [(i, fp.match(wh, orig)) for i, wh in ws]
        hit = [x for x in same if x[1]["same"]]
        good = sum(abs(r["offset"] - (i + round(ss * fp.FPS))) <= 1 for i, r in hit)
        print(f"{name:<24}| {len(ws):>5}  | {len(hit):>4} | {good:>4}")
    fp_total = fp_hit = 0
    for name, p, ss, end in negs:
        ws = windows(fp.video_hashes(p, ss, end))
        hit = sum(fp.match(wh, orig)["same"] for _, wh in ws)
        fp_total += len(ws)
        fp_hit += hit
        print(f"{name:<24}| {len(ws):>5}  | {hit:>4} |  (负样本，判同即误报)")

    # 表 3：检索规模（临时库：原片按 3 秒一镜头入库 + 上万个随机假镜头）
    print("\n表 3  检索规模（临时库）")
    conn = db.connect(TMP / "bench.db")
    aid = conn.execute("INSERT INTO assets (source_type, local_path, created_at) VALUES ('实测', ?, ?)",
                       (str(src), db.now())).lastrowid
    cuts = np.arange(0, span, 3.0).tolist() + [span]
    for i in range(len(cuts) - 1):
        conn.execute("INSERT INTO asset_shots (asset_id, idx, start_s, end_s) VALUES (?,?,?,?)",
                     (aid, i, cuts[i], cuts[i + 1]))
    t = time.time()
    fp.index_asset(conn, aid)
    idx_t = time.time() - t
    fake = conn.execute("INSERT INTO assets (source_type, created_at) VALUES ('假', ?)", (db.now(),)).lastrowid
    rng = random.Random(1)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO asset_shots (asset_id, idx, start_s, end_s, phash) VALUES (?,?,?,?,?)",
                     [(fake, i, 0, 5, fp._hex([rng.getrandbits(64) | 1 << 63 for _ in range(rng.randint(5, 40))]))
                      for i in range(20000)])
    conn.execute("COMMIT")
    t = time.time()
    fp._index(conn)
    build_t = time.time() - t
    q_name, q_path, q_ss = vs[1]  # 500k 低码率版
    ws = windows(fp.video_hashes(q_path))
    t = time.time()
    found = 0
    for i, wh in ws:
        res = fp.find_matches(conn, wh, limit=3)
        src_t = i / fp.FPS
        found += bool(res) and res[0]["asset_id"] == aid and abs(res[0]["src_at"] - src_t) <= 0.5
    q_t = (time.time() - t) / max(len(ws), 1)
    n_shots = conn.execute("SELECT count(*) FROM asset_shots").fetchone()[0]
    print(f"库内镜头 {n_shots}，原片建指纹 {idx_t:.2f}s，倒排索引构建 {build_t:.2f}s（有缓存，库不变不重建）")
    print(f"用「{q_name}」的 {len(ws)} 个 2 秒窗口检索：命中且定位正确 {found}/{len(ws)}，平均每次 {q_t * 1000:.1f}ms")

    print("\n汇总")
    pos = [(n, r, ok) for n, r, ok in rows if ok is not None and "翻转" not in n]
    neg = [(n, r) for n, r, ok in rows if ok is None]
    print(f"要求识别的变体：{sum(r['same'] and ok for _, r, ok in pos)}/{len(pos)} 判同且偏移正确")
    print(f"负样本整段误报：{sum(r['same'] for _, r in neg)}/{len(neg)}；2 秒窗口误报 {fp_hit}/{fp_total}")
    flip = [r for n, r, _ in rows if "翻转" in n][0]
    print(f"镜像翻转（仅记录）：same={flip['same']} score={flip['score']}")
    print(f"计算速度：每分钟视频 {np.mean(speeds):.1f}s（原片 {speeds[0]:.1f}s/分钟，最慢 {max(speeds):.1f}s/分钟）")
    print(f"参数：FPS={fp.FPS} MAX_DIST={fp.MAX_DIST} SCORE_MIN={fp.SCORE_MIN} MIN_SECONDS={fp.MIN_SECONDS}")
    print(f"总耗时 {time.time() - t0:.0f}s，临时目录 {TMP}")
    bad = [n for n, r, ok in pos if not (r["same"] and ok)] + [n for n, r in neg if r["same"]]
    if bad:
        print("未通过：" + "、".join(bad))
        sys.exit(1)


if __name__ == "__main__":
    main()
