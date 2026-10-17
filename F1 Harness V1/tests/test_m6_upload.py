"""M6-9 上传Drive 接入流水线：上传与核对 + 经验总结同批并行；没连 Drive 延后不计失败；失败重试、
核对不一致报错；已核对过跳过；走调度器到归档倒计时。Drive 和 codex 全部 mock。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hermes import codex_runner, config, db, drive, episodes, pipeline, prompts, steps
from hermes.scheduler import Scheduler, StepDeferred

ORIG_CONNECT = db.connect


@pytest.fixture(autouse=True)
def _patched(db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{ctx}|{notes}")
    sent = []
    monkeypatch.setattr(steps, "notify", lambda title, body, eid=None: sent.append(title))
    return sent


@pytest.fixture
def ep(conn):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    with db.transaction(conn):
        episodes.set_state(conn, eid, "上传Drive")
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def _fake_upload(calls, *, mismatch=(), error=None):
    async def up(conn, eid, **kw):
        calls.append(eid)
        if error:
            raise error
        if not mismatch:
            ts = datetime.now(timezone.utc)
            drive._set_archive(conn, eid, drive_path="F1 Hermes/前瞻/x", upload_state="verified",
                               verified_at=ts.isoformat(timespec="seconds"),
                               delete_after=(ts + timedelta(days=drive.KEEP_DAYS)).isoformat(timespec="seconds"))
        return {"files": 12, "bytes": 2_500_000_000, "mismatch": list(mismatch), "drive_path": "F1 Hermes/前瞻/x"}
    return up


def _task(attempts=1, error=None, step="上传与核对"):
    return {"id": None, "step": step, "model": None, "effort": None, "attempts": attempts, "error": error}


def _msgs(conn, eid):
    return [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=? ORDER BY id", (eid,))]


def _state(conn, eid):
    return conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()[0]


def test_pipeline_stage_has_both_steps():
    """G3 通过 → 经验总结 → 待发布（等用户手动点上传）→ 上传Drive → 归档倒计时。"""
    assert pipeline.next_state("G3待审") == "经验总结"
    assert pipeline.STAGE_PHASES["经验总结"] == [[("经验总结", "总控", False)]]
    assert pipeline.next_state("经验总结") == "待发布" and "待发布" not in pipeline.WORK_STAGES
    assert pipeline.next_state("待发布") == "上传Drive"
    assert pipeline.STAGE_PHASES["上传Drive"] == [[("上传与核对", "程序", False)]]
    assert "上传与核对" in steps.EXECUTORS and "经验总结" in steps.EXECUTORS
    assert pipeline.next_state("上传Drive") == "归档倒计时"


def test_upload_success(ep, conn, monkeypatch):
    calls = []
    monkeypatch.setattr(drive, "configured", lambda: True)
    monkeypatch.setattr(drive, "upload_episode", _fake_upload(calls))
    out = asyncio.run(steps.upload_step(_task(), ep))
    assert calls == [ep["id"]] and out["files"] == 12 and out["drive_path"] == "F1 Hermes/前瞻/x"
    assert out["delete_after"]
    msg = _msgs(conn, ep["id"])[-1]
    assert "已上传到 Drive「F1 Hermes/前瞻/x」" in msg and "12 个文件，共 2.50 GB" in msg and "7 天后" in msg
    # 再跑一次（重启续跑）直接跳过
    out2 = asyncio.run(steps.upload_step(_task(), ep))
    assert "skipped" in out2 and calls == [ep["id"]]


def test_upload_not_configured_defers_and_notifies_once(ep, conn, monkeypatch, _patched):
    monkeypatch.setattr(drive, "configured", lambda: False)
    monkeypatch.setattr(drive, "upload_episode", _fake_upload([]))
    with pytest.raises(StepDeferred) as e:
        asyncio.run(steps.upload_step(_task(), ep))
    assert e.value.seconds == steps.DRIVE_WAIT and str(e.value) == steps.DRIVE_HINT
    with pytest.raises(StepDeferred):
        asyncio.run(steps.upload_step(_task(error=steps.DRIVE_HINT), ep))
    assert sum(steps.DRIVE_HINT in m for m in _msgs(conn, ep["id"])) == 1 and len(_patched) == 1


def test_upload_error_retry_then_manual(ep, conn, monkeypatch):
    monkeypatch.setattr(drive, "configured", lambda: True)
    monkeypatch.setattr(drive, "upload_episode", _fake_upload([], error=drive.DriveError("上传失败：断网")))
    with pytest.raises(drive.DriveError):
        asyncio.run(steps.upload_step(_task(1), ep))
    assert "稍后自动重试（第 1 次）" in _msgs(conn, ep["id"])[-1]
    with pytest.raises(drive.DriveError):
        asyncio.run(steps.upload_step(_task(config.MAX_TASK_ATTEMPTS), ep))
    assert "转为需人工" in _msgs(conn, ep["id"])[-1] and "断网" in _msgs(conn, ep["id"])[-1]


def test_upload_mismatch_raises(ep, conn, monkeypatch):
    monkeypatch.setattr(drive, "configured", lambda: True)
    monkeypatch.setattr(drive, "upload_episode", _fake_upload([], mismatch=["* a.mp4", "+ b.wav"]))
    with pytest.raises(RuntimeError, match="2 个文件不一致"):
        asyncio.run(steps.upload_step(_task(), ep))
    assert "a.mp4" in _msgs(conn, ep["id"])[-1]


def _lesson_runner(answer, seen=None):
    async def run(prompt, model, effort, workdir, **kw):
        if seen is not None:
            seen.append({"prompt": prompt, "model": model})
        return codex_runner.CodexResult("t", json.dumps(answer, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1})
    return run


def test_scheduler_reaches_archive_countdown(ep, conn, db_path, monkeypatch):
    calls, seen = [], []
    monkeypatch.setattr(drive, "configured", lambda: True)
    monkeypatch.setattr(drive, "upload_episode", _fake_upload(calls))
    answer = {"lessons": [{"branch": "前瞻", "step": "D6剪辑决策", "content": "开头 5 秒内必须出现赛车画面",
                           "basis": "AI 评审 开头吸引力 50 分"}]}
    monkeypatch.setattr(codex_runner, "run", _lesson_runner(answer, seen))
    sched = Scheduler(lambda: ORIG_CONNECT(db_path),
                      {"上传与核对": steps.upload_step, "经验总结": steps.lesson_summary},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    with db.transaction(conn):
        episodes.set_state(conn, ep["id"], "经验总结")

    async def go(until):
        for _ in range(500):
            await sched.tick()
            await asyncio.sleep(0.001)
            if _state(conn, ep["id"]) == until and not sched.running:
                return
        raise AssertionError(f"停在 {_state(conn, ep['id'])}")
    asyncio.run(go("待发布"))
    for _ in range(5):  # 待发布不会自己上传
        asyncio.run(sched.tick())
    assert _state(conn, ep["id"]) == "待发布" and calls == [] and len(seen) == 1
    assert any("展示成果" in m and "上传 Drive" in m for m in _msgs(conn, ep["id"]))
    with pytest.raises(episodes.EpisodeError):
        episodes.publish(conn, ep["id"] + 999)
    episodes.publish(conn, ep["id"])
    asyncio.run(go("归档倒计时"))
    assert calls == [ep["id"]] and len(seen) == 1
    model, _ = config.ROLE_MODELS["总控"]
    assert seen[0]["model"] == model
    rows = conn.execute("SELECT stage, step, state FROM tasks WHERE episode_id=? AND stage IN ('经验总结', '上传Drive')",
                        (ep["id"],)).fetchall()
    assert sorted(tuple(r) for r in rows) == [("上传Drive", "上传与核对", "succeeded"), ("经验总结", "经验总结", "succeeded")]
    les = conn.execute("SELECT * FROM lessons").fetchall()
    assert len(les) == 1 and les[0]["state"] == "pending" and les[0]["step"] == "D6剪辑决策"
    arch = conn.execute("SELECT upload_state FROM archives WHERE episode_id=?", (ep["id"],)).fetchone()
    assert arch[0] == "verified"


def test_scheduler_waits_for_drive_without_failing(ep, conn, db_path, monkeypatch):
    monkeypatch.setattr(drive, "configured", lambda: False)
    monkeypatch.setattr(codex_runner, "run", _lesson_runner({"lessons": []}))
    sched = Scheduler(lambda: ORIG_CONNECT(db_path),
                      {"上传与核对": steps.upload_step, "经验总结": steps.lesson_summary},
                      tick_seconds=0, backoff=(0, 0, 0), quota_poll=0)

    async def go():
        for _ in range(20):
            await sched.tick()
            await asyncio.sleep(0.01)
    asyncio.run(go())
    t = conn.execute("SELECT state, attempts, error, not_before FROM tasks WHERE step='上传与核对'").fetchone()
    assert t["state"] == "queued" and t["attempts"] == 0 and t["error"] == steps.DRIVE_HINT and t["not_before"]
    assert _state(conn, ep["id"]) == "上传Drive"
    assert sum(steps.DRIVE_HINT in m for m in _msgs(conn, ep["id"])) == 1


def test_publish_only_from_waiting_and_resets_archive(ep, conn):
    """只有待发布能点上传；点了会清掉上次核对结果和删除倒计时（重做过封面要重传）。"""
    with pytest.raises(episodes.EpisodeError, match="只有待发布"):
        episodes.publish(conn, ep["id"])
    conn.execute("INSERT INTO archives(episode_id, drive_path, upload_state, verified_at, delete_after) "
                 "VALUES (?, 'p', 'verified', '2026-10-13T00:00:00+00:00', '2026-10-20T00:00:00+00:00')", (ep["id"],))
    conn.execute("UPDATE episodes SET state='待发布' WHERE id=?", (ep["id"],))
    episodes.publish(conn, ep["id"])
    a = conn.execute("SELECT upload_state, delete_after, drive_path FROM archives WHERE episode_id=?", (ep["id"],)).fetchone()
    assert tuple(a) == ("pending", None, "p") and _state(conn, ep["id"]) == "上传Drive"


def test_script_sets_episode_title(ep, conn, monkeypatch):
    async def fake(conn_, ep_, **kw):
        return {"title": "巴林大奖赛怎么跑到了雪邦 梅奔升级能奏效吗"}
    monkeypatch.setattr(steps.script, "write", fake)
    out = asyncio.run(steps.script_write(_task(), ep))
    assert out["episode_title"] == "大奖赛前瞻丨巴林大奖赛怎么跑到了雪邦 梅奔升级能奏效吗"
    assert episodes.get(conn, ep["id"])["title"] == out["episode_title"]
    assert episodes.full_title("复盘", " 标题 ") == "复盘丨标题"
