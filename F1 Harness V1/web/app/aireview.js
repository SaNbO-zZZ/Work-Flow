"use strict";
// G3 审核页和 Benchmark 页共用：AI 评审的分项行和问题行，每行 [标题, 说明, 样式]
function aiScoreRows(scores) {
  const cls = (s) => (s < 60 ? "bad" : s < 80 ? "warn" : "ok");
  return Object.entries(scores || {}).map(([k, v]) => [`${k} ${v.score}`, `权重 ${v.weight} · ${v.reason}`, cls(v.score)]);
}
function aiIssueRows(issues) {
  return (issues || []).map((i) => [`${i.at} ${i.severity} · ${i.item}`, `${i.problem} → ${i.fix}（打回到${i.back_to}）`,
    i.severity === "高" ? "bad" : i.severity === "中" ? "warn" : ""]);
}
