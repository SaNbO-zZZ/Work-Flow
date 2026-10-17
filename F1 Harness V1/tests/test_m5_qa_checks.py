"""M5-7 硬检查：时间线规则（重复画面、A-B-A-B、跨期复用、铺满、软提醒）与成片各项（水印、BGM、峰值、规格、字幕、科普期数、译名、交付）。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from hermes import compose, config, db, facts, qa_checks as qa

FF = compose.FFMPEG
REAL_BRAND = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/brand")

pytestmark = [
    pytest.mark.skipif(not Path(FF).exists() and not shutil.which("ffmpeg"), reason="没有 ffmpeg"),
    pytest.mark.skipif(not (REAL_BRAND / "watermark.png").exists(), reason="外置硬盘没插，没有品牌文件"),
]


@pytest.fixture(autouse=True)
def brand(monkeypatch):
    monkeypatch.setattr(config, "BRAND_DIR", REAL_BRAND)


def _ep(conn, branch="前瞻") -> int:
    ts = db.now()
    return conn.execute("INSERT INTO episodes(branch, title, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (branch, "QA 测试", "合成与QA", ts, ts)).lastrowid


def _asset(conn, shots=((0, 10, 0, 0),)) -> int:
    """shots: (start, end, has_subtitles, has_logo)"""
    aid = conn.execute("INSERT INTO assets(source_type, created_at) VALUES ('测试', ?)", (db.now(),)).lastrowid
    for i, (s, e, sub, logo) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, has_subtitles, has_logo) "
                     "VALUES (?,?,?,?,?,?)", (aid, i, s, e, sub, logo))
    return aid


def _clip(aid, idx, src_in, src_out, start, end, crop=None):
    return {"asset_id": aid, "shot_idx": idx, "src_in": src_in, "src_out": src_out, "start": start, "end": end,
            "crop": crop}


def _tl(clips, duration):
    return {"fps": 30, "size": [1920, 1080], "duration": duration, "voice": "03_配音/配音.wav",
            "subtitles": "03_配音/字幕.json", "video": clips}


def _by(items, name):
    return next(i for i in items if i["name"] == name)


# ---------- 时间线 ----------

def test_timeline_clean_has_every_rule(conn):
    eid = _ep(conn)
    a, b = _asset(conn, [(0, 10, 0, 0), (10, 20, 0, 0)]), _asset(conn)
    items = qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 0, 2, 0, 2), _clip(b, 0, 0, 2, 2, 4),
                                               _clip(a, 1, 12, 14, 4, 6)], 6))
    names = [i["name"] for i in items]
    assert names == ["同一段画面两次", "同一镜头用两次", "跨期复用", "画面铺满", "关键回合画面", "事件段用了他站画面",
                     "普通比赛画面占比", "画面段缺镜头", "镜头不够长", "对象不符", "看图不通过", "镜头过短", "带字幕镜头",
                     "台标未裁"]
    assert all(i["ok"] for i in items)
    assert [i["hard"] for i in items] == [True] * 11 + [False] * 2 + [True]   # 台标未裁是硬项（2026-10-14）


def test_timeline_duplicate_and_abab(conn):
    eid = _ep(conn)
    a, b = _asset(conn, [(0, 10, 0, 0), (10, 20, 0, 0)]), _asset(conn)
    clips = [_clip(a, 0, 1, 3, 0, 2), _clip(b, 0, 0, 2, 2, 4), _clip(a, 0, 4, 6, 4, 6), _clip(b, 0, 3, 5, 6, 8),
             _clip(a, 1, 12, 14, 8, 10), _clip(a, 0, 2.5, 4.0, 10, 11.5)]
    items = qa.timeline_issues(conn, eid, _tl(clips, 11.5))
    dup = _by(items, "同一段画面两次")
    assert not dup["ok"] and "第1个镜头" in dup["detail"] and "第6个镜头" in dup["detail"]
    again = _by(items, "同一镜头用两次")
    assert not again["ok"] and "第1个镜头" in again["detail"]
    # 同一镜头不重叠的另一段也算，隔多远都不行（2026-10-13 用户定）
    aba = [_clip(a, 0, 0, 2, 0, 2), _clip(b, 0, 0, 2, 2, 4), _clip(a, 0, 5, 7, 4, 6)]
    assert not _by(qa.timeline_issues(conn, eid, _tl(aba, 6)), "同一镜头用两次")["ok"]
    c = _asset(conn)
    far = [_clip(a, 0, 0, 2, 0, 2), _clip(b, 0, 0, 2, 2, 4), _clip(a, 1, 12, 14, 4, 6), _clip(c, 0, 3, 5, 6, 8),
           _clip(a, 0, 5, 7, 8, 10)]
    assert not _by(qa.timeline_issues(conn, eid, _tl(far, 10)), "同一镜头用两次")["ok"]
    # 首尾相接（差 0.1 秒以内）不算重复；紧挨着的同一镜头算用一次
    ok = qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 0, 2, 0, 2), _clip(a, 0, 2.05, 4, 2, 3.95)], 3.95))
    assert _by(ok, "同一段画面两次")["ok"] and _by(ok, "同一镜头用两次")["ok"]


def test_timeline_cross_episode_reuse(conn):
    eid, other = _ep(conn), _ep(conn)
    a = _asset(conn)
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (other, a, 1, 4, db.now()))
    tl = _tl([_clip(a, 0, 3, 6, 0, 3)], 3)
    r = _by(qa.timeline_issues(conn, eid, tl), "跨期复用")
    assert not r["ok"] and f"第 {other} 号期" in r["detail"]
    # 待审批不算
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, cross_episode, approval, created_at) "
                 "VALUES (?,?,?,?,1,'pending',?)", (eid, a, 3, 6, db.now()))
    assert not _by(qa.timeline_issues(conn, eid, tl), "跨期复用")["ok"]
    conn.execute("UPDATE asset_uses SET approval='approved' WHERE episode_id=?", (eid,))
    assert _by(qa.timeline_issues(conn, eid, tl), "跨期复用")["ok"]
    # 没用到别的期用过的那段
    assert _by(qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 4.5, 7, 0, 2.5)], 2.5)), "跨期复用")["ok"]


def test_timeline_coverage(conn):
    eid = _ep(conn)
    a = _asset(conn, [(0, 30, 0, 0)])
    hole = qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 0, 2, 0, 2), _clip(a, 0, 5, 7, 2.5, 4.5)], 5))
    d = _by(hole, "画面铺满")
    assert not d["ok"] and "2.00–2.50s 空洞" in d["detail"] and "4.50–5.00s 空洞" in d["detail"]
    over = qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 0, 2, 0, 2), _clip(a, 0, 5, 7, 1.5, 3.5)], 3.5))
    assert "重叠" in _by(over, "画面铺满")["detail"]
    # 1 帧以内的误差不算
    tiny = qa.timeline_issues(conn, eid, _tl([_clip(a, 0, 0, 2, 0.02, 2), _clip(a, 0, 5, 7, 2.02, 4)], 4.02))
    assert _by(tiny, "画面铺满")["ok"]


def test_timeline_soft_items(conn):
    eid = _ep(conn)
    a = _asset(conn, [(0, 5, 1, 0), (5, 10, 0, 1), (10, 20, 0, 1)])
    clips = [_clip(a, 0, 1, 2, 0, 1), _clip(a, 1, 6, 8, 1, 3),
             _clip(a, 2, 12, 14, 3, 5, crop={"x": 0, "y": 100, "w": 1920, "h": 980})]
    items = qa.timeline_issues(conn, eid, _tl(clips, 5))
    assert not _by(items, "镜头过短")["ok"] and "第1个镜头" in _by(items, "镜头过短")["detail"]
    assert "第1个镜头" in _by(items, "带字幕镜头")["detail"]
    logo = _by(items, "台标未裁")
    assert not logo["ok"] and logo["hard"] and "第2个镜头" in logo["detail"] and "第3个镜头" not in logo["detail"]
    assert all(i["ok"] for i in items if i["hard"] and i["name"] != "台标未裁")


# ---------- 成片 ----------

def make_video(path, *, size=(1920, 1080), wm="ok", dur=3.0, amp=1.8, fps=30):
    W, H = size
    bg = Image.linear_gradient("L").resize(size).convert("RGB")
    if wm != "none":
        im, (x, y) = compose._watermark(size)
        if wm == "shift":
            x, y = x + 30, y + 20
        if wm == "big":
            im = im.resize((round(im.width * 1.25), round(im.height * 1.25)), Image.LANCZOS)
        bg.paste(im, (x, y), im)
    png = path.with_suffix(".png")
    bg.save(png)
    subprocess.run([FF, "-v", "error", "-nostdin", "-y", "-loop", "1", "-framerate", str(fps), "-i", png,
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", str(dur),
                    "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-af", f"volume={amp}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-r", str(fps), *compose.COLOR_TAGS,
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", path], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("qa_videos")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "BRAND_DIR", REAL_BRAND)
        return {"ok": make_video(d / "ok.mp4"), "none": make_video(d / "none.mp4", wm="none"),
                "shift": make_video(d / "shift.mp4", wm="shift"), "big": make_video(d / "big.mp4", wm="big"),
                "720p": make_video(d / "720.mp4", size=(1280, 720)), "loud": make_video(d / "loud.mp4", amp=14)}


def make_ep(conn, tmp_path, videos, *, branch="前瞻", video="ok"):
    eid = _ep(conn, branch)
    a, b = _asset(conn), _asset(conn)
    ep = tmp_path / "ep"
    for sub in ("02_文案", "03_配音", "06_合成"):
        (ep / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(videos[video], ep / "06_合成" / "成片.mp4")
    tl = _tl([_clip(a, 0, 0, 1.5, 0, 1.5), _clip(b, 0, 0, 1.5, 1.5, 3.0)], 3.0)
    if branch == "前瞻":
        tl["overlays"] = [{"path": "05_图形/赛道图.png", "start": 0.0, "end": 3.0, "kind": "赛道图"}]
    (ep / "06_合成" / "时间线.json").write_text(json.dumps(tl), encoding="utf-8")
    (ep / "06_合成" / "混音.json").write_text(json.dumps(
        {"bgm_sha256": compose.BRAND_SHA["bgm.mp3"] + "0" * 48, "bgm_gain_db": -20.5, "duck_db": [0, 3.5]}))
    cues = [{"start": 0, "end": 1.4, "text": "维斯塔潘领先1.5秒"}, {"start": 1.5, "end": 2.9, "text": "比分 2:1 继续"}]
    (ep / "03_配音" / "字幕.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    (ep / "03_配音" / "字幕.srt").write_text(compose.cues_to_srt(cues), encoding="utf-8")
    (ep / "03_配音" / "配音稿.json").write_text(json.dumps({"units": [], "warnings": []}), encoding="utf-8")
    (ep / "02_文案" / "标题和简介.txt").write_text("雪邦前瞻 维斯塔潘能否卫冕\n简介", encoding="utf-8")
    # M6-4：数字要有 [F#]、配音要记录授权音色
    facts.add(conn, eid, "F", "维斯塔潘领先优势（秒）", source="测试", value="1.5")
    (ep / "02_文案" / "逐字稿.md").write_text("维斯塔潘领先1.5秒[F1]。\n", encoding="utf-8")
    (ep / "03_配音" / "配音来源.json").write_text(json.dumps({"method": "剪映脚本", "voice": "热血解说员"},
                                                     ensure_ascii=False), encoding="utf-8")
    return eid, ep


def _patch_json(path, **kw):
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(kw)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_run_all_pass_writes_json_and_timeline(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    for kind in ("成片", "粗剪", "成片"):
        conn.execute("INSERT INTO timelines(episode_id, stage_run, kind, path, created_at) VALUES (?,1,?,?,?)",
                     (eid, kind, "x", db.now()))
    res = qa.run(conn, eid, ep)
    bad = [i for i in res["items"] if not i["ok"]]
    assert res["passed"], bad
    assert json.loads((ep / "07_QA" / "硬检查.json").read_text(encoding="utf-8")) == res
    rows = conn.execute("SELECT kind, checks FROM timelines WHERE episode_id=? ORDER BY id", (eid,)).fetchall()
    assert rows[0]["checks"] is None and rows[1]["checks"] is None
    assert json.loads(rows[2]["checks"])["passed"] is True
    names = {i["name"] for i in res["items"]}
    for n in ("水印", "BGM 文件", "BGM 电平", "真峰值", "整体响度", "画面规格", "音轨", "时长", "字幕零标点",
              "字幕时长", "同一段画面两次", "同一镜头用两次", "跨期复用", "画面铺满", "科普不写期数", "未审译名",
              "标题不把传闻写成事实", "交付齐全", "前瞻缺赛道简图"):
        assert n in names
    assert _by(res["items"], "标题不把传闻写成事实")["hard"] is True


def test_run_no_timeline_row_skips(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    assert qa.run(conn, eid, ep)["passed"]


@pytest.mark.parametrize("kind", ["none", "shift", "big"])
def test_watermark_bad(conn, tmp_path, videos, kind):
    eid, ep = make_ep(conn, tmp_path, videos, video=kind)
    res = qa.run(conn, eid, ep)
    wm = _by(res["items"], "水印")
    assert not wm["ok"] and "左上角(28,28)" in wm["detail"]
    assert not res["passed"]


def test_watermark_thresholds_margin(videos):
    ok = qa.check_watermark(videos["ok"], 1920, 1080, 3.0)
    assert ok["ok"]
    for t in ok["detail"].split("；"):
        mae = float(t.split("误差")[1].split("/")[0])
        corr = float(t.split("相关")[1])
        assert mae < qa.WM_MAE_MAX / 2 and corr > 0.97


def test_watermark_brand_sha_wrong(conn, tmp_path, videos, monkeypatch):
    fake = tmp_path / "brand"
    fake.mkdir()
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(fake / "watermark.png")
    monkeypatch.setattr(config, "BRAND_DIR", fake)
    eid, ep = make_ep(conn, tmp_path, videos)
    wm = _by(qa.run(conn, eid, ep)["items"], "水印")
    assert not wm["ok"] and "不是指定文件" in wm["detail"]


def test_bgm_report(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    rep = ep / "06_合成" / "混音.json"
    _patch_json(rep, bgm_sha256="deadbeef" * 8, bgm_gain_db=-16.0)
    res = qa.run(conn, eid, ep)
    assert not _by(res["items"], "BGM 文件")["ok"] and "deadbeefdeadbeef" in _by(res["items"], "BGM 文件")["detail"]
    assert not _by(res["items"], "BGM 电平")["ok"] and not res["passed"]
    _patch_json(rep, bgm_sha256=compose.BRAND_SHA["bgm.mp3"] + "1" * 48, bgm_gain_db=-18.0)
    res = qa.run(conn, eid, ep)
    assert _by(res["items"], "BGM 文件")["ok"] and _by(res["items"], "BGM 电平")["ok"]
    rep.unlink()
    assert not _by(qa.run(conn, eid, ep)["items"], "BGM 文件")["ok"]


def test_true_peak_over(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos, video="loud")
    res = qa.run(conn, eid, ep)
    tp = _by(res["items"], "真峰值")
    assert not tp["ok"] and "超过 -1.0" in tp["detail"] and not res["passed"]


def test_spec_wrong_resolution_and_duration(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos, video="720p")
    _patch_json(ep / "06_合成" / "时间线.json", duration=3.5,
                video=[_clip(1, 0, 0, 1.5, 0, 1.5), _clip(2, 0, 0, 2, 1.5, 3.5)])
    res = qa.run(conn, eid, ep)
    spec = _by(res["items"], "画面规格")
    assert not spec["ok"] and "1280×720" in spec["detail"]
    assert _by(res["items"], "音轨")["ok"]
    assert not _by(res["items"], "时长")["ok"]


def test_subtitle_punct(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    cues = [{"start": 0, "end": 1, "text": "圈速1:31.456"}, {"start": 1, "end": 2, "text": "领先，三秒"},
            {"start": 2, "end": 3.6, "text": "完"}]
    (ep / "03_配音" / "字幕.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    (ep / "03_配音" / "字幕.srt").unlink()
    (ep / "08_交付").mkdir()
    (ep / "08_交付" / "字幕.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n胜利!\n\n", encoding="utf-8")
    res = qa.run(conn, eid, ep)
    p = _by(res["items"], "字幕零标点")
    assert not p["ok"] and "第2条" in p["detail"] and "，" in p["detail"] and "08_交付/字幕.srt 第1条" in p["detail"]
    assert "第1条「圈速" not in p["detail"]
    late = _by(res["items"], "字幕时长")
    assert not late["ok"] and "3.60s" in late["detail"]


def test_science_episode_number(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos, branch="科普")
    assert _by(qa.run(conn, eid, ep)["items"], "科普不写期数")["ok"]
    (ep / "02_文案" / "标题和简介.txt").write_text("F1 冷知识 第七期\n第 12 期见", encoding="utf-8")
    cues = [{"start": 0, "end": 1, "text": "欢迎来到第3期"}]
    (ep / "03_配音" / "字幕.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    item = _by(qa.run(conn, eid, ep)["items"], "科普不写期数")
    assert not item["ok"] and "第七期" in item["detail"] and "第 12 期" in item["detail"] and "第3期" in item["detail"]
    # 非科普不查
    eid2, ep2 = make_ep(conn, tmp_path / "b", videos)
    (ep2 / "02_文案" / "标题和简介.txt").write_text("第3期", encoding="utf-8")
    assert _by(qa.run(conn, eid2, ep2)["items"], "科普不写期数")["ok"]


def test_unreviewed_names(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    _patch_json(ep / "03_配音" / "配音稿.json", warnings=["未审译名：Lindblad", "别的提醒"])
    res = qa.run(conn, eid, ep)
    n = _by(res["items"], "未审译名")
    assert not n["ok"] and "Lindblad" in n["detail"] and "别的提醒" not in n["detail"] and not res["passed"]


def test_rumor_hedged_title_passes(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    (ep / "02_文案" / "标题和简介.txt").write_text("据悉汉密尔顿将退役\n", encoding="utf-8")
    res = qa.run(conn, eid, ep)
    r = _by(res["items"], "标题不把传闻写成事实")
    assert r["ok"] and r["hard"] and "据悉" in r["detail"] and res["passed"]


def test_timeline_rules_in_run(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    _patch_json(ep / "06_合成" / "时间线.json", video=[_clip(1, 0, 0, 1.5, 0, 1.5), _clip(1, 0, 1, 2, 1.8, 3.0)])
    res = qa.run(conn, eid, ep)
    assert not _by(res["items"], "同一段画面两次")["ok"] and not _by(res["items"], "画面铺满")["ok"]
    assert not res["passed"]


def test_deliveries(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    dst = ep / "08_交付"
    dst.mkdir()
    for kind, name in qa.DELIVER_FILES.items():
        p = dst / name
        shutil.copyfile(ep / "03_配音" / "字幕.srt", p) if kind == "字幕" else p.write_bytes(kind.encode())
        conn.execute("INSERT INTO deliveries(episode_id, kind, path, sha256, created_at) VALUES (?,?,?,?,?)",
                     (eid, kind, str(p), compose.sha256_file(p), db.now()))
    assert _by(qa.run(conn, eid, ep)["items"], "交付齐全")["ok"]
    (dst / "竖版封面.png").unlink()
    (dst / "制作记录.md").write_text("改过", encoding="utf-8")
    d = _by(qa.run(conn, eid, ep)["items"], "交付齐全")
    assert not d["ok"] and "缺竖版封面" in d["detail"] and "制作记录 sha256" in d["detail"]


def test_missing_video(conn, tmp_path, videos):
    eid, ep = make_ep(conn, tmp_path, videos)
    (ep / "06_合成" / "成片.mp4").unlink()
    res = qa.run(conn, eid, ep)
    assert not res["passed"] and not _by(res["items"], "成片")["ok"]
