"""2026-10-16 用户定：先按文案每段讲的场景找画面（讲升级找工厂研发、讲可靠性找车库，只有讲比赛的段才用比赛实况），
资讯、科普也由程序在本机搜 YouTube 官方候选；总控对话里说找素材发 resource 动作；连发两次的消息只处理一次。"""
import asyncio
import json
from pathlib import Path

import pytest
from test_m6_sourcing import _asset, _filler, _other_ep, _search

from hermes import chat, db, episodes, glossary, sourcing, steps

ORIG_CONNECT = db.connect
SCRIPT = ("## 负升级的疑问\n迈凯伦这是负升级了吗，新零件装上了，车迷还是不满意。\n\n"
          "## 可靠性也在添乱\n迈凯伦官方确认出现过喷油异常。到了新加坡正赛末段，诺里斯又遭遇传感器故障。\n\n"
          "## 名场面\n新加坡正赛诺里斯超车皮亚斯特里。\n")


@pytest.fixture
def cal(conn):
    for rnd, en, loc, day, zh in ((12, "British Grand Prix", "Silverstone", "2026-07-05", "英国大奖赛"),
                                  (17, "Singapore Grand Prix", "Marina Bay", "2026-10-11", "新加坡大奖赛")):
        conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                     "VALUES (2026, ?, ?, ?, ?, 'conventional', ?)", (rnd, en, loc, day, db.now()))
        glossary.add_manual(conn, "大奖赛", en, zh)


def _segs(conn):
    return sourcing.describe_scenes(conn, sourcing.segments(SCRIPT), "资讯")


def test_scene_by_script(conn, cal):
    segs = _segs(conn)
    assert [s["scene"] for s in segs] == ["工厂", "车库", "比赛"]
    assert [s["gps"] for s in segs] == [[], ["Singapore Grand Prix"], ["Singapore Grand Prix"]]
    assert [s["race_ok"] for s in segs] == [False, True, True]
    assert sourcing.scene("迈凯伦的车手合同", "资讯") == "运营" and sourcing.scene("车手合同", "复盘") == "比赛"


