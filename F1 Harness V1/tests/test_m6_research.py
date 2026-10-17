"""M6-1 选题与资料：D1 资料与事实、D5 梗与出处、总控汇总选题、打回意见收集。"""
import asyncio
import json
from pathlib import Path

import pytest

from hermes import codex_runner, config, db, edit, episodes, facts, prompts, research, steps, versions
from hermes.scheduler import Scheduler

ORIG_CONNECT = db.connect


def _row(conn, eid):
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}|{notes}")
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="Malaysian Grand Prix", title="雪邦前瞻")
    return _row(conn, eid)


def _calendar(conn):
    conn.execute("INSERT INTO race_events VALUES (2099, 16, 'Malaysian Grand Prix', NULL, 'Malaysia', 'Sepang', "
                 "'2099-10-25', 'conventional', '2099-01-01')")
    conn.execute("INSERT INTO race_events VALUES (2099, 17, 'X Grand Prix', NULL, 'X', 'Xville', '2099-11-01', "
                 "'conventional', '2099-01-01')")
    conn.execute("INSERT INTO race_sessions VALUES (2099, 16, 1, 'Practice 1', '2099-10-23T03:30:00+00:00')")
    conn.execute("INSERT INTO race_sessions VALUES (2099, 16, 5, 'Race', '2099-10-25T07:00:00+00:00')")


def _runner(answers: dict, seen: list):
    async def run(prompt, model, effort, workdir, **kw):
        seen.append({"prompt": prompt, "model": model, "kw": kw})
        name = Path(kw["output_schema"]).name.split(".")[0]
        data = answers[name]
        return codex_runner.CodexResult("t", data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                                        {"input_tokens": 10, "output_tokens": 5})
    return run


SOURCES = {"summary": "雪邦回归是最大看点。", "items": [
    {"content": "据 The Race 报道某车队带来升级", "source": "The Race", "url": "https://www.the-race.com/a",
     "excerpt": "The team will bring an upgrade.", "number": None},
    {"content": "没有链接的说法", "source": "x", "url": "", "excerpt": "y", "number": None},
    {"content": "雪邦赛道单圈长度（公里）", "source": "F1 官网", "url": "https://www.formula1.com/en/racing/2099/malaysia",
     "excerpt": "Circuit length 5.543 km", "number": "5.543"},
    {"content": "某媒体说的圈数", "source": "Motorsport", "url": "https://www.motorsport.com/b", "excerpt": "56 laps",
     "number": "56"},
]}


def _results(season, rnd, session, top):
    assert (season, rnd) == (2098, 3)
    if session == "Q":
        return [{"pos": 1, "name": "Pole Man", "team": "Team A", "laps": None}]
    return [{"pos": i, "name": f"Driver {i}", "team": "Team B", "laps": 56} for i in range(1, top + 1)]


async def _no_weather(conn, eid, season, rnd):
    raise KeyError("Sepang")


def test_collect_local_and_gpt(conn, ep):
    _calendar(conn)
    seen = []
    run = _runner({"资料": SOURCES}, seen)
    out = asyncio.run(research.collect(conn, ep, model="m", effort="high", runner=run, feedback=["多讲轮胎"],
                                       results_fn=_results, weather_fn=_no_weather, schedule_fn=lambda y, loc: 3))
    rows = {f["content"]: f for f in facts.items(conn, ep["id"])}
    assert rows["本站是本赛季第几站"]["value"] == "16"
    assert rows["本赛季总站数"]["value"] == "2"
    first = next(f for c, f in rows.items() if c.startswith("一练开始时间"))
    assert first["value"] == "11:30" and "10月23日" in first["content"] and first["source"] == "FastF1 赛历"
    assert "2098年本站正赛第1名：Driver 1（Team B）" in rows and "2098年本站排位杆位：Pole Man（Team A）" in rows
    assert rows["2098年本站正赛冠军完成圈数"]["value"] == "56"
    assert rows["雪邦赛道单圈长度（公里）"]["kind"] == "F" and rows["雪邦赛道单圈长度（公里）"]["value"] == "5.543"
    s = facts.items(conn, ep["id"], "S")
    assert len(s) == 1 and s[0]["url"] == "https://www.the-race.com/a"
    assert len(out["dropped"]) == 2 and any("天气" in w for w in out["warnings"])
    md = (Path(ep["folder"]) / "01_资料" / "资料.md").read_text(encoding="utf-8")
    assert "雪邦回归是最大看点" in md and "[S1]" in md
    p = seen[0]["prompt"]
    assert "G0 打回的意见" in p and "- 多讲轮胎" in p and "11:30" in p
    assert seen[0]["kw"]["web_search"] is True and seen[0]["kw"]["sandbox"] == "read-only"
    assert conn.execute("SELECT COUNT(*) FROM usage WHERE episode_id=?", (ep["id"],)).fetchone()[0] == 1

    # G0 打回后重做：整表重建，编号不累加
    asyncio.run(research.collect(conn, ep, model="m", effort="high", runner=run, results_fn=_results,
                                 weather_fn=_no_weather, schedule_fn=lambda y, loc: 3))
    assert [f["tag"] for f in facts.items(conn, ep["id"], "S")] == ["S1"]


