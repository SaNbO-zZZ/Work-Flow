"""第 3 批：版本保留（09_版本）、会话卡片、旧版本文件接口。全部不联网。"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from hermes import db, episodes, server, versions

H = {"Host": "127.0.0.1:8790"}


def _episode(conn):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="巴林大奖赛", title="巴林前瞻")
    ep = episodes.get(conn, eid)
    folder = Path(ep["folder"])
    (folder / "02_文案").mkdir(parents=True, exist_ok=True)
    return ep, folder


def _write_g1(folder, script, title="标题", desc="简介"):
    (folder / "02_文案" / "逐字稿.md").write_text(script, encoding="utf-8")
    (folder / "02_文案" / "标题和简介.txt").write_text(f"{title}\n{desc}\n", encoding="utf-8")
    (folder / "02_文案" / "._逐字稿.md").write_text("x", encoding="utf-8")   # exFAT 垃圾文件不存


def _enter_g1(conn, eid):
    with db.transaction(conn):
        episodes.set_state(conn, eid, "G1待审")
    return conn.execute("SELECT MAX(id) FROM gates WHERE episode_id=?", (eid,)).fetchone()[0]


def test_set_state_adds_gate_card(conn):
    ep, _ = _episode(conn)
    gid = _enter_g1(conn, ep["id"])
    m = conn.execute("SELECT * FROM messages WHERE episode_id=? AND role='card'", (ep["id"],)).fetchone()
    assert json.loads(m["card"]) == {"type": "gate", "gate": "G1", "gate_id": gid} and m["content"] == "G1 待审"


def test_snapshots_and_gate_versions(conn):
    ep, folder = _episode(conn)
    _write_g1(folder, "## 开场\n第一版\n", title="旧标题")
    g1 = _enter_g1(conn, ep["id"])
    job = versions.gate_job(conn, ep["id"], g1)
    assert job == (folder, "G1", 1, g1)
    dst = versions.snapshot_gate(*job)
    assert dst == folder / "09_版本" / "G1-v1"
    meta = json.loads((dst / "版本.json").read_text(encoding="utf-8"))
    assert meta["files"] == ["02_文案/逐字稿.md", "02_文案/标题和简介.txt"] and meta["v"] == 1
    assert not (dst / "02_文案" / "._逐字稿.md").exists()
    episodes.reject(conn, ep["id"], "G1", "改简介")

    _write_g1(folder, "## 开场\n第二版\n", title="新标题")      # 第二版没存（模拟第 3 批之前）→ 用当前文件
    g2 = _enter_g1(conn, ep["id"])
    vs = versions.gate_versions(conn, ep)["G1"]
    assert [(v["v"], v["state"], v["dir"], v["live"]) for v in vs] == [
        (1, "rejected", "09_版本/G1-v1", False), (2, "pending", "", True)]
    assert vs[0]["script"] == "## 开场\n第一版\n" and vs[0]["title"] == "旧标题" and vs[0]["feedback"] == "改简介"
    assert vs[1]["script"] == "## 开场\n第二版\n" and vs[1]["title"] == "新标题" and vs[1]["desc"] == "简介"

    versions.snapshot_gate(*versions.gate_job(conn, ep["id"], g2))
    episodes.reject(conn, ep["id"], "G1", "再改")
    (folder / "09_版本" / "G1-v1").rename(folder / "09_版本" / "旧")   # 旧版文件没了 → dir None，不读当前文件
    _enter_g1(conn, ep["id"])
    vs = versions.gate_versions(conn, ep)["G1"]
    assert [(v["dir"], v["live"]) for v in vs] == [(None, False), ("09_版本/G1-v2", False), ("", True)]
    assert "script" not in vs[0]


def test_snapshot_run_and_cards(conn):
    ep, folder = _episode(conn)
    (folder / "03_配音").mkdir(parents=True, exist_ok=True)
    (folder / "03_配音" / "试听.mp3").write_bytes(b"mp3")
    assert versions.snapshot_run(ep, "配音-r2", ["03_配音/试听.mp3", "03_配音/没有.mp3"]) == "09_版本/配音-r2"
    assert (folder / "09_版本" / "配音-r2" / "03_配音" / "试听.mp3").read_bytes() == b"mp3"
    mid = versions.add_card(conn, ep["id"], {"type": "voice", "run": 2}, "配音")
    versions.update_card(conn, mid, {"type": "voice", "run": 2, "duration": 3.0}, "配音好了")
    assert versions.find_card(conn, ep["id"], "voice", 2) == (mid, {"type": "voice", "run": 2, "duration": 3.0})
    assert versions.find_card(conn, ep["id"], "voice", 3) is None


def test_enrich_sourcing_live_state(conn):
    ep, folder = _episode(conn)
    (folder / "04_素材").mkdir(parents=True, exist_ok=True)
    aid = conn.execute("INSERT INTO assets(source_type, title, duration, created_at) VALUES ('GPT搜索', '雪邦航拍', 42.0, ?)",
                       (db.now(),)).lastrowid
    did = conn.execute("INSERT INTO downloads(url, state, asset_id, created_at) VALUES ('https://x/a', 'done', ?, ?)",
                       (aid, db.now())).lastrowid
    bad = conn.execute("INSERT INTO downloads(url, state, error, created_at) VALUES ('https://x/b', 'failed', ?, ?)",
                       ("ERROR: 403\n更多", db.now())).lastrowid
    manifest = {"stage_run": 4, "asset_ids": [aid], "summary": {"gpt_called": True, "forced_segments": ["赛道"]},
                "found": [{"url": "https://x/a", "state": "下载中", "download_id": did},
                          {"url": "https://x/b", "state": "下载中", "download_id": bad}]}
    (folder / "04_素材" / "素材清单.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    card = versions.enrich(conn, ep, {"type": "sourcing", "run": 4})
    a, b = card["found"]
    assert (a["state"], a["asset_id"], a["asset_title"], a["duration"]) == ("已下载", aid, "雪邦航拍", 42.0)
    assert (b["state"], b["note"]) == ("下载失败", "ERROR: 403")
    assert card["assets"] == [{"id": aid, "title": "雪邦航拍"}] and card["forced_segments"] == ["赛道"]
    assert versions.enrich(conn, ep, {"type": "sourcing", "run": 5})["found"] == []    # 别的轮次不读当前清单
    assert versions.enrich(conn, ep, {"type": "voice", "run": 4}) == {"type": "voice", "run": 4}


def test_file_endpoint_and_session_versions(monkeypatch, conn, db_path):
    connect = db.connect
    monkeypatch.setattr(server.db, "connect", lambda path=None: connect(db_path))
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    ep, folder = _episode(conn)
    _write_g1(folder, "## 开场\n正文\n")
    (folder / ".hidden").write_text("x", encoding="utf-8")
    (folder.parent / "外面.txt").write_text("x", encoding="utf-8")
    versions.snapshot_gate(*versions.gate_job(conn, ep["id"], _enter_g1(conn, ep["id"])))
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        base = f"/api/episodes/{ep['id']}/file/"
        r = c.get(base + "09_版本/G1-v1/02_文案/逐字稿.md", headers=H)
        assert r.status_code == 200 and r.text == "## 开场\n正文\n"
        for bad in ("..%2F外面.txt", "../外面.txt", ".hidden", "02_文案", "没有.txt"):
            assert c.get(base + bad, headers=H).status_code == 404, bad
        d = c.get(f"/api/sessions/{ep['id']}", headers=H).json()
        assert d["versions"]["G1"][0]["dir"] == "09_版本/G1-v1"
        assert [m["card"]["type"] for m in d["messages"] if m["card"]] == ["gate"]
