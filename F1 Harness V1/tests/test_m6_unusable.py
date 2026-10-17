"""镜头“不可用”标记的接口（剪辑、以文搜图跳过的用例在 test_m5_edit / test_m4_visual_search）。"""
from fastapi.testclient import TestClient

from hermes import db, server

ORIG_CONNECT = db.connect
H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_api(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    aid = conn.execute("INSERT INTO assets(source_type, state, created_at) VALUES ('测试', 'ready', ?)",
                       (db.now(),)).lastrowid
    conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s) VALUES (?,0,0,5)", (aid,))
    url = f"/api/assets/{aid}/shots/0/unusable"
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.put(url, json={"unusable": True}, headers={"Host": "127.0.0.1:8790"}).status_code == 403
        assert c.put(url, json={"unusable": "是"}, headers=H).status_code == 400
        assert c.put(f"/api/assets/{aid}/shots/9/unusable", json={"unusable": True}, headers=H).status_code == 404
        r = c.put(url, json={"unusable": True}, headers=H)
        assert r.status_code == 200 and r.json()["unusable"] is True and "phash" not in r.json()
        assert c.get(f"/api/assets/{aid}", headers=H).json()["shots"][0]["unusable"] is True
        assert c.put(url, json={"unusable": False}, headers=H).json()["unusable"] is False
