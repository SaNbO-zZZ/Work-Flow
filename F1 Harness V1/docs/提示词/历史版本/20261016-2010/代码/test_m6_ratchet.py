"""M6-7 棘轮与自动返工：低于基线自动退回、G1/G2 自动通过、返工意见进提示词、次数用完进 G3、
G3 通过抬基线、打分差太多提醒重新校准、用户打回后次数重算。codex 全部 mock。"""
import asyncio
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from hermes import (codex_runner, db, edit, episodes, overlays, prompts, ratchet, script, server, sourcing,
                    steps)
from hermes.assets import FFMPEG
from hermes.scheduler import Scheduler

ORIG_CONNECT = db.connect


def _issue(back_to, severity="高", at="00:01", at_s=1.0, problem=None):
    return {"at": at, "at_s": at_s, "item": "开头吸引力", "severity": severity,
            "problem": problem or f"{back_to}的问题", "fix": "改一下", "back_to": back_to}


def _answer(total_scores, issues):
    s = total_scores
    return {"scores": [{"item": k, "score": s, "reason": "r"} for k in
                       ("事实准确", "剪辑与节奏", "开头吸引力", "画面贴合度", "文案可听性", "规格合规")],
            "total": s, "issues": issues, "summary": "要改。"}


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("rt") / "片.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-y", str(p)], check=True)
    return p


def _new_ep(conn, branch="前瞻", title="雪邦前瞻"):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch=branch, gp_label="马来西亚大奖赛", title=title)
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


@pytest.fixture(autouse=True)
def _patched(db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}")


@pytest.fixture
def ep(conn, video):
    row = _new_ep(conn)
    _fill(row, video)
    return row


def _fill(ep, video):
    from pathlib import Path
    d = Path(ep["folder"])
    for sub in ("06_合成", "03_配音", "07_QA"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "06_合成" / "成片.mp4").write_bytes(video.read_bytes())
    (d / "02_文案" / "逐字稿.md").write_text("## 开场\n雪邦回来了。\n\n## 策略\n比赛有56圈，一停还是两停。\n",
                                          encoding="utf-8")
    (d / "02_文案" / "标题和简介.txt").write_text("雪邦前瞻\n简介", encoding="utf-8")
    (d / "03_配音" / "字幕.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n雪邦回来了\n", encoding="utf-8")
    (d / "03_配音" / "对齐.json").write_text(json.dumps({"duration": 2.0}), encoding="utf-8")
    tl = {"video": [{"asset_id": 1, "shot_idx": 0, "start": 0, "end": 2, "reason": "开场"}], "overlays": [],
          "credits": []}
    (d / "06_合成" / "时间线.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    (d / "07_QA" / "硬检查.json").write_text(json.dumps({"passed": True, "items": []}), encoding="utf-8")


def _runner(answers: list, seen: list | None = None):
    async def run(prompt, model, effort, workdir, **kw):
        if seen is not None:
            seen.append(prompt)
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        return codex_runner.CodexResult("t", json.dumps(a, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1})
    return run


def _task():
    return {"id": None, "step": "QA评审", "model": None, "effort": None}


def _state(conn, eid):
    return conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0]


def _to(conn, eid, state):
    with db.transaction(conn):
        episodes.set_state(conn, eid, state)


def _msgs(conn, eid):
    return [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=? ORDER BY id", (eid,))]


def _summary(ep):
    return json.loads((steps._dir(ep) / "07_QA" / "质检汇总.json").read_text(encoding="utf-8"))


def _set_base(conn, branch, value):
    conn.execute("UPDATE baselines SET value=? WHERE branch=?", (value, branch))


# ---------- 回退起点 ----------

def test_pick_stage_earliest_main_issue():
    assert ratchet.pick_stage([_issue("D6剪辑决策"), _issue("写逐字稿", "中"), _issue("配音", "低")]) == \
        ("写逐字稿", ["写逐字稿", "D6剪辑决策"])
    assert ratchet.pick_stage([_issue("D4图形与叠层"), _issue("配音"), _issue("写逐字稿", "低")])[0] == "配音"
    assert ratchet.pick_stage([_issue("D2找素材", "低"), _issue("D6剪辑决策", "低")]) == \
        ("配素材与粗剪", ["D2找素材", "D6剪辑决策"])
    assert ratchet.pick_stage([]) is None


# ---------- 评审后 ----------

def test_above_baseline_no_rework(ep, conn, monkeypatch):
    _to(conn, ep["id"], "合成与QA")
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(70, [_issue("写逐字稿")])]))
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["below_baseline"] is False and out["rework"]["back_to"] is None
    assert _state(conn, ep["id"]) == "合成与QA"
    assert conn.execute("SELECT COUNT(*) FROM reworks").fetchone()[0] == 0


