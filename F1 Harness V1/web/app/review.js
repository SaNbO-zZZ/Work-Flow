"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
const eid = +params.get("episode"), gate = params.get("gate");
const current = eid;   // cards.js 里的 fileUrl 用（页面不许内联脚本，放这里）
const api = async (p, o = {}) => { const r = await fetch(p, { ...o, headers: { "Content-Type": "application/json", "X-Hermes": "1" } }); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };
const toast = (t) => { const e = $("#toast"); e.textContent = t; e.classList.add("on"); setTimeout(() => e.classList.remove("on"), 4000); };

const dur = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
const media = (name) => `/api/episodes/${eid}/media/${encodeURIComponent(name)}`;
const rowsHtml = (title, rows) => rows.length ? `<div class="checks"><h6>${esc(title)}</h6>${rows.map(([a, b, c]) => `<div class="${c}"><b>${esc(a)}</b>${esc(b || "")}</div>`).join("")}</div>` : "";

async function g3() {
  const r = await api(`/api/episodes/${eid}/review`), m = r.media || {}, h = r.hard || {}, mix = r.mix || {};
  window.recal = r.recalibrate || [];
  $("#left").innerHTML = (m["成片"] ? `<video class="media-v" style="margin:0" controls preload="metadata" src="${media("成片")}"></video>` : `<div class="sub">成片还没生成</div>`)
    + `<div class="covers">${m["横版封面"] ? `<img src="${media("横版封面")}" alt="横版封面">` : ""}${m["竖版封面"] ? `<img src="${media("竖版封面")}" alt="竖版封面">` : ""}</div>`;
  const items = (h.items || []).slice().sort((a, b) => a.ok - b.ok);
  $("#right").innerHTML = `<div class="checks"><h6>标题和简介</h6><div><b>${esc(r.title)}</b></div><div>${esc(r.desc || "简介还没有（M6 写文案时生成）")}</div></div>`
    + (mix.integrated != null ? `<div class="checks"><h6>混音</h6><div>响度 ${esc(mix.integrated)} LUFS · 真峰值 ${esc(mix.true_peak)} dBTP · 口播避让 ${esc(Math.abs((mix.duck_db || [0])[0]))} dB</div></div>` : "")
    + (h.items ? rowsHtml(`硬检查 · ${h.passed ? "全部通过" : "有没过的项"}`, items.map((i) => [i.name, i.detail, i.ok ? "ok" : i.hard ? "bad" : "warn"])) : `<div class="sub">硬检查结果还没出来</div>`)
    + rowsHtml("配音提醒", (r.voice?.issues || []).map((i) => [`${dur(i.at)} ${i.kind}`, i.detail, "warn"]))
    + aiHtml(r.ai || {}, r.qa || {});
}

// G1：左边逐字稿全文（网址带 v= 看某一版，带 cmp= 时标出和那一版的改动），右边标题简介和资料（[F#]/[S#] 的出处）
async function g1(d) {
  const list = (d.versions || {}).G1 || [], v = list[(+params.get("v") || list.length) - 1], w = list[+params.get("cmp") - 1];
  if (!v || v.script == null) { $("#left").innerHTML = `<div class="sub">这一版逐字稿没有留下</div>`; return; }
  let body = esc(v.script);
  if (w && w.script != null && w.script !== v.script) body = sideBySide(w.script, v.script, true)[1];
  $("#title").textContent += ` · v${v.v}` + (w ? `（标出和 v${w.v} 的改动）` : "");
  $("#left").innerHTML = `<div class="pre" style="font-size:14px;line-height:1.8;color:var(--t1)">${body}</div>`;
  const facts = await fetch(fileUrl("01_资料/资料.md")).then((r) => (r.ok ? r.text() : "")).catch(() => "");
  $("#right").innerHTML = `<div class="checks"><h6>标题和简介</h6><div><b>${esc(v.title || "—")}</b></div><div class="pre">${esc(v.desc || "")}</div></div>`
    + (v.feedback ? `<div class="checks"><h6>审核意见</h6><div>${esc(v.feedback)}</div></div>` : "")
    + `<div class="checks"><h6>资料（[F#]/[S#] 的出处）</h6><div class="pre">${esc(facts || "资料文件没有找到")}</div></div>`;
}

