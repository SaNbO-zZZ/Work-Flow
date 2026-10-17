"use strict";
// 会话里的卡片（第 3 批，2026-10-13 用户定）：每个关卡、每轮找素材 / 配音 / 图形 / 封面、失败都是一张卡片，
// 审过的东西一直留在会话里；有打回时左右对照（左边上一版和打回意见，右边新版），改动的字标红（删）标绿（加）。
// 依赖 app.js 里的 $、esc、dur、bjTime、api、toast、current、detail。

const verSel = {};          // 每期每个关卡选中对照的两版 {"14:G1": [1, 2]}
const openKeys = new Set(); // 展开着的折叠块，重画会话后保持
const GATE_NAME = { G0: "选题", G1: "文案 · 逐字稿", G2: "粗剪", G3: "成片" };
const STATE_TAG = { pending: ["", "待审"], approved: ["ok", "已通过"], rejected: ["bad", "已打回"] };

const fileUrl = (rel, v) => `/api/episodes/${current}/file/${rel.split("/").map(encodeURIComponent).join("/")}${v ? `?v=${encodeURIComponent(v)}` : ""}`;
// 某一版里的文件：存在 09_版本 里的用存下的；最新一版没存到时用当前文件（live）
const verFile = (ver, rel) => (ver.dir == null ? null : ver.dir ? fileUrl(`${ver.dir}/${rel}`) : fileUrl(rel, ver.decided_at || ver.created_at));
const hasFile = (ver, rel) => ver.dir != null && (ver.files || []).includes(rel);
const tag = (cls, text) => `<span class="tag ${cls}">${esc(text)}</span>`;
const fold = (key, label, body) => `<details data-key="${esc(key)}"${openKeys.has(key) ? " open" : ""}><summary class="more">${esc(label)}</summary>${body}</details>`;

// ---------- 对照：最长公共子序列 ----------
function diffOps(a, b) {
  const n = a.length, m = b.length;
  if (n * m > 4e6) return null;   // 太长不比
  const t = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push(["=", a[i]]); i++; j++; } else if (t[i + 1][j] >= t[i][j + 1]) ops.push(["-", a[i++]]); else ops.push(["+", b[j++]]);
  }
  while (i < n) ops.push(["-", a[i++]]);
  while (j < m) ops.push(["+", b[j++]]);
  return ops;
}
// 返回 [左边 html, 右边 html, 有没有改动]；左边标删掉的，右边标加上的
function sideBySide(a, b, byLine) {
  const A = byLine ? (a || "").split("\n") : [...(a || "")], B = byLine ? (b || "").split("\n") : [...(b || "")];
  const ops = diffOps(A, B), sep = byLine ? "\n" : "";
  if (!ops) return [esc(a), esc(b), a !== b];
  const side = (skip, mark) => ops.filter(([o]) => o !== skip).map(([o, x]) => (o === "=" ? esc(x) : `<${mark}>${esc(x) || " "}</${mark}>`)).join(sep);
  return [side("+", "del"), side("-", "ins"), ops.some(([o]) => o !== "=")];
}
// 只看改动：改了的行和前后各一行
function changesOnly(a, b) {
  const ops = diffOps((a || "").split("\n"), (b || "").split("\n"));
  if (!ops) return `<div class="sub">逐字稿太长，不做逐行对照</div>`;
  const keep = ops.map((o, i) => o[0] !== "=" || (ops[i - 1] && ops[i - 1][0] !== "=") || (ops[i + 1] && ops[i + 1][0] !== "="));
  let out = "", gap = false;
  ops.forEach(([o, x], i) => {
    if (!keep[i]) { if (!gap) out += `<div class="sub">…</div>`; gap = true; return; }
    gap = false;
    out += o === "=" ? `<div>${esc(x) || " "}</div>` : o === "-" ? `<div><del>${esc(x) || " "}</del></div>` : `<div><ins>${esc(x) || " "}</ins></div>`;
  });
  return out || `<div class="sub">两版逐字稿相同</div>`;
}

