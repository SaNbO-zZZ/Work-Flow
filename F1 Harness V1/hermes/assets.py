"""素材入库与打标：算 sha256、切镜头、抽关键帧、拼图，GPT 打标签；素材卡永久保留供反查。"""
import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import codex_runner, config, db, fingerprint, glossary

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

# 09-30 用两段 F1 官方转播原片（1080p50）缩到 320 宽后逐帧算分：硬切 0.24–0.50，
# 快速运镜 ≤0.10，回放转场动画 ≤0.17；ffmpeg 常用的 0.3 会漏掉约一半硬切，故取 0.2
SCENE_THRESHOLD = 0.2
SCENE_SCALE_WIDTH = 320
MIN_SHOT_SECONDS = 0.5
# 长镜头切细（2026-10-13 用户定）：超过 12 秒的镜头按画面指纹找“已经换成另一个画面”的点切开，每段至少 4 秒。
# 判定：接下来 1 秒的每一帧与本段开头 1 秒最近帧的汉明距离都 >20（同一画面多在 14 以内，不同画面 24–34）。
# 期 14 实测：虚拟赛道 116 秒切成 24 段（一个弯一段），韦伯跟拍 33 秒切成 6 段，车载 90 秒只切 2 段
SPLIT_MIN_SHOT = 12.0
SPLIT_MIN_PART = 4.0
SPLIT_DIST = 20
KEYFRAME_WIDTH = 640
THUMB_WIDTH = 480
SHEET_COLS, SHEET_ROWS = 4, 3
CELL_W, CELL_H, LABEL_H = 480, 270, 40
# 一次 GPT 调用最多发 4 张拼图（48 个镜头），镜头更多就分批
MAX_SHEETS = 4
BLACK_LUMA = 16
TAG_TIMEOUT_SECONDS = 900
TAIL_SKIP = 20   # YouTube 视频最后 20 秒一律不用（片尾订阅卡、下期推荐；2026-10-15 第 18 期粗剪露出 F1 官方片尾卡）

# 镜头标签分层（2026-10-08 用户确认）：赛段、主标签、镜头类型各一个；子标签、车队、车手可多个
SESSIONS = ("正赛", "排位", "冲刺", "练习", "其他")
MAIN_TAGS = ("比赛中", "发车格", "起步", "进站", "维修区车库", "安全车", "红旗", "事故", "领奖台", "庆祝",
             "观众", "采访", "发布会", "数据图表", "航拍风景")
SUB_TAGS = ("超车", "轮对轮", "缠斗", "调查处罚")
SHOT_TYPES = ("车载", "跟拍", "航拍", "特写", "全景", "图表", "人物")
TAG_KEYS = ("teams", "drivers", "circuit", "session", "main", "sub", "shot_type")
LAYERS = (("赛段", "session"), ("主标签", "main"), ("子标签", "sub"), ("镜头类型", "shot_type"),
          ("车队", "teams"), ("车手", "drivers"), ("赛道", "circuit"))
# 关键回合：抖音禁播理由点名的发车、超车、决胜画面（2026-10-12），剪辑决策和硬检查共用
KEY_MAINS = ("起步", "事故", "红旗")
KEY_SUBS = ("超车", "轮对轮", "缠斗")
RACE_MAINS = ("比赛中", "起步", "安全车", "事故", "红旗")    # 赛车在赛道上跑的实况

TAG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["shots"],
    "properties": {
        "shots": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["idx", "teams", "drivers", "circuit", "session", "main", "sub", "shot_type",
                         "has_subtitles", "has_logo", "logo_note", "end_card"],
            "properties": {
                "idx": {"type": "integer"},
                "teams": {"type": "array", "items": {"type": "string"}},
                "drivers": {"type": "array", "items": {"type": "string"}},
                "circuit": {"type": ["string", "null"]},
                "session": {"type": "string", "enum": list(SESSIONS)},
                "main": {"type": "string", "enum": list(MAIN_TAGS)},
                "sub": {"type": "array", "items": {"type": "string", "enum": list(SUB_TAGS)}},
                "shot_type": {"type": "string", "enum": list(SHOT_TYPES)},
                "has_subtitles": {"type": "boolean"},
                "has_logo": {"type": "boolean"},
                "logo_note": {"type": "string"},
                "end_card": {"type": "boolean"},
            },
        }},
    },
}