def test_collect_without_event(conn, ep):
    seen = []
    out = asyncio.run(research.collect(conn, ep, model="m", effort="high",
                                       runner=_runner({"资料": SOURCES}, seen)))
    assert out["facts"] == {"F": 1, "S": 1} and out["warnings"] == []


def test_memes_marks_unverified(conn, ep):
    data = {"items": [
        {"text": "梗A", "meaning": "a", "url": "https://www.bilibili.com/x", "verified": True, "note": "开头用"},
        {"text": "梗B", "meaning": "b", "url": "", "verified": True, "note": ""},
        {"text": "梗C", "meaning": "c", "url": "https://weibo.com/y", "verified": False, "note": "不要用"},
        {"text": "", "meaning": "空", "url": "https://x.com/z", "verified": True, "note": ""},
    ]}
    out = asyncio.run(research.memes(conn, ep, model="m", effort="high", runner=_runner({"梗": data}, [])))
    items = json.loads((Path(ep["folder"]) / "01_资料" / "梗.json").read_text(encoding="utf-8"))["items"]
    assert out == {"items": 3, "verified": 1, "file": "01_资料/梗.json"}
    assert items[0]["verified"] is True and items[0]["note"] == "开头用"
    assert items[1]["verified"] is False and items[1]["note"].startswith("未核实") and "没有可用出处" in items[1]["note"]
    assert items[2]["verified"] is False and "不要用" in items[2]["note"]
    assert set(items[0]) == {"text", "meaning", "url", "verified", "note"}


def test_topic_renders_and_checks_tags(conn, ep):
    facts.add(conn, ep["id"], "F", "本站是本赛季第几站", source="FastF1 赛历", value="16")
    d = Path(ep["folder"]) / "01_资料"
    (d / "资料.md").write_text("# 资料\n摘要", encoding="utf-8")
    (d / "梗.json").write_text(json.dumps({"items": [
        {"text": "好梗", "meaning": "", "url": "https://a.com/x", "verified": True, "note": ""},
        {"text": "坏梗", "meaning": "", "url": "", "verified": False, "note": "未核实，不得使用"}]}), encoding="utf-8")
    data = {"angle": "雪邦回归", "titles": ["标题一", "标题二", "标题三"],
            "outline": [{"part": "钩子", "points": "开场", "seconds": 10, "tags": ["F1", "F99"]}],
            "seconds": 210, "memes": ["好梗", "坏梗"], "risks": "无"}
    seen = []
    out = asyncio.run(research.topic(conn, ep, model="astra", effort="high", runner=_runner({"选题": data}, seen)))
    md = (d / "选题.md").read_text(encoding="utf-8")
    assert "雪邦回归" in md and "预计 210 秒" in md and "[F1] 本站是本赛季第几站：16" in md
    assert "F99" not in md and "- 好梗" in md and "坏梗" not in md.split("## 准备用的梗")[1]
    assert out["unknown_tags"] == ["F99"] and out["dropped_memes"] == ["坏梗"]
    assert "【未核实，不得使用】坏梗" in seen[0]["prompt"] and seen[0]["kw"]["web_search"] is False


def test_lesson_notes(conn, ep):
    ts = "2099-01-01"
    for branch, content, state in ((None, "通用", "approved"), ("前瞻", "前瞻专用", "approved"),
                                   ("复盘", "复盘专用", "approved"), ("前瞻", "没批", "pending")):
        conn.execute("INSERT INTO lessons(branch, content, state, created_at) VALUES (?,?,?,?)",
                     (branch, content, state, ts))
    assert research.lesson_notes(conn, "前瞻") == "- 通用\n- 前瞻专用"


def test_episode_context_ad_info(conn, ep):
    assert ep["ad_info"] is None
    assert "- 广告信息：无（本期没有登记广告，不留广告位）" in research.episode_context(ep)
    conn.execute("UPDATE episodes SET ad_info='某品牌口播 30 秒' WHERE id=?", (ep["id"],))
    ctx = research.episode_context(_row(conn, ep["id"]))
    assert "- 广告信息：某品牌口播 30 秒" in ctx and "广告信息：无" not in ctx


def _gate(conn, eid, gate, state, fb):
    conn.execute("INSERT INTO gates(episode_id, gate, state, feedback, created_at) VALUES (?,?,?,?, '2099')",
                 (eid, gate, state, fb))


