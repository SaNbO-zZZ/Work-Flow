"""调度器：按发起先后（可插队）推进各期；最多 3 期同时制作（待审不占名额）；
剪映同时只给 1 个任务；失败重试 3 次；额度用完只等待不计次；重启后接着做。"""
import asyncio
import json
import logging
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from . import config, pipeline, ratchet, versions
from .codex_runner import QuotaExhausted
from .db import now, transaction
from .episodes import set_state

log = logging.getLogger("hermes.scheduler")

Executor = Callable[[sqlite3.Row, sqlite3.Row], Awaitable[dict]]
EventHook = Callable[[str, sqlite3.Row, str], None]


class StepNotReady(Exception):
    """该步骤所属里程碑还没实现：暂停这一期并说明，不算失败。"""


class StepDeferred(Exception):
    """稍后再做（用户点了“稍后”、等用户手动完成）：任务退回排队，seconds 秒后再跑，不算失败。"""

    def __init__(self, seconds: float, reason: str = ""):
        super().__init__(reason)
        self.seconds = seconds


class NeedsHuman(Exception):
    """程序和 GPT 都修不好、要用户处理：任务直接记失败、这一期转需人工，不再自动重试（2026-10-15）。"""


def _reason(err: str | None) -> str:
    """出错记录的最后一行（异常类型和说明）。"""
    lines = [x.strip() for x in (err or "").strip().splitlines() if x.strip()]
    return lines[-1][:300] if lines else "没有记录原因"


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Scheduler:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], executors: dict[str, Executor],
                 tick_seconds: float = 1.0, backoff: tuple[float, ...] = config.RETRY_BACKOFF_SECONDS,
                 quota_poll: float = config.QUOTA_POLL_SECONDS, on_event: EventHook | None = None):
        self.on_event = on_event or (lambda kind, ep, text: None)
        self.conn_factory = conn_factory
        self.conn = conn_factory()
        self.executors = executors
        self.tick_seconds = tick_seconds
        self.backoff = backoff
        self.quota_poll = quota_poll
        self.running: dict[int, asyncio.Task] = {}
        self.jianying_busy = False
        self.snapshots: list[tuple] = []   # 刚进关卡、还没存版本的（存版本在事务外做）
        self._stop = asyncio.Event()

    # ---------- 生命周期 ----------
    def recover(self) -> int:
        """启动时：上次中断在运行中的任务退回排队，不计失败次数。"""
        cur = self.conn.execute("UPDATE tasks SET state='queued', attempts=MAX(attempts-1,0) WHERE state='running'")
        return cur.rowcount

    async def run_forever(self) -> None:
        self.recover()
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), self.tick_seconds)
            except TimeoutError:
                pass
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    # ---------- 每一拍 ----------
    async def tick(self) -> None:
        self._promote_queued()
        self._advance_episodes()
        await self._save_snapshots()
        self._release_quota_waits()
        self._launch_tasks()

    async def _save_snapshots(self) -> None:
        """进关卡时把要审的东西存进 09_版本（成片 100 多 MB，放线程里复制）。在派新任务之前做完，免得被下一步改掉。"""
        while self.snapshots:
            await asyncio.to_thread(versions.snapshot_gate, *self.snapshots.pop(0))

    def _active_count(self) -> int:
        marks = ",".join("?" * len(pipeline.WORK_STAGES))
        return self.conn.execute(f"SELECT COUNT(*) FROM episodes WHERE state IN ({marks})", pipeline.WORK_STAGES).fetchone()[0]

    def _promote_queued(self) -> None:
        while self._active_count() < config.MAX_ACTIVE_EPISODES:
            ep = self.conn.execute(
                """SELECT id FROM episodes WHERE state='排队'
                   ORDER BY jump_at IS NULL, jump_at DESC, queued_at, id LIMIT 1""").fetchone()
            if ep is None:
                return
            with transaction(self.conn):
                set_state(self.conn, ep["id"], pipeline.next_state("排队"))

    def _advance_episodes(self) -> None:
        marks = ",".join("?" * len(pipeline.WORK_STAGES))
        for ep in self.conn.execute(f"SELECT * FROM episodes WHERE state IN ({marks})", pipeline.WORK_STAGES).fetchall():
            self._advance_one(ep)

    def _advance_one(self, ep: sqlite3.Row) -> None:
        stage, run = ep["state"], ep["stage_run"]
        tasks = self.conn.execute("SELECT * FROM tasks WHERE episode_id=? AND stage_run=? ORDER BY phase, id",
                                  (ep["id"], run)).fetchall()
        phases = pipeline.STAGE_PHASES[stage]
        with transaction(self.conn):
            if not tasks:
                self._create_phase(ep, 0)
                return
            current = max(t["phase"] for t in tasks)
            batch = [t for t in tasks if t["phase"] == current]
            failed = [t for t in batch if t["state"] == "failed"]
            if failed:
                self.conn.execute("UPDATE episodes SET paused_from=state, state='需人工', updated_at=? WHERE id=?",
                                  (now(), ep["id"]))
                # 失败卡片写清哪一步、什么原因，网页上直接给“继续”（第 3 批）；没重试满就失败的是 NeedsHuman
                human = all(t["attempts"] < config.MAX_TASK_ATTEMPTS for t in failed)
                versions.add_card(self.conn, ep["id"], {
                    "type": "failed", "stage": stage, "attempts": config.MAX_TASK_ATTEMPTS, "needs_human": human,
                    "steps": [{"step": t["step"], "error": _reason(t["error"])} for t in failed]},
                    f"「{ep['title']}」{stage}阶段有步骤需要你处理，已暂停" if human else
                    f"「{ep['title']}」{stage}阶段有步骤重试 {config.MAX_TASK_ATTEMPTS} 次仍失败，已暂停等你处理")
                self.on_event("needs_human", ep, stage)
                return
            if not all(t["state"] == "succeeded" for t in batch):
                return
            if current + 1 < len(phases):
                self._create_phase(ep, current + 1)
            else:
                nxt = pipeline.next_state(stage)
                set_state(self.conn, ep["id"], nxt)
                if nxt in pipeline.GATE_OF_STATE:
                    gid = self.conn.execute("SELECT MAX(id) FROM gates WHERE episode_id=?", (ep["id"],)).fetchone()[0]
                    job = versions.gate_job(self.conn, ep["id"], gid)
                    if job:
                        self.snapshots.append(job)
                    note = ratchet.auto_pass(self.conn, ep["id"], pipeline.GATE_OF_STATE[nxt])
                    if note:
                        self._notify(ep["id"], note)
                    else:
                        self.on_event("gate", ep, nxt)
                elif nxt == "待发布":
                    self._notify(ep["id"], "成片已就绪：在下方“展示成果”里看成片、封面、标题和简介，"
                                           "确认后手动点“上传 Drive”。")
                    self.on_event("publish", ep, nxt)

    def _create_phase(self, ep: sqlite3.Row, phase: int) -> None:
        ts = now()
        for step, role, jianying in pipeline.STAGE_PHASES[ep["state"]][phase]:
            model, effort = config.ROLE_MODELS.get(role, ("程序", "-"))
            self.conn.execute(
                """INSERT INTO tasks(episode_id, stage, step, stage_run, phase, role, model, effort, needs_jianying, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ep["id"], ep["state"], step, ep["stage_run"], phase, role, model, effort, int(jianying), ts))

    def _release_quota_waits(self) -> None:
        self.conn.execute("UPDATE tasks SET state='queued' WHERE state='waiting_quota' AND not_before <= ?", (now(),))

    def _launch_tasks(self) -> None:
        marks = ",".join("?" * len(pipeline.WORK_STAGES))
        rows = self.conn.execute(
            f"""SELECT t.* FROM tasks t JOIN episodes e ON e.id=t.episode_id
                WHERE t.state='queued' AND e.state IN ({marks}) AND t.stage_run=e.stage_run
                  AND (t.not_before IS NULL OR t.not_before <= ?)
                ORDER BY e.jump_at IS NULL, e.jump_at DESC, e.queued_at, t.id""",
            (*pipeline.WORK_STAGES, now())).fetchall()
        for t in rows:
            if t["id"] in self.running:
                continue
            if t["needs_jianying"]:
                if self.jianying_busy:
                    continue
                self.jianying_busy = True
            self.conn.execute("UPDATE tasks SET state='running', attempts=attempts+1, started_at=? WHERE id=?", (now(), t["id"]))
            self.running[t["id"]] = asyncio.create_task(self._run_task(t["id"]))

    # ---------- 执行单个任务 ----------
    async def _run_task(self, task_id: int) -> None:
        conn = self.conn_factory()
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (task["episode_id"],)).fetchone()
        try:
            executor = self.executors.get(task["step"]) or self.executors["*"]
            output = await executor(task, ep)
            conn.execute("UPDATE tasks SET state='succeeded', output=?, error=NULL, finished_at=? WHERE id=?",
                         (json.dumps(output, ensure_ascii=False), now(), task_id))
        except StepNotReady as e:
            conn.execute("UPDATE tasks SET state='queued', attempts=MAX(attempts-1,0) WHERE id=?", (task_id,))
            paused = conn.execute("UPDATE episodes SET paused_from=state, state='暂停', updated_at=? WHERE id=? AND state!='暂停'",
                                  (now(), ep["id"])).rowcount
            if paused:
                self._notify(ep["id"], f"「{task['step']}」还没实现（{e}），这一期先暂停", conn)
        except StepDeferred as e:
            conn.execute("UPDATE tasks SET state='queued', attempts=MAX(attempts-1,0), not_before=?, error=? WHERE id=?",
                         (_later(e.seconds), str(e)[:2000] or None, task_id))
        except QuotaExhausted as e:
            conn.execute("""UPDATE tasks SET state='waiting_quota', attempts=MAX(attempts-1,0), not_before=?, error=?
                            WHERE id=?""", (_later(self.quota_poll), str(e)[:2000], task_id))
            self._notify(ep["id"], "订阅额度已用完，任务在等额度恢复，恢复后自动继续", conn)
        except NeedsHuman as e:
            conn.execute("UPDATE tasks SET state='failed', error=?, finished_at=? WHERE id=?",
                         (str(e)[:4000], now(), task_id))
            log.warning("任务 %s(%s) 需人工：%s", task_id, task["step"], str(e)[:500])
        except asyncio.CancelledError:
            # 程序退出时被中断：退回排队，下次启动接着做
            conn.execute("UPDATE tasks SET state='queued', attempts=MAX(attempts-1,0) WHERE id=?", (task_id,))
            raise
        except Exception:
            err = traceback.format_exc()[-4000:]
            attempts = task["attempts"]
            if attempts < config.MAX_TASK_ATTEMPTS:
                delay = self.backoff[min(attempts - 1, len(self.backoff) - 1)]
                conn.execute("UPDATE tasks SET state='queued', not_before=?, error=? WHERE id=?", (_later(delay), err, task_id))
            else:
                conn.execute("UPDATE tasks SET state='failed', error=?, finished_at=? WHERE id=?", (err, now(), task_id))
            log.warning("任务 %s(%s) 第 %s 次失败：%s", task_id, task["step"], attempts, err[-500:])
        finally:
            if task["needs_jianying"]:
                self.jianying_busy = False
            self.running.pop(task_id, None)
            conn.close()

    def _notify(self, episode_id: int, text: str, conn: sqlite3.Connection | None = None) -> None:
        (conn or self.conn).execute(
            "INSERT INTO messages(episode_id, role, content, created_at) VALUES (?, 'system', ?, ?)",
            (episode_id, text, now()))
