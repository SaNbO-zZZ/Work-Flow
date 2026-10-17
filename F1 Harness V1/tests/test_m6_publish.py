"""G3 后的“展示成果”：待发布手动上传、在访达中打开、审核数据带记录标题和交付目录。"""
from pathlib import Path

from fastapi.testclient import TestClient

from hermes import db, episodes, server

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
    opened = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, check=False: opened.append(args))
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="巴林大奖赛", title="旧标题")
    episodes.set_publish_title(conn, eid, "巴林大奖赛怎么跑到了雪邦")
    folder = Path(episodes.get(conn, eid)["folder"])
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.post(f"/api/episodes/{eid}/action/publish", headers=H).status_code == 409
        (folder / "08_交付").rmdir()
        assert c.post(f"/api/episodes/{eid}/reveal", headers=H).status_code == 404
        (folder / "08_交付").mkdir()
        (folder / "08_交付" / "成片.mp4").write_bytes(b"v")
        assert c.post(f"/api/episodes/{eid}/reveal", headers=H).status_code == 200
        assert opened == [["open", "-R", str(folder / "08_交付" / "成片.mp4")]]
        r = c.get(f"/api/episodes/{eid}/review", headers=H).json()
        assert r["full_title"] == "大奖赛前瞻丨巴林大奖赛怎么跑到了雪邦" and r["delivery_dir"] == str(folder / "08_交付")
        conn.execute("UPDATE episodes SET state='待发布' WHERE id=?", (eid,))
        groups = {g["name"]: [i["id"] for i in g["items"]] for g in c.get("/api/sessions", headers=H).json()}
        assert eid in groups["等我审核"]
        assert c.post(f"/api/episodes/{eid}/action/publish", headers=H).status_code == 200
        assert episodes.get(conn, eid)["state"] == "上传Drive"
