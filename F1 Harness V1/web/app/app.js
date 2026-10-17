"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const k = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0));
const money = (u) => (u == null ? "—" : u.toFixed(2));

let boot = null, current = null, detail = null, es = null, typingEl = null, pickFor = null, pickProj = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Hermes": "1", ...(opts.headers || {}) } });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.status === 204 ? null : res.json();
}
function toast(text) { const t = $("#toast"); t.textContent = text; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 4000); }

// ---------- 模型与推理强度：闲聊会话一直保留手动选择；立项后的项目会话只对当前这条消息生效 ----------
const isProject = () => !!detail?.episode?.code;
const pickKey = () => `hermes.pick.${current}`;
function fillModels() {
  $("#model").innerHTML = boot.models.map((m) => `<option value="${esc(m.slug)}">${esc(m.name)}</option>`).join("");
  $("#model").onchange = () => { fillEfforts($("#effort").value); pickChanged(); };
  $("#effort").onchange = pickChanged;
  resetDefault();
}
function pickChanged() {
  if (current && !isProject()) localStorage.setItem(pickKey(), JSON.stringify({ model: $("#model").value, effort: $("#effort").value }));
  markScope();
}
function applyPick() {
  const proj = isProject();
  if (pickFor === current && pickProj === proj) return;
  pickFor = current; pickProj = proj;
  const saved = proj ? null : JSON.parse(localStorage.getItem(pickKey()) || "null");
  if (proj) localStorage.removeItem(pickKey());
  if (saved && boot.models.some((m) => m.slug === saved.model)) { $("#model").value = saved.model; fillEfforts(saved.effort); markScope(); }
  else resetDefault();
}
function fillEfforts(keep) {
  const m = boot.models.find((x) => x.slug === $("#model").value);
  const list = m ? m.efforts : ["high"];
  $("#effort").innerHTML = list.map((e) => `<option value="${e}">${esc(boot.effort_labels[e] || e)}</option>`).join("");
  $("#effort").value = list.includes(keep) ? keep : (list.includes("high") ? "high" : list[0]);
}
function resetDefault() { $("#model").value = boot.default.model; fillEfforts(boot.default.effort); markScope(); }
function markScope() {
  const changed = $("#model").value !== boot.default.model || $("#effort").value !== boot.default.effort;
  const d = `${boot.default.model} · ${boot.effort_labels[boot.default.effort]}`;
  $("#scope").textContent = !changed ? `默认 ${d}` : isProject() ? `只对这条消息生效，发送后回到 ${d}` : "会一直用这个，直到你手动改";
  $("#scope").style.color = changed ? "var(--primary)" : "";
}

// ---------- 侧栏 ----------
const dotOf = (s) => (s.endsWith("待审") || ["需人工", "待发布"].includes(s) ? "review" : ["排队", "暂停"].includes(s) ? "queue" : ["归档倒计时", "本地已清理", "已取消"].includes(s) ? "done" : s === "闲聊" ? "" : "work");
function metaOf(r) {
  if (r.state === "排队" && r.queue_pos) return `第 ${r.queue_pos}`;
  if (r.deadline_at) { const h = Math.round((new Date(r.deadline_at) - Date.now()) / 3.6e6); if (h >= 0) return `<span class="warn">剩 ${h} 小时</span>`; }
  return r.state === "闲聊" ? "" : esc(r.state.replace("待审", ""));
}
async function loadSessions() {
  const groups = await api("/api/sessions");
  $("#groups").innerHTML = groups.filter((g) => g.items.length).map((g) => `<div class="group"><h6>${esc(g.name)} <span>${g.items.length}</span></h6>${g.items.map((r) =>
    `<button class="row ${r.id === current ? "on" : ""}" data-id="${r.id}">${dotOf(r.state) ? `<span class="dot ${dotOf(r.state)}"></span>` : ""}<span class="t">${esc(r.title)}</span><span class="meta">${metaOf(r)}</span></button>`).join("")}</div>`).join("");
  document.querySelectorAll("#groups .row").forEach((b) => (b.onclick = () => openSession(+b.dataset.id)));
  return groups;
}

// ---------- 会话 ----------
async function openSession(id) {
  current = id;
  showPage("chat");
  await Promise.all([loadDetail(), loadSessions(), loadTopbar()]);
  connectStream();
}
async function loadDetail() {
  if (!current) return;
  detail = await api(`/api/sessions/${current}`);
  const g = detail.gates.filter((x) => x.state === "pending").pop();
  const want = (g && ["G2", "G3"].includes(g.gate)) || showResult(detail);
  detail.review = want ? await api(`/api/episodes/${current}/review`).catch(() => null) : null;
  applyPick(); renderHead(); renderThread(); renderStatus();
}
// 重跑找素材：G2/G3 待审，或停在配素材到合成之间的暂停、需人工（和 episodes.RESOURCE_FROM 一致）
const canResource = (ep) => ["G2待审", "G3待审"].includes(ep.state)
  || (["暂停", "需人工"].includes(ep.state) && ["配素材与粗剪", "G2待审", "G3待审", "合成与QA"].includes(ep.paused_from));
