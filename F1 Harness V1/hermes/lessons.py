"""M6-8 经验笔记：G3 通过后总控按本期评审、返工、打回意见总结几条笔记，用户批准后按分支和步骤进各步提示词。"""
from __future__ import annotations

import json
import sqlite3

from . import db, prompts
from .research import ask, lesson_notes

BRANCHES = ("资讯", "前瞻", "复盘", "科普")
STEPS = {
    "D1资料与事实": "查资料、建事实表",
    "D5梗与出处": "找梗并核实出处",
    "总控汇总选题": "定选题和角度",
    "写逐字稿": "写口播稿、标题和简介",
    "D2找素材": "找视频素材",
    "D6剪辑决策": "选镜头、排时间线",
    "QA评审": "AI 看成片打分",
}
ALL = "全部"
MAX_ITEMS = 5

SUMMARY_INSTRUCTION = """# 本步：经验总结（G3 通过后）

你是总控。这一期刚被用户在 G3 终审通过。根据下面的材料（AI 评审的分数和问题清单、自动返工记录、用户在各关卡的打回意见和打分），总结 0–5 条经验笔记，让以后各期不再犯同样的错。

要求：
1. 每条只说一件事，写成下一期能直接照做的规则（做什么、不做什么），不超过 60 字。不写本期的车手名、时间点等具体内容，要能用到别的期。
2. 只总结材料里有证据的问题。用户的打回意见和打分比 AI 评审更重要；反复出现、返工后才改好的问题优先。
3. 适用步骤（step）选最该看到这条笔记的那一步，只有对所有步骤都成立才填“全部”。可选的步骤：
{steps}
4. 适用分支（branch）：只对本期分支（{branch}）成立就填“{branch}”，对所有分支都成立填“全部”。
5. 依据（basis）写清来源，例如“AI 评审 剪辑与节奏 62 分：03:10 同一镜头重复”或“G2 打回意见：赛道段换成车载”。
6. 下面“经验笔记（已经用户批准）”里已有的意思不要重复。没有值得总结的就返回空列表，宁缺毋滥。
"""


def schema(branch: str) -> dict:
    item = {"type": "object", "additionalProperties": False, "required": ["branch", "step", "content", "basis"],
            "properties": {"branch": {"type": "string", "enum": [ALL, branch]},
                           "step": {"type": "string", "enum": [ALL, *STEPS]},
                           "content": {"type": "string"}, "basis": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["lessons"],
            "properties": {"lessons": {"type": "array", "maxItems": MAX_ITEMS, "items": item}}}


def _g3_time(conn, episode_id: int) -> str | None:
    row = conn.execute("SELECT MAX(decided_at) FROM gates WHERE episode_id=? AND gate='G3' AND state='approved'",
                       (episode_id,)).fetchone()
    return row[0]


def g3_passed(conn, episode_id: int) -> bool:
    return _g3_time(conn, episode_id) is not None


def g3_episodes(conn) -> list[dict]:
    """G3 已通过的期（补做经验总结的下拉列表），新的在前。"""
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.code, e.title, e.branch, MAX(g.decided_at) AS g3_at, "
        "(SELECT COUNT(*) FROM lessons l WHERE l.episode_id=e.id) AS lessons "
        "FROM episodes e JOIN gates g ON g.episode_id=e.id AND g.gate='G3' AND g.state='approved' "
        "GROUP BY e.id ORDER BY g3_at DESC")]


def done(conn, episode_id: int) -> bool:
    """本次 G3 通过后已经总结过（任务重跑时不重复写）。"""
    ts = _g3_time(conn, episode_id)
    return bool(ts) and conn.execute("SELECT 1 FROM lessons WHERE episode_id=? AND created_at>=?",
                                     (episode_id, ts)).fetchone() is not None


