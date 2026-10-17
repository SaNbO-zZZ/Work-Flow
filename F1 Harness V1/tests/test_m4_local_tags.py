import asyncio
import json

import numpy as np
import pytest

from hermes import assets, db, glossary, library, local_tags

ROSTER = [
    {"code": "VER", "name": "Max Verstappen", "team": "Red Bull Racing"},
    {"code": "LEC", "name": "Charles Leclerc", "team": "Ferrari"},
    {"code": "NOR", "name": "Lando Norris", "team": "McLaren"},
    {"code": "HAM", "name": "Lewis Hamilton", "team": "Ferrari"},
    {"code": "PIA", "name": "Oscar Piastri", "team": "McLaren"},
    {"code": "RUS", "name": "George Russell", "team": "Mercedes"},
    {"code": "ANT", "name": "Andrea Kimi Antonelli", "team": "Mercedes"},
]


def ln(t, x=0.4, y=0.4, w=0.1, h=0.03, c=1.0):
    return {"t": t, "c": c, "x": x, "y": y, "w": w, "h": h}


@pytest.fixture
def names(conn):
    for orig, zh in (("Max Verstappen", "维斯塔潘"), ("Charles Leclerc", "勒克莱尔"), ("Lando Norris", "诺里斯"),
                     ("Lewis Hamilton", "汉密尔顿"), ("Oscar Piastri", "皮亚斯特里")):
        glossary.add_manual(conn, "车手", orig, zh)
    for orig, zh in (("Red Bull Racing", "红牛"), ("Ferrari", "法拉利"), ("McLaren", "迈凯伦")):
        glossary.add_manual(conn, "车队", orig, zh)
    return local_tags.Names(conn, ROSTER)


def test_names_normalize(names):
    assert names.codes["VER"] == "维斯塔潘" and names.team_of["维斯塔潘"] == "红牛"
    assert names.surnames["NORRIS"] == "诺里斯" and names.teams["FERRARI"] == "法拉利"
    # 不在译名表里的车手保留原名
    assert names.codes["RUS"] == "George Russell"


def test_read_shot_battle_graphic(names):
    r = local_tags.read_shot([ln("LAP 6/70", x=0.05, y=0.1), ln("BATTLE FOR P2"), ln("NORRIS"),
                              ln("PIASTRI", y=0.5), ln("+0.503")], names)
    assert r["session_hint"] == "正赛" and "battle" in r["flags"] and r["gaps"] == [0.503]
    assert r["drivers"] == ["诺里斯", "皮亚斯特里"] and r["teams"] == ["迈凯伦"]
    assert r["order"] == ["诺里斯", "皮亚斯特里"]
    assert r["has_subtitles"] is False


def test_read_shot_skips_timing_tower(names):
    tower = [ln("LAP 12/70", x=0.02, y=0.08)] + [ln(f"{i} {c}", x=0.02, y=0.1 + i * 0.03)
                                                for i, c in enumerate(["VER", "LEC", "NOR", "HAM", "PIA"], 1)]
    r = local_tags.read_shot(tower + [ln("LECLERC", x=0.5, y=0.85)], names)
    assert r["drivers"] == ["勒克莱尔"] and r["teams"] == ["法拉利"]


def test_read_shot_code_limits(names):
    # 画面中间一次出现太多缩写（发车格图等）不算出镜
    many = [ln(c, y=0.1 * i) for i, c in enumerate(["VER", "LEC", "NOR", "HAM"])]
    assert local_tags.read_shot(many, names)["drivers"] == []
    assert local_tags.read_shot([ln("VER  +1.2")], names)["drivers"] == ["维斯塔潘"]


