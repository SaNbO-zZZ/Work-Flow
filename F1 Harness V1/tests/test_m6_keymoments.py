"""关键回合限量（2026-10-12 抖音禁播后定）：发车、超车、缠斗、事故、红旗合计 ≤10%、每段连续 ≤5 秒，
讲名场面那段的赛道实况也算，叠层面板盖住的时间不算。"""
import json
from pathlib import Path

import pytest

from hermes import db, edit, episodes, narration, qa_checks as qa


def _ep(conn) -> int:
    ts = db.now()
    return conn.execute("INSERT INTO episodes(branch, title, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("前瞻", "关键回合", "配素材与粗剪", ts, ts)).lastrowid


def _asset(conn, shots) -> int:
    """shots: [(start, end, main, sub, manual)]"""
    aid = conn.execute("INSERT INTO assets(source_type, created_at) VALUES ('测试', ?)", (db.now(),)).lastrowid
    for i, (s, e, main, sub, *manual) in enumerate(shots):
        tags = json.dumps({"main": main, "sub": sub, "session": "正赛"}, ensure_ascii=False)
        man = json.dumps(manual[0], ensure_ascii=False) if manual else None
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags, main_manual) VALUES (?,?,?,?,?,?)",
                     (aid, i, s, e, tags, man))
    return aid


def _clip(aid, idx, src_in, src_out, start, end):
    return {"asset_id": aid, "shot_idx": idx, "src_in": src_in, "src_out": src_out, "start": start, "end": end}


def _key(conn, eid, clips, duration, overlays=()):
    tl = {"fps": 30, "duration": duration, "video": clips, "overlays": list(overlays)}
    return next(i for i in qa.timeline_issues(conn, eid, tl) if i["name"] == "关键回合画面")


SHOTS = [(0, 10, ["起步"], []), (10, 20, ["比赛中"], ["缠斗", "轮对轮"]), (20, 30, ["比赛中"], []),
         (30, 40, ["事故"], []), (40, 50, ["起步"], [], ["航拍风景"])]


def test_share_limit(conn):
    eid, a = _ep(conn), _asset(conn, SHOTS)
    ok = [_clip(a, 0, 0, 2, 0, 2), _clip(a, 2, 20, 28, 2, 10), _clip(a, 1, 10, 14, 10, 14),
          _clip(a, 2, 28, 30, 14, 16), _clip(a, 4, 40, 50, 16, 60)]
    r = _key(conn, eid, ok, 60)
    assert r["ok"] and r["hard"] and r["detail"].startswith("关键回合共 6.0s，占全片 10%")   # 手改成航拍的不算
    over = ok[:4] + [_clip(a, 3, 30, 31, 16, 17), _clip(a, 4, 40, 49, 17, 60)]
    r = _key(conn, eid, over, 60)
    assert not r["ok"] and "7.0s" in r["detail"] and "12%" in r["detail"]


def test_continuous_run_and_overlay(conn):
    eid, a = _ep(conn), _asset(conn, SHOTS)
    clips = [_clip(a, 2, 20, 30, 0, 10), _clip(a, 0, 0, 3, 10, 13), _clip(a, 1, 10, 13, 13, 16),
             _clip(a, 4, 40, 50, 16, 100)]
    r = _key(conn, eid, clips, 100)
    assert not r["ok"] and "第2个镜头(10.00s)起连续 6.0s（起步、缠斗、轮对轮）" in r["detail"]
    r = _key(conn, eid, clips, 100, [{"start": 12.0, "end": 14.0, "kind": "赛程"}])
    assert r["ok"] and "4.0s" in r["detail"]                     # 面板盖住的 2 秒不算，剩两段各 2 秒


SCRIPT = "## 开场\n今天聊聊这一站。\n\n## 本赛道名场面\n二零一三年车队指令之争。\n\n## 结尾\n下期见。\n"


def test_famous_moment_section_counts_race_footage(conn):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    folder = Path(episodes.get(conn, eid)["folder"])
    (folder / "02_文案" / "逐字稿.md").write_text(SCRIPT, encoding="utf-8")
    segs, t = [], 0.0
    for u in narration.prepare(SCRIPT).units:
        segs.append({"text": u["say"], "show": u["show"], "start": t, "end": t + 10})
        t += 10
    (folder / "03_配音" / "对齐.json").write_text(json.dumps({"duration": t, "segments": segs}), encoding="utf-8")
    sec = qa.section_span(conn, eid, qa.KEY_SECTIONS)
    assert sec == (10.0, 20.0)
    a = _asset(conn, [(0, 30, ["比赛中"], []), (30, 60, ["维修区车库"], [])])
    race = [_clip(a, 0, 0, 10, 0, 8), _clip(a, 0, 10, 22, 8, 30)]
    r = _key(conn, eid, race, 30)
    assert not r["ok"] and "名场面实况" in r["detail"] and "10.0s" in r["detail"]
    garage = [_clip(a, 0, 0, 10, 0, 10), _clip(a, 1, 30, 40, 10, 20), _clip(a, 0, 10, 20, 20, 30)]
    assert _key(conn, eid, garage, 30)["ok"]
    assert qa.section_span(conn, _ep(conn), qa.KEY_SECTIONS) is None    # 没有期文件夹


