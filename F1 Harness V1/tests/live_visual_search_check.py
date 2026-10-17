"""M4-4 实测：下载/加载中文 CLIP，用自绘测试图建临时库，统计中文查询命中率。

运行：cd /Volumes/HermesCore && .venv/bin/python tests/live_visual_search_check.py
不碰正式数据库；模型缓存在 config.HF_CACHE。
"""
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from hermes import config, db, visual_search as vs  # noqa: E402

W, H = 640, 360
rng = np.random.default_rng(7)


def _font(size):
    for p in [*sorted(config.FONTS_DIR.glob("*Heavy*")), *sorted(config.FONTS_DIR.glob("*Bold*")),
              Path("/System/Library/Fonts/PingFang.ttc"), Path("/System/Library/Fonts/STHeiti Medium.ttc"),
              Path("/System/Library/Fonts/Hiragino Sans GB.ttc")]:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def red_car(p):
    im = Image.new("RGB", (W, H), (150, 150, 155))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 250, W, H], fill=(70, 70, 75))
    d.polygon([(80, 230), (180, 200), (300, 190), (360, 150), (430, 150), (470, 190), (580, 205), (590, 240), (80, 245)], fill=(215, 20, 25))
    d.rectangle([60, 185, 100, 240], fill=(200, 15, 20))
    d.rectangle([560, 215, 620, 240], fill=(200, 15, 20))
    d.ellipse([370, 155, 415, 185], fill=(30, 30, 35))
    for cx in (160, 500):
        d.ellipse([cx - 45, 200, cx + 45, 290], fill=(20, 20, 20))
        d.ellipse([cx - 18, 227, cx + 18, 263], fill=(90, 90, 90))
    im.save(p)


def blue_sky(p):
    y = np.linspace(0, 1, H)[:, None, None]
    arr = (np.array([40, 110, 220]) * (1 - y) + np.array([150, 200, 250]) * y) * np.ones((1, W, 1))
    im = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(im)
    for cx, cy in [(150, 100), (420, 70), (520, 200)]:
        for dx, dy, r in [(-40, 10, 35), (0, 0, 45), (45, 12, 35)]:
            d.ellipse([cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r], fill=(250, 250, 252))
    im.save(p)


def grass(p):
    arr = np.zeros((H, W, 3))
    arr[..., 1] = 140 + rng.normal(0, 25, (H, W))
    arr[..., 0] = 50 + rng.normal(0, 10, (H, W))
    arr[..., 2] = 30
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for _ in range(600):
        x, y = rng.integers(0, W), rng.integers(40, H)
        d.line([x, y, x + rng.integers(-6, 7), y - rng.integers(10, 30)], fill=(40, int(rng.integers(150, 210)), 40), width=2)
    im.save(p)


def checker(p):
    s = 40
    arr = ((np.indices((H, W)) // s).sum(axis=0) % 2 * 255).astype(np.uint8)
    Image.fromarray(arr).convert("RGB").save(p)


def night_city(p):
    im = Image.new("RGB", (W, H), (8, 10, 30))
    d = ImageDraw.Draw(im)
    x = 0
    while x < W:
        bw, bh = int(rng.integers(40, 90)), int(rng.integers(120, 300))
        d.rectangle([x, H - bh, x + bw, H], fill=(20, 22, 40))
        for wy in range(H - bh + 10, H - 10, 18):
            for wx in range(x + 6, x + bw - 8, 14):
                if rng.random() < 0.55:
                    d.rectangle([wx, wy, wx + 6, wy + 8], fill=(255, 210, 90))
        x += bw + 4
    for _ in range(40):
        cx, cy = rng.integers(0, W), rng.integers(0, 80)
        d.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=(230, 230, 255))
    im.save(p)


def rain(p):
    im = Image.new("RGB", (W, H), (105, 110, 118))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 270, W, H], fill=(70, 75, 82))
    for _ in range(500):
        x, y = rng.integers(0, W), rng.integers(0, H)
        d.line([x, y, x - 4, y + 22], fill=(200, 205, 215), width=1)
    for _ in range(25):
        cx, cy = rng.integers(0, W), rng.integers(280, H)
        d.ellipse([cx - 14, cy - 3, cx + 14, cy + 3], outline=(170, 175, 185))
    im.save(p)


