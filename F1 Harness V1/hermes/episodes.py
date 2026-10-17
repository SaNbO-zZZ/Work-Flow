"""会话与期的操作：开新会话、立项排队、命名建目录、暂停/继续/取消/插队、关卡审核。"""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, pipeline
from .db import now, transaction

BEIJING = ZoneInfo("Asia/Shanghai")
EPISODE_SUBDIRS = ["01_资料", "02_文案", "03_配音", "04_素材", "05_图形", "06_合成", "07_QA", "08_交付", "logs"]
BRANCHES = ("资讯", "前瞻", "复盘", "科普")
# 期标题 = 前缀丨发布标题；网页、制作记录、Drive 文件夹统一用（用户 2026-10-13 定）
TITLE_PREFIX = {"前瞻": "大奖赛前瞻", "资讯": "资讯", "复盘": "复盘", "科普": "科普"}


class EpisodeError(ValueError):
    pass


def new_session(conn: sqlite3.Connection, title: str = "新会话") -> int:
    ts = now()
    cur = conn.execute("INSERT INTO episodes(title, state, created_at, updated_at) VALUES (?, '闲聊', ?, ?)", (title, ts, ts))
    return cur.lastrowid


def get(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise EpisodeError(f"没有 id={episode_id} 的会话")
    return row


def make_code(conn: sqlite3.Connection, branch: str, gp_label: str, when: datetime | None = None) -> str:
    day = (when or datetime.now(BEIJING)).astimezone(BEIJING).strftime("%Y%m%d")
    prefix = f"{day}-{branch}-"
    used = conn.execute("SELECT COUNT(*) FROM episodes WHERE code LIKE ?", (prefix + "%",)).fetchone()[0]
    return f"{prefix}{gp_label}-{used + 1:02d}"


def start(conn: sqlite3.Connection, episode_id: int, *, branch: str, gp_label: str, title: str,
          kind: str | None = None, target_seconds: int | None = None, deadline_at: str | None = None,
          series_no: int | None = None, when: datetime | None = None) -> str:
    """闲聊会话立项：分配期号、建目录、进入排队（按发起先后）。"""
    if branch not in BRANCHES:
        raise EpisodeError(f"分支必须是 {BRANCHES}")
    ep = get(conn, episode_id)
    if ep["state"] != "闲聊":
        raise EpisodeError("只有闲聊会话可以立项")
    with transaction(conn):
        code = make_code(conn, branch, gp_label, when)
        folder = config.PRODUCTIONS_DIR / code
        for sub in EPISODE_SUBDIRS:
            (folder / sub).mkdir(parents=True, exist_ok=True)
        # Codex 从工作目录的 .agents/skills 发现技能；exFAT 支持符号链接
        (folder / ".agents").mkdir(exist_ok=True)
        link = folder / ".agents" / "skills"
        if not link.is_symlink():
            link.symlink_to(config.SKILLS_DIR)
        ts = now()
        conn.execute(
            """UPDATE episodes SET code=?, branch=?, kind=?, gp_label=?, title=?, target_seconds=?, deadline_at=?,
               series_no=?, folder=?, state='排队', queued_at=?, updated_at=? WHERE id=?""",
            (code, branch, kind, gp_label, title, target_seconds, deadline_at, series_no, str(folder), ts, ts, episode_id),
        )
    return code


def full_title(branch: str | None, publish_title: str) -> str:
    return f"{TITLE_PREFIX.get(branch or '', branch or '')}丨{publish_title.strip()}"


def set_publish_title(conn: sqlite3.Connection, episode_id: int, publish_title: str) -> str:
    """写文案定了发布标题后，期标题改成“前缀丨发布标题”。"""
    ep = get(conn, episode_id)
    title = full_title(ep["branch"], publish_title)
    conn.execute("UPDATE episodes SET title=?, updated_at=? WHERE id=?", (title, now(), episode_id))
    return title


def set_state(conn: sqlite3.Connection, episode_id: int, state: str) -> None:
    """进入工作阶段时轮次 +1；进入关卡时创建待审记录。调用方负责事务。"""
    ts = now()
    if state in pipeline.WORK_STAGES:
        conn.execute("UPDATE episodes SET state=?, stage_run=stage_run+1, updated_at=? WHERE id=?", (state, ts, episode_id))
    else:
        conn.execute("UPDATE episodes SET state=?, updated_at=? WHERE id=?", (state, ts, episode_id))
    if state in pipeline.GATE_OF_STATE:
        gate = pipeline.GATE_OF_STATE[state]
        gid = conn.execute("INSERT INTO gates(episode_id, gate, created_at) VALUES (?, ?, ?)",
                           (episode_id, gate, ts)).lastrowid
        # 会话里留一张关卡卡片，审完也一直显示这一版（第 3 批）
        conn.execute("INSERT INTO messages(episode_id, role, content, card, created_at) VALUES (?, 'card', ?, ?, ?)",
                     (episode_id, f"{gate} 待审", json.dumps({"type": "gate", "gate": gate, "gate_id": gid}), ts))


def _pending_gate(conn: sqlite3.Connection, episode_id: int, gate: str) -> sqlite3.Row:
    ep = get(conn, episode_id)
    if ep["state"] != pipeline.STATE_OF_GATE[gate]:
        raise EpisodeError(f"当前状态是 {ep['state']}，不是 {gate} 待审")
    return conn.execute("SELECT * FROM gates WHERE episode_id=? AND gate=? AND state='pending' ORDER BY id DESC LIMIT 1",
                        (episode_id, gate)).fetchone()


def approve(conn: sqlite3.Connection, episode_id: int, gate: str, user_score: int | None = None, feedback: str | None = None) -> str:
    with transaction(conn):
        g = _pending_gate(conn, episode_id, gate)
        conn.execute("UPDATE gates SET state='approved', user_score=?, feedback=?, decided_at=? WHERE id=?",
                     (user_score, feedback, now(), g["id"]))
        nxt = pipeline.next_state(pipeline.STATE_OF_GATE[gate])
        set_state(conn, episode_id, nxt)
    return nxt


def reject(conn: sqlite3.Connection, episode_id: int, gate: str, feedback: str, back_to: str | None = None,
           user_score: int | None = None) -> str:
    target = pipeline.reject_target(gate, back_to)
    with transaction(conn):
        g = _pending_gate(conn, episode_id, gate)
        conn.execute("UPDATE gates SET state='rejected', feedback=?, user_score=?, decided_at=? WHERE id=?",
                     (feedback, user_score, now(), g["id"]))
        set_state(conn, episode_id, target)
    return target


RESOURCE_TAG = "【重跑找素材】"
RESOURCE_FROM = ("配素材与粗剪", "G2待审", "G3待审", "合成与QA")


def resource(conn: sqlite3.Connection, episode_id: int, note: str = "") -> int:
    """重跑找素材：G2/G3 待审，或停在配素材到合成之间的暂停、需人工，退回“配素材与粗剪”重跑，返回新的轮次。
    待审的关卡记为打回（意见带【重跑找素材】），所以剪辑决策也能看到。正在跑的阶段要先暂停。"""
    with transaction(conn):
        ep = get(conn, episode_id)
        paused = ep["state"] in ("暂停", "需人工")
        at = ep["paused_from"] if paused else ep["state"]
        if at not in RESOURCE_FROM or (not paused and at in pipeline.WORK_STAGES):
            hint = "，先暂停再重跑找素材" if at in RESOURCE_FROM else "；只有配素材与粗剪到 G3 之间能重跑找素材"
            raise EpisodeError(f"当前状态是 {ep['state']}{hint}")
        if at in pipeline.GATE_OF_STATE:
            conn.execute("UPDATE gates SET state='rejected', feedback=?, decided_at=? "
                         "WHERE episode_id=? AND gate=? AND state='pending'",
                         (RESOURCE_TAG + (note.strip() or "按新要求重找素材"), now(), episode_id,
                          pipeline.GATE_OF_STATE[at]))
        conn.execute("UPDATE tasks SET state='cancelled' WHERE episode_id=? AND stage_run=? "
                     "AND state IN ('queued','waiting_quota','failed')", (episode_id, ep["stage_run"]))
        conn.execute("UPDATE episodes SET paused_from=NULL WHERE id=?", (episode_id,))
        set_state(conn, episode_id, "配素材与粗剪")
    return ep["stage_run"] + 1


def publish(conn: sqlite3.Connection, episode_id: int) -> None:
    """待发布时用户手动点“上传 Drive”：清掉上次的核对结果（封面等可能重做过），进上传Drive。"""
    with transaction(conn):
        ep = get(conn, episode_id)
        if ep["state"] != "待发布":
            raise EpisodeError(f"当前状态是 {ep['state']}，只有待发布的期可以上传 Drive")
        conn.execute("UPDATE archives SET upload_state='pending', delete_after=NULL, reminded_at=NULL "
                     "WHERE episode_id=? AND deleted_at IS NULL", (episode_id,))
        set_state(conn, episode_id, "上传Drive")


def pause(conn: sqlite3.Connection, episode_id: int) -> None:
    with transaction(conn):
        ep = get(conn, episode_id)
        if ep["state"] in pipeline.OTHER_STATES | pipeline.TERMINAL_STATES:
            raise EpisodeError(f"{ep['state']} 状态不能暂停")
        conn.execute("UPDATE episodes SET paused_from=state, state='暂停', updated_at=? WHERE id=?", (now(), episode_id))


def resume(conn: sqlite3.Connection, episode_id: int) -> str:
    """继续暂停的期；对"需人工"的期，把失败任务清零重排。"""
    with transaction(conn):
        ep = get(conn, episode_id)
        if ep["state"] not in ("暂停", "需人工"):
            raise EpisodeError("只有暂停或需人工的期可以继续")
        if ep["state"] == "需人工":
            conn.execute("""UPDATE tasks SET state='queued', attempts=0, not_before=NULL, error=NULL
                            WHERE episode_id=? AND stage_run=? AND state='failed'""", (episode_id, ep["stage_run"]))
        conn.execute("UPDATE episodes SET state=paused_from, paused_from=NULL, updated_at=? WHERE id=?", (now(), episode_id))
    return ep["paused_from"]


def cancel(conn: sqlite3.Connection, episode_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE tasks SET state='cancelled' WHERE episode_id=? AND state IN ('queued','waiting_quota')", (episode_id,))
        conn.execute("UPDATE episodes SET state='已取消', updated_at=? WHERE id=?", (now(), episode_id))


def jump(conn: sqlite3.Connection, episode_id: int) -> None:
    conn.execute("UPDATE episodes SET jump_at=?, updated_at=? WHERE id=?", (now(), now(), episode_id))