function renderHead() {
  const ep = detail.episode, flow = detail.flow.filter((s) => !["排队", "归档倒计时", "本地已清理"].includes(s));
  const idx = flow.indexOf(["暂停", "需人工"].includes(ep.state) ? ep.paused_from : ep.state);
  const steps = ep.code ? flow.map((s, i) => {
    const name = s.replace("待审", "").replace("选题与资料", "选题").replace("配素材与粗剪", "粗剪").replace("合成与QA", "合成").replace("写逐字稿", "文案").replace("上传Drive", "上传").replace("经验总结", "经验");
    return i < idx ? `<span class="ok"><svg class="i"><use href="#check"/></svg>${esc(name)}</span>` : i === idx ? `<span class="now">● ${esc(s)}</span>` : `<span>${esc(name)}</span>`;
  }).join("<em></em>") : "";
  const btn = (a, icon, label) => `<button class="ghost" data-act="${a}"><svg class="i"><use href="#${icon}"/></svg>${label}</button>`;
  const acts = ep.code && !["已取消", "本地已清理"].includes(ep.state)
    ? (["暂停", "需人工"].includes(ep.state) ? btn("resume", "up", "继续") : btn("pause", "pause", "暂停")) + (ep.state === "排队" ? btn("jump", "up", "插队") : "") + (canResource(ep) ? btn("resource", "photo", "重跑找素材") : "") + btn("cancel", "x", "取消") : "";
  $("#ephead").innerHTML = `<div><div class="code">${esc(ep.code || "闲聊 · 还没立项")}${ep.kind ? " · " + esc(ep.kind) : ""}</div><h1>${esc(ep.title)}</h1></div><div class="steps">${steps}</div><div class="actions">${acts}</div>`;
  document.querySelectorAll("#ephead [data-act]").forEach((b) => (b.onclick = async () => {
    // 取消需要再点一次确认（不用浏览器原生弹窗）
    if (b.dataset.act === "cancel" && !b.dataset.armed) { b.dataset.armed = "1"; b.lastChild.textContent = "再点一次确认取消"; setTimeout(() => { delete b.dataset.armed; b.lastChild.textContent = "取消"; }, 3000); return; }
    // 重跑找素材会联网花 token，同样点两次；审核卡片里写的意见一起带上（写明换哪段、换成什么）
    if (b.dataset.act === "resource" && !b.dataset.armed) { b.dataset.armed = "1"; b.lastChild.textContent = "再点一次确认重找"; setTimeout(() => { delete b.dataset.armed; b.lastChild.textContent = "重跑找素材"; }, 3000); return; }
    if (b.dataset.act === "resource") {
      try { await api(`/api/episodes/${current}/resource`, { method: "POST", body: JSON.stringify({ note: ($("#gateNote") || {}).value || "" }) }); toast("已退回配素材与粗剪，这一轮会联网重找素材"); } catch (e) { toast(e.message); }
      loadDetail(); loadSessions(); return;
    }
    try { await api(`/api/episodes/${current}/action/${b.dataset.act}`, { method: "POST" }); } catch (e) { toast(e.message); }
    loadDetail(); loadSessions();
  }));
}
function msgHtml(m) {
  if (m.role === "card") return cardHtml(m);
  if (m.role === "user") return `<div class="msg user"><div class="bubble text">${esc(m.content)}</div></div>`;
  if (m.role === "system") return `<div class="msg system"><div class="text">${esc(m.content)}</div></div>`;
  const chip = m.model ? `<span class="chip">${esc(m.model)} · ${esc(boot.effort_labels[m.effort] || m.effort || "")}</span>` : "";
  return `<div class="msg"><div class="who">总控 ${chip}</div><div class="text">${esc(m.content)}</div></div>`;
}
const mediaUrl = (name) => `/api/episodes/${current}/media/${encodeURIComponent(name)}?v=${((detail && detail.review) || {}).media?.[name] || ""}`;
function listBlock(title, rows, okText) {
  if (!rows.length) return okText ? `<div class="sub" style="margin:8px 0 0">${esc(okText)}</div>` : "";
  return `<div class="checks"><h6>${esc(title)} · ${rows.length}</h6>${rows.map(([a, b, c]) => `<div class="${c}"><b>${esc(a)}</b>${esc(b || "")}</div>`).join("")}</div>`;
}
// 配音和粗剪在上面的配音卡片、粗剪卡片里播，这里只列检查结果（第 3 批）
function g2Body() {
  const r = detail.review || {}, v = r.voice || {};
  const bad = (r.rough_checks || []).filter((c) => !c.ok);
  return `<div class="sub" style="margin:10px 0 0">粗剪预览是低清的，只看节奏和画面选择。</div>
    ${listBlock("配音提醒", (v.issues || []).map((i) => [`${dur(i.at)} ${i.kind}`, i.detail, "warn"]), "配音没发现读错、停顿和语速问题")}
    ${listBlock("粗剪规则检查", bad.map((c) => [c.name, c.detail, c.hard ? "bad" : "warn"]), "粗剪规则检查全部通过")}
    ${bad.some((c) => c.name === "普通比赛画面占比") ? `<div class="sub" style="margin:6px 0 0">普通比赛画面超过 45%；名场面多、确实要这么多，可以认可后通过 <button class="btn soft" id="raceShareOk">认可超比例</button></div>` : ""}`;
}
function g3Body() {
  const h = (detail.review || {}).hard || {}, bad = (h.items || []).filter((i) => !i.ok);
  const ai = (detail.review || {}).ai || {};
  const head = !h.items ? "硬检查结果还没出来" : h.passed ? `硬检查全部通过（${h.items.length} 项）` : `硬检查有 ${bad.filter((i) => i.hard).length} 项没过`;
  const rw = ((detail.review || {}).qa || {}).rework || {}, hist = rw.history || [];
  const aiText = ai.total == null ? "" : `；AI 评审 ${ai.total} 分${ai.below_baseline ? `，低于基线 ${ai.baseline}` : ""}，问题 ${(ai.issues || []).length} 条`
    + (hist.length > 1 ? `；分数 ${hist.join(" → ")}` : "") + (rw.note ? `；${rw.note}` : "")
    + ((detail.review || {}).recalibrate || []).map((a) => `；需要重新校准（你 ${a.user} 分 / AI ${a.ai} 分）`).join("");
  return `<div class="sub" style="margin:10px 0 0">${esc(head + aiText)}。全部检查项在独立窗口里看。</div>
    ${listBlock("没过的项", bad.map((i) => [i.name, i.detail, i.hard ? "bad" : "warn"]))}`;
}
// 待审关卡的检查结果、意见框和按钮，放在这一关的卡片里（cards.js 的 gateStageCard）
function gateActions(gate) {
  const targets = detail.reject_targets[gate] || [];
  const win = gate === "G3" ? `<button class="ghost" id="openReview"><svg class="i"><use href="#win"/></svg>在独立窗口看成片和全部检查项</button>` : "";
  const body = (gate === "G2" ? g2Body() : gate === "G3" ? g3Body() : "") + win
    + `<textarea class="gate-note" id="gateNote" rows="2" placeholder="意见（打回时必填；点上方“重跑找素材”时写明换哪段、换成什么）"></textarea>`
    + (gate === "G3" ? `<div class="sub" style="margin:6px 0 0">你的打分（可选，用于校准 AI） <input type="number" id="gateScore" min="0" max="100" placeholder="0–100" style="width:80px"></div>` : "");
  return { body, below: `<div class="below"><button class="btn primary" id="gateOk">通过</button>${targets.map((t) => `<button class="btn soft" data-back="${esc(t)}">打回到${esc(t)}</button>`).join("")}</div>` };
}
// 兜底：待审关卡没有卡片消息时（不该出现）单独画一张
function gateCard() {
  const g = detail.gates.filter((x) => x.state === "pending").pop();
  if (!g || detail.messages.some((m) => m.card && m.card.type === "gate" && m.card.gate_id === g.id)) return "";
  const a = gateActions(g.gate);
  return `<div class="msg"><div class="widget"><h5>${esc(g.gate)} 等你审核</h5>${a.body}</div>${a.below}</div>`;
}
function renderThread() {
  const msgs = detail.messages;
  $("#thread").innerHTML = (msgs.length ? msgs.map(msgHtml).join("") : `<div class="empty"><h3>想做点什么？</h3>比如"我想做一期资讯"，或者先聊聊这周的新闻。</div>`) + gateCard() + reuseCards() + resultCard() + archiveCard();
  bindThreadCards(); bindCards();
  // G2：普通比赛画面超比例由用户认可（2026-10-14 用户定）
  const rs = $("#raceShareOk");
  if (rs) rs.onclick = async () => {
    try { const r = await api(`/api/episodes/${current}/race_share/approve`, { method: "POST" }); toast(`已认可普通比赛画面占 ${Math.round(r.share * 100)}%`); } catch (e) { toast(e.message); }
    loadDetail();
  };
  const ok = $("#gateOk");
  if (ok) {
    const gate = detail.gates.filter((x) => x.state === "pending").pop().gate;
    ok.onclick = () => decide(gate, "approve");
    document.querySelectorAll("[data-back]").forEach((b) => (b.onclick = () => decide(gate, "reject", b.dataset.back)));
    const w = $("#openReview"); if (w) w.onclick = () => window.open(`/review?episode=${current}&gate=${gate}`, "g-review", "width=1280,height=800");
  }
  typingEl = null;
  $("#thread").scrollTop = $("#thread").scrollHeight;
}
async function decide(gate, decision, backTo) {
  const note = $("#gateNote").value.trim();
  if (decision === "reject" && !note) return toast("打回时请写意见");
  // 通过时写的备注只留记录、不会执行：先提醒一次，再点才通过（2026-10-16 第 21 期）
  const ok = $("#gateOk");
  if (decision === "approve" && note && ok && !ok.dataset.sure) {
    ok.dataset.sure = "1"; ok.textContent = "仍然通过（备注不执行）";
    return toast("通过时写的备注不会执行，只留作记录；要按备注改，请点“打回”");
  }
  const score = ($("#gateScore") || {}).value || "";
  try { const r = await api(`/api/episodes/${current}/gates/${gate}/${decision}`, { method: "POST", body: JSON.stringify({ feedback: note, back_to: backTo, score }) }); if ((r.notes || []).length) toast(r.notes.join(" ")); }
  catch (e) { toast(e.message); }
  loadDetail(); loadSessions();
}
function renderStatus() {
  const rows = detail.tasks.map((t) => `<li>${t.state === "running" ? '<span class="spin"></span>' : t.state === "succeeded" ? '<svg class="i" style="color:var(--green)"><use href="#check"/></svg>' : '<span class="dot queue"></span>'}${esc(t.step)} · ${esc(t.model)}${t.attempts > 1 ? ` · 第 ${t.attempts} 次` : ""}</li>`);
  $("#statusTitle").textContent = rows.length ? `本期任务 · ${detail.tasks.filter((t) => t.state === "succeeded").length}/${rows.length}` : "本期暂无后台任务";
  $("#statusList").innerHTML = rows.join("");
}

// ---------- 发送 ----------
async function send() {
  const text = $("#input").value.trim();
  if (!text || !current) return;
  if (text.startsWith("/")) return runSlash(text);
  $("#input").value = ""; $("#slash").classList.remove("on");
  const body = { text, model: $("#model").value, effort: $("#effort").value };
  if (isProject()) resetDefault();
  try { await api(`/api/sessions/${current}/messages`, { method: "POST", body: JSON.stringify(body) }); } catch (e) { toast(e.message); }
}
const SLASH = [["/model", "换模型，如 /model gpt-6-sol"], ["/effort", "换推理强度，如 /effort 极高"], ["/pause", "暂停本期"], ["/resume", "继续本期（含重试失败步骤）"], ["/jump", "插队到最前"], ["/cancel", "取消本期"], ["/status", "查看本期进度"], ["/new", "新建会话"]];
async function runSlash(text) {
  const [cmd, arg] = text.split(/\s+/, 2);
  $("#input").value = ""; $("#slash").classList.remove("on");
  if (cmd === "/model") { if (boot.models.some((m) => m.slug === arg)) { $("#model").value = arg; fillEfforts($("#effort").value); pickChanged(); } else toast("没有这个模型"); return; }
  if (cmd === "/effort") { const v = Object.entries(boot.effort_labels).find(([key, label]) => key === arg || label === arg); if (v && [...$("#effort").options].some((o) => o.value === v[0])) { $("#effort").value = v[0]; pickChanged(); } else toast("这个模型没有该推理强度"); return; }
  if (cmd === "/new") return newSession();
  if (cmd === "/status") { $("#status").classList.remove("fold"); return; }
  const act = { "/pause": "pause", "/resume": "resume", "/retry": "resume", "/jump": "jump", "/cancel": "cancel" }[cmd];
  if (!act) return toast("不认识的命令");
  try { await api(`/api/episodes/${current}/action/${act}`, { method: "POST" }); } catch (e) { toast(e.message); }
  loadDetail(); loadSessions();
}
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("input", (e) => {
  const v = e.target.value;
  $("#slash").innerHTML = SLASH.filter(([c]) => c.startsWith(v.split(" ")[0])).map(([c, d]) => `<div data-c="${c}"><code>${c}</code>${esc(d)}</div>`).join("");
  $("#slash").classList.toggle("on", v.startsWith("/") && !v.includes(" ") && $("#slash").innerHTML !== "");
  document.querySelectorAll("#slash [data-c]").forEach((d) => (d.onclick = () => { $("#input").value = d.dataset.c + " "; $("#slash").classList.remove("on"); $("#input").focus(); }));
});
$("#send").onclick = send;
$("#status .hd").onclick = () => $("#status").classList.toggle("fold");

