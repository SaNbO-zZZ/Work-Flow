import sys, statistics, difflib, json

def merge(tsv, fps=4.0):
    rows = []
    for line in open(tsv, encoding="utf-8"):
        name, _, text = line.rstrip("\n").partition("\t")
        rows.append(((int(name[:5]) - 1) / fps, text.strip()))
    segs = []
    for t, text in rows:
        if not text:
            continue
        if segs and difflib.SequenceMatcher(None, segs[-1]["text"], text).ratio() > 0.7 and t - segs[-1]["end"] < 0.6:
            segs[-1]["end"] = t + 1 / fps
            segs[-1]["votes"].append(text)
        else:
            segs.append({"start": t, "end": t + 1 / fps, "text": text, "votes": [text]})
    for s in segs:  # 取出现次数最多的识别结果
        s["text"] = max(set(s["votes"]), key=s["votes"].count)
        del s["votes"]
    return [s for s in segs if s["end"] - s["start"] >= 0.5]

pairs = [a.split("=") for a in sys.argv[1:]] if "=" in sys.argv[1] else zip("ABCD", sys.argv[1:5])
for v, dur in ((v, float(d)) for v, d in pairs):
    segs = merge(f"{v}.ocr.tsv")
    cuts = [float(x) for x in open(f"{v}.cuts")]
    shots = [b - a for a, b in zip([0] + cuts, cuts + [dur])]
    chars = sum(len(s["text"]) for s in segs)
    speech = sum(s["end"] - s["start"] for s in segs)
    with open(f"{v}.transcript.txt", "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{s['start']:6.1f}] {s['text']}\n")
    # 每 30 秒一个窗口的切点数，看节奏曲线
    curve = [sum(1 for c in cuts if i <= c < i + 30) for i in range(0, int(dur) + 1, 30)]
    print(json.dumps({"v": v, "dur": round(dur), "lines": len(segs), "chars": chars,
                      "chars_per_min": round(chars / dur * 60), "subtitle_cover": round(speech / dur, 2),
                      "cuts": len(cuts), "cuts_per_min": round(len(cuts) / dur * 60, 1),
                      "shot_median": round(statistics.median(shots), 1), "shot_mean": round(statistics.mean(shots), 1),
                      "shot_max": round(max(shots), 1), "cuts_per_30s": curve}, ensure_ascii=False))
