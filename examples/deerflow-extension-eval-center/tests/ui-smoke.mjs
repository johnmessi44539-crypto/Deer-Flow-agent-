/**
 * Lightweight UI interaction smoke test: node tests/ui-smoke.mjs
 * Uses a mock DOM and deterministic backend responses; no browser, server or
 * model is started. Browser layout, accessibility and real HTTP are tested
 * separately. Imports the production UI and packaged fixed dataset directly.
 */
import assert from "node:assert/strict";
import { setMaxListeners } from "node:events";
import { readFile } from "node:fs/promises";

// AbortSignal's Node listener warning does not model browser DOM listeners.
setMaxListeners(0);

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.events = new Map();
    this._text = "";
    this._value = undefined;
    this.disabled = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent ?? "").join(""); }
  set value(value) { this._value = String(value); }
  get value() { return this._value ?? (this.tag === "select" ? this.children[0]?.attrs.value ?? "" : this.attrs.value ?? ""); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  append(...children) {
    for (const child of children) { child.parent = this; this.children.push(child); }
  }
  replaceChildren(...children) { this.children = []; this._text = ""; this._value = undefined; this.append(...children); }
  addEventListener(type, handler, options = {}) {
    if (!this.events.has(type)) this.events.set(type, new Set());
    this.events.get(type).add(handler);
    options.signal?.addEventListener("abort", () => this.events.get(type).delete(handler), { once: true });
  }
  dispatch(type) {
    for (const handler of this.events.get(type) ?? []) handler({ target: this });
  }
  click() { if (!this.disabled) this.dispatch("click"); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
}

globalThis.document = { createElement: (tag) => new Element(tag) };
const { default: contribution, mountEvaluation } = await import("../deerflow_extension_eval_center/static/index.mjs");
assert.equal(contribution.module, "eval-center");
assert.equal(contribution.surfaces[0].id, "experiments");

const fixed = JSON.parse(await readFile(new URL("../deerflow_extension_eval_center/data/fixed-cases-v1.json", import.meta.url), "utf8"));
assert.equal(fixed.id, "fixed-cases-v1");
assert.equal(fixed.cases.length, 3);
const custom = {
  id: "custom-cases", name: "我的自定义任务", revision: 2,
  cases: [{ id: "my-case", title: "我的案例", prompt: "只给答案", expected: { total: 3 }, grader: "json_exact_v1" }],
};
const clone = (value) => structuredClone(value);
const catalog = [clone(fixed)];
const experiments = [];
const calls = [];
let lists = 0;
let maxLists = 0;
let failAgentOnce = true;
let holdComparison = null;
let releaseComparison = null;

const backend = async (name, payload = {}) => {
  calls.push({ name, payload: clone(payload) });
  if (name === "dataset_get") {
    return clone(catalog.find((item) => !payload.id || item.id === payload.id && item.revision === payload.revision));
  }
  if (name === "dataset_list") {
    return { items: catalog.map(({ id, name, revision }) => ({ id, name, revision, hash: id, builtin: id === fixed.id })) };
  }
  if (name === "dataset_import") { catalog.push(clone(payload.dataset)); return clone(payload.dataset); }
  if (name === "experiment_list") {
    lists++;
    maxLists = Math.max(maxLists, lists);
    await new Promise((resolve) => setImmediate(resolve));
    lists--;
    return { items: clone(experiments) };
  }
  if (name === "experiment_get") return clone(experiments.find((item) => item.id === payload.id));
  if (name === "experiment_start") {
    if (payload.mode === "agent" && failAgentOnce) {
      failAgentOnce = false;
      throw new Error("ambiguous network failure");
    }
    const source = catalog.find((item) => item.id === payload.dataset_id && item.revision === payload.dataset_revision);
    assert.ok(source, "start must carry the selected dataset version");
    const wrong = payload.offline_scenario === "wrong_total";
    const result = {
      id: "exp-" + (experiments.length + 1), mode: payload.mode,
      offline_scenario: payload.offline_scenario ?? "default", dataset: clone(source), dataset_hash: source.id,
      status: "completed", created_at: "2026-11-27T00:00:00Z",
      summary: { total: source.cases.length, passed: source.cases.length - (wrong ? 1 : 0), failed: wrong ? 1 : 0, unscored: 0 },
      items: source.cases.map((item, index) => {
        const expected = clone(item.expected);
        const actual = wrong && index === 0 ? { ...expected, grand_total: 999 } : expected;
        return {
          case_id: item.id, title: item.title, status: "completed", verdict: wrong && index === 0 ? "fail" : "pass",
          latency_ms: index + (wrong ? 15 : 10), output: JSON.stringify(actual),
          checks: index === 0 && item.id === "monthly-sales-001"
            ? [{ name: "$/grand_total", passed: !wrong, expected: 380, actual: wrong ? 999 : 380 }]
            : [{ name: "valid_json_object", passed: true, expected: "one valid JSON object", actual: "object" }],
        };
      }),
    };
    experiments.unshift(result);
    return clone(result);
  }
  if (name === "experiment_compare") {
    if (holdComparison) await holdComparison;
    const baseline = experiments.find((item) => item.id === payload.baseline_id);
    const candidate = experiments.find((item) => item.id === payload.candidate_id);
    const compatible = baseline.dataset_hash === candidate.dataset_hash;
    return {
      baseline_id: baseline.id, candidate_id: candidate.id,
      baseline_scenario: baseline.offline_scenario, candidate_scenario: candidate.offline_scenario,
      baseline_dataset_hash: baseline.dataset_hash, candidate_dataset_hash: candidate.dataset_hash,
      compatible, reason: compatible ? null : "Dataset hashes differ",
      notice: "Offline or mixed-mode results are not model capability.", is_model_comparison: false,
      summary: { improved: 0, regressed: 1, unchanged: 2, unscored: 0 },
      items: compatible ? candidate.items.map((item, index) => ({
        case_id: item.case_id, title: item.title, baseline_verdict: "pass", candidate_verdict: item.verdict,
        change: index === 0 ? "regressed" : "unchanged", baseline_latency_ms: 10 + index,
        candidate_latency_ms: item.latency_ms, latency_delta_ms: item.latency_ms - (10 + index),
      })) : [],
    };
  }
  if (name === "report_export") return { filename: "report.json", content: JSON.stringify(experiments[0]) };
  throw new Error("Unexpected backend action: " + name);
};

const root = new Element("root");
const life = new AbortController();
const mounted = mountEvaluation(root, { locale: "zh-CN", signal: life.signal, callBackend: backend });
const nodes = (node) => [node, ...node.children.flatMap(nodes)];
const button = (text) => nodes(root).find((node) => node.tag === "button" && node.textContent === text);
const select = (aria) => nodes(root).find((node) => node.tag === "select" && node.attrs["aria-label"] === aria);
// Drain microtasks and deferred backend replies without wall-clock sleeps.
const settle = async () => {
  for (let index = 0; index < 24; index++) await new Promise((resolve) => setImmediate(resolve));
};

try {
  await settle();
  assert.equal(calls.filter((call) => call.name === "experiment_start").length, 0, "mount must not start work");
  assert.equal(select("选择任务集与版本").value, JSON.stringify([fixed.id, fixed.revision]));
  assert.equal(button("运行离线演示").disabled, false);
  button("运行离线演示").click();
  await settle();
  assert.equal(experiments.length, 1);
  button("运行负向自检").click();
  await settle();
  assert.equal(experiments[0].offline_scenario, "wrong_total");
  assert.match(root.textContent, /故意把销售总额改为 999/);
  assert.match(root.textContent, /销售总额是否正确/);
  assert.equal(select("选择基线实验").value, "exp-1");
  button("与当前实验对比").click();
  await settle();
  assert.match(root.textContent, /退步/);
  assert.match(root.textContent, /当前更慢/);
  assert.match(root.textContent, /不代表模型能力差异/);
  assert.equal(calls.find((call) => call.name === "experiment_compare").payload.candidate_id, "exp-2");

  const input = nodes(root).find((node) => node.tag === "input" && node.attrs.type === "file");
  input.files = [{ size: 200, async text() { return JSON.stringify(custom); } }];
  input.dispatch("change");
  await settle();
  assert.match(root.textContent, /我的自定义任务/);
  assert.match(root.textContent, /自定义任务集请使用真实 Agent/);
  assert.equal(button("运行离线演示").disabled, true);
  assert.equal(button("运行负向自检").disabled, true);
  assert.equal(button("运行真实 Agent").disabled, false);
  button("运行真实 Agent").click();
  await settle();
  assert.ok(button("重试启动真实 Agent"));
  button("重试启动真实 Agent").click();
  await settle();
  const starts = calls.filter((call) => call.name === "experiment_start" && call.payload.mode === "agent");
  assert.equal(starts.length, 2);
  assert.equal(starts[0].payload.idempotency_key, starts[1].payload.idempotency_key);
  assert.equal(starts[0].payload.dataset_id, custom.id);
  assert.equal(starts[0].payload.dataset_revision, custom.revision);
  button("与当前实验对比").click();
  await settle();
  assert.match(root.textContent, /两个实验的任务集内容不同/);
  assert.match(root.textContent, /原始诊断/);

  const picker = select("选择任务集与版本");
  picker.value = JSON.stringify([fixed.id, fixed.revision]);
  picker.dispatch("change");
  await settle();
  assert.equal(button("运行离线演示").disabled, false);
  const importsBefore = calls.filter((call) => call.name === "dataset_import").length;
  input.files = [{ size: 1, async text() { return "{"; } }];
  input.dispatch("change");
  await settle();
  assert.equal(calls.filter((call) => call.name === "dataset_import").length, importsBefore);
  assert.match(root.textContent, /文件不是合法的 JSON/);
  assert.equal(maxLists, 1, "refresh must remain single-flight");

  holdComparison = new Promise((resolve) => { releaseComparison = resolve; });
  button("与当前实验对比").click();
  await settle();
  const originalHistory = nodes(root).find((node) =>
    node.tag === "button" && node.attrs.class?.includes("history-row") && node.textContent.includes("exp-1"));
  originalHistory.click();
  await settle();
  releaseComparison();
  await settle();
  assert.doesNotMatch(nodes(root).find((node) => node.attrs.class === "comparison-body").textContent, /原始诊断/);
} finally {
  releaseComparison?.();
  mounted.dispose();
}
assert.equal(root.children.length, 0, "dispose must remove the rendered page");

const previewRoot = new Element("root");
const preview = mountEvaluation(previewRoot, {
  locale: "zh-CN", signal: new AbortController().signal,
  settings: { preview_offline_only: true }, callBackend: backend,
});
try {
  await settle();
  const realPreview = nodes(previewRoot).find((node) => node.tag === "button" && node.textContent === "运行真实 Agent");
  assert.equal(realPreview.disabled, true, "preview cannot start real Agent work");
} finally {
  preview.dispose();
}

console.log("PASS: lightweight mock DOM interaction smoke test");
console.log("Covered: negative control, dataset import/version selection, execution gating, idempotent retry,");
console.log("comparison/mismatch, Chinese grading labels, stale-response fencing, preview mode, serial refresh and cleanup.");
console.log("No browser, server or model was started; real-browser validation remains separate.");