def test_read_shot_tower_battle_box(names):
    # 实测比利时站 #114：对战框在左侧计时塔里，列车手全名和三位小数间隔
    lines = [ln("LAP 5/44", x=0.08, y=0.13, w=0.05), ln("2 & ANT Leoder M", x=0.05, y=0.17, w=0.08),
             ln("BATTLE FOR2N0", x=0.06, y=0.2, w=0.08), ln("2O LECLERC", x=0.05, y=0.23, w=0.08),
             ln("+0.198", x=0.07, y=0.28, w=0.05), ln("I3 ： VERSTAPPEN", x=0.05, y=0.32, w=0.08),
             ln("+2.6M", x=0.13, y=0.36, w=0.04), ln("10 7 NOR", x=0.05, y=0.55, w=0.06),
             ln("HAMILTON", x=0.05, y=0.8, w=0.06)]
    r = local_tags.read_shot(lines, names)
    assert r["drivers"] == ["勒克莱尔", "维斯塔潘"] and r["order"] == ["勒克莱尔", "维斯塔潘"]
    assert r["gaps"] == [0.198] and "battle" in r["flags"]
    assert "start" in local_tags.read_shot([ln("LAP1170", x=0.08, y=0.13)], names)["flags"]


def test_grid_after_race_start_uses_next_best(conn, names):
    aid = _ready_asset(conn, 20)
    texts = {f"/k/{aid}/2.jpg": [ln("FORMATION LAP")]}
    base = [("发车格", "全景", "观众")] * 2 + [("发车格", "全景", "观众")] + [("比赛中", "跟拍", "安全车")] * 16 \
        + [("发车格", "全景", "庆祝")]
    out = local_tags.tag_asset(conn, aid, ocr_fn=lambda p: texts, classify_fn=lambda v: base,
                               roster_loader=lambda s: ROSTER)
    got = [s["main"][0] for s in out["shots"]]
    assert got[:3] == ["发车格", "发车格", "起步"] and got[19] == "庆祝"


def test_session_hints_and_flags(names):
    def read(*texts):
        return local_tags.read_shot([ln(t) for t in texts], names)
    assert read("Q3", "QUALIFYING")["session_hint"] == "排位"
    assert read("SPRINT")["session_hint"] == "冲刺" and read("FP2")["session_hint"] == "练习"
    assert read("TURN 4")["session_hint"] is None and read("TURN 4")["flags"] == set()
    assert read("FIA PRESS CONFERENCE")["flags"] == {"press"}
    assert read("FIA SAFETY CAR")["flags"] == {"sc"} and read("VSC")["flags"] == {"sc"}
    assert read("FORMATION LAP")["flags"] == {"start"} and "start" in read("LAP 1/70")["flags"]
    assert "start" not in read("LAP 12/70")["flags"]
    assert read("RACE CONTROL: RED FLAG")["flags"] == {"red"}
    assert read("CAR 44 STOPPED ON TRACK")["flags"] == {"incident"}
    assert read("FIA STEWARDS: 5 SECOND TIME PENALTY")["flags"] == {"probe"}


def test_title_session():
    assert local_tags._title_session("F12026-匈牙利大奖赛回放") == "正赛"
    assert local_tags._title_session("2026 英国站冲刺排位") == "排位"
    assert local_tags._title_session("Sprint highlights") == "冲刺"
    assert local_tags._title_session("FP1 集锦") == "练习"
    assert local_tags._title_session("随便") is None


def test_subtitles_chinese_only():
    assert local_tags._is_subtitle(ln("维斯塔潘在第一圈就超了上去", x=0.3, y=0.85, w=0.4))
    # 官方英文图形在下方居中，不当字幕
    assert not local_tags._is_subtitle(ln("Charles Leclerc leads the race", x=0.3, y=0.85, w=0.4))
    assert not local_tags._is_subtitle(ln("维斯塔潘在第一圈", x=0.3, y=0.3, w=0.4))       # 不在下方
    assert not local_tags._is_subtitle(ln("维斯塔潘在第一圈", x=0.0, y=0.85, w=0.2))       # 不居中
    assert not local_tags._is_subtitle(ln("维斯塔潘在第一圈", x=0.3, y=0.85, w=0.4, c=0.3))  # 置信度低


