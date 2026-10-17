import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes import config, db, episodes, pipeline
from hermes.codex_runner import CodexError, QuotaExhausted, parse_events
from hermes.scheduler import Scheduler


def make_sched(db_path, executors=None, **kw):
    ex = {"*": _ok}
    ex.update(executors or {})
    return Scheduler(lambda: db.connect(db_path), ex, tick_seconds=0, backoff=(0, 0, 0), quota_poll=0, **kw)


async def _ok(task, ep):
    await asyncio.sleep(0)
    return {"step": task["step"]}


async def drive(sched, until, limit=500):
    for _ in range(limit):
        await sched.tick()
        await asyncio.sleep(0.001)
        if until():
            return
    raise AssertionError("超过步数上限仍未达到预期状态")


def state(conn, eid):
    return conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0]


def new_episode(conn, branch="复盘", gp="巴林大奖赛", title="测试期"):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch=branch, gp_label=gp, title=title)
    return eid


def test_naming_and_folders(conn):
    when = datetime(2026, 9, 29, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    e1, e2 = episodes.new_session(conn), episodes.new_session(conn)
    c1 = episodes.start(conn, e1, branch="复盘", gp_label="巴林大奖赛", title="a", when=when)
    c2 = episodes.start(conn, e2, branch="复盘", gp_label="巴林大奖赛", title="b", when=when)
    assert (c1, c2) == ("20260929-复盘-巴林大奖赛-01", "20260929-复盘-巴林大奖赛-02")
    folder = config.PRODUCTIONS_DIR / c1
    assert all((folder / s).is_dir() for s in episodes.EPISODE_SUBDIRS)


def test_full_flow_g0_to_g3(conn, db_path):
    eid = new_episode(conn)
    sched = make_sched(db_path)
    for gate in ("G0", "G1", "G2", "G3"):
        asyncio.run(drive(sched, lambda: state(conn, eid) == pipeline.STATE_OF_GATE[gate]))
        episodes.approve(conn, eid, gate, user_score=70 if gate == "G3" else None)
    asyncio.run(drive(sched, lambda: state(conn, eid) == "待发布"))
    episodes.publish(conn, eid)  # 上传 Drive 由用户手动点
    asyncio.run(drive(sched, lambda: state(conn, eid) == "归档倒计时"))
    steps = [r[0] for r in conn.execute("SELECT step FROM tasks WHERE episode_id=? ORDER BY id", (eid,))]
    expected = [s for st in pipeline.WORK_STAGES for ph in pipeline.STAGE_PHASES[st] for s, _, _ in ph]
    assert sorted(steps) == sorted(expected)
    assert conn.execute("SELECT COUNT(*) FROM gates WHERE episode_id=? AND state='approved'", (eid,)).fetchone()[0] == 4


def test_reject_reruns_stage(conn, db_path):
    eid = new_episode(conn)
    sched = make_sched(db_path)
    asyncio.run(drive(sched, lambda: state(conn, eid) == "G0待审"))
    episodes.approve(conn, eid, "G0")
    asyncio.run(drive(sched, lambda: state(conn, eid) == "G1待审"))
    episodes.reject(conn, eid, "G1", "开头太慢")
    assert state(conn, eid) == "写逐字稿"
    asyncio.run(drive(sched, lambda: state(conn, eid) == "G1待审"))
    runs = conn.execute("SELECT COUNT(DISTINCT stage_run) FROM tasks WHERE episode_id=? AND step='写逐字稿'", (eid,)).fetchone()[0]
    assert runs == 2
    with pytest.raises(ValueError):
        episodes.reject(conn, eid, "G1", "x", back_to="配音")


def test_max_three_active_and_gate_frees_slot(conn, db_path):
    ids = [new_episode(conn, title=f"期{i}") for i in range(5)]
    sched = make_sched(db_path)
    asyncio.run(sched.tick())
    active = [i for i in ids if state(conn, i) in pipeline.WORK_STAGES]
    assert active == ids[:3]
    asyncio.run(drive(sched, lambda: state(conn, ids[0]) == "G0待审"))
    asyncio.run(sched.tick())
    assert state(conn, ids[3]) in pipeline.WORK_STAGES or state(conn, ids[3]).endswith("待审")


def test_jump_queue(conn, db_path):
    ids = [new_episode(conn, title=f"期{i}") for i in range(5)]
    episodes.jump(conn, ids[4])
    asyncio.run(make_sched(db_path).tick())
    assert state(conn, ids[4]) in pipeline.WORK_STAGES
    assert state(conn, ids[3]) == "排队"


def test_jianying_single_user(conn, db_path):
    peak = {"now": 0, "max": 0}

    async def jianying(task, ep):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.01)
        peak["now"] -= 1
        return {}

    a, b = new_episode(conn, title="A"), new_episode(conn, title="B")
    sched = make_sched(db_path, {"剪映生成配音": jianying})
    for gate in ("G0", "G1"):
        asyncio.run(drive(sched, lambda: all(state(conn, e) == pipeline.STATE_OF_GATE[gate] for e in (a, b))))
        episodes.approve(conn, a, gate)
        episodes.approve(conn, b, gate)
    asyncio.run(drive(sched, lambda: all(state(conn, e) == "G2待审" for e in (a, b))))
    assert peak["max"] == 1


