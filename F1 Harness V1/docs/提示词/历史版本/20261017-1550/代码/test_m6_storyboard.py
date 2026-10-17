"""画面段（2026-10-15 用户定）：口播按对象合成画面段、候选去冲突、程序补镜头、注水分时长、三项新硬检查、修不好转需人工。"""
import asyncio
import json

import pytest

from hermes import assets, codex_runner, db, edit, episodes, qa_checks as qa, steps, storyboard
from hermes.scheduler import NeedsHuman, Scheduler

ORIG_CONNECT = db.connect
DMAP = {"维斯塔潘": "红牛", "汉密尔顿": "法拉利", "勒克莱尔": "法拉利", "安东内利": "梅赛德斯", "拉塞尔": "梅赛德斯"}


def _seg(i, text, a, b):
    return {"i": i, "text": text, "show": text, "start": a, "end": b}


def _episode(conn, code="S1") -> int:
    return conn.execute("INSERT INTO episodes(code, branch, title, state, created_at, updated_at) "
                        "VALUES (?, '复盘', '测试', '配素材与粗剪', ?, ?)", (code, db.now(), db.now())).lastrowid


def _tags(main="维修区车库", drivers=(), teams=()):
    return {"teams": list(teams), "drivers": list(drivers), "circuit": None, "session": "正赛",
            "main": [main], "sub": [], "shot_type": "人物"}


def _asset(conn, shots, **cols) -> int:
    """shots: [(start, end, tags)]"""
    aid = conn.execute("INSERT INTO assets(source_type, title, width, height, created_at) VALUES "
                       "('测试', 'T', 1920, 1080, ?)", (db.now(),)).lastrowid
    for k, v in cols.items():
        conn.execute(f"UPDATE assets SET {k}=? WHERE id=?", (v, aid))
    for i, (s, e, tags) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags, has_subtitles, has_logo) "
                     "VALUES (?,?,?,?,?,0,0)", (aid, i, s, e, json.dumps(tags, ensure_ascii=False)))
    return aid


# ---------- 合成画面段 ----------

EP18 = [
    _seg(0, "先从红牛说起，维斯塔潘在排位拿到了杆位，", 0.0, 2.7),
    _seg(1, "赛车的竞争力已经有了表现。", 3.2, 4.6),
    _seg(2, "说到法拉利，车迷可能已经准备把策略老梗搬出来了，", 5.1, 7.8),
    _seg(3, "汉密尔顿错过入口后选择继续坚持。", 8.3, 9.5),
    _seg(4, "再听听勒克莱尔的评价，", 10.0, 11.0),
    _seg(5, "梅赛德斯这边，同样在研究怎么让赛车更好用，", 11.5, 13.7),
    _seg(6, "车队认为这次对比有帮助，", 14.2, 15.8),
    _seg(7, "安东内利的反馈偏积极，", 16.3, 17.7),
]


def test_beats_like_episode_18():
    bs = storyboard.beats(list(reversed(EP18)), 18.5, DMAP)
    assert [b["sents"] for b in bs] == [[0, 1], [2, 3], [4], [5, 6, 7]]
    assert [b["teams"] for b in bs] == [["红牛"], ["法拉利"], ["法拉利"], ["梅赛德斯"]]
    assert bs[0]["drivers"] == ["维斯塔潘"] and bs[3]["drivers"] == ["安东内利"]
    assert [(b["start"], b["end"]) for b in bs] == [(0.0, 5.1), (5.1, 10.0), (10.0, 11.5), (11.5, 18.5)]
    assert bs[1]["text"].startswith("说到法拉利") and bs[1]["text"].endswith("继续坚持。")
    assert [b["i"] for b in bs] == [0, 1, 2, 3] and bs[1]["key"] is False and bs[0]["event"] == []


def test_beats_turn_and_max_length():
    # 转场词开头但当前段不到 3 秒不切；没对象的段接过下一句的对象
    bs = storyboard.beats([_seg(0, "开场。", 0.5, 1.5), _seg(1, "接下来聊聊。", 2.0, 3.0),
                           _seg(2, "拉塞尔这站不错。", 3.5, 5.0)], 6.0, DMAP)
    assert len(bs) == 1 and bs[0]["teams"] == ["梅赛德斯"] and bs[0]["start"] == 0.0 and bs[0]["end"] == 6.0
    # 超过 BEAT_MAX 在句子边界切开，切出来的沿用原对象
    segs = [_seg(0, "红牛这站回升了。", 0.0, 2.5)] + [_seg(k, "继续说说。", 3.0 * k, 3.0 * k + 2.5) for k in range(1, 7)]
    bs = storyboard.beats(segs, 21.0, DMAP)
    assert len(bs) == 2 and all(b["teams"] == ["红牛"] for b in bs)
    assert all(b["end"] - b["start"] <= storyboard.BEAT_MAX for b in bs) and bs[1]["start"] == 12.0