function aiHtml(ai, qa) {
  if (!ai.scores) return `<div class="sub" style="margin-top:12px">${esc(qa.ai_skipped || "AI 评审结果还没出来")}</div>`;
  const base = ai.baseline == null ? "" : ` · ${ai.below_baseline ? "低于" : "不低于"}基线 ${ai.baseline}`;
  const scores = aiScoreRows(ai.scores);
  const issues = aiIssueRows(ai.issues);
  const rw = qa.rework || {}, hist = rw.history || [];
  const trail = (hist.length > 1 ? [[`分数变化 ${hist.join(" → ")}`, `自动返工 ${rw.rounds} 轮`, ""]] : [])
    .concat(rw.note ? [[rw.note, "", ai.below_baseline ? "bad" : "ok"]] : [])
    .concat((window.recal || []).map((a) => [`需要重新校准`, `你打 ${a.user} 分，AI ${a.ai} 分，相差 ${a.diff} 分`, "warn"]));
  return rowsHtml(`AI 评审 · ${ai.total} 分${base}`, scores)
    + rowsHtml("自动返工与校准", trail)
    + (ai.summary ? `<div class="sub" style="margin-top:6px">${esc(ai.summary)}</div>` : "")
    + (issues.length ? rowsHtml(`问题清单 · ${issues.length} 条`, issues) : `<div class="sub" style="margin-top:6px">AI 没列出问题</div>`);
}

(async function init() {
  const d = await api(`/api/sessions/${eid}`);
  const ep = d.episode;
  document.title = `${gate} 审核 · ${ep.title}`;
  $("#code").textContent = `${ep.code || ""} · 独立窗口，可拉大或全屏`;
  $("#title").textContent = `${gate} ${gate === "G1" ? "逐字稿" : gate === "G3" ? "成片终审" : "审核"} · ${ep.title}`;
  if (gate === "G3") await g3().catch((e) => toast(e.message));
  else if (gate === "G1") await g1(d).catch((e) => toast(e.message));
  if (gate !== "G3") { $("#score").style.display = "none"; $("#scoreLabel").style.display = "none"; }
  const pending = d.gates.filter((g) => g.gate === gate && g.state === "pending").length > 0;
  $("#backs").innerHTML = (d.reject_targets[gate] || []).map((t) => `<button class="btn soft" data-back="${esc(t)}">打回到${esc(t)}</button>`).join(" ");
  if (!pending) { document.querySelectorAll("footer button").forEach((b) => (b.disabled = true)); toast("这一关已经处理过了"); }
  $("#ok").onclick = () => decide("approve");
  document.querySelectorAll("[data-back]").forEach((b) => (b.onclick = () => decide("reject", b.dataset.back)));
})();

async function decide(decision, backTo) {
  const feedback = $("#note").value.trim();
  if (decision === "reject" && !feedback) return toast("打回时请写意见");
  const ok = $("#ok");   // 通过时的备注不会执行，先提醒一次（同 app.js）
  if (decision === "approve" && feedback && !ok.dataset.sure) {
    ok.dataset.sure = "1"; ok.textContent = "仍然通过（备注不执行）";
    return toast("通过时写的备注不会执行，只留作记录；要按备注改，请点“打回”");
  }
  try {
    const res = await api(`/api/episodes/${eid}/gates/${gate}/${decision}`, { method: "POST", body: JSON.stringify({ feedback, back_to: backTo, score: $("#score").value }) });
    toast((res.notes || []).join(" ") || "已提交"); document.querySelectorAll("footer button").forEach((b) => (b.disabled = true));
    if (gate === "G3") await g3().catch(() => {});
  } catch (e) { toast(e.message); }
}