def test_edit_marks_key_shots_and_rule():
    assert "6. 关键回合限量" in edit.STEP_INSTRUCTION and "10%" in edit.STEP_INSTRUCTION
    base = {"asset_id": 1, "shot_idx": 0, "start": 0.0, "end": 3.0, "duration": 3.0, "main": ["起步"],
            "shot_type": None, "names": [], "has_logo": False, "has_subtitles": False, "cross_used": False}
    assert edit._shot_line(dict(base, key=True)).endswith("关键")
    assert not edit._shot_line(dict(base, key=False)).endswith("关键")


# ---------- 事件段本站画面、普通比赛画面占比（2026-10-14 用户定） ----------

RACE = {"event": "Singapore Grand Prix", "season": 2026, "name_zh": "新加坡大奖赛"}


def _item(conn, eid, clips, duration, name, overlays=()):
    tl = {"fps": 30, "duration": duration, "video": clips, "overlays": list(overlays)}
    return next(i for i in qa.timeline_issues(conn, eid, tl) if i["name"] == name)


def _folder_ep(conn, segs):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="新加坡大奖赛", title="新加坡复盘")
    folder = Path(episodes.get(conn, eid)["folder"])
    (folder / "03_配音" / "对齐.json").write_text(json.dumps({"duration": 30.0, "segments": segs}, ensure_ascii=False),
                                                 encoding="utf-8")
    return eid, folder


EV_SEGS = [{"i": 0, "text": "开场。", "start": 0.0, "end": 9.5},
           {"i": 1, "text": "诺里斯赛车故障退赛。", "start": 10.0, "end": 19.5},
           {"i": 2, "text": "最后夺冠。", "start": 20.0, "end": 29.5}]


@pytest.fixture
def stations(conn, monkeypatch):
    here, there, ops = (_asset(conn, [(0, 30, ["比赛中"], []), (30, 60, ["维修区车库"], [])]) for _ in range(3))
    conn.execute("UPDATE assets SET title='英国站', category='比赛回放' WHERE id=?", (there,))
    conn.execute("UPDATE assets SET title='车队视频', category='车队运营' WHERE id=?", (ops,))
    where = {here: "本站", there: "他站"}
    monkeypatch.setattr(edit, "episode_race", lambda conn, eid: RACE)
    monkeypatch.setattr(edit, "race_of", lambda asset, race: where.get(asset["id"], "未知") if race else "未知")
    return here, there, ops


def test_event_segment_other_station_fails(conn, stations, monkeypatch):
    here, there, ops = stations
    eid, _ = _folder_ep(conn, EV_SEGS)
    name = qa.EVENT_NAME
    good = [_clip(here, 0, 0, 10, 0, 10), _clip(here, 1, 30, 40, 10, 20), _clip(there, 0, 0, 10, 20, 30)]
    r = _item(conn, eid, good, 30, name)
    assert r["ok"] and r["hard"] and "新加坡大奖赛本站画面" in r["detail"]   # 他站画面落在非事件段不管
    bad = [_clip(here, 0, 0, 10, 0, 10), _clip(there, 1, 30, 40, 10, 20), _clip(here, 0, 10, 20, 20, 30)]
    r = _item(conn, eid, bad, 30, name)
    assert not r["ok"] and "第2个镜头(10.00s)" in r["detail"] and "《英国站》他站" in r["detail"] \
        and "段 1（故障、退赛）" in r["detail"]
    # 叠层面板整个盖住就不算
    assert _item(conn, eid, bad, 30, name, [{"start": 9.0, "end": 20.0}])["ok"]
    # 认不出站的素材也算失败；认不出站的运营视频里的人物/维修区画面可以垫
    unknown = _asset(conn, [(0, 30, ["维修区车库"], [])])
    r = _item(conn, eid, [_clip(here, 0, 0, 10, 0, 10), _clip(unknown, 0, 0, 10, 10, 20),
                          _clip(here, 0, 10, 20, 20, 30)], 30, name)
    assert not r["ok"] and "未知" in r["detail"]
    assert _item(conn, eid, [_clip(here, 0, 0, 10, 0, 10), _clip(ops, 1, 30, 40, 10, 20),
                             _clip(here, 0, 10, 20, 20, 30)], 30, name)["ok"]
    assert not _item(conn, eid, [_clip(here, 0, 0, 10, 0, 10), _clip(ops, 0, 0, 10, 10, 20),
                                 _clip(here, 0, 10, 20, 20, 30)], 30, name)["ok"]   # 运营视频里的赛道实况不行
    # 认不出本期是哪一站：不查
    monkeypatch.setattr(edit, "episode_race", lambda conn, eid: None)
    r = _item(conn, eid, bad, 30, name)
    assert r["ok"] and "不查" in r["detail"]