def test_team_key_and_subject():
    assert storyboard.team_key("梅奔") == "梅赛德斯" and storyboard.team_key("木瓜") == "迈凯伦"
    assert storyboard.team_key("mercedes") == "梅赛德斯" and storyboard.team_key("Red Bull Racing") == "红牛"
    assert storyboard.team_key("小红牛") == "Racing Bulls" and storyboard.team_key("索伯") == storyboard.team_key("奥迪")
    assert storyboard.team_key(None) is None and storyboard.team_key("xyz") is None
    assert storyboard.subject("拉塞尔和汉密尔顿都提到梅奔", DMAP) == (sorted(["梅赛德斯", "法拉利"]), ["拉塞尔", "汉密尔顿"])
    assert storyboard.subject("小红牛表现不错", {}) == (["Racing Bulls"], [])


def test_conflict_and_on_subject():
    beat = {"teams": ["梅赛德斯"]}
    leclerc = {"teams": [], "drivers": ["勒克莱尔"], "team": None}
    merc_ops = {"teams": [], "drivers": [], "team": "梅赛德斯"}
    aerial = {"teams": [], "drivers": [], "team": None}
    both = {"teams": ["法拉利", "Mercedes"], "drivers": [], "team": None}
    assert storyboard.conflict(beat, leclerc, DMAP) and not storyboard.on_subject(beat, leclerc, DMAP)
    assert storyboard.on_subject(beat, merc_ops, DMAP) and not storyboard.conflict(beat, merc_ops, DMAP)
    assert not storyboard.conflict(beat, aerial, DMAP) and not storyboard.on_subject(beat, aerial, DMAP)
    assert storyboard.on_subject(beat, both, DMAP) and not storyboard.conflict(beat, both, DMAP)
    assert not storyboard.conflict({"teams": []}, leclerc, DMAP) and not storyboard.on_subject({"teams": []}, merc_ops, DMAP)
    assert storyboard.shot_teams(leclerc, DMAP) == {"法拉利"}


def test_driver_teams_majority(conn):
    a = _asset(conn, [(0, 1, _tags(drivers=["汉密尔顿"], teams=["法拉利"])), (1, 2, _tags(drivers=["汉密尔顿"], teams=["Ferrari"])),
                      (2, 3, _tags(drivers=["汉密尔顿", "勒克莱尔"], teams=["法拉利", "梅奔"])),   # 两支车队不算
                      (3, 4, _tags(drivers=["拉塞尔"], teams=["Mercedes"]))])
    b = _asset(conn, [(0, 1, _tags(drivers=["汉密尔顿"], teams=["梅赛德斯"]))])
    assert storyboard.driver_teams(conn, [a, b]) == {"汉密尔顿": "法拉利", "拉塞尔": "梅赛德斯"}
    assert storyboard.driver_teams(conn, [b]) == {"汉密尔顿": "梅赛德斯"}
    assert storyboard.driver_teams(conn) == {"汉密尔顿": "法拉利", "拉塞尔": "梅赛德斯"}
    assert storyboard.driver_teams(conn, []) == {}


# ---------- 程序补镜头、注水分时长、片尾 ----------

def _shot(aid, idx, a, b, **kw):
    return {"asset_id": aid, "shot_idx": idx, "start": a, "end": b, "teams": [], "drivers": [], "team": None,
            "key": False, "race": "未知", **kw}


def test_autofill_only_uncovered_beats():
    beats = [{"i": 0, "start": 0.0, "end": 7.0, "teams": ["梅赛德斯"], "event": []},
             {"i": 1, "start": 7.0, "end": 10.0, "teams": [], "event": []},
             {"i": 2, "start": 10.0, "end": 12.0, "teams": [], "event": []}]
    s = [_shot(1, 0, 0, 3), _shot(1, 1, 3, 6, drivers=["勒克莱尔"]), _shot(1, 2, 6, 9, key=True),
         _shot(1, 3, 9, 11), _shot(1, 4, 11, 14, team="梅赛德斯"), _shot(1, 5, 14, 20), _shot(1, 6, 20, 30)]
    cands = {"shots": s, "race": None, "segments": [
        {"i": 0, "candidates": [s[0], s[1], s[2], s[3], s[4], s[5]]},
        {"i": 1, "candidates": [s[6]]}, {"i": 2, "candidates": [s[6], s[5]]}]}
    cuts = [{"segment": 0, "asset_id": 1, "shot_idx": 0, "src_in": -5, "src_out": 2.5, "reason": "r"},  # 镜头内只有 2.5 秒
            {"segment": 1, "asset_id": 1, "shot_idx": 6, "src_in": 20, "src_out": 23, "reason": "r"}]
    warns = []
    out = edit._autofill(beats, cuts, cands, DMAP, warns)
    added = [(c["segment"], c["shot_idx"], c["src_in"], c["src_out"]) for c in out[2:]]
    # 段 0：拍到对象的 #4 先补，再按顺序补 #3；冲突的 #1、关键的 #2、已用的 #0 不补；段 1 已盖满不动；段 2 没镜头，补没用过的 #5
    assert added == [(0, 4, 11, 14), (0, 3, 9, 11), (2, 5, 14, 20)]
    assert out[:2] == cuts
    assert warns == ["画面段 0 程序补了 素材1#4、素材1#3", "画面段 2 程序补了 素材1#5"]


