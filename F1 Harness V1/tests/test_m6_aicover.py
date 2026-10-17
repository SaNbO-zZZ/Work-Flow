"""AI 封面（用户 2026-10-16 改定）：米白拼贴排版、模板封面、封面文字、整条流程（假模型、假搜图）、重做封面接口。"""
import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hermes import aicover, codex_runner, collage, config, db, episodes, server, steps

ORIG_CONNECT = db.connect
H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}
REAL_ROOT = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1")


@pytest.fixture
def real_assets(monkeypatch):
    fonts, brand = REAL_ROOT / "assets" / "fonts", REAL_ROOT / "assets" / "brand"
    if not (fonts / "SourceHanSansSC-Heavy.otf").exists() or not (brand / "watermark.png").exists():
        pytest.skip("外置盘字体或水印不在")
    monkeypatch.setattr(config, "FONTS_DIR", fonts)
    monkeypatch.setattr(config, "BRAND_DIR", brand)


def test_copy_from_title():
    c = collage.copy_from_title("资讯", "红牛官宣梅基斯续约 留到2031", "红牛官宣", date="10月15日")
    assert c.label == "F1资讯 / 红牛官宣" and c.lines == ["红牛官宣梅基斯续约"] and c.sub == ["留到2031"]
    assert c.team == "红牛" and c.summary is None and c.date == "10月15日"
    c = collage.copy_from_title("前瞻", "小Kimi首个冠军赛点丨红牛冲连胜 梅奔调升级", "美国大奖赛")
    assert c.label.endswith(" F1 · 美国前瞻") and c.lines == ["小Kimi首个冠军赛点"]
    assert c.summary == {"head": "红牛冲连胜 · 梅奔调升级", "points": []} and c.sub == []
    assert collage.team_of("小红牛新车") == "Racing Bulls" and collage.team_of("梅奔升级") == "梅赛德斯"
    assert collage.team_of("雪邦") is None
    assert collage.team_of("拉塞尔滨海湾称王 诺里斯止步第三") == "梅赛德斯"   # 先提到的算
    assert collage.team_of("诺里斯追拉塞尔") == "迈凯伦"


def _bg(o):
    return Image.new("RGB", collage.SIZES[o], collage.PAPER)


def test_compose_layout(real_assets):
    news = collage.Copy(label="F1资讯 / 红牛官宣", lines=["梅基斯", "留到2031！"], accent=1, sub=["红牛CEO兼领队续约"],
                        date="10月15日", team="红牛", branch="资讯")
    pre = collage.Copy(label="2026 F1 · 奥斯汀前瞻", lines=["小Kimi", "首个冠军赛点"], accent=1,
                       summary={"head": "红牛冲连胜 · 梅奔调升级", "points": ["赛道特点", "轮胎选择", "各队近况"]},
                       team="梅赛德斯", branch="前瞻")
    for o in ("landscape", "portrait"):
        L, (W, H_) = collage.LAYOUT[o], collage.SIZES[o]
        img, meta = collage.compose(news, _bg(o), o)
        assert img.size == (W, H_) and meta["footer_top"] == H_ - L["bar"]
        items = {i["kind"]: i for i in meta["items"]}
        assert [i["text"] for i in meta["items"] if i["kind"] == "title"] == ["梅基斯", "留到2031！"]
        titles = [i for i in meta["items"] if i["kind"] == "title"]
        assert titles[1]["fill"] == list(collage.accent_of("红牛")) and titles[0]["fill"] == list(collage.INK)
        assert titles[1]["size"] < titles[0]["size"]          # 第二行比首行长，字号降一档
        assert items["channel"]["text"].startswith("ALPHA PULSE F1") and "10月15日" in items["channel"]["text"]
        logo = items["logo"]["bbox"]
        assert logo[2] == W - 40 and logo[1] == 40          # 右上角圆标
        for i in meta["items"]:
            if i["kind"] in ("label", "title", "subtitle"):
                x0, y0, x1, y1 = i["bbox"]
                assert x1 <= L["x"] + L["col"] + 8 and y1 < meta["footer_top"] and x1 < logo[0], i
                if o == "portrait":
                    assert y1 <= L["text_bottom"] + 10, i
        _, meta = collage.compose(pre, _bg(o), o)
        kinds = [i["kind"] for i in meta["items"]]
        assert meta["footer_top"] == H_ - L["panel"] and "summary_head" in kinds and "channel" not in kinds
        assert "summary_points" in kinds


