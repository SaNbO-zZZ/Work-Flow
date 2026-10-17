import asyncio
import json

import pytest

from hermes import codex_runner, config, db, edit


def _episode(conn, code="E1") -> int:
    return conn.execute("INSERT INTO episodes(code, branch, title, state, created_at, updated_at) "
                        "VALUES (?, '前瞻', '测试', '配素材与粗剪', ?, ?)", (code, db.now(), db.now())).lastrowid


def _asset(conn, shots, *, credit="FORMULA 1", title="测试素材", w=1920, h=1080) -> int:
    """shots: [(start, end, tags, has_subtitles, has_logo, logo_note)]"""
    aid = conn.execute("INSERT INTO assets(source_type, title, credit, width, height, created_at) VALUES "
                       "('测试', ?, ?, ?, ?, ?)", (title, credit, w, h, db.now())).lastrowid
    for i, (s, e, tags, subs, logo, note) in enumerate(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags, has_subtitles, has_logo, logo_note) "
                     "VALUES (?,?,?,?,?,?,?,?)", (aid, i, s, e, json.dumps(tags, ensure_ascii=False), subs, logo, note))
    return aid


def _tags(main, drivers=(), teams=(), shot_type="全景"):
    return {"teams": list(teams), "drivers": list(drivers), "circuit": None, "session": "正赛",
            "main": [main], "sub": [], "shot_type": shot_type}


ALIGN = {"duration": 10.0, "segments": [
    {"i": 0, "text": "诺里斯起步很猛。", "show": "诺里斯起步很猛。", "start": 0.3, "end": 3.0},
    {"i": 1, "text": "随后进站。", "show": "随后进站。", "start": 3.4, "end": 6.0},
    {"i": 2, "text": "最后夺冠。", "show": "最后夺冠。", "start": 6.3, "end": 9.5},
]}


@pytest.fixture
def lib(conn, monkeypatch):
    a = _asset(conn, [
        (0.0, 5.0, _tags("起步", drivers=["诺里斯"], teams=["迈凯伦"]), 0, 0, None),
        (5.0, 10.0, _tags("进站"), 0, 0, None),
        (10.0, 11.0, _tags("比赛中"), 0, 0, None),           # 短
        (11.0, 20.0, _tags("起步"), 1, 0, None),             # 带字幕
    ])
    b = _asset(conn, [
        (0.0, 8.0, _tags("领奖台"), 0, 1, "右上角白色 Sky Sports 字样"),
        (8.0, 16.0, _tags("比赛中"), 0, 1, "台标"),
    ], credit=None, title="B")
    conn.execute("UPDATE assets SET uploader='某UP' WHERE id=?", (b,))
    # 画面小字写画面是什么（2026-10-14 用户定）
    conn.execute("UPDATE assets SET gp_event='Singapore Grand Prix', season=2026, session='正赛' WHERE id=?", (a,))
    conn.execute("UPDATE assets SET gp_event='Malaysian Grand Prix', session='排位' WHERE id=?", (b,))
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **k: [])
    return a, b


def test_candidates_structure_and_ranking(conn, lib, monkeypatch):
    a, b = lib
    other = _episode(conn, "OLD")
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (other, a, 6.0, 7.0, db.now()))
    monkeypatch.setattr(edit.visual_search, "search",
                        lambda conn, q, limit, asset_ids: [{"asset_id": b, "shot_idx": 0, "score": 0.3}]
                        if "夺冠" in q else [])
    res = edit.candidates(conn, [a, b], ALIGN["segments"], per_segment=3, episode_id=_episode(conn))
    segs = res["segments"]
    assert [s["i"] for s in segs] == [0, 1, 2] and all(len(s["candidates"]) == 3 for s in segs)
    c0 = segs[0]["candidates"][0]
    assert (c0["asset_id"], c0["shot_idx"]) == (a, 0)                 # 车手名命中
    assert {"start", "end", "duration", "main", "shot_type", "has_logo", "logo_note", "cross_used"} <= set(c0)
    assert any((c["asset_id"], c["shot_idx"]) == (a, 1) and c["cross_used"] is True for c in segs[1]["candidates"])
    assert (segs[2]["candidates"][0]["asset_id"], segs[2]["candidates"][0]["shot_idx"]) == (b, 0)  # 以文搜图
    subs = next(s for s in res["shots"] if s["has_subtitles"])
    assert all((c["asset_id"], c["shot_idx"]) != (subs["asset_id"], subs["shot_idx"]) for c in segs[0]["candidates"][:1])
    assert res["shots"][4]["logo_note"].startswith("右上角")


