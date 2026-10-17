"""AI 封面（用户 2026-10-16 改定，照原 Codex F1 智能体的做法）：
模型写封面文字、定主角 → 以文搜图从本期镜头和整个素材库找候选 → 模型看编号拼图给每个主角挑一张真人参考帧
→ 图像模型按参考帧画一张不带字的“米白拼贴”底图（横版 4:3、竖版 3:4 各一张）→ 程序排字、加底栏和右上角圆标（collage）
→ 模型看成品验收（是不是本人、有没有多余的字、字区干净不干净），不过就带着原因重画，最多再画 2 次。

参考封面在 assets/封面参考/（用户给的 6 张样张），只学风格。没做成的方向保留模板封面。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import codex_runner, collage, config, db

log = logging.getLogger("hermes.aicover")

REF_DIR = config.ROOT / "assets" / "封面参考"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
RETRIES = 2                 # 验收不过时最多再画几次
TIMELINE_MAX = 40           # 拼图里本期镜头最多几格
SEARCH_PER_SUBJECT = 12     # 每个主角、场景从素材库搜几格
CODEX_TIMEOUT = 1200
TILE = (480, 270)
SHAPE = {"landscape": ("横版 4:3", "2048×1536"), "portrait": ("竖版 3:4", "1536×2048")}
# 人脸位置照原智能体：横版右侧 47%、竖版下半部
FACES = {
    "landscape": {1: ["脸部中心约在 x=76%、y=44%，肩膀一直延伸到底边"],
                  2: ["脸部中心约在 x=67%、y=48%", "在稍后、稍高的位置，脸部中心约在 x=86%、y=40%"]},
    "portrait": {1: ["脸部中心约在 x=57%、y=65%，肩膀一直延伸到底边"],
                 2: ["脸部中心约在 x=35%、y=66%", "脸部中心约在 x=74%、y=65%"]},
}
AREA = {
    "landscape": "左侧 53% 从上到下都是安静的米白纸面，留给大标题：不能有人脸、深色墨迹、刷痕伸进来。主角放在右侧 47%。",
    "portrait": "上部 43% 完全是安静的米白纸面，留给大标题：不能有人脸、深色墨迹。主角放在下半部，头顶从 y=43% 以下开始。",
}

COPY_INSTRUCTION = """你在给一期 F1 中文短视频写封面文字、定封面主角。
类型：{branch}；大奖赛：{gp}
发布标题：{title}
简介：{desc}
{note}封面是“米白拼贴”风格：左上是大标题，旁边是主角的真人头肩像。要求：
- topic：小标签里的话题，2–6 个字（如“红牛官宣”“奥斯汀前瞻”“新加坡复盘”）；
- lines：主标题 2 行，每行不超过 6 个字（数字、英文两个算一个字）。第 1 行通常是主角，第 2 行是最抓人的结论，例如 ["梅基斯", "留到2031！"]、["小Kimi", "首个冠军赛点"]；
- accent：哪一行用车队色，通常是 1（第 2 行）；
- sub：副标题 0–1 行，不超过 14 个字，补关键信息，例如 ["红牛CEO兼领队续约"]、["10月23—25日 / 美洲赛道"]；
- summary：{summary_rule}
- team：主角所属车队，从 {teams} 里选一个，没有就写 null；
- subjects：封面上放的主角 1–2 个，优先真人（车手、领队）。每个写 name（中文名）、kind（person 或 car）、query（以文搜图用的中文描述，要能搜到这个人清楚的正脸，例如“梅基斯 采访 正脸特写”）；
- scene：背景氛围用的场景（赛道建筑、维修区等）的搜图描述，没有就写 null。
只用标题和简介里有的事实，不要编。只输出 JSON：
{{"topic": "", "lines": [], "accent": 1, "sub": [], "summary": null, "team": null, "subjects": [], "scene": null}}"""

SUMMARY_RULE = {True: '本期概要 {"head": "两件主要的事，用 · 隔开，不超过 16 个字", "points": ["本期讲的 3 个板块，每个 2–4 个字"]}',
                False: "写 null"}

PICK_INSTRUCTION = """你在给一期 F1 中文短视频的封面挑参考帧。附图是候选画面的编号拼图，每格左上角是编号。
发布标题：{title}
封面主角：{subjects}
背景场景：{scene}
{note}要看图确认，不要只凭编号来源猜：
- 每个主角挑 1 格做长相参考：确认是这个人本人；脸大、清楚、基本正面或微侧、不糊、没被头盔或别人挡住。赛车主角挑整车清楚、涂装认得出的一格。找不到就写 null，不要凑；
- 场景挑 1 格做背景氛围参考，没有合适的写 null。
只输出 JSON：{{"refs": {{"主角名": 编号或 null}}, "scene": 编号或 null, "reason": "一句话说明每格是谁、为什么选"}}"""

GEN_INSTRUCTION = """你是 F1 中文短视频频道的封面设计师。用图像生成工具画一张{shape}的封面底图（{size}）。
标题、底栏文字之后由程序另外排上去，所以这张底图上不能有任何文字。
附图：
{refs}
风格：高级的编辑部拼贴（editorial collage）。暖米白纸张底纹、炭黑墨迹、{team_color}的粗犷刷痕和撕纸毛边；主角是照片质感的大幅头肩抠像，带一点向内的朝向，神情专注自然。
构图：{area}
{faces}
头部完整、四周留点空；底部 12% 保持平静，留给底栏；右上角约 12% 见方保持干净，留给频道圆标。
硬性要求：
- 长相必须忠于对应的参考帧：脸型、五官、发型、眼镜、肤色和照片质感都照参考帧，不要卡通化，不要美颜换脸；不要加参考帧里没有的人。
- 衣服用纯色队服风格，可以带车队色细条；衣服上不要字母、数字、赞助商标。
- 整张图不要任何文字、字母、数字、logo、徽章、水印（NO TEXT, NO LETTERS, NO NUMBERS, NO LOGOS, NO WATERMARK）。
- 这是编辑拼贴，不是合影或活动现场照：不要奖杯、领奖台、麦克风、合同；不要画赛道走向图。
{note}{fix}生成后把图片复制到当前目录，命名 {name}.png，只回复文件名。"""

CHECK_INSTRUCTION = """你在验收一张 F1 中文短视频封面（{shape}）。第 1 张是成品封面，后面是主角的真实参考帧：{refs}。
逐条看图核对：
1. 封面上的人物和参考帧是同一个人，一眼认得出（脸型、发型、眼镜等）；没有多出来的人；脸和手没有畸形；
2. 除了程序排的中文标题、底栏文字和右上角频道圆标，画面里没有别的文字、字母、数字、logo、水印（衣服、背景都算；赛车涂装上的小标可以接受）；
3. 标题文字底下干净，字清楚好读；人脸没被标题、底栏、圆标挡住，头部完整没被裁掉；
4. 整体是米白纸面、车队色刷痕、真人头肩拼贴的风格，不像视频截图。
只输出 JSON：{{"ok": true 或 false, "why": "不过时说清哪条、哪里不对，以及重画时该怎么改"}}"""


class CoverError(RuntimeError):
    pass


# ---------- 文字 ----------

def make_copy(branch: str | None, gp: str | None, publish_title: str, data: dict) -> collage.Copy:
    """模型给的封面文字转成 collage.Copy；缺主标题就按发布标题排。"""
    branch = branch or "资讯"
    base = collage.copy_from_title(branch, publish_title, gp)
    lines = [str(x).strip() for x in data.get("lines") or [] if str(x).strip()][:3]
    if not lines:
        return base
    team = data.get("team") if data.get("team") in collage.TEAM_ACCENT else base.team
    summary = data.get("summary") if branch in collage.SUMMARY_BRANCHES else None
    if not (isinstance(summary, dict) and summary.get("head")):
        summary = base.summary if branch in collage.SUMMARY_BRANCHES else None
    accent = data.get("accent")
    return collage.Copy(
        label=collage.label_for(branch, str(data.get("topic") or "").strip()), lines=lines,
        accent=accent if isinstance(accent, int) and -len(lines) <= accent < len(lines) else -1,
        sub=[str(x).strip() for x in data.get("sub") or [] if str(x).strip()][:2],
        summary=summary, date=collage.today(), team=team, branch=branch)


def copy_dict(c: collage.Copy) -> dict:
    return {k: getattr(c, k) for k in ("label", "lines", "accent", "sub", "summary", "date", "team", "branch")}


# ---------- 候选画面 ----------

def _source(asset_id: int) -> Path:
    return config.LIBRARY_DIR / str(asset_id) / "source.mp4"


def candidates(conn, ep_dir: Path, subjects: list[dict], *, search=None) -> list[dict]:
    """本期成片用到的镜头（取实际用到那段的中点）在前，再按每个主体从整个素材库搜；只收本地有原片的。"""
    out, seen = [], set()
    tl_path = ep_dir / "06_合成" / "时间线.json"
    tl = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.exists() else {}
    for c in tl.get("video", []):
        key = (c["asset_id"], c.get("shot_idx"))
        if key in seen or not _source(c["asset_id"]).exists():
            continue
        seen.add(key)
        out.append({"asset_id": c["asset_id"], "shot_idx": c.get("shot_idx"),
                    "t": (c["src_in"] + c["src_out"]) / 2, "from": "本期"})
        if len(out) >= TIMELINE_MAX:
            break
    if search is None:
        from .visual_search import search
    for s in subjects:
        if not s.get("query"):
            continue
        for r in search(conn, s["query"], limit=SEARCH_PER_SUBJECT * 2):
            key = (r["asset_id"], r["shot_idx"])
            if key in seen or not _source(r["asset_id"]).exists():
                continue
            seen.add(key)
            out.append({"asset_id": r["asset_id"], "shot_idx": r["shot_idx"],
                        "t": (r["start_s"] + r["end_s"]) / 2, "from": f"素材库·{s['name']}"})
            if sum(1 for o in out if o["from"].endswith(s["name"])) >= SEARCH_PER_SUBJECT:
                break
    for i, o in enumerate(out, 1):
        o["n"] = i
    return out


def grab(item: dict, out: Path, width: int | None = None) -> Path:
    vf = ["-vf", f"scale={width}:-2"] if width else []
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-ss", f"{item['t']:.2f}", "-i", str(_source(item["asset_id"])),
                    "-frames:v", "1", *vf, str(out)], check=True, timeout=120)
    return out


def contact_sheet(items: list[dict], work: Path, cols: int = 5) -> Path:
    W, H = TILE
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (W * cols, H * rows))
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
    d = ImageDraw.Draw(sheet)
    tmp = work / "_tile.jpg"
    for i, it in enumerate(items):
        try:
            grab(it, tmp, width=W)
            im = Image.open(tmp).convert("RGB")
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("候选帧取不出来 %s：%s", it, e)
            continue
        im.thumbnail((W, H))
        x, y = (i % cols) * W, (i // cols) * H
        sheet.paste(im, (x, y))
        d.rectangle([x, y, x + 64, y + 44], fill="black")
        d.text((x + 5, y + 2), str(it["n"]), fill="yellow", font=font)
    tmp.unlink(missing_ok=True)
    out = work / "候选拼图.jpg"
    sheet.save(out, quality=85)
    return out


# ---------- 生成 ----------

def _json(text: str) -> dict:
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError as e:
        raise CoverError(f"模型没按格式回复：{text[:200]}") from e


def _generated(work: Path, name: str, thread_id: str | None) -> Path | None:
    p = work / f"{name}.png"
    if p.exists():
        return p
    if thread_id:   # 模型没复制过来时去 Codex 的生成目录找
        hits = sorted((Path.home() / ".codex" / "generated_images" / thread_id).glob("*.png"),
                      key=lambda x: x.stat().st_mtime)
        if hits:
            return Path(shutil.copyfile(hits[-1], p))
    return None




def style_ref(orientation: str, branch: str | None) -> Path | None:
    """按方向和类型挑一张样张做风格参考：前瞻、复盘用带“本期概要”的那张。"""
    side = "横版" if orientation == "landscape" else "竖版"
    names = [f"{side}-概要.jpg", f"{side}-资讯-1.jpg"] if branch in collage.SUMMARY_BRANCHES \
        else [f"{side}-资讯-1.jpg", f"{side}-概要.jpg"]
    return next((REF_DIR / n for n in names if (REF_DIR / n).exists()), None)


def _db_path(conn) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def _candidates_in_thread(path: Path | None, conn, ep_dir: Path, subjects: list[dict], search=None) -> list[dict]:
    """线程里要用自己的数据库连接（sqlite 连接不能跨线程，库路径在主线程先查好）；内存库（测试）才直接用传进来的。"""
    if path is None:
        return candidates(conn, ep_dir, subjects, search=search)
    c = db.connect(path)
    try:
        return candidates(c, ep_dir, subjects, search=search)
    finally:
        c.close()


# ---------- 主流程 ----------

async def generate(conn, ep, publish_title: str, desc: str, *, note: str | None = None,
                   runner=None, search=None) -> dict:
    """出横竖两张 AI 封面写到 06_合成/封面_横版.png、封面_竖版.png（旧的先存到 封面版本/<时间>/）。
    返回说明（封面文字、主角、参考帧、每个方向第几次通过），同时写 06_合成/封面说明.json。"""
    run = runner or codex_runner.run
    model, effort = config.ROLE_MODELS["图形"]
    ep_dir = Path(ep["folder"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = ep_dir / "06_合成" / "AI封面" / stamp
    work.mkdir(parents=True, exist_ok=True)
    note_line = f"用户对封面的意见（优先照办）：{note}\n" if note else ""
    branch = ep["branch"] or "资讯"

    async def ask(prompt: str, images: list[Path] | None = None, **kw):
        r = await run(prompt, model, effort, work, images=images, timeout=CODEX_TIMEOUT, **kw)
        codex_runner.record_usage(conn, r, model=model, effort=effort, episode_id=ep["id"])
        return r

    # 1. 封面文字和主角
    r = await ask(COPY_INSTRUCTION.format(
        branch=branch, gp=ep["gp_label"] or "无", title=publish_title, desc=desc or "无", note=note_line,
        summary_rule=SUMMARY_RULE[branch in collage.SUMMARY_BRANCHES],
        teams="、".join(collage.TEAM_ACCENT)), ephemeral=True)
    data = _json(r.message)
    copy = make_copy(branch, ep["gp_label"], publish_title, data)
    subjects = [{"name": str(s["name"]), "kind": s.get("kind") or "person", "query": s["query"]}
                for s in data.get("subjects") or [] if s.get("name") and s.get("query")][:2]
    if not subjects:
        raise CoverError("模型没给出封面主角")
    scene = data.get("scene") if isinstance(data.get("scene"), str) and data.get("scene").strip() else None

    # 2. 候选画面 → 挑参考帧
    queries = subjects + ([{"name": "场景", "query": scene}] if scene else [])
    items = await asyncio.to_thread(_candidates_in_thread, _db_path(conn), conn, ep_dir, queries, search)
    if not items:
        raise CoverError("找不到候选画面（本期镜头和素材库都没有本地原片）")
    sheet = await asyncio.to_thread(contact_sheet, items, work)
    r = await ask(PICK_INSTRUCTION.format(
        title=publish_title, note=note_line, scene=scene or "无",
        subjects="、".join(f"{s['name']}（{'赛车' if s['kind'] == 'car' else '真人'}）" for s in subjects)),
        images=[sheet], ephemeral=True)
    picked = _json(r.message)
    by_n = {it["n"]: it for it in items}
    refs_n = picked.get("refs") or {}
    people = [{**s, "item": by_n[refs_n[s["name"]]]} for s in subjects
              if isinstance(refs_n.get(s["name"]), int) and refs_n[s["name"]] in by_n]
    if not people:
        raise CoverError(f"拼图里没找到主角清楚的画面：{picked.get('reason') or refs_n}")
    for p in people:
        p["frame"] = await asyncio.to_thread(grab, p["item"], work / f"参考_{p['name']}.png")
    scene_item = by_n.get(picked.get("scene")) if isinstance(picked.get("scene"), int) else None
    scene_frame = await asyncio.to_thread(grab, scene_item, work / "参考_场景.png") if scene_item else None
    accent = "#%02X%02X%02X" % collage.accent_of(copy.team)
    team_color = f"{copy.team}车队色（{accent}）" if copy.team else f"红色（{accent}）"

    async def one(orient: str) -> dict:
        shape, size = SHAPE[orient]
        style = style_ref(orient, branch)
        images = [p["frame"] for p in people] + ([scene_frame] if scene_frame else []) + ([style] if style else [])
        lines = [f"- 第 {i} 张：{p['name']}的真实画面，{'只用来照这个人的长相' if p['kind'] != 'car' else '只用来照这台车的样子和涂装'}。"
                 for i, p in enumerate(people, 1)]
        if scene_frame:
            lines.append(f"- 第 {len(people) + 1} 张：场景参考，只取氛围，淡淡画在主角身后。")
        if style:
            lines.append(f"- 第 {len(images)} 张：风格样张，只学纸面、刷痕、拼贴的感觉和留白位置；不要用它的人物，"
                         "也不要画它上面的字和底栏（字是程序排的）。")
        pos = FACES[orient][len(people)]
        faces = "\n".join(f"- {p['name']}：{'头肩抠像，' if p['kind'] != 'car' else '整车抠图，'}{pos[i]}。"
                          for i, p in enumerate(people))
        tries, fix = [], ""
        for attempt in range(1 + RETRIES):
            name = f"{orient}_{attempt + 1}"
            prompt = GEN_INSTRUCTION.format(shape=shape, size=size, refs="\n".join(lines), team_color=team_color,
                                            area=AREA[orient], faces=faces, note=note_line, fix=fix, name=name)
            try:
                r = await ask(prompt, images=images)
            except codex_runner.QuotaExhausted:
                raise
            except codex_runner.CodexError as e:
                tries.append({"attempt": attempt + 1, "error": str(e)[:300]})
                continue
            raw = _generated(work, name, r.thread_id)
            if raw is None:
                tries.append({"attempt": attempt + 1, "error": "没找到生成的图片"})
                continue
            done = work / f"{name}_成品.png"

            def typeset():
                with Image.open(raw) as im:
                    bg = collage.fit_background(im, orient)
                img, meta = collage.compose(copy, bg, orient)
                return collage.save(img, {**meta, "background": "AI", "raw": str(raw)}, done)

            await asyncio.to_thread(typeset)
            try:
                r = await ask(CHECK_INSTRUCTION.format(shape=shape, refs="、".join(p["name"] for p in people)),
                              images=[done, *[p["frame"] for p in people]], ephemeral=True)
                chk = _json(r.message)
            except codex_runner.QuotaExhausted:
                raise
            except (codex_runner.CodexError, CoverError) as e:
                chk = {"ok": False, "why": f"验收没做成：{str(e)[:200]}"}
            ok = chk.get("ok") is True
            tries.append({"attempt": attempt + 1, "file": str(done), "ok": ok, "why": str(chk.get("why") or "")})
            if ok:
                return {"ok": True, "file": done, "tries": tries}
            fix = f"上一次验收没过，原因：{chk.get('why') or '未说明'}。这次照这个改。\n"
        return {"ok": False, "tries": tries}

    res = dict(zip(("landscape", "portrait"), await asyncio.gather(one("landscape"), one("portrait"))))
    out_dir = ep_dir / "06_合成"
    names = {"landscape": "封面_横版.png", "portrait": "封面_竖版.png"}
    backup = out_dir / "封面版本" / stamp
    for orient, r in res.items():
        if not r["ok"]:
            continue
        cur = out_dir / names[orient]
        if cur.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cur, backup / cur.name)
        shutil.copyfile(r["file"], cur)
    info = {
        "time": stamp, "note": note, "copy": copy_dict(copy), "subjects": subjects, "scene": scene,
        "picked": [{"name": p["name"], **{k: p["item"][k] for k in ("n", "asset_id", "shot_idx", "t", "from")}}
                   for p in people] + ([{"name": "场景", **{k: scene_item[k] for k in ("n", "asset_id", "shot_idx", "t", "from")}}]
                                       if scene_item else []),
        "reason": picked.get("reason", ""), "candidates": len(items), "work": str(work),
        "result": {o: {"ok": r["ok"], "attempts": len(r["tries"]), "tries": r["tries"]} for o, r in res.items()},
    }
    (out_dir / "封面说明.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    return info


def summary(info: dict) -> str:
    """给会话看的一段话。"""
    names = {"landscape": "横版", "portrait": "竖版"}
    parts = []
    for o, r in info["result"].items():
        if r["ok"]:
            parts.append(f"{names[o]}第 {r['attempts']} 次通过看图验收")
        else:
            last = next((t.get("why") or t.get("error") for t in reversed(r["tries"])), "")
            parts.append(f"{names[o]}画了 {r['attempts']} 次都没通过验收，保留原来的封面" + (f"（{last}）" if last else ""))
    pics = "、".join(f"{p.get('name', '')}用素材 {p['asset_id']}" + (f" 镜头 {p['shot_idx']}" if p.get("shot_idx") is not None else "")
                    for p in info["picked"])
    title = " / ".join((info.get("copy") or {}).get("lines") or [])
    return f"AI 封面：标题「{title}」；参考帧 {pics}。{info.get('reason') or ''} {'；'.join(parts)}。"
