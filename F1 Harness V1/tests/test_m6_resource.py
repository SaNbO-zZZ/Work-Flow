"""“重跑找素材”按钮：状态退回、关卡记打回、请求只对本轮 D2 有效且强制 GPT 重找、接口。"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes import db, episodes, prompts, server, sourcing, steps

ORIG_CONNECT = db.connect
H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}
SCRIPT = "# 雪邦前瞻\n\n## 赛道\n雪邦有两条长直道。\n\n## 名场面\n2013 年 Multi 21。\n"


@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(steps, "notify", lambda *a, **kw: None)
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    row = episodes.get(conn, eid)
    (Path(row["folder"]) / "02_文案" / "逐字稿.md").write_text(SCRIPT, encoding="utf-8")
    (Path(row["folder"]) / "04_素材" / steps.RESOURCE_REQ).unlink(missing_ok=True)   # 各用例的期文件夹同名
    return row


def _to(conn, eid, state):
    episodes.set_state(conn, eid, state)
    return episodes.get(conn, eid)


def _task(conn, eid, run, state):
    conn.execute("INSERT INTO tasks(episode_id, stage, step, stage_run, phase, role, model, effort, state, created_at) "
                 "VALUES (?, '配素材与粗剪', 'D6剪辑决策', ?, 2, '剪辑决策', 'm', 'high', ?, ?)",
                 (eid, run, state, db.now()))


def _msgs(conn, eid):
    return [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=? AND role='system'", (eid,))]


def test_from_g2_pending_rejects_gate_and_goes_back(conn, ep):
    e = _to(conn, ep["id"], "配素材与粗剪")
    e = _to(conn, ep["id"], "G2待审")
    run = steps.request_resource(conn, ep["id"], "名场面换成 2013 年正赛画面")
    now = episodes.get(conn, ep["id"])
    assert now["state"] == "配素材与粗剪" and now["stage_run"] == e["stage_run"] + 1 == run
    g = conn.execute("SELECT state, feedback FROM gates WHERE episode_id=? AND gate='G2'", (ep["id"],)).fetchone()
    assert g["state"] == "rejected" and g["feedback"] == "【重跑找素材】名场面换成 2013 年正赛画面"
    req = json.loads((Path(ep["folder"]) / "04_素材" / steps.RESOURCE_REQ).read_text(encoding="utf-8"))
    assert req["stage_run"] == run and req["note"] == "名场面换成 2013 年正赛画面"
    assert any("重跑找素材" in m for m in _msgs(conn, ep["id"]))


def test_from_paused_stage_cancels_leftover_tasks(conn, ep):
    e = _to(conn, ep["id"], "配素材与粗剪")
    _task(conn, ep["id"], e["stage_run"], "queued")
    _task(conn, ep["id"], e["stage_run"], "succeeded")
    episodes.pause(conn, ep["id"])
    steps.request_resource(conn, ep["id"])
    now = episodes.get(conn, ep["id"])
    assert now["state"] == "配素材与粗剪" and now["paused_from"] is None and now["stage_run"] == e["stage_run"] + 1
    states = [r[0] for r in conn.execute("SELECT state FROM tasks WHERE episode_id=? ORDER BY id", (ep["id"],))]
    assert states == ["cancelled", "succeeded"]
    assert not conn.execute("SELECT 1 FROM gates WHERE episode_id=?", (ep["id"],)).fetchone()


def test_paused_at_gate_also_closes_gate(conn, ep):
    _to(conn, ep["id"], "G3待审")
    episodes.pause(conn, ep["id"])
    steps.request_resource(conn, ep["id"])
    g = conn.execute("SELECT state, feedback FROM gates WHERE episode_id=? AND gate='G3'", (ep["id"],)).fetchone()
    assert g["state"] == "rejected" and g["feedback"] == "【重跑找素材】按新要求重找素材"


@pytest.mark.parametrize("state", ["写逐字稿", "配素材与粗剪", "合成与QA", "上传Drive"])
def test_refused_outside_range_or_while_running(conn, ep, state):
    e = _to(conn, ep["id"], state)
    with pytest.raises(episodes.EpisodeError) as err:
        steps.request_resource(conn, ep["id"])
    assert ("先暂停" in str(err.value)) == (state in ("配素材与粗剪", "合成与QA"))
    assert episodes.get(conn, ep["id"])["stage_run"] == e["stage_run"]
    assert not (Path(ep["folder"]) / "04_素材" / steps.RESOURCE_REQ).exists()


def test_find_assets_forces_only_for_requested_run(conn, ep, monkeypatch):
    seen = []

    async def fake_run(conn, ep, **kw):
        seen.append(kw)
        return {"existing": 0, "from_library": 0, "segments": [], "gpt_called": kw["force"], "gpt_found": 0,
                "forced_by_feedback": kw["force"], "forced_segments": [], "accepted": 0, "downloaded": 0}
    monkeypatch.setattr(sourcing, "run", fake_run)
    monkeypatch.setattr(sourcing, "summary_text", lambda out: "找素材完成")
    _to(conn, ep["id"], "G2待审")
    steps.request_resource(conn, ep["id"], "名场面换成正赛画面")
    task = {"id": 0, "model": "程序", "effort": ""}
    asyncio.run(steps.find_assets(task, episodes.get(conn, ep["id"])))
    assert seen[-1]["force"] is True and "名场面换成正赛画面" in seen[-1]["redo"]

    episodes.set_state(conn, ep["id"], "配素材与粗剪")                 # 下一轮：请求过期，不再强制
    asyncio.run(steps.find_assets(task, episodes.get(conn, ep["id"])))
    assert seen[-1]["force"] is False


def test_resource_redo_texts(ep, tmp_path):
    path = Path(ep["folder"]) / "04_素材" / steps.RESOURCE_REQ
    run = ep["stage_run"]
    assert steps._resource_redo(ep, "旧意见") == (False, "旧意见")
    path.write_text(json.dumps({"stage_run": run, "note": ""}), encoding="utf-8")
    assert steps._resource_redo(ep, None) == (True, "用户点了“重跑找素材”：全片重新找画面")
    path.write_text(json.dumps({"stage_run": run, "note": "赛道段换车载"}), encoding="utf-8")
    assert steps._resource_redo(ep, None) == (True, "用户点了“重跑找素材”：赛道段换车载")
    assert steps._resource_redo(ep, "【重跑找素材】赛道段换车载") == (True, "【重跑找素材】赛道段换车载")
    assert steps._resource_redo(ep, "节奏太慢") == (True, "节奏太慢\n用户点“重跑找素材”时补充：赛道段换车载")


def test_sourcing_force_calls_gpt_without_keywords(conn, ep, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}|{notes}")
    monkeypatch.setattr(sourcing, "coverage", lambda *a, **kw: {"enough": True, "library": [], "segments": []})
    monkeypatch.setattr(sourcing, "existing", lambda conn, ep, old: [1])
    asked = []

    async def ask(conn, ep, prompt, **kw):
        asked.append(prompt)
        return []
    monkeypatch.setattr(sourcing, "ask_gpt", ask)
    kw = dict(model="m", effort="medium")
    out = asyncio.run(sourcing.run(conn, ep, redo="用户点了“重跑找素材”：名场面节奏太慢", **kw))
    assert out["gpt_called"] is False and not asked                      # 措辞不像要求换素材 → 不重找
    out = asyncio.run(sourcing.run(conn, ep, force=True, redo="用户点了“重跑找素材”：名场面节奏太慢", **kw))
    assert out["gpt_called"] and out["forced_segments"] == ["名场面"]   # 网页点重跑：不看措辞
    asked.clear()
    out = asyncio.run(sourcing.run(conn, ep, force=True, redo="用户点了“重跑找素材”：名场面换正赛画面", **kw))
    assert out["gpt_called"] and out["forced_by_feedback"] and out["forced_segments"] == ["名场面"]
    assert "名场面换正赛画面" in asked[0]


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_api(conn, ep, monkeypatch):
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    monkeypatch.setattr(steps, "EXECUTORS", {})
    _to(conn, ep["id"], "写逐字稿")
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        url = f"/api/episodes/{ep['id']}/resource"
        assert c.post(url, json={}, headers={"Host": "127.0.0.1:8790"}).status_code == 403
        assert c.post("/api/episodes/999/resource", json={}, headers=H).status_code == 404
        r = c.post(url, json={}, headers=H)
        assert r.status_code == 409 and "配素材与粗剪到 G3" in r.json()["detail"]
        _to(conn, ep["id"], "G2待审")
        r = c.post(url, json={"note": "赛道段换车载"}, headers=H)
        assert r.status_code == 200 and r.json()["stage_run"] == episodes.get(conn, ep["id"])["stage_run"]
        assert episodes.get(conn, ep["id"])["state"] == "配素材与粗剪"
