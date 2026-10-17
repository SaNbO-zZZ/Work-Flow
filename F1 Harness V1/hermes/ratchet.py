"""M6-7 棘轮与自动返工：成片 AI 评审低于本分支基线就自动退回重做（中间的 G1/G2 自动通过），
用户 G3 通过后按最近几期中位数抬基线；用户打分和 AI 差太多时提醒重新校准。

自动返工次数按"轮次周期"计：本期每被用户打回一次（任意关卡）就开新周期，次数从 0 算起。
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from . import db, pipeline
from .episodes import set_state
from .events import notify

STAGE_OF_STEP = {"写逐字稿": "写逐字稿", "配音": "配音", "D2找素材": "配素材与粗剪",
                 "D4图形与叠层": "配素材与粗剪", "D6剪辑决策": "配素材与粗剪"}
MAIN_SEVERITY = ("高", "中")
AUTO_GATES = ("G1", "G2")
ALERTS_KEY = "qa.recalibrate_alerts"


def _int_setting(conn, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return int(float(row["value"]))
    except (TypeError, ValueError):
        return default


def baseline(conn, branch: str) -> float | None:
    row = conn.execute("SELECT value FROM baselines WHERE branch=?", (branch,)).fetchone()
    return float(row["value"]) if row and row["value"] is not None else None


def cycle(conn, episode_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM gates WHERE episode_id=? AND state='rejected'",
                        (episode_id,)).fetchone()[0]


def rounds(conn, episode_id: int, cyc: int | None = None) -> list:
    cyc = cycle(conn, episode_id) if cyc is None else cyc
    return conn.execute("SELECT * FROM reworks WHERE episode_id=? AND cycle=? ORDER BY round",
                        (episode_id, cyc)).fetchall()


def active(conn, episode_id: int):
    return conn.execute("SELECT * FROM reworks WHERE episode_id=? AND state='running' ORDER BY id DESC LIMIT 1",
                        (episode_id,)).fetchone()


def close(conn, episode_id: int) -> None:
    conn.execute("UPDATE reworks SET state='done', finished_at=? WHERE episode_id=? AND state='running'",
                 (db.now(), episode_id))


def pick_stage(issues: list[dict]) -> tuple[str, list[str]] | None:
    """高/中问题的 back_to 里按流水线顺序最早的那一步；没有高/中就看全部问题。"""
    main = [i for i in issues if i.get("severity") in MAIN_SEVERITY] or issues
    steps = [i["back_to"] for i in main if i.get("back_to") in STAGE_OF_STEP]
    if not steps:
        return None
    stage = min((STAGE_OF_STEP[s] for s in steps), key=pipeline.WORK_STAGES.index)
    return stage, sorted(set(steps), key=list(STAGE_OF_STEP).index)


def _lines(issues: list[dict]) -> str:
    return "\n".join(f"  {i.get('at') or '全片'} [{i['severity']}] {i['item']}：{i['problem']} → 改法：{i['fix']}"
                     for i in issues)


def _issues(rw, step: str) -> list[dict]:
    return [i for i in json.loads(rw["issues"] or "[]") if i.get("back_to") == step] if rw else []


def arrow(history) -> str:
    return " → ".join(f"{t:g}" for t in history)


# ---------- 返工意见进各步骤 ----------

def feedback_for(conn, episode_id: int, step: str) -> list[str]:
    rw = active(conn, episode_id)
    issues = _issues(rw, step)
    if not issues:
        return []
    return [f"AI 评审自动返工第 {rw['round']} 轮（成片 {rw['total']:g} 分，低于基线 {rw['baseline']:g}）"
            f"指出这一步要改的问题：\n" + _lines(issues)]


def segments_at(segs: list[dict], seconds: list[float], duration: float | None) -> list[str]:
    """按各段估算时长占比，把成片时间点换成段名。"""
    total = sum(s["seconds"] for s in segs)
    if not segs or not duration or total <= 0:
        return []
    out = []
    for t in seconds:
        x, acc = t / duration * total, 0.0
        for s in segs:
            acc += s["seconds"]
            if x < acc or s is segs[-1]:
                if s["name"] not in out:
                    out.append(s["name"])
                break
    return out


def redo_text(conn, ep) -> str | None:
    """本期正在自动返工且有 D2找素材 的问题时，给 sourcing 的重找意见（写明换素材和涉及的段）。"""
    issues = _issues(active(conn, ep["id"]), "D2找素材")
    if not issues:
        return None
    from .sourcing import segments
    folder = Path(ep["folder"])
    try:
        segs = segments((folder / "02_文案" / "逐字稿.md").read_text(encoding="utf-8"))
        duration = json.loads((folder / "03_配音" / "对齐.json").read_text(encoding="utf-8")).get("duration")
    except (OSError, ValueError):
        segs, duration = [], None
    names = [] if any(i.get("at_s") is None for i in issues) else \
        segments_at(segs, [i["at_s"] for i in issues], duration)
    where = f"涉及段：{'、'.join(names)}" if names else "全片"
    return f"AI 评审要求换素材、重找画面（{where}）：\n" + _lines(issues)


# ---------- 评审后：要不要自动返工 ----------

def after_review(conn, ep, r: dict) -> tuple[dict, list[str]]:
    """返回 (写进质检汇总的 rework 信息, 要发给用户的话)。低于基线且次数没用完就退回重做。"""
    eid = ep["id"]
    cyc = cycle(conn, eid)
    done = rounds(conn, eid, cyc)
    limit = _int_setting(conn, "qa.rework_rounds", 2)
    history = [rw["total"] for rw in done] + [r["total"]]
    info = {"rounds": len(done), "limit": limit, "history": history, "back_to": None, "note": ""}
    base = r.get("baseline")
    if not r.get("below_baseline"):
        if done and base is not None:
            info["note"] = f"自动返工 {len(done)} 轮后达到基线 {base:g} 分"
        return info, []
    state = conn.execute("SELECT state FROM episodes WHERE id=?", (eid,)).fetchone()["state"]
    if state != "合成与QA":
        return info, []
    if len(done) >= limit:
        info["note"] = (f"自动返工 {len(done)} 轮后仍低于基线 {base:g} 分" if done
                        else f"低于基线 {base:g} 分（自动返工已关闭）")
        tail = f"（{arrow(history)}）" if done else ""
        return info, [f"{info['note']}{tail}，交给你在 G3 审。"]
    pick = pick_stage(r.get("issues") or [])
    if pick is None:
        info["note"] = f"低于基线 {base:g} 分，但 AI 没列出问题，没法自动返工"
        return info, [info["note"] + "，交给你在 G3 审。"]
    stage, steps = pick
    n = len(done) + 1
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO reworks(episode_id, cycle, round, review_id, total, baseline, back_to, steps, issues, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, cyc, n, r.get("review_id"), r["total"], base, stage, json.dumps(steps, ensure_ascii=False),
             json.dumps(r.get("issues") or [], ensure_ascii=False), db.now()))
        set_state(conn, eid, stage)
    info.update(rounds=n, back_to=stage)
    main = [i for i in r["issues"] if i["severity"] in MAIN_SEVERITY] or r["issues"]
    top = "；".join(f"{k}) {i.get('at') or '全片'} {i['problem']}" for k, i in enumerate(main[:3], start=1))
    return info, [f"成片 {r['total']:g} 分，低于{ep['branch']}基线 {base:g}，自动返工第 {n}/{limit} 轮：退回「{stage}」重做，"
                  f"中间的 G1/G2 自动通过，做完直接再评审。主要问题：{top}"]


def auto_pass(conn, episode_id: int, gate: str) -> str | None:
    """自动返工期间到了 G1/G2：记一条 auto 通过并直接进下一阶段。调用方负责事务。"""
    rw = active(conn, episode_id) if gate in AUTO_GATES else None
    if rw is None:
        return None
    g = conn.execute("SELECT id FROM gates WHERE episode_id=? AND gate=? AND state='pending' ORDER BY id DESC LIMIT 1",
                     (episode_id, gate)).fetchone()
    conn.execute("UPDATE gates SET state='approved', auto=1, feedback=?, decided_at=? WHERE id=?",
                 (f"自动返工第 {rw['round']} 轮，自动通过", db.now(), g["id"]))
    set_state(conn, episode_id, pipeline.next_state(pipeline.STATE_OF_GATE[gate]))
    return f"{gate} 自动通过（自动返工第 {rw['round']} 轮，你不用审，做完直接再评审）。"


# ---------- 用户 G3：棘轮和校准提醒 ----------

def final_review(conn, episode_id: int):
    """本期最近一次合成与QA 对应的成片评审；那一轮硬检查没过没评就返回 None。"""
    run = conn.execute("SELECT MAX(stage_run) FROM tasks WHERE episode_id=? AND stage='合成与QA'",
                       (episode_id,)).fetchone()[0]
    if run is None:
        return None
    return conn.execute("SELECT * FROM reviews WHERE episode_id=? AND kind='成片' AND stage_run=? ORDER BY id DESC "
                        "LIMIT 1", (episode_id, run)).fetchone()


def passed_count(conn, branch: str) -> int:
    """本分支 G3 通过且有成片评分的期数。"""
    return conn.execute("SELECT COUNT(DISTINCT episode_id) FROM reviews WHERE kind='成片' AND branch=? "
                        "AND approved_at IS NOT NULL AND total IS NOT NULL", (branch,)).fetchone()[0]


def baseline_min(conn) -> int:
    return _int_setting(conn, "qa.baseline_min", 3)


def update_baseline(conn, branch: str, *, episode_id: int | None = None, review_id: int | None = None) -> str:
    window = _int_setting(conn, "qa.baseline_window", 5)
    totals = [r[0] for r in conn.execute(
        "SELECT total FROM reviews WHERE kind='成片' AND branch=? AND approved_at IS NOT NULL AND total IS NOT NULL "
        "ORDER BY approved_at DESC, id DESC LIMIT ?", (branch, window))]
    old = baseline(conn, branch)
    if not totals:
        return f"{branch}基线不变" + (f"（{old:g}）" if old is not None else "")
    n, need = passed_count(conn, branch), baseline_min(conn)
    if old is not None and n < need:
        return f"{branch}基线保持 {old:g}（已通过 {n} 期，满 {need} 期后按中位数调整）。"
    med = round(statistics.median(totals), 1)
    new = med if old is None else max(old, med)
    if old is None:
        text = f"{branch}基线起点定为 {new:g} 分（本期评审分）。"
    elif new > old:
        text = f"{branch}基线 {old:g} → {new:g}（最近 {len(totals)} 期中位数）。"
    if new != old:
        ts = db.now()
        conn.execute("INSERT INTO baselines(branch, value, updated_at) VALUES (?,?,?) ON CONFLICT(branch) "
                     "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (branch, new, ts))
        conn.execute("INSERT INTO baseline_history(branch, old_value, value, reason, episode_id, review_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (branch, old, new, text, episode_id, review_id, ts))
        return text
    return f"{branch}基线保持 {old:g}（最近 {len(totals)} 期中位数 {med:g}，只升不降）。"


def alerts(conn, episode_id: int | None = None) -> list[dict]:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (ALERTS_KEY,)).fetchone()
    try:
        items = json.loads(row["value"]) if row else []
    except ValueError:
        items = []
    return [a for a in items if episode_id is None or a.get("episode_id") == episode_id]


def on_gate(conn, ep, gate: str, decision: str, score: int | None) -> list[str]:
    """用户在关卡做完决定后调用（ep 是决定前的行）。返回要发给用户的话。"""
    if gate != "G3":
        return []
    eid = ep["id"]
    close(conn, eid)
    rv = final_review(conn, eid)
    msgs = []
    if score is not None and rv is not None and rv["total"] is not None:
        gap = _int_setting(conn, "qa.recalibrate_gap", 10)
        diff = round(abs(score - rv["total"]), 1)
        if diff > gap:
            text = (f"你给成片 {score} 分，AI 评审 {rv['total']:g} 分，相差 {diff:g} 分（超过 {gap}），"
                    f"需要重新校准评分标准。")
            items = alerts(conn) + [{"episode_id": eid, "branch": ep["branch"], "review_id": rv["id"],
                                     "user": score, "ai": rv["total"], "diff": diff, "at": db.now()}]
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                         (ALERTS_KEY, json.dumps(items, ensure_ascii=False)))
            notify("F1 Harness · 需要重新校准 AI 评审", f"{ep['title']}：{text}", eid)
            msgs.append(text)
    if decision == "approve":
        if rv is None or rv["total"] is None:
            msgs.append(f"本期最后一次合成没有 AI 评分，{ep['branch']}基线不变。")
        else:
            conn.execute("UPDATE reviews SET approved_at=? WHERE id=?", (db.now(), rv["id"]))
            msgs.append(update_baseline(conn, ep["branch"], episode_id=eid, review_id=rv["id"]))
    return msgs
