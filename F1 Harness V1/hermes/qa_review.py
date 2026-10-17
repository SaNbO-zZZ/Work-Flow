"""M6-5 独立 AI 评审：抽帧拼图 + 文字材料喂给新的 Codex 线程，按评分标准逐项打分、列问题清单。

既评流水线成片（有 ep_dir），也评没有工程目录的历史片（kind=校准）。总分由程序按权重重算。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import shutil
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import codex_runner, compose, config, db, facts, prompts
from .assets import FFMPEG, _font, _run, _timecode, detect_shots, probe, shot_layers
from .news import parse_json

RUBRIC_VERSION = "v1-2026-10-11"
KINDS = ("成片", "校准", "测试")
SEVERITY = ("高", "中", "低")
BACK_TO = ("写逐字稿", "D2找素材", "D6剪辑决策", "D4图形与叠层", "配音")
TIMEOUT = 1800

INTERVAL = 2.0          # 全片每 2 秒一帧
GRID_COLS, GRID_ROWS = 4, 4
CELL = (480, 270)
MAX_GRIDS = 8           # 全片拼图最多 8 张，片子更长就拉大间隔
INTRO_SECONDS, INTRO_STEP = 15.0, 0.5
INTRO_COLS = 6
INTRO_CELL = (320, 180)
LABEL_H = 36
SHORT_SHOT, LONG_SHOT = 1.2, 6.0

RUBRIC = (
    {"name": "事实准确", "weight": 26,
     "look": "口播里的数字、成绩、处罚、规则、历史、车手原话有没有依据（逐字稿的 [F#]/[S#] 对应事实表）；"
             "有没有说错、张冠李戴、把传闻说成定论；结论能不能从事实推出来。",
     "s60": "没发现明确错误，但有 1–2 处说法含糊或没有依据的判断（如\"大概率夺冠\"却不给理由），或个别非关键数字、年份在材料里核对不到。",
     "s80": "数字和关键说法都能在事实表里找到依据，传闻都交代了出处或用了\"据报道\"，结论有数据或事实撑着；最多 1 处措辞不够严谨。",
     "s100": "全部有据可查且准确，传闻和事实分得清清楚楚，用数据把结论讲透。",
     "cap": "有一处能确认的事实错误（车手、车队、赛道、名次、年份张冠李戴）本项不高于 50；把传闻说成定论不高于 40；编造数据不高于 20。"},
    {"name": "剪辑与节奏", "weight": 20,
     "look": "镜头数、平均镜头时长、最长镜头，有没有长时间静止或同类画面堆在一起，段落转换，切点跟不跟口播，语速（材料里的字/分）。"
             "参考：口播约 360 字/分；平均镜头 2–4 秒合适；超过 6 秒且画面里没有车在动、也没有叠层算\"长静态\"（科普的数据段除外）；短于 1.2 秒的闪切显得乱。",
     "s60": "平均镜头 4–6 秒，或有 2–3 处长静态、闪切；整体能看但有拖沓段，或开头 50 秒语速明显比正文慢。",
     "s80": "平均镜头 2–4 秒，最多 1 处略拖；切换跟着口播句子走，段落转换清楚，语速均匀。",
     "s100": "切点卡在口播重音和语义转折上，快慢有层次（信息段紧、名场面给足时间），全程没有让人想快进的地方。",
     "cap": "同一段画面重复出现、A-B-A 来回切，本项不高于 50。"},
    {"name": "开头吸引力", "weight": 17,
     "look": "前 5 秒有没有钩子（悬念、反差、名场面、直接抛出本期最大看点）；是不是直接进主题，有没有空镜、自我介绍、慢热铺垫；"
             "前 5 秒的画面是不是本期最好看的之一；求赞、求关注的位置是否符合本分支规定（资讯、科普开头不求赞）。",
     "s60": "前 5 秒说清了本期主题，但只是平铺直叙（如\"本周是某某大奖赛\"），画面普通；或钩子拖到 8–10 秒才出现。",
     "s80": "前 5 秒有一个具体的钩子（悬念、反差或名场面），画面和钩子对得上，5 秒内进入主题；求赞、求关注位置合规且不拖沓。",
     "s100": "第一句就让车迷想看下去，配上本期最有冲击力的画面，开场 15 秒里没有一句废话。",
     "cap": "前 5 秒是黑屏、片头动画或无关空镜，本项不高于 50；15 秒后才进主题不高于 40。"},
    {"name": "画面贴合度", "weight": 15,
     "look": "讲到哪位车手、哪支车队、哪个弯、哪个名场面时，画面里有没有对应的车、人、地点；讲天气、赛程、数据、官方文件、报道时有没有叠截图或图解；"
             "前瞻介绍赛道时有没有赛道简图；有没有和口播无关的凑数空镜、别家台标、素材自带字幕。",
     "s60": "大致对上话题（讲比赛就放比赛画面），但约一半点名的车手、车队、事件没有对应画面，或信息类口播没有叠层。",
     "s80": "点名的车手、车队、名场面八成以上有对应画面，信息类内容都有叠层，偶有 1–2 处泛用画面。",
     "s100": "每次点名都有对应画面，名场面用比赛实况，叠层清楚好读，画面本身也在讲故事。",
     "cap": "讲甲车手放乙车手、讲 A 赛道放 B 赛道这类明显错配，每处扣 10 分起。"},
    {"name": "文案可听性", "weight": 14,
     "look": "是不是写给耳朵听的口语；一个意思能不能一口气说完（不切成三四个字的碎句，也不长到喘不过气）；有没有书面腔（\"值得注意的是\"\"无疑\"\"进一步\"）、"
             "绕口词、同音歧义；译名和称呼是否统一；人设是不是懂行车迷跟朋友聊天，有态度不站队，吐槽克制好笑；结尾互动是否合规。",
     "s60": "意思能听懂，但书面腔或碎句有 3 处以上，信息堆砌像念稿，没有态度。",
     "s80": "口语自然、句长合适，书面腔最多 1 处，有观点，有一两处好笑的吐槽，转场顺。",
     "s100": "像资深车迷当面聊天，句句顺耳，信息密但不累，梗用得准，听完能记住两三个点。",
     "cap": "人身攻击、拿国籍、伤病、家人开玩笑，本项不高于 30；配音读错或怪停顿（见配音提醒）每处扣 3 分。"},
    {"name": "规格合规", "weight": 8,
     "look": "水印（左上角）、零标点字幕（每行约 16 字、不挡关键画面）、右下角出处小字、口播和 BGM 的平衡（整体约 −16 LUFS，真峰值不超过 −1 dBTP，口播压得住 BGM）、"
             "1920×1080 30 帧、时长在本分支范围内。",
     "s60": "硬检查过了，但有 2–3 处硬检查管不到的问题：字幕挡住关键信息、出处小字缺失或看不清、口播被 BGM 压、时长超出本分支范围、叠层字太小或停留太短读不完、图形互相遮挡。",
     "s80": "硬检查过了，看不出上面这些问题。",
     "s100": "图形、字幕、出处、声音都干净统一，像专业频道出品。",
     "cap": "没有硬检查结果（历史片）时按画面和响度数据自己判断：缺水印、字幕带标点、响度明显偏离，本项不高于 60。"},
)
WEIGHTS = {r["name"]: r["weight"] for r in RUBRIC}


def rubric_text() -> str:
    head = ("每项打 0–100 的整数，档与档之间按接近程度给（如 70）。\n"
            "目标是 100 分：每一项都拿“完美”去比，把离 100 分还差的每一处都找出来写进问题清单，给出能做到 100 分的改法。"
            "分数照实际质量给，不要往 60 或任何固定分数靠，也不要放水。\n"
            "60 分 = 能发但平庸的及格线：没有硬伤，但观众看完记不住、容易划走。\n"
            "80 分 = B 站 F1 中文区做得好的那一档。100 分 = 挑不出毛病，可以当本分支范本。\n"
            "只按材料里看得到、听得到的打分；材料缺了就按能看到的部分打，并在理由里写明缺了什么。硬检查已拦下的问题不重复扣分，硬检查的\"提醒\"可以作为扣分依据。\n"
            "总分 = Σ(各项分 × 权重) / 100。")
    parts = [head]
    for n, r in enumerate(RUBRIC, start=1):
        parts.append(f"{n}. {r['name']}（权重 {r['weight']}）\n看什么：{r['look']}\n"
                     f"- 60 分：{r['s60']}\n- 80 分：{r['s80']}\n- 100 分：{r['s100']}\n- 封顶：{r['cap']}")
    return "\n\n".join(parts)


INSTRUCTION = """你是独立的看片评审，没有参与这期视频的任何制作。上面的公共规范和分支规范是制作方必须遵守的要求，用来判断这条片子达不达标。按下面的评分标准给成片打分并列问题清单。

