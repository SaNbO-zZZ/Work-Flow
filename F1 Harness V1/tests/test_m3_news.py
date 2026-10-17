import asyncio
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from hermes import news
from hermes.codex_runner import QuotaExhausted
from hermes.news import NewsItem

DAY = date(2026, 9, 29)
SINCE = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def add(conn, n=4):
    items = [NewsItem(f"新闻{i}", f"https://www.the-race.com/formula-1/story-{i}/", "The Race") for i in range(n)]
    news.store(conn, items, "gpt")
    return [r[0] for r in conn.execute("SELECT id FROM news_items ORDER BY id")]


class FakeSource:
    def __init__(self, name, items=None, exc=None):
        self.name, self.items, self.exc, self.calls = name, items or [], exc, 0

    async def fetch(self, since):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.items


def test_valid_url():
    assert news.valid_url("https://www.formula1.com/en/latest/article/x")
    assert news.valid_url("http://racefans.net/2026/09/29/a/")
    for bad in ("ftp://a.com/x", "javascript:alert(1)", "https://", "https://localhost/x", "www.a.com/x", "", "a b"):
        assert not news.valid_url(bad), bad


def test_normalize_and_dedupe(conn):
    u = "HTTPS://WWW.Autosport.com:443/f1/news/abc/?utm_source=x&utm_medium=y&fbclid=1&page=2#top"
    assert news.normalize_url(u) == "https://www.autosport.com/f1/news/abc?page=2"
    items = [
        NewsItem("A", "https://www.autosport.com/f1/news/abc/?utm_source=rss"),
        NewsItem("A 重复", "https://WWW.autosport.com/f1/news/abc"),
        NewsItem("非法链接", "not-a-url"),
        NewsItem("", "https://www.autosport.com/f1/news/empty-title"),
        NewsItem("B", "https://www.bbc.com/sport/formula1/articles/b1"),
    ]
    assert news.store(conn, items, "gpt") == 2
    assert news.store(conn, [NewsItem("A 再来", "https://www.autosport.com/f1/news/abc?utm_campaign=z")], "script") == 0
    urls = {r[0] for r in conn.execute("SELECT url FROM news_items")}
    assert urls == {"https://www.autosport.com/f1/news/abc", "https://www.bbc.com/sport/formula1/articles/b1"}


def test_script_source_mock_transport():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json=[
            {"title": "Verstappen news", "url": "https://www.motorsport.com/f1/news/v/", "source": "Motorsport.com",
             "published_at": "2026-09-29T01:00:00Z", "summary": "摘要"},
            {"title": "缺链接"},
            "垃圾",
        ])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            src = news.ScriptSource("http://127.0.0.1:9000/", client=c)
            assert src.available()
            return await src.fetch(datetime(2026, 9, 28, 8, 0, tzinfo=news.BEIJING))

    items = run(go())
    assert seen["url"] == "http://127.0.0.1:9000/news?since=2026-09-28T00%3A00%3A00%2B00%3A00"
    assert items[0] == NewsItem("Verstappen news", "https://www.motorsport.com/f1/news/v/", "Motorsport.com",
                                "2026-09-29T01:00:00Z", "摘要")
    assert len(items) == 2 and items[1].url == ""


def test_script_source_http_error():
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
            await news.ScriptSource("http://127.0.0.1:9000", client=c).fetch(SINCE)
    with pytest.raises(httpx.HTTPStatusError):
        run(go())


def test_from_settings(conn):
    assert news.ScriptSource.from_settings(conn) is None
    conn.execute("INSERT INTO settings(key, value) VALUES ('news.script_url', '  ')")
    assert news.ScriptSource.from_settings(conn) is None
    conn.execute("UPDATE settings SET value='http://127.0.0.1:9000' WHERE key='news.script_url'")
    src = news.ScriptSource.from_settings(conn)
    assert src.base_url == "http://127.0.0.1:9000" and src.available()
    assert not news.ScriptSource("").available()


def test_collect_falls_back_to_gpt(conn):
    script = FakeSource("script", exc=httpx.ConnectError("拒绝连接"))
    gpt = FakeSource("gpt", [NewsItem("Hamilton", "https://www.skysports.com/f1/news/1")])
    res = run(news.collect(conn, SINCE, sources=[script, gpt]))
    assert res["via"] == "gpt" and res["new"] == 1 and "拒绝连接" in res["error"]
    assert conn.execute("SELECT via FROM news_items").fetchone()[0] == "gpt"