// ---------- 实时推送 ----------
function connectStream() {
  if (es) es.close();
  es = new EventSource(current ? `/api/stream?session=${current}` : "/api/stream");
  es.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.role === "user" || m.role === "system" || m.role === "assistant") { if (m.role === "assistant" && typingEl) { typingEl.remove(); typingEl = null; } $(".empty")?.remove(); $("#thread").insertAdjacentHTML("beforeend", msgHtml(m)); $("#thread").scrollTop = $("#thread").scrollHeight; } });
  es.addEventListener("assistant_start", (e) => { const d = JSON.parse(e.data); $("#thread").insertAdjacentHTML("beforeend", `<div class="msg" id="typing"><div class="who">总控 <span class="chip">${esc(d.model)} · ${esc(boot.effort_labels[d.effort] || d.effort)}</span></div><div class="text typing"></div></div>`); typingEl = $("#typing"); });
  es.addEventListener("delta", (e) => { if (!typingEl) return; typingEl.querySelector(".text").textContent += JSON.parse(e.data).text; $("#thread").scrollTop = $("#thread").scrollHeight; });
  es.addEventListener("assistant_end", () => { typingEl?.remove(); typingEl = null; loadTopbar(); });
  es.addEventListener("refresh", (e) => { loadSessions(); if (JSON.parse(e.data).episode_id === current) loadDetail(); });
  es.addEventListener("topbar", loadTopbar);
  es.addEventListener("data", (e) => {
    const d = JSON.parse(e.data || "{}");
    if ($("#data").classList.contains("on")) loadData();
    if (d.kind === "assets" && $("#assets").classList.contains("on") && !$("#assetQ").value.trim()) loadAssets();
    if (d.kind === "accounts" && $("#settings").classList.contains("on")) loadAccounts();
    loadBadge();
  });
  es.addEventListener("notify", (e) => {
    const d = JSON.parse(e.data);
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification(d.title, { body: d.body }); n.onclick = () => { window.focus(); if (d.episode_id) openSession(d.episode_id); };
    } else toast(`${d.title}：${d.body}`);
  });
}

// ---------- 顶部栏 ----------
async function loadTopbar() {
  const t = await api(`/api/topbar${current ? "?session=" + current : ""}`);
  const q = t.quota ? `<span class="quota">额度 ${t.quota.window_days === 7 ? "本周" : ""}剩 <b>${t.quota.remaining}%</b><span class="bar"><i style="width:${t.quota.remaining}%"></i></span>${esc(t.quota.resets || "")}重置</span>` : `<span class="quota">额度 读取中</span>`;
  const cny = (u) => (u.cny == null ? "—" : u.cny.toFixed(2));
  const s = t.session ? `<span>本对话 输入 <b>${k(t.session.input)}</b> · 缓存 ${k(t.session.cached)} · 输出 <b>${k(t.session.output)}</b> · 推理 ${k(t.session.reasoning)} ≈ <b>$${money(t.session.usd)} / ¥${cny(t.session)}</b></span>` : "";
  const fx = t.fx.rate ? `汇率 ${t.fx.rate}（${esc(t.fx.date)}）` : "汇率获取中";
  $("#topbar").innerHTML = `${q}<span class="sep"></span>${s}${s ? '<span class="sep"></span>' : ""}<span>今日 ≈ <b>$${money(t.today.usd)} / ¥${cny(t.today)}</b></span><span style="margin-left:auto" class="hint">金额按 API 公开价折算 · ${fx}</span>`;
}

// ---------- 页面切换 / 用量 / 设置 ----------
function showPage(id) {
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("on", p.id === id));
  document.querySelectorAll(".nav").forEach((n) => n.classList.toggle("on", n.dataset.page === id));
  if (id === "usage") loadUsage();
  if (id === "data") loadData();
  if (id === "glossary") loadGlossary();
  if (id === "bench") loadBench();
  if (id === "notes") loadNotes();
  if (id === "assets") { loadDownloads(); loadAssets(); }
  if (!es) connectStream();
}
document.querySelectorAll(".nav[data-page]").forEach((b) => (b.onclick = () => { current = null; loadSessions(); showPage(b.dataset.page); }));
document.querySelectorAll("#usagePeriod button").forEach((b) => (b.onclick = () => { document.querySelectorAll("#usagePeriod button").forEach((x) => x.classList.toggle("on", x === b)); loadUsage(b.dataset.p); }));
async function loadUsage(period = "day") {
  const u = await api(`/api/usage?period=${period}`), rate = u.fx.rate;
  $("#usageSub").textContent = `金额按 OpenAI API 公开价折算，仅用于比较成本；订阅不按 token 扣费。合计 ≈ $${money(u.total.usd)}${rate ? " / ¥" + (u.total.usd * rate).toFixed(2) : ""}${u.total.unpriced.length ? "（无公开价：" + u.total.unpriced.join("、") + "）" : ""}`;
  const H = u.hours, max = Math.max(1, ...H.map((h) => h.input)), X = (h) => 40 + h * 28, Y = (v) => 180 - (v / max) * 160;
  const line = (key, c) => `<polyline fill="none" stroke="${c}" stroke-width="1.8" points="${H.map((h) => `${X(h.hour)},${Y(h[key])}`).join(" ")}"/>`;
  $("#chartSvg").innerHTML = H.filter((h) => h.hour % 3 === 0).map((h) => `<text x="${X(h.hour)}" y="196" font-size="10" fill="currentColor" opacity=".5" text-anchor="middle">${h.hour}:00</text>`).join("")
    + `<line x1="40" y1="180" x2="${X(23)}" y2="180" stroke="currentColor" opacity=".15"/>` + line("input", "var(--primary)") + line("output", "var(--warm)") + line("reasoning", "var(--green)");
  const rows = H.filter((h) => h.input || h.output);
  $("#usageRows").innerHTML = rows.length ? rows.map((h) => `<tr><td>${String(h.hour).padStart(2, "0")}:00</td><td>${k(h.input)}</td><td>${k(h.cached)}</td><td>${k(h.output)}</td><td>${k(h.reasoning)}</td><td>$${h.usd.toFixed(3)}</td><td>${rate ? "¥" + (h.usd * rate).toFixed(2) : "—"}</td></tr>`).join("") : `<tr><td colspan="7">这段时间还没有用量</td></tr>`;
}
$("#openSettings").onclick = () => { $("#settings").classList.add("on"); loadAccounts(); };
$("#closeSettings").onclick = () => { $("#settings").classList.remove("on"); clearTimeout(qrTimer); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#settings").classList.remove("on"); $("#assetPanel").classList.remove("on"); clearTimeout(qrTimer); } });

async function newSession() { const r = await api("/api/sessions", { method: "POST" }); await openSession(r.id); $("#input").focus(); }
$("#newSession").onclick = newSession;

// ---------- 赛历与数据 ----------
const WEEK = "日一二三四五六";
const SESSION_ZH = { "Practice 1": "一练", "Practice 2": "二练", "Practice 3": "三练", Qualifying: "排位", "Sprint Qualifying": "冲刺排位", "Sprint Shootout": "冲刺排位", Sprint: "冲刺赛", Race: "正赛" };
const FORMAT_ZH = { conventional: "常规", sprint_qualifying: "冲刺", sprint_shootout: "冲刺", sprint: "冲刺" };
const bjTime = (iso) => { if (!iso) return "—"; const d = new Date(iso), p = (n) => String(n).padStart(2, "0"); const b = new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000); return `${b.getMonth() + 1}月${b.getDate()}日 ${p(b.getHours())}:${p(b.getMinutes())}`; };
const ago = (iso) => { if (!iso) return "还没运行"; const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : bjTime(iso); };
const every = (s) => (s >= 86400 ? "每天" : s >= 7200 ? "每 2 小时" : "每小时");
let calSeason = null;