def line_chart(p):
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=100)
    x = np.arange(1, 21)
    for c in ("tab:blue", "tab:orange", "tab:green"):
        ax.plot(x, np.cumsum(rng.normal(0.3, 1, len(x))), marker="o", color=c)
    ax.grid(True, alpha=0.4)
    ax.set_xlabel("Lap")
    ax.set_ylabel("Gap (s)")
    fig.tight_layout()
    fig.savefig(p)
    plt.close(fig)


def bar_chart(p):
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=100)
    ax.bar(list("ABCDEFG"), rng.integers(5, 30, 7), color="tab:purple")
    ax.set_ylabel("Points")
    fig.tight_layout()
    fig.savefig(p)
    plt.close(fig)


def poster(p):
    im = Image.new("RGB", (W, H), (250, 200, 30))
    d = ImageDraw.Draw(im)
    d.text((W // 2, 150), "周末大促", font=_font(120), fill=(20, 20, 20), anchor="mm")
    d.text((W // 2, 280), "全场五折 限时三天", font=_font(44), fill=(180, 20, 20), anchor="mm")
    im.save(p)


def sunset(p):
    y = np.linspace(0, 1, H)[:, None, None]
    sky = np.array([250, 120, 40]) * (1 - y) + np.array([255, 200, 120]) * y
    arr = sky * np.ones((1, W, 1))
    arr[220:] = np.array([30, 60, 110]) + rng.normal(0, 8, (H - 220, W, 3))
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    d.ellipse([260, 150, 380, 270], fill=(255, 230, 150))
    d.rectangle([0, 220, W, 222], fill=(255, 190, 110))
    im.save(p)


def snow_mountain(p):
    im = Image.new("RGB", (W, H), (110, 170, 235))
    d = ImageDraw.Draw(im)
    for base, peak, h in [(0, 200, 230), (180, 380, 280), (380, 560, 210)]:
        top = (peak, H - h)
        d.polygon([(base - 60, H), top, (base + 360, H)], fill=(90, 95, 110))
        d.polygon([top, (peak - 50, H - h + 70), (peak + 55, H - h + 75)], fill=(250, 250, 252))
    d.rectangle([0, 320, W, H], fill=(240, 244, 250))
    im.save(p)


def track_map(p) -> str:
    """优先用 FastF1 离线缓存画巴库赛道；缓存里没有位置数据就画一个仿赛道图。"""
    try:
        import fastf1

        logging.getLogger("fastf1").setLevel(logging.ERROR)
        fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
        fastf1.Cache.offline_mode(True)
        s = fastf1.get_session(2026, "Azerbaijan", "R")
        s.load(laps=True, telemetry=True, weather=False, messages=False)
        pos = s.laps.pick_fastest().get_pos_data()
        xs, ys = pos["X"].to_numpy(), pos["Y"].to_numpy()
        src = "FastF1 2026 巴库"
    except Exception as e:  # noqa: BLE001
        t = np.linspace(0, 2 * np.pi, 400)
        r = 1 + 0.25 * np.sin(3 * t) + 0.12 * np.cos(5 * t)
        xs, ys = r * np.cos(t) * 1.6, r * np.sin(t)
        src = f"仿赛道图（FastF1 缓存不可用：{type(e).__name__}: {str(e)[:60]}）"
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=100)
    fig.patch.set_facecolor("#15151e")
    ax.set_facecolor("#15151e")
    ax.plot(xs, ys, color="white", linewidth=4)
    ax.plot(xs[:1], ys[:1], "o", color="red", markersize=10)
    step = max(1, len(xs) // 12)
    for n, i in enumerate(range(step, len(xs), step), 1):
        ax.text(xs[i], ys[i], str(n), color="#ffcc00", fontsize=9, ha="center", va="center")
    ax.set_aspect("equal")
    ax.axis("off")
    fig.savefig(p, facecolor=fig.get_facecolor())
    plt.close(fig)
    return src


IMAGES = {
    "红色的车": red_car, "蓝色的天空": blue_sky, "绿色草地": grass, "黑白棋盘格": checker,
    "夜晚城市灯光": night_city, "雨天": rain, "折线图": line_chart, "柱状图": bar_chart,
    "文字海报": poster, "海边日落": sunset, "雪山": snow_mountain, "赛道地图": track_map,
}
QUERIES = {
    "红色赛车": "红色的车", "下雨": "雨天", "折线图": "折线图", "赛道地图": "赛道地图",
    "夜景": "夜晚城市灯光", "蓝天白云": "蓝色的天空", "绿色的草地": "绿色草地",
    "写着大字的海报": "文字海报", "海边的日落": "海边日落", "柱状图": "柱状图",
}


def du(path: Path) -> str:
    if not path.exists():
        return "（不存在）"
    return subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True).stdout.split()[0]


def main():
    from huggingface_hub import list_repo_files, snapshot_download

    print(f"模型 {config.CLIP_MODEL}；缓存目录 {config.HF_CACHE}（运行前 {du(config.HF_CACHE)}）")
    t0 = time.perf_counter()
    files = list_repo_files(config.CLIP_MODEL)
    weights = ["*.safetensors"] if any(f.endswith(".safetensors") for f in files) else ["*.bin"]
    snapshot_download(config.CLIP_MODEL, cache_dir=config.HF_CACHE, allow_patterns=["*.json", "*.txt", *weights])
    print(f"1) 下载（已有缓存则为校验）耗时 {time.perf_counter() - t0:.1f}s，权重格式 {weights[0]}")

    emb = vs.default_embedder()
    emb._load()
    print(f"   加载耗时 {emb.load_seconds:.1f}s，设备 {emb.device}")

    tmp = Path(tempfile.mkdtemp(prefix="hermes-vs-live-"))
    paths = {}
    notes = {}
    for name, fn in IMAGES.items():
        p = tmp / f"{name}.png"
        notes[name] = fn(p)
        paths[name] = p
    print(f"2) 测试图 {len(paths)} 张，目录 {tmp}；赛道图来源：{notes['赛道地图']}")

    one_img, one_txt = [paths["红色的车"]], ["红色赛车"]
    emb.images(one_img), emb.texts(one_txt)  # 预热
    n = 10
    t = time.perf_counter()
    for _ in range(n):
        emb.images(one_img)
    ti = (time.perf_counter() - t) / n
    t = time.perf_counter()
    for _ in range(n):
        emb.texts(one_txt)
    tt = (time.perf_counter() - t) / n
    print(f"   单张图片编码 {ti * 1000:.0f}ms，单句文本编码 {tt * 1000:.0f}ms（{emb.device}，{n} 次平均）")

    conn = db.connect(tmp / "live.db")
    aid = conn.execute("INSERT INTO assets (source_type, title, created_at) VALUES ('实测', '以文搜图测试图', ?)", (db.now(),)).lastrowid
    names = list(paths)
    for i, name in enumerate(names):
        conn.execute(
            "INSERT INTO asset_shots (asset_id, idx, start_s, end_s, keyframe) VALUES (?,?,?,?,?)",
            (aid, i, i * 3.0, i * 3.0 + 3.0, str(paths[name])),
        )
    t = time.perf_counter()
    cnt = vs.index_asset(conn, aid)
    print(f"3) 索引 {cnt} 个镜头，耗时 {time.perf_counter() - t:.2f}s")

    for prompts in (False, True):
        print(f"\n--- 查询模板 {'开（原句+“一张…的照片”+“…的视频截图”取平均）' if prompts else '关（只用原句）'} ---")
        hit1 = hit3 = 0
        t = time.perf_counter()
        for q, want in QUERIES.items():
            res = vs.search(conn, q, limit=3, prompts=prompts)
            got = [names[r["shot_idx"]] for r in res]
            hit1 += got[:1] == [want]
            hit3 += want in got
            mark = "✓" if got[:1] == [want] else ("△" if want in got else "✗")
            print(f"{mark} {q:<8} 期望={want:<6} " + " | ".join(f"{g} {r['score']:.3f}" for g, r in zip(got, res)))
        k = len(QUERIES)
        print(f"top-1 {hit1}/{k} = {hit1 / k:.0%}，top-3 {hit3}/{k} = {hit3 / k:.0%}；每条查询平均 {(time.perf_counter() - t) / k * 1000:.0f}ms")

    conn.close()
    print(f"\n4) 模型缓存实际占用 {du(config.HF_CACHE)}")


if __name__ == "__main__":
    main()
