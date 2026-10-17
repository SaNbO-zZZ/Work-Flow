"""M6-5 独立 AI 评审：分数校验与重算、抽帧拼图、写 reviews 表、基线比较、硬检查没过不调 AI、每次新线程。"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from hermes import codex_runner, db, episodes, prompts, qa_review, steps
from hermes.assets import FFMPEG

ORIG_CONNECT = db.connect

ANSWER = {
    "scores": [{"item": "事实准确", "score": 70, "reason": "数字都有出处"},
               {"item": "剪辑与节奏", "score": 60, "reason": "00:02 起有长静态"},
               {"item": "开头吸引力", "score": 50, "reason": "开头是空镜"},
               {"item": "画面贴合度", "score": 65, "reason": "点名车手有画面"},
               {"item": "文案可听性", "score": 80, "reason": "口语自然"},
               {"item": "规格合规", "score": 90, "reason": "硬检查全过"}],
    "total": 99,
    "issues": [{"at": "00:01", "item": "规格合规", "severity": "低", "problem": "出处小字偏小", "fix": "放大",
                "back_to": "D4图形与叠层"},
               {"at": "00:00", "item": "开头吸引力", "severity": "高", "problem": "前 5 秒是空镜", "fix": "换名场面",
                "back_to": "D6剪辑决策"}],
    "summary": "开头要换。",
}
EXPECTED = round((70 * 26 + 60 * 20 + 50 * 17 + 65 * 15 + 80 * 14 + 90 * 8) / 100, 1)


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("qa") / "片.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-y", str(p)], check=True)
    return p


@pytest.fixture
def ep(conn, db_path, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}|{notes}")
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    return conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def _runner(seen: list, answer=ANSWER):
    async def run(prompt, model, effort, workdir, **kw):
        seen.append({"prompt": prompt, "model": model, "effort": effort, "kw": kw})
        return codex_runner.CodexResult(f"thread-{len(seen)}", json.dumps(answer, ensure_ascii=False),
                                        {"input_tokens": 10, "output_tokens": 5})
    return run


def _fill(ep, video, *, passed=True):
    d = Path(ep["folder"])
    (d / "06_合成").mkdir(parents=True, exist_ok=True)
    (d / "06_合成" / "成片.mp4").write_bytes(video.read_bytes())
    (d / "02_文案" / "逐字稿.md").write_text("雪邦回来了，比赛有56圈[F1]。\n", encoding="utf-8")
    (d / "02_文案" / "标题和简介.txt").write_text("雪邦前瞻\n简介", encoding="utf-8")
    (d / "03_配音").mkdir(parents=True, exist_ok=True)
    (d / "03_配音" / "字幕.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\n雪邦回来了\n\n2\n00:00:01,500 --> 00:00:03,000\n比赛有五十六圈\n",
        encoding="utf-8")
    tl = {"video": [{"asset_id": 1, "shot_idx": 0, "start": 0, "end": 1, "reason": "开场"},
                    {"asset_id": 1, "shot_idx": 1, "start": 1, "end": 3, "reason": "赛道"}],
          "overlays": [], "credits": [{"text": "画面：YouTube · F1", "start": 0, "end": 3}]}
    (d / "06_合成" / "时间线.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    hard = {"passed": passed, "items": [{"name": "水印", "ok": passed, "detail": "", "hard": True},
                                        {"name": "时长", "ok": False, "detail": "偏短", "hard": False}]}
    (d / "07_QA").mkdir(parents=True, exist_ok=True)
    (d / "07_QA" / "硬检查.json").write_text(json.dumps(hard, ensure_ascii=False), encoding="utf-8")
    return d


# ---------- 校验与算分 ----------

def test_validate_recomputes_total_and_sorts_issues():
    r = qa_review.validate(ANSWER)
    assert r["total"] == EXPECTED and r["model_total"] == 99
    assert list(r["scores"]) == list(qa_review.WEIGHTS)
    assert r["scores"]["开头吸引力"] == {"score": 50, "weight": 17, "reason": "开头是空镜"}
    assert [i["severity"] for i in r["issues"]] == ["高", "低"]
    assert r["issues"][1]["at_s"] == 1.0
    assert sum(qa_review.WEIGHTS.values()) == 100


@pytest.mark.parametrize("bad, msg", [
    ({"scores": ANSWER["scores"][:5]}, "缺评分项"),
    ({"scores": ANSWER["scores"][:5] + [{"item": "规格合规", "score": 101, "reason": ""}]}, "0–100"),
    ({"scores": ANSWER["scores"][:5] + [{"item": "规格合规", "score": -1, "reason": ""}]}, "0–100"),
    ({"scores": ANSWER["scores"] + [{"item": "事实准确", "score": 1, "reason": ""}]}, "重复"),
    ({"scores": ANSWER["scores"] + [{"item": "配乐", "score": 1, "reason": ""}]}, "未知"),
    ({"scores": ANSWER["scores"], "issues": [dict(ANSWER["issues"][0], back_to="合成")]}, "问题清单"),
    ([], "JSON 对象"),
])
def test_validate_rejects(bad, msg):
    with pytest.raises(ValueError, match=msg):
        qa_review.validate(bad)


def test_baseline(conn):
    assert qa_review.below_baseline(conn, "前瞻", 59.9) == (60.0, True)
    assert qa_review.below_baseline(conn, "前瞻", 60) == (60.0, False)
    assert qa_review.below_baseline(conn, "科普", 10) == (None, None)


def test_frame_step_and_srt():
    assert qa_review.frame_step(180) == 2.0
    assert qa_review.frame_step(600) == 5.0
    cues = qa_review.parse_srt("1\n00:00:01,000 --> 00:00:02,500\n第一行\n第二行\n\n2\n00:01:00,000 --> 00:01:01,000\n末\n")
    assert cues == [(1.0, 2.5, "第一行第二行"), (60.0, 61.0, "末")]


def test_make_sheets(video, tmp_path):
    paths, step = qa_review.make_sheets(video, tmp_path / "看片", 3.0)
    assert [p.name for p in paths] == ["开头0-15秒.jpg", "全片_01.jpg"] and step == 2.0
    assert all(p.stat().st_size > 0 for p in paths)
    assert not (tmp_path / "看片" / "_帧").exists()


# ---------- 评审 ----------

def test_review_pipeline_writes_reviews_and_new_thread(ep, conn, video):
    d = _fill(ep, video)
    seen = []
    r = asyncio.run(qa_review.review(conn, d / "06_合成" / "成片.mp4", episode_id=ep["id"], branch="前瞻",
                                     kind="成片", ep_dir=d, runner=_runner(seen)))
    kw = seen[0]["kw"]
    assert "resume_thread" not in kw and kw["sandbox"] == "read-only"
    assert [p.name for p in kw["images"]] == ["开头0-15秒.jpg", "全片_01.jpg"]
    assert Path(kw["output_schema"]).is_file()
    assert seen[0]["model"] == "gpt-6-astra"
    prompt = seen[0]["prompt"]
    for s in ("雪邦回来了", "比赛有56圈[F1]", "时间线摘要", "镜头数 2", "画面：YouTube · F1", "硬检查", "提醒｜时长",
              qa_review.RUBRIC_VERSION, "规格合规（权重 8）"):
        assert s in prompt, s
    assert r["total"] == EXPECTED and r["baseline"] == 60.0 and r["below_baseline"] is False
    assert r["codex_thread_id"] == "thread-1" and r["materials"]["transcript"] == "字幕"
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (r["review_id"],)).fetchone()
    assert (row["episode_id"], row["kind"], row["branch"], row["rubric_version"], row["total"], row["codex_thread_id"]) \
        == (ep["id"], "成片", "前瞻", qa_review.RUBRIC_VERSION, EXPECTED, "thread-1")
    assert row["stage_run"] == ep["stage_run"]
    assert json.loads(row["scores"])["事实准确"]["score"] == 70
    saved = json.loads((d / "07_QA" / "AI评审.json").read_text(encoding="utf-8"))
    assert saved["review_id"] == r["review_id"] and saved["rubric_version"] == qa_review.RUBRIC_VERSION
    assert (d / "07_QA" / "看片" / "开头0-15秒.jpg").is_file()

    asyncio.run(qa_review.review(conn, d / "06_合成" / "成片.mp4", episode_id=ep["id"], branch="前瞻",
                                 kind="成片", ep_dir=d, runner=_runner(seen)))
    assert "resume_thread" not in seen[1]["kw"]
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2


def test_review_history_without_ep_dir(conn, video, tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}")
    seen, out = [], tmp_path / "评审"

    def no_asr(video, work):
        raise RuntimeError("模型不在")
    low = dict(ANSWER, scores=[dict(s, score=40) for s in ANSWER["scores"]])
    r = asyncio.run(qa_review.review(conn, video, branch="复盘", kind="校准", out_dir=out, runner=_runner(seen, low),
                                     transcriber=no_asr))
    assert r["total"] == 40 and r["below_baseline"] is True and r["episode_id"] is None
    assert r["materials"]["transcript"] == "无"
    assert any("语音识别失败" in n for n in r["materials"]["notes"])
    prompt = seen[0]["prompt"]
    assert "响度（ebur128 实测）" in prompt and "镜头统计" in prompt and "语音识别失败" in prompt
    assert (out / "AI评审.json").is_file() and (out / "看片" / "全片_01.jpg").is_file()
    row = conn.execute("SELECT kind, episode_id, stage_run FROM reviews").fetchone()
    assert tuple(row) == ("校准", None, None)


def test_review_history_with_asr(conn, video, tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": ctx)
    seen = []
    r = asyncio.run(qa_review.review(conn, video, branch="资讯", kind="校准", out_dir=tmp_path, runner=_runner(seen),
                                     transcriber=lambda v, w: [(0.0, 2.0, "围场简报来了")]))
    assert r["materials"]["transcript"] == "语音识别"
    assert "语音识别转写（带时间）" in seen[0]["prompt"] and "围场简报来了" in seen[0]["prompt"]


def test_review_rejects_bad_args(conn, video, tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": ctx)
    with pytest.raises(ValueError, match="kind"):
        asyncio.run(qa_review.review(conn, video, branch="前瞻", kind="随便", out_dir=tmp_path))
    with pytest.raises(ValueError, match="out_dir"):
        asyncio.run(qa_review.review(conn, video, branch="前瞻", kind="校准"))
    with pytest.raises(ValueError, match="缺评分项"):
        asyncio.run(qa_review.review(conn, video, branch="前瞻", kind="测试", out_dir=tmp_path,
                                     runner=_runner([], {"scores": [], "total": 0, "issues": [], "summary": ""}),
                                     transcriber=lambda v, w: []))
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


# ---------- 流水线 QA评审 步骤 ----------

def _task():
    return {"id": None, "step": "QA评审", "model": None, "effort": None}


def test_step_skips_ai_when_hard_check_failed(ep, conn, video, monkeypatch):
    d = _fill(ep, video, passed=False)

    async def boom(*a, **kw):
        raise AssertionError("硬检查没过不该调 AI")
    monkeypatch.setattr(qa_review, "review", boom)
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["ai_review"] is None and "水印" in out["ai_skipped"]
    saved = json.loads((d / "07_QA" / "质检汇总.json").read_text(encoding="utf-8"))
    assert saved["hard_passed"] is False and "不做 AI 评审" in saved["ai_skipped"]
    assert steps.review_data(conn, ep)["ai"] == {}


def test_step_runs_ai_and_summarizes(ep, conn, video, monkeypatch):
    d = _fill(ep, video)
    seen = []
    monkeypatch.setattr(codex_runner, "run", _runner(seen))
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert "resume_thread" not in seen[0]["kw"]
    assert seen[0]["model"] == "gpt-6-astra" and seen[0]["effort"] == "high"
    ai = out["ai_review"]
    assert ai["total"] == EXPECTED and ai["scores"]["开头吸引力"] == 50 and len(ai["issues"]) == 2
    assert ai["rubric_version"] == qa_review.RUBRIC_VERSION
    assert out["baseline"] == 60.0 and out["below_baseline"] is False
    saved = json.loads((d / "07_QA" / "质检汇总.json").read_text(encoding="utf-8"))
    assert saved["ai_review"]["review_id"] == ai["review_id"]
    data = steps.review_data(conn, ep)
    assert data["ai"]["total"] == EXPECTED and data["qa"]["below_baseline"] is False
    msg = conn.execute("SELECT content FROM messages WHERE episode_id=? ORDER BY id DESC", (ep["id"],)).fetchone()[0]
    assert msg.startswith(f"AI 评审 {EXPECTED:g} 分，不低于前瞻基线 60")


def test_step_below_baseline(ep, conn, video, monkeypatch):
    _fill(ep, video)
    conn.execute("UPDATE baselines SET value=70 WHERE branch='前瞻'")
    monkeypatch.setattr(codex_runner, "run", _runner([]))
    out = asyncio.run(steps.qa_review(_task(), ep))
    assert out["baseline"] == 70.0 and out["below_baseline"] is True


def test_cli_parses(monkeypatch, tmp_path, video):
    got = {}

    async def fake(conn, video, **kw):
        got.update(kw, video=video)
        return {"total": 61.0, "scores": {}, "issues": [], "baseline": 60.0, "below_baseline": False}
    monkeypatch.setattr(qa_review, "review", fake)
    qa_review.main([str(video), "--branch", "前瞻", "--out", str(tmp_path), "--srt", "a.srt"])
    assert got["kind"] == "校准" and got["branch"] == "前瞻" and got["srt"] == "a.srt" and got["out_dir"] == str(tmp_path)