def test_autofill_reaches_whole_library():
    """前 12 个候选被别的段用光时，按整库排序补；短于 1.2 秒、关键、冲突的不补（2026-10-15 第 18 期 GPT 交空 cuts）。"""
    beats = [{"i": 0, "start": 0.0, "end": 5.0, "teams": ["法拉利"], "event": []},
             {"i": 1, "start": 5.0, "end": 10.0, "teams": ["法拉利"], "event": []}]
    s = [_shot(1, 0, 0, 6, teams=["法拉利"]), _shot(1, 1, 6, 6.5, teams=["法拉利"]), _shot(1, 2, 7, 13, key=True, teams=["法拉利"]),
         _shot(1, 3, 13, 19, drivers=["拉塞尔"]), _shot(1, 4, 19, 25), _shot(1, 5, 25, 31, teams=["法拉利"])]
    cands = {"shots": s, "race": None, "segments": [
        {"i": 0, "candidates": [s[0]], "ranked": [(1, 0), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5)]},
        {"i": 1, "candidates": [s[0]], "ranked": [(1, 0), (1, 1), (1, 2), (1, 4), (1, 5)]}]}
    warns = []
    out = edit._autofill(beats, [], cands, DMAP, warns)
    assert [(c["segment"], c["shot_idx"]) for c in out] == [(0, 0), (1, 5)]
    assert warns == ["画面段 0 程序补了 素材1#0", "画面段 1 程序补了 素材1#5"]


def test_autofill_skips_same_footage():
    beats = [{"i": 0, "start": 0.0, "end": 5.0, "teams": [], "event": []}]
    s = [_shot(1, 0, 0, 6), _shot(1, 1, 6, 12)]
    cands = {"shots": s, "race": None, "segments": [{"i": 0, "candidates": [s[0], s[1]]}]}
    cuts = [{"segment": 9, "asset_id": 2, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": "r"}]
    seen = []
    same = lambda c, picked: seen.append(list(picked)) or c["shot_idx"] == 0
    out = edit._autofill(beats, cuts, cands, DMAP, [], same)
    assert [(c["segment"], c["shot_idx"]) for c in out[1:]] == [(0, 1)]
    assert seen[0] == [] and seen[1] == []   # 素材 2 不在镜头清单，范围算不出，不进已选


def test_dedupe_keeps_first_use():
    s = [_shot(1, 0, 0, 6), _shot(1, 1, 6, 12), _shot(2, 0, 0, 6)]
    cands = {"shots": s, "segments": []}
    cuts = [{"segment": 0, "asset_id": 1, "shot_idx": 0, "src_in": 0, "src_out": 3},
            {"segment": 1, "asset_id": 1, "shot_idx": 1, "src_in": 6, "src_out": 9},
            {"segment": 2, "asset_id": 1, "shot_idx": 0, "src_in": 3, "src_out": 6},     # 同一镜头第二次
            {"segment": 3, "asset_id": 2, "shot_idx": 0, "src_in": 0, "src_out": 4},     # 指纹看起来一样
            {"segment": 4, "asset_id": "x", "shot_idx": 0}]                              # 认不出的留给后面报错
    same = lambda c, picked: c["asset_id"] == 2
    warns = []
    out = edit._dedupe(cuts, cands, warns, same)
    assert out == [cuts[0], cuts[1], cuts[4]]
    assert warns == ["画面段 2 删了重复的 素材1#0", "画面段 3 删了重复的 素材2#0"]


def test_fill_caps_and_redistributes():
    assert edit._fill(10, [1, 1], [2, 100]) == [2, 8]
    assert edit._fill(6, [2, 1], [100, 100]) == [4, 2]
    out = edit._fill(10, [1, 1], [2, 3])           # 全顶到上限还差：按上限比例摊
    assert abs(sum(out) - 10) < 1e-9 and out[0] < out[1]


@pytest.fixture
def lib(conn, monkeypatch):
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **k: [])
    x = _asset(conn, [(0.0, 10.0, _tags(drivers=["勒克莱尔"], teams=["法拉利"])),
                      (10.0, 20.0, _tags(drivers=["拉塞尔"], teams=["梅赛德斯"])),
                      (20.0, 22.0, _tags("航拍")), (22.0, 40.0, _tags("航拍"))])
    return x


def test_water_filling_never_overruns_shot(conn, lib):
    x = lib
    align = {"duration": 8.0, "segments": [_seg(0, "赛道很难跑。", 0.3, 7.5)]}
    beats = storyboard.beats(align["segments"], 8.0, {})
    cuts = [{"segment": 0, "asset_id": x, "shot_idx": 2, "src_in": 20, "src_out": 22, "reason": "短"},
            {"segment": 0, "asset_id": x, "shot_idx": 3, "src_in": 22, "src_out": 24, "reason": "长"}]
    warns = []
    tl = edit.build_timeline(conn, "/tmp", align, cuts, warns, beats=beats)
    v = tl["video"]
    assert v[0]["start"] == 0.0 and v[-1]["end"] == 8.0 and all(c["beat"] == 0 == c["segment"] for c in v)
    assert all(c["src_out"] - c["src_in"] >= c["end"] - c["start"] - 1 / 30 for c in v)
    assert v[0]["end"] - v[0]["start"] <= 2.0 + 1e-9 and v[1]["end"] - v[1]["start"] >= 6.0
    assert not any("定格" in w for w in warns)
    assert tl["beats"] == [{"i": 0, "start": 0.0, "end": 8.0, "teams": [], "drivers": [], "text": "赛道很难跑。",
                            "key": False}]
    assert qa.beat_items(conn, _episode(conn), v, tl["beats"], 1 / 30)[1]["ok"]


def test_usable_end_clamps_shots(conn, monkeypatch):
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **k: [])
    y = _asset(conn, [(0.0, 50.0, _tags()), (50.0, 95.0, _tags()), (95.0, 100.0, _tags())],
               duration=100.0, source_url="https://www.youtube.com/watch?v=x")
    stop = assets.usable_end(assets._asset(conn, y))
    assert stop is not None and 50 < stop < 95
    got = {s["shot_idx"]: s for s in edit.load_shots(conn, [y])}
    assert set(got) == {0, 1} and got[1]["end"] == round(stop, 3)
    align = {"duration": 40.0, "segments": [_seg(0, "一段话。", 0.0, 40.0)]}
    beats = storyboard.beats(align["segments"], 40.0, {})
    warns = []
    tl = edit.build_timeline(conn, "/tmp", align, [
        {"segment": 0, "asset_id": y, "shot_idx": 1, "src_in": 60, "src_out": 95, "reason": ""},
        {"segment": 0, "asset_id": y, "shot_idx": 2, "src_in": 95, "src_out": 100, "reason": "片尾"}], warns, beats=beats)
    assert len(tl["video"]) == 1 and tl["video"][0]["src_out"] <= stop + 1e-6
    assert any("片尾" in w for w in warns)