def test_collect_script_ok_skips_gpt_and_unavailable(conn):
    gpt = FakeSource("gpt")
    ok = FakeSource("script", [NewsItem("A", "https://www.racefans.net/a")])
    res = run(news.collect(conn, SINCE, sources=[news.ScriptSource(""), ok, gpt]))
    assert res == {"via": "script", "new": 1, "error": None} and gpt.calls == 0


def test_collect_quota_propagates_and_all_fail(conn):
    with pytest.raises(QuotaExhausted):
        run(news.collect(conn, SINCE, sources=[FakeSource("gpt", exc=QuotaExhausted("usage limit"))]))
    res = run(news.collect(conn, SINCE, sources=[FakeSource("script", exc=ValueError("坏"))]))
    assert res["via"] is None and res["new"] == 0 and "坏" in res["error"]


def test_collect_default_without_script_uses_gpt(conn, monkeypatch):
    async def fake_fetch(self, since):
        return [NewsItem("X", "https://www.formula1.com/en/latest/article/x")]
    monkeypatch.setattr(news.GPTSource, "fetch", fake_fetch)
    assert run(news.collect(conn, SINCE))["via"] == "gpt"


def test_gpt_source_parse_fenced_and_drop_bad_urls(conn):
    payload = {"items": [
        {"title": "Leclerc 续约", "url": "https://www.formula1.com/en/latest/article/leclerc.1", "source": "Formula 1 官网",
         "published_at": None, "summary": "勒克莱尔续约"},
        {"title": "编造", "url": "www.fake.com/x", "source": "X", "published_at": None, "summary": ""},
        {"title": "", "url": "https://www.the-race.com/a", "source": "The Race", "published_at": None, "summary": ""},
        {"title": "FTP", "url": "ftp://fia.com/doc.pdf", "source": "FIA", "published_at": None, "summary": ""},
    ]}
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return "好的：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    items = run(news.GPTSource(conn, runner=runner).fetch(datetime(2026, 9, 28, 8, 0, tzinfo=news.BEIJING)))
    assert [i.title for i in items] == ["Leclerc 续约"] and items[0].published_at is None
    assert "2026-09-28 08:00" in prompts[0] and "The Race" in prompts[0]


def test_gpt_source_async_runner_and_bare_list(conn):
    async def runner(prompt):
        return json.dumps([{"title": "T", "url": "https://www.autosport.com/f1/news/t"}])
    assert len(run(news.GPTSource(conn, runner=runner).fetch(SINCE))) == 1
    with pytest.raises(ValueError):
        run(news.GPTSource(conn, runner=lambda p: "不是 JSON").fetch(SINCE))


def test_pick_top3_filters_invalid(conn):
    ids = add(conn)
    out = {"picks": [
        {"news_id": 9999, "branch": "资讯", "headline": "不存在", "reason": "x"},
        {"news_id": ids[0], "branch": "娱乐", "headline": "非法分支", "reason": "x"},
        {"news_id": ids[1], "branch": "资讯", "headline": "维斯塔潘\n转会传闻", "reason": "单条，讨论度高"},
        {"news_id": ids[1], "branch": "前瞻", "headline": "重复", "reason": "x"},
        {"news_id": str(ids[2]), "branch": "前瞻", "headline": "新加坡前瞻", "reason": "下周比赛"},
        {"news_id": ids[3], "branch": "科普", "headline": "新规", "reason": "讲规则"},
        {"news_id": ids[0], "branch": "复盘", "headline": "第四条", "reason": "应被截断"},
    ]}
    picks = run(news.pick_top3(conn, DAY, runner=lambda p: json.dumps(out, ensure_ascii=False)))
    assert [(p["rank"], p["news_id"], p["branch"]) for p in picks] == [
        (1, ids[1], "资讯"), (2, ids[2], "前瞻"), (3, ids[3], "科普")]
    assert picks[0]["headline"] == "维斯塔潘 转会传闻" and picks[0]["reason"] == "单条，讨论度高"
    assert picks[0]["url"].startswith("https://www.the-race.com/") and picks[0]["source"] == "The Race"


def test_pick_top3_all_invalid_raises(conn):
    ids = add(conn, 2)
    with pytest.raises(ValueError):
        run(news.pick_top3(conn, DAY, runner=lambda p: json.dumps([{"news_id": ids[0], "branch": "八卦"}])))
    with pytest.raises(ValueError):
        run(news.pick_top3(conn, DAY, runner=lambda p: "抱歉"))
    assert news.picks_for(conn, DAY) == []


