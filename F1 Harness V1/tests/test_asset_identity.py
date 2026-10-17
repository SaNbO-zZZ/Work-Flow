"""素材身份：按标题认哪站、哪年、赛段、类别；用户手改不被覆盖；复盘期认本站（2026-10-14 用户定）。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from hermes import asset_identity as ai
from hermes import db, episodes, glossary, server

ORIG_CONNECT = db.connect
EVENTS = [  # (轮次, 英文站名, 国家, 地点, 正赛日期, 中文站名)
    (3, "Japanese Grand Prix", "Japan", "Suzuka", "2026-03-29", "日本大奖赛"),
    (7, "Barcelona Grand Prix", "Spain", "Barcelona", "2026-06-14", "巴塞罗那大奖赛"),
    (6, "Monaco Grand Prix", "Monaco", "Monaco", "2026-06-07", "摩纳哥大奖赛"),
    (12, "British Grand Prix", "United Kingdom", "Silverstone", "2026-07-05", "英国大奖赛"),
    (14, "Hungarian Grand Prix", "Hungary", "Budapest", "2026-07-26", "匈牙利大奖赛"),
    (15, "Spanish Grand Prix", "Spain", "Madrid", "2026-09-13", "西班牙大奖赛"),
    (17, "Singapore Grand Prix", "Singapore", "Marina Bay", "2026-10-11", "新加坡大奖赛"),
    (24, "Abu Dhabi Grand Prix", "United Arab Emirates", "Yas Island", "2026-12-06", "阿布扎比大奖赛"),
]


def seed(conn):
    for rnd, en, country, loc, day, zh in EVENTS:
        conn.execute("INSERT INTO race_events(season, round, name_en, country, location, event_date, format, updated_at) "
                     "VALUES (2026, ?, ?, ?, ?, ?, 'conventional', ?)", (rnd, en, country, loc, day, db.now()))
        glossary.add_manual(conn, "大奖赛", en, zh)


def _asset(conn, title, uploader=None, duration=None) -> int:
    return conn.execute("INSERT INTO assets(source_type, title, uploader, duration, created_at) VALUES ('测试', ?, ?, ?, ?)",
                        (title, uploader, duration, db.now())).lastrowid


def test_migration_adds_identity_columns(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(assets)")}
    assert {"gp_event", "season", "session", "category", "identity_manual"} <= set(cols)
    assert cols["identity_manual"]["notnull"] == 1 and cols["identity_manual"]["dflt_value"] == "0"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


@pytest.mark.parametrize("title, uploader, duration, want", [
    ("F12026-英国大奖赛回放", None, None, ("British Grand Prix", 2026, "正赛", "比赛回放")),
    ("【排位】F1 R17 2026 新加坡大奖赛 4K HDR", None, 4000, ("Singapore Grand Prix", 2026, "排位", "比赛回放")),
    ("Singapore GP 2026 highlights", "FORMULA 1", 600, ("Singapore Grand Prix", 2026, "正赛", "集锦")),
    ("2025 摩纳哥排位 Norris 车载", None, None, ("Monaco Grand Prix", 2025, "排位", "车载")),
    ("2025 日本正赛（雪邦前瞻旧素材）", "FORMULA 1", 300, ("Japanese Grand Prix", 2025, "正赛", "集锦")),
    ("F12026-西班牙-加泰罗尼亚大奖赛回放", None, None, ("Barcelona Grand Prix", 2026, "正赛", "比赛回放")),
    ("2021 葡萄牙正赛", "FORMULA 1", 300, ("Portuguese Grand Prix", 2021, "正赛", "集锦")),
    ("2025 巴林季前测试第一天", None, None, (None, 2025, "其他", "其他")),
    ("The 'Multi 21' fiasco | Malaysia 2013", "FORMULA 1", 300, ("Malaysian Grand Prix", 2013, "其他", "官方运营")),
    ("Virtual Circuit Guide - Malaysia", None, None, ("Malaysian Grand Prix", None, "其他", "赛道介绍")),
    ("Onboard pole position lap - Lewis Hamilton, Malaysia 2017", None, None,
     ("Malaysian Grand Prix", 2017, "排位", "车载")),
    ("Behind the scenes in Singapore 2026", "Scuderia Ferrari HP", 400, ("Singapore Grand Prix", 2026, "其他", "车队运营")),
])
def test_identify(conn, title, uploader, duration, want):
    seed(conn)
    got = ai.identify(conn, title, uploader, duration)
    assert tuple(got[k] for k in ai.FIELDS) == want


def test_apply_backfill_and_manual(conn):
    seed(conn)
    a = _asset(conn, "F12026-英国大奖赛回放")
    b = _asset(conn, "Singapore GP 2026 highlights", "FORMULA 1")
    assert ai.backfill(conn) == 2 and ai.backfill(conn) == 0   # 幂等：算过的不再算
    assert conn.execute("SELECT gp_event FROM assets WHERE id=?", (a,)).fetchone()[0] == "British Grand Prix"

    got = ai.set_manual(conn, b, {"gp_event": "新加坡", "season": "2026", "session": "冲刺"})
    assert got == {"gp_event": "Singapore Grand Prix", "season": 2026, "session": "冲刺", "category": "集锦",
                   "identity_manual": 1}
    assert ai.apply(conn, b)["session"] == "冲刺"   # 手改过的不按标题重算
    assert conn.execute("SELECT session FROM assets WHERE id=?", (b,)).fetchone()[0] == "冲刺"
    with pytest.raises(ValueError):
        ai.set_manual(conn, b, {"category": "花絮"})
    with pytest.raises(ValueError):
        ai.set_manual(conn, b, {"gp_event": "火星大奖赛"})
    with pytest.raises(ValueError):
        ai.set_manual(conn, b, {"season": "明年"})
    with pytest.raises(KeyError):
        ai.set_manual(conn, 999, {})
    assert ai.apply(conn, 999) is None


def test_same_race():
    race = {"event": "Singapore Grand Prix", "season": 2026, "name_zh": "新加坡大奖赛"}
    assert ai.same_race({"gp_event": "Singapore Grand Prix", "season": 2026}, race) is True
    assert ai.same_race({"gp_event": "British Grand Prix", "season": 2026}, race) is False
    assert ai.same_race({"gp_event": "Singapore Grand Prix", "season": 2025}, race) is False
    assert ai.same_race({"gp_event": "Singapore Grand Prix", "season": None}, race) is None
    assert ai.same_race({"gp_event": None, "season": None}, race) is None
    assert ai.same_race({"gp_event": "Singapore Grand Prix", "season": 2026}, None) is None


def _ep(conn, branch, gp_label, when):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch=branch, gp_label=gp_label, title="t", when=when)
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def test_episode_race(conn):
    seed(conn)
    ep = _ep(conn, "复盘", "新加坡大奖赛", datetime(2026, 10, 13, 4, tzinfo=timezone.utc))
    assert ai.episode_race(conn, ep) == {"event": "Singapore Grand Prix", "season": 2026, "name_zh": "新加坡大奖赛"}
    # 元旦后复盘上赛季最后一站：当年没有赛历，取上一年
    ep = _ep(conn, "复盘", "阿布扎比大奖赛", datetime(2027, 1, 3, 4, tzinfo=timezone.utc))
    assert ai.episode_race(conn, ep)["season"] == 2026
    assert ai.episode_race(conn, _ep(conn, "前瞻", "新加坡大奖赛", datetime(2026, 10, 5, tzinfo=timezone.utc))) is None
    assert ai.episode_race(conn, _ep(conn, "复盘", "火星大奖赛", datetime(2026, 10, 5, tzinfo=timezone.utc))) is None


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_identity_api_and_startup_backfill(conn, db_path, monkeypatch):
    seed(conn)
    aid = _asset(conn, "Singapore GP 2026 highlights", "FORMULA 1")
    monkeypatch.setattr(server.db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    H = {"Host": "127.0.0.1:8790"}
    W = {**H, "X-Hermes": "1"}
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        row = next(a for a in c.get("/api/assets", headers=H).json()["assets"] if a["id"] == aid)
        assert (row["gp_event"], row["season"], row["category"], row["identity_manual"]) == \
            ("Singapore Grand Prix", 2026, "集锦", 0)
        assert c.put(f"/api/assets/{aid}/identity", headers=H, json={"session": "排位"}).status_code == 403
        assert c.put(f"/api/assets/{aid}/identity", headers=W, json={"session": "热身"}).status_code == 400
        assert c.put("/api/assets/999/identity", headers=W, json={}).status_code == 404
        r = c.put(f"/api/assets/{aid}/identity", headers=W, json={"gp_event": "英国大奖赛", "category": "车载"})
        assert r.status_code == 200 and r.json()["gp_event"] == "British Grand Prix"
        card = c.get(f"/api/assets/{aid}", headers=H).json()
        assert (card["gp_event"], card["category"], card["identity_manual"]) == ("British Grand Prix", "车载", 1)