# ---------- 新硬检查 ----------

def test_beat_items(conn, lib):
    x, eid = lib, _episode(conn)
    beats = [{"i": 0, "start": 0.0, "end": 4.0, "teams": ["梅赛德斯"]}, {"i": 1, "start": 4.0, "end": 6.0, "teams": []}]

    def clip(idx, a, b, s, e, beat=0):
        return {"asset_id": x, "shot_idx": idx, "src_in": a, "src_out": b, "start": s, "end": e, "beat": beat}
    items = {i["name"]: i for i in qa.beat_items(conn, eid, [clip(0, 0, 4, 0, 4), clip(2, 20, 21, 4, 6)], beats, 1 / 30)}
    assert all(i["hard"] for i in items.values())
    assert not items["画面段缺镜头"]["ok"] and "画面段 1" in items["画面段缺镜头"]["detail"]
    assert not items["镜头不够长"]["ok"] and "原片只有 1.00 秒，要放 2.00 秒" in items["镜头不够长"]["detail"]
    assert not items["对象不符"]["ok"]
    assert f"画面段0（讲梅赛德斯）用了 素材{x}#0（法拉利）" in items["对象不符"]["detail"]
    # 没冲突但一个都没拍到对象，库里明明有
    items = {i["name"]: i for i in qa.beat_items(conn, eid, [clip(3, 22, 26, 0, 4), clip(2, 20, 22, 4, 6, 1)], beats, 1 / 30)}
    assert items["画面段缺镜头"]["ok"] and items["镜头不够长"]["ok"]
    assert f"画面段0（讲梅赛德斯）没有一个镜头拍到梅赛德斯，库里有 素材{x}#1" in items["对象不符"]["detail"]
    items = {i["name"]: i for i in qa.beat_items(conn, eid, [clip(1, 10, 14, 0, 4), clip(2, 20, 22, 4, 6, 1)], beats, 1 / 30)}
    assert all(i["ok"] for i in items.values())
    # 没有画面段的旧时间线：只查镜头不够长
    items = {i["name"]: i for i in qa.beat_items(conn, eid, [clip(2, 20, 21, 0, 2)], None, 1 / 30)}
    assert items["画面段缺镜头"]["ok"] and items["对象不符"]["ok"] and not items["镜头不够长"]["ok"]


# ---------- decide ----------

def _ep_dir(tmp_path, align):
    d = tmp_path / "ep"
    (d / "03_配音").mkdir(parents=True)
    (d / "03_配音" / "对齐.json").write_text(json.dumps(align, ensure_ascii=False), encoding="utf-8")
    return d


def _beat_checks(conn, eid, tl):
    clips = sorted(tl["video"], key=lambda c: c["start"])
    return qa.beat_items(conn, eid, clips, tl.get("beats"), 1 / 30)


MERC = {"duration": 10.0, "segments": [_seg(0, "梅赛德斯这边，拉塞尔表现不错。", 0.2, 3.8),
                                      _seg(1, "接下来看看赛道本身。", 4.3, 9.6)]}


