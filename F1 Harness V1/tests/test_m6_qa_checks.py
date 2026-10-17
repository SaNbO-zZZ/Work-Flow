"""M6-4 程序硬检查：数字 [F#]、复盘成绩处罚复核、标题传闻、未授权人声。FastF1 与网络全部 mock。"""
import json

import pytest

from hermes import db, facts, qa_checks as qa


def _ep(conn, branch="复盘", gp="巴林大奖赛") -> int:
    ts = db.now()
    return conn.execute("INSERT INTO episodes(branch, title, gp_label, state, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?)", (branch, "QA 测试", gp, "合成与QA", ts, ts)).lastrowid


def _dir(tmp_path, script=None, title=None):
    ep = tmp_path / "ep"
    for sub in ("02_文案", "03_配音", "06_合成"):
        (ep / sub).mkdir(parents=True, exist_ok=True)
    if script is not None:
        (ep / "02_文案" / "逐字稿.md").write_text(script, encoding="utf-8")
    if title is not None:
        (ep / "02_文案" / "标题和简介.txt").write_text(title + "\n简介", encoding="utf-8")
    return ep


def _vals(text):
    return [t[0] for t in qa.numbers(text)[0]]


# ---------- 数字归一化 ----------

@pytest.mark.parametrize("text, want", [
    ("一圈五点五四三公里", [(5.543,)]),
    ("一圈5.543公里", [(5.543,)]),
    ("排位跑出一分三十秒零七六", [(90.076,)]),
    ("排位跑出1:30.076", [(90.076,)]),
    ("排位跑出1分30秒076", [(90.076,)]),
    ("北京时间下午四点排位", [(240.0, 960.0)]),
    ("15:00 正赛", [(900.0,)]),
    ("晚上11点半开赛", [(690.0, 1410.0)]),
    ("周二到周六大约二十五到三十四度", [(25.0,), (34.0,)]),
    ("百分之二十的降雨概率", [(20.0,)]),
    ("领先1,234.5米", [(1234.5,)]),
    ("一百零五圈", [(105.0,)]),
    ("第三名完赛", [(3.0,)]),
    ("维斯塔潘P3完赛", [(3.0,)]),
    ("九月二十八日", [(9.0,), (28.0,)]),
    ("2026赛季第16站", [(16.0,)]),
    ("罚五秒", [(5.0,)]),
])
def test_numbers_normalize(text, want):
    assert _vals(text) == want
    assert qa.numbers(text)[1]


@pytest.mark.parametrize("text", [
    "二〇一七年他驾驶W08", "2017年的赛车", "倍耐力选了C2、C3和C4", "F1重返雪邦", "MGU-K 和 V6 引擎",
    "一位车手拿到一次机会", "十分精彩", "第3期节目", "第一时间告诉大家", "唯一的机会",
])
def test_numbers_excluded_need_no_tag(text):
    assert qa.numbers(text) == ([], False)


def test_numbers_year_with_era_still_counts():
    assert qa.numbers("九十年代的赛车")[1]


def test_found_rounds_to_fewer_decimals():
    pool = qa.numbers("5.543", exclude=False)[0]
    assert qa._found(qa.numbers("五点五公里")[0][0], pool)
    assert qa._found(qa.numbers("五点五四公里")[0][0], pool)
    assert not qa._found(qa.numbers("五公里")[0][0], pool)
    assert not qa._found(qa.numbers("五点六公里")[0][0], pool)


# ---------- 数字 [F#] ----------

def test_number_issues_pass(conn, tmp_path):
    eid = _ep(conn, "前瞻")
    facts.add(conn, eid, "F", "雪邦赛道一圈长度（公里）", source="F1 官网", value="5.543")
    facts.add(conn, eid, "F", "2017 年雪邦排位杆位圈速", source="F1 官网", value="1:30.076")
    facts.add(conn, eid, "F", "正赛开始时间（北京时间）", source="FastF1", value="2026-10-11 15:00")
    text = ("# 标题行 3 个字\n一圈五点五四三公里[F1]。二〇一七年，杆位一分三十秒零七六[F2]。\n"
            "北京时间周日下午三点正赛[F3]。<!-- 注释 99 -->\n这里没有数字。\n[AD] 广告 88 元\n")
    issues, n = qa.number_issues(conn, eid, text)
    assert issues == [] and n == 3
    r = qa._numbers_item(conn, eid, _dir(tmp_path, text))
    assert r["ok"] and r["hard"] and r["name"] == qa.NUM_NAME


