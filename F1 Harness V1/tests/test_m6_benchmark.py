"""M6-8 Benchmark 页与经验笔记：基线历史、Benchmark 数据、总控总结笔记（mock）、批准/改写/停用、
笔记按分支和步骤进提示词、接口。codex 全部 mock。"""
import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes import (benchmark, codex_runner, db, edit, episodes, lessons, prompts, qa_review, ratchet, research,
                    server, steps)
from hermes.assets import FFMPEG

ORIG_CONNECT = db.connect
H = {"Host": "127.0.0.1:8790", "X-Hermes": "1"}


@pytest.fixture(autouse=True)
def _patched(db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(ratchet, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(steps, "notify", lambda *a, **kw: None)


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _new_ep(conn, branch="前瞻", title="雪邦前瞻"):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch=branch, gp_label="马来西亚大奖赛", title=title)
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def _to(conn, eid, state):
    with db.transaction(conn):
        episodes.set_state(conn, eid, state)


def _review(conn, ep, total, at, kind="成片"):
    run = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (ep["id"],)).fetchone()[0]
    scores = {"事实准确": {"score": total, "weight": 26, "reason": "数字有出处"}}
    issues = [{"at": "00:01", "item": "开头吸引力", "severity": "高", "problem": "空镜", "fix": "换画面",
               "back_to": "D6剪辑决策"}]
    return conn.execute("INSERT INTO reviews(episode_id, stage_run, kind, branch, video_path, total, scores, issues, "
                        "model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ep["id"], run, kind, ep["branch"], "x.mp4", total, json.dumps(scores, ensure_ascii=False),
                         json.dumps(issues, ensure_ascii=False), "m", at)).lastrowid


def _qa_task(conn, ep):
    run = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (ep["id"],)).fetchone()[0]
    conn.execute("INSERT INTO tasks(episode_id, stage, step, stage_run, phase, role, model, effort, state, created_at) "
                 "VALUES (?, '合成与QA', 'QA评审', ?, 2, 'QA评审', 'm', 'high', 'succeeded', ?)",
                 (ep["id"], run, db.now()))


def _lesson(conn, content, *, branch=None, step=None, state="approved", eid=None):
    return conn.execute("INSERT INTO lessons(episode_id, branch, step, content, state, created_at) VALUES (?,?,?,?,?,?)",
                        (eid, branch, step, content, state, db.now())).lastrowid


# ---------- 迁移与基线历史 ----------