def test_decide_autofills_too_few_cuts(conn, lib, tmp_path, monkeypatch):
    x, ep = lib, _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    monkeypatch.setattr(edit, "_checks", _beat_checks)
    prompts = []

    async def runner(prompt, model, effort, workdir, **kw):   # 只给第 0 段一个短镜头
        prompts.append(prompt)
        return codex_runner.CodexResult("t", json.dumps({"cuts": [
            {"segment": 0, "asset_id": x, "shot_idx": 1, "src_in": 10, "src_out": 11, "reason": "拉塞尔"}], "gaps": []}), {})
    d = _ep_dir(tmp_path, MERC)
    res = asyncio.run(edit.decide(conn, ep, d, asset_ids=[x], branch="复盘", runner=runner))
    assert res["attempts"] == 1 and res["problems"] == []
    assert [b["teams"] for b in res["beats"]] == [["梅赛德斯"], []]
    assert any(w.startswith("画面段 0 程序补了") for w in res["warnings"])
    assert any(w.startswith("画面段 1 程序补了") for w in res["warnings"])
    v = res["timeline"]["video"]
    assert {c["beat"] for c in v} == {0, 1} and (x, 0) not in {(c["asset_id"], c["shot_idx"]) for c in v if c["beat"] == 0}  # 法拉利不补进梅赛德斯段
    assert all(c["src_out"] - c["src_in"] >= c["end"] - c["start"] - 1 / 30 for c in v)
    board = json.loads((d / "06_合成" / "分镜.json").read_text(encoding="utf-8"))
    assert [len(b["clips"]) > 0 for b in board["beats"]] == [True, True] and board["gaps"] == []
    assert (d / "06_合成" / "_剪辑决策" / "回复_第1次.json").exists()
    cands = edit.candidates(conn, [x], res["beats"], dmap=storyboard.driver_teams(conn, [x]))
    c0 = cands["segments"][0]["candidates"]
    # 标签和对象冲突的（法拉利 #0）不再去掉，降分排最后，交给看图判断（2026-10-16）
    assert (c0[0]["asset_id"], c0[0]["shot_idx"]) == (x, 1) and c0[-1]["shot_idx"] == 0
    assert "[0] 0.00-4.30 (4.3s) 对象:梅赛德斯(拉塞尔) 梅赛德斯这边" in edit.build_context(conn, cands, [x])


def test_decide_gives_up_with_problems(conn, lib, tmp_path, monkeypatch):
    x, ep = lib, _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    monkeypatch.setattr(edit, "_checks", _beat_checks)
    prompts = []

    async def runner(prompt, model, effort, workdir, **kw):   # 一直在梅赛德斯段放勒克莱尔
        prompts.append(prompt)
        return codex_runner.CodexResult("t", json.dumps({"cuts": [
            {"segment": 0, "asset_id": x, "shot_idx": 0, "src_in": 0, "src_out": 5, "reason": "勒克莱尔"},
            {"segment": 1, "asset_id": x, "shot_idx": 3, "src_in": 22, "src_out": 30, "reason": "航拍"}], "gaps": []}), {})
    d = _ep_dir(tmp_path, MERC)
    res = asyncio.run(edit.decide(conn, ep, d, asset_ids=[x], branch="复盘", runner=runner))
    assert res["attempts"] == edit.MAX_FIX_ROUNDS + 1 == 3
    assert [p["name"] for p in res["problems"]] == ["对象不符"]
    assert "对象不符" in prompts[1] and "对象不符" in prompts[2]
    assert all((d / "06_合成" / "_剪辑决策" / f"回复_第{k}次.json").exists() for k in (1, 2, 3))


def test_decide_keeps_best_round(conn, lib, tmp_path, monkeypatch):
    """修了几轮取硬问题最少的一版：第 1 轮 1 个、后两轮各 2 个，用第 1 轮的。"""
    x, ep = lib, _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    calls = []

    def checks(conn, eid, tl):
        calls.append(tl)
        n = 1 if len(calls) == 1 else 2
        return [{"name": f"问题{k}", "ok": False, "hard": True, "detail": ""} for k in range(n)]
    monkeypatch.setattr(edit, "_checks", checks)

    async def runner(prompt, model, effort, workdir, **kw):
        return codex_runner.CodexResult("t", json.dumps({"cuts": [
            {"segment": 0, "asset_id": x, "shot_idx": 1, "src_in": 10, "src_out": 15, "reason": "拉塞尔"},
            {"segment": 1, "asset_id": x, "shot_idx": 3, "src_in": 22, "src_out": 30, "reason": "航拍"}], "gaps": []}), {})
    res = asyncio.run(edit.decide(conn, ep, _ep_dir(tmp_path, MERC), asset_ids=[x], branch="复盘", runner=runner))
    assert res["attempts"] == 3 and [p["name"] for p in res["problems"]] == ["问题0"]
    assert res["timeline"] is calls[0]
    assert "剪辑决策用了第 1 轮的结果（共 3 轮，这一轮硬问题最少）" in res["warnings"]


# ---------- 修不好转需人工 ----------

@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="新加坡大奖赛", title="新加坡复盘")
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def test_edit_decide_raises_needs_human(conn, ep, monkeypatch):
    saved = []

    async def decide(*a, **k):
        return {"timeline": {"video": [{}], "gaps": []}, "attempts": 3, "warnings": [],
                "problems": [{"name": "对象不符", "detail": "画面段3（讲梅赛德斯）用了 素材19#4（法拉利）\n第二行"}]}
    monkeypatch.setattr(edit, "decide", decide)
    monkeypatch.setattr(edit, "save", lambda c, eid, d, tl, *, stage_run, preview=True: saved.append(preview) or
                        {"hard_failed": True})
    with pytest.raises(NeedsHuman) as e:
        asyncio.run(steps.edit_decide({"id": 0, "step": "D6剪辑决策"}, ep))
    msg = str(e.value)
    assert saved == [False] and "\n" not in msg
    assert msg.startswith("剪辑决策修了 2 轮仍有硬问题，没有渲染：对象不符：画面段3（讲梅赛德斯）用了 素材19#4（法拉利）")


