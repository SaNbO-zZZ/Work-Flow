"""画面核对：剪辑决策排好时间线后，每个镜头按实际要用的那段抽开头、中间、结尾三帧拼图，交给模型看图判断
是不是这个画面段讲的对象、中间有没有切到别的画面。标签只作参考（2026-10-15 用户定：标签只是辅助，
要 AI 自己识别画面；期 18 粗剪里多车同框的冲刺全景标了法拉利、实际是梅奔，21#72 标红牛庆祝、后半是汉密尔顿车载）。
2026-10-16 用户定选镜头以看图为主、标签只作辅助：剪辑决策前先给候选镜头拼图（candidate_sheets）让模型看着选；
核对时顺带看出是不是赛道实况、关键回合、片头片尾或品牌画面、水印在哪个角，写进时间线给 QA 用（标签只在没看过时兜底）。"""
import asyncio
import hashlib
import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import codex_runner, config
from .assets import FFMPEG, _font, _run
from .news import parse_json

log = logging.getLogger(__name__)

FRAME = (480, 270)
LABEL_H = 40
PER_SHEET = 6        # 一张图几个镜头（一行一个镜头、三帧）
PICK_FRAME = (384, 216)   # 剪辑决策候选拼图：小一点，一张放 PICK_PER_SHEET 个镜头
PICK_PER_SHEET = 8
GRAB_WORKERS = 8
CORNERS = ("左上", "右上", "左下", "右下")
EDGE = 0.2           # 头尾帧离切点多远，避开切点上的黑场、残帧
TIMEOUT = 900

INSTRUCTION = """画面核对：下面每张图里一行是一个镜头，行首标着“编号”，左、中、右三帧是这个镜头实际要用的那一段的开头、中间、结尾。
每个镜头附了它所在画面段的口播、这一段讲的对象（车队、车手）和素材库标签。标签是以前程序或模型只看一帧打的，可能不准，只作参考，以你在图里看到的为准。

逐个镜头判断能不能用在它的画面段，下面任一条成立就给 ok=false：
1. 这一段有对象时，三帧里认不出对象：画面主体是别的车队的车、车手、车库，或者对象的车只是远处认不清的小点。多车同框时，对象的车要清楚可认（车身配色、车号、头盔、车队标识）；车载镜头看车头和方向盘认车队。
2. 三帧之间切到了别的画面（转场动画、换成另一辆车或另一个场景），切过去的部分不是这一段的对象。
3. 出现片头片尾、订阅提示、频道推荐、整屏成绩表或积分榜；或者品牌画面：车队和赞助商的联名标题卡、宣传片字卡（比如迈凯伦和 Gemini 的“70 Days”标题）、带车队或品牌商标的转场动画、整屏 logo；或者第三方水印、搬运账号标识（F1 官方标识、转播台标和计时图形不算）。
4. 口播点名讲某位车手（对象里括号写了车手）时，画面主体是他的队友（队友的车号、头盔、进站）或别人的采访、特写，也算不通过；同队两车同框、车库里的工程师和机械师可以。
口播讲的是具体事件时，素材不够允许用对象的普通画面，不要只因为不是事件那一刻就判不通过；但画面和口播明显矛盾的算不通过。
没有对象的画面段只按第 2、3 条判断。拿不准的给 ok=true，在 why 里写清楚哪里拿不准。

输出：每个镜头一条，n 是编号；seen 一句话写你在三帧里实际看到的（哪支车队、哪位车手、什么场景，中间有没有切换）；ok；why 写不通过的原因，通过时可以留空；
live：画面主体是赛道上的比赛实况（行驶、发车、安全车、红旗、事故，含车载）为 true，车库、维修区、人物、采访、航拍、图表为 false；
key：拍到发车、超车、轮对轮缠斗、事故、红旗这类关键回合为 true；
card：有第 3 条里的片头片尾或品牌画面为 true；
marks：第三方水印、搬运台标在哪些角（左上、右上、左下、右下），没有给空数组。"""

SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "seen": {"type": "string"}, "ok": {"type": "boolean"},
                       "why": {"type": "string"}, "live": {"type": "boolean"}, "key": {"type": "boolean"},
                       "card": {"type": "boolean"},
                       "marks": {"type": "array", "items": {"type": "string", "enum": list(CORNERS)}}},
        "required": ["n", "seen", "ok", "why", "live", "key", "card", "marks"], "additionalProperties": False}}},
    "required": ["clips"], "additionalProperties": False,
}


