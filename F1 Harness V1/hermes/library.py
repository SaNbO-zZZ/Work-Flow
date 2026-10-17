"""素材库接入：下载完在后台入库、建画面指纹、长镜头切细、算以文搜图向量、本机打标；混合检索（标签过滤 + 画面语义排序）。"""
import asyncio
import json
import logging

from . import asset_identity, assets, db, fingerprint, local_tags, visual_search
from .codex_runner import QuotaExhausted
from .events import broker

log = logging.getLogger("hermes.library")
_lock = asyncio.Lock()          # 指纹和向量都吃 CPU/GPU，一次只处理一个素材
_tasks: set[asyncio.Task] = set()
TAG_LIST_LIMIT = 300            # 只按标签筛、不写查询语句时最多列出的镜头数


def _in_thread(fn, *args, **kw):
    # sqlite 连接不能跨线程，线程里自己开
    def work():
        conn = db.connect()
        try:
            return fn(conn, *args, **kw)
        finally:
            conn.close()
    return asyncio.to_thread(work)


async def ingest_and_process(conn, path, **kw) -> int:
    """给 download.worker 用的 ingest：入库放线程里跑，按标题记下哪站哪年，之后的处理挂到后台。"""
    asset_id = await _in_thread(assets.ingest, path, **kw)
    asset_identity.apply(conn, asset_id)
    schedule(conn, asset_id)
    return asset_id


def schedule(conn, asset_id: int) -> None:
    task = asyncio.create_task(process(conn, asset_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def gpt_tagging(conn) -> bool:
    """入库自动打标默认走本机（不花 token）；设置项 assets.gpt_tagging=1 才改用 GPT。"""
    row = conn.execute("SELECT value FROM settings WHERE key='assets.gpt_tagging'").fetchone()
    return bool(row and row[0] == "1")


def needs(conn, asset_id: int) -> dict:
    row = conn.execute(
        """SELECT SUM(phash IS NULL) np, SUM(clip_vec IS NULL AND keyframe IS NOT NULL) nc
           FROM asset_shots WHERE asset_id=?""", (asset_id,)).fetchone()
    tagged = conn.execute("SELECT tagged_at FROM assets WHERE id=?", (asset_id,)).fetchone()[0]
    return {"fingerprint": bool(row["np"]), "clip": bool(row["nc"]), "tags": tagged is None}


async def process(conn, asset_id: int) -> dict:
    done = {}
    async with _lock:
        todo = needs(conn, asset_id)
        try:
            if todo["fingerprint"]:
                done["fingerprint"] = await _in_thread(fingerprint.index_asset, asset_id)
                # 长镜头按画面切细要用指纹；新段没向量，紧接着的以文搜图会补上
                done["split"] = await _in_thread(assets.split_long_shots, asset_id)
                todo["clip"] = todo["clip"] or bool(done["split"])
            if todo["clip"]:
                done["clip"] = await _in_thread(visual_search.index_asset, asset_id)
            if todo["tags"]:
                done["tags"] = await _tag(conn, asset_id, "gpt" if gpt_tagging(conn) else "local")
        except QuotaExhausted:
            log.warning("素材 %s 打标时额度用完，稍后补做", asset_id)
        except Exception:
            log.exception("素材 %s 后处理失败", asset_id)
    broker.publish("*", "data", {"kind": "assets", "asset_id": asset_id})
    return done


async def _tag(conn, asset_id: int, engine: str) -> str:
    if engine == "gpt":
        await assets.tag_asset(conn, asset_id)
    else:
        await _in_thread(local_tags.tag_asset, asset_id)
    return engine


async def retag(conn, asset_id: int, engine: str = "local") -> dict:
    """手动重新打标：local 本机，gpt 花 token。和入库处理排同一个队，失败只记日志。"""
    done = {"asset_id": asset_id, "engine": engine, "ok": False}
    async with _lock:
        try:
            await _tag(conn, asset_id, engine)
            done["ok"] = True
        except QuotaExhausted:
            log.warning("素材 %s 用 GPT 重新打标时额度用完", asset_id)
        except Exception:
            log.exception("素材 %s 重新打标失败", asset_id)
    broker.publish("*", "data", {"kind": "assets", "asset_id": asset_id})
    return done


def schedule_retag(conn, asset_id: int, engine: str) -> None:
    if engine not in ("local", "gpt"):
        raise ValueError("engine 只能是 local 或 gpt")
    task = asyncio.create_task(retag(conn, asset_id, engine))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def set_main(conn, asset_id: int, idx: int, main: list[str] | None) -> dict:
    """用户改/加主标签，并把手改结果并进原型（下次本机打标生效）。"""
    shot = assets.set_main_manual(conn, asset_id, idx, main)
    try:
        await _in_thread(local_tags.learn)
    except Exception:
        log.exception("手改主标签后重算原型失败")
    broker.publish("*", "data", {"kind": "assets", "asset_id": asset_id})
    return shot


def pending(conn) -> list[int]:
    """重启或额度恢复后需要补做的素材。"""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT a.id FROM assets a LEFT JOIN asset_shots s ON s.asset_id = a.id
           WHERE a.state='ready' AND (s.phash IS NULL OR (a.tagged_at IS NULL AND s.keyframe IS NOT NULL)
                 OR (s.clip_vec IS NULL AND s.keyframe IS NOT NULL)) ORDER BY a.id""")]


def _shot_tags(raw) -> list[str]:
    try:
        tags = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(tags, dict):
        out = []
        for v in tags.values():
            out += v if isinstance(v, list) else ([v] if v else [])
        return [str(t) for t in out]
    return [str(t) for t in tags or []]


def search(conn, query: str = "", *, tag: str | None = None, tags: list[str] | None = None,
           limit: int = 40) -> list[dict]:
    """tag 按整条素材圈定，tags 按镜头组合筛选（同一镜头要全带）；有查询语句时按画面语义排序，
    镜头标签里直接出现查询词的加分；只有 tags 没有查询语句时按素材、镜头顺序列出。"""
    ids = [a["id"] for a in assets.list_assets(conn, tag=tag, limit=10000)] if tag else None
    if ids == []:
        return []
    query = query.strip()
    picked = assets.filter_shots(conn, tags, limit=None if query else TAG_LIST_LIMIT) if tags else None
    if not query:
        return picked or []
    if picked == []:
        return []
    hits = visual_search.search(conn, query, limit=limit * 3, asset_ids=ids,
                                shot_ids=[s["id"] for s in picked] if picked is not None else None)
    for h in hits:
        if any(query in t or t in query for t in _shot_tags(h.get("tags")) if len(t) >= 2):
            h["score"] += 0.1
            h["tag_hit"] = True
    hits.sort(key=lambda h: h["score"], reverse=True)
    titles = {r["id"]: r["title"] for r in conn.execute(
        f"SELECT id, title FROM assets WHERE id IN ({','.join('?' * len(hits)) or 'NULL'})",
        [h["asset_id"] for h in hits])}
    return [h | {"title": titles.get(h["asset_id"])} for h in hits[:limit]]