def test_scheduler_needs_human_fails_without_retry(conn, db_path):
    async def ok(task, ep):
        return {}

    async def human(task, ep):
        raise NeedsHuman("剪辑决策修了 2 轮仍有硬问题，没有渲染：对象不符：画面段3")

    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="巴林大奖赛", title="t")
    sched = Scheduler(lambda: ORIG_CONNECT(db_path), {"*": ok, "D1资料与事实": human},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    async def drive():
        for _ in range(300):
            await sched.tick()
            await asyncio.sleep(0.001)
            if conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0] == "需人工":
                return
        raise AssertionError("没到需人工")
    asyncio.run(drive())
    t = conn.execute("SELECT * FROM tasks WHERE episode_id=? AND step='D1资料与事实'", (eid,)).fetchone()
    assert (t["state"], t["attempts"]) == ("failed", 1) and t["error"].startswith("剪辑决策修了 2 轮")
    card = json.loads(conn.execute("SELECT card FROM messages WHERE episode_id=? AND role='card' ORDER BY id DESC",
                                   (eid,)).fetchone()[0])
    assert card["type"] == "failed" and card["needs_human"] is True
    assert card["steps"] == [{"step": "D1资料与事实", "error": "剪辑决策修了 2 轮仍有硬问题，没有渲染：对象不符：画面段3"}]


# ---------- 看图核对（2026-10-15） ----------

def test_leads_only_main_team():
    bt = {"teams": ["法拉利"]}
    assert storyboard.leads(bt, _shot(1, 0, 0, 5, teams=["法拉利"]), DMAP)
    assert storyboard.leads(bt, _shot(1, 0, 0, 5, teams=["法拉利", "迈凯伦"]), DMAP)
    assert not storyboard.leads(bt, _shot(1, 0, 0, 5, teams=["梅赛德斯", "红牛", "法拉利"]), DMAP)
    assert storyboard.leads(bt, _shot(1, 0, 0, 5, drivers=["汉密尔顿", "拉塞尔"]), DMAP)
    assert not storyboard.leads({"teams": []}, _shot(1, 0, 0, 5, teams=["法拉利"]), DMAP)


def test_autofill_prefers_lead_team():
    """多车同框只沾到对象的排在对象是主角的后面（期 18 冲刺全景标了法拉利，实际是梅奔在前）。"""
    beats = [{"i": 0, "start": 0.0, "end": 5.0, "teams": ["法拉利"], "event": []}]
    s = [_shot(1, 0, 0, 6, teams=["梅赛德斯", "法拉利"]), _shot(1, 1, 6, 12, teams=["法拉利"])]
    cands = {"shots": s, "race": None, "segments": [{"i": 0, "candidates": [s[0], s[1]]}]}
    out = edit._autofill(beats, [], cands, DMAP, [])
    assert [c["shot_idx"] for c in out] == [1]


def test_autofill_ops_before_race_and_session_matches():
    """2026-10-15 用户定：素材不够先用足车队运营视频，比赛画面后补；口播没讲冲刺、排位、练习时，那几节的画面放最后。"""
    beats = [{"i": 0, "start": 0.0, "end": 14.0, "teams": ["法拉利"], "event": [], "text": "法拉利这次策略又慢了半拍"}]
    s = [_shot(1, 0, 0, 5, teams=["法拉利"], category="集锦", session="冲刺"),
         _shot(1, 1, 5, 10, teams=["法拉利"], category="集锦", session="正赛"),
         _shot(2, 0, 0, 5, teams=["法拉利"], category="车队运营", team="法拉利"),
         _shot(2, 1, 5, 10, teams=["法拉利"], category="车队运营", team="法拉利")]
    cands = {"shots": s, "race": None, "segments": [{"i": 0, "candidates": s}]}
    out = edit._autofill(beats, [], cands, DMAP, [])
    assert [(c["asset_id"], c["shot_idx"]) for c in out] == [(2, 0), (2, 1), (1, 1)]
    assert edit.off_session(s[0], "法拉利策略") and not edit.off_session(s[0], "冲刺赛里法拉利")
    assert not edit.off_session(s[1], "法拉利策略") and not edit.off_session({"session": None}, "")
    sh = {"names": [], "main": [], "has_subtitles": False, "has_logo": False, "duration": 3, "cross_used": False,
          "race": "本站", "session": "练习", "category": "集锦", "team": None}
    gap = edit._score(sh, "一练里法拉利很快", 0.5) - edit._score(sh, "法拉利很快", 0.5)
    assert gap == pytest.approx(edit.OFF_SESSION_PENALTY)