def test_empty_baseline_no_rework(conn, db_path, monkeypatch, video):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    e = _new_ep(conn, branch="科普", title="轮胎科普")
    _fill(e, video)
    _to(conn, e["id"], "合成与QA")
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(20, [_issue("写逐字稿")])]))
    out = asyncio.run(steps.qa_review(_task(), e))
    assert out["baseline"] is None and not out["below_baseline"]
    assert _state(conn, e["id"]) == "合成与QA"
    assert conn.execute("SELECT COUNT(*) FROM reworks").fetchone()[0] == 0


def test_below_baseline_rework_and_message(ep, conn, monkeypatch):
    _to(conn, ep["id"], "合成与QA")
    run_before = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (ep["id"],)).fetchone()[0]
    issues = [_issue("D6剪辑决策", problem="前 5 秒空镜"), _issue("配音", "中", problem="语速太快"),
              _issue("写逐字稿", "低", problem="结尾拖")]
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(50, issues)]))
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["rework"]["back_to"] == "配音" and out["rework"]["rounds"] == 1
    row = conn.execute("SELECT state, stage_run FROM episodes WHERE id=?", (ep["id"],)).fetchone()
    assert row["state"] == "配音" and row["stage_run"] == run_before + 1
    rw = conn.execute("SELECT * FROM reworks").fetchone()
    assert rw["state"] == "running" and rw["cycle"] == 0 and rw["round"] == 1 and rw["total"] == 50
    msg = _msgs(conn, ep["id"])[-1]
    assert "成片 50 分，低于前瞻基线 60" in msg and "自动返工第 1/2 轮" in msg and "退回「配音」" in msg
    assert "1) 00:01 前 5 秒空镜" in msg and "2) 00:01 语速太快" in msg and "结尾拖" not in msg


def test_not_in_qa_stage_no_rework(ep, conn, monkeypatch):
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(40, [_issue("写逐字稿")])]))
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["below_baseline"] is True and out["rework"]["back_to"] is None
    assert conn.execute("SELECT COUNT(*) FROM reworks").fetchone()[0] == 0


# ---------- 返工意见进提示词 ----------

def test_rework_feedback_reaches_steps(ep, conn, monkeypatch):
    _to(conn, ep["id"], "合成与QA")
    issues = [_issue("写逐字稿", problem="开头太平"), _issue("D2找素材", "中", at="00:01", at_s=1.5,
                                                        problem="策略段没有进站画面"),
              _issue("D4图形与叠层", "中", problem="出处字太小"), _issue("D6剪辑决策", "中", problem="切太慢")]
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(40, issues)]))
    asyncio.run(steps.qa_review(_task(), ep))
    assert _state(conn, ep["id"]) == "写逐字稿"
    ep2 = conn.execute("SELECT * FROM episodes WHERE id=?", (ep["id"],)).fetchone()
    got = {}

    async def fake_write(conn, ep, *, feedback, **kw):
        got["写逐字稿"] = feedback
        return {}
    monkeypatch.setattr(script, "write", fake_write)
    asyncio.run(steps.script_write({"id": None, "model": None, "effort": None}, ep2))
    assert any("开头太平" in f and "自动返工第 1 轮" in f for f in got["写逐字稿"])
    assert not any("出处字太小" in f for f in got["写逐字稿"])

    async def fake_run(conn, ep, *, feedback, redo, **kw):
        got["D2"] = (feedback, redo)
        return {}
    monkeypatch.setattr(sourcing, "run", fake_run)
    monkeypatch.setattr(sourcing, "summary_text", lambda out: "找完了")
    asyncio.run(steps.find_assets({"id": None, "model": None, "effort": None}, ep2))
    fb, redo = got["D2"]
    assert any("进站画面" in f for f in fb)
    assert sourcing.asks_new_material(redo) and "涉及段：策略" in redo and "进站画面" in redo

    def fake_build(conn, ep, feedback, **kw):
        got["D4"] = feedback
        return {"warnings": []}
    monkeypatch.setattr(overlays, "build", fake_build)
    asyncio.run(steps.graphics_step({"id": None, "model": None, "effort": None}, ep2))
    assert any("出处字太小" in f for f in got["D4"]) and not any("切太慢" in f for f in got["D4"])

    async def fake_decide(conn, eid, d, *, feedback, **kw):
        got["D6"] = feedback
        raise RuntimeError("够了")
    monkeypatch.setattr(edit, "decide", fake_decide)
    with pytest.raises(RuntimeError):
        asyncio.run(steps.edit_decide({"id": None}, ep2))
    assert any("切太慢" in f for f in got["D6"])

    ratchet.close(conn, ep["id"])
    assert ratchet.feedback_for(conn, ep["id"], "写逐字稿") == [] and ratchet.redo_text(conn, ep2) is None