def test_migration_history_start(conn):
    rows = conn.execute("SELECT branch, old_value, value, reason FROM baseline_history ORDER BY branch").fetchall()
    assert sorted(tuple(r) for r in rows) == [("前瞻", None, 60.0, "起点"), ("复盘", None, 60.0, "起点"),
                                             ("资讯", None, 60.0, "起点")]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lessons)")}
    assert {"step", "basis", "original"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_update_baseline_writes_history(conn):
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('qa.baseline_min', '1')")
    e = _new_ep(conn)
    _to(conn, e["id"], "合成与QA")
    _qa_task(conn, e)
    rid = _review(conn, e, 64, db.now())
    conn.execute("UPDATE reviews SET approved_at=? WHERE id=?", (db.now(), rid))
    msg = ratchet.update_baseline(conn, "前瞻", episode_id=e["id"], review_id=rid)
    assert msg == "前瞻基线 60 → 64（最近 1 期中位数）。"
    h = conn.execute("SELECT * FROM baseline_history WHERE branch='前瞻' ORDER BY id DESC").fetchone()
    assert (h["old_value"], h["value"], h["episode_id"], h["review_id"], h["reason"]) == (60, 64, e["id"], rid, msg)
    ratchet.update_baseline(conn, "前瞻")   # 不变不记
    assert conn.execute("SELECT COUNT(*) FROM baseline_history WHERE branch='前瞻'").fetchone()[0] == 2


# ---------- Benchmark 数据 ----------

def test_benchmark_data(conn):
    e = _new_ep(conn)
    _to(conn, e["id"], "合成与QA")
    _qa_task(conn, e)
    r1 = _review(conn, e, 50, _ago(3))
    conn.execute("INSERT INTO reworks(episode_id, cycle, round, review_id, total, baseline, back_to, steps, state, "
                 "created_at) VALUES (?,0,1,?,50,60,'配音','[]','done',?)", (e["id"], r1, _ago(2.5)))
    r2 = _review(conn, e, 65, _ago(2))
    _to(conn, e["id"], "G3待审")
    episodes.approve(conn, e["id"], "G3", 70)
    steps.after_gate(conn, e, "G3", "approve", 70)
    conn.execute("INSERT INTO reviews(kind, branch, video_path, total, scores, issues, created_at) "
                 "VALUES ('校准', '前瞻', '/x/旧片.mp4', 72, '{}', '[]', ?)", (db.now(),))

    d = benchmark.data(conn)
    assert d["branches"] == list(lessons.BRANCHES) and sum(x["weight"] for x in d["rubric"]) == 100
    new, old = d["reviews"]
    assert (new["id"], new["total"], new["baseline"], new["below_baseline"], new["rework_round"]) == (r2, 65, 60, False, 1)
    assert new["user_score"] == 70 and new["user_decision"] == "approved" and new["approved"] is True
    assert (old["id"], old["baseline"], old["below_baseline"], old["rework_round"], old["user_score"]) == \
        (r1, 60, True, 0, None)
    assert old["scores"]["事实准确"]["score"] == 50 and old["issues"][0]["back_to"] == "D6剪辑决策"
    assert d["trend"]["前瞻"] == [{"review_id": r2, "episode_id": e["id"], "code": e["code"], "title": e["title"],
                                  "total": 65, "created_at": new["created_at"], "approved": True}]
    b = next(x for x in d["baselines"] if x["branch"] == "前瞻")
    assert b["value"] == 60 and [h["value"] for h in b["history"]] == [60]
    assert (b["passed"], b["min"], b["window"]) == (1, 3, 5)
    assert d["calibrations"][0]["video"] == "旧片.mp4" and d["calibrations"][0]["kind"] == "校准"

    z = benchmark.data(conn, "资讯")
    assert z["reviews"] == [] and [x["branch"] for x in z["baselines"]] == ["资讯"] and list(z["trend"]) == ["资讯"]
    assert z["calibrations"] == []


# ---------- 笔记进提示词 ----------

def test_lesson_notes_filter(conn):
    _lesson(conn, "通用全步骤")
    _lesson(conn, "前瞻剪辑", branch="前瞻", step="D6剪辑决策")
    _lesson(conn, "前瞻逐字稿", branch="前瞻", step="写逐字稿")
    _lesson(conn, "复盘剪辑", branch="复盘", step="D6剪辑决策")
    _lesson(conn, "待批准", step="D6剪辑决策", state="pending")
    _lesson(conn, "已停用", state="rejected")
    assert research.lesson_notes(conn, "前瞻", "D6剪辑决策") == "- 通用全步骤\n- 前瞻剪辑"
    assert research.lesson_notes(conn, "前瞻") == "- 通用全步骤\n- 前瞻剪辑\n- 前瞻逐字稿"
    assert research.lesson_notes(conn, "复盘", "写逐字稿") == "- 通用全步骤"


def test_edit_prompt_gets_step_notes(conn, tmp_path, monkeypatch):
    _lesson(conn, "开头 5 秒要有赛车", branch="前瞻", step="D6剪辑决策")
    _lesson(conn, "标题别超 20 字", branch="前瞻", step="写逐字稿")
    eid = conn.execute("INSERT INTO episodes(code, branch, title, state, created_at, updated_at) "
                       "VALUES ('E1', '前瞻', '测试', '配素材与粗剪', ?, ?)", (db.now(), db.now())).lastrowid
    aid = conn.execute("INSERT INTO assets(source_type, title, credit, width, height, created_at) VALUES "
                       "('测试', 't', 'F1', 1920, 1080, ?)", (db.now(),)).lastrowid
    conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, tags) VALUES (?,0,0,9,'{}')", (aid,))
    d = tmp_path / "ep"
    (d / "03_配音").mkdir(parents=True)
    align = {"duration": 4.0, "segments": [{"i": 0, "text": "起步", "show": "起步", "start": 0, "end": 4}]}
    (d / "03_配音" / "对齐.json").write_text(json.dumps(align, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **k: [])
    monkeypatch.setattr(edit, "_checks", lambda conn, eid, tl: [])
    monkeypatch.setattr(edit.prompts, "assemble", lambda branch, step, ctx="", notes="": f"NOTES[{notes}]")
    seen = []

    async def runner(prompt, model, effort, workdir, **kw):
        seen.append(prompt)
        cuts = {"cuts": [{"segment": 0, "asset_id": aid, "shot_idx": 0, "src_in": 0, "src_out": 4, "reason": "r"}]}
        return codex_runner.CodexResult("t", json.dumps(cuts), {"input_tokens": 1, "output_tokens": 1})
    asyncio.run(edit.decide(conn, eid, d, asset_ids=[aid], branch="前瞻", runner=runner))
    assert "开头 5 秒要有赛车" in seen[0] and "标题别超 20 字" not in seen[0]


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("bm") / "片.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-y", str(p)], check=True)
    return p