def test_build_timeline_fills_and_clamps(conn, lib):
    a, b = lib
    cuts = [
        {"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 1.0, "src_out": 3.0, "reason": "起步"},
        {"segment": 0, "asset_id": a, "shot_idx": 1, "src_in": 5.0, "src_out": 6.0, "reason": "x"},
        # 段 1 没有镜头 → 并入段 0
        {"segment": 2, "asset_id": a, "shot_idx": 2, "src_in": 10.5, "src_out": 13.0, "reason": "短镜头"},
        {"segment": 9, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 1, "reason": "段不存在"},
    ]
    warns = []
    tl = edit.build_timeline(conn, "/tmp", ALIGN, cuts, warns)
    v = tl["video"]
    assert tl["fps"] == 30 and tl["size"] == [1920, 1080] and tl["duration"] == 10.0
    assert tl["voice"] == "03_配音/配音.wav" and tl["subtitles"] == "03_配音/字幕.json" and tl["overlays"] == []
    assert v[0]["start"] == 0.0 and v[-1]["end"] == 10.0
    assert all(v[k]["end"] == v[k + 1]["start"] for k in range(len(v) - 1))
    assert len(v) == 3 and v[2]["start"] == 6.3
    assert abs((v[0]["end"] - v[0]["start"]) / (v[1]["end"] - v[1]["start"]) - 2) < 0.05   # 按 src 时长比例
    for c in v:
        assert set(c) == {"asset_id", "shot_idx", "src_in", "src_out", "start", "end", "crop", "segment", "beat",
                          "reason"}
    assert v[2]["src_in"] == 10.0 and v[2]["src_out"] == 11.0                                  # 夹在镜头内
    assert any("定格" in w for w in warns) and any("段不存在" in w or "不存在" in w for w in warns)
    assert tl["credits"] == [{"text": "2026 新加坡大奖赛正赛", "start": 0.0, "end": 10.0}]


def test_crop_and_credits(conn, lib):
    a, b = lib
    cuts = [{"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": ""},
            {"segment": 1, "asset_id": b, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": ""},
            {"segment": 2, "asset_id": b, "shot_idx": 1, "src_in": 8, "src_out": 11, "reason": ""}]
    tl = edit.build_timeline(conn, "/tmp", ALIGN, cuts)
    v = tl["video"]
    assert v[0]["crop"] is None
    assert v[1]["crop"] == {"x": 154, "y": 173, "w": 1612, "h": 906}  # 右上 → 纵向躲开，水平居中
    assert v[2]["crop"]["x"] == (1920 - 1688) // 2 and v[2]["crop"]["y"] == (1080 - 948) // 2
    assert [c["text"] for c in tl["credits"]] == ["2026 新加坡大奖赛正赛", "马来西亚大奖赛排位"]
    assert tl["credits"][1]["start"] == v[1]["start"] and tl["credits"][1]["end"] == 10.0


def test_logo_crop_corners():
    assert edit.logo_crop(1920, 1080, "左上角常驻「SPRINT」字样") == {"x": 154, "y": 173, "w": 1612, "h": 906}
    assert edit.logo_crop(1920, 1080, "右下角") == {"x": 154, "y": 1, "w": 1612, "h": 906}
    assert edit.logo_crop(1920, 1080, "台标") == {"x": 116, "y": 66, "w": 1688, "h": 948}   # 没写位置居中
    c = edit.logo_crop(1440, 1080, "左下")              # 4:3 源仍裁成 16:9
    assert abs(c["w"] / c["h"] - 16 / 9) < 0.01 and c["w"] == 1440 and c["y"] + c["h"] <= 1080 * 0.84


def _ep_dir(tmp_path):
    d = tmp_path / "ep"
    (d / "03_配音").mkdir(parents=True)
    (d / "03_配音" / "对齐.json").write_text(json.dumps(ALIGN, ensure_ascii=False), encoding="utf-8")
    return d


def test_decide_calls_runner_and_retries_once(conn, lib, tmp_path, monkeypatch):
    a, b = lib
    ep = _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}")
    calls = []
    good = {"cuts": [{"segment": i, "asset_id": a, "shot_idx": i, "src_in": 0, "src_out": 3, "reason": "r"}
                     for i in range(3)]}

    async def runner(prompt, model, effort, workdir, **kw):
        calls.append({"prompt": prompt, "model": model, "effort": effort, **kw})
        return codex_runner.CodexResult("t", json.dumps(good), {"input_tokens": 100, "output_tokens": 10})

    seen = []

    def fake_checks(conn, eid, tl):
        seen.append(tl)
        bad = len(seen) == 1
        return [{"name": "同一段画面两次", "ok": not bad, "hard": True, "detail": "素材 1#0 用了两次"},
                {"name": "跨期复用未同意", "ok": False, "hard": True, "detail": ""}]

    monkeypatch.setattr(edit, "_checks", fake_checks)
    res = asyncio.run(edit.decide(conn, ep, _ep_dir(tmp_path), asset_ids=[a, b], branch="前瞻", runner=runner, task_id=None))
    assert len(calls) == 2 and res["attempts"] == 2
    model, effort = config.ROLE_MODELS["剪辑决策"]
    assert calls[0]["model"] == model and calls[0]["effort"] == effort
    schema = json.loads(calls[0]["output_schema"].read_text())
    assert schema["required"] == ["cuts", "gaps"]
    assert set(schema["properties"]["cuts"]["items"]["required"]) == {"segment", "asset_id", "shot_idx", "src_in",
                                                                     "src_out", "reason"}
    assert calls[0]["sandbox"] == "read-only"
    assert "剪辑决策" in calls[0]["prompt"] and f"{a}#0" in calls[0]["prompt"]
    # 画面段：前两句讲诺里斯合成一段，“最后”开头另起（2026-10-15）
    assert "[0] 0.00-6.30 (6.3s) 对象:迈凯伦(诺里斯) 诺里斯起步很猛。随后进站。" in calls[0]["prompt"]
    assert "[1] 6.30-10.00 (3.7s) 对象:无 最后夺冠。" in calls[0]["prompt"]
    assert "同一段画面两次" in calls[1]["prompt"] and "上一版 cuts" in calls[1]["prompt"]
    assert conn.execute("SELECT COUNT(*) FROM usage WHERE episode_id=?", (ep,)).fetchone()[0] == 2
    assert res["timeline"]["video"][-1]["end"] == 10.0


def test_decide_no_retry_when_clean_and_requires_assets(conn, lib, tmp_path, monkeypatch):
    a, b = lib
    ep = _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *a, **k: "p")
    monkeypatch.setattr(edit, "_checks", lambda *a: [{"name": "跨期复用未同意", "ok": False, "hard": True}])
    n = []

    async def runner(prompt, model, effort, workdir, **kw):
        n.append(1)
        return codex_runner.CodexResult("t", '```json\n{"cuts":[{"segment":0,"asset_id":%d,"shot_idx":0,'
                                             '"src_in":0,"src_out":2,"reason":""}]}\n```' % a, {})
    d = _ep_dir(tmp_path)
    res = asyncio.run(edit.decide(conn, ep, d, asset_ids=[a], branch="前瞻", runner=runner))
    assert len(n) == 1 and res["attempts"] == 1
    prompts_seen = []

    async def runner2(prompt, *x, **kw):
        prompts_seen.append(prompt)
        return await runner(prompt, *x, **kw)
    asyncio.run(edit.decide(conn, ep, d, asset_ids=[a], branch="前瞻", runner=runner2, feedback=["不要 ABA"]))
    assert "G2/G3 打回的意见" in prompts_seen[0] and "- 不要 ABA" in prompts_seen[0]
    with pytest.raises(ValueError):
        asyncio.run(edit.decide(conn, ep, d, asset_ids=None, branch="前瞻", runner=runner))
    conn.execute("INSERT INTO downloads(url, episode_id, state, asset_id, created_at) VALUES ('u', ?, 'done', ?, ?)",
                 (ep, b, db.now()))
    assert edit.episode_assets(conn, ep) == [b]