def test_render_template(real_assets, tmp_path):
    frame = tmp_path / "f.png"
    Image.new("RGB", (1920, 1080), (30, 60, 200)).save(frame)
    c = collage.copy_from_title("复盘", "拉塞尔滨海湾称王 诺里斯止步第三", "新加坡大奖赛")
    for o in ("landscape", "portrait"):
        out = collage.render_template(c, frame, o, tmp_path / f"{o}.png")
        im = Image.open(out)
        assert im.size == collage.SIZES[o]
        meta = json.loads(im.text["hermes_cover"])
        assert meta["layout"] == "米白拼贴" and meta["background"] == "模板"
        assert im.convert("RGB").getpixel((40, 300)) != (30, 60, 200)    # 文字区是纸面，不是画面
    # 同一份文字出的底图固定
    a = collage.template_background(c, Image.open(frame), "landscape")
    b = collage.template_background(c, Image.open(frame), "landscape")
    assert a.tobytes() == b.tobytes()


def test_make_copy():
    data = {"topic": "红牛官宣", "lines": ["梅基斯", "留到2031！"], "accent": 5, "sub": ["红牛CEO兼领队续约"],
            "summary": {"head": "x", "points": []}, "team": "红牛", "subjects": []}
    c = aicover.make_copy("资讯", None, "梅基斯续约", data)
    assert c.label == "F1资讯 / 红牛官宣" and c.accent == -1 and c.summary is None and c.team == "红牛"
    c = aicover.make_copy("前瞻", "美国大奖赛", "小Kimi首个冠军赛点 红牛冲连胜", {"lines": [], "team": "外星队"})
    assert c.lines == ["小Kimi首个冠军赛点"] and c.summary["head"] == "红牛冲连胜"
    c = aicover.make_copy("前瞻", "美国大奖赛", "t", {"topic": "奥斯汀前瞻", "lines": ["小Kimi", "首个冠军赛点"],
                                                   "summary": {"head": "红牛冲连胜 · 梅奔调升级"}, "team": "梅赛德斯"})
    assert c.label.endswith("奥斯汀前瞻") and c.summary["head"] == "红牛冲连胜 · 梅奔调升级" and c.team == "梅赛德斯"


def _setup(conn, tmp_path, monkeypatch):
    refs = tmp_path / "refs"
    refs.mkdir()
    for n in ("横版-资讯-1.jpg", "横版-概要.jpg", "竖版-资讯-1.jpg", "竖版-概要.jpg"):
        Image.new("RGB", (60, 40)).save(refs / n)
    monkeypatch.setattr(aicover, "REF_DIR", refs)
    for a in (16, 3):
        (config.LIBRARY_DIR / str(a)).mkdir(parents=True, exist_ok=True)
        (config.LIBRARY_DIR / str(a) / "source.mp4").write_bytes(b"v")
    monkeypatch.setattr(aicover, "grab", lambda item, out, width=None: (Image.new("RGB", (320, 180)).save(out), out)[1])
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="巴林大奖赛", title="t")
    ep = episodes.get(conn, eid)
    comp = Path(ep["folder"]) / "06_合成"
    shutil.rmtree(comp, ignore_errors=True)   # 各测试的库是新的，期文件夹可能重名
    comp.mkdir(parents=True)
    (comp / "时间线.json").write_text(json.dumps({"video": [
        {"asset_id": 16, "shot_idx": 6, "src_in": 10, "src_out": 14},
        {"asset_id": 16, "shot_idx": 6, "src_in": 10, "src_out": 14},
        {"asset_id": 99, "shot_idx": 1, "src_in": 0, "src_out": 2}]}), encoding="utf-8")
    Image.new("RGB", (1920, 1440), "gray").save(comp / "封面_横版.png")
    return ep, comp