// ---------- 关卡卡片 ----------
function pickPair(gate, list) {
  const key = `${current}:${gate}`, n = list.length;
  let sel = (verSel[key] || []).filter((v) => v >= 1 && v <= n);
  if (!sel.length) sel = n > 1 ? [n - 1, n] : [n];
  return [...sel].sort((x, y) => x - y);
}
function verChips(gate, list, sel) {
  if (list.length < 2) return "";
  return `<span class="vers">${list.map((v) => `<span data-ver="${gate}:${v.v}" class="${sel.includes(v.v) ? "on" : ""} ${v.state === "rejected" ? "rej" : ""}" title="${esc(STATE_TAG[v.state][1])}">v${v.v}</span>`).join("")}</span>`;
}
const verHead = (v, last) => `v${v.v}（${v.state === "pending" ? "待审" : v.state === "approved" ? (v.auto ? "自动通过" : "已通过") : "已打回"}${last && v.state !== "pending" ? " · 当前" : ""} · ${bjTime(v.decided_at || v.created_at)}）`;
const fbHtml = (v) => (v.feedback ? `<div class="fb ${v.state === "rejected" ? "" : "okfb"}">${v.state === "rejected" ? "打回意见" : "审核意见"}：${esc(v.feedback)}</div>` : "");
const noFiles = `<div class="ph wide">这一版没有留下文件（第 3 批之前的版本没存）</div>`;
function decideLine(gate, v) {
  if (v.state === "pending") return "";
  const ok = v.state === "approved";
  return `<div class="decide ${ok ? "" : "rej"}">${esc(gate)} v${v.v} ${ok ? (v.auto ? "自动通过" : "通过") : "打回"} · ${bjTime(v.decided_at)}${v.feedback ? ` · ${esc(v.feedback)}` : ""}</div>`;
}