def test_save_writes_everything(conn, lib, tmp_path, monkeypatch):
    a, b = lib
    ep, old = _episode(conn), _episode(conn, "OLD")
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (old, b, 0.0, 4.0, db.now()))
    conn.execute("INSERT INTO asset_uses(episode_id, asset_id, src_in, src_out, created_at) VALUES (?,?,?,?,?)",
                 (ep, a, 99.0, 100.0, db.now()))     # 本期旧记录，应被替换
    cuts = [{"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": ""},
            {"segment": 1, "asset_id": b, "shot_idx": 0, "src_in": 0, "src_out": 3, "reason": ""},
            {"segment": 2, "asset_id": a, "shot_idx": 1, "src_in": 5, "src_out": 8, "reason": ""}]
    tl = edit.build_timeline(conn, tmp_path, ALIGN, cuts)
    d = _ep_dir(tmp_path)
    monkeypatch.setattr(edit, "_checks", lambda *x: [{"name": "A-B-A", "ok": False, "hard": True, "detail": ""}])
    rendered = []

    def fake_render(ep_dir, timeline, out, *, preview=False, conn=None):
        rendered.append(preview)
        out.write_bytes(b"")
        return out
    from hermes import compose
    monkeypatch.setattr(compose, "render", fake_render)

    res = edit.save(conn, ep, d, tl, stage_run=2)
    assert json.loads((d / "06_合成" / "时间线.json").read_text(encoding="utf-8")) == tl
    assert res["preview"].endswith("粗剪.mp4") and rendered == [True] and res["hard_failed"] is True
    uses = [dict(r) for r in conn.execute("SELECT * FROM asset_uses WHERE episode_id=? ORDER BY timeline_at", (ep,))]
    assert len(uses) == 3 and uses[0]["src_in"] == 0.0 and uses[1]["timeline_at"] == tl["video"][1]["start"]
    assert (uses[1]["cross_episode"], uses[1]["approval"]) == (1, "pending")
    assert (uses[0]["cross_episode"], uses[0]["approval"]) == (0, "n/a")
    row = conn.execute("SELECT * FROM timelines WHERE episode_id=?", (ep,)).fetchone()
    assert row["kind"] == "粗剪" and row["stage_run"] == 2 and row["video_path"] == res["preview"]
    assert json.loads(row["checks"])[0]["name"] == "A-B-A"

    # 重存：已同意的跨期复用沿用；不出预览
    conn.execute("UPDATE asset_uses SET approval='approved' WHERE episode_id=? AND cross_episode=1", (ep,))
    res2 = edit.save(conn, ep, d, tl, stage_run=2, preview=False)
    assert res2["preview"] is None
    assert conn.execute("SELECT approval FROM asset_uses WHERE episode_id=? AND cross_episode=1",
                        (ep,)).fetchone()[0] == "approved"
    assert conn.execute("SELECT COUNT(*) FROM asset_uses WHERE episode_id=?", (ep,)).fetchone()[0] == 3