def test_retry_then_needs_human_then_resume(conn, db_path):
    calls = {"n": 0}

    async def flaky(task, ep):
        calls["n"] += 1
        raise RuntimeError("模拟失败")

    eid = new_episode(conn)
    sched = make_sched(db_path, {"D1资料与事实": flaky})
    asyncio.run(drive(sched, lambda: state(conn, eid) == "需人工"))
    assert calls["n"] == config.MAX_TASK_ATTEMPTS
    card = conn.execute("SELECT card FROM messages WHERE episode_id=? AND role='card'", (eid,)).fetchall()   # 失败卡片（第 3 批）
    assert len(card) == 1 and json.loads(card[0][0])["steps"][0]["error"] == "RuntimeError: 模拟失败"

    sched.executors["D1资料与事实"] = _ok
    assert episodes.resume(conn, eid) == "选题与资料"
    asyncio.run(drive(sched, lambda: state(conn, eid) == "G0待审"))


def test_quota_does_not_count_as_failure(conn, db_path):
    calls = {"n": 0}

    async def quota_then_ok(task, ep):
        calls["n"] += 1
        if calls["n"] <= 5:
            raise QuotaExhausted("usage limit reached")
        return {}

    eid = new_episode(conn)
    sched = make_sched(db_path, {"D1资料与事实": quota_then_ok})
    asyncio.run(drive(sched, lambda: state(conn, eid) == "G0待审"))
    assert calls["n"] == 6


def test_recover_after_restart(conn, db_path):
    eid = new_episode(conn)
    sched = make_sched(db_path)
    asyncio.run(sched.tick())
    conn.execute("UPDATE tasks SET state='running', attempts=1 WHERE episode_id=?", (eid,))
    restarted = make_sched(db_path)
    assert restarted.recover() > 0
    asyncio.run(drive(restarted, lambda: state(conn, eid) == "G0待审"))


def test_pause_resume_cancel(conn, db_path):
    eid = new_episode(conn)
    episodes.pause(conn, eid)
    asyncio.run(make_sched(db_path).tick())
    assert state(conn, eid) == "暂停"
    episodes.resume(conn, eid)
    assert state(conn, eid) == "排队"
    episodes.cancel(conn, eid)
    assert state(conn, eid) == "已取消"


def test_parse_events():
    ok = parse_events([
        '{"type":"thread.started","thread_id":"t1"}',
        'noise line',
        '{"type":"item.completed","item":{"type":"agent_message","text":"收到"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,"output_tokens":5,"reasoning_output_tokens":2}}',
    ])
    assert (ok.thread_id, ok.message, ok.usage["input_tokens"], ok.usage["reasoning_output_tokens"]) == ("t1", "收到", 100, 2)
    with pytest.raises(QuotaExhausted):
        parse_events(['{"type":"error","message":"You have hit your usage limit"}'])
    with pytest.raises(CodexError):
        parse_events(['{"type":"turn.failed","error":{"message":"boom"}}'])
    flaky = parse_events([
        '{"type":"error","message":"Reconnecting... 2/5 (request timed out)"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"好了"}}',
        '{"type":"turn.completed","usage":{}}',
    ])
    assert flaky.message == "好了"
