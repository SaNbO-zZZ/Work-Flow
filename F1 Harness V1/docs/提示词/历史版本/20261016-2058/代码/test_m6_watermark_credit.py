"""第 18 期新加坡复盘返工（2026-10-14）：多角台标裁剪、全片常驻台标、复盘本站按赛段排序、画面小字写画面是什么、QA 台标硬项。"""
import json

import pytest

from hermes import db, edit, glossary, qa_checks as qa
from hermes import asset_identity as ai

NOTE13 = "镜头 #1–#6、…：右上角「AEchihiro」字样；镜头 #124：左下角「Sky sports」字样；镜头 #152：右下角「AEchihiro」字样"


def _asset(conn, shots, *, has_logo=1, note=None, gp=None, season=None, session=None) -> int:
    """shots: [(start, end, has_logo, logo_note)]"""
    aid = conn.execute("INSERT INTO assets(source_type, title, width, height, has_logo, logo_note, gp_event, season, "
                       "session, created_at) VALUES ('测试', 'T', 1920, 1080, ?, ?, ?, ?, ?, ?)",
                       (has_logo, note, gp, season, session, db.now())).lastrowid
    tags = json.dumps({"main": ["比赛中"], "sub": [], "shot_type": "全景"}, ensure_ascii=False)
    for i, (s, e, logo, n) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags, has_subtitles, has_logo, logo_note) "
                     "VALUES (?,?,?,?,?,0,?,?)", (aid, i, s, e, tags, logo, n))
    return aid


def _ten(flagged=6):
    """10 个镜头，前 flagged 个标了右上角台标。"""
    return [(i * 5.0, i * 5.0 + 5, 1 if i < flagged else 0, "右上角「AEchihiro」字样" if i < flagged else None)
            for i in range(10)]


# ---------- 多角裁剪 ----------

def test_logo_crop_one_corner_dodges_vertically():
    for note in ("右上角", "左上", "左下", "右下角字样"):
        c = edit.logo_crop(1920, 1080, note)
        assert c["w"] == 1612 and c["h"] == 906 and c["x"] == 154       # 0.84，水平居中
        assert c["w"] % 2 == 0 and c["h"] % 2 == 0 and abs(c["w"] / c["h"] - 16 / 9) < 0.01
        assert edit.crop_hits(c, 1920, 1080, note) == []


def test_logo_crop_two_right_corners_anchor_left():
    c = edit.logo_crop(1920, 1080, "右上角台标；右下角「AEchihiro bilibili」")
    assert c == {"x": 0, "y": 108, "w": 1536, "h": 864}                  # 0.8，贴左、上下居中
    assert c["x"] + c["w"] <= 1920 * (1 - edit.LOGO_W)


def test_logo_crop_many_corners_avoid_every_box():
    for note in (NOTE13, "左上 右上 左下 右下", "左上、右下"):
        c = edit.logo_crop(1920, 1080, note)
        assert c["w"] > 0 and c["x"] >= 0 and c["y"] >= 0 and c["x"] + c["w"] <= 1920 and c["y"] + c["h"] <= 1080
        assert edit.crop_hits(c, 1920, 1080, note) == []
    assert edit.logo_crop(1920, 1080, NOTE13)["w"] == 1536


def test_crop_hits_catches_old_single_corner_crop():
    old = {"x": 0, "y": 132, "w": 1688, "h": 948}                         # 旧算法右上只留 88%，角标没裁干净
    assert edit.crop_hits(old, 1920, 1080, "右上角") == ["右上"]
    assert edit.crop_hits(None, 1920, 1080, "右上、右下") == ["右上", "右下"]
    assert edit.crop_hits(old, 1920, 1080, "台标") == []


# ---------- 全片常驻台标 ----------

def test_persistent_logo_rule(conn):
    a = _asset(conn, _ten(6), note=NOTE13)
    note = edit.asset_logo(conn, {"id": a, "has_logo": 1, "logo_note": NOTE13})
    assert note and all(c in note for c in ("右上", "左下", "右下")) and "左上" not in note
    shots = [{"has_logo": i < 3, "logo_note": None} for i in range(10)]
    assert edit.persistent_logo({"has_logo": 1, "logo_note": "右上角"}, shots) == "全片常驻台标：右上角"   # 30% 正好够
    assert edit.persistent_logo({"has_logo": 1}, shots[:2] + shots[3:]) is None                        # 2/9 不够
    assert edit.persistent_logo({"has_logo": 0}, shots) is None
    assert edit.persistent_logo({"has_logo": 1}, [{"has_logo": True, "logo_note": None}]) == "全片常驻台标"