def test_number_issues_untagged_s_only_unknown_and_mismatch(conn, tmp_path):
    eid = _ep(conn, "前瞻")
    facts.add(conn, eid, "F", "雪邦赛道一圈长度（公里）", source="F1 官网", value="5.543")
    facts.add(conn, eid, "S", "传闻某车队要换 3 名工程师", source="某媒体", url="https://example.com/b", excerpt="x")
    text = "一共五十六圈。传闻要换三名工程师[S1]。见[F9]。一圈五点六公里[F1]。"
    issues, _ = qa.number_issues(conn, eid, text)
    assert len(issues) == 4
    assert "带数字没标 [F#]" in issues[0] and "五十六圈" in issues[0]
    assert "（只有 [S1]）" in issues[1]
    assert "[F9] 不在事实表里" in issues[2]
    assert "和事实对不上" in issues[3] and "五点六" in issues[3]
    r = qa._numbers_item(conn, eid, _dir(tmp_path, text))
    assert not r["ok"] and r["hard"]


def test_number_value_from_content_and_detail_truncated(conn, tmp_path):
    eid = _ep(conn, "前瞻")
    facts.add(conn, eid, "F", "维特尔在雪邦赢过 4 次", source="F1 官网")
    assert qa.number_issues(conn, eid, "维特尔四次夺冠[F1]")[0] == []
    text = "".join(f"第{i}句有{i + 10}圈。" for i in range(8))
    r = qa._numbers_item(conn, eid, _dir(tmp_path, text))
    assert not r["ok"] and "等共 8 处" in r["detail"]


def test_numbers_missing_script(conn, tmp_path):
    r = qa._numbers_item(conn, _ep(conn), _dir(tmp_path))
    assert not r["ok"] and "逐字稿" in r["detail"]


# ---------- 复盘成绩、处罚 ----------

def _official(pens=None):
    def d(num, full, pos, cls, status, pts):
        return {"number": num, "full": full, "last": full.split()[-1], "pos": pos, "classified": cls,
                "status": status, "points": pts}
    return {"drivers": [d("1", "Max Verstappen", 1, "1", "Finished", 25.0),
                        d("4", "Lando Norris", 2, "2", "Finished", 18.0),
                        d("16", "Charles Leclerc", 19, "R", "Retired", 0.0),
                        d("44", "Lewis Hamilton", 20, "D", "Disqualified", 0.0)],
            "penalties": {"4": [5]} if pens is None else pens}


@pytest.fixture
def names(conn):
    ts = db.now()
    for en, zh in (("Max Verstappen", "维斯塔潘"), ("Lando Norris", "诺里斯"),
                   ("Charles Leclerc", "勒克莱尔"), ("Lewis Hamilton", "汉密尔顿")):
        conn.execute("INSERT OR REPLACE INTO glossary(kind, original, approved, state, updated_at) "
                     "VALUES ('车手', ?, ?, 'approved', ?)", (en, zh, ts))


def test_review_issues_pass(conn, names):
    text = ("维斯塔潘赢下比赛拿到25分。诺里斯第二名完赛并吃了5秒罚时。勒克莱尔中途退赛。"
            "汉密尔顿赛后被取消成绩。诺里斯一直在追维斯塔潘。维斯塔潘排位第三。诺里斯没能夺冠。")
    checked, bad, unparsed = qa.review_issues(conn, text, _official())
    assert bad == [] and checked == 6 and unparsed == 2


@pytest.mark.parametrize("text", [
    "并且让汉密尔顿失去了登上领奖台的机会",      # 期 18 误报：否定说法当成了上领奖台
    "汉密尔顿错过了领奖台",
    "汉密尔顿痛失领奖台",
    "汉密尔顿本来有望登上领奖台",
    "勒克莱尔还在冲击领奖台",
    "诺里斯丢掉了冠军",
])
def test_review_issues_negated_claims_not_flagged(conn, names, text):
    _, bad, unparsed = qa.review_issues(conn, text, _official())
    assert bad == [] and unparsed == 1


@pytest.mark.parametrize("text, want", [
    ("诺里斯拿下冠军", "官方第2名，稿里说冠军"),
    ("维斯塔潘第三名完赛", "稿里说第三"),
    ("Leclerc P5", "官方未完赛"),
    ("诺里斯被罚十秒", "赛会罚时5 秒，稿里说罚 10 秒"),
    ("维斯塔潘被罚5秒", "赛会罚时没有"),
    ("维斯塔潘退赛", "稿里说退赛"),
    ("诺里斯被取消成绩", "稿里说被取消成绩"),
    ("诺里斯拿到二十五分", "官方 18.0 分"),
    ("勒克莱尔登上领奖台", "稿里说上了领奖台"),
])
def test_review_issues_mismatch(conn, names, text, want):
    _, bad, _ = qa.review_issues(conn, text, _official())
    assert len(bad) == 1 and want in bad[0]


