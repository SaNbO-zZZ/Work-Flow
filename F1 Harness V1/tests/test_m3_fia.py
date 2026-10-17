import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from hermes import fia

HTML = (Path(__file__).parent / "fixtures" / "fia_season.html").read_text()
SEASON = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14/season/season-2026-2072"
SPANISH = SEASON + "/event/Spanish%20Grand%20Prix"
# 锦标赛页的赛季下拉（原样摘录）；赛季页里当前赛季选项指回锦标赛页，不能用来找链接
CHAMP_HTML = (
    '<select id="facetapi_select_facet_form_3"><option value="0">Season</option>'
    '<option value="/documents/championships/fia-formula-one-world-championship-14/season/season-2026-2072">'
    'SEASON 2026</option><option value="/documents/championships/fia-formula-one-world-championship-14/season/'
    'season-2025-2071">SEASON 2025</option></select>')
# 按分站筛选页的结构：只有一个展开的分站
SPANISH_HTML = """<div class="decision-document-list"><ul class="event-wrapper"><li>
<div class="event-title active">Spanish Grand Prix</div><ul class="document-type-wrapper open"><li>
<ul class="document-row-wrapper non-categorised">
<li class="document-row key-74"><a href="/system/files/decision-document/2026_spanish_grand_prix_-_championship_points.pdf">
<div class="title"> Doc 74 - Championship Points </div>
<div class="published">Published on <span class="date-display-single">13.09.26 20:15</span> CET</div></a></li>
<li class="document-row key-72"><a href="/system/files/decision-document/2026_azerbaijan_grand_prix_-_championship_points.pdf">
<div class="title">Doc 72 - Championship Points</div></a></li>
<li class="document-row key-30"><div class="recalled-document"><div class="title">Recalled - Doc 30 - Curfew</div>
<div class="published">Published on <span class="date-display-single">25.09.26 08:14</span> CET</div></div></li>
</ul></li></ul></li></ul></div>"""


def doc(n: int) -> fia.Doc:
    return fia.Doc(f"https://www.fia.com/x/{n}.pdf", f"Doc {n}", "Azerbaijan Grand Prix", None)


def test_parse_real_page():
    docs = fia.parse(HTML)
    assert len(docs) == 6
    assert all(d.url.startswith("https://www.fia.com/system/files/decision-document/") and d.url.endswith(".pdf")
               for d in docs)
    assert all(d.title and d.event == "Azerbaijan Grand Prix" for d in docs)
    assert docs[0] == fia.Doc(
        "https://www.fia.com/system/files/decision-document/2026_azerbaijan_grand_prix_-_championship_points.pdf",
        "Doc 72 - Championship Points", "Azerbaijan Grand Prix", "2026-09-26T16:20:00+00:00")
    assert len({d.url for d in docs}) == len(docs)


def test_parse_dedup_and_missing_date():
    docs = fia.parse(SPANISH_HTML + SPANISH_HTML)
    assert [d.title for d in docs] == ["Doc 74 - Championship Points", "Doc 72 - Championship Points"]
    assert docs[0].published_at == "2026-09-13T18:15:00+00:00"
    assert docs[1].published_at is None


def test_published_timezone():
    # 冬令时 UTC+1，夏令时 UTC+2
    assert fia._published("08.03.26 09:37") == "2026-03-08T08:37:00+00:00"
    assert fia._published("26.09.26 18:20") == "2026-09-26T16:20:00+00:00"
    assert fia._published("n/a") is None


def test_season_url():
    assert fia.season_url(CHAMP_HTML, 2026) == SEASON
    assert fia.season_url(CHAMP_HTML, 2025).endswith("/season/season-2025-2071")
    assert fia.season_url(CHAMP_HTML, 1999) is None
    assert fia.season_url(HTML, 2026) is None
    assert fia._previous_event_url(HTML) == SPANISH


def test_fetch_merges_previous_event():
    pages = {fia.CHAMPIONSHIP_URL: CHAMP_HTML, SEASON: HTML, SPANISH: SPANISH_HTML}
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["user-agent"])
        return httpx.Response(200, text=pages[str(req.url)]) if str(req.url) in pages else httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": fia.UA}) as c:
            return await fia.fetch(2026, client=c)

    docs = asyncio.run(run())
    assert len(docs) == 7 and docs[-1].event == "Spanish Grand Prix"
    assert len(seen) == 3 and "Mozilla" in seen[0]
    with pytest.raises(ValueError):
        async def missing():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                await fia.fetch(1999, client=c)
        asyncio.run(missing())


def test_store_baseline_then_new(conn):
    assert fia.store(conn, [doc(1), doc(2)]) == []
    assert [r["notified"] for r in fia.recent(conn)] == [1, 1]
    new = fia.store(conn, [doc(3), doc(1), doc(2)])
    assert [(n["url"], n["title"]) for n in new] == [(doc(3).url, "Doc 3")]
    assert set(new[0]) == {"id", "url", "title", "event", "published_at"}
    assert fia.store(conn, [doc(3)]) == []
    row = conn.execute("SELECT notified FROM fia_docs WHERE id=?", (new[0]["id"],)).fetchone()
    assert row[0] == 0
    fia.mark_notified(conn, [new[0]["id"]])
    fia.mark_notified(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM fia_docs WHERE notified=0").fetchone()[0] == 0


def test_store_empty_baseline_keeps_table_empty(conn):
    assert fia.store(conn, []) == []
    assert fia.store(conn, [doc(1)]) == []


def test_recent_order(conn):
    fia.store(conn, [fia.Doc("https://a/1.pdf", "旧", "E", "2026-09-01T00:00:00+00:00"),
                     fia.Doc("https://a/2.pdf", "新", "E", "2026-09-02T00:00:00+00:00")])
    assert [r["title"] for r in fia.recent(conn)] == ["新", "旧"]
    assert len(fia.recent(conn, limit=1)) == 1


def test_poll_with_injected_fetch(conn):
    batches = [[doc(1), doc(2)], [doc(4), doc(1), doc(2)]]
    calls = []

    async def fake(season):
        calls.append(season)
        return batches[len(calls) - 1]

    assert asyncio.run(fia.poll(conn, 2026, fetch_fn=fake)) == []
    new = asyncio.run(fia.poll(conn, 2026, fetch_fn=fake))
    assert [n["title"] for n in new] == ["Doc 4"] and calls == [2026, 2026]


def test_interval_seconds():
    assert fia.interval_seconds("比赛周", True) == 3600
    assert fia.interval_seconds("比赛周", False) == 7200
    for k in ("发车区", "夏休", "冬歇"):
        assert fia.interval_seconds(k, False) == 86400
        assert fia.interval_seconds(k, True) == 86400
    with pytest.raises(ValueError):
        fia.interval_seconds("未知", False)


def test_due_boundaries():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    ago = lambda s: (now - timedelta(seconds=s)).isoformat(timespec="seconds")  # noqa: E731
    assert fia.due(None, now, "冬歇", False)
    assert fia.due(ago(3600), now, "比赛周", True)
    assert not fia.due(ago(3599), now, "比赛周", True)
    assert not fia.due(ago(3600), now, "比赛周", False)
    assert fia.due(ago(7200), now, "比赛周", False)
    assert not fia.due(ago(86399), now, "夏休", False)
    assert fia.due(ago(86400), now, "发车区", False)
    assert fia.due("2026-09-26T11:00:00", now, "比赛周", True)