async function loadData() {
  const o = await api("/api/data/overview"), d = new Date(o.today + "T00:00:00");
  const run = (n) => o.running.includes(n);
  $("#dataSub").textContent = `今天 ${d.getMonth() + 1}月${d.getDate()}日 周${WEEK[d.getDay()]} · ${o.period === "比赛周" ? o.label + " 比赛周" : o.period}${o.race_day ? " · 今天是比赛日" : ""} · 文件夹名里写「${o.label}」`;
  const card = (name, title, lines, btn) => `<div class="card"><h4>${title}</h4><div class="sub">${lines}</div><button class="btn soft" data-run="${name}" ${run(name) ? "disabled" : ""}>${run(name) ? '<span class="spin"></span> 进行中' : btn}</button></div>`;
  $("#dataCards").innerHTML =
    card("calendar", "赛历", `FastF1 官方赛历，每天 06:00 刷新一次，有变动就通知你<br>上次：${ago(o.calendar_at)}`, "立即刷新")
    + card("fia", "FIA 文件", `当前${every(o.fia_interval)}查一次（按频率表）<br>上次：${ago(o.fia_at)}`, "立即检查")
    + card("news", "每日前三推荐", `每天 08:00 由 GPT 联网抓新闻并挑前三（一次约 $1）<br>最近：${o.news_done || "还没生成"}`, "立即生成");
  document.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    // 生成推荐要花额度，再点一次确认
    if (b.dataset.run === "news" && !b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "再点一次确认（会用额度）"; setTimeout(() => { delete b.dataset.armed; b.textContent = "立即生成"; }, 3000); return; }
    await api(`/api/data/run/${b.dataset.run}`, { method: "POST" }); toast("已开始，完成后会自动刷新"); loadData();
  }));
  const [cal, fia, news] = await Promise.all([api(`/api/data/calendar${calSeason ? "?season=" + calSeason : ""}`), api("/api/data/fia"), api("/api/data/news")]);
  calSeason = cal.season;
  $("#newsPicks").innerHTML = news.picks.length ? news.picks.map((p) => `<div class="pick"><b>${p.rank}. ${esc(p.headline)}</b><span class="chip">${esc(p.branch)}</span><div>${esc(p.reason)}</div><div><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.source || "原文")}</a></div></div>`).join("")
    : `<div class="sub">还没有推荐，每天 08:00 自动生成。</div>`;
  $("#seasonSeg").innerHTML = cal.seasons.map((s) => `<button data-y="${s}" class="${s === cal.season ? "on" : ""}">${s}</button>`).join("");
  document.querySelectorAll("#seasonSeg button").forEach((b) => (b.onclick = () => { calSeason = +b.dataset.y; loadData(); }));
  $("#calRows").innerHTML = cal.events.length ? cal.events.map((e) => {
    const race = e.sessions.find((s) => s.name === "Race");
    const detail = e.sessions.map((s) => `${SESSION_ZH[s.name] || esc(s.name)} ${bjTime(s.start_utc)}`).join(" · ");
    return `<tr class="click ${e.round === cal.current_round ? "now" : ""}" data-r="${e.round}"><td>${e.round}</td><td>${esc(e.name_zh || e.name_en)}<span class="src">${e.name_zh ? esc(e.name_en) : ""}</span></td><td>${esc(e.location_zh || e.location)}</td><td>${FORMAT_ZH[e.format] || esc(e.format)}</td><td>${race ? bjTime(race.start_utc) : esc(e.event_date)}</td></tr><tr class="detail" data-d="${e.round}" hidden><td></td><td colspan="4">${detail}</td></tr>`;
  }).join("") : `<tr><td colspan="5">赛历还没载入，点上面“立即刷新”</td></tr>`;
  document.querySelectorAll("#calRows tr.click").forEach((tr) => (tr.onclick = () => { const d = $(`#calRows tr[data-d="${tr.dataset.r}"]`); d.hidden = !d.hidden; }));
  $("#calChanges").innerHTML = cal.changes.length ? cal.changes.map((c) => `<li>${bjTime(c.detected_at)} · ${esc(c.detail)}</li>`).join("") : "<li>还没有发现变动</li>";
  $("#fiaRows").innerHTML = fia.docs.length ? fia.docs.slice(0, 40).map((f) => `<tr><td>${bjTime(f.published_at || f.first_seen_at)}</td><td>${esc(f.event || "")}</td><td><a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.title)}</a></td></tr>`).join("")
    : `<tr><td colspan="3">还没抓过，点上面“立即检查”</td></tr>`;
}

