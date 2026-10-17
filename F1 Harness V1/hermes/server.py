"""本地网页服务（只监听 127.0.0.1:8790）：会话、聊天（SSE 逐字）、关卡审核、额度/用量、设置。"""
import asyncio
import json
import logging
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import segno

from . import (assets, benchmark, bilibili, calendar, chat, codex_runner, compose, config, db, download, drive, episodes, fia,
               glossary, jobs, lessons, library, macui, news, pipeline, pricing, steps, versions)
from . import asset_identity, sourcing
from . import social_login
from .appserver import AppServer
from .events import broker, notify
from .scheduler import Scheduler, StepNotReady

log = logging.getLogger("hermes.server")
BEIJING = ZoneInfo("Asia/Shanghai")
WEB = Path(__file__).resolve().parents[1] / "web" / "app"
PORT = 8790
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class State:
    conn = None
    app_server: AppServer | None = None
    scheduler: Scheduler | None = None
    runner: "jobs.Jobs | None" = None
    loaded_threads: set[str] = set()
    tasks: list[asyncio.Task] = []


S = State()


async def _not_ready(task, ep):
    raise StepNotReady("要到 M6 才实现")


def _on_event(kind: str, ep, detail: str) -> None:
    if kind == "gate":
        notify("F1 Harness · 等你审核", f"{ep['title']} 到了 {detail}", ep["id"])
    elif kind == "publish":
        notify("F1 Harness · 成片待发布", f"{ep['title']}：去“展示成果”取文件，确认后手动上传 Drive", ep["id"])
    elif kind == "needs_human":
        notify("F1 Harness · 需要你处理", f"{ep['title']} 在 {detail} 阶段失败 3 次", ep["id"])
    broker.publish("*", "refresh", {"episode_id": ep["id"]})


async def _periodic() -> None:
    while True:
        try:
            pricing.refresh_fx(S.conn)
            await S.app_server.read_rate_limits()
            broker.publish("*", "topbar", {})
        except Exception as e:
            log.warning("定时刷新失败：%s", e)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.conn = db.connect()
    # 上次退出时正在入库的素材作废，下载队列重跑时会沿用原编号重新入库
    S.conn.execute("UPDATE assets SET state='failed' WHERE state='processing'")
    try:
        asset_identity.backfill(S.conn)   # 补算还没认过哪站哪年的素材
    except Exception:
        log.exception("素材身份补算失败")
    S.app_server = AppServer()
    S.scheduler = Scheduler(db.connect, {"*": _not_ready, **steps.EXECUTORS}, on_event=_on_event)
    S.runner = jobs.Jobs(S.conn, db.connect)
    S.tasks = [asyncio.create_task(S.scheduler.run_forever()), asyncio.create_task(_periodic())]
    if config.RUN_JOBS:
        S.tasks.append(asyncio.create_task(S.runner.run_forever()))
        S.tasks.append(asyncio.create_task(download.worker(S.conn, ingest=library.ingest_and_process)))
    yield
    S.scheduler.stop()
    for t in S.tasks:
        t.cancel()
    await S.app_server.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # 防 DNS 重绑定和跨站请求：只认本机 Host；写操作必须带自定义头且同源
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("x-hermes") != "1" or (origin and origin.split("//", 1)[-1] not in ALLOWED_HOSTS):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # 界面文件每次都向服务确认版本，改完刷新即生效
    resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/review")
async def review_page():
    return FileResponse(WEB / "review.html")


# ---------- 数据 ----------
def _row(r):
    return dict(r) if r else None


@app.get("/api/bootstrap")
async def bootstrap():
    return {
        "models": codex_runner.available_models(),
        "effort_labels": config.EFFORT_LABELS,
        "default": {"model": config.DEFAULT_CHAT_MODEL[0], "effort": config.DEFAULT_CHAT_MODEL[1]},
        "roles": [{"role": k, "model": v[0], "effort": config.EFFORT_LABELS.get(v[1], v[1])} for k, v in config.ROLE_MODELS.items()],
        "price_source": pricing.PRICE_SOURCE,
    }


GROUPS = [("等我审核", lambda s: s.endswith("待审") or s in ("需人工", "待发布")), ("制作中", lambda s: s in pipeline.WORK_STAGES),
          ("排队中", lambda s: s in ("排队", "暂停")), ("已完成", lambda s: s in ("归档倒计时", "本地已清理", "已取消")),
          ("闲聊", lambda s: s == "闲聊")]


