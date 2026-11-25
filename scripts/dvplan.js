#!/usr/bin/env node
/* Daevanion suggestions for every character, for the hourly job.
   Uses the optimiser inside the published page (index.html, between DVOPT-START and DVOPT-END), so page and job
   always agree. stdin: {share: {Class: {skill: [open, total]}}, chars: {rid: {cls, skills, boards}}}
   stdout: {rid: {up: [[skill, from, to]], u, p, gain, items, sig}} */
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const a = html.indexOf("/* DVOPT-START"), b = html.indexOf("/* DVOPT-END */");
if (a < 0 || b < 0) { console.error("optimiser not found in index.html"); process.exit(1); }
const dvOptimise = new Function(html.slice(a, b) + "\nreturn dvOptimise;")();
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const tier = l => [8, 12, 16, 20].filter(x => l >= x).pop() || 0;
const out = {};
for (const [rid, c] of Object.entries(input.chars || {})) {
  try {
    const ps = {}, sh = (input.share || {})[c.cls] || {};
    for (const k in sh) if (sh[k][1]) ps[k] = sh[k][0] / sh[k][1];
    const r = dvOptimise(c.boards, c.skills || [], {passive: ps});
    const up = r.changes.filter(x => x.kind === "Active" && x.eq && tier(x.to) > tier(x.from)).map(x => [x.s, x.from, x.to]);
    /* signature: which tiles change, so the page can tell when a suggestion is new */
    const key = n => n.b + ":" + n.r + ":" + n.c;
    const sig = r.unspec.map(i => "-" + key(r.nodes[i])).concat(r.pick.map(i => "+" + key(r.nodes[i]))).sort().join(",");
    let h = 0; for (let i = 0; i < sig.length; i++) h = (h * 31 + sig.charCodeAt(i)) | 0;
    out[rid] = {up, u: r.unspec.length, p: r.pick.length, gain: Math.round(r.gain * 10) / 10, items: r.tiles, sig: r.unspec.length ? (h >>> 0).toString(36) : ""};
  } catch (e) { out[rid] = {err: String(e)}; }
}
process.stdout.write(JSON.stringify(out));
