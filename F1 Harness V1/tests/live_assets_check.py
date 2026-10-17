"""M4-2 实测：合成 20 秒视频 → 临时库入库 → 真实调用 GPT 逐镜头打标（台标/字幕只放在个别镜头）。只碰临时目录，不碰正式库。"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="hermes-live-assets-"))
os.environ["HERMES_CORE"] = str(_tmp / "core")
os.environ["HERMES_ROOT"] = str(_tmp / "root")
os.environ["HERMES_JOBS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from hermes import assets, config, db, glossary  # noqa: E402

FONT_SRC = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/fonts/SourceHanSansSC-Bold.otf")
SEG = 4

W, H = 1280, 720
# (lavfi 源, [(文字, 字号, 颜色, 位置, 底框颜色)])；#1 右上角假台标，#2 底部字幕，#4 左下角 UP 主水印
SEGMENTS = [
    ("color=c=0x0b2a5b", [("排位赛 Q3", 110, "white", "center", None), ("铃鹿赛道", 60, "yellow", "below", None)]),
    ("testsrc2", [("维修区", 120, "white", "center", (0, 0, 0, 150)), ("某某体育", 40, "white", "topright", (200, 0, 0, 210))]),
    ("mandelbrot", [("赛后采访", 100, "white", "center", None),
                    ("今天的赛车非常难开 我们还要继续努力", 44, "white", "bottom", (0, 0, 0, 130))]),
    ("color=c=0x7a0000", [("正赛 第1圈", 110, "white", "center", None)]),
    ("smptebars", [("FIA 新闻发布会", 90, "black", "center", (255, 255, 255, 230)),
                   ("@赛车小王", 34, (255, 255, 255, 200), "bottomleft", (0, 0, 0, 110))]),
]
EXPECT_LOGO, EXPECT_SUBS = {1, 4}, {2}


def _overlay(items, out: Path) -> Path:
    """本机 ffmpeg 没编 freetype（无 drawtext），文字用 Pillow 画成透明 PNG 再 overlay。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for text, size, color, pos, box in items:
        font = ImageFont.truetype(str(FONT_SRC), size)
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        tw, th = r - l, b - t
        x, y = {"center": ((W - tw) // 2, (H - th) // 2), "below": ((W - tw) // 2, H // 2 + 90),
                "topright": (W - tw - 40, 36), "bottom": ((W - tw) // 2, H - th - 60),
                "bottomleft": (40, H - th - 40)}[pos]
        if box:
            d.rectangle((x - 12, y - 10, x + tw + 12, y + th + 14), fill=box)
        d.text((x - l, y - t), text, font=font, fill=color)
    img.save(out)
    return out


def make_video(out: Path) -> Path:
    n = len(SEGMENTS)
    args = ["ffmpeg", "-v", "error", "-y"]
    for src, _ in SEGMENTS:
        sep = ":" if "=" in src else "="
        # mandelbrot 没有 d 参数，统一用输入端 -t 限时
        args += ["-t", str(SEG), "-f", "lavfi", "-i", f"{src}{sep}s={W}x{H}:r=25"]
    for i, (_, items) in enumerate(SEGMENTS):
        args += ["-i", str(_overlay(items, out.parent / f"text_{i}.png"))]
    args += ["-f", "lavfi", "-i", f"sine=f=330:d={SEG * n}"]
    chains = [f"[{i}:v][{n + i}:v]overlay=0:0,format=yuv420p[v{i}]" for i in range(n)]
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    args += ["-filter_complex", ";".join(chains + [concat]), "-map", "[v]", "-map", f"{2 * n}:a",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)]
    subprocess.run(args, check=True)
    return out


async def main() -> None:
    work = _tmp / "work"
    work.mkdir(parents=True)
    video = make_video(work / "synthetic.mp4")
    print("临时目录：", _tmp)
    print("合成视频探测：", assets.probe(video))

    conn = db.connect()
    for kind, names in glossary.BUILTIN.items():
        for orig, zh in names.items():
            glossary.add_manual(conn, kind, orig, zh)

    t0 = time.monotonic()
    aid = assets.ingest(conn, video, source_type="脚本", title="合成测试素材：日本站排位与维修区",
                        uploader="测试频道", credit="画面：测试频道")
    print(f"\n入库 id={aid}，耗时 {time.monotonic() - t0:.2f} 秒")
    for s in conn.execute("SELECT idx, start_s, end_s FROM asset_shots WHERE asset_id=? ORDER BY idx", (aid,)):
        print(f"  镜头 #{s['idx']}: {s['start_s']:.2f} – {s['end_s']:.2f}")
    folder = config.LIBRARY_DIR / str(aid)
    print("生成文件：")
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(folder)}  {p.stat().st_size // 1024} KB")
    sheet = Path(assets.card(conn, aid)["thumbs"]["sheets"][0])
    shutil.copyfile(sheet, "/tmp/m4-assets-sheet.jpg")
    print("拼图副本：/tmp/m4-assets-sheet.jpg")

    t0 = time.monotonic()
    out = await assets.tag_asset(conn, aid)
    print(f"\nGPT 打标耗时 {time.monotonic() - t0:.1f} 秒，{out['batches']} 批，结果：")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("\n逐镜头（库里读回）：")
    ok = True
    for s in assets.card(conn, aid)["shots"]:
        want = (s["idx"] in EXPECT_SUBS, s["idx"] in EXPECT_LOGO)
        hit = want == (s["has_subtitles"], s["has_logo"])
        ok &= hit
        print(f"  #{s['idx']}: 字幕={s['has_subtitles']} 台标={s['has_logo']} 说明={s['logo_note']!r}"
              f"  期望 字幕={want[0]} 台标={want[1]}  {'✓' if hit else '✗'}")
    print("逐镜头是否全对：", ok)
    print("全片汇总：字幕", out["has_subtitles"], "；台标", out["has_logo"], "；说明：", out["logo_note"])
    print("干净镜头（无台标）：", [s["idx"] for s in assets.clean_shots(conn, aid)])
    print("干净镜头（无台标无字幕）：", [s["idx"] for s in assets.clean_shots(conn, aid, allow_subtitles=False)])
    for u in conn.execute("SELECT * FROM usage ORDER BY id"):
        print("token 用量：", {k: u[k] for k in ("model", "effort", "input_tokens", "cached_tokens",
                                               "output_tokens", "reasoning_tokens")})
    print("按标签查 '维修区'：", [a["id"] for a in assets.list_assets(conn, tag="维修区")])


if __name__ == "__main__":
    asyncio.run(main())