def test_review_item_branches(conn, names, tmp_path, monkeypatch):
    calls = []

    def fake(season, rnd):
        calls.append((season, rnd))
        return _official(pens={})
    monkeypatch.setattr(qa, "official_results", fake)
    monkeypatch.setattr(qa, "find_event", lambda c, ep: {"season": 2026, "round": 5})
    get = lambda eid: conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()  # noqa: E731

    r = qa._review_item(conn, get(_ep(conn, "前瞻")), _dir(tmp_path, "诺里斯拿下冠军"))
    assert r["ok"] and "前瞻分支不查" in r["detail"] and not calls

    eid = _ep(conn)
    r = qa._review_item(conn, get(eid), _dir(tmp_path, "维斯塔潘赢下比赛。诺里斯第二。"))
    assert r["ok"] and r["hard"] and "2026 第 5 站：核对 2 处一致" in r["detail"] and calls == [(2026, 5)]

    r = qa._review_item(conn, get(eid), _dir(tmp_path, "诺里斯拿下冠军。"))
    assert not r["ok"] and "稿里说冠军" in r["detail"]

    monkeypatch.setattr(qa, "official_results", lambda s, n: _official(pens=None) | {"penalties": None})
    r = qa._review_item(conn, get(eid), _dir(tmp_path, "诺里斯被罚五秒。"))
    assert r["ok"] and "没法自动核对" in r["detail"] and "罚时消息没取到" in r["detail"]


def test_review_item_failures(conn, tmp_path, monkeypatch):
    get = lambda eid: conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()  # noqa: E731
    ep = get(_ep(conn))
    r = qa._review_item(conn, ep, _dir(tmp_path))
    assert not r["ok"] and "逐字稿" in r["detail"]
    monkeypatch.setattr(qa, "find_event", lambda c, e: None)
    r = qa._review_item(conn, ep, _dir(tmp_path, "x"))
    assert not r["ok"] and "找不到本期对应的分站" in r["detail"]

    def boom(season, rnd):
        raise RuntimeError("离线")
    monkeypatch.setattr(qa, "find_event", lambda c, e: {"season": 2026, "round": 5})
    monkeypatch.setattr(qa, "official_results", boom)
    r = qa._review_item(conn, ep, _dir(tmp_path, "x"))
    assert not r["ok"] and r["hard"] and "取不到官方成绩无法复核" in r["detail"]


def test_official_results_from_mocked_session(monkeypatch):
    import pandas as pd
    from hermes import graphics

    class S:
        results = pd.DataFrame([
            {"DriverNumber": "4", "FullName": "Lando Norris", "LastName": "Norris", "Position": 1.0,
             "ClassifiedPosition": "1", "Status": "Finished", "Points": 25.0},
            {"DriverNumber": "16", "FullName": "Charles Leclerc", "LastName": "Leclerc", "Position": float("nan"),
             "ClassifiedPosition": "R", "Status": "Retired", "Points": float("nan")}])
        race_control_messages = pd.DataFrame({"Message": ["FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 4 (NOR)",
                                                          "TRACK CLEAR"]})

        def load(self, **kw):
            assert kw.get("messages") is True
    monkeypatch.setattr(graphics, "_load_session", lambda *a, **k: S())
    off = qa.official_results(2026, 5)
    assert off["penalties"] == {"4": [5]}
    assert off["drivers"][0]["pos"] == 1 and off["drivers"][1]["pos"] is None
    assert off["drivers"][1]["points"] is None and qa._retired(off["drivers"][1])


# ---------- 标题传闻 ----------

@pytest.mark.parametrize("title, ok, word", [
    ("雪邦前瞻 维斯塔潘能否卫冕", True, "没有人事"),
    ("据悉汉密尔顿将退役", True, "据悉"),
    ("汉密尔顿要退役？", True, "？"),
    ("汉密尔顿退役", False, "找不到对应的官方依据"),
])
def test_rumor_title_without_script(conn, tmp_path, title, ok, word):
    eid = _ep(conn, "资讯")
    r = qa._rumor_item(conn, eid, _dir(tmp_path, "", title))
    assert r["ok"] is ok and r["hard"] and word in r["detail"]