{images}

步骤：
1. 看开头拼图和前 15 秒字幕，评「开头吸引力」。
2. 对照字幕时间和拼图时间码，逐段看讲到的人、车、弯、事件有没有对应画面，评「画面贴合度」。
3. 看时间线摘要（镜头数、平均时长、最长镜头）、语速数据和拼图里画面变化的快慢，评「剪辑与节奏」。
4. 对照事实表核对逐字稿里的数字和说法，评「事实准确」。事实表里没有的说法按你的 F1 知识判断：确定是错的才算错误；拿不准的写进问题清单，严重程度标"低"。
5. 读去标签的逐字稿（没有就读字幕），评「文案可听性」。
6. 看硬检查、响度和画面里的水印、字幕、出处小字，评「规格合规」。

输出要求：
- scores：6 项各一条，score 是 0–100 的整数；reason 写打这个分的具体依据，引用时间点或原句，不要空泛。total 按权重算一下（程序会重算）。
- issues：问题清单，按严重程度从高到低，最多 15 条。at 写时间点 mm:ss（全片性的问题写"全片"）；item 写所属项；severity：高=观众会明显感到、事实错误或该打回，中=影响观感，低=可改可不改；problem 写问题；fix 写具体改法；back_to 写该打回到哪一步（写逐字稿、D2找素材、D6剪辑决策、D4图形与叠层、配音）。
- summary：一两句总评，点出最该改的一件事。
只输出符合 schema 的 JSON。