def test_find_logos():
    per = {i: [ln("F1", x=0.02, y=0.02), ln("LAP 3/70", x=0.02, y=0.05)] for i in range(20)}
    per[3].append(ln("SKY SPORTS F1", x=0.85, y=0.03))
    per[4].append(ln("@某UP主", x=0.85, y=0.9))
    per[5].append(ln("某UP主 出品", x=0.02, y=0.9))
    for i in range(10, 16):  # 6/20 个镜头右上角常驻同一文字
        per[i].append(ln("XYZ TV", x=0.9, y=0.02, w=0.08))
    hits = local_tags.find_logos(per, "某UP主")
    assert sorted(hits) == [3, 4, 5, *range(10, 16)]
    assert hits[3] == "右上角「SKY SPORTS F1」字样" and hits[10] == "右上角常驻「XYZ TV」字样"
    # 只出现在少数镜头的角落文字不算
    per2 = {i: [] for i in range(20)}
    for i in range(3):
        per2[i].append(ln("XYZ TV", x=0.9, y=0.02, w=0.08))
    assert local_tags.find_logos(per2, None) == {}


def test_circuit_from_title(conn):
    glossary.add_manual(conn, "大奖赛", "Hungarian Grand Prix", "匈牙利大奖赛")
    glossary.add_manual(conn, "赛道", "Budapest", "布达佩斯")
    conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                 "VALUES (2026, 13, 'Hungarian Grand Prix', 'Budapest', '2026-07-26', 'conventional', ?)",
                 (db.now(),))
    assert local_tags.circuit_from_title(conn, "F12026-匈牙利大奖赛回放") == "布达佩斯"
    assert local_tags.circuit_from_title(conn, "2026 Hungarian Grand Prix highlights") == "布达佩斯"
    assert local_tags.circuit_from_title(conn, "随便一个视频") is None
    assert local_tags.circuit_from_title(conn, None) is None


def test_roster_cached(conn):
    calls = []

    def loader(season):
        calls.append(season)
        return ROSTER
    assert local_tags.roster(conn, 2026, loader=loader) == ROSTER
    assert local_tags.roster(conn, 2026, loader=loader) == ROSTER
    assert calls == [2026]

    def broken(season):
        raise RuntimeError("断网")
    assert local_tags.roster(conn, 2025, loader=broken) == []
    assert local_tags._season("F12026-匈牙利大奖赛回放") == 2026


def _ready_asset(conn, n, *, title="F12026-匈牙利大奖赛回放", uploader="某UP主", sha="sha-local"):
    aid = conn.execute("INSERT INTO assets(source_type, title, uploader, sha256, state, duration, created_at) "
                       "VALUES ('脚本', ?, ?, ?, 'ready', ?, ?)", (title, uploader, sha, n * 2.0, db.now())).lastrowid
    rng = np.random.default_rng(aid)
    for i in range(n):
        v = rng.normal(size=8).astype(np.float32)
        v /= np.linalg.norm(v)
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, keyframe, clip_vec) VALUES (?,?,?,?,?,?)",
                     (aid, i, i * 2.0, i * 2.0 + 2, f"/k/{aid}/{i}.jpg", v.astype(np.float16).tobytes()))
    return aid