// ---------- 素材库（M4） ----------
const dur = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
const parseTags = (t) => { try { const v = typeof t === "string" ? JSON.parse(t) : t; return Array.isArray(v) ? v : []; } catch { return []; } };
let assetTags = new Set(), dlTimer = null, mainTagList = [];
const GROUP_ORDER = ["主标签", "子标签", "赛段", "镜头类型", "车队", "车手", "赛道"];
const layerChips = (L) => [...(L["主标签"] || []).map((t) => `<span class="tg main">${esc(t)}</span>`), ...(L["子标签"] || []).map((t) => `<span class="tg sub">${esc(t)}</span>`)].join("");
async function loadDownloads() {
  const d = await api("/api/downloads");
  const st = { queued: "排队", running: "下载中", done: "完成", failed: "失败" };
  $("#dlList").innerHTML = d.items.slice(0, 8).map((x) => `<li>${esc(st[x.state] || x.state)}${x.state === "running" ? `<span class="prog"><i style="width:${Math.round((x.progress || 0) * 100)}%"></i></span>` : " · "}<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url.slice(0, 80))}</a>${x.error ? ` <span class="warnchip">${esc(x.error.slice(-80))}</span>` : ""}</li>`).join("");
  clearTimeout(dlTimer);
  if (d.items.some((x) => ["queued", "running"].includes(x.state)) && $("#assets").classList.contains("on")) dlTimer = setTimeout(() => { loadDownloads(); loadAssets(); }, 3000);
}
$("#dlAdd").onclick = async () => {
  const url = $("#dlUrl").value.trim(); if (!url) return;
  try { await api("/api/downloads", { method: "POST", body: JSON.stringify({ url }) }); $("#dlUrl").value = ""; toast("已加入下载队列"); loadDownloads(); } catch (e) { toast(e.message); }
};
async function loadTagBar() {
  const g = await api("/api/assets/tag_groups");
  mainTagList = g.main_tags;
  $("#tagBar").innerHTML = GROUP_ORDER.filter((k) => g.groups[k]).map((k) => `<div class="tagrow"><span class="tglabel">${k}</span>${g.groups[k].map(([t, n]) => `<button class="${assetTags.has(t) ? "on" : ""}" data-t="${esc(t)}">${esc(t)} <i>${n}</i></button>`).join("")}</div>`).join("");
  document.querySelectorAll("#tagBar button").forEach((b) => (b.onclick = () => { const t = b.dataset.t; assetTags.has(t) ? assetTags.delete(t) : assetTags.add(t); loadAssets(); }));
}
async function loadAssets() {
  const q = $("#assetQ").value.trim();
  const params = new URLSearchParams({ ...(assetTags.size && { tags: [...assetTags].join(",") }), ...(q && { q }) });
  $("#assetGrid").innerHTML = q ? `<div class="sub">正在按画面搜索…（首次要加载模型，约 5 秒）</div>` : "";
  loadTagBar();
  const r = await api(`/api/assets?${params}`);
  if (r.shots) {
    const head = assetTags.size ? `<div class="sub" style="grid-column:1/-1">同时带有「${[...assetTags].map(esc).join("」「")}」的镜头：${r.shots.length}${r.shots.length >= 300 ? "+" : ""} 个</div>` : "";
    $("#assetGrid").innerHTML = r.shots.length ? head + r.shots.map((s) => `<div class="acard" data-a="${s.asset_id}" data-i="${s.shot_idx}"><img src="/api/assets/${s.asset_id}/shots/${s.shot_idx}" alt="" loading="lazy"><div class="meta2"><b>${esc(s.title || "素材 " + s.asset_id)}</b>镜头 #${s.shot_idx} · ${dur(s.start_s)}–${dur(s.end_s)}${s.score != null ? ` · 相似度 ${s.score.toFixed(2)}` : ""}${s.tag_hit ? " · 标签命中" : ""}<div>${s.layers ? layerChips(s.layers) : ""}</div></div></div>`).join("") : `<div class="sub">没有找到符合的镜头</div>`;
  } else {
    const all = r.assets;
    const stateZh = { processing: "处理中", failed: "入库失败", ready: "" };
    $("#assetGrid").innerHTML = all.length ? all.map((a) => `<div class="acard" data-a="${a.id}"><img src="/api/assets/${a.id}/thumb" alt="" loading="lazy" onerror="this.style.visibility='hidden'"><div class="meta2"><b>${esc(a.title || "素材 " + a.id)}</b>${dur(a.duration)} · ${a.width || "?"}×${a.height || "?"}${stateZh[a.state] ? " · " + stateZh[a.state] : ""}<div>${a.has_logo ? '<span class="warnchip">带台标</span>' : ""}${a.has_subtitles ? '<span class="warnchip">自带字幕</span>' : ""}${a.drive_path ? '<span class="tg">已上 Drive</span>' : ""}</div><div>${parseTags(a.tags).slice(0, 6).map((t) => `<span class="tg">${esc(t)}</span>`).join("")}</div></div></div>`).join("") : `<div class="sub">素材库还是空的，贴一个链接试试。</div>`;
  }
  document.querySelectorAll("#assetGrid .acard").forEach((c) => (c.onclick = () => openAsset(+c.dataset.a, c.dataset.i != null ? +c.dataset.i : null)));
}
$("#assetSearch").onclick = loadAssets;
$("#assetQ").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) loadAssets(); });
$("#assetClear").onclick = () => { $("#assetQ").value = ""; assetTags.clear(); loadAssets(); };
const shotCaption = (aid, s) => {
  const L = s.layers || {};
  const mains = (L["主标签"] || []).map((t) => `<span class="tg main">${esc(t)}</span>`).join("");
  const others = [...(L["子标签"] || []).map((t) => `<span class="tg sub">${esc(t)}</span>`), ...[...(L["镜头类型"] || []), ...(L["车手"] || []), ...(L["车队"] || [])].map((t) => `<span class="tg">${esc(t)}</span>`)].join("");
  return `#${s.idx} ${dur(s.start_s)}–${dur(s.end_s)}<br>${mains}${s.main_manual ? '<span class="handmark">手改</span>' : ""} <button class="linkbtn" data-edit="${s.idx}">改/加主标签</button> <button class="linkbtn" data-unuse>${s.unusable ? "恢复可用" : "标为不可用"}</button>${s.unusable ? '<span class="warnchip">不可用</span>' : ""}<br>${others}<div class="mainpick" hidden></div>`;
};
function editMain(aid, fig, s) {
  const box = fig.querySelector(".mainpick");
  const cur = new Set((s.layers || {})["主标签"] || []);
  box.innerHTML = mainTagList.map((t) => `<button class="${cur.has(t) ? "on" : ""}" data-t="${esc(t)}">${esc(t)}</button>`).join("") + `<div class="pickact"><button class="btn primary" data-act="save">保存</button><button class="ghost" data-act="auto">恢复自动</button><button class="ghost" data-act="cancel">取消</button></div>`;
  box.hidden = false;
  box.querySelectorAll("button[data-t]").forEach((b) => (b.onclick = () => b.classList.toggle("on")));
  box.querySelectorAll("button[data-act]").forEach((b) => (b.onclick = async () => {
    if (b.dataset.act === "cancel") { box.hidden = true; return; }
    const main = b.dataset.act === "auto" ? null : [...box.querySelectorAll("button[data-t].on")].map((x) => x.dataset.t);
    if (main && !main.length) { toast("至少选一个主标签；不想手改就点「恢复自动」"); return; }
    try {
      const shot = await api(`/api/assets/${aid}/shots/${s.idx}/main`, { method: "PUT", body: JSON.stringify({ main }) });
      Object.assign(s, shot);
      fig.querySelector("figcaption").innerHTML = shotCaption(aid, s);
      bindShot(aid, fig, s);
      toast(main ? "已改主标签，以后本机打标会参考这次修改" : "已恢复自动主标签");
      loadTagBar();
    } catch (e) { toast(e.message); }
  }));
}
function bindShot(aid, fig, s) {
  fig.classList.toggle("unusable", !!s.unusable);
  fig.querySelector("[data-edit]").onclick = () => editMain(aid, fig, s);
  fig.querySelector("[data-unuse]").onclick = async () => {
    try {
      const shot = await api(`/api/assets/${aid}/shots/${s.idx}/unusable`, { method: "PUT", body: JSON.stringify({ unusable: !s.unusable }) });
      Object.assign(s, shot);
      fig.querySelector("figcaption").innerHTML = shotCaption(aid, s);
      bindShot(aid, fig, s);
      toast(s.unusable ? "已标为不可用，剪辑和搜镜头都会跳过" : "已恢复可用");
      loadTagBar();
    } catch (e) { toast(e.message); }
  };
}
// 素材身份：哪站 · 哪年 · 赛段 · 类别
const identityText = (a) => [a.gp_event || "站名未知", a.season || "年份未知", a.session || "其他", a.category || "其他"].join(" · ");
async function openAsset(id, focusIdx = null) {
  const a = await api(`/api/assets/${id}`);
  if (!mainTagList.length) mainTagList = (await api("/api/assets/tag_groups")).main_tags;
  $("#assetDetail").innerHTML = `<h2 style="margin:0 0 4px; font-size:16px">${esc(a.title || "素材 " + a.id)}</h2>
    <div class="sub">${esc(a.credit || "")} · ${dur(a.duration)} · ${a.width}×${a.height} · 入库 ${bjTime(a.created_at)}${a.source_url ? ` · <a href="${esc(a.source_url)}" target="_blank" rel="noopener noreferrer">原链接</a>` : ""}${a.drive_path ? " · Drive：" + esc(a.drive_path) : " · 还没上 Drive"}</div>
    <div class="sub">${esc(identityText(a))}${a.identity_manual ? '<span class="handmark">手改</span>' : ""} <button class="linkbtn" id="identityEdit">改</button></div>
    ${a.has_logo ? `<div><span class="warnchip">带其他频道台标</span>${esc(a.logo_note || "")}（能不用就不用，非用不可要裁掉）</div>` : ""}${a.has_subtitles ? '<div><span class="warnchip">自带字幕</span></div>' : ""}
    ${a.tagged_at ? `<div class="sub">标签更新于 ${bjTime(a.tagged_at)} <button class="ghost" data-retag="local">本机重新打标</button> <button class="ghost" data-retag="gpt">用 GPT 重新打标（花 token）</button></div>` : `<div class="sub">还没打标签 <button class="ghost" id="reprocess">重新处理</button></div>`}
    ${a.main_counts && a.main_counts.length ? `<div class="sub">主标签（镜头数）：${a.main_counts.map(([t, n]) => `<span class="tg main">${esc(t)} ${n}</span>`).join("")}</div>` : ""}
    <div class="shots">${a.shots.map((s) => `<figure data-i="${s.idx}"><img src="/api/assets/${a.id}/shots/${s.idx}" alt="" loading="lazy"><figcaption>${shotCaption(a.id, s)}</figcaption></figure>`).join("")}</div>
    <h3>使用记录</h3><ul class="plain">${a.uses.length ? a.uses.map((u) => `<li>${esc(u.episode_code || "")} ${esc(u.episode_title || "")} · ${dur(u.src_in)}–${dur(u.src_out)}${u.cross_episode ? " · 跨期复用" : ""}</li>`).join("") : "<li>还没被用过</li>"}</ul>`;
  $("#identityEdit").onclick = async () => {
    const v = prompt("按“站名 · 年份 · 赛段 · 类别”改（赛段：正赛/排位/冲刺/练习/其他；类别：比赛回放/集锦/车载/车队运营/官方运营/赛道介绍/其他）", identityText(a));
    if (v == null) return;
    const [gp_event, season, session, category] = v.split(/[·•]/).map((x) => x.trim());
    try {
      await api(`/api/assets/${id}/identity`, { method: "PUT", body: JSON.stringify({ gp_event: gp_event === "站名未知" ? "" : gp_event, season: season === "年份未知" ? "" : season, session, category }) });
      toast("已改，找素材按这个认哪站");
      openAsset(id);
    } catch (e) { toast(e.message); }
  };
  const rp = $("#reprocess"); if (rp) rp.onclick = async () => { await api(`/api/assets/${id}/process`, { method: "POST" }); toast("已开始处理"); };
  document.querySelectorAll("#assetDetail [data-retag]").forEach((b) => (b.onclick = async () => {
    const gpt = b.dataset.retag === "gpt";
    if (gpt && !confirm("用 GPT 重新打标会花 token，继续？")) return;
    await api(`/api/assets/${id}/retag?engine=${b.dataset.retag}`, { method: "POST" });
    toast(gpt ? "已开始用 GPT 打标" : "已开始本机打标，大约一两分钟");
  }));
  const byIdx = new Map(a.shots.map((s) => [s.idx, s]));
  document.querySelectorAll("#assetDetail .shots figure").forEach((f) => bindShot(a.id, f, byIdx.get(+f.dataset.i)));
  $("#assetPanel").classList.add("on");
  if (focusIdx != null) {
    const f = document.querySelector(`#assetDetail figure[data-i="${focusIdx}"]`);
    if (f) { f.classList.add("hl"); f.scrollIntoView({ block: "center" }); }
  }
}
$("#closeAsset").onclick = () => $("#assetPanel").classList.remove("on");

