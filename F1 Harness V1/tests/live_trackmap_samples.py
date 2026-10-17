"""M6-3 赛道简图实测（会联网）：OSM 雪邦 + FastF1 2025 铃鹿，出样图并叠到雪邦前瞻成片画面上。

.venv/bin/python tests/live_trackmap_samples.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from hermes import graphics, trackmap  # noqa: E402

OUT = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/docs/里程碑/M6_QA和Benchmark/样图")
EP = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/productions/20261010-前瞻-马来西亚大奖赛-01")


def frame_at(t: float, out: Path) -> Path:
    subprocess.run([graphics.FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(EP / "06_合成" / "成片.mp4"),
                    "-frames:v", "1", str(out)], check=True)
    return out


def overlay(png: Path, bg: Path, out: Path) -> None:
    base = Image.open(bg).convert("RGBA").resize((1920, 1080))
    base.alpha_composite(Image.open(png).convert("RGBA"))
    base.convert("RGB").save(out)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    sep = trackmap.osm_outline("Sepang International Circuit", expected_km=5.543, clockwise=True)
    print(f"雪邦 OSM：{time.time() - t0:.1f}s 节点 {sep.get('osm_nodes')} 点 {len(sep['points'])} "
          f"长度 {sep['length_m']:.0f} m（官方 5543）顺时针 {sep['clockwise']} 方向依据 {sep['direction_from']} "
          f"起点依据 {sep['start_from']}")
    t0 = time.time()
    suz = trackmap.fastf1_outline(2025, 3, "Q")
    print(f"铃鹿 FastF1：{time.time() - t0:.1f}s 点 {len(suz['points'])} 长度 {suz['length_m']:.0f} m（官方 5807）"
          f" 弯道 {len(suz['corners'])} 旋转 {suz['rotation']} {suz.get('session')}")

    a = trackmap.render(sep, OUT / "M6-3_赛道图_雪邦_OSM.png", title="雪邦赛道", subtitle="马来西亚大奖赛",
                        length_km=5.543)
    b = trackmap.render(suz, OUT / "M6-3_赛道图_铃鹿_FastF1.png", title="铃鹿赛道", subtitle="日本大奖赛",
                        length_km=5.807)
    align = json.loads((EP / "03_配音" / "对齐.json").read_text(encoding="utf-8"))
    seg = next(s for s in align["segments"] if "布局" in s["show"] or "一圈" in s["show"])
    t = (seg["start"] + seg["end"]) / 2
    bg = frame_at(t, OUT / "_frame.png")
    overlay(a, bg, OUT / "M6-3_叠画面_雪邦.png")
    overlay(b, bg, OUT / "M6-3_叠画面_铃鹿.png")
    bg.unlink()
    print("叠画面时间", round(t, 2), seg["show"])
    if "--mov" in sys.argv:
        t0 = time.time()
        trackmap.animate(sep, OUT / "M6-3_赛道图动画_雪邦.mov", title="雪邦赛道", subtitle="马来西亚大奖赛",
                         length_km=5.543)
        print(f"动画 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