@app.get("/api/sessions")
async def sessions():
    rows = S.conn.execute("SELECT id, code, title, state, branch, deadline_at, jump_at, queued_at FROM episodes ORDER BY updated_at DESC").fetchall()
    queue = [r["id"] for r in S.conn.execute(
        "SELECT id FROM episodes WHERE state='排队' ORDER BY jump_at IS NULL, jump_at DESC, queued_at, id")]
    out = []
    for name, match in GROUPS:
        items = [dict(r) | ({"queue_pos": queue.index(r["id"]) + 1} if r["id"] in queue else {}) for r in rows if match(r["state"])]
        out.append({"name": name, "items": items})
    return out


@app.post("/api/sessions")
async def new_session():
    return {"id": episodes.new_session(S.conn)}


def _get_episode(eid: int):
    try:
        return episodes.get(S.conn, eid)
    except episodes.EpisodeError:
        raise HTTPException(404, "没有这个会话")


@app.get("/api/sessions/{eid}")
async def session_detail(eid: int):
    ep = _get_episode(eid)
    # 按时间排：关卡卡片是迁移时补的，id 在后面（第 3 批）
    msgs = [dict(m) | {"card": versions.enrich(S.conn, ep, json.loads(m["card"])) if m["card"] else None}
            for m in S.conn.execute("SELECT id, role, content, model, effort, card, created_at FROM messages "
                                    "WHERE episode_id=? ORDER BY created_at, id", (eid,))]
    gates = [dict(g) for g in S.conn.execute("SELECT * FROM gates WHERE episode_id=? ORDER BY id", (eid,))]
    tasks = [dict(t) for t in S.conn.execute(
        "SELECT id, step, role, model, effort, state, attempts FROM tasks WHERE episode_id=? AND stage_run=? ORDER BY phase, id",
        (eid, ep["stage_run"]))]
    reuse = [dict(r) for r in S.conn.execute(
        """SELECT u.id, u.asset_id, u.src_in, u.src_out, a.title, a.credit FROM asset_uses u JOIN assets a ON a.id=u.asset_id
           WHERE u.episode_id=? AND u.approval='pending' ORDER BY u.id""", (eid,))]
    archive = S.conn.execute("SELECT drive_path, upload_state, delete_after, deleted_at FROM archives WHERE episode_id=?",
                             (eid,)).fetchone()
    return {"episode": dict(ep), "messages": msgs, "gates": gates, "tasks": tasks,
            "flow": pipeline.FLOW, "reject_targets": pipeline.REJECT_TARGETS,
            "reuse": reuse, "archive": dict(archive) if archive else None,
            "versions": versions.gate_versions(S.conn, ep)}


@app.get("/api/episodes/{eid}/review")
async def episode_review(eid: int):
    return {**steps.review_data(S.conn, _get_episode(eid)), "cover_running": eid in _cover_runs}


@app.get("/api/episodes/{eid}/media/{name}")
async def episode_media(eid: int, name: str):
    # 只给白名单里的固定文件，不接受路径参数
    path = steps.media_path(_get_episode(eid), name)
    if path is None:
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/episodes/{eid}/file/{rel:path}")
async def episode_file(eid: int, rel: str):
    """会话卡片里的旧版本文件（09_版本 等）；只给期文件夹里面的文件。"""
    ep = _get_episode(eid)
    if not ep["folder"]:
        raise HTTPException(404)
    root = Path(ep["folder"]).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file() or any(p.startswith(".") for p in path.relative_to(root).parts):
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/sessions/{eid}/messages")
async def post_message(eid: int, request: Request):
    _get_episode(eid)
    body = await request.json()
    text = (body.get("text") or "").strip()
    model, effort = body.get("model") or config.DEFAULT_CHAT_MODEL[0], body.get("effort") or config.DEFAULT_CHAT_MODEL[1]
    allowed = {m["slug"]: m["efforts"] for m in codex_runner.available_models()}
    if not text or len(text) > 20000:
        raise HTTPException(400, "消息为空或过长")
    if model not in allowed or effort not in allowed[model]:
        raise HTTPException(400, "模型或推理强度不可用")
    if chat.is_duplicate(eid, text):
        return JSONResponse({"ok": True, "duplicate": True}, status_code=202)
    asyncio.create_task(chat.handle_user_message(S.conn, S.app_server, eid, text, model, effort, S.loaded_threads))
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/api/episodes/{eid}/action/{action}")
async def episode_action(eid: int, action: str):
    _get_episode(eid)
    fn = {"pause": episodes.pause, "resume": episodes.resume, "jump": episodes.jump, "cancel": episodes.cancel,
          "publish": episodes.publish}.get(action)
    if not fn:
        raise HTTPException(404)
    try:
        fn(S.conn, eid)
    except episodes.EpisodeError as e:
        raise HTTPException(409, str(e))
    broker.publish("*", "refresh", {"episode_id": eid})
    return {"ok": True}