def test_persistent_logo_crops_every_clip(conn):
    a = _asset(conn, _ten(6), note="右上角；右下角「AEchihiro bilibili」")
    shots = edit.load_shots(conn, [a])
    assert all(s["has_logo"] and s["persistent_logo"] and "右下" in s["logo_note"] for s in shots)
    align = {"duration": 6.0, "segments": [{"i": 0, "text": "x", "start": 0.0, "end": 3.0},
                                           {"i": 1, "text": "y", "start": 3.0, "end": 6.0}]}
    cuts = [{"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": ""},
            {"segment": 1, "asset_id": a, "shot_idx": 8, "src_in": 40, "src_out": 43, "reason": ""}]   # 没标台标的镜头
    v = edit.build_timeline(conn, "/tmp", align, cuts)["video"]
    assert v[0]["crop"] == v[1]["crop"] == {"x": 0, "y": 108, "w": 1536, "h": 864}
    item = qa.logo_item(conn, v)
    assert item["ok"] and item["hard"]


def test_persistent_logo_score_penalty():
    base = {"names": [], "main": [], "has_subtitles": False, "duration": 3.0, "cross_used": False}
    plain = edit._score({**base, "has_logo": False}, "", 0.0)
    once = edit._score({**base, "has_logo": True, "persistent_logo": False}, "", 0.0)
    always = edit._score({**base, "has_logo": True, "persistent_logo": True}, "", 0.0)
    assert plain - once == pytest.approx(0.05) and plain - always == pytest.approx(0.2)


def test_qa_logo_item_hard_and_lists_clips(conn):
    a = _asset(conn, _ten(6), note="右上角")
    clean = _asset(conn, [(0, 10, 0, None)], has_logo=0)
    clip = lambda aid, i, s, crop=None: {"asset_id": aid, "shot_idx": i, "src_in": s, "src_out": s + 2,
                                         "start": 0, "end": 2, "crop": crop}
    clips = [clip(a, 8, 40.5), clip(a, 0, 0.5, {"x": 0, "y": 132, "w": 1688, "h": 948}),
             clip(a, 1, 5.5, edit.logo_crop(1920, 1080, "右上角")), clip(clean, 0, 1)]
    item = qa.logo_item(conn, clips)
    assert not item["ok"] and item["hard"]
    assert "第1个镜头" in item["detail"] and "没裁" in item["detail"]          # 常驻台标的未标镜头也要裁
    assert "第2个镜头" in item["detail"] and "没避开右上角" in item["detail"]
    assert "第3个镜头" not in item["detail"] and "第4个镜头" not in item["detail"]
    assert edit._hard_problems([item]) == []                                  # 程序裁的，不找 GPT 重做


# ---------- 复盘本站按赛段 ----------

def test_session_bonus_and_marks():
    base = {"names": [], "main": [], "has_subtitles": False, "has_logo": False, "duration": 3.0,
            "cross_used": False, "race": "本站"}
    said = "冲刺 排位 练习"   # 口播讲到这一节
    race = edit._score({**base, "session": "正赛"}, said, 0.0)
    sprint = edit._score({**base, "session": "冲刺"}, said, 0.0)
    quali = edit._score({**base, "session": "排位"}, said, 0.0)
    practice = edit._score({**base, "session": "练习"}, said, 0.0)
    unknown = edit._score({**base, "session": None}, said, 0.0)
    assert (race, sprint, unknown, quali, practice) == (0.25, 0.15, 0.1, 0.0, 0.0)
    # 口播没讲到那一节再减（2026-10-15）
    assert edit._score({**base, "session": "冲刺"}, "", 0.0) == pytest.approx(0.15 - edit.OFF_SESSION_PENALTY)
    assert edit._score({**base, "session": "正赛"}, "", 0.0) == 0.25
    # 他站不看赛段；他站只罚比赛实况和事件段（2026-10-16）
    track = {**base, "race": "他站", "session": "正赛", "main": ["比赛中"]}
    assert edit._score(track, "", 0.0) == pytest.approx(-0.15 - edit.RACE_OFF_PENALTY)
    assert edit._score({**base, "race": "他站", "session": "正赛"}, "", 0.0) == 0
    line = lambda **k: edit._shot_line({**base, "asset_id": 1, "shot_idx": 0, "start": 0.0, "end": 3.0,
                                        "shot_type": None, "key": False, **k})
    assert "本站正赛" in line(session="正赛") and "本站排位" in line(session="排位")
    assert line(session=None).endswith(" 本站") and " 他站" in line(race="他站", session="正赛")
    assert "他站正赛" not in line(race="他站", session="正赛")