def test_race_share_limit_and_g2_approval(conn):
    eid, folder = _folder_ep(conn, EV_SEGS)
    a = _asset(conn, [(0, 100, ["比赛中"], []), (100, 200, ["维修区车库"], []), (200, 300, ["发车格"], [])])
    name = qa.RACE_SHARE_NAME

    def tl(race_s, grid_s=0.0):
        return [_clip(a, 0, 0, race_s, 0, race_s), _clip(a, 2, 200, 200 + grid_s, race_s, race_s + grid_s),
                _clip(a, 1, 100, 200 - race_s - grid_s, race_s + grid_s, 100)]
    r = _item(conn, eid, tl(40, 4), 100, name)                # 发车格也算
    assert r["ok"] and r["hard"] and "44.0s" in r["detail"] and "44%" in r["detail"]
    r = _item(conn, eid, tl(46), 100, name)
    assert not r["ok"] and "46%" in r["detail"] and "认可超比例" in r["detail"]
    assert _item(conn, eid, tl(46), 100, name, [{"start": 0, "end": 2}])["ok"]   # 叠层盖住的不算
    with pytest.raises(ValueError):
        qa.approve_race_share(conn, eid)                     # 还没有时间线

    timeline = {"fps": 30, "duration": 100, "video": tl(46), "overlays": []}
    (folder / "06_合成" / "时间线.json").write_text(json.dumps(timeline), encoding="utf-8")
    checks = qa.timeline_issues(conn, eid, timeline)
    conn.execute("INSERT INTO timelines(episode_id, stage_run, kind, path, checks, created_at) VALUES (?,?,?,?,?,?)",
                 (eid, 1, "粗剪", "x", json.dumps(checks, ensure_ascii=False), db.now()))
    rec = qa.approve_race_share(conn, eid)
    assert rec["share"] == 0.46 and rec["item"]["ok"]
    saved = json.loads((folder / qa.RACE_SHARE_FILE).read_text(encoding="utf-8"))
    assert saved["share"] == 0.46 and saved["stage_run"] == episodes.get(conn, eid)["stage_run"]
    stored = json.loads(conn.execute("SELECT checks FROM timelines WHERE episode_id=?", (eid,)).fetchone()[0])
    assert next(c for c in stored if c["name"] == name)["ok"]
    r = _item(conn, eid, tl(46), 100, name)
    assert r["ok"] and "已在 G2 认可" in r["detail"]
    assert not _item(conn, eid, tl(50), 100, name)["ok"]    # 比认可时还高要重新认可
    conn.execute("UPDATE episodes SET stage_run=stage_run+1 WHERE id=?", (eid,))
    assert _item(conn, eid, tl(46), 100, name)["ok"]        # G2 通过进合成（stage_run+1）画面没变，认可还在
    assert qa.race_share_approval(conn, eid, tl(46))["share"] == 0.46
    redo = [dict(c, src_in=c["src_in"] + 1, src_out=c["src_out"] + 1) for c in tl(46)]
    assert not _item(conn, eid, redo, 100, name)["ok"]      # 配素材与粗剪重跑换了画面，旧认可作废
    assert qa.race_share_approval(conn, eid, redo) is None and qa.race_share_approval(conn, eid) is None
    # 旧记录没有画面指纹：只认同一轮
    (folder / qa.RACE_SHARE_FILE).write_text(json.dumps({"stage_run": saved["stage_run"], "share": 0.46}), encoding="utf-8")
    assert not _item(conn, eid, tl(46), 100, name)["ok"]
    (folder / "06_合成" / "时间线.json").write_text(json.dumps({**timeline, "video": tl(40)}), encoding="utf-8")
    with pytest.raises(ValueError, match="不用认可"):
        qa.approve_race_share(conn, eid)