@app.post("/api/episodes/{eid}/reveal")
async def episode_reveal(eid: int):
    """在访达里打开交付文件夹（选中成片），方便拖去抖音创作者中心。"""
    ep = _get_episode(eid)
    if not ep["folder"]:
        raise HTTPException(404, "这一期还没有文件夹")
    out = Path(ep["folder"]) / "08_交付"
    video = out / "成片.mp4"
    if not out.is_dir():
        raise HTTPException(404, "还没有交付文件夹")
    subprocess.run(["open", "-R", str(video)] if video.is_file() else ["open", str(out)], check=False)
    return {"ok": True, "path": str(out)}


_cover_runs: dict[int, asyncio.Task] = {}
COVER_STATES = ("经验总结", "待发布")


@app.post("/api/episodes/{eid}/cover")
async def episode_cover(eid: int, request: Request):
    """重做封面（AI）：后台跑，做完重出 08_交付；旧封面留在 06_合成/封面版本/。body 可带 note（对封面的意见）。"""
    ep = _get_episode(eid)
    if ep["state"] not in COVER_STATES:
        raise HTTPException(409, f"只有{'、'.join(COVER_STATES)}时能重做封面，现在是{ep['state']}")
    if eid in _cover_runs:
        raise HTTPException(409, "封面正在重做，完成后会在会话里说")
    body = await request.json() if await request.body() else {}
    note = str(body.get("note") or "").strip() or None

    async def run():
        res = await steps.ai_cover(ep, note)
        if res.get("ok"):
            await steps._thread(compose.deliver, eid, Path(ep["folder"]))
        broker.publish("*", "refresh", {"episode_id": eid})

    t = asyncio.create_task(run())
    _cover_runs[eid] = t
    t.add_done_callback(lambda _: _cover_runs.pop(eid, None))
    broker.publish("*", "refresh", {"episode_id": eid})
    return JSONResponse({"ok": True, "detail": "已开始重做封面，大约 5 分钟"}, status_code=202)


@app.post("/api/episodes/{eid}/resource")
async def episode_resource(eid: int, request: Request):
    """重跑找素材：退回配素材与粗剪，本轮 D2 强制联网重找。body 可带 note（要换哪段、换成什么）。"""
    _get_episode(eid)
    body = await request.json() if await request.body() else {}
    try:
        run = steps.request_resource(S.conn, eid, str(body.get("note") or ""))
    except episodes.EpisodeError as e:
        raise HTTPException(409, str(e))
    broker.publish("*", "refresh", {"episode_id": eid})
    return {"ok": True, "stage_run": run}


