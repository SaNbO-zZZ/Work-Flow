"""M5-8 流水线执行器：稍后再做、剪映三级降级、配音后处理、素材清单、审核页文件白名单。"""
import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from hermes import db, episodes, jianying, pipeline, server, steps
from hermes.scheduler import Scheduler, StepDeferred, StepNotReady

ORIG_CONNECT = db.connect
SCRIPT = "# 雪邦前瞻\n\n马来西亚站回来了。倍耐力带来2套新配方，比赛有56圈。\n"


@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    from pathlib import Path
    (Path(row["folder"]) / "02_文案" / "逐字稿.md").write_text(SCRIPT, encoding="utf-8")
    return row


def _task(conn, ep):
    return {"id": 0, "step": "剪映生成配音", "episode_id": ep["id"]}


def _msgs(conn, eid):
    return [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=? AND role='system'", (eid,))]


# ---------- 调度器：稍后再做 ----------

def test_step_deferred_requeues_without_penalty(conn, db_path):
    calls = {"n": 0}

    async def later_then_ok(task, ep):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise StepDeferred(0, "等一下")
        return {}

    async def ok(task, ep):
        return {}

    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="巴林大奖赛", title="t")
    sched = Scheduler(lambda: ORIG_CONNECT(db_path), {"*": ok, "D1资料与事实": later_then_ok},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    async def drive():
        for _ in range(300):
            await sched.tick()
            await asyncio.sleep(0.001)
            if conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0] == "G0待审":
                return
        raise AssertionError("没到 G0")
    asyncio.run(drive())
    assert calls["n"] == 5
    assert conn.execute("SELECT attempts FROM tasks WHERE episode_id=? AND step='D1资料与事实'", (eid,)).fetchone()[0] == 1


def test_step_deferred_sets_not_before(conn, db_path):
    async def later(task, ep):
        raise StepDeferred(3600, "稍后")

    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="巴林大奖赛", title="t")
    sched = Scheduler(lambda: ORIG_CONNECT(db_path), {"*": later}, tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    async def run():
        await sched.tick()
        for _ in range(50):
            await asyncio.sleep(0.001)
            if not sched.running:
                break
    asyncio.run(run())
    rows = conn.execute("SELECT state, not_before, error FROM tasks WHERE episode_id=?", (eid,)).fetchall()
    assert all(r["state"] == "queued" for r in rows)
    assert any(r["not_before"] and r["error"] == "稍后" for r in rows)
    assert conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0] in pipeline.WORK_STAGES


# ---------- 剪映生成配音 ----------

def test_voice_script_ok(conn, ep, monkeypatch):
    async def yes(title):
        return True

    def fake_generate(text, out_dir, shots_dir, *, voice):
        assert "两套" in text and voice == "热血解说员"
        out_dir.mkdir(parents=True)
        (out_dir / "a.wav").write_bytes(b"x")
        return {"draft": "草稿1", "files": [out_dir / "a.wav"]}

    monkeypatch.setattr(steps, "_ask_start", yes)
    monkeypatch.setattr(jianying, "generate", fake_generate)
    info = asyncio.run(steps.voice_generate(_task(conn, ep), ep))
    assert info["method"] == "剪映脚本" and info["draft"] == "草稿1"
    d = steps._dir(ep) / "03_配音"
    assert json.loads((d / "配音稿.json").read_text())["units"]
    assert json.loads((d / steps.SOURCE).read_text())["stage_run"] == ep["stage_run"]


def test_voice_later(conn, ep, monkeypatch):
    async def no(title):
        return False

    monkeypatch.setattr(steps, "_ask_start", no)
    with pytest.raises(StepDeferred) as e:
        asyncio.run(steps.voice_generate(_task(conn, ep), ep))
    assert e.value.seconds == steps.LATER_SECONDS


def test_voice_falls_back_to_codex(conn, ep, monkeypatch):
    async def yes(title):
        return True

    def broken(*a, **k):
        raise jianying.UIError("没找到按钮")

    async def codex(conn_, ep_, task, text, voice, raw, logs):
        raw.mkdir(parents=True)
        (raw / "b.wav").write_bytes(b"x")
        return "草稿2"

    monkeypatch.setattr(steps, "_ask_start", yes)
    monkeypatch.setattr(jianying, "generate", broken)
    monkeypatch.setattr(steps, "_by_codex", codex)
    info = asyncio.run(steps.voice_generate(_task(conn, ep), ep))
    assert (info["method"], info["draft"], info["files"]) == ("Codex", "草稿2", 1)
    assert any("改让 Codex" in m for m in _msgs(conn, ep["id"]))