def material(conn, ep) -> str:
    eid = ep["id"]
    parts = [f"## 本期\n- 标题：{ep['title']}\n- 分支：{ep['branch']}\n- 期号：{ep['code'] or ''}"]
    lines = []
    for r in conn.execute("SELECT * FROM reviews WHERE episode_id=? AND kind='成片' ORDER BY id", (eid,)):
        scores = json.loads(r["scores"] or "{}")
        lines.append(f"### 第 {len(lines) + 1} 次评审：{r['total']:g} 分（{r['created_at'][:16]}）" if r["total"] is not None
                     else f"### 第 {len(lines) + 1} 次评审：没有总分")
        lines += [f"- {k} {v.get('score')} 分：{v.get('reason') or ''}" for k, v in scores.items()]
        lines += [f"- 问题 {i.get('at') or '全片'} [{i.get('severity')}] {i.get('item')}：{i.get('problem')} → "
                  f"{i.get('fix')}（打回到{i.get('back_to')}）" for i in json.loads(r["issues"] or "[]")]
    parts.append("## AI 评审\n" + ("\n".join(lines) or "（本期没有 AI 评审）"))
    rws = [f"- 第 {r['cycle'] + 1} 周期第 {r['round']} 轮：{r['total']:g} 分低于基线 {r['baseline']:g}，"
           f"退回「{r['back_to']}」（{'、'.join(json.loads(r['steps'] or '[]'))}）"
           for r in conn.execute("SELECT * FROM reworks WHERE episode_id=? ORDER BY id", (eid,))]
    parts.append("## 自动返工\n" + ("\n".join(rws) or "（没有自动返工）"))
    gs = []
    for g in conn.execute("SELECT * FROM gates WHERE episode_id=? AND auto=0 AND state!='pending' ORDER BY id", (eid,)):
        what = "通过" if g["state"] == "approved" else "打回"
        score = f"，打分 {g['user_score']}" if g["user_score"] is not None else ""
        gs.append(f"- {g['gate']} {what}{score}" + (f"：{g['feedback']}" if g["feedback"] else ""))
    parts.append("## 用户关卡决定\n" + ("\n".join(gs) or "（没有记录）"))
    return "\n\n".join(parts)


def _clean(items, branch: str) -> list[dict]:
    out, seen = [], set()
    for it in (items or [])[:MAX_ITEMS]:
        it = it if isinstance(it, dict) else {}
        content = str(it.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        b, s = it.get("branch"), it.get("step")
        out.append({"content": content, "basis": str(it.get("basis") or "").strip(),
                    "branch": b if b == branch else None, "step": s if s in STEPS else None})
    return out


async def summarize(conn, ep, *, model: str, effort: str, task_id=None, runner=None) -> list[int]:
    branch = ep["branch"]
    steps = "\n".join(f"   - {k}：{v}" for k, v in STEPS.items())
    prompt = prompts.assemble(branch, SUMMARY_INSTRUCTION.format(steps=steps, branch=branch), material(conn, ep),
                              lesson_notes(conn, branch))
    data = await ask(conn, ep, prompt, sub="07_QA", name="经验总结", schema=schema(branch), model=model,
                     effort=effort, task_id=task_id, runner=runner)
    have = {r[0] for r in conn.execute("SELECT content FROM lessons WHERE state!='rejected'")}
    ids = []
    for it in _clean(data.get("lessons"), branch):
        if it["content"] in have:
            continue
        cur = conn.execute("INSERT INTO lessons(episode_id, branch, step, content, basis, created_at) "
                           "VALUES (?,?,?,?,?,?)", (ep["id"], it["branch"], it["step"], it["content"], it["basis"],
                                                    db.now()))
        ids.append(cur.lastrowid)
    return ids


# ---------- 经验笔记页 ----------

def listing(conn, state: str | None = None) -> dict:
    sql = ("SELECT l.*, e.code, e.title FROM lessons l LEFT JOIN episodes e ON e.id=l.episode_id"
           + (" WHERE l.state=?" if state else "") + " ORDER BY l.id DESC")
    items = [dict(r) for r in conn.execute(sql, (state,) if state else ())]
    pending = conn.execute("SELECT COUNT(*) FROM lessons WHERE state='pending'").fetchone()[0]
    return {"items": items, "pending": pending, "steps": list(STEPS), "branches": list(BRANCHES)}


def decide(conn: sqlite3.Connection, lesson_id: int, action: str, *, content: str | None = None,
           branch: str | None = None, step: str | None = None) -> dict:
    """approve（可带改写后的内容、分支、步骤）/ reject（驳回或停用已批准的）。"""
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if row is None:
        raise LookupError("没有这条笔记")
    if action == "reject":
        conn.execute("UPDATE lessons SET state='rejected', decided_at=? WHERE id=?", (db.now(), lesson_id))
    elif action == "approve":
        content = row["content"] if content is None else content.strip()
        if not content:
            raise ValueError("笔记内容不能为空")
        branch = row["branch"] if branch is None else (branch if branch in BRANCHES else None)
        step = row["step"] if step is None else (step if step in STEPS else None)
        original = row["original"] or (row["content"] if content != row["content"] else None)
        conn.execute("UPDATE lessons SET state='approved', content=?, branch=?, step=?, original=?, decided_at=? "
                     "WHERE id=?", (content, branch, step, original, db.now(), lesson_id))
    else:
        raise ValueError(f"未知操作：{action}")
    return dict(conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone())