@app.post("/api/episodes/{eid}/race_share/approve")
async def race_share_approve(eid: int):
    """G2 认可普通比赛画面超比例（上限 45%，2026-10-14 用户定）：记进 06_合成/比赛画面占比认可.json。"""
    from . import qa_checks
    _get_episode(eid)
    try:
        rec = qa_checks.approve_race_share(S.conn, eid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    broker.publish("*", "refresh", {"episode_id": eid})
    return {"ok": True, "share": rec["share"], "stage_run": rec["stage_run"]}


@app.post("/api/episodes/{eid}/gates/{gate}/{decision}")
async def gate_decision(eid: int, gate: str, decision: str, request: Request):
    ep = _get_episode(eid)
    body = await request.json() if await request.body() else {}
    score = body.get("score")
    try:
        score = None if score in (None, "") else int(score)
    except (TypeError, ValueError):
        raise HTTPException(400, "打分要是 0–100 的整数")
    if score is not None and not 0 <= score <= 100:
        raise HTTPException(400, "打分要是 0–100 的整数")
    try:
        if decision == "approve":
            nxt = episodes.approve(S.conn, eid, gate, score, body.get("feedback"))
        elif decision == "reject":
            nxt = episodes.reject(S.conn, eid, gate, body.get("feedback") or "", body.get("back_to"), score)
        else:
            raise HTTPException(404)
    except (episodes.EpisodeError, ValueError, KeyError) as e:
        raise HTTPException(409, str(e))
    notes = steps.after_gate(S.conn, ep, gate, decision, score)
    broker.publish("*", "refresh", {"episode_id": eid})
    return {"ok": True, "state": nxt, "notes": notes}


@app.post("/api/episodes/{eid}/sourcing/other_library/{decision}")
async def sourcing_other_library(eid: int, decision: str):
    ep = _get_episode(eid)
    try:
        out = sourcing.decide_other_library(S.conn, ep, decision)
    except ValueError:
        raise HTTPException(404)
    except LookupError as e:
        raise HTTPException(409, str(e))
    broker.publish("*", "refresh", {"episode_id": eid})
    return {"ok": True, **out}


# ---------- 数据层（M3） ----------
def _setting(key: str) -> str | None:
    row = S.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


@app.get("/api/data/overview")
async def data_overview():
    today = datetime.now(BEIJING).date()
    p = calendar.period(S.conn, today)
    race_day = calendar.is_race_day(S.conn, today)
    return {
        "today": today.isoformat(), "period": p.kind, "label": calendar.gp_label(S.conn, today), "race_day": race_day,
        "calendar_at": _setting("job.calendar.last"), "fia_at": _setting("job.fia.last"),
        "fia_interval": fia.interval_seconds(p.kind, race_day), "news_done": _setting("job.news.done"),
        "news_session": _setting("job.news.session"), "glossary_pending": glossary.pending_count(S.conn),
        "running": [k for k, t in S.runner.running.items() if not t.done()] if S.runner else [],
    }


@app.get("/api/data/calendar")
async def data_calendar(season: int | None = None):
    today = datetime.now(BEIJING).date()
    season = season or today.year
    p = calendar.period(S.conn, today)
    evs = calendar.events(S.conn, season)
    for e in evs:
        e["location_zh"] = glossary.zh(S.conn, "赛道", e["location"] or "")
    changes = [dict(r) for r in S.conn.execute("SELECT * FROM calendar_changes ORDER BY id DESC LIMIT 20")]
    seasons = [r[0] for r in S.conn.execute("SELECT DISTINCT season FROM race_events ORDER BY season")]
    return {"season": season, "seasons": seasons, "events": evs, "changes": changes,
            "current_round": p.round if p.season == season else None}


@app.get("/api/data/fia")
async def data_fia():
    return {"docs": fia.recent(S.conn, 60)}


@app.get("/api/data/news")
async def data_news():
    day = _setting("job.news.done")
    picks = news.picks_for(S.conn, datetime.fromisoformat(day).date()) if day else []
    return {"day": day, "picks": picks}


@app.post("/api/data/run/{name}")
async def data_run(name: str):
    if name not in jobs.JOB_TIMEOUT:
        raise HTTPException(404)
    return {"started": S.runner.trigger(name)}


@app.get("/api/glossary")
async def glossary_list(state: str | None = None, kind: str | None = None):
    return {"items": glossary.items(S.conn, state=state, kind=kind), "pending": glossary.pending_count(S.conn),
            "kinds": glossary.KINDS}


@app.post("/api/glossary")
async def glossary_add(request: Request):
    body = await request.json()
    kind, original, text = (body.get(k, "").strip() for k in ("kind", "original", "text"))
    if kind not in glossary.KINDS or not original or not text:
        raise HTTPException(400, "类别、原名、译名都要填")
    return {"id": glossary.add_manual(S.conn, kind, original, text)}


@app.post("/api/glossary/{item_id}/{action}")
async def glossary_action(item_id: int, action: str, request: Request):
    body = await request.json() if await request.body() else {}
    try:
        if action == "approve":
            glossary.set_translation(S.conn, item_id, (body.get("text") or "").strip() or None)
        elif action == "reopen":
            glossary.reopen(S.conn, item_id)
        else:
            raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "pending": glossary.pending_count(S.conn)}


# ---------- Benchmark 与经验笔记（M6-8） ----------
@app.get("/api/benchmark")
async def benchmark_data(branch: str | None = None):
    return benchmark.data(S.conn, branch)


