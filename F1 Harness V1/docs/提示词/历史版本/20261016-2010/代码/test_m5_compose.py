"""M5-6 合成与混音：混音响度/避让/BGM 电平、品牌文件 sha 校验、按时间线出片（图层、水印、字幕、静音）、预览尺寸、交付。"""
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import hermes
from hermes import compose, config, db, episodes

FF = compose.FFMPEG
REAL_ROOT = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1")
SR = compose.SR

pytestmark = pytest.mark.skipif(not Path(FF).exists() and not shutil.which("ffmpeg"), reason="没有 ffmpeg")


def ff(*args):
    subprocess.run([FF, "-v", "error", "-nostdin", "-y", *map(str, args)], check=True, capture_output=True)


def tone(path, freq, dur, vol=0.5, layout="mono"):
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={dur}", "-af",
       f"volume={vol},aformat=channel_layouts={layout}", path)
    return path


def voice_bursts(path, dur, on=((1.0, 3.0), (5.0, 7.0)), amp=0.3):
    """1 kHz 正弦，只在 on 区间发声，当作人声。"""
    t = np.arange(int(dur * SR)) / SR
    v = np.zeros_like(t)
    for a, b in on:
        m = (t >= a) & (t < b)
        v[m] = amp * np.sin(2 * np.pi * 1000 * t[m])
    proc = subprocess.run([FF, "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", str(path)],
                          input=v.astype(np.float32).tobytes(), capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return path


def band_db(x, freq, a, b):
    """x 在 [a,b) 秒内 freq 处的幅度（dB），用正交解调。"""
    seg = x[int(a * SR):int(b * SR)]
    t = np.arange(len(seg)) / SR
    amp = 2 * abs(np.mean(seg * np.exp(-2j * np.pi * freq * t)))
    return 20 * np.log10(amp + 1e-12)


def read_wav(path, ch=2):
    return compose._decode(Path(path), ch)


# ---------- 混音 ----------

def test_mix_loudness_duck_and_bgm_level(tmp_path):
    bgm = tone(tmp_path / "bgm.wav", 200, 12, vol=0.5, layout="stereo")
    voice = voice_bursts(tmp_path / "voice.wav", 8)
    rep = {}
    out = compose.mix(voice, 9.0, tmp_path / "mix.wav", bgm=bgm, report=rep)
    m = compose.measure(out)
    assert abs(m["duration"] - 9.0) < 0.01
    assert abs(m["integrated"] - compose.TARGET_LUFS) <= 0.5
    assert m["true_peak"] <= compose.TARGET_TP + 0.2

    x = read_wav(out)[:, 0]
    src = band_db(read_wav(bgm)[:, 0], 200, 0, 4)
    quiet = band_db(x, 200, 3.8, 4.8)          # 人声停了 0.8 秒以上，避让已恢复
    loud = band_db(x, 200, 1.5, 2.8)
    # BGM 无人声时正好 -20.5 dB（相对原文件），有人声时最多再压 3.5 dB
    assert abs(quiet - src - compose.BGM_GAIN_DB) < 0.3
    assert 3.0 < quiet - loud <= compose.DUCK_MAX_DB + 0.1
    assert band_db(x, 200, 8.2, 9.0) == pytest.approx(quiet, abs=0.3)   # 片尾不淡出
    assert 0 <= rep["duck_db"][0] and rep["duck_db"][1] <= compose.DUCK_MAX_DB


def test_duck_curve_never_exceeds_limit():
    rng = np.random.default_rng(1)
    v = rng.normal(0, 0.5, SR * 3).astype(np.float32)   # 远超阈值的人声
    red = compose.duck_curve(v)
    assert red.max() <= compose.DUCK_MAX_DB + 1e-6
    assert red[SR:2 * SR].min() > compose.DUCK_MAX_DB - 0.05
    assert compose.duck_curve(np.full(SR, 1e-4, dtype=np.float32)).max() < 0.01   # 低于阈值不压


def test_loop_crossfade_and_no_fadeout():
    L = SR * 2
    bgm = np.ones((L, 2), dtype=np.float32)
    out = compose.loop_bgm(bgm, SR * 5)
    assert out.shape == (SR * 5, 2)
    xf = int(compose.LOOP_XFADE * SR)
    seam = L - xf
    # 等功率交叉淡化：接缝中点 sin+cos=√2，接缝外为 1
    assert out[seam + xf // 2, 0] == pytest.approx(np.sqrt(2), abs=0.02)
    assert np.allclose(out[seam - 10:seam - 1], 1) and np.allclose(out[seam + xf + 1:seam + xf + 10], 1)
    assert np.allclose(out[-SR // 10:], 1)   # 末尾直接截断


def test_brand_sha_checked(tmp_path, monkeypatch):
    brand = tmp_path / "brand"
    brand.mkdir()
    tone(brand / "bgm.mp3", 300, 1)
    monkeypatch.setattr(config, "BRAND_DIR", brand)
    voice = voice_bursts(tmp_path / "v.wav", 1)
    with pytest.raises(ValueError, match="不是指定文件"):
        compose.mix(voice, 1.0, tmp_path / "m.wav")
    monkeypatch.setitem(compose.BRAND_SHA, "bgm.mp3", compose.sha256_file(brand / "bgm.mp3")[:16])
    assert compose.brand_file("bgm.mp3") == brand / "bgm.mp3"


@pytest.mark.skipif(not (REAL_ROOT / "assets/brand/bgm.mp3").exists(), reason="外置盘不在")
def test_real_brand_files_pass_sha(monkeypatch):
    monkeypatch.setattr(config, "BRAND_DIR", REAL_ROOT / "assets" / "brand")
    compose.brand_file("bgm.mp3")
    compose.brand_file("watermark.png")


# ---------- 出片 ----------

GREEN, MAGENTA, BLUE = (0, 220, 0), (255, 0, 255), (0, 0, 255)


@pytest.fixture
def episode(tmp_path, monkeypatch, conn):
    fonts = REAL_ROOT / "assets" / "fonts"
    if not (fonts / "SourceHanSansSC-Medium.otf").exists():
        pytest.skip("外置盘字体不在")
    monkeypatch.setattr(config, "FONTS_DIR", fonts)
    brand = tmp_path / "brand"
    brand.mkdir()
    Image.new("RGBA", (400, 400), (*GREEN, 255)).save(brand / "watermark.png")
    tone(brand / "bgm.mp3", 200, 6, layout="stereo")
    monkeypatch.setattr(config, "BRAND_DIR", brand)
    for name in ("watermark.png", "bgm.mp3"):
        monkeypatch.setitem(compose.BRAND_SHA, name, compose.sha256_file(brand / name)[:16])

    # 假字幕渲染：底部中间一块洋红色
    def render_all(cues, out_dir, size=(1920, 1080)):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        res = []
        for i, c in enumerate(cues):
            im = Image.new("RGBA", size, (0, 0, 0, 0))
            w, h = size
            ImageDraw.Draw(im).rectangle((w * 0.4, h * 0.85, w * 0.6, h * 0.93), fill=(*MAGENTA, 255))
            p = out_dir / f"{i:04d}.png"
            im.save(p)
            res.append({"start": c["start"], "end": c["end"], "path": str(p)})
        return res
    fake = types.ModuleType("hermes.subtitles")
    fake.render_all = render_all
    monkeypatch.setitem(sys.modules, "hermes.subtitles", fake)
    monkeypatch.setattr(hermes, "subtitles", fake, raising=False)

    lib = tmp_path / "lib"
    src = lib / "1" / "source.mp4"
    src.parent.mkdir(parents=True)
    # 320×180 彩条 3 秒，带 3 kHz 原声
    ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3", "-f", "lavfi", "-i",
       "sine=frequency=3000:sample_rate=48000:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", src)
    aid = conn.execute("INSERT INTO assets(source_type, title, source_url, local_path, credit, created_at) "
                       "VALUES ('测试','彩条','https://example.com/a',?, '画面：测试', ?)",
                       (str(src), db.now())).lastrowid

    eid = episodes.new_session(conn)
    code = episodes.start(conn, eid, branch="前瞻", gp_label="测试站", title="合成测试")
    ep_dir = config.PRODUCTIONS_DIR / code
    voice_bursts(ep_dir / "03_配音" / "配音.wav", 4, on=((0.5, 3.5),))
    (ep_dir / "03_配音" / "字幕.json").write_text(json.dumps(
        [{"start": 0.5, "end": 1.5, "text": "第一句，测试！"}, {"start": 2.5, "end": 3.0, "text": "第二句"}],
        ensure_ascii=False), encoding="utf-8")
    ov = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rectangle((860, 440, 1060, 640), fill=(*BLUE, 255))
    (ep_dir / "05_图形").mkdir(exist_ok=True)
    ov.save(ep_dir / "05_图形" / "box.png")
    timeline = {
        "fps": 30, "size": [1920, 1080], "duration": 4.0,
        "voice": "03_配音/配音.wav", "subtitles": "03_配音/字幕.json",
        "video": [
            {"asset_id": aid, "shot_idx": 0, "src_in": 0.5, "src_out": 2.5, "start": 0.0, "end": 2.0,
             "crop": {"x": 0, "y": 0, "w": 160, "h": 180}, "segment": 0, "reason": "裁左半"},
            # 源只剩 1 秒，要定格补满 2 秒
            {"asset_id": aid, "shot_idx": 1, "src_in": 2.0, "src_out": 3.0, "start": 2.0, "end": 4.0,
             "crop": None, "segment": 0, "reason": "定格"},
        ],
        "overlays": [{"path": "05_图形/box.png", "start": 1.0, "end": 2.0}],
        "credits": [{"text": "画面：测试", "start": 0.0, "end": 4.0}],
    }
    (ep_dir / "06_合成" / "时间线.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    return {"id": eid, "dir": ep_dir, "timeline": timeline, "conn": conn}


def frame_at(video, t, tmp):
    p = Path(tmp) / f"f_{t:.2f}.png"
    ff("-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", p)
    return np.asarray(Image.open(p).convert("RGB"), dtype=int)


def near(px, color, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(px, color))


def streams(video):
    out = subprocess.run([compose.FFPROBE, "-v", "error", "-show_entries",
                          "stream=codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration",
                          "-of", "json", str(video)], capture_output=True, text=True).stdout
    return json.loads(out)


def test_render_final(episode, tmp_path):
    ep_dir = episode["dir"]
    out = compose.render(ep_dir, episode["timeline"], ep_dir / "06_合成" / "成片.mp4", conn=episode["conn"])
    info = streams(out)
    v = next(s for s in info["streams"] if s.get("width"))
    a = next(s for s in info["streams"] if s.get("sample_rate"))
    assert (v["codec_name"], v["width"], v["height"], v["r_frame_rate"]) == ("h264", 1920, 1080, "30/1")
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("aac", "48000", 2)
    assert abs(float(info["format"]["duration"]) - 4.0) < 0.06
    assert not (ep_dir / "06_合成" / "_work").exists()

    f = frame_at(out, 1.2, tmp_path)
    # 水印：左上角 (28,28)-(188,188) 是绿色，外侧不是
    assert near(f[108, 108], GREEN) and near(f[40, 40], GREEN) and near(f[180, 180], GREEN)
    assert not near(f[10, 10], GREEN) and not near(f[200, 200], GREEN)
    assert near(f[960, 960], MAGENTA)            # 字幕
    assert near(f[540, 960], BLUE)               # overlay
    assert f[1040, 1700].mean() > 0              # 出处小字区域有内容
    f2 = frame_at(out, 2.2, tmp_path)
    assert not near(f2[960, 960], MAGENTA) and not near(f2[540, 960], BLUE)
    assert near(f2[108, 108], GREEN)
    f3 = frame_at(out, 3.8, tmp_path)            # 定格段仍有画面
    assert f3[300:800, 300:1600].std() > 10

    # 素材原声（3 kHz）被丢弃
    x = compose._decode(out, 1)[:, 0]
    assert band_db(x, 3000, 0.5, 3.5) < band_db(x, 1000, 0.5, 3.5) - 50


def test_render_preview(episode, tmp_path):
    ep_dir = episode["dir"]
    out = compose.render(ep_dir, episode["timeline"], ep_dir / "06_合成" / "粗剪.mp4", preview=True,
                         conn=episode["conn"])
    v = next(s for s in streams(out)["streams"] if s.get("width"))
    assert (v["width"], v["height"]) == (960, 540)
    f = frame_at(out, 0.8, tmp_path)
    assert near(f[54, 54], GREEN) and not near(f[100, 100], GREEN)   # 水印按比例缩到 80 宽、边距 14
    assert near(f[480, 480], MAGENTA)


def test_deliver(episode, tmp_path):
    conn, ep_dir = episode["conn"], episode["dir"]
    ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2", "-f", "lavfi", "-i",
       "sine=frequency=500:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", ep_dir / "06_合成" / "成片.mp4")
    for name in ("封面_横版.png", "封面_竖版.png"):
        Image.new("RGB", (64, 36), (200, 0, 0)).save(ep_dir / "06_合成" / name)
    (ep_dir / "02_文案" / "标题和简介.txt").write_text("标题\n简介", encoding="utf-8")

    res = compose.deliver(conn, episode["id"], ep_dir)
    kinds = {r["kind"] for r in res}
    assert kinds == {"成片", "字幕", "横版封面", "竖版封面", "标题和简介", "制作记录"}
    for r in res:
        assert Path(r["path"]).parent == ep_dir / "08_交付"
        assert r["sha256"] == compose.sha256_file(r["path"])
    srt = (ep_dir / "08_交付" / "字幕.srt").read_text(encoding="utf-8")
    assert "00:00:00,500 --> 00:00:01,500" in srt and "第一句 测试" in srt
    assert not any(ch in srt for ch in "，！。")
    rec = (ep_dir / "08_交付" / "制作记录.md").read_text(encoding="utf-8")
    assert "LUFS" in rec and "彩条" in rec and episodes.get(conn, episode["id"])["code"] in rec

    rows = conn.execute("SELECT kind, sha256 FROM deliveries WHERE episode_id=?", (episode["id"],)).fetchall()
    assert len(rows) == 6 and {r["kind"]: r["sha256"] for r in rows} == {r["kind"]: r["sha256"] for r in res}
    # 重做一次：行数不变，sha 跟着更新
    (ep_dir / "02_文案" / "标题和简介.txt").write_text("新标题", encoding="utf-8")
    compose.deliver(conn, episode["id"], ep_dir)
    assert conn.execute("SELECT COUNT(*) FROM deliveries WHERE episode_id=?", (episode["id"],)).fetchone()[0] == 6
    sha = conn.execute("SELECT sha256 FROM deliveries WHERE episode_id=? AND kind='标题和简介'",
                       (episode["id"],)).fetchone()[0]
    assert sha == compose.sha256_file(ep_dir / "08_交付" / "标题和简介.txt")


def test_strip_punct():
    assert compose.strip_punct("圈速1:32.5，领先0.3秒！") == "圈速1 32.5 领先0.3秒"