COPY = {"topic": "雪邦前瞻", "lines": ["拉塞尔", "雪邦能赢吗"], "accent": 1, "sub": [],
        "summary": {"head": "梅奔升级 · 红牛追分", "points": ["赛道特点", "轮胎选择"]}, "team": "梅赛德斯",
        "subjects": [{"name": "拉塞尔", "kind": "person", "query": "拉塞尔 正脸特写"}], "scene": "雪邦看台"}


def _runner(calls, gen_ok=True, refs='{"拉塞尔": 2}', check=None):
    async def run(prompt, model, effort, workdir, images=None, timeout=None, ephemeral=False):
        calls.append((prompt, images))
        if "写封面文字" in prompt:
            return codex_runner.CodexResult("t1", json.dumps(COPY, ensure_ascii=False))
        if "挑参考帧" in prompt:
            return codex_runner.CodexResult("t2", f'好的 {{"refs": {refs}, "scene": 1, "reason": "2 号是拉塞尔正脸"}}')
        if "你在验收" in prompt:
            return codex_runner.CodexResult("t4", json.dumps(check(images[0]), ensure_ascii=False))
        name = prompt.rsplit("命名 ", 1)[1].split(".png")[0]
        if gen_ok:
            shape = (1448, 1086) if name.startswith("landscape") else (1086, 1448)
            Image.new("RGB", shape, collage.PAPER).save(Path(workdir) / f"{name}.png")
        return codex_runner.CodexResult("t3", f"{name}.png")
    return run


def test_generate(conn, tmp_path, monkeypatch, real_assets):
    ep, comp = _setup(conn, tmp_path, monkeypatch)
    threads = []

    def search(c, q, limit):
        # 搜图在线程里跑，用的必须是线程自己开的连接（原来共用主线程连接会报 sqlite 跨线程错）
        threads.append(threading.get_ident())
        assert c is not conn and c.execute("SELECT 1").fetchone()[0] == 1
        return [{"asset_id": 3, "shot_idx": 878, "start_s": 6000, "end_s": 6040, "keyframe": ""},
                {"asset_id": 16, "shot_idx": 6, "start_s": 0, "end_s": 1, "keyframe": ""}]

    def check(path):   # 横版第一次说人不像，第二次过；竖版一次过
        if "landscape_1" in str(path):
            return {"ok": False, "why": "脸不像参考帧"}
        return {"ok": True, "why": ""}

    calls = []
    info = asyncio.run(aicover.generate(conn, ep, "拉塞尔雪邦能赢吗", "简介", note="人要大",
                                        runner=_runner(calls, check=check), search=search))
    assert threads and threading.get_ident() not in threads
    assert [p["name"] for p in info["picked"]] == ["拉塞尔", "场景"] and info["picked"][0]["asset_id"] == 3
    assert info["result"]["landscape"]["attempts"] == 2 and info["result"]["portrait"]["attempts"] == 1
    assert info["copy"]["lines"] == ["拉塞尔", "雪邦能赢吗"] and info["copy"]["label"].endswith("雪邦前瞻")
    assert all("人要大" in c for c, _ in calls if "你在验收" not in c)
    gens = [(c, im) for c, im in calls if "命名 landscape" in c]
    assert "脸不像参考帧" in gens[1][0] and "脸不像参考帧" not in gens[0][0]
    assert [Path(p).name for p in gens[0][1]] == ["参考_拉塞尔.png", "参考_场景.png", "横版-概要.jpg"]
    assert "NO TEXT" in gens[0][0] and "x=76%" in gens[0][0]
    land, port = Image.open(comp / "封面_横版.png"), Image.open(comp / "封面_竖版.png")
    assert land.size == (1920, 1440) and port.size == (1440, 1920)
    assert json.loads(land.text["hermes_cover"])["background"] == "AI"
    backup = list((comp / "封面版本").glob("*/封面_横版.png"))
    assert len(backup) == 1 and Image.open(backup[0]).getpixel((0, 0)) == (128, 128, 128)
    assert json.loads((comp / "封面说明.json").read_text(encoding="utf-8"))["reason"] == "2 号是拉塞尔正脸"
    assert "横版第 2 次通过" in aicover.summary(info) and "拉塞尔 / 雪邦能赢吗" in aicover.summary(info)
    # 文字 1 + 挑帧 1 + 横版画 2、验 2 + 竖版画 1、验 1
    assert conn.execute("SELECT COUNT(*) FROM usage WHERE episode_id=?", (ep["id"],)).fetchone()[0] == 8