@app.get("/api/lessons")
async def lessons_list(state: str | None = None):
    return lessons.listing(S.conn, state or None)


@app.post("/api/lessons/{lesson_id}/{action}")
async def lesson_action(lesson_id: int, action: str, request: Request):
    if action not in ("approve", "reject"):
        raise HTTPException(404)
    body = await request.json() if await request.body() else {}
    edits = {k: body[k] for k in ("content", "branch", "step") if isinstance(body.get(k), str)}
    try:
        item = lessons.decide(S.conn, lesson_id, action, **edits)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": item, "pending": lessons.listing(S.conn, "pending")["pending"]}


@app.get("/api/lessons/episodes")
async def lesson_episodes():
    return {"items": lessons.g3_episodes(S.conn)}


_lesson_runs: dict[int, asyncio.Task] = {}


@app.post("/api/episodes/{eid}/lessons/summarize")
async def lesson_rerun(eid: int):
    _get_episode(eid)
    if not lessons.g3_passed(S.conn, eid):
        raise HTTPException(400, "这一期还没在 G3 通过，不能做经验总结")
    if eid in _lesson_runs:
        raise HTTPException(409, "这一期的经验总结正在做，完成后出现在待批准里")
    t = asyncio.create_task(steps.lesson_rerun(eid))
    _lesson_runs[eid] = t
    t.add_done_callback(lambda _: _lesson_runs.pop(eid, None))
    return JSONResponse({"ok": True, "detail": "已开始补做经验总结，完成后出现在待批准里"}, status_code=202)