# 评分标准（{version}）
{rubric}"""

STR = {"type": "string"}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["scores", "total", "issues", "summary"],
    "properties": {
        "scores": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["item", "score", "reason"],
            "properties": {"item": {"type": "string", "enum": list(WEIGHTS)},
                           "score": {"type": "integer", "minimum": 0, "maximum": 100}, "reason": STR}}},
        "total": {"type": "number"},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["at", "item", "severity", "problem", "fix", "back_to"],
            "properties": {"at": STR, "item": {"type": "string", "enum": list(WEIGHTS)},
                           "severity": {"type": "string", "enum": list(SEVERITY)},
                           "problem": STR, "fix": STR, "back_to": {"type": "string", "enum": list(BACK_TO)}}}},
        "summary": STR,
    },
}


# ---------- 校验与算分 ----------

def _at_seconds(at: str) -> float | None:
    m = re.search(r"(\d+):(\d{1,2}(?:\.\d+)?)", at or "")
    return round(int(m.group(1)) * 60 + float(m.group(2)), 1) if m else None


def validate(data) -> dict:
    """6 项齐全、分数 0–100，总分按权重重算；不合格就抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("AI 评审输出不是 JSON 对象")
    scores = {}
    for s in data.get("scores") or []:
        name = (s or {}).get("item")
        if name not in WEIGHTS:
            raise ValueError(f"未知评分项：{name!r}")
        if name in scores:
            raise ValueError(f"评分项重复：{name}")
        v = s.get("score")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise ValueError(f"{name} 的分数不在 0–100：{v!r}")
        scores[name] = {"score": int(round(v)), "weight": WEIGHTS[name], "reason": str(s.get("reason") or "").strip()}
    missing = [k for k in WEIGHTS if k not in scores]
    if missing:
        raise ValueError("缺评分项：" + "、".join(missing))
    issues = []
    for i in data.get("issues") or []:
        i = i or {}
        if i.get("item") not in WEIGHTS or i.get("severity") not in SEVERITY or i.get("back_to") not in BACK_TO:
            raise ValueError(f"问题清单字段不合规：{i}")
        at = str(i.get("at") or "").strip()
        issues.append({"at": at, "at_s": _at_seconds(at), "item": i["item"], "severity": i["severity"],
                       "problem": str(i.get("problem") or "").strip(), "fix": str(i.get("fix") or "").strip(),
                       "back_to": i["back_to"]})
    issues.sort(key=lambda i: SEVERITY.index(i["severity"]))
    total = round(sum(scores[k]["score"] * w for k, w in WEIGHTS.items()) / 100, 1)
    model_total = data.get("total")
    return {"total": total, "model_total": model_total if isinstance(model_total, (int, float)) else None,
            "scores": {k: scores[k] for k in WEIGHTS}, "issues": issues,
            "summary": str(data.get("summary") or "").strip()}