def test_tag_asset_end_to_end(conn, names):
    aid = _ready_asset(conn, 4)
    texts = {
        f"/k/{aid}/0.jpg": [ln("NORRIS", y=0.5), ln("维斯塔潘在第一圈就超了上去", x=0.3, y=0.85, w=0.4)],
        f"/k/{aid}/1.jpg": [ln("LAP 5/70", x=0.02, y=0.05), ln("FIA PRESS CONFERENCE")],
        f"/k/{aid}/2.jpg": [ln("LAP 6/70", x=0.02, y=0.05), ln("SKY SPORTS", x=0.88, y=0.02)],
        f"/k/{aid}/3.jpg": [],
    }
    classes = [("比赛中", "跟拍"), ("数据图表", "人物"), ("比赛中", "车载"), ("观众", "全景")]
    seen = {}

    def ocr_fn(paths):
        seen["paths"] = paths
        return texts

    def classify_fn(vecs):
        seen["shape"] = vecs.shape
        return classes

    out = local_tags.tag_asset(conn, aid, ocr_fn=ocr_fn, classify_fn=classify_fn, roster_loader=lambda s: ROSTER)
    assert out["engine"] == "local" and seen["shape"] == (4, 8) and len(seen["paths"]) == 4
    shots = {s["idx"]: s for s in out["shots"]}
    # 赛段按标题定（大奖赛回放 → 正赛）
    assert shots[0]["session"] == "正赛" and shots[0]["main"] == ["比赛中"] and shots[0]["drivers"] == ["诺里斯"]
    assert shots[0]["has_subtitles"]
    assert shots[1]["main"] == ["发布会"]
    assert shots[2]["has_logo"] and "SKY SPORTS" in shots[2]["logo_note"]
    assert shots[3]["main"] == ["观众"] and shots[3]["shot_type"] == "全景" and shots[3]["sub"] == []
    row = conn.execute("SELECT tags, has_subtitles, has_logo, tagged_at FROM assets WHERE id=?", (aid,)).fetchone()
    assert row["tagged_at"] and row["has_subtitles"] == 1 and row["has_logo"] == 1
    assert json.loads(row["tags"])[:4] == ["正赛", "比赛中", "发布会", "观众"]
    assert "诺里斯" in json.loads(row["tags"]) and "迈凯伦" in json.loads(row["tags"])


def test_tag_asset_main_and_sub_rules(conn, names):
    aid = _ready_asset(conn, 13)
    k = lambda i: f"/k/{aid}/{i}.jpg"  # noqa: E731
    texts = {
        k(0): [ln("FORMATION LAP")],
        k(3): [ln("LAP 1/70", x=0.02, y=0.05)],
        k(4): [ln("FIA SAFETY CAR", x=0.02, y=0.05)],
        k(7): [ln("VSC", x=0.02, y=0.05)],
        k(8): [ln("BATTLE FOR P2"), ln("NORRIS", y=0.4), ln("PIASTRI", y=0.5), ln("+0.150", x=0.6, y=0.5)],
        k(9): [ln("BATTLE FOR P2"), ln("PIASTRI", y=0.4), ln("NORRIS", y=0.5), ln("+0.400", x=0.6, y=0.5)],
        k(10): [ln("RACE CONTROL: RED FLAG"), ln("FIA STEWARDS: 5 SECOND TIME PENALTY", y=0.8)],
        k(11): [ln("CAR 44 STOPPED")],
        k(12): [ln("RACE CONTROL: RED FLAG")],
    }
    base = ["比赛中"] * 11 + ["领奖台", "数据图表"]
    out = local_tags.tag_asset(conn, aid, ocr_fn=lambda p: texts, roster_loader=lambda s: ROSTER,
                               classify_fn=lambda v: [(b, "跟拍") for b in base])
    got = {s["idx"]: (s["main"][0], s["sub"]) for s in out["shots"]}
    assert [got[i][0] for i in range(4)] == ["起步"] * 4           # 暖胎圈到第 1 圈之间都算起步
    assert [got[i][0] for i in range(4, 8)] == ["安全车"] * 4
    assert got[8] == ("比赛中", ["轮对轮", "缠斗"])
    assert got[9] == ("比赛中", ["超车", "缠斗"])            # 对战图上两人前后颠倒
    assert got[10] == ("红旗", ["调查处罚"])
    assert got[11] == ("领奖台", [])                       # 非赛道画面不被文字改写
    assert got[12] == ("数据图表", [])


def test_tag_asset_rejects_not_ready(conn):
    aid = _ready_asset(conn, 1)
    conn.execute("UPDATE assets SET state='processing' WHERE id=?", (aid,))
    with pytest.raises(ValueError):
        local_tags.tag_asset(conn, aid, ocr_fn=dict, classify_fn=list, roster_loader=lambda s: [])