# ---------- 素材库（M4） ----------
def _library_file(p: str | None) -> Path:
    # 只允许读素材库里的文件，防止拿数据库里的路径读任意文件
    if not p:
        raise HTTPException(404)
    path = Path(p).resolve()
    if not path.is_relative_to(config.LIBRARY_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return path


@app.get("/api/assets")
async def assets_list(tag: str | None = None, q: str | None = None, tags: str | None = None):
    picked = [t.strip() for t in (tags or "").split(",") if t.strip()]
    if (q and q.strip()) or picked:
        def work():
            c = db.connect()
            try:
                return library.search(c, q or "", tag=tag or None, tags=picked or None)
            finally:
                c.close()
        return {"shots": await asyncio.to_thread(work)}
    rows = assets.list_assets(S.conn, tag=tag or None, limit=200)
    return {"assets": [{k: r.get(k) for k in ("id", "title", "uploader", "duration", "width", "height", "state", "tags",
                                              "has_subtitles", "has_logo", "drive_path", "source_url", "credit", "created_at",
                                              *asset_identity.FIELDS, "identity_manual")}
                       for r in rows]}


@app.get("/api/assets/tag_groups")
async def asset_tag_groups():
    return {"groups": assets.tag_groups(S.conn), "main_tags": list(assets.MAIN_TAGS)}


@app.put("/api/assets/{aid}/shots/{idx}/main")
async def asset_shot_main(aid: int, idx: int, request: Request):
    body = await request.json()
    main = body.get("main")
    if main is not None and not (isinstance(main, list) and all(isinstance(t, str) for t in main)):
        raise HTTPException(400, "main 要是字符串数组")
    try:
        shot = await library.set_main(S.conn, aid, idx, main)
    except KeyError:
        raise HTTPException(404, "没有这个镜头")
    except ValueError as e:
        raise HTTPException(400, str(e))
    shot.pop("phash", None)
    return shot


@app.put("/api/assets/{aid}/shots/{idx}/unusable")
async def asset_shot_unusable(aid: int, idx: int, request: Request):
    body = await request.json()
    if not isinstance(body.get("unusable"), bool):
        raise HTTPException(400, "unusable 要是 true 或 false")
    try:
        shot = assets.set_unusable(S.conn, aid, idx, body["unusable"])
    except KeyError:
        raise HTTPException(404, "没有这个镜头")
    broker.publish("*", "data", {"kind": "assets", "asset_id": aid})
    shot.pop("phash", None)
    return shot


@app.put("/api/assets/{aid}/identity")
async def asset_identity_set(aid: int, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "要传 JSON 对象")
    try:
        ident = asset_identity.set_manual(S.conn, aid, body)
    except KeyError:
        raise HTTPException(404, "没有这个素材")
    except ValueError as e:
        raise HTTPException(400, str(e))
    broker.publish("*", "data", {"kind": "assets", "asset_id": aid})
    return ident


@app.get("/api/assets/{aid}")
async def asset_card(aid: int):
    try:
        card = assets.card(S.conn, aid)
    except (KeyError, ValueError):
        raise HTTPException(404, "没有这个素材")
    if not card:
        raise HTTPException(404, "没有这个素材")
    for s in card.get("shots", []):
        s.pop("phash", None)
        s.pop("clip_vec", None)
    return card


@app.get("/api/assets/{aid}/thumb")
async def asset_thumb(aid: int):
    row = S.conn.execute("SELECT thumbs FROM assets WHERE id=?", (aid,)).fetchone()
    thumbs = json.loads(row["thumbs"]) if row and row["thumbs"] else {}
    return FileResponse(_library_file(thumbs.get("thumb")))


@app.get("/api/assets/{aid}/shots/{idx}")
async def asset_shot(aid: int, idx: int):
    row = S.conn.execute("SELECT keyframe FROM asset_shots WHERE asset_id=? AND idx=?", (aid, idx)).fetchone()
    return FileResponse(_library_file(row["keyframe"] if row else None))


@app.post("/api/downloads")
async def downloads_add(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        did = download.enqueue(S.conn, url, body.get("episode_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": did}


@app.get("/api/downloads")
async def downloads_list():
    return {"items": download.items(S.conn, 20)}


@app.post("/api/assets/{aid}/process")
async def asset_process(aid: int):
    library.schedule(S.conn, aid)
    return {"ok": True}


@app.post("/api/assets/{aid}/retag")
async def asset_retag(aid: int, engine: str = "local"):
    if not S.conn.execute("SELECT 1 FROM assets WHERE id=? AND state='ready'", (aid,)).fetchone():
        raise HTTPException(404, "没有这个素材或还没入库完")
    try:
        library.schedule_retag(S.conn, aid, engine)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "engine": engine}


@app.get("/api/accounts")
async def accounts():
    return {"bilibili": bilibili.status(), "drive": {"configured": drive.configured()},
            **{site: social_login.status(site) for site in social_login.SITES}}


@app.get("/api/system/permissions")
async def system_permissions():
    try:
        return await asyncio.to_thread(macui.permissions)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/system/permissions/request")
async def system_permissions_request():
    """让系统把 F1 Harness 加进“辅助功能”“屏幕录制”列表，并打开还没开的那一页设置。"""
    try:
        got = await asyncio.to_thread(macui.permissions, True)
    except Exception as e:
        raise HTTPException(500, str(e))
    for kind in ("accessibility", "screen"):
        if not got[kind]:
            macui.open_settings(kind)
            break
    return got


@app.post("/api/bilibili/qr")
async def bili_qr():
    try:
        qr = await bilibili.qr_start()
    except Exception as e:
        raise HTTPException(502, f"B 站二维码获取失败：{e}")
    # svg_inline 不带 xmlns，放进 <img> 显示不出来；改用 PNG data URI，也不把外部内容当 HTML 插入页面
    img = segno.make(qr["url"], error="m").png_data_uri(scale=6, border=2)
    return {"key": qr["qrcode_key"], "img": img}


@app.get("/api/bilibili/qr/{key}")
async def bili_poll(key: str):
    try:
        return await bilibili.qr_poll(key)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/bilibili/logout")
async def bili_logout():
    bilibili.logout()
    return {"ok": True}


def _social_site(site: str) -> str:
    if site not in social_login.SITES:
        raise HTTPException(404, "不支持的网站")
    return site


@app.post("/api/social/{site}/login")
async def social_start(site: str):
    """打开本机 Chrome 的登录页，后台每 2 秒看一次 cookie，登上后自动关窗口。"""
    try:
        return await social_login.start(_social_site(site),
                                        on_done=lambda: broker.publish("*", "data", {"kind": "accounts"}))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/api/social/{site}/status")
async def social_status(site: str):
    return social_login.status(_social_site(site))


@app.post("/api/social/{site}/logout")
async def social_logout(site: str):
    social_login.logout(_social_site(site))
    return {"ok": True}


_drive_auth: dict = {}


@app.post("/api/drive/authorize")
async def drive_authorize():
    task = _drive_auth.get("task")
    if task and not task.done():
        return {"started": False}

    async def run():
        _drive_auth["result"] = await drive.authorize()
        broker.publish("*", "data", {"kind": "accounts"})
    _drive_auth.update(task=asyncio.create_task(run()), result=None)
    return {"started": True}


@app.get("/api/drive/status")
async def drive_status():
    task = _drive_auth.get("task")
    info = {"configured": drive.configured(), "authorizing": bool(task and not task.done()),
            "last": _drive_auth.get("result")}
    if info["configured"]:
        try:
            info["usage"] = await drive.usage()
        except Exception as e:
            info["usage_error"] = str(e)[:200]
    return info


@app.post("/api/episodes/{eid}/archive/extend")
async def archive_extend(eid: int):
    try:
        return {"delete_after": drive.extend(S.conn, eid)}
    except drive.DriveError as e:
        raise HTTPException(409, str(e))


@app.post("/api/uses/{use_id}/{decision}")
async def use_decision(use_id: int, decision: str):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    row = S.conn.execute("SELECT episode_id FROM asset_uses WHERE id=? AND approval='pending'", (use_id,)).fetchone()
    if not row:
        raise HTTPException(409, "这条复用已经处理过")
    S.conn.execute("UPDATE asset_uses SET approval=? WHERE id=?", ("approved" if decision == "approve" else "rejected", use_id))
    broker.publish("*", "refresh", {"episode_id": row["episode_id"]})
    return {"ok": True}


def _day_bounds_utc(day: datetime) -> tuple[str, str]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds"), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")


@app.get("/api/topbar")
async def topbar(session: int | None = None):
    today = _day_bounds_utc(datetime.now(BEIJING))
    rl = S.app_server.rate_limits if S.app_server else None
    quota = None
    if rl and rl.get("primary"):
        p = rl["primary"]
        reset = datetime.fromtimestamp(p["resetsAt"], BEIJING) if p.get("resetsAt") else None
        quota = {"remaining": 100 - p["usedPercent"], "window_days": round((p.get("windowDurationMins") or 0) / 1440),
                 "resets": f"{reset.month} 月 {reset.day} 日" if reset else None}
    return {
        "quota": quota,
        "session": chat.usage_summary(S.conn, "episode_id=?", (session,)) if session else None,
        "today": chat.usage_summary(S.conn, "ts >= ? AND ts < ?", today),
        "fx": pricing.fx(S.conn),
    }


@app.get("/api/usage")
async def usage(day: str | None = None, period: str = "day"):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BEIJING) if day else datetime.now(BEIJING)
    start_day = {"day": d, "week": d - timedelta(days=d.weekday()), "month": d.replace(day=1)}.get(period, d)
    lo = _day_bounds_utc(start_day)[0]
    hi = _day_bounds_utc(d)[1]
    rows = S.conn.execute("SELECT * FROM usage WHERE ts >= ? AND ts < ?", (lo, hi)).fetchall()
    hours = [{"hour": h, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "usd": 0.0} for h in range(24)]
    for r in rows:
        h = datetime.fromisoformat(r["ts"]).astimezone(BEIJING).hour
        b = hours[h]
        b["input"] += r["input_tokens"]; b["cached"] += r["cached_tokens"]
        b["output"] += r["output_tokens"]; b["reasoning"] += r["reasoning_tokens"]
        b["usd"] += pricing.cost_usd(S.conn, r["model"], r["input_tokens"], r["cached_tokens"], r["cache_write_tokens"], r["output_tokens"]) or 0
    return {"hours": hours, "total": chat.usage_summary(S.conn, "ts >= ? AND ts < ?", (lo, hi)), "fx": pricing.fx(S.conn)}


@app.get("/api/stream")
async def stream(request: Request, session: int | None = None):
    channel = f"s{session}" if session else "*"
    q = broker.subscribe(channel)
    q_all = broker.subscribe("*") if session else None

    async def gen():
        waiters: list[asyncio.Task] = []
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                waiters = [asyncio.create_task(q.get())] + ([asyncio.create_task(q_all.get())] if q_all else [])
                done, pending = await asyncio.wait(waiters, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                if not done:
                    yield ": keepalive\n\n"
                for d in done:
                    item = d.result()
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
        finally:
            for w in waiters:
                w.cancel()
            broker.unsubscribe(channel, q)
            if q_all:
                broker.unsubscribe("*", q_all)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def main() -> None:
    import uvicorn
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=config.LOG_DIR / "server.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
