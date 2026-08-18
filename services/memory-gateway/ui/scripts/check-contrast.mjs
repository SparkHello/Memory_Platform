#!/usr/bin/env node
/**
 * 对比度验证：解析 tokens.css 的深（:root）/浅（[data-theme="light"]）主题，
 * 对正文/辅助文字 token 与其常见背景（纯色底与玻璃面板合成色）计算 WCAG 对比度。
 * 用法：node scripts/check-contrast.mjs   （在 ui/ 目录下运行）
 * 退出码：存在 < 4.5:1 的组合时为 1，否则为 0。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "../src/styles/tokens.css"), "utf8");

/** 取出某个 selector 块内的 --token 定义（支持 #hex 与 rgba()）。 */
function parseBlock(selector) {
  const start = css.indexOf(selector);
  if (start < 0) return {};
  const open = css.indexOf("{", start);
  let depth = 0;
  let end = open;
  for (; end < css.length; end++) {
    if (css[end] === "{") depth++;
    else if (css[end] === "}") { depth--; if (depth === 0) break; }
  }
  const body = css.slice(open, end);
  const out = {};
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    out[m[1]] = m[2].trim();
  }
  return out;
}

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  const v = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16) / 255);
}
function parseColor(raw) {
  raw = raw.trim();
  if (raw.startsWith("#")) { const [r, g, b] = hexToRgb(raw); return { r, g, b, a: 1 }; }
  const m = raw.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map((s) => parseFloat(s));
    return { r: p[0] / 255, g: p[1] / 255, b: p[2] / 255, a: p.length > 3 ? p[3] : 1 };
  }
  return null;
}
/** 把半透明 fg 合成到 bg 上。 */
function composite(fg, bg) {
  return {
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
  };
}
function luminance({ r, g, b }) {
  const f = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function ratio(a, b) {
  const [l1, l2] = [luminance(a), luminance(b)];
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}
/** 从渐变里取第一个 rgba 作为面板近似合成色。 */
function firstGradientColor(raw) {
  const m = raw && raw.match(/rgba?\([^)]+\)/);
  return m ? parseColor(m[0]) : null;
}

const themes = [
  { name: "dark", block: parseBlock(":root") },
  { name: "light", block: parseBlock('[data-theme="light"]') },
];

const THRESHOLD = 4.5;
let failures = 0;
const rows = [];

for (const theme of themes) {
  const t = theme.block;
  const bg = parseColor(t["--bg"]);
  const bgDeep = parseColor(t["--bg-deep"]);
  const solidBg = parseColor(t["--solid-bg"]);
  // 玻璃面板：渐变第一层 rgba 合成到 --bg 上，近似面板实际底色
  const panelRaw = t["--glass-panel-bg"];
  const panelFirst = firstGradientColor(panelRaw);
  const panelBg = panelFirst && bg ? composite(panelFirst, bg) : solidBg || bg;
  const sidebarBg = solidBg || bg; // sidebar 用 solid 近似

  const text = parseColor(t["--text"]);
  const muted = parseColor(t["--muted"]);
  const mutedStrong = parseColor(t["--muted-strong"]);
  const sidebarText = parseColor(t["--sidebar-text"]);
  const sidebarMuted = parseColor(t["--sidebar-muted"]);

  const checks = [
    ["--text", text, "--bg", bg],
    ["--text", text, "glass-panel", panelBg],
    ["--muted", muted, "--bg", bg],
    ["--muted", muted, "glass-panel", panelBg],
    ["--muted-strong", mutedStrong, "--bg", bg],
    ["--muted-strong", mutedStrong, "glass-panel", panelBg],
    ["--sidebar-text", sidebarText, "sidebar", sidebarBg],
    ["--sidebar-muted", sidebarMuted, "sidebar", sidebarBg],
  ];
  for (const [token, fg, bgName, bgC] of checks) {
    if (!fg || !bgC) continue;
    const r = ratio(fg, bgC);
    const pass = r >= THRESHOLD;
    if (!pass) failures++;
    rows.push({ theme: theme.name, token, bg: bgName, ratio: r.toFixed(2), pass });
  }
}

console.log(`\nWCAG 对比度（阈值 ${THRESHOLD}:1）\n`);
for (const row of rows) {
  const mark = row.pass ? "PASS" : "FAIL";
  console.log(`  [${mark}] ${row.theme.padEnd(5)} ${row.token.padEnd(16)} on ${row.bg.padEnd(11)} ${row.ratio}:1`);
}
console.log(`\n${failures === 0 ? "全部通过" : `${failures} 组不达标`}\n`);
process.exit(failures === 0 ? 0 : 1);