def test_voice_falls_back_to_manual_then_picks_up(conn, ep, monkeypatch, tmp_path):
    async def yes(title):
        return True

    def broken(*a, **k):
        raise jianying.UIError("没找到按钮")

    async def codex_broken(*a, **k):
        raise RuntimeError("Codex 也不行")

    sent = []
    monkeypatch.setattr(steps, "_ask_start", yes)
    monkeypatch.setattr(jianying, "generate", broken)
    monkeypatch.setattr(steps, "_by_codex", codex_broken)
    monkeypatch.setattr(jianying, "_drafts", lambda: {"旧草稿"})
    monkeypatch.setattr(steps, "notify", lambda *a: sent.append(a))
    with pytest.raises(StepDeferred):
        asyncio.run(steps.voice_generate(_task(conn, ep), ep))
    d = steps._dir(ep) / "03_配音"
    src = json.loads((d / steps.SOURCE).read_text())
    assert src["method"] == "手动" and src["before"] == ["旧草稿"]
    assert sent and any("手动配" in m for m in _msgs(conn, ep["id"]))

    # 还没生成好：继续等
    monkeypatch.setattr(steps, "_new_audio_drafts", lambda before, since=None: [])
    with pytest.raises(StepDeferred):
        asyncio.run(steps.voice_generate(_task(conn, ep), ep))

    # 用户在剪映里配好了：自动发现、拷进 raw
    draft = tmp_path / "新草稿"
    (draft / "textReading").mkdir(parents=True)
    (draft / "textReading" / "c.wav").write_bytes(b"x")
    monkeypatch.setattr(steps, "_new_audio_drafts", lambda before, since=None: [draft])
    monkeypatch.setattr(steps, "_settled", lambda dr: True)
    info = asyncio.run(steps.voice_generate(_task(conn, ep), ep))
    assert (info["method"], info["draft"], info["files"]) == ("手动", "新草稿", 1)
    assert (d / "raw" / "c.wav").exists()


def test_new_audio_drafts_since(tmp_path, monkeypatch):
    # 脚本建好的草稿（在 before 里）里后来才朗读出配音，也要认出来
    monkeypatch.setattr(jianying, "DRAFTS_ROOT", tmp_path)
    for name in ("旧草稿", "脚本草稿", "新草稿", "空草稿"):
        (tmp_path / name).mkdir()
    for name in ("旧草稿", "脚本草稿", "新草稿"):
        (tmp_path / name / "textReading").mkdir()
        (tmp_path / name / "textReading" / "a.wav").write_bytes(b"x")
    old = time.time() - 3600
    os.utime(tmp_path / "旧草稿" / "textReading" / "a.wav", (old, old))
    before = {"旧草稿", "脚本草稿", "空草稿"}
    assert [d.name for d in steps._new_audio_drafts(before)] == ["新草稿"]
    got = {d.name for d in steps._new_audio_drafts(before, time.time() - 60)}
    assert got == {"脚本草稿", "新草稿"}


def test_voice_too_long_is_not_downgraded(conn, ep, monkeypatch):
    async def yes(title):
        return True

    def too_long(*a, **k):
        raise ValueError("配音文本 6000 字，超过剪映上限 5000")

    monkeypatch.setattr(steps, "_ask_start", yes)
    monkeypatch.setattr(jianying, "generate", too_long)
    with pytest.raises(ValueError):
        asyncio.run(steps.voice_generate(_task(conn, ep), ep))


# ---------- 配音后处理 ----------

def test_voice_post_writes_subtitles_and_take(conn, ep, monkeypatch):
    d = steps._dir(ep) / "03_配音"
    (d / steps.SOURCE).write_text(json.dumps({"method": "剪映脚本", "draft": "草稿1", "voice": "热血解说员"}))
    seen = {}

    def fake_process(voice_dir, *, speed, gap):
        seen.update(speed=speed, gap=gap)
        return {"duration": 4.0, "issues": [{"kind": "停顿", "at": 1.0, "detail": "x"}],
                "segments": [{"i": 0, "text": "马来西亚站回来了。", "show": "马来西亚站回来了。", "start": 0.0, "end": 2.0},
                             {"i": 1, "text": "比赛有56圈。", "show": "比赛有56圈。", "start": 2.5, "end": 4.0}]}

    monkeypatch.setattr(steps.voice_post, "process", fake_process)
    out = asyncio.run(steps.voice_post_step({"id": 0}, ep))
    assert seen == {"speed": 1.3, "gap": 0.5}
    assert out["issues"] == {"停顿": 1}
    cues = json.loads((d / "字幕.json").read_text())
    assert cues and "。" not in "".join(c["text"] for c in cues)
    assert (d / "字幕.srt").exists()
    take = conn.execute("SELECT method, draft, speed, duration FROM voice_takes WHERE episode_id=?", (ep["id"],)).fetchone()
    assert tuple(take) == ("剪映脚本", "草稿1", 1.3, 4.0)


# ---------- 素材 ----------

