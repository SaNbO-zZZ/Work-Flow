"""M5-3 配音后处理：剪映乱序片段 → 语音识别排序对位 → 变速、压停顿拼成 配音.wav → 查读错/停顿/语速。

做法沿用验收①用户选中的样本 2：原速片段按序拼接 → atempo 1.3 → 超过 0.5 秒的静音压到 0.5 秒
（剪映在句中切开的拼接处压到 0.1 秒）；
响度归一留给合成，这里只给试听版做。
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import config
from .assets import FFMPEG, _run

SR = 48000
FRAME = SR // 100              # 10 ms 一帧判静音
SILENCE_DB = -45.0             # 与样本 2 的 silenceremove 阈值一致
MATCH_MIN = 0.8                # 低于此相似度报"读错"
PAUSE_LIMIT = 0.15             # 非标点处静音超过此值（变速后）报"停顿"
PAUSE_MIN = 0.08               # 短于此的静音当作字间正常间隙，不参与判断
SNAP = 3                       # 片段边界离单元边界不超过几个字就算在标点处
PUNCT_TOL = 3                  # 静音位置离标点不超过几个字就算标点停顿
PAUSE_PUNCT = re.compile(r"[，。！？、；：,!?;:…—]+|\.(?!\d)")   # 允许停顿的标点
SPLICE = 0.1                   # 剪映在句子中间切开的拼接处最多留这么久
RATE_TARGET = 360              # 字/分
RATE_HIGH, RATE_LOW = 1.25, 0.7   # 1.3 倍实测 400 上下，超过 450 才提醒
ASR_PROMPT = "以下是F1赛车中文解说：倍耐力、麦凯伦、法拉利、梅赛德斯、红牛、威廉姆斯、阿斯顿·马丁、围场、正赛、排位赛、冲刺赛、圈速、进站、轮胎。"
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac"}


def model_path() -> Path:
    return config.CORE / "tools" / "models" / "whisper-turbo-q4"


def transcribe(path: Path, prompt: str = "") -> str:
    import mlx_whisper
    out = mlx_whisper.transcribe(str(path), path_or_hf_repo=str(model_path()), language="zh",
                                 initial_prompt=prompt or None, condition_on_previous_text=False)
    return out["text"].strip()


# ---------- 文本归一：数字转中文读法，去标点 ----------

_DIG = "零一二三四五六七八九"


def _small(n: int) -> str:
    s, zero = "", False
    for unit, v in (("千", 1000), ("百", 100), ("十", 10), ("", 1)):
        d, n = divmod(n, v)
        if d:
            s += ("零" if zero else "") + _DIG[d] + unit
            zero = False
        elif s:
            zero = True
    return s


def _int_cn(n: int) -> str:
    if n == 0:
        return "零"
    hi, lo = divmod(n, 10000)
    out = (_small(hi) + "万" + ("零" if 0 < lo < 1000 else "")) if hi else ""
    out += _small(lo) if lo else ""
    return out[1:] if out.startswith("一十") else out


def _digits(s: str) -> str:
    return "".join(_DIG[int(c)] for c in s)


def _num(m: re.Match) -> str:
    s, frac = m.group(1), m.group(2)
    if frac:
        return _int_cn(int(s)) + "点" + _digits(frac)
    after = m.string[m.end():m.end() + 1]
    if (len(s) > 1 and s[0] == "0") or len(s) > 8 or (len(s) == 4 and after == "年"):
        return _digits(s)
    return _int_cn(int(s))


def spoken(text: str) -> str:
    """阿拉伯数字转成中文读法：年份逐位读，小数点后逐位读，0 开头逐位读。"""
    return re.sub(r"(\d+)(?:\.(\d+))?", _num, text)


def norm(text: str) -> str:
    return re.sub(r"[\W_]", "", spoken(text).replace("〇", "零").lower())


def count_chars(text: str) -> int:
    """语速字数：只数汉字和字母数字。"""
    return len(re.findall(r"[\w]", text.replace("_", "")))


# ---------- 排序对位 ----------

def _locate(ref: str, a: str) -> tuple[int, float]:
    """在 ref 里找与 a 最像的起点；候选起点来自共同的两字片段。
    原文窗口除了取 a 的长度，还试长一些的：英文名被识别成短音译（The Race → 德瑞斯）时，
    只取 a 的长度会截掉对的位置后半句，片段被排到别处（2026-10-16 第 21 期）。"""
    if not a:
        return len(ref), 0.0
    grams = {}
    for i in range(len(ref) - 1):
        grams.setdefault(ref[i:i + 2], []).append(i)
    cands = {max(0, i - j) for j in range(len(a) - 1) for i in grams.get(a[j:j + 2], ())}
    if not cands:
        cands = {max(0, ref.find(c)) for c in a if c in ref}
    best = (len(ref), 0.0)
    lens = sorted({len(a), len(a) + len(a) // 4, len(a) + len(a) // 2})
    for p in sorted(cands):
        r = max(difflib.SequenceMatcher(None, ref[p:p + n], a, autojunk=False).ratio() for n in lens)
        if r > best[1]:
            best = (p, r)
    return best


def order(raw_files, units, *, transcribe=transcribe) -> list[dict]:
    """每段 ASR 后按在全文里的位置排序；落在单元中间的片段边界（剪映在长句中间切的）并入前一段。

    返回的每一项对应连续单元 [u0, u1)，可能包含多个文件（files）；file 为文件名用 + 连接。
    """
    keys = [norm(u["say"]) for u in units]
    bounds = [0]
    for k in keys:
        bounds.append(bounds[-1] + len(k))
    ref = "".join(keys)
    rows = []
    for f in raw_files:
        f = Path(f)
        asr = transcribe(f, ASR_PROMPT)
        off, _ = _locate(ref, norm(asr))
        rows.append({"path": str(f), "name": f.name, "asr": asr, "off": off})
    rows.sort(key=lambda r: (r["off"], r["name"]))

    groups = []
    for r in rows:
        k = min(range(len(bounds)), key=lambda i: abs(bounds[i] - r["off"]))
        snapped = abs(bounds[k] - r["off"]) <= SNAP
        if not groups or (snapped and groups[-1]["u0"] < k < len(units)):
            groups.append({"u0": k if groups else 0, "rows": []})
        groups[-1]["rows"].append(r)
    out = []
    for g, nxt in zip(groups, groups[1:] + [{"u0": len(units)}]):
        us = units[g["u0"]:nxt["u0"]]
        say = "".join(u["say"] for u in us)
        asr = "".join(r["asr"] for r in g["rows"])
        out.append({
            "file": "+".join(r["name"] for r in g["rows"]),
            "files": [r["path"] for r in g["rows"]],
            "u0": g["u0"], "u1": nxt["u0"],
            "say": say, "show": "".join(u.get("show") or u["say"] for u in us),
            "asr": asr,
            "match": round(difflib.SequenceMatcher(None, norm(say), norm(asr), autojunk=False).ratio(), 3),
        })
    return out


# ---------- 拼接 ----------

def _decode(path, speed: float) -> np.ndarray:
    tempo = f"atempo={speed}," if speed != 1 else ""
    args = [FFMPEG, "-v", "error", "-i", str(path), "-af", f"{tempo}aresample={SR}",
            "-ac", "1", "-f", "f32le", "-"]
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败：{path}：{proc.stderr.decode(errors='replace')[-800:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _speech_frames(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, bool)
    rms = np.sqrt(np.mean(x[:n * FRAME].reshape(n, FRAME) ** 2, axis=1))
    return 20 * np.log10(rms + 1e-9) > SILENCE_DB


def _split(x: np.ndarray, gap: float):
    """返回 (头静音秒, 去头尾并压过内部长静音的音频, 内部静音[(在音频里的位置样本, 时长秒)], 尾静音秒)。"""
    sp = _speech_frames(x)
    idx = np.flatnonzero(sp)
    if not len(idx):
        return len(x) / SR, np.zeros(0, np.float32), [], 0.0
    a, b = idx[0], idx[-1] + 1
    keep = int(gap * SR)
    parts, pauses, pos, i = [], [], 0, a
    while i < b:
        j = i
        while j < b and sp[j] == sp[i]:
            j += 1
        seg = x[i * FRAME:j * FRAME]
        if not sp[i] and len(seg) > keep:
            seg = np.concatenate([seg[:keep // 2], seg[len(seg) - (keep - keep // 2):]])
        if not sp[i] and len(seg) >= PAUSE_MIN * SR:
            pauses.append((pos, len(seg) / SR))
        parts.append(seg)
        pos += len(seg)
        i = j
    return a * FRAME / SR, np.concatenate(parts), pauses, (len(x) - b * FRAME) / SR


def assemble(clips, out_wav: Path, *, speed=1.3, gap=0.3, splice=SPLICE) -> list[dict]:
    """按 clips 顺序拼接。每段先变速；段内超过 gap 的静音压到 gap；段间停顿取 min(原有尾+头静音, gap)，
    同一段里多个文件（剪映句中切开）的拼接处取 min(原有, splice)。

    返回每段 {"start","end","pauses":[{"at","dur","frac"}]}，frac 是停顿前的说话时间占本段说话时间的比例。
    """
    out, pos, prev_tail = [], 0, None
    timings = []
    for c in clips:
        files = c.get("files") or [c["file"]]
        start, pauses = None, []
        for f in files:
            head, core, inner, tail = _split(_decode(f, speed), gap)
            if not len(core):
                continue
            if prev_tail is not None:
                join = int(round(min(prev_tail + head, gap if start is None else splice) * SR))
                out.append(np.zeros(join, np.float32))
                if start is not None and join >= PAUSE_MIN * SR:
                    pauses.append((pos, join / SR))
                pos += join
            if start is None:
                start = pos
            pauses += [(pos + p, d) for p, d in inner]
            out.append(core)
            pos += len(core)
            prev_tail = tail
        if start is None:
            start = pos
        talk = (pos - start) / SR - sum(d for _, d in pauses)
        before, ps = 0.0, []
        for p, d in pauses:
            ps.append({"at": round(p / SR, 3), "dur": round(d, 3),
                       "frac": round(((p - start) / SR - before) / talk, 4) if talk > 0 else 0.0})
            before += d
        timings.append({"start": round(start / SR, 3), "end": round(pos / SR, 3), "pauses": ps})
    data = np.concatenate(out) if out else np.zeros(0, np.float32)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return timings


# ---------- 检查 ----------

def _pause_issues(i: int, seg_units: list[dict], timing: dict) -> list[dict]:
    """段内停顿：每个标点认领离它最近的一个停顿，剩下超过 PAUSE_LIMIT 的报出来。"""
    keys = [norm(k) for u in seg_units for k in PAUSE_PUNCT.split(u["say"])]
    keys = [k for k in keys if k]
    text = "".join(keys)
    total = len(text)
    if not total:
        return []
    cuts, acc = [], 0
    for k in keys[:-1]:
        acc += len(k)
        cuts.append(acc)
    pauses = [dict(p, char=p["frac"] * total) for p in timing["pauses"]]
    pairs = sorted((abs(p["char"] - c), pi, ci) for pi, p in enumerate(pauses) for ci, c in enumerate(cuts))
    used_p, used_c = set(), set()
    for d, pi, ci in pairs:
        if d <= PUNCT_TOL and pi not in used_p and ci not in used_c:
            used_p.add(pi)
            used_c.add(ci)
    issues = []
    for pi, p in enumerate(pauses):
        if pi in used_p or p["dur"] <= PAUSE_LIMIT:
            continue
        c = int(round(p["char"]))
        issues.append({"kind": "停顿", "segment": i, "at": p["at"],
                       "detail": f"「{text[max(0, c - 5):c]}｜{text[c:c + 5]}」处停顿 {p['dur']:.2f} 秒"})
    return issues


def _rate_issues(units, clips, timings) -> list[dict]:
    """按段落算语速：段内时间按字数分给各单元，段落时长 = 首单元开始到末单元结束。"""
    spans = {}
    for i, (c, t) in enumerate(zip(clips, timings)):
        us = units[c["u0"]:c["u1"]]
        n = [max(1, count_chars(u["say"])) for u in us]
        tot, acc = sum(n), 0
        for u, k in zip(us, n):
            a = t["start"] + (t["end"] - t["start"]) * acc / tot
            acc += k
            b = t["start"] + (t["end"] - t["start"]) * acc / tot
            s = spans.setdefault(u.get("para", 0), {"seg": i, "a": a, "b": b, "chars": 0})
            s["b"] = b
            s["chars"] += count_chars(u["say"])
    issues = []
    for para, s in spans.items():
        dur = s["b"] - s["a"]
        if dur <= 0:
            continue
        rate = s["chars"] / dur * 60
        if rate > RATE_TARGET * RATE_HIGH or rate < RATE_TARGET * RATE_LOW:
            issues.append({"kind": "语速", "segment": s["seg"], "at": round(s["a"], 3),
                           "detail": f"第 {para} 段 {s['chars']} 字 / {dur:.1f} 秒 = {rate:.0f} 字/分"
                                     f"（基准 {RATE_TARGET}）"})
    return issues


def process(voice_dir: Path, *, speed=1.3, gap=0.3, transcribe=transcribe) -> dict:
    """读 配音稿.json 和 raw/，写 配音.wav、对齐.json、试听.mp3，返回对齐.json 内容。"""
    voice_dir = Path(voice_dir)
    units = json.loads((voice_dir / "配音稿.json").read_text(encoding="utf-8"))["units"]
    raw = sorted(p for p in (voice_dir / "raw").iterdir()
                 if p.suffix.lower() in AUDIO_EXT and not p.name.startswith("._"))
    if not raw:
        raise FileNotFoundError(f"没有配音片段：{voice_dir / 'raw'}")
    clips = order(raw, units, transcribe=transcribe)
    wav = voice_dir / "配音.wav"
    timings = assemble(clips, wav, speed=speed, gap=gap)

    segments, issues = [], []
    for i, (c, t) in enumerate(zip(clips, timings)):
        segments.append({"i": i, "file": c["file"], "text": c["say"], "show": c["show"],
                         "start": t["start"], "end": t["end"], "match": c["match"], "asr": c["asr"]})
        if c["match"] < MATCH_MIN:
            issues.append({"kind": "读错", "segment": i, "at": t["start"],
                           "detail": f"应为「{c['say']}」听成「{c['asr']}」（相似度 {c['match']:.2f}）"})
        issues += _pause_issues(i, units[c["u0"]:c["u1"]], t)
    issues += _rate_issues(units, clips, timings)
    issues.sort(key=lambda x: (x["at"], x["kind"]))

    with wave.open(str(wav), "rb") as w:
        duration = round(w.getnframes() / w.getframerate(), 3)
    result = {"speed": speed, "gap": gap, "duration": duration, "segments": segments, "issues": issues}
    (voice_dir / "对齐.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    _run([FFMPEG, "-v", "error", "-y", "-i", wav, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
          "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", "192k", voice_dir / "试听.mp3"])
    return result