def test_generate_fallback(conn, tmp_path, monkeypatch, real_assets):
    """模型一直没出图：两个方向都保留原来的封面。"""
    ep, comp = _setup(conn, tmp_path, monkeypatch)
    info = asyncio.run(aicover.generate(conn, ep, "拉塞尔雪邦能赢吗", "", runner=_runner([], gen_ok=False, refs='{"拉塞尔": 1}'),
                                        search=lambda conn, q, limit: []))
    assert not info["result"]["landscape"]["ok"] and info["result"]["portrait"]["attempts"] == 3
    assert Image.open(comp / "封面_横版.png").getpixel((0, 0)) == (128, 128, 128)
    assert not (comp / "封面_竖版.png").exists() and not (comp / "封面版本").exists()
    assert "保留原来的封面" in aicover.summary(info)


def test_generate_no_face(conn, tmp_path, monkeypatch, real_assets):
    """拼图里找不到主角清楚的脸：不硬画，报错留模板封面。"""
    ep, _ = _setup(conn, tmp_path, monkeypatch)
    with pytest.raises(aicover.CoverError, match="没找到主角"):
        asyncio.run(aicover.generate(conn, ep, "拉塞尔雪邦能赢吗", "", runner=_runner([], refs='{"拉塞尔": null}'),
                                     search=lambda conn, q, limit: []))


def test_ai_cover_failure_does_not_block(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="巴林大奖赛", title="t")
    ep = episodes.get(conn, eid)

    async def boom(*a, **k):
        raise aicover.CoverError("找不到候选画面")
    monkeypatch.setattr(aicover, "generate", boom)
    assert asyncio.run(steps.ai_cover(ep)) == {"ok": False, "error": "找不到候选画面"}
    last = conn.execute("SELECT content FROM messages WHERE episode_id=? ORDER BY id DESC", (eid,)).fetchone()[0]
    assert "重做封面" in last
    conn.execute("INSERT INTO settings(key, value) VALUES ('cover.ai', '0')")
    assert asyncio.run(steps.ai_cover(ep))["skipped"]


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_cover_api(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    got, delivered = [], []

    async def fake_ai_cover(ep, note=None):
        got.append(note)
        return {"ok": True}
    monkeypatch.setattr(server.steps, "ai_cover", fake_ai_cover)
    monkeypatch.setattr(server.compose, "deliver", lambda conn, eid, folder: delivered.append(eid) or [])
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="巴林大奖赛", title="t")
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.post(f"/api/episodes/{eid}/cover", headers=H).status_code == 409
        conn.execute("UPDATE episodes SET state='待发布' WHERE id=?", (eid,))
        r = c.post(f"/api/episodes/{eid}/cover", headers=H, json={"note": " 换成整车 "})
        assert r.status_code == 202
        for _ in range(50):
            if delivered:
                break
            c.get(f"/api/episodes/{eid}/review", headers=H)
        assert got == ["换成整车"] and delivered == [eid]
        assert c.get(f"/api/episodes/{eid}/review", headers=H).json()["cover_running"] is False