def baseline(conn, branch: str) -> float | None:
    row = conn.execute("SELECT value FROM baselines WHERE branch=?", (branch,)).fetchone()
    return float(row["value"]) if row and row["value"] is not None else None


def below_baseline(conn, branch: str, total: float) -> tuple[float | None, bool | None]:
    base = baseline(conn, branch)
    return base, (None if base is None else total < base)


# ---------- 抽帧拼图 ----------

def _extract(video: Path, out: Path, prefix: str, start: float, length: float, step: float, width: int):
    _run([FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", video, "-an", "-sn",
          "-vf", f"fps=1/{step},scale={width}:-2", "-q:v", "4", "-y", out / f"{prefix}_%04d.jpg"])
    files = sorted(out.glob(f"{prefix}_*.jpg"))
    return [(round(start + k * step, 2), p) for k, p in enumerate(files) if k * step < length]


def _sheet(frames, cols: int, cell: tuple[int, int], path: Path) -> Path:
    w, h = cell
    rows = -(-len(frames) // cols)
    sheet = Image.new("RGB", (cols * w, rows * (h + LABEL_H)), (40, 40, 40))
    draw, font = ImageDraw.Draw(sheet), _font(24)
    for k, (t, p) in enumerate(frames):
        x, y = k % cols * w, k // cols * (h + LABEL_H)
        with Image.open(p) as im:
            im = ImageOps.contain(im.convert("RGB"), (w - 4, h - 4))
        sheet.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
        draw.text((x + 8, y + h + 4), _timecode(t), fill="white", font=font)
    sheet.save(path, quality=85)
    return path


def frame_step(duration: float) -> float:
    per = GRID_COLS * GRID_ROWS
    return max(INTERVAL, math.ceil(duration / (per * MAX_GRIDS) * 2) / 2)


def make_sheets(video: Path, look: Path, duration: float) -> tuple[list[Path], float]:
    """07_QA/看片/：开头 0–15 秒每 0.5 秒一帧一张；全片每 2 秒一帧，4×4 一张。"""
    shutil.rmtree(look, ignore_errors=True)
    raw = look / "_帧"
    raw.mkdir(parents=True)
    intro = _extract(video, raw, "intro", 0, min(INTRO_SECONDS, duration), INTRO_STEP, INTRO_CELL[0])
    paths = [_sheet(intro, INTRO_COLS, INTRO_CELL, look / "开头0-15秒.jpg")] if intro else []
    step = frame_step(duration)
    frames = _extract(video, raw, "all", 0, duration, step, CELL[0])
    per = GRID_COLS * GRID_ROWS
    for n, k in enumerate(range(0, len(frames), per), start=1):
        paths.append(_sheet(frames[k:k + per], GRID_COLS, CELL, look / f"全片_{n:02d}.jpg"))
    shutil.rmtree(raw, ignore_errors=True)
    return paths, step


# ---------- 文字材料 ----------

def parse_srt(text: str) -> list[tuple[float, float, str]]:
    def sec(s: str) -> float:
        h, m, rest = s.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.strip().splitlines()
        for k, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->")
                cues.append((sec(a), sec(b), "".join(lines[k + 1:]).strip()))
                break
    return cues


def _cue_lines(cues) -> str:
    return "\n".join(f"{_timecode(a)}–{_timecode(b)} {t}" for a, b, t in cues)


def speech_rate(cues) -> str:
    def rate(cs):
        n = sum(len(re.sub(r"\s", "", t)) for _, _, t in cs)
        span = cs[-1][1] - cs[0][0] if cs else 0
        return n / span * 60 if span > 0 else 0
    if not cues:
        return ""
    head = [c for c in cues if c[0] < 50]
    return f"全片约 {rate(cues):.0f} 字/分；开头 50 秒约 {rate(head):.0f} 字/分（参考 360 字/分，开头不能比正文慢）。"


def _durations_summary(spans: list[tuple[float, float]]) -> list[str]:
    if not spans:
        return ["没有镜头数据。"]
    durs = [b - a for a, b in spans]
    longest = max(range(len(spans)), key=lambda i: durs[i])
    short = sum(1 for d in durs if d < SHORT_SHOT)
    long_ = [f"{_timecode(a)}（{b - a:.1f} 秒）" for a, b in spans if b - a > LONG_SHOT]
    return [f"镜头数 {len(spans)}，平均 {sum(durs) / len(durs):.1f} 秒，最长 {durs[longest]:.1f} 秒"
            f"（{_timecode(spans[longest][0])} 起），短于 {SHORT_SHOT} 秒的 {short} 个。",
            f"超过 {LONG_SHOT:.0f} 秒的镜头：" + ("、".join(long_) if long_ else "无")]


def timeline_summary(conn, tl: dict) -> str:
    clips = sorted(tl.get("video") or [], key=lambda c: c["start"])
    lines = _durations_summary([(c["start"], c["end"]) for c in clips])
    titles: dict[int, str] = {}
    lines.append("逐个镜头（成片时间｜素材｜镜头标签｜剪辑理由）：")
    for c in clips:
        aid = c.get("asset_id")
        if aid not in titles:
            row = conn.execute("SELECT title FROM assets WHERE id=?", (aid,)).fetchone()
            titles[aid] = (row["title"] if row else None) or ""
        shot = conn.execute("SELECT tags, main_manual FROM asset_shots WHERE asset_id=? AND idx=?",
                            (aid, c.get("shot_idx"))).fetchone()
        tag = ""
        if shot:
            layers = shot_layers(json.loads(shot["tags"] or "null"), json.loads(shot["main_manual"] or "null"))
            tag = " ".join("/".join(layers.get(k) or []) for k in ("主标签", "车手", "车队") if layers.get(k))
        lines.append(f"- {_timecode(c['start'])}–{_timecode(c['end'])}｜素材#{aid}《{titles[aid][:30]}》镜头#{c.get('shot_idx')}"
                     f"｜{tag or '无标签'}｜{c.get('reason') or ''}")
    for o in tl.get("overlays") or []:
        lines.append(f"- 叠层 {_timecode(o['start'])}–{_timecode(o['end'])}：{o.get('kind') or ''} {Path(o['path']).name}")
    credits = sorted({c["text"] for c in tl.get("credits") or [] if c.get("text")})
    if credits:
        lines.append("右下角出处小字：" + "；".join(credits))
    return "\n".join(lines)


def _read(path: Path | None) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _asr(video: Path, work: Path) -> list[tuple[float, float, str]]:
    import mlx_whisper
    from .voice_post import ASR_PROMPT, model_path
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "_asr.wav"
    try:
        _run([FFMPEG, "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", "-y", wav])
        out = mlx_whisper.transcribe(str(wav), path_or_hf_repo=str(model_path()), language="zh",
                                     initial_prompt=ASR_PROMPT, condition_on_previous_text=False)
    finally:
        wav.unlink(missing_ok=True)
    return [(float(s["start"]), float(s["end"]), s["text"].strip()) for s in out.get("segments") or []
            if s.get("text", "").strip()]


def _db_part(conn, episode_id, ep_dir: Path | None) -> dict:
    out = {"stage_run": None, "facts": "", "timeline": ""}
    if episode_id is not None:
        row = conn.execute("SELECT stage_run FROM episodes WHERE id=?", (episode_id,)).fetchone()
        out["stage_run"] = row["stage_run"] if row else None
        out["facts"] = facts.render(conn, episode_id)
    if ep_dir:
        tl = _read_json(ep_dir / "06_合成" / "时间线.json")
        if tl.get("video"):
            out["timeline"] = timeline_summary(conn, tl)
    return out


def _media_part(video: Path, out: Path, ep_dir: Path | None, srt, script, title, transcriber) -> dict:
    info = probe(video)
    images, step = make_sheets(video, out / "看片", info["duration"])
    sec, notes = [], []
    sec.append(("成片规格", f"时长 {info['duration']:.1f} 秒，{info['width']}×{info['height']}，{info['fps']} 帧，"
                f"视频编码 {info['vcodec']}，{'有' if info['has_audio'] else '没有'}音轨。"))
    if ep_dir:
        srt = srt or ep_dir / "03_配音" / "字幕.srt"
        script = script or ep_dir / "02_文案" / "逐字稿.md"
        title = title or ep_dir / "02_文案" / "标题和简介.txt"
    cues, transcript = [], "无"
    if srt and _read(Path(srt)):
        cues, transcript = parse_srt(_read(Path(srt))), "字幕"
    elif not ep_dir and info["has_audio"]:
        try:
            cues, transcript = (transcriber or _asr)(video, out), "语音识别"
            notes.append("没有字幕文件，口播是语音识别转写的，可能有错字，不要据此扣事实分。")
        except Exception as e:
            notes.append(f"语音识别失败（{str(e)[:120]}），只能靠画面和音频数据评。")
    if not cues:
        notes.append("没有字幕或转写，口播内容只能从画面里的字幕看。")
        transcript = "无"
    text = _read(Path(script)) if script else ""
    if text:
        if "[F" in text or "[S" in text:
            sec.append(("逐字稿（带事实标签）", text))
        sec.append(("逐字稿（去标签）", facts.strip_tags(text)))
    if cues:
        sec.append(("字幕（带时间）" if transcript == "字幕" else "语音识别转写（带时间）", _cue_lines(cues)))
        sec.append(("语速", speech_rate(cues)))
    t = _read(Path(title)) if title else ""
    sec.append(("标题和简介", t or "没有。"))
    if ep_dir:
        hard = _read_json(ep_dir / "07_QA" / "硬检查.json")
        if hard:
            rows = [f"- {'通过' if i.get('ok') else ('没过' if i.get('hard') else '提醒')}｜{i.get('name')}｜{i.get('detail') or ''}"
                    for i in hard.get("items") or []]
            sec.append(("硬检查", f"整体{'通过' if hard.get('passed') else '没通过'}\n" + "\n".join(rows)))
        mix = _read_json(ep_dir / "06_合成" / "混音.json")
        if mix:
            sec.append(("混音与响度", "；".join(f"{k}={v}" for k, v in mix.items() if k != "bgm_sha256")))
        issues = _read_json(ep_dir / "03_配音" / "对齐.json").get("issues") or []
        if issues:
            sec.append(("配音提醒（程序检测）", "\n".join(f"- {_timecode(float(i.get('at') or 0))} {i.get('kind')}：{i.get('detail') or ''}"
                                                   for i in issues[:30])))
    else:
        notes.append("历史片没有程序硬检查结果，「规格合规」按画面和下面的响度数据判断。")
        try:
            m = compose.measure(video)
            sec.append(("响度（ebur128 实测）", f"整体 {m['integrated']} LUFS，响度范围 {m['lra']} LU，"
                        f"真峰值 {m['true_peak']} dBTP"))
        except Exception as e:
            notes.append(f"响度没测出来（{str(e)[:120]}）。")
        try:
            shots = detect_shots(video, info["duration"])
            sec.append(("镜头统计（场景切换检测估算，没有时间线）", "\n".join(_durations_summary(shots))))
        except Exception as e:
            notes.append(f"镜头检测失败（{str(e)[:120]}）。")
    return {"images": images, "step": step, "sections": sec, "notes": notes, "transcript": transcript,
            "duration": info["duration"]}


def _image_text(images: list[Path], step: float) -> str:
    lines = ["附图说明（每格下方白字是成片时间码 分:秒）："]
    for n, p in enumerate(images, start=1):
        what = (f"开头 0–15 秒，每 {INTRO_STEP} 秒一帧" if p.name.startswith("开头")
                else f"全片按时间顺序，每 {step:g} 秒一帧，4×4 一张")
        lines.append(f"- 第 {n} 张 {p.name}：{what}")
    return "\n".join(lines)


# ---------- 评审 ----------

async def review(conn: sqlite3.Connection, video, *, episode_id: int | None = None, branch: str, kind: str,
                 ep_dir=None, out_dir=None, runner=None, model: str | None = None, effort: str | None = None,
                 task_id: int | None = None, srt=None, script=None, title=None, transcriber=None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}：{kind!r}")
    if branch not in prompts.BRANCH_FILES:
        raise ValueError(f"未知分支：{branch!r}")
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"找不到视频：{video}")
    ep_dir = Path(ep_dir) if ep_dir else None
    out = Path(out_dir) if out_dir else (ep_dir / "07_QA" if ep_dir else None)
    if out is None:
        raise ValueError("没有 ep_dir 时要给 out_dir")
    out.mkdir(parents=True, exist_ok=True)
    if not model:
        model, effort = config.ROLE_MODELS["QA评审"]
    effort = effort or config.ROLE_MODELS["QA评审"][1]

    dbp = _db_part(conn, episode_id, ep_dir)
    media = await asyncio.to_thread(_media_part, video, out, ep_dir, srt, script, title, transcriber)
    sections = list(media["sections"])
    if dbp["timeline"]:
        sections.append(("时间线摘要", dbp["timeline"]))
    sections.append(("事实表", dbp["facts"] or "没有事实表（历史片或本期没登记）。"))
    if media["notes"]:
        sections.append(("材料说明", "\n".join(f"- {n}" for n in media["notes"])))
    context = f"分支：{branch}\n\n" + "\n\n".join(f"## {h}\n{t}" for h, t in sections)
    instruction = INSTRUCTION.format(images=_image_text(media["images"], media["step"]),
                                     version=RUBRIC_VERSION, rubric=rubric_text())
    from .research import lesson_notes
    prompt = prompts.assemble(branch, instruction, context,
                              lesson_notes(conn, branch, "QA评审") if kind == "成片" else "")

    work = out / "_AI评审"
    work.mkdir(parents=True, exist_ok=True)
    schema_path = work / "AI评审.schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "prompt.md").write_text(prompt, encoding="utf-8")
    run = runner or codex_runner.run
    # 每次都开新线程：不传 resume_thread，评审不带任何制作对话的上下文
    result = await run(prompt, model, effort, work, sandbox="read-only", output_schema=schema_path,
                       images=media["images"], timeout=TIMEOUT)
    codex_runner.record_usage(conn, result, model=model, effort=effort, episode_id=episode_id, task_id=task_id)
    res = validate(parse_json(result.message))
    base, below = below_baseline(conn, branch, res["total"])
    created = db.now()
    cur = conn.execute(
        "INSERT INTO reviews(episode_id, stage_run, kind, branch, video_path, rubric_version, total, scores, issues, "
        "model, codex_thread_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, dbp["stage_run"], kind, branch, str(video), RUBRIC_VERSION, res["total"],
         json.dumps(res["scores"], ensure_ascii=False), json.dumps(res["issues"], ensure_ascii=False),
         model, result.thread_id, created))
    report = {"review_id": cur.lastrowid, "kind": kind, "branch": branch, "episode_id": episode_id,
              "stage_run": dbp["stage_run"], "video": str(video), "rubric_version": RUBRIC_VERSION,
              "model": model, "effort": effort, "codex_thread_id": result.thread_id, **res,
              "baseline": base, "below_baseline": below,
              "materials": {"images": [str(p.relative_to(out)) for p in media["images"]],
                            "frame_step": media["step"], "transcript": media["transcript"],
                            "notes": media["notes"]},
              "created_at": created}
    (out / "AI评审.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m hermes.qa_review", description="AI 看片评审（历史片校准用）")
    p.add_argument("video")
    p.add_argument("--branch", required=True, choices=list(prompts.BRANCH_FILES))
    p.add_argument("--kind", default="校准", choices=KINDS)
    p.add_argument("--out", required=True, help="输出目录：AI评审.json 和 看片/ 拼图")
    p.add_argument("--srt", help="字幕文件；不给就用语音识别转写")
    p.add_argument("--script", help="口播稿")
    p.add_argument("--title", help="标题和简介.txt")
    p.add_argument("--model")
    p.add_argument("--effort")
    a = p.parse_args(argv)
    conn = db.connect()
    try:
        r = asyncio.run(review(conn, a.video, branch=a.branch, kind=a.kind, out_dir=a.out, model=a.model,
                               effort=a.effort, srt=a.srt, script=a.script, title=a.title))
    finally:
        conn.close()
    print(json.dumps({"total": r["total"], "scores": {k: v["score"] for k, v in r["scores"].items()},
                      "issues": len(r["issues"]), "baseline": r["baseline"], "below_baseline": r["below_baseline"],
                      "report": str(Path(a.out) / "AI评审.json")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