def test_frame_check_flags_and_caches(conn, tmp_path):
    from hermes import frame_check
    from hermes.assets import FFMPEG
    import subprocess
    src = tmp_path / "a.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "4", "-y", str(src)],
                   check=True)
    x = _asset(conn, [(0.0, 2.0, _tags(teams=["法拉利"])), (2.0, 4.0, _tags(teams=["红牛"]))], local_path=str(src))
    beats = [{"i": 0, "text": "说到法拉利", "teams": ["法拉利"], "drivers": []}]
    tl = {"video": [{"asset_id": x, "shot_idx": 0, "src_in": 0.0, "src_out": 2.0, "beat": 0},
                    {"asset_id": x, "shot_idx": 1, "src_in": 2.0, "src_out": 4.0, "beat": 0}]}
    calls = []

    async def runner(prompt, model, effort, workdir, **kw):
        calls.append((prompt, kw["images"]))
        return codex_runner.CodexResult("t", json.dumps({"clips": [
            {"n": 1, "seen": "法拉利赛车", "ok": True, "why": ""},
            {"n": 2, "seen": "红牛赛车", "ok": False, "why": "是红牛不是法拉利"}]}), {})
    shots = edit.load_shots(conn, [x])
    res = asyncio.run(frame_check.check(conn, tl, beats, tmp_path, run=runner, shots=shots))
    assert res["checked"] == 2 and res["error"] is None
    assert [(c["shot_idx"], c["why"]) for c in res["bad"]] == [(1, "是红牛不是法拉利")]
    prompt, images = calls[0]
    assert f"- 编号 2｜画面段 0｜对象：法拉利｜口播：说到法拉利｜素材{x}#1 正赛 标签：红牛、维修区车库、人物" in prompt
    assert len(images) == 1 and images[0].exists()
    res = asyncio.run(frame_check.check(conn, tl, beats, tmp_path, run=runner, shots=shots))   # 核对过的不重看
    assert len(calls) == 1 and res["checked"] == 0 and len(res["bad"]) == 1


def test_decide_look_swaps_bad_clip(conn, lib, tmp_path, monkeypatch):
    """看图不通过的镜头删掉、整库重补，不再补回来。"""
    x, ep = lib, _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    monkeypatch.setattr(edit, "_checks", _beat_checks)
    looks = []

    async def check(conn, timeline, beats, workdir, **kw):
        looks.append([(c["asset_id"], c["shot_idx"], c["beat"]) for c in timeline["video"]])
        bad = [{**c, "seen": "其实是法拉利", "why": "不是梅赛德斯"} for c in timeline["video"]
               if c["shot_idx"] == 1 and len(looks) == 1]
        return {"bad": bad, "checked": len(timeline["video"]), "error": None}
    monkeypatch.setattr(edit.frame_check, "check", check)

    async def runner(prompt, model, effort, workdir, **kw):
        return codex_runner.CodexResult("t", json.dumps({"cuts": [
            {"segment": 0, "asset_id": x, "shot_idx": 1, "src_in": 10, "src_out": 15, "reason": "拉塞尔"},
            {"segment": 1, "asset_id": x, "shot_idx": 3, "src_in": 22, "src_out": 30, "reason": "航拍"}], "gaps": []}), {})
    res = asyncio.run(edit.decide(conn, ep, _ep_dir(tmp_path, MERC), asset_ids=[x], branch="复盘", runner=runner,
                                  look=True))
    assert len(looks) == 2 and (x, 1, 0) in looks[0]
    assert all(c["shot_idx"] != 1 for c in res["timeline"]["video"])
    assert f"画面段 0 看图不对，换掉 素材{x}#1：不是梅赛德斯" in res["warnings"]


# ---------- 看图选镜头（2026-10-16 用户定：看图为主、标签只作辅助） ----------

def test_pick_pool_round_robin():
    shots = [_shot(1, i, i, i + 1) for i in range(6)]
    segs = [{"candidates": [shots[0], shots[1], shots[2]]}, {"candidates": [shots[0], shots[3], shots[4]]}]
    assert [s["shot_idx"] for s in edit.pick_pool(segs, shots, per=2, limit=10)] == [0, 1, 3]
    assert [s["shot_idx"] for s in edit.pick_pool(segs, shots, per=3, limit=4)] == [0, 1, 2, 3]


def test_candidate_sheets_and_look_fields(conn, tmp_path):
    from PIL import Image
    from hermes import frame_check
    from hermes.assets import FFMPEG
    import subprocess
    src = tmp_path / "a.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "4", "-y", str(src)],
                   check=True)
    x = _asset(conn, [(0.0, 2.0, _tags(teams=["法拉利"])), (2.0, 4.0, _tags(teams=["红牛"]))], local_path=str(src))
    shots = edit.load_shots(conn, [x])
    out = tmp_path / "pick"
    imgs = frame_check.candidate_sheets(shots, frame_check._sources(conn, shots), out)
    assert [p.name for p in imgs] == ["候选_01.jpg"]
    w, h = frame_check.PICK_FRAME
    with Image.open(imgs[0]) as im:
        assert im.size == (3 * w, 2 * (h + frame_check.LABEL_H))
    assert frame_check.candidate_sheets(shots, {}, out) == [] and not list(out.glob("候选_*.jpg"))
    beats = [{"i": 0, "text": "说到法拉利", "teams": ["法拉利"], "drivers": []}]
    tl = {"video": [{"asset_id": x, "shot_idx": 0, "src_in": 0.0, "src_out": 2.0, "beat": 0}]}

    async def runner(prompt, model, effort, workdir, **kw):
        return codex_runner.CodexResult("t", json.dumps({"clips": [
            {"n": 1, "seen": "法拉利赛车在赛道上", "ok": True, "why": "", "live": True, "key": False, "card": False,
             "marks": ["右上", "中间"]}]}), {})
    res = asyncio.run(frame_check.check(conn, tl, beats, tmp_path, run=runner, shots=shots))
    v = res["looks"][frame_check.clip_key(tl["video"][0], beats[0])]
    assert v["live"] and not v["key"] and not v["card"] and v["marks"] == ["右上"]