def test_qa_prompt_notes_only_for_episode(conn, video, tmp_path, monkeypatch):
    _lesson(conn, "评审时重点看开头", branch="前瞻", step="QA评审")
    _lesson(conn, "剪辑笔记", branch="前瞻", step="D6剪辑决策")
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"NOTES[{notes}]")
    answer = {"scores": [{"item": k, "score": 70, "reason": "r"} for k in qa_review.WEIGHTS], "total": 70,
              "issues": [], "summary": "好"}
    seen = []

    async def runner(prompt, model, effort, workdir, **kw):
        seen.append(prompt)
        return codex_runner.CodexResult("t", json.dumps(answer, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1})
    for kind in ("成片", "校准"):
        asyncio.run(qa_review.review(conn, video, episode_id=None, branch="前瞻", kind=kind, out_dir=tmp_path / kind,
                                     runner=runner))
    assert "评审时重点看开头" in seen[0] and "剪辑笔记" not in seen[0]
    assert "NOTES[]" in seen[1]


# ---------- 总结笔记 ----------

def _lesson_runner(answer, seen):
    async def run(prompt, model, effort, workdir, **kw):
        seen.append({"prompt": prompt, "kw": kw})
        return codex_runner.CodexResult("t", json.dumps(answer, ensure_ascii=False), {"input_tokens": 1, "output_tokens": 1})
    return run


def _g3_passed(conn):
    e = _new_ep(conn)
    _to(conn, e["id"], "合成与QA")
    _qa_task(conn, e)
    _review(conn, e, 66, _ago(1))
    _to(conn, e["id"], "G3待审")
    episodes.approve(conn, e["id"], "G3", 80)
    conn.execute("UPDATE gates SET feedback='开头太慢' WHERE episode_id=? AND gate='G3'", (e["id"],))
    return conn.execute("SELECT * FROM episodes WHERE id=?", (e["id"],)).fetchone()


def test_summarize(conn, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{step}\n{ctx}\nNOTES[{notes}]")
    _lesson(conn, "已有的笔记", branch="前瞻")
    e = _g3_passed(conn)
    answer = {"lessons": [
        {"branch": "全部", "step": "D6剪辑决策", "content": "开头 5 秒内必须出现赛车", "basis": "G3 意见：开头太慢"},
        {"branch": "前瞻", "step": "全部", "content": "已有的笔记", "basis": "重复"},
        {"branch": "前瞻", "step": "写逐字稿", "content": "开头 5 秒内必须出现赛车", "basis": "同一条"},
        {"branch": "前瞻", "step": "写逐字稿", "content": "  ", "basis": "空"}]}
    seen = []
    ids = asyncio.run(lessons.summarize(conn, e, model="m", effort="high", runner=_lesson_runner(answer, seen)))
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (ids[0],)).fetchone()
    assert (row["branch"], row["step"], row["state"], row["episode_id"], row["basis"]) == \
        (None, "D6剪辑决策", "pending", e["id"], "G3 意见：开头太慢")
    p = seen[0]["prompt"]
    assert "经验总结" in p and "66 分" in p and "G3 通过，打分 80：开头太慢" in p and "NOTES[- 已有的笔记]" in p
    assert "D6剪辑决策：选镜头、排时间线" in p and "填“前瞻”" in p
    schema = json.loads(Path(seen[0]["kw"]["output_schema"]).read_text(encoding="utf-8"))
    item = schema["properties"]["lessons"]["items"]
    assert item["properties"]["branch"]["enum"] == ["全部", "前瞻"] and schema["properties"]["lessons"]["maxItems"] == 5
    assert lessons.done(conn, e["id"])


