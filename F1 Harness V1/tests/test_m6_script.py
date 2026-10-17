"""M6-2 写逐字稿：数字核对改一轮、标题和简介、科普不提期数、未审译名、G1/G3 打回意见。"""
import asyncio
import json
from pathlib import Path

import pytest

from hermes import codex_runner, db, episodes, facts, glossary, prompts, script, steps

ORIG_CONNECT = db.connect


def _new(conn, branch, gp):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch=branch, gp_label=gp, title="测试")
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}")
    row = _new(conn, "前瞻", "马来西亚大奖赛")
    facts.add(conn, row["id"], "F", "一练开始时间（北京时间 10月23日 周五）", source="FastF1 赛历", value="11:30")
    facts.add(conn, row["id"], "S", "据 The Race 报道某车队带来升级", source="The Race",
              url="https://www.the-race.com/a", excerpt="upgrade")
    (Path(row["folder"]) / "01_资料" / "选题.md").write_text("# 选题\n雪邦回归", encoding="utf-8")
    return row


def _runner(replies: list, seen: list):
    async def run(prompt, model, effort, workdir, **kw):
        seen.append({"prompt": prompt, "kw": kw})
        data = replies[min(len(seen), len(replies)) - 1]
        return codex_runner.CodexResult("t", json.dumps(data, ensure_ascii=False), {})
    return run


GOOD = {"script": "## 第1段 开场\n雪邦回来了。\n一练在北京时间周五11:30开始[F1]。\n据报道有车队带来升级[S1]。\n",
        "title": "雪邦前瞻 传闻升级能不能翻身", "description": "简介第一行\n简介第二行"}
BAD = {"script": "一练在11点开始[F1]。去年有20万人到场。据报道某车队要换人[S9]。", "title": "t", "description": "d"}


def test_fix_round_then_write(conn, ep):
    seen = []
    out = asyncio.run(script.write(conn, ep, model="m", effort="high", runner=_runner([BAD, GOOD], seen)))
    assert out["attempts"] == 2 and out["issues"] == []
    assert "上一版的问题" in seen[1]["prompt"] and "[S9]" in seen[1]["prompt"] and "没标 [F#]" in seen[1]["prompt"]
    assert "11:30" in seen[1]["prompt"]
    d = Path(ep["folder"]) / "02_文案"
    assert (d / "逐字稿.md").read_text(encoding="utf-8").startswith("## 第1段 开场")
    lines = (d / "标题和简介.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["雪邦前瞻 传闻升级能不能翻身", "简介第一行", "简介第二行"]
    assert steps.title_and_desc(ep) == ("雪邦前瞻 传闻升级能不能翻身", "简介第一行\n简介第二行")
    assert "雪邦回归" in seen[0]["prompt"] and "[F1]" in seen[0]["prompt"]
    assert seen[0]["kw"]["web_search"] is False


def test_issues_left_after_one_fix_go_to_output(conn, ep):
    seen = []
    out = asyncio.run(script.write(conn, ep, model="m", effort="high", runner=_runner([BAD], seen)))
    assert len(seen) == 1 + script.MAX_FIX_ROUNDS
    kinds = " ".join(out["issues"])
    assert "[S9]" in kinds and "没标 [F#]" in kinds and "值是 11:30" in kinds
    assert (Path(ep["folder"]) / "02_文案" / "逐字稿.md").exists()


def test_science_no_episode_number(conn, ep):
    sci = _new(conn, "科普", "发车区")
    bad = {"script": "这是第三期科普。", "title": "DRS 科普 第 3 期", "description": "欢迎收看"}
    out = asyncio.run(script.write(conn, sci, model="m", effort="high", runner=_runner([bad], [])))
    assert any("口播" in i and "第三期" in i for i in out["issues"])
    assert any("标题" in i and "第 3 期" in i for i in out["issues"])
    assert not any("简介" in i for i in out["issues"])
    assert script.problems(conn, ep, "这是第三期节目。", "t", "d") != []
    assert not any("期" in i for i in script.problems(conn, ep, "雪邦回来了。", "t", "d"))


def test_unapproved_names(conn, ep):
    glossary.upsert_originals(conn, "车手", ["Zed Testdriver"])
    reply = dict(GOOD, script=GOOD["script"] + "Zed Testdriver 这周状态不错。\n")
    out = asyncio.run(script.write(conn, ep, model="m", effort="high", runner=_runner([reply], [])))
    assert [n["original"] for n in out["unapproved_names"]] == ["Zed Testdriver"]


def test_step_collects_g1_and_g3_feedback(conn, ep, monkeypatch):
    for gate, fb in (("G1", "开头太长"), ("G2", "镜头碎"), ("G3", "结尾再改改")):
        conn.execute("INSERT INTO gates(episode_id, gate, state, feedback, created_at) VALUES (?,?,'rejected',?, '2099')",
                     (ep["id"], gate, fb))
    seen = []
    monkeypatch.setattr(codex_runner, "run", _runner([GOOD], seen))
    task = {"id": None, "model": "gpt-6-sol", "effort": "high"}
    out = asyncio.run(steps.script_write(task, ep))
    p = seen[0]["prompt"]
    assert "G1/G3 打回的意见" in p and "- 开头太长" in p and "- 结尾再改改" in p and "镜头碎" not in p
    assert out["file"] == "02_文案/逐字稿.md" and out["attempts"] == 1