def test_gate_feedback(conn, ep):
    other = episodes.new_session(conn)
    _gate(conn, ep["id"], "G0", "rejected", "第一次")
    _gate(conn, ep["id"], "G0", "rejected", "")
    _gate(conn, ep["id"], "G0", "approved", "通过了")
    _gate(conn, ep["id"], "G1", "rejected", "稿子太长")
    _gate(conn, ep["id"], "G0", "rejected", "第二次")
    _gate(conn, other, "G0", "rejected", "别的期")
    assert steps._gate_feedback(conn, ep["id"], ("G0",)) == ["第一次", "第二次"]
    assert steps._gate_feedback(conn, ep["id"], ("G0", "G1")) == ["第一次", "稿子太长", "第二次"]


def test_edit_decide_gets_g2_and_g3(conn, ep, monkeypatch):
    _gate(conn, ep["id"], "G2", "rejected", "镜头太碎")
    _gate(conn, ep["id"], "G3", "rejected", "结尾素材换掉")
    got = {}

    async def fake_decide(conn, eid, d, **kw):
        got.update(kw)
        return {"timeline": {"video": []}, "attempts": 1, "warnings": []}

    monkeypatch.setattr(steps, "asset_ids", lambda conn, ep: [1])
    monkeypatch.setattr(edit, "decide", fake_decide)
    monkeypatch.setattr(edit, "save", lambda *a, **k: {"hard_failed": False})
    asyncio.run(steps.edit_decide({"id": None}, ep))
    assert got["feedback"] == ["镜头太碎", "结尾素材换掉"]


def test_pipeline_reaches_g0_and_redo_gets_feedback(conn, db_path, ep, monkeypatch):
    seen = []
    answers = {"资料": SOURCES, "梗": {"items": []},
               "选题": {"angle": "雪邦回归", "titles": ["t"], "outline": [], "seconds": 210, "memes": [], "risks": "无"}}
    monkeypatch.setattr(codex_runner, "run", _runner(answers, seen))

    async def ok(task, ep):
        return {}

    sched = Scheduler(lambda: ORIG_CONNECT(db_path), {"*": ok, **steps.EXECUTORS},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    async def drive():
        for _ in range(300):
            await sched.tick()
            await asyncio.sleep(0.001)
            if _row(conn, ep["id"])["state"] == "G0待审" and not sched.running:
                return
        raise AssertionError(conn.execute("SELECT step, state, error FROM tasks").fetchall())

    asyncio.run(drive())
    assert conn.execute("SELECT state FROM gates WHERE episode_id=? AND gate='G0'", (ep["id"],)).fetchone()[0] == "pending"
    # 选题全文不再发成一大段文字，改在 G0 卡片里显示（第 3 批）
    msgs = [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=? AND role='system'", (ep["id"],))]
    assert not any(m.startswith("# 选题") for m in msgs)
    card = conn.execute("SELECT card FROM messages WHERE episode_id=? AND role='card'", (ep["id"],)).fetchone()[0]
    assert json.loads(card)["gate"] == "G0"
    g0 = versions.gate_versions(conn, episodes.get(conn, ep["id"]))["G0"][0]
    assert g0["topic"].startswith("# 选题") and g0["dir"] == "09_版本/G0-v1" and not g0["live"]
    outs = {r["step"]: json.loads(r["output"]) for r in conn.execute("SELECT step, output FROM tasks WHERE episode_id=?",
                                                                      (ep["id"],))}
    assert outs["D1资料与事实"]["file"] == "01_资料/资料.md" and "markdown" not in outs["总控汇总选题"]
    assert {s["model"] for s in seen} == {config.ROLE_MODELS[r][0] for r in ("资料", "梗核实", "总控")}

    episodes.reject(conn, ep["id"], "G0", "角度换成轮胎策略")
    seen.clear()
    asyncio.run(drive())
    assert len(seen) == 3 and all("角度换成轮胎策略" in s["prompt"] for s in seen)


def test_edit_decide_tells_gaps(conn, ep, monkeypatch):
    gaps = [{"segment": 3, "start": 83.5, "end": 90.0, "need": "本站诺里斯赛车冒烟停在路边", "text": "诺里斯退赛。"}]

    async def fake_decide(conn, eid, d, **kw):
        return {"timeline": {"video": [], "gaps": gaps}, "attempts": 1, "warnings": []}

    monkeypatch.setattr(steps, "asset_ids", lambda conn, ep: [1])
    monkeypatch.setattr(edit, "decide", fake_decide)
    monkeypatch.setattr(edit, "save", lambda *a, **k: {"hard_failed": False})
    out = asyncio.run(steps.edit_decide({"id": None}, ep))
    assert out["gaps"] == gaps
    msg = conn.execute("SELECT content FROM messages WHERE episode_id=? AND role='system' ORDER BY id DESC",
                       (ep["id"],)).fetchone()[0]
    assert "1 处缺画面" in msg and "- 画面段 3（01:23）本站诺里斯赛车冒烟停在路边" in msg