def clip_key(clip: dict, beat: dict | None) -> str:
    """同一镜头同一段原片放在同一个画面段（口播和对象一样）才算同一次核对。"""
    b = beat or {}
    sig = json.dumps([b.get("text") or "", b.get("teams") or [], b.get("drivers") or []], ensure_ascii=False)
    return (f"{clip['asset_id']}#{clip.get('shot_idx')}@{float(clip['src_in']):.2f}-{float(clip['src_out']):.2f}"
            f"|{hashlib.sha1(sig.encode()).hexdigest()[:10]}")


def frame_times(a: float, b: float) -> list[float]:
    edge = min(EDGE, (b - a) / 4)
    return [round(a + edge, 3), round((a + b) / 2, 3), round(b - edge, 3)]


def _grab(src: str, t: float, out: Path, width: int = FRAME[0]) -> Path | None:
    try:
        _run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-an", "-sn",
              "-vf", f"scale={width}:-2", "-q:v", "4", "-y", out])
    except RuntimeError as e:
        log.warning("画面核对抽帧失败：%s", e)
        return None
    return out if out.exists() else None


def make_sheets(items: list[tuple[int, dict, str]], out: Path, *, label=None, frame=FRAME, per_sheet=PER_SHEET,
                name="核对") -> list[Path]:
    """items: [(编号, 镜头, 原片路径)]，镜头要有 src_in/src_out；一行一个镜头三帧，per_sheet 行一张。
    label(编号, 镜头) 是行首文字，不给按时间线镜头写“编号 n 画面段 … 素材a#i”。"""
    raw = out / f"_帧_{name}"
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True)
    w, h = frame
    font = _font(26 if w >= FRAME[0] else 20)
    label = label or (lambda n, c: f"编号 {n}　画面段 {c.get('beat')}　素材{c['asset_id']}#{c.get('shot_idx')}")
    jobs = [(src, t, raw / f"{n}_{k}.jpg") for n, c, src in items
            for k, t in enumerate(frame_times(float(c["src_in"]), float(c["src_out"])))]
    with ThreadPoolExecutor(GRAB_WORKERS) as pool:
        got = dict(zip([j[2] for j in jobs], pool.map(lambda j: _grab(*j, width=w), jobs)))
    paths = []
    for s in range(0, len(items), per_sheet):
        chunk = items[s:s + per_sheet]
        sheet = Image.new("RGB", (3 * w, len(chunk) * (h + LABEL_H)), (40, 40, 40))
        draw = ImageDraw.Draw(sheet)
        for r, (n, clip, src) in enumerate(chunk):
            y = r * (h + LABEL_H)
            draw.text((8, y + 6), label(n, clip), fill="white", font=font)
            for k in range(3):
                p = got.get(raw / f"{n}_{k}.jpg")
                if not p:
                    continue
                with Image.open(p) as im:
                    im = ImageOps.contain(im.convert("RGB"), (w - 4, h - 4))
                sheet.paste(im, (k * w + (w - im.width) // 2, y + LABEL_H + (h - im.height) // 2))
        path = out / f"{name}_{s // per_sheet + 1:02d}.jpg"
        sheet.save(path, quality=85)
        paths.append(path)
    shutil.rmtree(raw, ignore_errors=True)
    return paths


def candidate_sheets(shots: list[dict], src: dict[int, str], out: Path) -> list[Path]:
    """剪辑决策用的候选镜头拼图：一行一个镜头，三帧取整个镜头（截到可用部分）的开头、中间、结尾，
    行首写“素材a#i 起-止 时长”，和提示词里的镜头清单对得上。src 是 _sources 查的原片（放线程里跑，不碰数据库）；
    找不到原片的镜头不画，一个都画不了返回空。"""
    out = Path(out)
    for old in out.glob("候选_*.jpg"):
        old.unlink()
    items = [(n, {**s, "src_in": s["start"], "src_out": s["end"]}, src[s["asset_id"]])
             for n, s in enumerate((s for s in shots if s["asset_id"] in src), start=1)]
    if not items:
        return []
    out.mkdir(parents=True, exist_ok=True)
    return make_sheets(items, out, frame=PICK_FRAME, per_sheet=PICK_PER_SHEET, name="候选",
                       label=lambda n, c: f"素材{c['asset_id']}#{c['shot_idx']}　{c['start']:.1f}-{c['end']:.1f}s"
                                          f"　{c['end'] - c['start']:.1f}s")


def _who(beat: dict | None) -> str:
    b = beat or {}
    if not b.get("teams"):
        return "无"
    return "、".join(b["teams"]) + (f"（{'、'.join(b['drivers'])}）" if b.get("drivers") else "")


def context(items: list[tuple[int, dict, str]], beats: dict, shots: dict) -> str:
    lines = []
    for n, clip, _ in items:
        bt = beats.get(clip.get("beat")) or {}
        s = shots.get((clip["asset_id"], clip.get("shot_idx"))) or {}
        tags = "、".join(str(x) for x in (s.get("teams") or []) + (s.get("drivers") or []) + (s.get("main") or [])
                        + [s.get("shot_type")] if x) or "无"
        lines.append(f"- 编号 {n}｜画面段 {clip.get('beat')}｜对象：{_who(bt)}｜口播：{(bt.get('text') or '').strip()}"
                     f"｜素材{clip['asset_id']}#{clip.get('shot_idx')}"
                     + (f" {s['session']}" if s.get("session") else "") + f" 标签：{tags}")
    return "\n".join(lines)


def _sources(conn, clips) -> dict[int, str]:
    ids = sorted({c["asset_id"] for c in clips})
    if not ids:
        return {}
    rows = conn.execute(f"SELECT id, local_path FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: r["local_path"] for r in rows if r["local_path"] and Path(r["local_path"]).exists()}


async def check(conn, timeline: dict, beats: list[dict], workdir: Path, *, shots: list[dict] | None = None,
                run=None, episode_id=None, task_id=None) -> dict:
    """看时间线里每个镜头；核对过的（同镜头同段原片同画面段）从 画面核对.json 取，不重看。
    返回 {"bad": [不通过的镜头 + 看图结论], "checked": 这次新看了几个, "error": 没做成的原因或 None,
    "looks": {clip_key: 看图结论}}；看图结论是 ok/seen/why/live/key/card/marks（旧缓存可能只有前三个）。"""
    out = Path(workdir) / "_画面核对"
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "画面核对.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    by_i = {b["i"]: b for b in beats}
    shot_of = {(s["asset_id"], s["shot_idx"]): s for s in shots or []}
    clips = timeline.get("video") or []
    src = _sources(conn, clips)
    todo, keys, missing = [], {}, set()
    for c in clips:
        k = keys[id(c)] = clip_key(c, by_i.get(c.get("beat")))
        if k in cache or any(k == key for _, _, key in todo):
            continue
        if c["asset_id"] not in src:
            missing.add(c["asset_id"])
            continue
        todo.append((c, src[c["asset_id"]], k))
    error = f"素材 {'、'.join(map(str, sorted(missing)))} 找不到原片，没看图" if missing else None
    checked = 0
    if todo:
        items = [(n, c, path) for n, (c, path, _) in enumerate(todo, start=1)]
        try:
            images = await asyncio.to_thread(make_sheets, items, out)
            prompt = f"{INSTRUCTION}\n\n# 镜头\n{context(items, by_i, shot_of)}"
            (out / "prompt.md").write_text(prompt, encoding="utf-8")
            schema = out / "画面核对.schema.json"
            schema.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
            model, effort = config.ROLE_MODELS["画面核对"]
            result = await (run or codex_runner.run)(prompt, model, effort, out, sandbox="read-only", ephemeral=True,
                                                     output_schema=schema, images=images, timeout=TIMEOUT)
            codex_runner.record_usage(conn, result, model=model, effort=effort, episode_id=episode_id, task_id=task_id)
            (out / "回复.json").write_text(result.message or "", encoding="utf-8")
            got = {int(v["n"]): v for v in parse_json(result.message or "").get("clips") or []}
        except Exception as e:   # 看图没做成不挡剪辑决策，交给 G2 人看
            log.warning("画面核对失败：%s", e)
            return {"bad": [], "checked": 0, "error": f"画面核对没做成：{e}", "looks": {}}
        for n, (c, _, k) in enumerate(todo, start=1):
            v = got.get(n)
            if v is None:
                continue
            cache[k] = {"ok": bool(v.get("ok")), "seen": str(v.get("seen") or ""), "why": str(v.get("why") or ""),
                        "live": bool(v.get("live")), "key": bool(v.get("key")), "card": bool(v.get("card")),
                        "marks": [m for m in CORNERS if m in (v.get("marks") or [])]}
            checked += 1
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [{**c, **cache[keys[id(c)]]} for c in clips if keys[id(c)] in cache and not cache[keys[id(c)]]["ok"]]
    looks = {keys[id(c)]: cache[keys[id(c)]] for c in clips if keys[id(c)] in cache}
    return {"bad": bad, "checked": checked, "error": error, "looks": looks}