def test_rumor_title_backed_or_not(conn, tmp_path):
    eid = _ep(conn, "资讯")
    facts.add(conn, eid, "S", "某媒体称汉密尔顿将退役", source="某媒体", url="https://example.com/a", excerpt="x")
    facts.add(conn, eid, "S", "法拉利官宣汉密尔顿退役", source="F1 官网", url="https://www.formula1.com/x",
              excerpt="x")
    facts.add(conn, eid, "S", "据报道汉密尔顿或退役", source="F1 官网", url="https://www.formula1.com/y",
              excerpt="x")
    title = "汉密尔顿退役"
    r = qa._rumor_item(conn, eid, _dir(tmp_path, "汉密尔顿要退役了[S1]。", title))
    assert not r["ok"] and "只有非官方来源" in r["detail"]
    r = qa._rumor_item(conn, eid, _dir(tmp_path, "汉密尔顿要退役了[S3]。", title))
    assert not r["ok"]
    r = qa._rumor_item(conn, eid, _dir(tmp_path, "汉密尔顿正式退役[S2]。", title))
    assert r["ok"] and "官方依据" in r["detail"]
    facts.add(conn, eid, "F", "汉密尔顿宣布退役", source="FIA")
    r = qa._rumor_item(conn, eid, _dir(tmp_path, "汉密尔顿宣布退役[F1]。", title))
    assert r["ok"]


def test_rumor_missing_title_file(conn, tmp_path):
    r = qa._rumor_item(conn, _ep(conn), _dir(tmp_path, "x"))
    assert not r["ok"] and r["hard"] and "标题和简介" in r["detail"]


# ---------- 未授权人声 ----------

def _voice_ep(conn, tmp_path, *, method="剪映脚本", voice="热血解说员", take_voice="热血解说员", source=True):
    eid = _ep(conn)
    ep = _dir(tmp_path)
    if source:
        (ep / "03_配音" / "配音来源.json").write_text(json.dumps({"method": method, "voice": voice},
                                                             ensure_ascii=False), encoding="utf-8")
    if take_voice:
        conn.execute("INSERT INTO voice_takes(episode_id, stage_run, method, voice, raw_dir, audio_path, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (eid, 1, method, take_voice, str(ep / "03_配音"),
                                                str(ep / qa.VOICE_FILE), db.now()))
    tl = {"voice": qa.VOICE_FILE, "video": [{"asset_id": 1, "shot_idx": 0, "src_in": 0, "src_out": 2,
                                             "start": 0, "end": 2}]}
    return eid, ep, tl


def test_voice_pass(conn, tmp_path):
    eid, ep, tl = _voice_ep(conn, tmp_path)
    r = qa._voice_item(conn, eid, ep, tl)
    assert r["ok"] and r["hard"] and "热血解说员" in r["detail"]


@pytest.mark.parametrize("kw, edit, want", [
    ({"voice": "温柔女声"}, None, "配音来源记的音色是「温柔女声」"),
    ({"method": "外部录音"}, None, "不是剪映配音"),
    ({"take_voice": "温柔女声"}, None, "配音记录的音色是「温柔女声」"),
    ({"source": False}, None, "缺 03_配音/配音来源.json"),
    ({}, lambda tl: tl.update(voice="01_素材/采访.wav"), "人声轨是 01_素材/采访.wav"),
    ({}, lambda tl: tl.update(audio=[{"path": "x.wav"}]), "多了音轨 audio"),
    ({}, lambda tl: tl["video"][0].update(volume=0.6), "保留了素材原声"),
    ({}, lambda tl: tl["video"][0].update(keep_audio=True), "保留了素材原声"),
])
def test_voice_fail(conn, tmp_path, kw, edit, want):
    eid, ep, tl = _voice_ep(conn, tmp_path, **kw)
    if edit:
        edit(tl)
    r = qa._voice_item(conn, eid, ep, tl)
    assert not r["ok"] and r["hard"] and want in r["detail"]


def test_voice_other_take_path_and_no_timeline(conn, tmp_path):
    eid, ep, tl = _voice_ep(conn, tmp_path)
    conn.execute("UPDATE voice_takes SET audio_path='/elsewhere/配音.wav' WHERE episode_id=?", (eid,))
    assert "不是本期配音记录里的文件" in qa._voice_item(conn, eid, ep, tl)["detail"]
    assert "缺 06_合成/时间线.json" in qa._voice_item(conn, eid, ep, None)["detail"]


def test_voice_unauthorized_setting(conn, tmp_path):
    eid, ep, tl = _voice_ep(conn, tmp_path)
    conn.execute("DELETE FROM settings WHERE key='voice.name'")
    r = qa._voice_item(conn, eid, ep, tl)
    assert not r["ok"] and "没有授权音色" in r["detail"]


# ---------- 接入 ----------

def test_safe_turns_crash_into_hard_fail():
    def boom(*a):
        raise ValueError("坏了")
    for name in (qa.NUM_NAME, qa.REVIEW_NAME, qa.RUMOR_NAME, qa.VOICE_NAME):
        r = qa._safe(name, boom)
        assert r == [{"name": name, "ok": False, "hard": True, "detail": "检查出错：坏了"}]
