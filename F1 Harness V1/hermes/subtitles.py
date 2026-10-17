"""M5-4 字幕：按对齐结果生成零标点字幕（SRT/JSON），并用 Pillow 画烧录用透明 PNG。

ffmpeg 8.1 没有 drawtext/libass，字幕只能画成整幅透明 PNG 由合成叠加。
样式对照雪邦前瞻成片实测（参考帧和对比图在 data/m5trial/sub_style/）。
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .graphics import font

SENT_END = set("。！？!?；;…")
DROP = set("“”‘’\"'《》〈〉「」『』()（）[]【】")   # 成对符号直接删，不留空格
EXTRA_PUNCT = set("—–-~～·・/")

# 样式（1920×1080 基准）
FONT_WEIGHT = "Heavy"
FONT_SIZE = 66
STROKE = 6
SHADOW_OFFSET = 4
SHADOW_BLUR = 4
SHADOW_ALPHA = 150
BASELINE = 957          # 文字基线 y；参考成片汉字底边约在 963
MAX_TEXT_W = 1680


def _is_punct(text: str, i: int) -> bool:
    ch = text[i]
    if ch in ".:" and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
        return False      # 小数点、比分里的冒号保留
    return ch in EXTRA_PUNCT or unicodedata.category(ch).startswith("P")


def count(text: str) -> int:
    return len(text.replace(" ", ""))


def strip_punct(text: str) -> list[list[str]]:
    """拆成 句子→短语：句末标点断句，句中标点断短语，成对符号直接删。"""
    sents, phrases, buf = [], [], ""

    def close_phrase():
        nonlocal buf
        p = re.sub(r"\s+", " ", buf).strip()
        if p:
            phrases.append(p)
        buf = ""

    for i, ch in enumerate(text):
        if ch in DROP:
            continue
        if not _is_punct(text, i):
            buf += ch
            continue
        close_phrase()
        if ch in SENT_END and phrases:
            sents.append(phrases)
            phrases = []
    close_phrase()
    if phrases:
        sents.append(phrases)
    return sents


def _cut(p: str, max_chars: int) -> list[str]:
    """超长短语均分，不切开连续的字母数字。"""
    n = math.ceil(count(p) / max_chars)
    if n <= 1:
        return [p]
    parts, last = [], 0
    for k in range(1, n):
        c = round(len(p) * k / n)
        while 0 < c < len(p) and re.match(r"[A-Za-z0-9.]", p[c - 1]) and re.match(r"[A-Za-z0-9.]", p[c]):
            c += 1
        if last < c < len(p):
            parts.append(p[last:c].strip())
            last = c
    parts.append(p[last:].strip())
    return [x for x in parts if x]


def _lines(phrases: list[str], max_chars: int) -> list[str]:
    """把一句的短语排成若干行：先求最少行数，再让各行字数尽量平均；只在原标点或切点处断。"""
    pieces = []      # (文字, 前面的连接符)
    for p in phrases:
        for j, part in enumerate(_cut(p, max_chars)):
            pieces.append((part, " " if j == 0 else ""))
    lens = [count(t) for t, _ in pieces]
    n = len(pieces)
    target = sum(lens) / max(1, math.ceil(sum(lens) / max_chars))
    best, prev = [0.0] + [math.inf] * n, [0] * (n + 1)
    for j in range(1, n + 1):
        w = 0
        for i in range(j - 1, -1, -1):
            w += lens[i]
            if w > max_chars and j - i > 1:
                break
            cost = best[i] + 1000 + (w - target) ** 2
            if cost < best[j]:
                best[j], prev[j] = cost, i
    out, j = [], n
    while j > 0:
        i = prev[j]
        out.append((pieces[i][0] + "".join(s + t for t, s in pieces[i + 1:j])).strip())
        j = i
    return out[::-1]


def build(segments, *, max_chars=16, min_dur=0.8) -> list[dict]:
    """segments 用对齐.json 的 segments（show 字段，没有就用 text）。"""
    cues = []
    for s in segments:
        lines = [ln for ph in strip_punct(s.get("show") or s.get("text") or "") for ln in _lines(ph, max_chars)]
        total = sum(count(ln) for ln in lines)
        if not total:
            continue
        t, acc = s["start"], 0
        for ln in lines:
            acc += count(ln)
            end = s["start"] + (s["end"] - s["start"]) * acc / total
            cues.append({"start": t, "end": end, "text": ln})
            t = end
    # 相邻衔接：上一条一直显示到下一条开始
    for a, b in zip(cues, cues[1:]):
        a["end"] = b["start"]
    # 过短的并到较短的邻居上
    while True:
        best = None
        for k, c in enumerate(cues):
            if c["end"] - c["start"] >= min_dur:
                continue
            for m in (k - 1, k + 1):
                if 0 <= m < len(cues) and count(c["text"]) + count(cues[m]["text"]) <= max_chars + 4:
                    if best is None or count(cues[m]["text"]) < count(cues[best[1]]["text"]):
                        best = (k, m)
            if best:
                break
        if not best:
            break
        a, b = sorted(best)
        cues[a:b + 1] = [{"start": cues[a]["start"], "end": cues[b]["end"],
                          "text": cues[a]["text"] + " " + cues[b]["text"]}]
    return [{"start": round(c["start"], 3), "end": round(c["end"], 3), "text": c["text"]} for c in cues]


def write_json(cues, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cues, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n\n" for i, c in enumerate(cues, 1))
    path.write_text(body, encoding="utf-8")
    return path


def render(text: str, size=(1920, 1080)) -> Image.Image:
    """全画幅透明 PNG：白色粗黑体 + 黑描边 + 柔和下投影，居中，基线固定在底部。"""
    W, H = size
    k = W / 1920
    px = FONT_SIZE
    f = font(FONT_WEIGHT, round(px * k))
    while f.getlength(text) > MAX_TEXT_W * k and px > 36:
        px -= 2
        f = font(FONT_WEIGHT, round(px * k))
    stroke = max(1, round(STROKE * k))
    xy = (W / 2, BASELINE * k)
    shadow = Image.new("L", size, 0)
    ImageDraw.Draw(shadow).text((xy[0], xy[1] + SHADOW_OFFSET * k), text, font=f, fill=SHADOW_ALPHA,
                                anchor="ms", stroke_width=stroke, stroke_fill=SHADOW_ALPHA)
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * k))
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.putalpha(shadow)
    top = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(top).text(xy, text, font=f, fill=(255, 255, 255, 255), anchor="ms",
                             stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return Image.alpha_composite(img, top)


def render_all(cues, out_dir: Path, size=(1920, 1080)) -> list[dict]:
    """每条字幕一张 PNG，同文本复用同一张。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths, out = {}, []
    for c in cues:
        if c["text"] not in paths:
            p = out_dir / f"sub_{len(paths) + 1:04d}.png"
            render(c["text"], size).save(p)
            paths[c["text"]] = p
        out.append({"start": c["start"], "end": c["end"], "path": str(paths[c["text"]])})
    return out
