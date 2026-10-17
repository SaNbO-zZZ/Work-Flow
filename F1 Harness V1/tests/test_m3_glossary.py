import asyncio

import pytest

from hermes import glossary as g


def _row(conn, kind, original):
    return dict(conn.execute("SELECT * FROM glossary WHERE kind=? AND original=?", (kind, original)).fetchone())


def test_upsert_dedup_and_builtin(conn):
    assert g.upsert_originals(conn, "车手", ["Max Verstappen", "Max Verstappen", " ", "Zed Nobody"], source_hint="FastF1 2026") == 2
    assert g.upsert_originals(conn, "车手", ["Max Verstappen", "Arvid Lindblad"]) == 1
    r = _row(conn, "车手", "Max Verstappen")
    assert (r["suggested"], r["source"], r["state"], r["note"]) == ("维斯塔潘", "内置词典", "pending", "FastF1 2026")
    r = _row(conn, "车手", "Zed Nobody")
    assert r["suggested"] is None and r["source"] is None
    with pytest.raises(ValueError):
        g.upsert_originals(conn, "教练", ["X"])


def test_upsert_keeps_existing(conn):
    i = g.add_manual(conn, "车队", "Ferrari", "法拉利车队")
    assert g.upsert_originals(conn, "车队", ["Ferrari"]) == 0
    r = _row(conn, "车队", "Ferrari")
    assert r["id"] == i and r["approved"] == "法拉利车队" and r["state"] == "approved"


def test_seed_with_fake_loader(conn):
    def loader(season):
        assert season == 2025
        return {"大奖赛": ["Bahrain Grand Prix"], "赛道": ["Kuala Lumpur", "Suzuka"],
                "车手": ["Lewis Hamilton"], "车队": ["Racing Bulls"]}
    assert g.seed(conn, loader=loader, season=2025) == 5
    assert g.seed(conn, loader=loader, season=2025) == 0
    assert g.zh(conn, "赛道", "Kuala Lumpur") == "雪邦"
    assert g.zh(conn, "车队", "Racing Bulls") is None
    assert _row(conn, "赛道", "Suzuka")["note"] == "FastF1 2025"


def test_builtin_covers_known_kinds():
    assert set(g.BUILTIN) <= set(g.KINDS)
    assert len(g.BUILTIN["大奖赛"]) >= 23


def test_suggest_missing_fenced_json(conn):
    g.upsert_originals(conn, "车手", ["Max Verstappen", "Isack Hadjar"])
    g.upsert_originals(conn, "车队", ["Racing Bulls"])
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return '好的\n```json\n{"车手": {"Isack Hadjar": "哈贾尔", "Max Verstappen": "别覆盖"}, "车队": {"Racing Bulls": "小红牛"}}\n```'
    assert asyncio.run(g.suggest_missing(conn, ask=ask)) == 2
    assert "Isack Hadjar" in prompts[0] and "Max Verstappen" not in prompts[0].split("待翻译：")[1]
    r = _row(conn, "车手", "Isack Hadjar")
    assert (r["suggested"], r["source"], r["state"]) == ("哈贾尔", "GPT", "pending")
    assert g.zh(conn, "车手", "Max Verstappen") == "维斯塔潘"
    assert asyncio.run(g.suggest_missing(conn, ask=lambda p: pytest.fail("不该再调用"))) == 0


def test_suggest_missing_async_ask_and_partial(conn):
    g.upsert_originals(conn, "赛道", ["Miami Gardens", "Spielberg"])

    async def ask(prompt):
        return '{"赛道": {"Miami Gardens": "迈阿密"}, "车手": ["乱写"]}'
    assert asyncio.run(g.suggest_missing(conn, ask=ask)) == 1
    assert _row(conn, "赛道", "Spielberg")["suggested"] is None


def test_suggest_missing_bad_json(conn):
    g.upsert_originals(conn, "赛道", ["Spielberg"])
    for bad in ("不是 JSON", '{"赛道": {"Spielberg": }', "[1, 2]"):
        with pytest.raises(ValueError):
            asyncio.run(g.suggest_missing(conn, ask=lambda p, b=bad: b))
    assert _row(conn, "赛道", "Spielberg")["suggested"] is None


def test_set_translation(conn):
    g.upsert_originals(conn, "车手", ["Lando Norris", "Oliver Bearman", "Zed Nobody"])
    a, b, c = (_row(conn, "车手", n)["id"] for n in ("Lando Norris", "Oliver Bearman", "Zed Nobody"))
    g.set_translation(conn, a)
    r = _row(conn, "车手", "Lando Norris")
    assert (r["approved"], r["state"], r["source"]) == ("诺里斯", "approved", "内置词典")
    g.set_translation(conn, b, " 小熊 ")
    r = _row(conn, "车手", "Oliver Bearman")
    assert (r["approved"], r["suggested"], r["source"]) == ("小熊", "贝尔曼", "用户")
    assert g.zh(conn, "车手", "Oliver Bearman") == "小熊"
    with pytest.raises(ValueError):
        g.set_translation(conn, c)
    with pytest.raises(ValueError):
        g.set_translation(conn, 9999, "x")
    assert _row(conn, "车手", "Zed Nobody")["state"] == "pending"


def test_reopen(conn):
    g.upsert_originals(conn, "车手", ["Oliver Bearman"])
    i = _row(conn, "车手", "Oliver Bearman")["id"]
    g.set_translation(conn, i, "小熊")
    g.reopen(conn, i)
    r = _row(conn, "车手", "Oliver Bearman")
    assert (r["state"], r["approved"], r["suggested"]) == ("pending", None, "小熊")
    g.set_translation(conn, i)
    assert _row(conn, "车手", "Oliver Bearman")["approved"] == "小熊"


def test_add_manual_and_pending_count(conn):
    g.upsert_originals(conn, "车队", ["Racing Bulls", "Audi"])
    assert g.pending_count(conn) == 2
    i = g.add_manual(conn, "车队", "Racing Bulls", "小红牛")
    assert g.pending_count(conn) == 1
    assert g.add_manual(conn, "其他", "DRS", "DRS") != i
    r = _row(conn, "其他", "DRS")
    assert (r["state"], r["approved"], r["source"]) == ("approved", "DRS", "用户")
    assert [x["original"] for x in g.items(conn, state="approved")] == ["DRS", "Racing Bulls"]
    assert [x["original"] for x in g.items(conn, kind="车队")] == ["Audi", "Racing Bulls"]
    with pytest.raises(ValueError):
        g.add_manual(conn, "车队", "Audi", " ")


def test_unapproved_in(conn):
    g.upsert_originals(conn, "车手", ["Max Verstappen", "Lewis Hamilton", "Isack Hadjar"])
    g.set_translation(conn, _row(conn, "车手", "Lewis Hamilton")["id"])
    hits = g.unapproved_in(conn, "维斯塔潘和汉密尔顿在 isack hadjar 前面")
    assert sorted(h["original"] for h in hits) == ["Isack Hadjar", "Max Verstappen"]
    assert g.unapproved_in(conn, "今天没有车手") == []


def test_zh_approved_first(conn):
    assert g.zh(conn, "车手", "Nobody") is None
    g.upsert_originals(conn, "车手", ["Charles Leclerc"])
    assert g.zh(conn, "车手", "Charles Leclerc") == "勒克莱尔"
    g.set_translation(conn, _row(conn, "车手", "Charles Leclerc")["id"], "勒克莱尔（定稿）")
    assert g.zh(conn, "车手", "Charles Leclerc") == "勒克莱尔（定稿）"
