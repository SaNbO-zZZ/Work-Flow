"""AI 封面（用户 2026-10-13 定）：文字拆分、文字核对、裁切加水印、整条流程（假模型、假搜图、假识别）、重做封面接口。"""
import asyncio
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from hermes import aicover, codex_runner, config, db, episodes, server, steps

ORIG_CONNECT = db.connect
H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}


def test_cover_texts():
    assert aicover.cover_texts("前瞻", "巴林大奖赛怎么跑到了雪邦 梅奔还能赢吗") == {
        "label": "大奖赛前瞻", "main": "巴林大奖赛怎么跑到了雪邦", "sub": "梅奔还能赢吗"}
    assert aicover.cover_texts("前瞻", "雪邦") == {"label": "大奖赛前瞻", "main": "雪邦"}


def test_check_text():
    # 本机识别会认错个别字、把标题拆成几行
    assert aicover.text_score("巴林大奖赛前膽", ["巴林大奖", "赛前瞻"]) > 0.8
    ocr = lambda paths: {str(paths[0]): [{"t": "大奖赛前瞻"}, {"t": "巴林大奖赛"}, {"t": "怎么跑到了雪邦"}]}
    ok = aicover.check_text(Path("x.png"), {"label": "大奖赛前瞻", "main": "巴林大奖赛怎么跑到了雪邦"}, ocr=ocr)
    assert ok["ok"] and ok["scores"]["main"] == 1.0
    bad = aicover.check_text(Path("x.png"), {"main": "梅奔还能赢吗"}, ocr=ocr)
    assert not bad["ok"]


def _brand():
    config.BRAND_DIR.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (400, 120), (255, 255, 255, 200)).save(config.BRAND_DIR / "watermark.png")


def test_finish(tmp_path):
    _brand()
    src = tmp_path / "a.png"
    Image.new("RGB", (1536, 1024), "blue").save(src)
    assert Image.open(aicover.finish(src, "landscape", tmp_path / "l.png")).size == (1920, 1080)
    Image.new("RGB", (1024, 1536), "blue").save(src)
    assert Image.open(aicover.finish(src, "portrait", tmp_path / "p.png")).size == (1080, 1920)


def _setup(conn, tmp_path, monkeypatch):
    _brand()
    refs = tmp_path / "refs"
    refs.mkdir()
    Image.new("RGB", (60, 40)).save(refs / "参考1.png")
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
    Image.new("RGB", (1920, 1080), "gray").save(comp / "封面_横版.png")
    return ep, comp


def _runner(calls, gen_ok=True):
    async def run(prompt, model, effort, workdir, images=None, timeout=None, ephemeral=False):
        calls.append(prompt)
        if "定封面主体" in prompt:
            return codex_runner.CodexResult("t1", '{"subjects": [{"name": "梅奔赛车", "query": "梅奔赛车正面"}]}')
        if "挑封面底图" in prompt:
            return codex_runner.CodexResult("t2", '好的 {"pick": [1, 2], "reason": "一格地点一格主角"}')
        name = prompt.rsplit("命名 ", 1)[1].split(".png")[0]
        if gen_ok:
            shape = (1536, 1024) if name.startswith("landscape") else (1024, 1536)
            Image.new("RGB", shape, "red").save(Path(workdir) / f"{name}.png")
        return codex_runner.CodexResult("t3", f"{name}.png")
    return run


def test_generate(conn, tmp_path, monkeypatch):
    ep, comp = _setup(conn, tmp_path, monkeypatch)
    search = lambda conn, q, limit: [{"asset_id": 3, "shot_idx": 878, "start_s": 6000, "end_s": 6040, "keyframe": ""},
                                     {"asset_id": 16, "shot_idx": 6, "start_s": 0, "end_s": 1, "keyframe": ""}]
    seen = {"n": 0}

    def ocr(paths):   # 横版第一次识别不对，第二次对；竖版一次就对
        seen["n"] += 1
        p = str(paths[0])
        good = [{"t": "大奖赛前瞻"}, {"t": "巴林大奖赛怎么跑到了雪邦"}]
        return {p: [{"t": "乱码"}] if "landscape_1" in p else good}

    calls = []
    info = asyncio.run(aicover.generate(conn, ep, "巴林大奖赛怎么跑到了雪邦", "简介", note="要有冲击力",
                                        runner=_runner(calls), search=search, ocr=ocr))
    assert [p["asset_id"] for p in info["picked"]] == [16, 3] and info["candidates"] == 2
    assert info["result"]["landscape"]["attempts"] == 2 and info["result"]["portrait"]["attempts"] == 1
    assert all("要有冲击力" in c for c in calls)
    assert Image.open(comp / "封面_横版.png").size == (1920, 1080)
    assert Image.open(comp / "封面_竖版.png").size == (1080, 1920)
    backup = list((comp / "封面版本").glob("*/封面_横版.png"))
    assert len(backup) == 1 and Image.open(backup[0]).getpixel((0, 0)) == (128, 128, 128)
    assert json.loads((comp / "封面说明.json").read_text(encoding="utf-8"))["reason"] == "一格地点一格主角"
    assert "横版第 2 次通过" in aicover.summary(info)
    assert conn.execute("SELECT COUNT(*) FROM usage WHERE episode_id=?", (ep["id"],)).fetchone()[0] == 5


def test_generate_fallback(conn, tmp_path, monkeypatch):
    """模型一直没出图：两个方向都保留原来的封面。"""
    ep, comp = _setup(conn, tmp_path, monkeypatch)
    info = asyncio.run(aicover.generate(conn, ep, "巴林大奖赛怎么跑到了雪邦", "", runner=_runner([], gen_ok=False),
                                        search=lambda conn, q, limit: [], ocr=lambda p: {}))
    assert not info["result"]["landscape"]["ok"] and info["result"]["portrait"]["attempts"] == 3
    assert Image.open(comp / "封面_横版.png").getpixel((0, 0)) == (128, 128, 128)
    assert not (comp / "封面_竖版.png").exists() and not (comp / "封面版本").exists()
    assert "保留原来的封面" in aicover.summary(info)


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