function g0Body(list, [a, b]) {
  const vb = list[b - 1];
  if (vb.topic == null) return `<div class="sub" style="margin:0">选题文件没有留下</div>`;
  const parts = vb.topic.split(/\n(?=## )/), head = parts.filter((p) => /^## (角度|标题方向)/.test(p)).join("\n");
  const prev = a !== b ? list[a - 1] : null;
  const cmp = prev && prev.topic != null ? fold(`${current}:G0:cmp`, `和 v${a} 对照`, `<div class="cmp">${sideBySide(prev.topic, vb.topic, true).slice(0, 2).map((h, i) => `<div><h6>${verHead(i ? vb : prev, i === 1)}</h6><div class="pre">${h}</div>${i ? "" : fbHtml(prev)}</div>`).join("")}</div>`) : "";
  return `<div class="pre">${esc(head || parts[0] || "")}</div>${fold(`${current}:G0:full`, "展开选题全文", `<div class="pre">${esc(vb.topic)}</div>`)}${cmp}`;
}

function g1Body(list, [a, b]) {
  const vb = list[b - 1], va = a !== b ? list[a - 1] : null;
  if (vb.script == null) return `<div class="sub" style="margin:0">逐字稿文件没有留下</div>`;
  const words = (vb.script || "").replace(/^#.*$/gm, "").replace(/\[[SF]\d+\]/g, "").replace(/\s/g, "").length;
  const secs = (vb.script.match(/^## /gm) || []).length;
  const win = `<a class="more" data-win="G1:${vb.v}${va ? ":" + va.v : ""}">在独立窗口打开</a>`;
  let top;
  if (va && va.script != null) {
    const [ta, tb] = sideBySide(va.title, vb.title, false), [da, db] = sideBySide(va.desc, vb.desc, false);
    top = `<div class="cmp"><div><h6>${verHead(va)}</h6><div><b>${ta || "—"}</b></div><div class="sub pre" style="margin:4px 0 0">${da || "—"}</div>${fbHtml(va)}</div>
      <div><h6>${verHead(vb, true)}</h6><div><b>${tb || "—"}</b></div><div class="sub pre" style="margin:4px 0 0">${db || "—"}</div>${fbHtml(vb)}</div></div>`;
  } else {
    top = `<div><b>${esc(vb.title || "—")}</b></div><div class="sub pre" style="margin:4px 0 0">${esc(vb.desc || "")}</div>`
      + (va ? `<div class="sub" style="margin:6px 0 0">v${va.v} 的文件没有留下，只有打回意见：${esc(va.feedback || "无")}</div>` : "");
  }
  const same = va && va.script != null ? (va.script === vb.script ? ` · 和 v${va.v} 相同` : ` · 和 v${va.v} 有改动`) : "";
  const diff = va && va.script != null && va.script !== vb.script ? fold(`${current}:G1:diff`, "只看改动", `<div class="pre script-diff">${changesOnly(va.script, vb.script)}</div>`) : "";
  return `${top}<div class="sub" style="margin:10px 0 4px"><b>逐字稿 v${vb.v}</b> · ${secs} 段 · 约 ${words} 字${same}</div>
    ${fold(`${current}:G1:full`, "展开全文", `<div class="pre script">${esc(vb.script)}</div>`)}${diff}<div>${win}</div>`;
}

function videoOf(v, rel) { return hasFile(v, rel) ? `<video class="media-v" style="margin:0" controls preload="none" src="${verFile(v, rel)}"></video>` : noFiles; }
function g2Meta(v) {
  if (v.duration == null) return "";
  const ov = (v.overlays || []).length;
  return `<div class="sub" style="margin:6px 0 0">${dur(v.duration)} · ${v.shots} 个镜头${ov ? ` · 叠层 ${ov} 张` : ""}</div>`;
}
function g3Meta(v) {
  if (!v.hard) return "";
  const h = v.hard;
  return `<div class="sub" style="margin:6px 0 0">${h.count ? (h.passed ? `硬检查 ${h.count} 项全过` : `硬检查没过：${esc(h.bad.join("、"))}`) : "硬检查结果没有留下"}${v.ai != null ? ` · AI 评审 ${v.ai} 分` : ""}</div>`;
}
function mediaBody(list, [a, b], rel, meta) {
  const vb = list[b - 1], va = a !== b ? list[a - 1] : null;
  const one = (v, last) => `<div><h6>${verHead(v, last)}</h6>${videoOf(v, rel)}${meta(v)}${fbHtml(v)}</div>`;
  return va ? `<div class="cmp">${one(va)}${one(vb, true)}</div>` : `${videoOf(vb, rel)}${meta(vb)}`;
}
function g3Covers(v) {
  const c = ["06_合成/封面_横版.png", "06_合成/封面_竖版.png"].filter((r) => hasFile(v, r));
  return c.length ? `<div class="covers">${c.map((r) => `<a href="${verFile(v, r)}" target="_blank"><img src="${verFile(v, r)}" alt=""></a>`).join("")}</div>` : "";
}

// 某关卡最新一张卡片画完整卡（版本标签、对照、待审时的意见框和按钮）；更早的画成一行审核结果
function gateStageCard(m) {
  const gate = m.card.gate, list = (detail.versions || {})[gate] || [];
  const idx = list.findIndex((v) => v.gate_id === m.card.gate_id);
  const latestId = [...detail.messages].reverse().find((x) => x.card && x.card.type === "gate" && x.card.gate === gate)?.id;
  if (idx < 0) return "";
  if (m.id !== latestId) return decideLine(gate, list[idx]);
  // sel 是选中的版本（一版或两版），pair 固定两项 [左, 右]，只选一版时左右相同、不对照
  const sel = pickPair(gate, list), pair = [sel[0], sel[sel.length - 1]], cur = list[list.length - 1];
  const [cls, label] = STATE_TAG[cur.state];
  const body = gate === "G0" ? g0Body(list, pair) : gate === "G1" ? g1Body(list, pair)
    : gate === "G2" ? mediaBody(list, pair, "06_合成/粗剪.mp4", g2Meta)
    : mediaBody(list, pair, "06_合成/成片.mp4", g3Meta) + g3Covers(list[pair[1] - 1]);
  const pending = cur.state === "pending" ? gateActions(gate) : null;
  const rejects = list.filter((v) => v.state === "rejected").length;
  return `<div class="msg"><div class="widget"><h5>${esc(GATE_NAME[gate])} ${tag(cls, `${gate} ${label}`)}${rejects ? tag("bad", `打回 ${rejects} 次`) : ""}${verChips(gate, list, sel)}</h5>
    ${body}${pending ? pending.body : ""}</div>${pending ? pending.below : decideLine(gate, cur)}</div>`;
}

// ---------- 过程卡片 ----------
const STATE_CLS = { "已下载": "ok", "下载失败": "bad", "超时": "bad", "跳过": "", "下载中": "" };
function sourcingCard(m) {
  const c = m.card;
  if (!c.found && !c.segments) return `<div class="msg system"><div class="text">${esc(m.content)}</div></div>`;
  const head = !c.done ? tag("", "下载中") : c.forced_segments.length ? tag("", `按打回意见重找：${c.forced_segments.join("、")}`) : c.gpt_called ? "" : tag("ok", "现有素材够用，没联网找");
  const segs = (c.segments || []).map((s) => `<div class="seg"><span>${esc(s.name)}</span><div class="bar"><i style="width:${Math.min(100, s.need ? (s.have / s.need) * 100 : 100)}%;${s.have < s.need ? "background:var(--amber)" : ""}"></i></div><span>需 ${Math.round(s.need)} · 有 ${Math.round(s.have)}</span></div>`).join("");
  const rows = (c.found || []).map((f) => {
    const img = f.asset_id ? `<img src="/api/assets/${f.asset_id}/thumb" alt="" loading="lazy">` : `<div class="ph">${esc(f.state === "下载中" ? "下载中" : "没下到")}</div>`;
    const meta = [f.channel, f.duration ? dur(f.duration) : "", f.asset_id ? `素材 ${f.asset_id}` : "", f.segment ? `用在：${f.segment}` : ""].filter(Boolean).join(" · ");
    const why = f.note && f.state !== "已下载" ? `<br><span class="sub" style="margin:0;color:var(--red)">${esc(f.note)}</span>` : f.reason ? `<br><span class="sub" style="margin:0">${esc(f.reason)}</span>` : "";
    return `<tr><td>${img}</td><td><a href="${esc(f.url)}" target="_blank"><b>${esc(f.asset_title || f.title || f.url)}</b></a><br><span class="sub" style="margin:0">${esc(meta)}</span>${why}</td><td>${tag(STATE_CLS[f.state] ?? "", f.state === "已下载" ? "已入库" : f.state)}</td></tr>`;
  }).join("");
  const have = (c.assets || []).map((a) => `素材 ${a.id}${a.title ? `（${a.title}）` : ""}${c.race && a.label ? `［${a.label}${a.same_race === false ? "，其他站" : ""}］` : ""}`).join("、");
  const race = c.race ? tag("", `本站：${c.race.season} ${c.race.name_zh}`) + (c.other_library === "allow" ? tag("", "用了其他站画面") : "") : "";
  const notes = [...(c.short || []).length ? [`画面可能偏少的段：${c.short.join("、")}`] : [], ...(c.notes || []), ...(c.lib_notes || [])];
  return `<div class="msg"><div class="widget"><h5>找素材 · 第 ${c.run} 轮 ${head}${race}</h5>
    ${segs ? `<div class="sub" style="margin:0">各段需要和已有的画面（秒）</div>${segs}` : ""}
    ${rows ? `<table class="vl">${rows}</table>` : c.done && c.gpt_called ? `<div class="sub" style="margin:8px 0 0">联网没找到能用的新视频</div>` : ""}
    ${have ? fold(`${current}:src:${c.run}`, `本期用的素材 ${c.assets.length} 条`, `<div class="sub" style="margin:4px 0 0">${esc(have)}</div>`) : ""}
    ${notes.map((n) => `<div class="sub" style="margin:6px 0 0">${esc(n)}</div>`).join("")}</div></div>`;
}
// 本站画面不够时问要不要用素材库里其他站的（2026-10-14 用户定）
const OTHER_LIB = { ask: ["bad", "需要你决定"], allow: ["ok", "已允许用其他站画面"], deny: ["", "不用其他站画面"] };
function otherLibraryCard(m) {
  const c = m.card, [cls, label] = OTHER_LIB[c.state] || OTHER_LIB.ask;
  const live = c.state === "ask" && detail.episode.stage_run === c.run;
  return `<div class="msg"><div class="widget"><h5>找素材 · 第 ${c.run} 轮 · ${esc(c.race || "")} ${tag(cls, label)}</h5>
    <div class="sub" style="margin:0">${esc(m.content)}</div></div>
    ${live ? `<div class="below"><button class="btn primary" data-otherlib="allow">允许用其他站画面</button><button class="btn soft" data-otherlib="deny">不用，就用现有的</button></div>` : ""}</div>`;
}
function voiceCard(m) {
  const c = m.card, kinds = Object.entries(c.kinds || {});
  const issues = (c.issues || []).map((i) => `<div class="warn"><b>${dur(i.at)} ${esc(i.kind)}</b>${esc(i.detail || "")}</div>`).join("");
  return `<div class="msg"><div class="widget"><h5>配音 · 第 ${c.run} 轮 ${tag("", `${Math.round(c.duration)} 秒 · ${c.cues} 条字幕`)}</h5>
    ${c.file ? `<audio class="media-a" style="margin:0" controls preload="none" src="${fileUrl(c.file)}"></audio>` : ""}
    <div class="sub" style="margin:6px 0 0">${kinds.length ? "提醒：" + esc(kinds.map(([k, n]) => `${k} ${n} 处`).join("、")) : "没发现读错、停顿和语速问题"}</div>
    ${issues ? fold(`${current}:voice:${c.run}`, "展开提醒", `<div class="checks">${issues}</div>`) : ""}</div></div>`;
}
function overlaysCard(m) {
  const c = m.card;
  const figs = (c.items || []).map((i) => `<figure>${i.path ? `<a href="${fileUrl(i.path)}" target="_blank"><img src="${fileUrl(i.path)}" alt="" loading="lazy"></a>` : `<div class="ph wide">没有图</div>`}<figcaption>${esc(i.name)} · ${i.start.toFixed(1)}–${i.end.toFixed(1)} 秒（按${esc(i.how)}定位）</figcaption></figure>`).join("");
  return `<div class="msg"><div class="widget"><h5>图形叠层 · 第 ${c.run} 轮 ${tag("", `${(c.items || []).length} 张`)}</h5>
    ${figs ? `<div class="thumbs">${figs}</div>` : ""}
    ${(c.warnings || []).map((w) => `<div class="sub" style="margin:6px 0 0">提醒：${esc(w)}</div>`).join("")}</div></div>`;
}
function coverCard(m) {
  const c = m.card, key = `${current}:cover:${c.run}`;
  const show = (paths) => `<div class="covers">${paths.map((p) => `<a href="${fileUrl(p)}" target="_blank"><img src="${fileUrl(p)}" alt="" loading="lazy"></a>`).join("")}</div>`;
  if (c.running) return `<div class="msg"><div class="widget"><h5>封面 ${tag("", "AI 正在做，大约 5 分钟")}</h5>${c.note ? `<div class="sub" style="margin:0">意见：${esc(c.note)}</div>` : ""}</div></div>`;
  const after = c.after || [], before = c.before || [];
  const pick = verSel[key] || (after.length ? "after" : "before");
  const chips = after.length && before.length ? `<span class="vers"><span data-cover="${esc(c.run)}:before" class="${pick === "before" ? "on" : ""}">做之前</span><span data-cover="${esc(c.run)}:after" class="${pick === "after" ? "on" : ""}">AI 新封面</span></span>` : "";
  return `<div class="msg"><div class="widget"><h5>封面 ${c.error ? tag("bad", "AI 没做成，用原来的") : tag("ok", "AI 封面")}${chips}</h5>
    ${show(pick === "after" ? after : before)}
    ${c.note ? `<div class="sub" style="margin:6px 0 0">意见：${esc(c.note)}</div>` : ""}
    <div class="sub" style="margin:6px 0 0">${esc(c.error || c.summary || "")}</div></div></div>`;
}
function failedCard(m) {
  const c = m.card, last = [...detail.messages].reverse().find((x) => x.card && x.card.type === "failed");
  const live = last && last.id === m.id && detail.episode.state === "需人工";
  return `<div class="msg"><div class="widget err"><h5>${esc(c.stage)}暂停了 ${live ? tag("bad", "需要你处理") : tag("", "已处理")}</h5>
    ${(c.steps || []).map((s) => `<div class="sub" style="margin:0 0 4px">${esc(s.step)}：${c.needs_human ? "需要你处理" : `重试 ${c.attempts} 次都失败`}，原因：${esc(s.error)}</div>`).join("")}</div>
    ${live ? `<div class="below"><button class="btn primary" data-resume="1">继续（重试失败的步骤）</button></div>` : ""}</div>`;
}
function cardHtml(m) {
  const t = m.card && m.card.type;
  try {
    if (t === "gate") return gateStageCard(m);
    if (t === "sourcing") return sourcingCard(m);
    if (t === "other_library") return otherLibraryCard(m);
    if (t === "voice") return voiceCard(m);
    if (t === "overlays") return overlaysCard(m);
    if (t === "cover") return coverCard(m);
    if (t === "failed") return failedCard(m);
  } catch (e) { console.error(e); }
  return m.content ? `<div class="msg system"><div class="text">${esc(m.content)}</div></div>` : "";
}
// 切换版本、封面后重画会话，但停在原来的位置
function redraw() { const t = $("#thread"), y = t.scrollTop; renderThread(); t.scrollTop = y; }
function bindCards() {
  document.querySelectorAll("#thread details[data-key]").forEach((d) => (d.ontoggle = () => (d.open ? openKeys.add(d.dataset.key) : openKeys.delete(d.dataset.key))));
  document.querySelectorAll("#thread [data-ver]").forEach((s) => (s.onclick = () => {
    // 点一个版本：没选中就加进来（最多对照两版，挤掉先选的），已选中就去掉
    const [gate, v] = s.dataset.ver.split(":"), key = `${current}:${gate}`;
    const cur = verSel[key] || pickPair(gate, detail.versions[gate]);
    const next = cur.includes(+v) ? cur.filter((x) => x !== +v) : [...cur, +v].slice(-2);
    verSel[key] = next.length ? next : [+v];
    redraw();
  }));
  document.querySelectorAll("#thread [data-cover]").forEach((s) => (s.onclick = () => { const [run, which] = s.dataset.cover.split(":"); verSel[`${current}:cover:${run}`] = which; redraw(); }));
  document.querySelectorAll("#thread [data-win]").forEach((a) => (a.onclick = () => {
    const [gate, v, w] = a.dataset.win.split(":");
    window.open(`/review?episode=${current}&gate=${gate}&v=${v}${w ? `&cmp=${w}` : ""}`, "g-review", "width=1280,height=800");
  }));
  document.querySelectorAll("#thread [data-otherlib]").forEach((b) => (b.onclick = async () => {
    const d = b.dataset.otherlib;
    try { await api(`/api/episodes/${current}/sourcing/other_library/${d}`, { method: "POST" }); toast(d === "allow" ? "好，找素材会从素材库补其他站的画面" : "好，就用现有素材接着做"); } catch (e) { toast(e.message); }
    loadDetail(); loadSessions();
  }));
  document.querySelectorAll("#thread [data-resume]").forEach((b) => (b.onclick = async () => {
    try { await api(`/api/episodes/${current}/action/resume`, { method: "POST" }); toast("已继续，失败的步骤会重试"); } catch (e) { toast(e.message); }
    loadDetail(); loadSessions();
  }));
}