def test_lesson_summary_step_idempotent_and_safe(conn, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda *a, **k: "p")
    e = _g3_passed(conn)
    seen = []
    answer = {"lessons": [{"branch": "前瞻", "step": "QA评审", "content": "评审要看开头", "basis": "b"}]}
    monkeypatch.setattr(codex_runner, "run", _lesson_runner(answer, seen))
    task = {"id": None, "step": "经验总结", "model": None, "effort": None}
    assert len(asyncio.run(steps.lesson_summary(task, e))["lessons"]) == 1
    assert "skipped" in asyncio.run(steps.lesson_summary(task, e)) and len(seen) == 1

    e2 = _g3_passed(conn)

    async def boom(*a, **k):
        raise RuntimeError("模型挂了")
    monkeypatch.setattr(codex_runner, "run", boom)
    out = asyncio.run(steps.lesson_summary(task, e2))
    assert "模型挂了" in out["error"]
    msgs = [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=?", (e2["id"],))]
    assert any("经验总结没做成" in m and "不影响发布" in m for m in msgs)

    async def quota(*a, **k):
        raise codex_runner.QuotaExhausted("没额度")
    monkeypatch.setattr(codex_runner, "run", quota)
    with pytest.raises(codex_runner.QuotaExhausted):
        asyncio.run(steps.lesson_summary(task, e2))


# ---------- 手动补做经验总结 ----------

def test_lesson_rerun_ignores_done_and_dedups(conn, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda *a, **k: "p")
    e = _g3_passed(conn)
    _to(conn, e["id"], "暂停")
    seen = []
    answer = {"lessons": [{"branch": "前瞻", "step": "QA评审", "content": "评审要看开头", "basis": "b"}]}
    monkeypatch.setattr(codex_runner, "run", _lesson_runner(answer, seen))
    task = {"id": None, "step": "经验总结", "model": None, "effort": None}
    assert len(asyncio.run(steps.lesson_summary(task, e))["lessons"]) == 1 and lessons.done(conn, e["id"])
    assert asyncio.run(steps.lesson_rerun(e["id"])) == {"lessons": []} and len(seen) == 2
    answer["lessons"].append({"branch": "全部", "step": "全部", "content": "新的一条", "basis": "b"})
    ids = asyncio.run(steps.lesson_rerun(e["id"]))["lessons"]
    rows = conn.execute("SELECT content, state FROM lessons WHERE episode_id=? ORDER BY id", (e["id"],)).fetchall()
    assert len(ids) == 1 and [tuple(r) for r in rows] == [("评审要看开头", "pending"), ("新的一条", "pending")]
    assert conn.execute("SELECT state FROM episodes WHERE id=?", (e["id"],)).fetchone()[0] == "暂停"

    async def quota(*a, **k):
        raise codex_runner.QuotaExhausted("没额度")
    monkeypatch.setattr(codex_runner, "run", quota)
    assert "error" in asyncio.run(steps.lesson_rerun(e["id"]))
    msgs = [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=?", (e["id"],))]
    assert any("补做经验总结没做成" in m and "额度" in m for m in msgs)


# ---------- 批准 / 改写 / 停用 ----------

def test_decide(conn):
    lid = _lesson(conn, "AI 原话", branch="前瞻", step="写逐字稿", state="pending")
    r = lessons.decide(conn, lid, "approve", content=" 改过的话 ", branch="", step="D6剪辑决策")
    assert (r["state"], r["content"], r["branch"], r["step"], r["original"]) == \
        ("approved", "改过的话", None, "D6剪辑决策", "AI 原话")
    r = lessons.decide(conn, lid, "approve", content="再改", branch="科普", step="乱写")
    assert (r["original"], r["branch"], r["step"]) == ("AI 原话", "科普", None)
    assert lessons.decide(conn, lid, "reject")["state"] == "rejected"
    lid2 = _lesson(conn, "原样", state="pending")
    assert lessons.decide(conn, lid2, "approve")["original"] is None
    with pytest.raises(ValueError):
        lessons.decide(conn, lid2, "approve", content="  ")
    with pytest.raises(LookupError):
        lessons.decide(conn, 999, "approve")
    lst = lessons.listing(conn, "approved")
    assert [i["id"] for i in lst["items"]] == [lid2] and lst["pending"] == 0 and "QA评审" in lst["steps"]


# ---------- 接口 ----------

class _FakeAppServer:
    async def read_rate_limits(self):
        return None

    async def close(self):
        pass


def test_api(conn, monkeypatch):
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    monkeypatch.setattr(steps, "EXECUTORS", {})
    lid = _lesson(conn, "原话", state="pending")
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.get("/api/benchmark", headers=H).json()["branches"] == list(lessons.BRANCHES)
        assert [b["branch"] for b in c.get("/api/benchmark?branch=复盘", headers=H).json()["baselines"]] == ["复盘"]
        assert c.get("/api/lessons?state=pending", headers=H).json()["pending"] == 1
        assert c.post(f"/api/lessons/{lid}/approve", json={}, headers={"Host": "127.0.0.1:8790"}).status_code == 403
        assert c.post(f"/api/lessons/{lid}/approve", json={"content": " "}, headers=H).status_code == 400
        assert c.post("/api/lessons/999/approve", json={}, headers=H).status_code == 404
        assert c.post(f"/api/lessons/{lid}/delete", json={}, headers=H).status_code == 404
        r = c.post(f"/api/lessons/{lid}/approve", json={"content": "新话", "branch": "前瞻", "step": "QA评审"}, headers=H)
        assert r.status_code == 200 and r.json()["pending"] == 0
        assert (r.json()["item"]["content"], r.json()["item"]["original"], r.json()["item"]["step"]) == \
            ("新话", "原话", "QA评审")
        assert c.post(f"/api/lessons/{lid}/reject", headers=H).json()["item"]["state"] == "rejected"


def test_api_lesson_rerun(conn, monkeypatch):
    monkeypatch.setattr(server, "AppServer", _FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    monkeypatch.setattr(steps, "EXECUTORS", {})
    calls = []

    async def fake(eid):
        calls.append(eid)
        return {"lessons": []}
    monkeypatch.setattr(steps, "lesson_rerun", fake)
    other = _new_ep(conn, title="没到 G3")
    e = _g3_passed(conn)
    _to(conn, e["id"], "暂停")
    count = "SELECT COUNT(*) FROM tasks WHERE episode_id=?"
    before = conn.execute(count, (e["id"],)).fetchone()[0]
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        url = "/api/episodes/{}/lessons/summarize"
        r = c.post(url.format(other["id"]), headers=H)
        assert r.status_code == 400 and "G3" in r.json()["detail"]
        assert c.post(url.format(999), headers=H).status_code == 404
        assert c.post(url.format(e["id"]), headers={"Host": "127.0.0.1:8790"}).status_code == 403
        r = c.post(url.format(e["id"]), headers=H)
        assert r.status_code == 202 and "待批准" in r.json()["detail"]
        items = c.get("/api/lessons/episodes", headers=H).json()["items"]
        assert [i["id"] for i in items] == [e["id"]] and items[0]["lessons"] == 0
    assert calls == [e["id"]]
    assert conn.execute("SELECT state FROM episodes WHERE id=?", (e["id"],)).fetchone()[0] == "暂停"
    assert conn.execute(count, (e["id"],)).fetchone()[0] == before