def test_segments_at():
    segs = [{"name": "开场", "seconds": 10}, {"name": "策略", "seconds": 30}]
    assert ratchet.segments_at(segs, [1.0, 35.0, 99.0], 40.0) == ["开场", "策略"]
    assert ratchet.segments_at(segs, [1.0], None) == []


# ---------- 走调度器：G1/G2 自动通过、2 轮用完进 G3 ----------

def _drive(conn, db_path, eid, until="G3待审"):
    async def ok(task, ep):
        return {}
    sched = Scheduler(lambda: ORIG_CONNECT(db_path), {"*": ok, "QA评审": steps.qa_review},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)
    events = []
    sched.on_event = lambda kind, ep, text: events.append((kind, text))

    async def go():
        for _ in range(2000):
            await sched.tick()
            await asyncio.sleep(0.001)
            if _state(conn, eid) == until and not sched.running:
                return
        raise AssertionError(f"没到 {until}，停在 {_state(conn, eid)}")
    asyncio.run(go())
    return events


def test_full_loop_auto_pass_then_g3(ep, conn, db_path, monkeypatch):
    _set_base(conn, "前瞻", 70)
    seen = []
    answers = [_answer(52, [_issue("写逐字稿", "中")]), _answer(57, [_issue("D6剪辑决策")]),
               _answer(61, [_issue("D6剪辑决策")])]
    monkeypatch.setattr(codex_runner, "run", _runner(answers, seen))
    _to(conn, ep["id"], "合成与QA")
    events = _drive(conn, db_path, ep["id"])
    assert len(seen) == 3
    assert events == [("gate", "G3待审")]
    gates = conn.execute("SELECT gate, state, auto FROM gates WHERE episode_id=? ORDER BY id", (ep["id"],)).fetchall()
    assert [tuple(g) for g in gates] == [("G1", "approved", 1), ("G2", "approved", 1), ("G2", "approved", 1),
                                         ("G3", "pending", 0)]
    rws = conn.execute("SELECT round, back_to, state FROM reworks ORDER BY id").fetchall()
    assert [tuple(r) for r in rws] == [(1, "写逐字稿", "done"), (2, "配素材与粗剪", "done")]
    s = _summary(ep)
    assert s["rework"]["history"] == [52, 57, 61] and s["rework"]["note"] == "自动返工 2 轮后仍低于基线 70 分"
    msgs = _msgs(conn, ep["id"])
    assert any("G1 自动通过" in m for m in msgs) and any("52 → 57 → 61" in m for m in msgs)
    data = steps.review_data(conn, ep)
    assert data["qa"]["rework"]["note"].startswith("自动返工 2 轮后仍低于基线")


def test_reaching_baseline_after_rework_goes_to_g3(ep, conn, db_path, monkeypatch):
    answers = [_answer(50, [_issue("D6剪辑决策")]), _answer(65, [])]
    monkeypatch.setattr(codex_runner, "run", _runner(answers))
    _to(conn, ep["id"], "合成与QA")
    _drive(conn, db_path, ep["id"])
    s = _summary(ep)
    assert s["rework"]["history"] == [50, 65] and s["rework"]["note"] == "自动返工 1 轮后达到基线 60 分"
    assert conn.execute("SELECT COUNT(*) FROM gates WHERE auto=1").fetchone()[0] == 1