def test_build_prototypes_learn_and_classify(conn, tmp_path, monkeypatch):
    aid = _ready_asset(conn, 7, sha="sha-proto")
    vec = {r["idx"]: np.frombuffer(r["clip_vec"], dtype=np.float16).astype(np.float32)
           for r in conn.execute("SELECT idx, clip_vec FROM asset_shots WHERE asset_id=?", (aid,))}
    labels = [("比赛中", "跟拍"), ("比赛中", "跟拍"), ("比赛中", "跟拍"), ("观众", "人物"), ("观众", "人物"),
              ("观众", "人物")]
    examples = [{"sha256": "sha-proto", "idx": i, "main": m, "shot_type": t} for i, (m, t) in enumerate(labels)]
    examples.append({"sha256": "sha-proto", "idx": 6, "main": "采访"})  # 只有 1 个样例的类不建
    examples.append({"sha256": "没有这个素材", "idx": 0, "main": "比赛中", "shot_type": "跟拍"})
    ex_file, out_file, learned = tmp_path / "ex.json", tmp_path / "p.npz", tmp_path / "data" / "learned.npz"
    ex_file.write_text(json.dumps({"examples": examples}, ensure_ascii=False))
    info = local_tags.build_prototypes(conn, ex_file, out_file)
    assert info["examples"] == 7 and info["missing"] == 1 and info["main"]["采访"] == 1
    monkeypatch.setattr(local_tags, "PROTOTYPES_FILE", out_file)
    monkeypatch.setattr(local_tags, "LEARNED_FILE", learned)
    m_names, _, t_names, _ = local_tags.prototypes()
    assert m_names == ["比赛中", "观众"] and t_names == ["人物", "跟拍"]
    got = local_tags.classify(np.stack([vec[i] for i in range(6)]))
    assert all(m in m_names and t in t_names and alt in m_names and alt != m for m, t, alt in got)

    # 用户把 #0、#1 改成采访，#6 也是采访 → 采访够 3 个，学到的原型里就有了，且优先于自带原型
    monkeypatch.setattr(local_tags, "EXAMPLES_FILE", ex_file)
    for i in (0, 1):
        assets.set_main_manual(conn, aid, i, ["采访"])
    info = local_tags.build_prototypes(conn, ex_file, learned, manual=True)
    assert info["main"] == {"比赛中": 1, "观众": 3, "采访": 3}
    assert local_tags.prototypes()[0] == ["观众", "采访"]


def test_shipped_prototypes_load():
    p = local_tags.PROTOTYPES_FILE
    m_names, m_mat, t_names, t_mat = local_tags._load(str(p), p.stat().st_mtime)
    assert set(m_names) >= {"比赛中", "发车格", "进站", "领奖台", "庆祝", "观众", "数据图表", "安全车"}
    assert set(m_names) <= set(assets.MAIN_TAGS)
    assert set(t_names) == set(assets.SHOT_TYPES)
    assert np.allclose(np.linalg.norm(m_mat, axis=1), 1, atol=1e-3) and t_mat.shape[0] == 7


def _tagged(conn, n=4):
    aid = _ready_asset(conn, n, sha=f"sha-tagged-{n}")
    mains = ["比赛中", "比赛中", "发车格", "观众"]
    shots = {i: {"teams": ["法拉利"] if i < 3 else [], "drivers": ["勒克莱尔"] if i == 2 else [],
                 "circuit": "布达佩斯", "session": "正赛", "main": [mains[i]], "sub": ["缠斗"] if i == 1 else [],
                 "shot_type": "全景", "has_subtitles": False, "has_logo": False, "logo_note": ""} for i in range(n)}
    assets.save_tags(conn, aid, shots)
    return aid