def test_review_episode_prefers_race_session(conn, monkeypatch):
    conn.execute("INSERT INTO race_events(season, round, name_en, country, location, event_date, format, updated_at) "
                 "VALUES (2026, 17, 'Singapore Grand Prix', 'Singapore', 'Marina Bay', '2026-10-11', 'conventional', ?)",
                 (db.now(),))
    glossary.add_manual(conn, "大奖赛", "Singapore Grand Prix", "新加坡大奖赛")
    eid = conn.execute("INSERT INTO episodes(code, branch, gp_label, title, state, created_at, updated_at) "
                       "VALUES ('20261013-01', '复盘', '新加坡大奖赛', 'T', '配素材与粗剪', ?, ?)",
                       (db.now(), db.now())).lastrowid
    gp = "Singapore Grand Prix"
    q = _asset(conn, [(0, 5, 0, None)], has_logo=0, gp=gp, season=2026, session="排位")
    r = _asset(conn, [(0, 5, 0, None)], has_logo=0, gp=gp, season=2026, session="正赛")
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **k: [])
    res = edit.candidates(conn, [q, r], [{"i": 0, "text": "比赛很精彩", "start": 0, "end": 3}], episode_id=eid)
    first = res["segments"][0]["candidates"][0]
    assert first["asset_id"] == r and first["session"] == "正赛" and first["race"] == "本站"
    ctx = edit.build_context(conn, res, [q, r])
    assert "本站正赛" in ctx and "本站排位" in ctx


# ---------- 画面小字 ----------

def test_picture_credit(conn):
    glossary.add_manual(conn, "大奖赛", "Singapore Grand Prix", "新加坡大奖赛")
    sg = {"gp_event": "Singapore Grand Prix", "season": 2026, "session": "正赛",
          "uploader": "AEchihiro", "title": "【F1】新加坡站 正赛", "source_url": "https://www.bilibili.com/video/BV1"}
    assert ai.picture_credit(conn, sg) == "2026 新加坡大奖赛正赛"
    assert ai.picture_credit(conn, {**sg, "session": "其他"}) == "2026 新加坡大奖赛"
    assert ai.picture_credit(conn, {**sg, "session": None, "season": None}) == "新加坡大奖赛"
    assert ai.picture_credit(conn, {**sg, "session": "排位", "season": None}) == "新加坡大奖赛排位"
    assert ai.picture_credit(conn, {"gp_event": "Malaysian Grand Prix", "season": 2017, "session": "冲刺"}) \
        == "2017 马来西亚大奖赛冲刺"
    assert ai.picture_credit(conn, {"gp_event": "Sakhir Grand Prix", "season": 2020}) == "2020 萨基尔大奖赛"
    assert ai.picture_credit(conn, {**sg, "gp_event": None}) is None


def test_timeline_credit_says_what_not_where(conn):
    a = _asset(conn, [(0, 10, 0, None)], has_logo=0, gp="Singapore Grand Prix", season=2026, session="正赛")
    conn.execute("UPDATE assets SET uploader='AEchihiro', credit='AEchihiro', source_url='https://www.bilibili.com/video/BV1' "
                 "WHERE id=?", (a,))
    align = {"duration": 4.0, "segments": [{"i": 0, "text": "x", "start": 0.0, "end": 2.0},
                                           {"i": 1, "text": "y", "start": 2.0, "end": 4.0}]}
    cuts = [{"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 2, "reason": ""},
            {"segment": 1, "asset_id": a, "shot_idx": 0, "src_in": 4, "src_out": 6, "reason": ""}]
    credits = edit.build_timeline(conn, "/tmp", align, cuts)["credits"]
    assert credits == [{"text": "2026 新加坡大奖赛正赛", "start": 0.0, "end": 4.0}]           # 同文字连着的合并
    assert not any(w in credits[0]["text"] for w in ("AEchihiro", "B站", "bilibili", "画面："))