def test_unusable_shots_skipped(conn, lib):
    a, b = lib
    shot = edit.assets.set_unusable(conn, a, 0, True)
    assert shot["unusable"] is True
    assert (a, 0) not in {(s["asset_id"], s["shot_idx"]) for s in edit.load_shots(conn, [a, b])}
    assert 0 not in {s["idx"] for s in edit.assets.clean_shots(conn, a)}
    warns = []
    tl = edit.build_timeline(conn, "/tmp", ALIGN, [
        {"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 1.0, "src_out": 3.0, "reason": "不可用"},
        {"segment": 0, "asset_id": a, "shot_idx": 1, "src_in": 5.0, "src_out": 6.0, "reason": "x"}], warns)
    assert all(c["shot_idx"] != 0 for c in tl["video"]) and any("不可用" in w for w in warns)
    assert edit.assets.set_unusable(conn, a, 0, False)["unusable"] is False
    with pytest.raises(KeyError):
        edit.assets.set_unusable(conn, a, 99, True)


# ---------- 本站画面、事件段、缺口（2026-10-14 用户定） ----------

RACE = {"event": "Singapore Grand Prix", "season": 2026, "name_zh": "新加坡大奖赛"}
EV_ALIGN = {"duration": 10.0, "segments": [
    {"i": 0, "text": "诺里斯赛车故障退赛。", "show": "诺里斯赛车故障退赛。", "start": 0.3, "end": 3.0},
    {"i": 1, "text": "随后进站。", "show": "随后进站。", "start": 3.4, "end": 6.0},
    {"i": 2, "text": "最后夺冠。", "show": "最后夺冠。", "start": 6.3, "end": 9.5},
]}


def _walk(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk(v)


def test_cuts_schema_strict():
    objs = list(_walk(edit.CUTS_SCHEMA))
    assert len(objs) == 3
    for o in objs:
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])
    gap = edit.CUTS_SCHEMA["properties"]["gaps"]["items"]
    assert gap["properties"] == {"segment": {"type": "integer"}, "need": {"type": "string"}}