def test_manual_main_filter_and_groups(conn):
    aid = _tagged(conn)
    assert [s["shot_idx"] for s in assets.filter_shots(conn, ["发车格", "法拉利"])] == [2]
    assert [s["shot_idx"] for s in assets.filter_shots(conn, ["比赛中", "缠斗"])] == [1]
    assert assets.filter_shots(conn, ["观众", "法拉利"]) == []

    shot = assets.set_main_manual(conn, aid, 3, ["发车格", "观众", "发车格"])  # 改 + 加，重复的去掉
    assert shot["main_manual"] == ["发车格", "观众"] and shot["layers"]["主标签"] == ["发车格", "观众"]
    assert shot["tags"]["main"] == ["观众"]                                 # 自动结果还在
    assert [s["shot_idx"] for s in assets.filter_shots(conn, ["发车格"])] == [2, 3]
    card = assets.card(conn, aid)
    assert card["main_counts"] == [("比赛中", 2), ("发车格", 2), ("观众", 1)]
    groups = assets.tag_groups(conn)
    assert dict(groups["主标签"]) == {"比赛中": 2, "发车格": 2, "观众": 1}
    assert dict(groups["车队"]) == {"法拉利": 3} and dict(groups["子标签"]) == {"缠斗": 1}

    # 重新打标不覆盖手改
    _ = assets.save_tags(conn, aid, {3: {**card["shots"][3]["tags"], "main": ["庆祝"], "has_subtitles": False,
                                         "has_logo": False, "logo_note": ""}})
    assert assets.card(conn, aid)["shots"][3]["layers"]["主标签"] == ["发车格", "观众"]
    assert "庆祝" not in json.loads(conn.execute("SELECT tags FROM assets WHERE id=?", (aid,)).fetchone()[0])

    restored = assets.set_main_manual(conn, aid, 3, None)
    assert restored["main_manual"] is None and restored["layers"]["主标签"] == ["庆祝"]
    with pytest.raises(ValueError):
        assets.set_main_manual(conn, aid, 3, ["缠斗"])                      # 子标签不能当主标签
    with pytest.raises(KeyError):
        assets.set_main_manual(conn, aid, 99, ["观众"])


def test_search_by_tags_without_query(conn):
    _tagged(conn)
    got = library.search(conn, "", tags=["法拉利", "比赛中"])
    assert [s["shot_idx"] for s in got] == [0, 1] and got[0]["layers"]["赛段"] == ["正赛"]
    assert library.search(conn, "") == []


def test_old_scene_format_still_readable():
    layers = assets.shot_layers({"teams": ["红牛"], "scene": "维修区", "shot_type": "特写"})
    assert layers["主标签"] == ["维修区"] and layers["车队"] == ["红牛"] and layers["子标签"] == []


def test_set_main_learns(conn, monkeypatch, db_path):
    aid = _tagged(conn)
    learned = []
    monkeypatch.setattr(local_tags, "learn", lambda c: learned.append(1))
    connect = db.connect
    monkeypatch.setattr(library.db, "connect", lambda *a, **k: connect(db_path))
    shot = asyncio.run(library.set_main(conn, aid, 0, ["事故"]))
    assert shot["layers"]["主标签"] == ["事故"] and learned == [1]


def test_process_tags_locally_by_default(conn, monkeypatch, db_path):
    aid = _ready_asset(conn, 2)
    conn.execute("UPDATE asset_shots SET phash=1 WHERE asset_id=?", (aid,))
    assert library.pending(conn) == [aid]
    calls = []

    def fake_local(c, asset_id):
        calls.append(("local", asset_id))
        c.execute("UPDATE assets SET tagged_at=? WHERE id=?", (db.now(), asset_id))

    async def fake_gpt(c, asset_id):
        calls.append(("gpt", asset_id))

    monkeypatch.setattr(local_tags, "tag_asset", fake_local)
    monkeypatch.setattr(library.assets, "tag_asset", fake_gpt)
    connect = db.connect
    monkeypatch.setattr(library.db, "connect", lambda *a, **k: connect(db_path))
    assert asyncio.run(library.process(conn, aid)) == {"tags": "local"}
    assert library.pending(conn) == []
    assert asyncio.run(library.retag(conn, aid, "gpt"))["ok"] is True
    assert calls == [("local", aid), ("gpt", aid)]
    with pytest.raises(ValueError):
        library.schedule_retag(conn, aid, "别的")