def test_user_reject_resets_rounds(ep, conn, db_path, monkeypatch):
    monkeypatch.setattr(codex_runner, "run", _runner([_answer(40, [_issue("D6剪辑决策")])]))
    _to(conn, ep["id"], "合成与QA")
    _drive(conn, db_path, ep["id"])
    assert len(ratchet.rounds(conn, ep["id"])) == 2
    episodes.reject(conn, ep["id"], "G3", "再改改", "配素材与粗剪")
    assert ratchet.cycle(conn, ep["id"]) == 1 and ratchet.rounds(conn, ep["id"]) == []
    _to(conn, ep["id"], "合成与QA")
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["rework"]["rounds"] == 1 and out["rework"]["back_to"] == "配素材与粗剪"
    assert conn.execute("SELECT cycle FROM reworks ORDER BY id DESC").fetchone()[0] == 1


def test_gates_not_auto_without_rework(conn):
    e = _new_ep(conn)
    _to(conn, e["id"], "G1待审")
    assert ratchet.auto_pass(conn, e["id"], "G1") is None
    assert _state(conn, e["id"]) == "G1待审"


# ---------- G3：棘轮 ----------

def _reviewed(conn, ep, total, kind="成片"):
    """造一期走到 G3 待审、最后一次合成与QA 评了 total 分。"""
    _to(conn, ep["id"], "合成与QA")
    run = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (ep["id"],)).fetchone()[0]
    conn.execute("INSERT INTO tasks(episode_id, stage, step, stage_run, phase, role, model, effort, state, created_at) "
                 "VALUES (?, '合成与QA', 'QA评审', ?, 2, 'QA评审', 'm', 'high', 'succeeded', ?)",
                 (ep["id"], run, db.now()))
    conn.execute("INSERT INTO reviews(episode_id, stage_run, kind, branch, video_path, total, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (ep["id"], run, kind, ep["branch"], "x.mp4", total, db.now()))
    _to(conn, ep["id"], "G3待审")


def _pass(conn, ep, score=None):
    episodes.approve(conn, ep["id"], "G3", score)
    return steps.after_gate(conn, ep, "G3", "approve", score)


def test_ratchet_median_only_up(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    eps = [_new_ep(conn, title=f"t{i}") for i in range(7)]
    totals = [64, 50, 70, 62, 66, 68, 40]
    notes = []
    for e, t in zip(eps, totals):
        _reviewed(conn, e, t)
        notes.append(_pass(conn, e))
    assert notes[0] == ["前瞻基线保持 60（已通过 1 期，满 3 期后按中位数调整）。"]
    assert notes[1] == ["前瞻基线保持 60（已通过 2 期，满 3 期后按中位数调整）。"]
    assert notes[2] == ["前瞻基线 60 → 64（最近 3 期中位数）。"]
    assert notes[3] == ["前瞻基线保持 64（最近 4 期中位数 63，只升不降）。"]
    # 最近 5 期 [68, 66, 62, 70, 50]→66；再一期 40：[40,68,66,62,70]→66 不变
    assert ratchet.baseline(conn, "前瞻") == 66
    assert notes[-1] == ["前瞻基线保持 66（最近 5 期中位数 66，只升不降）。"]
    assert any("前瞻基线 64 → 66" in m for m in _msgs(conn, eps[5]["id"]))


def test_ratchet_ignores_calibration_and_unapproved(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('qa.baseline_min', '1')")
    conn.execute("INSERT INTO reviews(kind, branch, video_path, total, approved_at, created_at) "
                 "VALUES ('校准', '前瞻', 'a.mp4', 95, ?, ?), ('测试', '前瞻', 'b.mp4', 95, ?, ?)",
                 (db.now(),) * 4)
    other = _new_ep(conn, title="没过")
    _reviewed(conn, other, 99)
    e = _new_ep(conn)
    _reviewed(conn, e, 62)
    assert _pass(conn, e) == ["前瞻基线 60 → 62（最近 1 期中位数）。"]


def test_ratchet_uses_last_review_and_kepu_start(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    e = _new_ep(conn, branch="科普", title="轮胎")
    _to(conn, e["id"], "合成与QA")
    run = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (e["id"],)).fetchone()[0]
    conn.execute("INSERT INTO reviews(episode_id, stage_run, kind, branch, video_path, total, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (e["id"], run, "成片", "科普", "x.mp4", 30, db.now()))
    _reviewed(conn, e, 55)
    assert _pass(conn, e) == ["科普基线起点定为 55 分（本期评审分）。"]
    assert ratchet.baseline(conn, "科普") == 55


def test_baseline_min_threshold(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    eps = [_new_ep(conn, title=f"t{i}") for i in range(3)]
    notes = []
    for e, t in zip(eps, [80, 90, 70]):
        _reviewed(conn, e, t)
        notes.append(_pass(conn, e))
    assert notes[0] == ["前瞻基线保持 60（已通过 1 期，满 3 期后按中位数调整）。"]
    assert notes[1] == ["前瞻基线保持 60（已通过 2 期，满 3 期后按中位数调整）。"]
    assert notes[2] == ["前瞻基线 60 → 80（最近 3 期中位数）。"]
    h = conn.execute("SELECT episode_id FROM baseline_history WHERE branch='前瞻' AND old_value IS NOT NULL").fetchall()
    assert [r[0] for r in h] == [eps[2]["id"]]
    assert ratchet.passed_count(conn, "前瞻") == 3


def test_kepu_first_sets_start_then_waits(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    e1, e2 = _new_ep(conn, branch="科普", title="轮胎"), _new_ep(conn, branch="科普", title="尾翼")
    _reviewed(conn, e1, 55)
    assert _pass(conn, e1) == ["科普基线起点定为 55 分（本期评审分）。"]
    _reviewed(conn, e2, 80)
    assert _pass(conn, e2) == ["科普基线保持 55（已通过 2 期，满 3 期后按中位数调整）。"]
    assert ratchet.baseline(conn, "科普") == 55


def test_ratchet_no_review_keeps_baseline(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    e = _new_ep(conn)
    _to(conn, e["id"], "G3待审")
    assert _pass(conn, e) == ["本期最后一次合成没有 AI 评分，前瞻基线不变。"]
    assert ratchet.baseline(conn, "前瞻") == 60


# ---------- 用户打分与重新校准 ----------

def test_recalibrate_alert(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    sent = []
    monkeypatch.setattr(ratchet, "notify", lambda title, body, eid=None: sent.append(title))
    e = _new_ep(conn)
    _reviewed(conn, e, 70)
    notes = _pass(conn, e, 85)
    assert notes[0].startswith("你给成片 85 分，AI 评审 70 分，相差 15 分") and sent
    a = ratchet.alerts(conn, e["id"])
    assert a[0]["user"] == 85 and a[0]["ai"] == 70 and a[0]["diff"] == 15
    assert steps.review_data(conn, e)["recalibrate"] == a
    e2 = _new_ep(conn, title="差得不多")
    _reviewed(conn, e2, 70)
    assert len(_pass(conn, e2, 80)) == 1 and ratchet.alerts(conn, e2["id"]) == []


class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_server_score_and_notes(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    monkeypatch.setattr(ratchet, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(steps, "EXECUTORS", {})
    e = _new_ep(conn)
    _reviewed(conn, e, 60)
    H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}
    url = f"/api/episodes/{e['id']}/gates/G3"
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.post(f"{url}/approve", json={"score": 120}, headers=H).status_code == 400
        assert c.post(f"{url}/approve", json={"score": "好"}, headers=H).status_code == 400
        r = c.post(f"{url}/reject", json={"feedback": "开头再改", "back_to": "配素材与粗剪", "score": 40}, headers=H)
        assert r.status_code == 200 and "相差 20 分" in r.json()["notes"][0]
        e2 = _new_ep(conn, title="过")
        _reviewed(conn, e2, 70)
        r = c.post(f"/api/episodes/{e2['id']}/gates/G3/approve", json={"score": "72"}, headers=H)
        assert r.json()["notes"] == ["前瞻基线保持 60（已通过 1 期，满 3 期后按中位数调整）。"]
    g = conn.execute("SELECT user_score FROM gates WHERE episode_id=? AND state='rejected'", (e["id"],)).fetchone()
    assert g[0] == 40 and ratchet.baseline(conn, "前瞻") == 60