def test_shot_line_marks_race_and_category():
    base = {"asset_id": 1, "shot_idx": 0, "start": 0.0, "end": 3.0, "duration": 3.0, "main": ["维修区车库"],
            "shot_type": "人物", "names": [], "has_logo": False, "has_subtitles": False, "cross_used": False,
            "key": True}
    line = edit._shot_line(dict(base, race="他站", category="车队运营"))
    assert "他站" in line and "车队运营" in line and line.endswith("关键")
    line = edit._shot_line(dict(base, race="未知", category="其他", key=False))
    assert "未知" not in line and "其他" not in line and "本站" not in line
    assert "本站 官方运营" in edit._shot_line(dict(base, race="本站", category="官方运营"))
    assert edit.event_words("诺里斯赛车故障，被迫停车") == ["故障", "停车"] and edit.event_words("最后夺冠") == []
    for word in ("7. 事件段只用本站画面", "gaps", "8. 普通比赛画面限量", "45%", "对象一致（规则 9）优先于“先用运营视频”", "9. 对象一致",
                 "10. 时长够用", "cuts 不能为空", "对象一致（规则 9）> 时长够用（规则 10）", "视频最后 20 秒已被程序去掉",
                 "车队运营:某车队=这支车队自己发的运营视频", "镜头标记里写了“车队运营:某车队”"):
        assert word in edit.STEP_INSTRUCTION


@pytest.fixture
def raced(conn, lib, monkeypatch):
    """lib 的 a 是本站、b 是他站，再加一条认不出站的车队运营视频 c。"""
    a, b = lib
    c = _asset(conn, [(0.0, 6.0, _tags("维修区车库", shot_type="人物"), 0, 0, None)], title="C")
    conn.execute("UPDATE assets SET category='车队运营' WHERE id=?", (c,))
    where = {a: "本站", b: "他站"}
    monkeypatch.setattr(edit, "episode_race", lambda conn, eid: RACE if eid is not None else None)
    monkeypatch.setattr(edit, "race_of", lambda asset, race: where.get(asset["id"], "未知") if race else "未知")
    return a, b, c


def test_event_segments_and_race_scores(conn, raced, monkeypatch):
    a, b, c = raced
    ep = _episode(conn)
    res = edit.candidates(conn, [a, b, c], EV_ALIGN["segments"], per_segment=8, episode_id=ep)
    assert res["race"] == RACE and res["segments"][0]["event"] == ["故障", "退赛"] and res["segments"][1]["event"] == []
    shots = {(s["asset_id"], s["shot_idx"]): s for s in res["shots"]}
    assert shots[(a, 0)]["race"] == "本站" and shots[(b, 0)]["race"] == "他站" and shots[(c, 0)]["race"] == "未知"
    assert shots[(c, 0)]["category"] == "车队运营"
    s = shots[(b, 0)]
    # 他站只罚事件段和比赛实况；非事件段的他站运营画面不降分（2026-10-16 用户定）
    assert edit._score(s, "x", 0.0, True) < edit._score(s, "x", 0.0, False) == edit._score(dict(s, race="未知"), "x", 0.0)
    track = dict(s, main=["比赛中"])
    assert edit._score(track, "x", 0.0, False) < edit._score(dict(track, race="未知"), "x", 0.0, False)
    op = shots[(c, 0)]
    assert edit._score(op, "x", 0.0, False) > edit._score(op, "x", 0.0, True)        # 运营视频非事件段加分
    ctx = edit.build_context(conn, res, [a, b, c])
    assert "[0] 0.30-3.00 (2.7s) 对象:无 [事件] 诺里斯赛车故障退赛。" in ctx and "[1] 3.40-6.00 (2.6s) 对象:无 随后进站。" in ctx
    assert f"{b}#0 " in ctx and "他站" in ctx.split(f"{b}#0 ")[1].splitlines()[0]
    # 认不出站：不标事件，也不分本站他站
    monkeypatch.setattr(edit, "episode_race", lambda conn, eid: None)
    res = edit.candidates(conn, [a, b, c], EV_ALIGN["segments"], per_segment=8, episode_id=ep)
    ctx = edit.build_context(conn, res, [a, b, c])
    assert "[事件]" not in ctx and "认不出本期是哪一站" in ctx and all(s["race"] == "未知" for s in res["shots"])