def test_find_assets_needs_material(conn, ep, monkeypatch):
    from hermes import codex_runner, prompts

    async def no_links(prompt, model, effort, workdir, **kw):
        return codex_runner.CodexResult("t", '{"items": []}', {})
    monkeypatch.setattr(codex_runner, "run", no_links)
    monkeypatch.setattr(prompts, "assemble", lambda *a, **kw: "p")
    task = {"id": None, "model": "程序", "effort": None}
    with pytest.raises(StepNotReady):
        asyncio.run(steps.find_assets(task, ep))
    aid = conn.execute("INSERT INTO assets(source_type, created_at) VALUES ('测试', ?)", (db.now(),)).lastrowid
    (steps._dir(ep) / "04_素材" / steps.MANIFEST).write_text(json.dumps({"asset_ids": [aid, 999]}))
    assert asyncio.run(steps.find_assets(task, ep))["asset_ids"] == [aid]


def test_tag_assets_waits_for_postprocess(conn, ep):
    ts = db.now()
    aid = conn.execute("INSERT INTO assets(source_type, state, created_at) VALUES ('测试', 'ready', ?)",
                       (ts,)).lastrowid
    conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, keyframe) VALUES (?,0,0,5,'k.jpg')", (aid,))
    (steps._dir(ep) / "04_素材" / steps.MANIFEST).write_text(json.dumps({"asset_ids": [aid]}))
    with pytest.raises(StepDeferred) as e:                     # 没建指纹、向量、没打标
        asyncio.run(steps.tag_assets({"id": 0}, ep))
    assert e.value.seconds == steps.TAG_POLL and str(aid) in str(e.value)
    conn.execute("UPDATE asset_shots SET phash='[]', clip_vec=x'00' WHERE asset_id=?", (aid,))
    conn.execute("UPDATE assets SET tagged_at=? WHERE id=?", (ts, aid))
    assert asyncio.run(steps.tag_assets({"id": 0}, ep)) == {"assets": 1}


def test_title_and_desc(ep):
    assert steps.title_and_desc(ep) == ("雪邦前瞻", "")
    (steps._dir(ep) / "02_文案" / "标题和简介.txt").write_text("标题A\n简介第一行\n第二行", encoding="utf-8")
    assert steps.title_and_desc(ep) == ("标题A", "简介第一行\n第二行")


# ---------- 审核页文件 ----------

def test_media_whitelist(conn, ep):
    d = steps._dir(ep)
    (d / "03_配音" / "试听.mp3").write_bytes(b"ID3")
    assert steps.media_path(ep, "试听") == d / "03_配音" / "试听.mp3"
    assert steps.media_path(ep, "成片") is None
    assert steps.media_path(ep, "../../hermes.db") is None
    data = steps.review_data(conn, ep)
    assert data["media"]["试听"] > 0 and data["media"]["成片"] == 0


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_media_route(conn, ep, db_path, monkeypatch):
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    (steps._dir(ep) / "03_配音" / "试听.mp3").write_bytes(b"ID3" + b"0" * 100)
    H = {"Host": "127.0.0.1:8790"}
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        r = c.get(f"/api/episodes/{ep['id']}/media/试听", headers=H)
        assert r.status_code == 200 and r.content.startswith(b"ID3")
        r = c.get(f"/api/episodes/{ep['id']}/media/试听", headers={**H, "Range": "bytes=0-9"})
        assert r.status_code == 206 and len(r.content) == 10
        assert c.get(f"/api/episodes/{ep['id']}/media/..%2F..%2Fhermes.db", headers=H).status_code == 404
        assert c.get(f"/api/episodes/{ep['id']}/media/成片", headers=H).status_code == 404
        assert c.get(f"/api/episodes/{ep['id']}/review", headers=H).json()["media"]["试听"] > 0


@pytest.mark.parametrize("front_ok", [True, False])
def test_by_codex_places_window_first(conn, ep, monkeypatch, tmp_path, front_ok):
    from hermes import codex_runner, computer_use_runner
    calls = []

    def front():
        calls.append("front")
        if not front_ok:
            raise jianying.UIError("剪映切不到前台")

    async def run(prompt, model, effort, workdir, timeout):
        calls.append("codex")
        return type("R", (), {"message": "完成"})()

    draft = tmp_path / "新草稿"
    (draft / "textReading").mkdir(parents=True)
    (draft / "textReading" / "c.wav").write_bytes(b"x")
    monkeypatch.setattr(jianying, "front", front)
    monkeypatch.setattr(jianying, "_drafts", lambda: set())
    monkeypatch.setattr(computer_use_runner, "run", run)
    monkeypatch.setattr(codex_runner, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(steps, "_new_audio_drafts", lambda before, since=None: [draft])
    monkeypatch.setattr(steps, "_settled", lambda dr: True)
    name = asyncio.run(steps._by_codex(conn, ep, _task(conn, ep), "测试", "热血解说员", tmp_path / "raw", tmp_path / "logs"))
    assert name == "新草稿" and calls == ["front", "codex"]