// ---------- 账号连接 ----------
async function loadPerms() {
  let p;
  try { p = await api("/api/system/permissions"); } catch (e) { $("#acctPerm").innerHTML = `<span class="name">系统权限</span><span class="state">查询失败：${esc(e.message)}</span>`; return; }
  const ok = p.accessibility && p.screen;
  const s = (b) => (b ? "已开" : "未开");
  $("#acctPerm").innerHTML = `<span class="name">系统权限</span><span class="state">自动操作剪映用：辅助功能${s(p.accessibility)}，屏幕录制${s(p.screen)}${ok ? "" : "。点右边按钮后，在弹出的系统设置里把 F1 Harness 的开关打开，再回来点“重新检查”"}</span>${ok ? "" : '<button class="btn primary" id="permReq">申请权限</button><button class="ghost" id="permRe">重新检查</button>'}`;
  const rq = $("#permReq"); if (rq) rq.onclick = async () => { await api("/api/system/permissions/request", { method: "POST" }); toast("已把 F1 Harness 加进系统设置的列表，请把它的开关打开"); loadPerms(); };
  const re = $("#permRe"); if (re) re.onclick = loadPerms;
}
let qrTimer = null;
async function loadAccounts() {
  loadPerms();
  const dP = api("/api/drive/status");  // Drive 容量要问 Google，较慢，不挡 B 站那一行
  const a = await api("/api/accounts");
  const b = a.bilibili;
  $("#acctBili").innerHTML = `<span class="name">B 站</span><span class="state">${b.logged_in ? `已登录：${esc(b.uname || "")}（到 ${bjTime(b.expires_at)} 过期）` : "未登录（未登录时 B 站视频最高约 480p）"}</span>${b.logged_in ? '<button class="ghost" id="biliOut">退出</button>' : '<button class="btn primary" id="biliIn">扫码登录</button>'}`;
  const out = $("#biliOut"); if (out) out.onclick = async () => { await api("/api/bilibili/logout", { method: "POST" }); loadAccounts(); };
  const bin = $("#biliIn"); if (bin) bin.onclick = startBiliQr;
  for (const [site, sel] of SOCIAL) socialRow(site, sel, a[site]);
  if (!$("#acctDrive").innerHTML) $("#acctDrive").innerHTML = `<span class="name">Google Drive</span><span class="state">查询中…</span>`;
  const d = await dP;
  const gb = (n) => (n >= 1e12 ? (n / 1e12).toFixed(1) + " TB" : (n / 1e9).toFixed(1) + " GB");
  const usage = d.usage ? `，已用 ${gb(d.usage.used)} / ${gb(d.usage.total)}` : "";
  $("#acctDrive").innerHTML = `<span class="name">Google Drive</span><span class="state">${d.configured ? `已连接${usage}` : d.authorizing ? "等待你在浏览器里点同意…" : `未连接${d.last && !d.last.ok ? "（上次失败：" + esc(d.last.error || "") + "）" : ""}`}</span>${d.configured ? "" : `<button class="btn primary" id="driveIn" ${d.authorizing ? "disabled" : ""}>连接 Google Drive</button>`}`;
  const din = $("#driveIn"); if (din) din.onclick = async () => { await api("/api/drive/authorize", { method: "POST" }); toast("浏览器会打开 Google 授权页，点\"允许\"即可"); loadAccounts(); pollDrive(); };
}
async function startBiliQr() {
  try {
    const qr = await api("/api/bilibili/qr", { method: "POST" });
    $("#acctBili").innerHTML = `<span class="name">B 站</span><img src="${qr.img}" alt="B 站登录二维码"><span class="state" id="biliState">用 B 站 App 扫一扫</span>`;
    const poll = async () => {
      const r = await api(`/api/bilibili/qr/${encodeURIComponent(qr.key)}`);
      if (r.state === "ok") { toast("B 站已登录"); return loadAccounts(); }
      if (r.state === "expired") { $("#biliState").textContent = "二维码过期了"; return setTimeout(loadAccounts, 1500); }
      $("#biliState").textContent = r.state === "scanned" ? "已扫码，请在手机上确认" : "用 B 站 App 扫一扫";
      qrTimer = setTimeout(poll, 2000);
    };
    qrTimer = setTimeout(poll, 2000);
  } catch (e) { toast(e.message); }
}
// X / Instagram：本机 Chrome 打开登录页，用户自己登录，服务端取到 cookie 后自动关窗口
const SOCIAL = [["x", "#acctX"], ["instagram", "#acctIg"]];
const SOCIAL_NAME = { x: "X", instagram: "Instagram" };
const socialTimers = {};
function socialRow(site, sel, s) {
  const name = SOCIAL_NAME[site];
  if (!s) return;
  if (s.state === "waiting" && !s.logged_in) {
    $(sel).innerHTML = `<span class="name">${name}</span><span class="state">已打开浏览器窗口，请在窗口里登录（登录后会自动关闭）</span>`;
    return pollSocial(site);
  }
  $(sel).innerHTML = `<span class="name">${name}</span><span class="state">${s.logged_in ? `已登录${s.expires_at ? `（到 ${bjTime(s.expires_at)} 过期）` : ""}` : `未登录（未登录时部分视频下载不了）`}</span>${s.logged_in ? `<button class="ghost" data-sout="${site}">退出</button>` : `<button class="btn primary" data-sin="${site}">登录</button>`}`;
  const out = $(sel).querySelector("[data-sout]"); if (out) out.onclick = async () => { await api(`/api/social/${site}/logout`, { method: "POST" }); loadAccounts(); };
  const bin = $(sel).querySelector("[data-sin]"); if (bin) bin.onclick = async () => {
    try { await api(`/api/social/${site}/login`, { method: "POST" }); } catch (e) { return toast(e.message); }
    socialRow(site, sel, { state: "waiting", logged_in: false });
  };
}
function pollSocial(site) {
  clearTimeout(socialTimers[site]);
  socialTimers[site] = setTimeout(async () => {
    let s;
    try { s = await api(`/api/social/${site}/status`); } catch (e) { return pollSocial(site); }
    if (s.state === "waiting" && !s.logged_in) return pollSocial(site);
    const name = SOCIAL_NAME[site];
    if (s.logged_in) toast(`${name} 已登录`);
    else if (s.state === "cancelled") toast(`${name} 登录已取消（窗口被关掉了）`);
    else if (s.state === "expired") toast(`${name} 10 分钟内没登录，已关掉窗口`);
    else if (s.state === "error") toast(`${name} 登录出错：${s.error || ""}`);
    loadAccounts();
  }, 2000);
}
function pollDrive() { clearTimeout(qrTimer); qrTimer = setTimeout(async () => { const d = await api("/api/drive/status"); loadAccounts(); if (d.authorizing) pollDrive(); }, 3000); }

// ---------- 会话里的复用卡、归档卡 ----------
function reuseCards() {
  return (detail.reuse || []).map((u) => `<div class="msg"><div class="widget"><h5>跨期复用：这段画面以前别的期用过</h5>
    <div class="shots" style="grid-template-columns:200px 1fr"><img src="/api/assets/${u.asset_id}/thumb" alt=""><div class="sub" style="margin:0">${esc(u.title || "素材 " + u.asset_id)} · ${dur(u.src_in)}–${dur(u.src_out)}<br>${esc(u.credit || "")}<br>按规则要你同意后才能用。</div></div></div>
    <div class="below"><button class="btn primary" data-use="${u.id}" data-d="approve">同意复用</button><button class="btn soft" data-use="${u.id}" data-d="reject">不用，换别的</button></div></div>`).join("");
}
function archiveCard() {
  const a = detail.archive;
  if (!a || a.upload_state !== "verified" || a.deleted_at) return "";
  return `<div class="msg system"><div class="text">已上传 Drive 并核对完毕，本地文件将在 ${bjTime(a.delete_after)} 删除 <button class="ghost" id="keep7">再留 7 天</button></div></div>`;
}
// G3 通过后的“展示成果”：成片、封面、标题和简介随时可取；上传 Drive 只能手动点（用户 2026-10-13 定）
const RESULT_STATES = ["经验总结", "待发布", "上传Drive", "归档倒计时"];
const showResult = (d) => RESULT_STATES.includes(d.episode.state) && !(d.archive && d.archive.deleted_at);
function copyRow(label, text, key) {
  return `<div class="copyrow"><span class="lb">${esc(label)}</span><div class="tx">${esc(text || "—")}</div>${text ? `<button class="ghost" data-copy="${key}">复制</button>` : ""}</div>`;
}
function resultCard() {
  if (!showResult(detail)) return "";
  const r = detail.review || {}, m = r.media || {}, st = detail.episode.state;
  const upload = st === "待发布" ? `<button class="btn primary" id="publishBtn">上传 Drive</button>`
    : st === "上传Drive" ? `<span class="sub" style="margin:0">正在上传 Drive…</span>`
    : st === "经验总结" ? `<span class="sub" style="margin:0">经验总结跑完后可以上传 Drive</span>` : "";
  // 重做封面：经验总结、待发布时可用，后台跑约 5 分钟
  const coverRedo = r.cover_running ? `<div class="sub" style="margin:8px 0 0">封面正在重做…</div>`
    : ["经验总结", "待发布"].includes(st) ? `<div class="redo"><input id="coverNote" placeholder="对封面的意见（可不填），例如：主体换成拉塞尔的赛车"><button class="btn soft" id="coverBtn">重做封面</button></div>` : "";
  return `<div class="msg"><div class="widget result"><h5>展示成果</h5>
    ${m["成片"] ? `<video class="media-v" controls preload="metadata" src="${mediaUrl("成片")}"></video>` : `<div class="sub">成片还没生成</div>`}
    <div class="covers">${m["横版封面"] ? `<a href="${mediaUrl("横版封面")}" target="_blank"><img src="${mediaUrl("横版封面")}" alt="横版封面"></a>` : ""}${m["竖版封面"] ? `<a href="${mediaUrl("竖版封面")}" target="_blank"><img src="${mediaUrl("竖版封面")}" alt="竖版封面"></a>` : ""}</div>
    ${copyRow("发布标题", r.title, "title")}${copyRow("简介", r.desc, "desc")}${copyRow("记录标题", r.full_title, "full_title")}
    ${r.cover_note ? `<div class="sub" style="margin:8px 0 0">${esc(r.cover_note)}</div>` : ""}
    ${r.delivery_dir ? `<div class="sub" style="margin:8px 0 0">文件在：${esc(r.delivery_dir)}</div>` : ""}
    ${coverRedo}</div>
    <div class="below">${r.delivery_dir ? `<button class="btn soft" id="revealBtn">在访达中打开</button>` : ""}${upload}</div></div>`;
}
function bindThreadCards() {
  document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => { await navigator.clipboard.writeText((detail.review || {})[b.dataset.copy] || ""); toast("已复制"); }));
  const rv = $("#revealBtn"); if (rv) rv.onclick = async () => { try { await api(`/api/episodes/${current}/reveal`, { method: "POST" }); } catch (e) { toast(e.message); } };
  const cb = $("#coverBtn");
  if (cb) cb.onclick = async () => {
    try { await api(`/api/episodes/${current}/cover`, { method: "POST", body: JSON.stringify({ note: $("#coverNote").value }) }); toast("开始重做封面，大约 5 分钟"); } catch (e) { toast(e.message); }
    loadDetail();
  };
  const pb = $("#publishBtn");
  if (pb) pb.onclick = async () => {
    // 上传是对外动作，点两次确认
    if (!pb.dataset.armed) { pb.dataset.armed = "1"; pb.textContent = "再点一次确认上传"; setTimeout(() => { delete pb.dataset.armed; pb.textContent = "上传 Drive"; }, 3000); return; }
    try { await api(`/api/episodes/${current}/action/publish`, { method: "POST" }); toast("开始上传 Drive"); } catch (e) { toast(e.message); }
    loadDetail(); loadSessions();
  };
  document.querySelectorAll("[data-use]").forEach((b) => (b.onclick = async () => { try { await api(`/api/uses/${b.dataset.use}/${b.dataset.d}`, { method: "POST" }); } catch (e) { toast(e.message); } loadDetail(); }));
  const k = $("#keep7"); if (k) k.onclick = async () => { try { const r = await api(`/api/episodes/${current}/archive/extend`, { method: "POST" }); toast(`已延到 ${bjTime(r.delete_after)}`); } catch (e) { toast(e.message); } loadDetail(); };
}