def test_gaps_into_timeline_and_decide(conn, lib, tmp_path, monkeypatch):
    a, b = lib
    gaps = []
    cuts = edit._parse(json.dumps({"cuts": [{"segment": 0, "asset_id": a, "shot_idx": 0, "src_in": 0, "src_out": 3,
                                              "reason": "r"}],
                                   "gaps": [{"segment": 0, "need": "本站诺里斯赛车冒烟停在路边"},
                                            {"segment": 7, "need": "段不存在"}]}), gaps)
    assert len(cuts) == 1 and len(gaps) == 2
    assert edit._parse('{"cuts": []}') == []                     # 旧调用方式不受影响
    warns = []
    tl = edit.build_timeline(conn, "/tmp", EV_ALIGN, cuts, warns, gaps)
    assert tl["gaps"] == [{"segment": 0, "start": 0.3, "end": 3.0, "need": "本站诺里斯赛车冒烟停在路边",
                           "text": "诺里斯赛车故障退赛。"}]
    assert any("缺口" in w for w in warns)
    assert edit.build_timeline(conn, "/tmp", EV_ALIGN, cuts)["gaps"] == []

    ep = _episode(conn)
    monkeypatch.setattr(edit.prompts, "assemble", lambda *x, **k: "p")
    monkeypatch.setattr(edit, "_checks", lambda *x: [])
    out = {"cuts": [{"segment": i, "asset_id": a, "shot_idx": i, "src_in": 0, "src_out": 3, "reason": "r"}
                    for i in range(3)], "gaps": [{"segment": 0, "need": "本站退赛画面"}]}

    async def runner(prompt, model, effort, workdir, **kw):
        return codex_runner.CodexResult("t", json.dumps(out, ensure_ascii=False), {})
    d = _ep_dir(tmp_path)
    (d / "03_配音" / "对齐.json").write_text(json.dumps(EV_ALIGN, ensure_ascii=False), encoding="utf-8")
    res = asyncio.run(edit.decide(conn, ep, d, asset_ids=[a], branch="复盘", runner=runner))
    assert res["gaps"] == res["timeline"]["gaps"] and res["gaps"][0]["need"] == "本站退赛画面"


def test_race_share_fixed_once_then_left_to_g2():
    from hermes.qa_checks import RACE_SHARE_NAME
    checks = [{"name": RACE_SHARE_NAME, "ok": False, "hard": True}, {"name": "事件段用了他站画面", "ok": False, "hard": True},
              {"name": "跨期复用", "ok": False, "hard": True}]
    assert [c["name"] for c in edit._hard_problems(checks)] == [RACE_SHARE_NAME, "事件段用了他站画面"]
    assert [c["name"] for c in edit._hard_problems(checks, 1)] == ["事件段用了他站画面"]


def test_race_of_uses_asset_identity():
    assert edit.race_of({"gp_event": "Singapore Grand Prix", "season": 2026}, RACE) == "本站"
    assert edit.race_of({"gp_event": "British Grand Prix", "season": 2026}, RACE) == "他站"
    assert edit.race_of({"gp_event": None, "season": None}, RACE) == "未知"
    assert edit.race_of({"gp_event": "Singapore Grand Prix", "season": 2026}, None) == "未知"