TAG_PROMPT = """你是中文 F1 自媒体的素材管理员。附图是同一段视频素材的镜头拼图：每格是一个镜头的中间帧，格子下方白字是镜头序号（#0 起）和起止时间码。
素材信息：标题「{title}」，上传者「{uploader}」，时长 {duration:.1f} 秒，共 {total} 个镜头；本次标注镜头 #{first} 到 #{last}，每个都要输出。

逐个镜头标注：
- teams / drivers：画面里能确认的车队、车手（看涂装、车号、头盔、画面文字、标题）；认不准就留空，不要猜。
- circuit：能确认的赛道，否则 null。
- session：赛段，只能从 {sessions} 里选一个。
- main：主标签，这个镜头主要在拍什么，只能从 {mains} 里选一个。
- sub：子标签，可多选也可为空，只能从 {subs} 里选（超车=正在完成超越；轮对轮=两车并排；缠斗=前后紧咬或画面有 BATTLE 字样；调查处罚=画面有干事调查或处罚通知）。
- shot_type：只能从 {shot_types} 里选一个。
- has_subtitles：这个镜头画面里有没有自带字幕（逐句的对白、解说或翻译文字，多在画面底部）；转播计时图、车手名条、标题字卡不算字幕。
- has_logo：这个镜头有没有其他频道、平台或 UP 主的台标水印（如 Sky Sports、咪咕、腾讯体育、B站 UP 主水印、常驻角落的频道文字）；B站等平台的角标（如 bilibili 字样）也算；F1 官方转播自带的计时图形和 F1 标志不算。
- logo_note：把这个镜头里每一个台标水印都列出来，每个写清在哪个角（左上角、右上角、左下角、右下角）、长什么样，用分号隔开（如"右上角「AEchihiro」徽标；右下角「AEchihiro bilibili」字样"）；没有台标写空字符串。
- end_card：片头片尾卡和非正片画面标 true：订阅提示（SUBSCRIBE、关注频道）、频道或下期视频推荐（WATCH MORE、缩略图墙）、片头片尾字卡、整屏成绩表或积分榜文字页，以及品牌画面：车队和赞助商的联名标题卡、宣传片字卡（比如迈凯伦和 Gemini 的“70 Days”标题）、带车队或品牌商标的转场动画、整屏 logo；正常比赛、人物、图表画面标 false。
字幕和台标只按各自格子里的画面判断，不要因为别的镜头有就连带标上。

车队、车手、赛道的中文名必须用下面译名表的写法（车手只写姓氏译名）；表里没有的写中国大陆通行译名：
{names}

只输出 JSON。"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run([str(a) for a in args], capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} 失败（退出码 {proc.returncode}）：{proc.stderr[-1500:]}")
    return proc


def _rate(text: str | None) -> float | None:
    try:
        num, _, den = (text or "").partition("/")
        return round(float(num) / float(den or 1), 3) if float(den or 1) else None
    except ValueError:
        return None


def probe(path) -> dict:
    out = _run([FFPROBE, "-v", "error", "-of", "json", "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate"
                ":stream_disposition=attached_pic", path]).stdout
    info = json.loads(out)
    streams = info.get("streams", [])
    # yt-dlp 嵌入的封面图也是 video 流，要排除
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video:
        raise ValueError(f"没有视频流：{path}")
    return {
        "duration": float(info.get("format", {}).get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "vcodec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _segments(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    bounds = [0.0] + sorted(c for c in cuts if 0 < c < duration) + [duration]
    shots: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if shots and b - a < MIN_SHOT_SECONDS:
            shots[-1] = (shots[-1][0], b)
        else:
            shots.append((a, b))
    if len(shots) > 1 and shots[0][1] - shots[0][0] < MIN_SHOT_SECONDS:
        shots[:2] = [(shots[0][0], shots[1][1])]
    return [(round(a, 3), round(b, 3)) for a, b in shots]


def detect_shots(path, duration) -> list[tuple[float, float]]:
    vf = f"scale={SCENE_SCALE_WIDTH}:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo"
    err = _run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-an", "-sn", "-dn",
                "-vf", vf, "-f", "null", "-"]).stderr
    cuts = [float(m) for m in re.findall(r"Parsed_showinfo.*?pts_time:\s*([\d.]+)", err)]
    return _segments(cuts, float(duration))


def visual_cuts(hashes: list[int]) -> list[int]:
    """一个镜头的指纹序列里画面换掉的帧号（按 fingerprint.FPS 计）。
    an 是比对基准（当前画面开头），a 是当前段开头；画面换了但离段头或片尾不到一段长，只换基准不切。"""
    import numpy as np
    fps = fingerprint.FPS
    hs = np.asarray(hashes, dtype=np.uint64)
    n, part = len(hs), int(SPLIT_MIN_PART * fps)
    cuts, a, an = [], 0, 0
    for k in range(1, n):
        anchor, win = hs[an:an + fps], hs[k:k + fps]
        anchor, win = anchor[anchor != 0], win[win != 0]   # 0 是纯色帧，不比
        if not len(anchor) or not len(win):
            continue
        if min(int(np.bitwise_count(anchor ^ w).min()) for w in win) > SPLIT_DIST:
            if k - a >= part and n - k >= part:
                cuts.append(k)
                a = k
            an = k
    return cuts


def split_long_shots(conn: sqlite3.Connection, asset_id: int) -> int:
    """把长镜头按画面变化切细：切出来的段追加成新镜头（编号接在最后，旧期时间线里的编号不变），
    沿用原镜头的标签和标记，补关键帧和指纹；原镜头标 split=1。返回新增段数。需要先建好指纹。"""
    asset = _asset(conn, asset_id)
    src = Path(asset["local_path"] or "")
    rows = conn.execute("SELECT * FROM asset_shots WHERE asset_id=? AND NOT split AND parent_idx IS NULL "
                        "AND end_s - start_s > ? AND phash IS NOT NULL ORDER BY idx",
                        (asset_id, SPLIT_MIN_SHOT)).fetchall()
    if not rows or not src.is_file():
        return 0
    nxt = conn.execute("SELECT MAX(idx) FROM asset_shots WHERE asset_id=?", (asset_id,)).fetchone()[0] + 1
    added = 0
    for r in rows:
        hs = fingerprint._unhex(r["phash"])
        cuts = visual_cuts(hs)
        if not cuts:
            continue
        t0 = fingerprint._shot_t0(r["start_s"])
        ks = [0] + cuts + [len(hs)]
        times = [r["start_s"]] + [round(t0 + k / fingerprint.FPS, 3) for k in cuts] + [r["end_s"]]
        parts = []
        for i in range(len(cuts) + 1):
            a, b = times[i], times[i + 1]
            frame = Path(r["keyframe"]).parent / f"{nxt:03d}.jpg" if r["keyframe"] \
                else config.LIBRARY_DIR / str(asset_id) / "shots" / f"{nxt:03d}.jpg"
            frame.parent.mkdir(parents=True, exist_ok=True)
            _keyframe(src, (a + b) / 2, frame)
            parts.append((asset_id, nxt, a, b, str(frame), fingerprint._hex(hs[ks[i]:ks[i + 1]]), r["tags"],
                          r["has_subtitles"], r["has_logo"], r["logo_note"], r["main_manual"], r["unusable"], r["idx"]))
            nxt += 1
        with db.transaction(conn):
            conn.executemany("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, keyframe, phash, tags, has_subtitles, "
                             "has_logo, logo_note, main_manual, unusable, parent_idx) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             parts)
            conn.execute("UPDATE asset_shots SET split=1 WHERE id=?", (r["id"],))
        added += len(parts)
    if added:
        fingerprint._INDEX.clear()
    return added


def _place(src: Path, dest: Path, move: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return
    if move:
        try:
            os.replace(src, dest)
            return
        except OSError:
            pass  # 跨卷只能复制
    # copyfile 不带扩展属性，免得 exFAT 上生成 ._ 影子文件
    shutil.copyfile(src, dest)
    if move:
        src.unlink()


def _keyframe(src: Path, at: float, out: Path) -> None:
    _run([FFMPEG, "-v", "error", "-ss", f"{at:.3f}", "-i", src, "-frames:v", "1",
          "-vf", f"scale={KEYFRAME_WIDTH}:-2", "-q:v", "3", "-y", out])
    if not out.exists():
        raise RuntimeError(f"关键帧没抽出来：{at:.3f} 秒")


def _font(size: int):
    path = config.FONTS_DIR / "SourceHanSansSC-Medium.otf"
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size)


def _timecode(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:04.1f}"


def _make_sheets(shots: list[tuple[float, float]], frames: list[Path], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    per, font, paths = SHEET_COLS * SHEET_ROWS, _font(24), []
    for n, start in enumerate(range(0, len(shots), per), start=1):
        idxs = range(start, min(start + per, len(shots)))
        rows = -(-len(idxs) // SHEET_COLS)
        sheet = Image.new("RGB", (SHEET_COLS * CELL_W, rows * (CELL_H + LABEL_H)), (40, 40, 40))
        draw = ImageDraw.Draw(sheet)
        for k, i in enumerate(idxs):
            x, y = k % SHEET_COLS * CELL_W, k // SHEET_COLS * (CELL_H + LABEL_H)
            with Image.open(frames[i]) as im:
                im = ImageOps.contain(im.convert("RGB"), (CELL_W - 6, CELL_H - 6))
            sheet.paste(im, (x + (CELL_W - im.width) // 2, y + (CELL_H - im.height) // 2))
            a, b = shots[i]
            draw.text((x + 8, y + CELL_H + 4), f"#{i}  {_timecode(a)} - {_timecode(b)}", fill="white", font=font)
        path = out_dir / f"sheet_{n}.jpg"
        sheet.save(path, quality=88)
        paths.append(path)
    return paths


def _make_thumb(frames: list[Path], out: Path) -> Path:
    def lit(p: Path) -> bool:
        with Image.open(p) as im:
            return sum(im.convert("L").getdata()) / (im.width * im.height) > BLACK_LUMA
    src = next((p for p in frames if lit(p)), frames[0])
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.resize((THUMB_WIDTH, max(1, round(im.height * THUMB_WIDTH / im.width)))).save(out, quality=88)
    return out


def ingest(conn: sqlite3.Connection, path: Path, *, source_type: str, source_url=None, title=None,
           uploader=None, credit=None, move=True) -> int:
    path = Path(path)
    digest = sha256_file(path)
    row = conn.execute("SELECT id, state, local_path FROM assets WHERE sha256=?", (digest,)).fetchone()
    if row and row["state"] != "failed":
        if move and path.resolve() != Path(row["local_path"] or "/").resolve():
            path.unlink(missing_ok=True)
        return row["id"]
    fields = (source_type, source_url, title, uploader, credit)
    if row:
        # 上次失败的同一文件：沿用原 id 重做
        asset_id = row["id"]
        with db.transaction(conn):
            conn.execute("DELETE FROM asset_shots WHERE asset_id=?", (asset_id,))
            conn.execute("UPDATE assets SET source_type=?, source_url=?, title=?, uploader=?, credit=?, "
                         "state='processing' WHERE id=?", (*fields, asset_id))
    else:
        asset_id = conn.execute(
            "INSERT INTO assets(source_type, source_url, title, uploader, credit, sha256, state, created_at) "
            "VALUES (?,?,?,?,?,?, 'processing', ?)", (*fields, digest, db.now())).lastrowid
    try:
        folder = config.LIBRARY_DIR / str(asset_id)
        dest = folder / f"source{path.suffix.lower()}"
        _place(path, dest, move)
        conn.execute("UPDATE assets SET local_path=? WHERE id=?", (str(dest), asset_id))
        info = probe(dest)
        shots = detect_shots(dest, info["duration"])
        for sub in ("shots", "sheets"):
            shutil.rmtree(folder / sub, ignore_errors=True)
        (folder / "shots").mkdir(parents=True)
        frames = []
        for i, (a, b) in enumerate(shots):
            frames.append(folder / "shots" / f"{i:03d}.jpg")
            _keyframe(dest, (a + b) / 2, frames[-1])
        sheets = _make_sheets(shots, frames, folder / "sheets")
        thumb = _make_thumb(frames, folder / "thumb.jpg")
        thumbs = {"thumb": str(thumb), "sheets": [str(p) for p in sheets]}
        with db.transaction(conn):
            conn.executemany("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, keyframe) VALUES (?,?,?,?,?)",
                             [(asset_id, i, a, b, str(frames[i])) for i, (a, b) in enumerate(shots)])
            conn.execute("UPDATE assets SET duration=?, width=?, height=?, thumbs=?, state='ready' WHERE id=?",
                         (info["duration"], info["width"], info["height"],
                          json.dumps(thumbs, ensure_ascii=False), asset_id))
    except BaseException:
        conn.execute("UPDATE assets SET state='failed' WHERE id=?", (asset_id,))
        raise
    return asset_id


def _loads(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _asset(conn: sqlite3.Connection, asset_id: int) -> dict:
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise ValueError(f"素材不存在：{asset_id}")
    return _asset_dict(row)


def _asset_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("tags", "thumbs", "fingerprints"):
        d[k] = _loads(d.get(k))
    return d


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 else None
    except json.JSONDecodeError as e:
        raise ValueError(f"GPT 打标输出不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("GPT 打标输出不是 JSON 对象")
    return data


def _approved_names(conn: sqlite3.Connection) -> tuple[str, dict[str, str]]:
    """返回给提示词用的译名清单，以及把原名/建议名/车手姓氏归一到已审写法的映射。"""
    lines: dict[str, list[str]] = {}
    mapping: dict[str, str] = {}
    for it in glossary.items(conn, state="approved"):
        if it["kind"] not in ("车手", "车队", "大奖赛", "赛道"):
            continue
        zh, orig = it["approved"], it["original"]
        lines.setdefault(it["kind"], []).append(f"{orig}={zh}")
        for key in (orig, zh, it["suggested"]):
            if key:
                mapping[key.strip().lower()] = zh
        if it["kind"] == "车手":
            mapping.setdefault(orig.split()[-1].lower(), zh)
    text = "\n".join(f"- {k}：" + "；".join(v) for k, v in lines.items()) or "（译名表暂无已审条目）"
    return text, mapping


def _norm(name, mapping: dict[str, str]) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return mapping.get(name.strip().lower(), name.strip())


def _norm_list(names, mapping: dict[str, str]) -> list[str]:
    out = [_norm(n, mapping) for n in names] if isinstance(names, list) else []
    return list(dict.fromkeys(n for n in out if n))


def _default_ask(conn: sqlite3.Connection) -> Callable:
    async def ask(prompt: str, images: list[Path]) -> str:
        model, effort = config.ROLE_MODELS["素材打标"]
        workdir = config.CORE / "data" / "tag-work"
        workdir.mkdir(parents=True, exist_ok=True)
        schema = workdir / "asset-tag.schema.json"
        schema.write_text(json.dumps(TAG_SCHEMA, ensure_ascii=False))
        result = await codex_runner.run(prompt, model, effort, workdir, sandbox="read-only", ephemeral=True,
                                        output_schema=schema, images=images, timeout=TAG_TIMEOUT_SECONDS)
        codex_runner.record_usage(conn, result, model=model, effort=effort)
        return result.message
    return ask


def _idx_ranges(idxs: list[int]) -> str:
    parts, start = [], None
    for k, i in enumerate(idxs):
        start = i if start is None else start
        if k + 1 == len(idxs) or idxs[k + 1] != i + 1:
            parts.append(f"#{start}" if start == i else f"#{start}–#{i}")
            start = None
    return "、".join(parts)


LOGO_NOTE_GROUPS = 3


def _logo_summary(shots: dict[int, dict]) -> str:
    """把逐镜头台标说明按相同描述归并，如"镜头 #3、#7：右上角白色某某体育字样"。"""
    groups: dict[str, list[int]] = {}
    for i in sorted(shots):
        if shots[i]["has_logo"]:
            groups.setdefault(shots[i]["logo_note"], []).append(i)
    lines = [f"镜头 {_idx_ranges(v)}：{k}" if k else f"镜头 {_idx_ranges(v)} 有台标"
             for k, v in list(groups.items())[:LOGO_NOTE_GROUPS]]
    rest = sum(len(v) for v in list(groups.values())[LOGO_NOTE_GROUPS:])
    return "；".join(lines + ([f"另有 {rest} 个镜头有台标"] if rest else []))


async def tag_asset(conn: sqlite3.Connection, asset_id: int, *, ask=None) -> dict:
    asset = _asset(conn, asset_id)
    if asset["state"] != "ready":
        raise ValueError(f"素材 {asset_id} 状态是 {asset['state']}，不能打标")
    sheets = [Path(p) for p in (asset["thumbs"] or {}).get("sheets", [])]
    all_idx = [r["idx"] for r in conn.execute("SELECT idx FROM asset_shots WHERE asset_id=? AND parent_idx IS NULL "
                                              "ORDER BY idx", (asset_id,))]
    if not sheets or not all_idx:
        raise ValueError(f"素材 {asset_id} 没有可打标的镜头")
    names, mapping = _approved_names(conn)
    fn, per = ask or _default_ask(conn), SHEET_COLS * SHEET_ROWS
    shots: dict[int, dict] = {}
    batches = 0
    for b in range(0, len(sheets), MAX_SHEETS):
        batch = sheets[b:b + MAX_SHEETS]
        # 拼图按 12 个镜头一张顺序生成，第 n 张对应 #12(n-1) 起
        covered = all_idx[b * per:(b + len(batch)) * per]
        if not covered:
            break
        prompt = TAG_PROMPT.format(
            title=asset["title"] or "未知", uploader=asset["uploader"] or "未知", duration=asset["duration"] or 0,
            total=len(all_idx), first=covered[0], last=covered[-1], sessions="/".join(SESSIONS),
            mains="/".join(MAIN_TAGS), subs="/".join(SUB_TAGS), shot_types="/".join(SHOT_TYPES), names=names)
        reply = fn(prompt, batch)
        if inspect.isawaitable(reply):
            reply = await reply
        batches += 1
        for s in _parse_json(reply).get("shots") or []:
            if not isinstance(s, dict) or s.get("idx") not in covered:
                continue
            has_logo = bool(s.get("has_logo"))
            shots[s["idx"]] = {
                "teams": _norm_list(s.get("teams"), mapping),
                "drivers": _norm_list(s.get("drivers"), mapping),
                "circuit": _norm(s.get("circuit"), mapping),
                "session": s.get("session") if s.get("session") in SESSIONS else "其他",
                "main": [s["main"]] if s.get("main") in MAIN_TAGS else [],
                "sub": list(dict.fromkeys(x for x in s.get("sub") or [] if x in SUB_TAGS)),
                "shot_type": s.get("shot_type") if s.get("shot_type") in SHOT_TYPES else None,
                "has_subtitles": bool(s.get("has_subtitles")),
                "has_logo": has_logo,
                "logo_note": str(s.get("logo_note") or "").strip() if has_logo else "",
                "end_card": bool(s.get("end_card")),
            }
    return save_tags(conn, asset_id, shots) | {"batches": batches, "engine": "gpt"}


def shot_layers(tags, manual=None) -> dict[str, list[str]]:
    """镜头标签按层展开；手改的主标签替代自动结果。"""
    if isinstance(tags, list):
        return {"其他": [str(t) for t in tags if t]}
    t = tags if isinstance(tags, dict) else {}
    out = {}
    for name, key in LAYERS:
        v = t.get(key)
        if key == "main":
            if manual:
                v = manual
            elif v is None and t.get("scene"):  # 10-08 以前的旧格式
                v = [t["scene"]]
        out[name] = [str(x) for x in (v if isinstance(v, list) else [v]) if x]
    return out


def key_moment(layers: dict[str, list[str]]) -> list[str]:
    """镜头里属于关键回合的标签（主标签起步/事故/红旗，子标签超车/轮对轮/缠斗）。"""
    return ([t for t in layers.get("主标签", []) if t in KEY_MAINS]
            + [t for t in layers.get("子标签", []) if t in KEY_SUBS])


def flat_tags(layers: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(t for v in layers.values() for t in v))


def refresh_asset_tags(conn: sqlite3.Connection, asset_id: int) -> list[str]:
    """按层汇总全部镜头的标签写回整条素材。"""
    per_layer: dict[str, list[str]] = {}
    for r in conn.execute("SELECT tags, main_manual FROM asset_shots WHERE asset_id=? AND NOT split ORDER BY idx",
                          (asset_id,)):
        for name, vals in shot_layers(_loads(r["tags"]), _loads(r["main_manual"])).items():
            per_layer.setdefault(name, []).extend(vals)
    merged = flat_tags(per_layer)
    conn.execute("UPDATE assets SET tags=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), asset_id))
    return merged


def save_tags(conn: sqlite3.Connection, asset_id: int, shots: dict[int, dict]) -> dict:
    """逐镜头标签写库，并汇总成整条素材的标签和字幕/台标标记（GPT 打标和本机打标共用）。
    标了片头片尾卡（end_card）的镜头顺手标不可用；只标不清，用户手标的不可用重打标也保留。"""
    ordered = [shots[i] for i in sorted(shots)]
    has_subtitles = any(t["has_subtitles"] for t in ordered)
    has_logo = any(t["has_logo"] for t in ordered)
    logo_note = _logo_summary(shots)
    with db.transaction(conn):
        for idx, sh in shots.items():
            tags = {k: sh[k] for k in TAG_KEYS}
            # 切细出来的段（parent_idx=原镜头）跟着原镜头的标签走
            conn.execute("UPDATE asset_shots SET tags=?, has_subtitles=?, has_logo=?, logo_note=? "
                         "WHERE asset_id=? AND (idx=? OR parent_idx=?)",
                         (json.dumps(tags, ensure_ascii=False), int(sh["has_subtitles"]), int(sh["has_logo"]),
                          sh["logo_note"] or None, asset_id, idx, idx))
            if sh.get("end_card"):
                conn.execute("UPDATE asset_shots SET unusable=1 WHERE asset_id=? AND (idx=? OR parent_idx=?)",
                             (asset_id, idx, idx))
        conn.execute("UPDATE assets SET has_subtitles=?, has_logo=?, logo_note=?, tagged_at=? WHERE id=?",
                     (int(has_subtitles), int(has_logo), logo_note or None, db.now(), asset_id))
        merged = refresh_asset_tags(conn, asset_id)
    return {"shots": [{"idx": i, **shots[i]} for i in sorted(shots)], "tags": merged,
            "has_subtitles": has_subtitles, "has_logo": has_logo, "logo_note": logo_note}


SHOT_COLUMNS = "idx, start_s, end_s, keyframe, phash, tags, has_subtitles, has_logo, logo_note, main_manual, unusable"


def _flag(value) -> bool | None:
    return None if value is None else bool(value)


def _shot_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"], d["main_manual"] = _loads(d["tags"]), _loads(d["main_manual"])
    d["has_subtitles"], d["has_logo"] = _flag(d["has_subtitles"]), _flag(d["has_logo"])
    d["unusable"] = bool(d["unusable"])
    d["layers"] = shot_layers(d["tags"], d["main_manual"])
    return d


def _shots(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    """可用的镜头（切细过的长镜头只给切出来的段），按时间排。"""
    return [_shot_dict(r) for r in conn.execute(
        f"SELECT {SHOT_COLUMNS} FROM asset_shots WHERE asset_id=? AND NOT split ORDER BY start_s, idx", (asset_id,))]


def clean_shots(conn: sqlite3.Connection, asset_id: int, *, allow_subtitles=True) -> list[dict]:
    """给剪辑挑镜头：去掉标了不可用的、带其他频道台标的，allow_subtitles=False 时再去掉自带字幕的。"""
    asset = _asset(conn, asset_id)
    out = []
    for s in _shots(conn, asset_id):
        # 旧数据没有逐镜头标记，按全片标记算；全片也没打标的视为干净
        logo = s["has_logo"] if s["has_logo"] is not None else asset["has_logo"]
        subs = s["has_subtitles"] if s["has_subtitles"] is not None else asset["has_subtitles"]
        if s["unusable"] or logo or (subs and not allow_subtitles):
            continue
        out.append(s)
    return out


def card(conn: sqlite3.Connection, asset_id: int) -> dict:
    asset = _asset(conn, asset_id)
    shots = _shots(conn, asset_id)
    uses = [dict(r) for r in conn.execute(
        "SELECT u.*, e.code AS episode_code, e.title AS episode_title FROM asset_uses u "
        "JOIN episodes e ON e.id = u.episode_id WHERE u.asset_id=? ORDER BY u.id", (asset_id,))]
    main_counts: dict[str, int] = {}
    for s in shots:
        for t in s["layers"].get("主标签", []):
            main_counts[t] = main_counts.get(t, 0) + 1
    return {**asset, "shots": shots, "uses": uses,
            "main_counts": sorted(main_counts.items(), key=lambda kv: -kv[1])}


def set_main_manual(conn: sqlite3.Connection, asset_id: int, idx: int, main: list[str] | None) -> dict:
    """用户改/加主标签；传空就恢复自动结果。"""
    main = list(dict.fromkeys(main or []))
    bad = [t for t in main if t not in MAIN_TAGS]
    if bad:
        raise ValueError(f"不是主标签：{'、'.join(map(str, bad))}")
    with db.transaction(conn):
        cur = conn.execute("UPDATE asset_shots SET main_manual=? WHERE asset_id=? AND idx=?",
                           (json.dumps(main, ensure_ascii=False) if main else None, asset_id, idx))
        if not cur.rowcount:
            raise KeyError(f"没有这个镜头：素材 {asset_id} #{idx}")
        refresh_asset_tags(conn, asset_id)
    row = conn.execute(f"SELECT {SHOT_COLUMNS} FROM asset_shots WHERE asset_id=? AND idx=?",
                       (asset_id, idx)).fetchone()
    return _shot_dict(row)


def set_unusable(conn: sqlite3.Connection, asset_id: int, idx: int, unusable: bool) -> dict:
    """用户把镜头标为不可用（剪辑和以文搜图都跳过）或恢复可用。"""
    with db.transaction(conn):
        cur = conn.execute("UPDATE asset_shots SET unusable=? WHERE asset_id=? AND idx=?",
                           (int(bool(unusable)), asset_id, idx))
        if not cur.rowcount:
            raise KeyError(f"没有这个镜头：素材 {asset_id} #{idx}")
    row = conn.execute(f"SELECT {SHOT_COLUMNS} FROM asset_shots WHERE asset_id=? AND idx=?",
                       (asset_id, idx)).fetchone()
    return _shot_dict(row)


def _tagged_shots(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT s.id, s.asset_id, s.idx, s.start_s, s.end_s, s.tags, s.main_manual, a.title FROM asset_shots s "
        "JOIN assets a ON a.id = s.asset_id WHERE s.tags IS NOT NULL AND a.state='ready' AND NOT s.unusable AND NOT s.split "
        "ORDER BY s.asset_id DESC, s.idx")


def filter_shots(conn: sqlite3.Connection, tags: list[str], *, limit: int | None = 200) -> list[dict]:
    """镜头级组合筛选：同一个镜头要同时带着全部标签。"""
    want, out = set(tags), []
    for r in _tagged_shots(conn):
        layers = shot_layers(_loads(r["tags"]), _loads(r["main_manual"]))
        if want <= set(flat_tags(layers)):
            out.append({"id": r["id"], "asset_id": r["asset_id"], "shot_idx": r["idx"], "start_s": r["start_s"],
                        "end_s": r["end_s"], "title": r["title"], "layers": layers})
            if limit and len(out) >= limit:
                break
    return out


def tag_groups(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """全库镜头标签按层计数（给标签栏分组显示）。"""
    counts: dict[str, dict[str, int]] = {name: {} for name, _ in LAYERS}
    for r in _tagged_shots(conn):
        for name, vals in shot_layers(_loads(r["tags"]), _loads(r["main_manual"])).items():
            for t in set(vals):
                c = counts.setdefault(name, {})
                c[t] = c.get(t, 0) + 1
    return {k: sorted(v.items(), key=lambda kv: -kv[1]) for k, v in counts.items() if v}


def list_assets(conn: sqlite3.Connection, *, tag: str | None = None, state=None, limit=100) -> list[dict]:
    sql, args = "SELECT * FROM assets WHERE 1=1", []
    if tag:
        sql += (" AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(assets.tags) THEN assets.tags END)"
                " WHERE value=?)")
        args.append(tag)
    if state:
        sql += " AND state=?"
        args.append(state)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    return [_asset_dict(r) for r in rows]


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def usable_end(asset) -> float | None:
    """素材能用到第几秒：YouTube 视频长于 60 秒的去掉最后 TAIL_SKIP 秒，其他到片尾；没有时长返回 None。"""
    dur = _get(asset, "duration")
    if dur is None:
        return None
    url = _get(asset, "source_url") or ""
    if ("youtube.com" in url or "youtu.be" in url) and dur > 60:
        return float(dur) - TAIL_SKIP
    return float(dur)


def credit_of(conn: sqlite3.Connection, asset_id: int) -> str | None:
    """右下角出处文字：优先入库时给的 credit，没有就用上传者。"""
    row = conn.execute("SELECT credit, uploader FROM assets WHERE id=?", (asset_id,)).fetchone()
    return (row["credit"] or row["uploader"]) if row else None


_SITES = (("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("bilibili.com", "B站"), ("b23.tv", "B站"),
          ("formula1.com", "F1 官网"), ("fia.com", "FIA"),
          ("x.com", "X"), ("twitter.com", "X"), ("instagram.com", "Instagram"), ("facebook.com", "Facebook"),
          ("fb.watch", "Facebook"), ("tiktok.com", "TikTok"), ("weibo.com", "微博"), ("weibo.cn", "微博"),
          ("douyin.com", "抖音"), ("iesdouyin.com", "抖音"), ("pirelli.com", "Pirelli"))
_CREDIT_PREFIX = re.compile(r"^\s*(?:来源|画面|出处)\s*[:：]\s*")
_TAIL_NOTE = re.compile(r"\s*[（(【\[][^（()）【】\[\]]*[)）】\]]\s*$")
CREDIT_TITLE_MAX = 20


def site_name(url: str | None) -> str:
    """来源网址 → 站点名；常见站点有固定叫法，其他用去掉 www 的域名。"""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url.strip()).hostname or "").lower() if url and url.strip() else ""
    except ValueError:
        return ""
    if not host:
        return ""
    for dom, name in _SITES:
        if host == dom or host.endswith("." + dom):
            return name
    return re.sub(r"^(?:www\d*|m)\.", "", host)


def credit_line(asset) -> str | None:
    """出处小字：画面：<网站> · <频道>《<信息名称>》，不放网址；没有网址就省掉"<网站> · "。"""
    a = dict(asset)
    site = site_name(a.get("source_url"))
    who = (a.get("uploader") or "").strip()
    if not who:
        who = _CREDIT_PREFIX.sub("", a.get("credit") or "").strip()
        if "@" in who:
            who = who.split("@", 1)[1].strip()
    name = (a.get("title") or "").strip()
    while _TAIL_NOTE.search(name) and _TAIL_NOTE.sub("", name):
        name = _TAIL_NOTE.sub("", name).strip()
    if len(name) > CREDIT_TITLE_MAX:
        name = name[:CREDIT_TITLE_MAX].rstrip() + "…"
    if not who and not name:
        return None
    head = " · ".join(x for x in (site, who) if x)
    return "画面：" + head + (f"《{name}》" if name else "")