def test_pick_top3_no_candidates_skips_gpt(conn):
    def runner(p):
        raise AssertionError("不应调用")
    assert run(news.pick_top3(conn, DAY, runner=runner)) == []
    conn.execute("INSERT INTO news_items(url, title, via, fetched_at) VALUES ('https://a.com/old', '旧闻', 'gpt', "
                 "'2026-01-01T00:00:00+00:00')")
    assert run(news.pick_top3(conn, DAY, runner=runner)) == []


def test_pick_top3_same_day_rerun_overwrites(conn):
    ids = add(conn)
    first = [{"news_id": i, "branch": "资讯", "headline": f"h{i}", "reason": "r"} for i in ids[:3]]
    run(news.pick_top3(conn, DAY, runner=lambda p: json.dumps(first)))
    second = [{"news_id": ids[3], "branch": "复盘", "headline": "只剩一条", "reason": "r2"}]
    picks = run(news.pick_top3(conn, DAY, runner=lambda p: json.dumps(second)))
    assert [(p["rank"], p["news_id"]) for p in picks] == [(1, ids[3])]
    assert conn.execute("SELECT COUNT(*) FROM news_picks WHERE day='2026-09-29'").fetchone()[0] == 1
    run(news.pick_top3(conn, date(2026, 9, 30), runner=lambda p: json.dumps(first)))
    assert len(news.picks_for(conn, DAY)) == 1 and len(news.picks_for(conn, date(2026, 9, 30))) == 3


def test_pick_prompt_lists_candidates(conn):
    ids = add(conn, 2)
    prompts = []

    def runner(p):
        prompts.append(p)
        return json.dumps({"picks": [{"news_id": ids[0], "branch": "资讯", "headline": "h", "reason": "r"}]})
    run(news.pick_top3(conn, DAY, runner=runner))
    assert f"[{ids[0]}] The Race" in prompts[0] and "2026-09-29" in prompts[0]


def test_daily_since_and_flow(conn):
    assert news.daily_since(DAY) == datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    src = FakeSource("gpt", [NewsItem("Norris", "https://www.bbc.com/sport/formula1/articles/n")])
    got = {}

    async def fetch(since):
        got["since"] = since
        return src.items
    src.fetch = fetch

    def runner(p):
        nid = conn.execute("SELECT id FROM news_items").fetchone()[0]
        return json.dumps({"picks": [{"news_id": nid, "branch": "资讯", "headline": "诺里斯", "reason": "r"}]})
    picks = run(news.daily(conn, DAY, sources=[src], runner=runner))
    assert got["since"] == news.daily_since(DAY) and picks[0]["title"] == "Norris"


def test_format_picks():
    picks = [
        {"rank": 1, "headline": "维斯塔潘续约传闻", "title": "Verstappen", "branch": "资讯",
         "reason": "单条，讨论度高", "source": "The Race", "url": "https://www.the-race.com/a"},
        {"rank": 2, "headline": "", "title": "Original title", "branch": "前瞻", "reason": "",
         "source": None, "url": "https://www.formula1.com/b"},
    ]
    assert news.format_picks(picks, DAY) == (
        "9月29日 前三推荐\n"
        "1. 维斯塔潘续约传闻\n   建议分支：资讯\n   理由：单条，讨论度高\n   来源：The Race https://www.the-race.com/a\n"
        "2. Original title\n   建议分支：前瞻\n   理由：—\n   来源：未知来源 https://www.formula1.com/b")
    assert news.format_picks([], date(2026, 10, 1)) == "10月1日 前三推荐\n今天没有值得推荐的新闻。"


def test_run_with_search_args(monkeypatch, tmp_path):
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self, data):
            captured["stdin"] = data
            lines = [{"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
                     {"type": "turn.completed", "usage": {"input_tokens": 5}}]
            return "\n".join(json.dumps(x) for x in lines).encode(), b""

    async def fake_exec(*args, **kw):
        captured["args"], captured["env"] = args, kw["env"]
        return Proc()
    monkeypatch.setattr(news.codex_runner.asyncio, "create_subprocess_exec", fake_exec)
    r = run(news.run_with_search("p", "gpt-6-sol", "high", tmp_path))
    args = list(captured["args"])
    assert r.message == "{}" and r.usage == {"input_tokens": 5}
    assert args[-3:] == ["-c", 'web_search="live"', "-"] and "--ephemeral" in args
    assert args[args.index("-s") + 1] == "read-only" and any(a.startswith("skills.config=") for a in args)
    assert captured["env"]["HERMES_HOME"].endswith("skill-home")
