"""M4-6 图形样例：跑一次，在 docs/里程碑/M4_素材和图形/图形样例/ 生成全部样例和总览图。

用法：cd /Volumes/HermesCore && .venv/bin/python tests/live_graphics_samples.py [--resnap]
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from hermes import config, glossary, graphics as g  # noqa: E402

OUT = config.DOCS_DIR / "里程碑" / "M4_素材和图形" / "图形样例"
WEBSNAP = config.CORE / "tools" / "websnap"
FIA_URL = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14"
SEASON, ROUND = 2026, 15


def step(msg: str) -> None:
    print(f"== {msg}", flush=True)


def sample_screenshot() -> Path:
    step("1 截图叠层")
    raw = OUT / "1_网页截图_原图.png"
    if not raw.exists() or "--resnap" in sys.argv:
        # 1280×1300 视口（Retina 下 2560×2600），比 800 高是为了让文件列表露出在 cookie 横幅上方
        subprocess.run([str(WEBSNAP), FIA_URL, str(raw), "1280", "1300", "0"], check=True, timeout=120)
    # 只裁文件标题列（不要右侧发布时间），1080p 下字才够大；Retina 像素坐标，页面有新文件时要重新核对
    crop = OUT / "1_网页截图_裁切.png"
    Image.open(raw).crop((40, 956, 1360, 1880)).save(crop)
    png = g.screenshot_overlay(
        crop, OUT / "1_截图叠层.png",
        highlights=[(110, 630, 900, 692), (110, 750, 690, 812)],  # Doc 68 黄旗超车、Doc 67 15 号弯事故
        underline=[(118, 538, 332)],  # Doc 71 正式成绩
        credit="来源：FIA 官网")
    g.preview_mp4(png, OUT / "1_截图叠层_预览.mp4", seconds=3)
    return png


def ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def zh_driver(conn, full_name: str, fallback: str) -> str:
    return glossary.zh(conn, "车手", full_name) or fallback


def sample_chart(conn) -> Path:
    step("2 数据图")
    s = g._load_session(SEASON, ROUND, "R", laps=False)
    top3 = list(s.results.sort_values("Position")["Abbreviation"][:3])
    data = g.laps_from_fastf1(SEASON, ROUND, "R", top3)
    for d in data["drivers"]:
        d["name"] = zh_driver(conn, d["full_name"], d["code"])
    gp = glossary.zh(conn, "大奖赛", data["event"]) or data["event"]
    png = g.data_chart("laps", data, OUT / "2_数据图_前三圈速.png", title=f"{gp} 前三名每圈用时", credit="来源：FastF1")
    g.preview_mp4(png, OUT / "2_数据图_预览.mp4", seconds=3)
    return png


def _gap(row) -> str:
    if row["Position"] == 1:
        t = row["Time"].total_seconds()
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{sec:06.3f}"
    if row["Time"] == row["Time"]:  # 非 NaT
        return f"+{row['Time'].total_seconds():.3f}"
    st = str(row["Status"])
    return st.replace(" Laps", "圈").replace(" Lap", "圈") if st.startswith("+") else st


def sample_tower(conn) -> Path:
    step("3 排名塔")
    s = g._load_session(SEASON, ROUND, "R", laps=False)
    res = s.results.sort_values("Position").head(10)
    rows = [{"pos": int(r["Position"]), "name": zh_driver(conn, r["FullName"], r["Abbreviation"]),
             "team": r["TeamName"], "gap": _gap(r)} for _, r in res.iterrows()]
    gp = glossary.zh(conn, "大奖赛", s.event["EventName"]) or s.event["EventName"]
    still = OUT / "3_排名塔_静帧.png"
    mov = g.ranking_tower(rows, OUT / "3_排名塔.mov", seconds=4, title=f"{gp} 正赛", still=still)
    g.preview_mp4(mov, OUT / "3_排名塔_预览.mp4", seconds=4)
    return still


def sample_title() -> Path:
    step("4 花字标题卡")
    png = g.title_card("红牛巴库双登台", OUT / "4_花字标题卡.png", sub="阿塞拜疆大奖赛复盘")
    mov = g.title_card("红牛巴库双登台", OUT / "4_花字标题卡.mov", sub="阿塞拜疆大奖赛复盘", seconds=2)
    g.preview_mp4(mov, OUT / "4_花字标题卡_预览.mp4", seconds=2)
    return png


def overview(items: list[tuple[str, Path]]) -> Path:
    """每张静帧叠在默认背景 + 水印上，2×2 拼图，模拟成片里的样子。"""
    step("5 总览图")
    wm = Image.open(config.BRAND_DIR / "watermark.png").convert("RGBA").resize((160, 160), Image.LANCZOS)
    tw, th, gap, head = 960, 540, 16, 56
    sheet = Image.new("RGB", (tw * 2 + gap * 3, (th + head) * 2 + gap * 3), (12, 13, 16))
    d = ImageDraw.Draw(sheet)
    for i, (label, path) in enumerate(items):
        frame = g._default_background((1920, 1080)).convert("RGBA")
        frame.alpha_composite(Image.open(path).convert("RGBA"))
        frame.alpha_composite(wm, (g.MARGIN, g.MARGIN))
        x = gap + (i % 2) * (tw + gap)
        y = gap + (i // 2) * (th + head + gap)
        d.text((x, y + head / 2), label, font=g.font("Bold", 30), fill=(235, 238, 242), anchor="lm")
        sheet.paste(frame.convert("RGB").resize((tw, th), Image.LANCZOS), (x, y + head))
    out = OUT / "总览.jpg"
    sheet.save(out, quality=92)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = ro_conn()
    items = [("1 网页截图叠层 + 黄色马克笔", sample_screenshot()),
             ("2 FastF1 数据图", sample_chart(conn)),
             ("3 排名塔（静帧）", sample_tower(conn)),
             ("4 花字标题卡", sample_title())]
    print(overview(items))
    subprocess.run(["find", str(OUT), "-name", "._*", "-delete"], check=False)
    for p in sorted(OUT.iterdir()):
        print(f"{p.name}\t{p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