def test_decide_look_picks_from_images_and_repicks(conn, lib, tmp_path, monkeypatch):
    """看图时候选拼图附给 GPT；看图不通过的让 GPT 看图重选；看图结论写进时间线，QA 信看图不信标签。"""
    x, ep = lib, _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    monkeypatch.setattr(edit, "_checks", _beat_checks)
    sheet = tmp_path / "候选_01.jpg"
    sheet.write_bytes(b"x")
    pools = []
    monkeypatch.setattr(edit.frame_check, "candidate_sheets", lambda shots, src, out: pools.append(shots) or [sheet])

    async def check(conn, timeline, beats, workdir, **kw):
        by, looks, bad = {b["i"]: b for b in beats}, {}, []
        for c in timeline["video"]:
            card = c["shot_idx"] == 1
            v = {"ok": not card, "seen": "联名标题卡" if card else "梅奔车库", "why": "品牌标题卡" if card else "",
                 "live": False, "key": False, "card": card, "marks": ["右上"] if c["shot_idx"] == 3 else []}
            looks[edit.frame_check.clip_key(c, by.get(c["beat"]))] = v
            if not v["ok"]:
                bad.append({**c, **v})
        return {"bad": bad, "checked": len(timeline["video"]), "error": None, "looks": looks}
    monkeypatch.setattr(edit.frame_check, "check", check)
    calls = []

    async def runner(prompt, model, effort, workdir, **kw):
        calls.append((prompt, kw.get("images")))
        first = {"segment": 0, "asset_id": x, "shot_idx": 1, "src_in": 10, "src_out": 15, "reason": "拉塞尔"}
        again = {"segment": 0, "asset_id": x, "shot_idx": 0, "src_in": 0, "src_out": 5, "reason": "看图是梅奔车库"}
        return codex_runner.CodexResult("t", json.dumps({"cuts": [
            first if len(calls) == 1 else again,
            {"segment": 1, "asset_id": x, "shot_idx": 3, "src_in": 22, "src_out": 30, "reason": "航拍"}], "gaps": []}), {})
    res = asyncio.run(edit.decide(conn, ep, _ep_dir(tmp_path, MERC), asset_ids=[x], branch="复盘", runner=runner,
                                  look=True))
    assert len(calls) == 2 and calls[0][1] == [sheet] == calls[1][1] and pools and pools[0]
    assert "看图核对不通过" in calls[1][0] and f"不能再用：素材{x}#1" in calls[1][0]
    assert f"画面段 0 看图不对，换掉 素材{x}#1：品牌标题卡" in res["warnings"]
    v = sorted(res["timeline"]["video"], key=lambda c: c["start"])
    assert [c["shot_idx"] for c in v] == [0, 3] and all(c["look"]["ok"] and c["look"]["k"] for c in v)
    assert edit.crop_hits(v[1]["crop"], 1920, 1080, "右上角") == []          # 看出的水印角并进裁剪
    assert res["problems"] == []                                               # 标签说 #0 是法拉利，看图认过是梅奔
    tl = res["timeline"]
    assert qa.look_item(qa.with_looks(tl, v))["ok"]
    moved = qa.with_looks(tl, [dict(c, src_in=c["src_in"] + 0.5) for c in v])   # 改过入出点的看图结论作废
    assert not any("look" in c for c in moved)
    assert not _beat_checks(conn, ep, dict(tl, video=moved))[2]["ok"]


def test_qa_prefers_look_over_tags(conn, lib):
    from hermes import frame_check
    x, eid = lib, _episode(conn)
    beats = [{"i": 0, "start": 0.0, "end": 4.0, "teams": ["梅赛德斯"], "drivers": [], "text": "梅奔"}]
    c = {"asset_id": x, "shot_idx": 0, "src_in": 0.0, "src_out": 4.0, "start": 0.0, "end": 4.0, "beat": 0, "crop": None}
    k = frame_check.clip_key(c, beats[0])

    def issues(**look):
        v = {"ok": True, "seen": "梅奔车库", "why": "", "live": False, "key": False, "card": False, "marks": [], "k": k}
        tl = {"fps": 30, "duration": 4.0, "beats": beats, "video": [dict(c, look={**v, **look})]}
        return {i["name"]: i for i in qa.timeline_issues(conn, eid, tl)}
    got = issues(live=True)
    assert got["对象不符"]["ok"] and got["看图不通过"]["ok"] and got["台标未裁"]["ok"]
    assert not got["普通比赛画面占比"]["ok"]                        # 标签是维修区车库，看图是赛道实况
    got = issues(ok=False, card=True, why="联名标题卡", marks=["右上"])
    assert not got["看图不通过"]["ok"] and "联名标题卡" in got["看图不通过"]["detail"]
    assert not got["台标未裁"]["ok"]
    got = issues(live=True, k="旧")                                  # 结论对不上镜头就回到按标签查
    assert not got["对象不符"]["ok"] and got["普通比赛画面占比"]["ok"] and got["看图不通过"]["ok"]
