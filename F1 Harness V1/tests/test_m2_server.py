import asyncio

import pytest
from fastapi.testclient import TestClient

from hermes import db, episodes, server

H = {"Host": "127.0.0.1:8790"}
W = {**H, "X-Hermes": "1"}


class FakeAppServer:
    rate_limits = {"primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1791046728}}

    async def read_rate_limits(self):
        return self.rate_limits

    async def close(self):
        pass

    async def start_thread(self, cwd, instructions, model, effort):
        return "thread-1"

    async def resume_thread(self, tid):
        pass

    async def run_turn(self, tid, text, model, effort, on_delta):
        reply = '好的，马上立项。\n```hermes-action\n{"type":"start_episode","branch":"资讯","title":"测试周报","gp_label":"发车区"}\n```'
        for ch in reply[:6]:
            on_delta(ch)
        return {"text": reply, "usage": {"inputTokens": 1000, "cachedInputTokens": 400, "outputTokens": 50}, "error": None}


@pytest.fixture
def client(monkeypatch, db_path):
    monkeypatch.setattr(server.db, "connect", lambda path=None: db.__dict__["_orig_connect"](db_path))
    monkeypatch.setattr(server, "AppServer", FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        yield c


db._orig_connect = db.connect


def test_security_guards(client):
    assert client.get("/api/sessions", headers={"Host": "evil.com:8790"}).status_code == 403
    assert client.post("/api/sessions", headers=H).status_code == 403
    assert client.post("/api/sessions", headers={**W, "Origin": "http://evil.com"}).status_code == 403
    r = client.post("/api/sessions", headers={**W, "Origin": "http://127.0.0.1:8790"})
    assert r.status_code == 200
    assert "default-src 'self'" in client.get("/", headers=H).headers["content-security-policy"]


def test_message_model_validation(client, monkeypatch):
    sid = client.post("/api/sessions", headers=W).json()["id"]
    monkeypatch.setattr(server.codex_runner, "available_models",
                        lambda: [{"slug": "gpt-6-astra", "name": "A", "efforts": ["high"]}, {"slug": "gpt-6-sol", "name": "S", "efforts": ["low", "high"]}])
    bad = client.post(f"/api/sessions/{sid}/messages", headers=W, json={"text": "hi", "model": "gpt-6-sol", "effort": "ultra"})
    assert bad.status_code == 400
    ok = client.post(f"/api/sessions/{sid}/messages", headers=W, json={"text": "做一期资讯", "model": "gpt-6-sol", "effort": "low"})
    assert ok.status_code == 202
    for _ in range(50):
        d = client.get(f"/api/sessions/{sid}", headers=H).json()
        if d["episode"]["code"]:
            break
        asyncio.run(asyncio.sleep(0.02))
    msgs = d["messages"]
    assert msgs[0]["model"] == "gpt-6-sol" and msgs[0]["effort"] == "low"
    assert "hermes-action" not in msgs[1]["content"]
    # 赛历没载入时期名写待定；按赛历起名见 test_m3_integration
    assert d["episode"]["code"].endswith("-资讯-待定-01")
    top = client.get(f"/api/topbar?session={sid}", headers=H).json()
    assert top["quota"]["remaining"] == 75 and top["session"]["input"] == 1000 and top["session"]["usd"] > 0


def test_gate_endpoints(client, db_path):
    conn = db._orig_connect(db_path)
    sid = episodes.new_session(conn)
    episodes.start(conn, sid, branch="复盘", gp_label="巴林大奖赛", title="t")
    conn.execute("UPDATE episodes SET state='G1待审' WHERE id=?", (sid,))
    conn.execute("INSERT INTO gates(episode_id, gate, created_at) VALUES (?, 'G1', '2026-09-29')", (sid,))
    assert client.post(f"/api/episodes/{sid}/gates/G1/reject", headers=W, json={"feedback": "x", "back_to": "配音"}).status_code == 409
    r = client.post(f"/api/episodes/{sid}/gates/G1/reject", headers=W, json={"feedback": "开头太长"})
    assert r.json()["state"] == "写逐字稿"


def test_race_share_approve_endpoint(client, monkeypatch):
    sid = client.post("/api/sessions", headers=W).json()["id"]
    assert client.post(f"/api/episodes/{sid}/race_share/approve", headers=H).status_code == 403
    r = client.post(f"/api/episodes/{sid}/race_share/approve", headers=W)
    assert r.status_code == 409 and "还没有时间线" in r.json()["detail"]
    from hermes import qa_checks
    monkeypatch.setattr(qa_checks, "approve_race_share", lambda conn, eid: {"share": 0.5, "stage_run": 1})
    assert client.post(f"/api/episodes/{sid}/race_share/approve", headers=W).json() == \
        {"ok": True, "share": 0.5, "stage_run": 1}