// ---------- 译名表 ----------
let gState = "pending", gKind = "";
async function loadBadge() {
  const o = await api("/api/data/overview"); $("#glossaryBadge").textContent = o.glossary_pending || "";
  const n = await api("/api/lessons?state=pending"); $("#notesBadge").textContent = n.pending || "";
}
async function loadGlossary() {
  const g = await api(`/api/glossary?${new URLSearchParams({ ...(gState && { state: gState }), ...(gKind && { kind: gKind }) })}`);
  $("#glossaryBadge").textContent = g.pending || "";
  $("#glossarySub").textContent = `待审 ${g.pending} 条。采纳建议或改成你要的写法；没审过的译名会在 G1 逐字稿里标黄，成片前必须审完。`;
  $("#glossaryKind").innerHTML = ["", ...g.kinds].map((x) => `<button data-k="${x}" class="${x === gKind ? "on" : ""}">${x || "全部类别"}</button>`).join("");
  document.querySelectorAll("#glossaryKind button").forEach((b) => (b.onclick = () => { gKind = b.dataset.k; loadGlossary(); }));
  if (!$("#gKind").options.length) $("#gKind").innerHTML = g.kinds.map((x) => `<option>${x}</option>`).join("");
  $("#glossaryRows").innerHTML = g.items.length ? g.items.map((r) => r.state === "approved"
    ? `<tr><td>${esc(r.kind)}</td><td>${esc(r.original)}</td><td>${esc(r.suggested || "")}</td><td><b>${esc(r.approved)}</b><span class="src">${esc(r.source || "")}</span></td><td><button class="ghost" data-reopen="${r.id}">撤回</button></td></tr>`
    : `<tr><td>${esc(r.kind)}</td><td>${esc(r.original)}</td><td>${r.suggested ? esc(r.suggested) + `<span class="src">${esc(r.source || "")}</span>` : '<span class="src">没有建议</span>'}</td><td><input class="gl-input" data-id="${r.id}" placeholder="${r.suggested ? "留空=采纳建议" : "填写译名"}"></td><td><button class="btn primary" data-ok="${r.id}">${r.suggested ? "通过" : "保存"}</button></td></tr>`).join("")
    : `<tr><td colspan="5">${gState === "pending" ? "没有待审的译名" : "没有条目"}</td></tr>`;
  const approve = async (id) => {
    const text = $(`.gl-input[data-id="${id}"]`).value.trim();
    try { await api(`/api/glossary/${id}/approve`, { method: "POST", body: JSON.stringify({ text }) }); loadGlossary(); } catch (e) { toast(e.message); }
  };
  document.querySelectorAll("[data-ok]").forEach((b) => (b.onclick = () => approve(b.dataset.ok)));
  document.querySelectorAll(".gl-input").forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) approve(i.dataset.id); }));
  document.querySelectorAll("[data-reopen]").forEach((b) => (b.onclick = async () => { await api(`/api/glossary/${b.dataset.reopen}/reopen`, { method: "POST" }); loadGlossary(); }));
}
document.querySelectorAll("#glossaryState button").forEach((b) => (b.onclick = () => { document.querySelectorAll("#glossaryState button").forEach((x) => x.classList.toggle("on", x === b)); gState = b.dataset.s; loadGlossary(); }));
$("#gAdd").onclick = async () => {
  try { await api("/api/glossary", { method: "POST", body: JSON.stringify({ kind: $("#gKind").value, original: $("#gOrig").value, text: $("#gText").value }) }); $("#gOrig").value = $("#gText").value = ""; toast("已添加"); loadGlossary(); }
  catch (e) { toast(e.message); }
};
// ---------- Benchmark ----------
let bBranch = "";
const BCOLOR = { "资讯": "var(--primary)", "前瞻": "var(--warm)", "复盘": "var(--green)", "科普": "var(--amber)" };
const epName = (r) => (r.code ? `${r.code} ${r.title || ""}` : r.title || "—");
function benchSvg(d) {
  const pts = Object.entries(d.trend).flatMap(([b, list]) => list.map((p) => ({ ...p, branch: b }))).sort((a, b) => a.created_at.localeCompare(b.created_at));
  if (!pts.length) return `<text x="360" y="110" font-size="12" fill="currentColor" opacity=".5" text-anchor="middle">还没有成片评审</text>`;
  const vals = pts.map((p) => p.total).concat(d.baselines.map((b) => b.value).filter((v) => v != null));
  const lo = Math.max(0, Math.floor(Math.min(...vals) / 10) * 10 - 10), hi = 100;
  const X = (i) => (pts.length === 1 ? 380 : 40 + (i * 660) / (pts.length - 1)), Y = (v) => 190 - ((v - lo) / (hi - lo)) * 170;
  const idx = new Map(pts.map((p, i) => [p.review_id, i]));
  let s = [lo, (lo + hi) / 2, hi].map((v) => `<text x="32" y="${Y(v) + 3}" font-size="10" fill="currentColor" opacity=".5" text-anchor="end">${Math.round(v)}</text><line x1="40" y1="${Y(v)}" x2="700" y2="${Y(v)}" stroke="currentColor" opacity=".08"/>`).join("");
  for (const b of d.baselines) if (b.value != null) s += `<line x1="40" y1="${Y(b.value)}" x2="700" y2="${Y(b.value)}" stroke="${BCOLOR[b.branch]}" stroke-dasharray="4 4" opacity=".7"/>`;
  for (const [b, list] of Object.entries(d.trend)) {
    if (list.length > 1) s += `<polyline fill="none" stroke="${BCOLOR[b]}" stroke-width="1.8" points="${list.map((p) => `${X(idx.get(p.review_id))},${Y(p.total)}`).join(" ")}"/>`;
    s += list.map((p) => `<circle cx="${X(idx.get(p.review_id))}" cy="${Y(p.total)}" r="3.5" fill="${p.approved ? BCOLOR[b] : "var(--card)"}" stroke="${BCOLOR[b]}" stroke-width="1.5"><title>${esc(epName(p))} · ${p.total}${p.approved ? "" : "（未通过 G3）"}</title></circle>`).join("");
  }
  return s;
}
async function loadBench() {
  const d = await api(`/api/benchmark${bBranch ? "?branch=" + encodeURIComponent(bBranch) : ""}`);
  $("#benchBranch").innerHTML = ["", ...d.branches].map((b) => `<button data-b="${b}" class="${b === bBranch ? "on" : ""}">${b || "全部分支"}</button>`).join("");
  document.querySelectorAll("#benchBranch button").forEach((b) => (b.onclick = () => { bBranch = b.dataset.b; loadBench(); }));
  $("#benchCards").innerHTML = d.baselines.map((b) => {
    const last = b.history[b.history.length - 1], n = d.trend[b.branch]?.length || 0;
    const rule = b.passed < b.min ? `已通过 ${b.passed} 期 / 满 ${b.min} 期开始调整` : `已通过 ${b.passed} 期 / 按最近 ${b.window} 期中位数调整`;
    return `<div class="card"><h4>${esc(b.branch)}</h4><div class="sub">${b.value == null ? "还没有基线" : `基线 <b>${b.value}</b>`} · ${n} 期有评分</div><div class="sub">${rule}</div><div class="hint">${last ? `${esc(bjTime(last.created_at))} ${esc(last.reason || "")}` : "G3 通过后开始记录"}</div></div>`;
  }).join("");
  $("#benchSvg").innerHTML = benchSvg(d);
  $("#benchLegend").innerHTML = Object.keys(d.trend).map((b) => `<span class="tg" style="color:${BCOLOR[b]}">● ${esc(b)}</span>`).join("") + '<span class="tg">空心点 = 还没通过 G3</span>';
  const scoreTags = (sc) => Object.entries(sc).map(([k2, v]) => `<span class="tg" title="${esc(v.reason)}">${esc(k2)} ${v.score}</span>`).join("");
  const detailRow = (r, cols) => `<tr class="detail" hidden><td colspan="${cols}">${listBlock("分项", aiScoreRows(r.scores))}${listBlock("问题清单", aiIssueRows(r.issues), "AI 没提问题")}<div class="hint">${esc(r.model || "")} · 评分表 ${esc(r.rubric_version || "")}</div></td></tr>`;
  $("#benchRows").innerHTML = d.reviews.length ? d.reviews.map((r) => `<tr class="click"><td>${esc(bjTime(r.created_at))}</td><td>${esc(epName(r))}</td><td>${esc(r.branch)}</td>`
    + `<td><b style="color:${r.below_baseline ? "var(--red)" : "inherit"}">${r.total ?? "—"}</b>${r.approved ? ' <span class="hint">已通过</span>' : ""}</td><td>${r.baseline ?? "—"}${r.below_baseline ? ' <span class="hint">低于</span>' : ""}</td>`
    + `<td>${r.rework_round ? `第 ${r.rework_round} 轮后` : "—"}</td><td>${r.user_score ?? (r.user_decision ? (r.user_decision === "approved" ? "通过" : "打回") : "—")}</td><td>${scoreTags(r.scores)}</td></tr>` + detailRow(r, 8)).join("")
    : `<tr><td colspan="8">还没有成片评审</td></tr>`;
  const hist = d.baselines.flatMap((b) => b.history).sort((a, b) => b.created_at.localeCompare(a.created_at));
  $("#benchHist").innerHTML = hist.length ? hist.map((h) => `<tr><td>${esc(bjTime(h.created_at))}</td><td>${esc(h.branch)}</td><td>${h.old_value ?? "—"} → <b>${h.value ?? "—"}</b></td><td>${esc(h.reason || "")}${h.code ? ` <span class="hint">${esc(epName(h))}</span>` : ""}</td></tr>`).join("")
    : `<tr><td colspan="4">基线还没变过</td></tr>`;
  $("#benchAlerts").innerHTML = listBlock("需要重新校准", d.alerts.map((a) => [`${a.branch || ""} · 你打 ${a.user} 分，AI ${a.ai} 分`, `相差 ${a.diff} 分 · ${bjTime(a.at)}`, "warn"]));
  $("#benchCalib").innerHTML = d.calibrations.length ? d.calibrations.map((r) => `<tr class="click"><td>${esc(bjTime(r.created_at))}</td><td>${esc(r.kind)}</td><td>${esc(r.branch || "")}</td><td>${esc(r.video)}</td><td>${r.total ?? "—"}</td><td>${esc(r.model || "")}</td></tr>` + detailRow(r, 6)).join("")
    : `<tr><td colspan="6">还没有校准记录</td></tr>`;
  document.querySelectorAll("#bench tr.click").forEach((tr) => (tr.onclick = () => { tr.nextElementSibling.hidden = !tr.nextElementSibling.hidden; }));
}

