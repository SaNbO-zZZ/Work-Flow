import asyncio
from datetime import date, datetime, timezone

from hermes import calendar as cal
from hermes import chat, db, episodes, jobs, news
from test_m3_calendar import loader_of, season_2026


def _load(conn):
    cal.refresh(conn, [2026], loader=loader_of({2026: season_2026()}))
    conn.execute("INSERT INTO glossary(kind, original, suggested, state, source, updated_at) VALUES "
                 "('大奖赛','Bahrain Grand Prix','巴林大奖赛','pending','测试',?),"
                 "('大奖赛','Azerbaijan Grand Prix','阿塞拜疆大奖赛','pending','测试',?),"
                 "('赛道','Kuala Lumpur','雪邦','pending','测试',?)", (db.now(),) * 3)


def test_episode_label_by_calendar(conn):
    assert chat.episode_label(conn, "资讯", None, date(2026, 9, 29)) == "待定"
    _load(conn)
    cases = [
        ("资讯", None, date(2026, 9, 29), "巴林大奖赛"),
        ("科普", None, date(2026, 8, 5), "夏休"),
        ("资讯", None, date(2026, 4, 15), "发车区"),
        ("科普", None, date(2026, 12, 20), "冬歇"),
        ("前瞻", "雪邦", date(2026, 9, 29), "巴林大奖赛"),
        ("前瞻", None, date(2026, 9, 29), "巴林大奖赛"),
        ("复盘", None, date(2026, 9, 29), "阿塞拜疆大奖赛"),
        ("复盘", "Azerbaijan", date(2026, 9, 29), "阿塞拜疆大奖赛"),
    ]
    for branch, gp, day, want in cases:
        assert chat.episode_label(conn, branch, gp, day) == want, (branch, gp, day)


def test_start_episode_uses_calendar(conn):
    _load(conn)
    eid = episodes.new_session(conn)
    msg = chat.apply_action(conn, eid, {"type": "start_episode", "branch": "资讯", "title": "车手市场"})
    code = episodes.get(conn, eid)["code"]
    assert "-资讯-" in code and code.endswith("-01") and code in msg


def test_period_context(conn):
    assert "赛历还没载入" in chat.period_context(conn, date(2026, 9, 29))
    _load(conn)
    text = chat.period_context(conn, date(2026, 9, 29))
    assert "周二" in text and "本周是 巴林大奖赛 比赛周" in text
    assert "上一站：第6站 阿塞拜疆大奖赛" in text and "下一站：第7站 巴林大奖赛（雪邦" in text


def test_job_due_rules(conn):
    t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
    assert jobs.calendar_due(conn, t("2026-09-29 00:00"))
    jobs._set(conn, "job.calendar.last", "2026-09-28T23:00:00+00:00")   # 北京 9-29 07:00
    assert not jobs.calendar_due(conn, t("2026-09-29 10:00"))
    assert jobs.calendar_due(conn, t("2026-09-29 22:00"))               # 北京 9-30 06:00

    assert not jobs.news_due(conn, t("2026-09-29 23:59"))               # 北京 07:59
    assert jobs.news_due(conn, t("2026-09-30 00:00"))                   # 北京 08:00
    jobs._set(conn, "job.news.tries", "2026-09-30:1")
    jobs._set(conn, "job.news.last_try", "2026-09-30T00:00:00+00:00")
    assert not jobs.news_due(conn, t("2026-09-30 00:10"))               # 失败后 30 分钟内不重试
    assert jobs.news_due(conn, t("2026-09-30 00:31"))
    jobs._set(conn, "job.news.tries", "2026-09-30:3")
    assert not jobs.news_due(conn, t("2026-09-30 02:00"))               # 一天最多 3 次
    jobs._set(conn, "job.news.done", "2026-09-30")
    assert not jobs.news_due(conn, t("2026-09-30 05:00"))

    _load(conn)
    assert jobs.fia_due(conn, t("2026-09-29 00:00"))
    jobs._set(conn, "job.fia.last", "2026-09-29T00:00:00+00:00")
    assert not jobs.fia_due(conn, t("2026-09-29 01:30"))                # 比赛周非比赛日 2 小时
    assert jobs.fia_due(conn, t("2026-09-29 02:00"))


def test_job_news_posts_session(conn, monkeypatch):
    picks = [{"rank": 1, "headline": "诺里斯道歉", "branch": "资讯", "reason": "讨论度高", "title": "t",
              "url": "https://www.formula1.com/a", "source": "F1"}]

    async def fake_daily(c, day, **kw):
        return picks
    monkeypatch.setattr(news, "daily", fake_daily)
    sent = []
    monkeypatch.setattr(jobs, "notify", lambda title, body, eid=None: sent.append((title, body, eid)))
    eid = asyncio.run(jobs.job_news(conn, datetime(2026, 9, 30, 0, 5, tzinfo=timezone.utc)))
    ep = episodes.get(conn, eid)
    assert ep["title"] == "9月30日 前三推荐" and ep["state"] == "闲聊"
    msg = conn.execute("SELECT content FROM messages WHERE episode_id=?", (eid,)).fetchone()[0]
    assert "诺里斯道歉" in msg and "https://www.formula1.com/a" in msg
    assert sent == [("F1 Harness · 今日前三推荐", "诺里斯道歉", eid)]
    assert jobs._get(conn, "job.news.done") == "2026-09-30"


def test_fia_retry_after_failure(conn):
    t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
    jobs._set(conn, "job.fia.retry_after", "2026-09-29T10:10:00+00:00")
    assert not jobs.fia_due(conn, t("2026-09-29 10:05"))
    assert jobs.fia_due(conn, t("2026-09-29 10:11"))