def test_coverage_race_footage_only_for_race_segments(conn, cal, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    eid = episodes.new_session(conn)
    gb = _asset(conn, tmp_path, 30, title="英国大奖赛回放")
    sg = _asset(conn, tmp_path, 30, title="新加坡集锦")
    ops = _asset(conn, tmp_path, 30, title="McLaren factory")
    for aid, gp, cat in ((gb, "British Grand Prix", "比赛回放"), (sg, "Singapore Grand Prix", "集锦"),
                         (ops, None, "车队运营")):
        conn.execute("UPDATE assets SET gp_event=?, season=2026, session=?, category=? WHERE id=?",
                     (gp, "正赛" if gp else "其他", cat, aid))
    segs = _segs(conn)
    # 本期带着英国站回放：哪段都算不上
    cov = sourcing.coverage(conn, eid, [gb], segs, search=_search({gb}))
    assert [g["have"] for g in cov["segments"]] == [0, 0, 0]
    # 新加坡集锦只算进讲新加坡比赛的段；英国站回放库里不拿
    cov = sourcing.coverage(conn, eid, [sg], segs, search=_search({sg}))
    assert cov["segments"][0]["have"] == 0 and cov["segments"][1]["have"] > 0 and cov["segments"][2]["have"] > 0
    cov = sourcing.coverage(conn, eid, [sg], segs, search=_search({ops}))   # 英国站回放不在可拿的里面
    assert cov["library"] == [ops]


def test_candidates_without_race_by_scene(conn, cal):
    asked = []

    def e(vid, title, channel, dur=300):
        return {"id": vid, "title": title, "channel": channel, "duration": dur,
                "url": f"https://www.youtube.com/watch?v={vid}"}
    hits = {
        "McLaren 2026 factory": [e("MCLFACTORY1", "McLaren 2026 wind tunnel upgrade", "McLaren"),
                                 e("MCLOLDYEAR1", "Inside the MTC", "McLaren"),
                                 e("FANFANFAN01", "McLaren 2026 upgrade explained", "F1 Fan Edits")],
        "McLaren 2026 garage": [e("MCLGARAGE01", "Garage cam", "McLaren")],
        "Singapore Grand Prix highlights": [e("SGHIGHLIGH1", "Singapore GP 2026 highlights", "FORMULA 1"),
                                            e("GBHIGHLIGH1", "British GP 2026 highlights", "FORMULA 1")],
    }

    async def search(q, n=8):
        asked.append(q)
        return [x for k, xs in hits.items() if k in q for x in xs]

    async def probe(url):
        return {"upload_date": "20250601" if "OLDYEAR" in url else "20260901"}

    cands, gaps = asyncio.run(sourcing.candidates(conn, _segs(conn), None, search=search, probe=probe, season=2026))
    assert "McLaren 2026 factory" in asked and "McLaren 2026 garage" in asked
    assert "2026 Singapore Grand Prix highlights" in asked
    assert "https://www.youtube.com/@mclarenracing/videos" in asked
    got = {c["url"][-11:]: c for c in cands}
    assert set(got) == {"MCLFACTORY1", "MCLGARAGE01", "SGHIGHLIGH1"}
    assert got["MCLFACTORY1"]["segment"] == "负升级的疑问" and got["SGHIGHLIGH1"]["race_footage"]
    assert all(c["same_race"] is None for c in cands) and gaps == []


def test_screen_without_race_needs_this_year_team_ops(conn):
    items = [{"url": "https://www.youtube.com/watch?v=OLDOLDOLD01", "title": "Inside the MTC", "channel": "McLaren",
              "segment": "s", "reason": "r"},
             {"url": "https://www.youtube.com/watch?v=NEWNEWNEW01", "title": "Wind tunnel", "channel": "McLaren",
              "segment": "s", "reason": "r"}]

    async def probe(url):
        return {"webpage_url": url, "duration": 600, "title": "t", "uploader": "McLaren",
                "upload_date": "20250101" if "OLD" in url else "20260101"}
    out = asyncio.run(sourcing.screen(conn, items, limit=6, max_minutes=5, probe=probe, season=2026))
    assert out[0]["state"] == "跳过" and "2025 年发的车队运营视频" in out[0]["note"]
    assert out[1]["state"] == "待下载"   # 车队运营视频按 30 分钟上限


def test_chat_resource_action(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(steps, "notify", lambda *a, **kw: None)
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="资讯", gp_label="发车区", title="迈凯伦升级")
    episodes.set_state(conn, eid, "配素材与粗剪")
    run = episodes.get(conn, eid)["stage_run"]
    msg = chat.apply_action(conn, eid, {"type": "resource", "note": "多用工厂画面"})
    ep = episodes.get(conn, eid)
    assert "已退回配素材与粗剪" in msg and ep["state"] == "配素材与粗剪" and ep["stage_run"] == run + 1
    req = json.loads((Path(ep["folder"]) / "04_素材" / steps.RESOURCE_REQ).read_text(encoding="utf-8"))
    assert req["note"] == "多用工厂画面" and req["stage_run"] == run + 1
    # 不在能重找的阶段：不执行，状态不变
    episodes.set_state(conn, eid, "配音")
    assert "动作没有执行" in chat.apply_action(conn, eid, {"type": "resource"})
    assert episodes.get(conn, eid)["state"] == "配音"
    assert '"type": "resource"' in chat.CONDUCTOR_INSTRUCTIONS.format(docs="d", state="s")


def test_duplicate_message():
    assert not chat.is_duplicate(999, "去网上下新素材", at=100.0)
    assert chat.is_duplicate(999, "去网上下新素材", at=105.0)
    assert not chat.is_duplicate(999, "去网上下新素材", at=200.0)
    assert not chat.is_duplicate(999, "别的话", at=201.0)