// ---------- 经验笔记 ----------
let nState = "pending";
const N_STATE = { pending: "待批准", approved: "已生效", rejected: "已驳回或停用" };
async function loadNotes() {
  const n = await api(`/api/lessons${nState ? "?state=" + nState : ""}`);
  $("#notesBadge").textContent = n.pending || "";
  $("#notesSub").textContent = `待批准 ${n.pending} 条。每期上传 Drive 时总控会从评审、返工和你的意见里总结经验；批准后只注入对应分支、对应步骤的提示词（选“全部”即不限）。`;
  const opts = (list, v) => `<option value="">全部</option>` + list.map((x) => `<option${x === v ? " selected" : ""}>${esc(x)}</option>`).join("");
  api("/api/lessons/episodes").then((r) => {
    const sel = $("#rerunEp"), keep = sel.value;
    sel.innerHTML = r.items.length ? r.items.map((e) => `<option value="${e.id}">${esc(epName(e))}${e.lessons ? ` · 已有 ${e.lessons} 条` : ""}</option>`).join("") : `<option value="">还没有 G3 通过的期</option>`;
    if (keep && r.items.some((e) => String(e.id) === keep)) sel.value = keep;
    $("#rerunGo").disabled = !r.items.length;
  }).catch(() => {});
  $("#notesList").innerHTML = n.items.length ? n.items.map((l) => `<div class="pick" data-id="${l.id}">
    <div class="hint">${esc(N_STATE[l.state] || l.state)} · 来自 ${esc(epName(l))} · ${esc(bjTime(l.created_at))}${l.original ? " · 已改写" : ""}</div>
    <textarea class="n-content" rows="2" style="width:100%; margin:6px 0"${l.state === "approved" ? " readonly" : ""}>${esc(l.content)}</textarea>
    ${l.basis ? `<div class="sub">依据：${esc(l.basis)}</div>` : ""}${l.original ? `<div class="hint">AI 原文：${esc(l.original)}</div>` : ""}
    <div style="display:flex; gap:8px; align-items:center; margin-top:6px">分支 <select class="n-branch"${l.state === "approved" ? " disabled" : ""}>${opts(n.branches, l.branch)}</select>
      步骤 <select class="n-step"${l.state === "approved" ? " disabled" : ""}>${opts(n.steps, l.step)}</select>
      <span style="margin-left:auto"></span>${l.state === "approved" ? '<button class="btn soft" data-act="reject">停用</button>'
        : `<button class="btn text" data-act="reject"${l.state === "rejected" ? " hidden" : ""}>驳回</button><button class="btn primary" data-act="approve">${l.state === "rejected" ? "重新启用" : "批准"}</button>`}</div></div>`).join("")
    : `<div class="sub">${nState === "pending" ? "没有待批准的笔记" : "没有笔记"}</div>`;
  document.querySelectorAll("#notesList [data-act]").forEach((b) => (b.onclick = async () => {
    const box = b.closest(".pick"), act = b.dataset.act;
    const body = act === "approve" ? { content: box.querySelector(".n-content").value, branch: box.querySelector(".n-branch").value, step: box.querySelector(".n-step").value } : {};
    try { await api(`/api/lessons/${box.dataset.id}/${act}`, { method: "POST", body: JSON.stringify(body) }); toast(`已${b.textContent}`); loadNotes(); } catch (e) { toast(e.message); }
  }));
}
document.querySelectorAll("#notesState button").forEach((b) => (b.onclick = () => { document.querySelectorAll("#notesState button").forEach((x) => x.classList.toggle("on", x === b)); nState = b.dataset.s; loadNotes(); }));
$("#rerunGo").onclick = async () => {
  const id = $("#rerunEp").value;
  if (!id) return;
  try { await api(`/api/episodes/${id}/lessons/summarize`, { method: "POST" }); toast("已开始，完成后出现在待批准里"); } catch (e) { toast(e.message); }
};
document.addEventListener("click", () => { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); }, { once: true });

(async function init() {
  boot = await api("/api/bootstrap");
  fillModels();
  $("#roleRows").innerHTML = boot.roles.map((r) => `<tr><td>${esc(r.role)}</td><td>${esc(r.model)}</td><td>${esc(r.effort)}</td></tr>`).join("");
  $("#priceSource").textContent = "价格来源：" + boot.price_source;
  const groups = await loadSessions();
  if (["#usage", "#assets", "#notes", "#bench", "#data", "#glossary"].includes(location.hash)) { showPage(location.hash.slice(1)); loadTopbar(); loadBadge(); return setInterval(loadTopbar, 60000); }
  const first = groups.flatMap((g) => g.items)[0];
  if (first) await openSession(first.id); else await newSession();
  loadBadge();
  setInterval(loadTopbar, 60000);
})();
