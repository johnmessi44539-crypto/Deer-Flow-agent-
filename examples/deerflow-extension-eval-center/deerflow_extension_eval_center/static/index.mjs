const STATUS_LABELS = {
  queued: "排队中",
  pending: "等待中",
  running: "执行中",
  cancelling: "正在取消",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  needs_reconciliation: "需要核对",
  pass: "通过",
  fail: "未通过",
  unscored: "未评分",
  error: "错误",
  blocked: "等待输入",
};

const isBusy = (status) => ["queued", "pending", "running", "cancelling"].includes(status);
const pretty = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "—";
const label = (status) => STATUS_LABELS[status] ?? status ?? "—";
const CHECK_LABELS = {
  valid_json_object: "JSON 对象格式：输出必须是一个合法的 JSON 对象",
  json_parse: "JSON 对象格式：输出必须是一个合法的 JSON 对象",
  "$ keys": "最外层字段完整性：不能缺少或增加字段",
  "$/month_totals keys": "月度汇总字段完整性：月份必须与参考答案一致",
  "$/month_totals/2026-01": "1 月销售额是否正确",
  "$/month_totals/2026-02": "2 月销售额是否正确",
  "$/grand_total": "销售总额是否正确",
  "$/net_revenue": "净收入是否正确",
  "$/fully_refunded_orders": "全额退款订单数是否正确",
  "$/error_code": "错误码是否正确",
  "$/invalid_row": "非法行号是否正确",
};
const timestamp = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
};
const experimentName = (experiment) => experiment.mode !== "offline"
  ? "真实 Agent 评测"
  : (experiment.offline_scenario ?? experiment.scenario) === "wrong_total" ? "负向自检（总额 999）" : "离线流程演示";

export function mountEvaluation(root, context) {
  const life = new AbortController();
  const signal = life.signal;
  let disposed = false;
  let timer = null;
  let refreshing = false;
  let refreshRequested = false;
  let starting = false;
  let operationBusy = false;
  let loadingDataset = false;
  let importingDataset = false;
  let dataset = null;
  let datasets = [];
  let catalogLoaded = false;
  let datasetGeneration = 0;
  let experiments = [];
  let selectedId = null;
  let selected = null;
  let historyLife = new AbortController();
  let detailLife = new AbortController();
  let historyFingerprint = null;
  let detailFingerprint = null;
  let baselineFingerprint = null;
  let comparing = false;
  let comparisonGeneration = 0;
  const uncertainStarts = new Map();
  const downloadURLs = new Set();
  const expandedChecks = new Set();
  const active = () => !disposed && !signal.aborted && !context.signal?.aborted;
  const previewOnly = context.settings?.preview_offline_only === true;
  const builtinDataset = () => dataset?.id === "fixed-cases-v1";
  const datasetKey = (value) => JSON.stringify([value.id, value.revision]);
  const startKey = (mode, scenario) => `${mode}:${scenario ?? "default"}:${dataset ? datasetKey(dataset) : ""}`;
  const make = (tag, text, attributes = {}) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    return node;
  };
  const on = (node, type, handler, eventSignal = signal) => node.addEventListener(type, handler, { signal: eventSignal });
  const button = (text, handler, className = "", eventSignal = signal) => {
    const node = make("button", text, { type: "button", class: className });
    on(node, "click", handler, eventSignal);
    return node;
  };
  const badge = (text, kind = "") => make("span", text, { class: `badge ${kind}` });
  const field = (name, content) => {
    const section = make("section", undefined, { class: "output-field" });
    section.append(make("h4", name), make("pre", pretty(content)));
    return section;
  };
  const errorText = (cause) => String(cause?.message ?? "操作未完成，请重试。").slice(0, 800);

  const stylesheet = make("link", undefined, {
    rel: "stylesheet",
    crossorigin: "use-credentials",
    href: new URL("./styles.css", import.meta.url).href,
  });
  const page = make("main", undefined, { class: "eval-page" });
  const header = make("header", undefined, { class: "page-header" });
  const titleArea = make("div");
  titleArea.append(
    make("p", "固定用例 · 可复现判分", { class: "eyebrow" }),
    make("h2", "Agent 评测工作台"),
    make("p", "先看用例与验收标准，再运行实验，逐条检查实际输出。", { class: "muted" }),
  );
  const refreshButton = button("刷新", () => void refresh());
  header.append(titleArea, refreshButton);
  const notification = make("p", "正在读取固定用例和实验记录…", { role: "status", "aria-live": "polite", class: "status-line" });
  const error = make("p", "", { role: "alert", class: "error-line" });
  error.hidden = true;
  const datasetSection = make("section", undefined, { class: "dataset-section", "aria-label": "固定用例" });
  const datasetTitle = make("h3", "首批固定用例");
  const datasetMeta = make("p", "正在加载…", { class: "muted dataset-meta" });
  const caseList = make("div", undefined, { class: "case-list" });
  datasetSection.append(datasetTitle, datasetMeta, caseList);

  const controls = make("section", undefined, { class: "run-controls", "aria-label": "启动评测" });
  const controlsText = make("div");
  controlsText.append(
    make("h3", "运行一次实验"),
    make("p", "离线演示使用预置响应，验证调度与评分流程。真实 Agent 使用服务端配置并产生模型调用。", { class: "muted" }),
  );
  const controlButtons = make("div", undefined, { class: "button-row" });
  const offlineButton = button("运行离线演示", () => void start("offline"));
  const negativeButton = button("运行负向自检", () => void start("offline", "wrong_total"));
  const agentButton = button("运行真实 Agent", () => void start("agent"), "primary");
  controlButtons.append(offlineButton, negativeButton, agentButton);
  controls.append(controlsText, controlButtons);
  const datasetControls = make("div", undefined, { class: "dataset-controls" });
  const datasetSelectLabel = make("label", "任务集与版本");
  const datasetSelect = make("select", undefined, { "aria-label": "选择任务集与版本" });
  datasetSelectLabel.append(datasetSelect);
  const importInput = make("input", undefined, { type: "file", accept: ".json,application/json", "aria-label": "选择任务集 JSON 文件" });
  importInput.hidden = true;
  const importButton = button("导入 JSON 任务集", () => importInput.click());
  const datasetHint = make("p", "正在读取任务集…", { class: "muted dataset-hint" });
  const negativeHint = make("p", "负向自检会故意把销售总额写成 999，预期 2 条通过、1 条未通过；用于验证评分器能发现错误。", { class: "muted" });
  datasetControls.append(datasetSelectLabel, importButton, importInput);
  controlsText.append(datasetControls, datasetHint, negativeHint);
  on(datasetSelect, "change", () => void chooseDataset());
  on(importInput, "change", () => void importDataset());

  const historySection = make("section", undefined, { class: "history-section" });
  historySection.append(make("h3", "实验记录"));
  const history = make("div", undefined, { class: "history-list" });
  historySection.append(history);
  const detailSection = make("section", undefined, { class: "detail-section", "aria-label": "实验详情" });
  const detailHeader = make("div", undefined, { class: "section-heading" });
  const detailTitle = make("h3", "实验详情");
  const detailActions = make("div", undefined, { class: "button-row" });
  const cancelButton = button("取消实验", () => void cancel());
  const exportButton = button("下载 JSON 报告", () => void exportReport());
  detailActions.append(cancelButton, exportButton);
  detailHeader.append(detailTitle, detailActions);
  const detail = make("div", "尚未启动实验。点击上方“运行离线演示”后，才会出现实际输出和评分。", { class: "detail-body" });
  const comparisonSection = make("section", undefined, { class: "comparison-section", "aria-label": "实验对比" });
  comparisonSection.append(make("h3", "与历史实验对比"), make("p", "选择一个已结束的实验作为基线，对比当前实验的逐项判分和耗时。", { class: "muted" }));
  const comparisonControls = make("div", undefined, { class: "comparison-controls" });
  const baselineLabel = make("label", "基线实验");
  const baselineSelect = make("select", undefined, { "aria-label": "选择基线实验" });
  const compareButton = button("与当前实验对比", () => void compareExperiments());
  baselineLabel.append(baselineSelect);
  comparisonControls.append(baselineLabel, compareButton);
  const comparisonBody = make("div", "先运行至少两次实验，再选择基线进行对比。", { class: "comparison-body", "aria-live": "polite" });
  comparisonSection.append(comparisonControls, comparisonBody);
  on(baselineSelect, "change", clearComparison);
  detailSection.append(detailHeader, detail, comparisonSection);
  page.append(header, notification, error, controls, datasetSection, historySection, detailSection);
  root.append(stylesheet, page);

  function showError(cause) {
    if (!active()) return;
    error.textContent = errorText(cause);
    error.hidden = false;
  }

  function clearError() {
    error.textContent = "";
    error.hidden = true;
  }

  async function call(name, payload = {}) {
    if (typeof context.callBackend !== "function") throw new Error("当前宿主不支持扩展后端动作，请检查 DeerFlow 版本。");
    return await context.callBackend(name, payload);
  }

  function updateButtons() {
    const changingDataset = loadingDataset || importingDataset;
    const unavailable = !dataset || starting || changingDataset;
    offlineButton.disabled = unavailable || !builtinDataset();
    negativeButton.disabled = unavailable || !builtinDataset();
    agentButton.disabled = unavailable || previewOnly;
    datasetSelect.disabled = starting || changingDataset || !datasets.length;
    importButton.disabled = starting || changingDataset;
    importButton.textContent = importingDataset ? "正在导入…" : "导入 JSON 任务集";
    cancelButton.disabled = !selected || !isBusy(selected.status) || operationBusy;
    exportButton.disabled = !selected || operationBusy;
    refreshButton.disabled = refreshing;
    offlineButton.textContent = uncertainStarts.has(startKey("offline")) ? "重试启动离线演示" : "运行离线演示";
    negativeButton.textContent = uncertainStarts.has(startKey("offline", "wrong_total")) ? "重试启动负向自检" : "运行负向自检";
    agentButton.textContent = uncertainStarts.has(startKey("agent")) ? "重试启动真实 Agent" : "运行真实 Agent";
    if (changingDataset) datasetHint.textContent = "正在读取任务集与参考答案…";
    else if (!dataset) datasetHint.textContent = "请先选择或导入任务集。";
    else if (!builtinDataset()) datasetHint.textContent = previewOnly
      ? "自定义任务集请使用真实 Agent。当前为离线预览，可导入查看输入和参考答案；实际执行需进入 DeerFlow 宿主。"
      : "自定义任务集请使用真实 Agent；离线演示和负向自检仅适用于内置的三条用例。";
    else datasetHint.textContent = previewOnly ? "当前为离线预览，可以运行正常演示和负向自检。" : "已选择内置任务集，可运行离线演示、负向自检或真实 Agent。";
    const baseline = experiments.find((item) => item.id === baselineSelect.value);
    baselineSelect.disabled = comparing || !selected || isBusy(selected.status);
    compareButton.disabled = comparing || !selected || isBusy(selected.status) || !baseline || isBusy(baseline.status);
    compareButton.textContent = comparing ? "正在对比…" : "与当前实验对比";
  }

  function renderDatasetSelector() {
    const available = [...datasets];
    if (dataset && !available.some((entry) => datasetKey(entry) === datasetKey(dataset))) available.unshift(dataset);
    datasetSelect.replaceChildren();
    for (const entry of available) {
      const option = make("option", `${entry.name ?? entry.id} · v${entry.revision}${entry.id === "fixed-cases-v1" ? " · 内置" : ""}`, { value: datasetKey(entry) });
      datasetSelect.append(option);
    }
    if (dataset) datasetSelect.value = datasetKey(dataset);
  }

  async function chooseDataset() {
    if (!active() || starting || importingDataset || loadingDataset) return;
    const generation = ++datasetGeneration;
    loadingDataset = true;
    clearError();
    updateButtons();
    try {
      const [id, revision] = JSON.parse(datasetSelect.value);
      const value = await call("dataset_get", { id, revision });
      if (!active() || generation !== datasetGeneration) return;
      dataset = value;
      renderDataset();
      notification.textContent = "任务集已切换。请先核对输入和参考答案，再启动实验。";
    } catch (cause) { showError(cause); }
    finally {
      if (generation === datasetGeneration) {
        loadingDataset = false;
        if (active()) { renderDatasetSelector(); updateButtons(); }
      }
    }
  }

  async function importDataset() {
    const file = importInput.files?.[0];
    if (!active() || !file || starting || importingDataset || loadingDataset) return;
    importingDataset = true;
    ++datasetGeneration;
    clearError();
    updateButtons();
    try {
      if (file.size > 240 * 1024) throw new Error("JSON 文件不能超过 240 KiB。请导入最多 20 条用例的小任务集。");
      let value;
      try { value = JSON.parse(await file.text()); }
      catch { throw new Error("文件不是合法的 JSON，请检查格式后重新导入。"); }
      if (!active()) return;
      const imported = await call("dataset_import", { dataset: value });
      if (!active()) return;
      dataset = imported;
      catalogLoaded = false;
      renderDatasetSelector();
      renderDataset();
      await refresh();
      if (active()) notification.textContent = "任务集已导入并选中。输入和参考答案已展示，请核对后运行。";
    } catch (cause) { showError(cause); }
    finally {
      importingDataset = false;
      importInput.value = "";
      if (active()) updateButtons();
    }
  }

  function renderDataset() {
    if (!dataset) return;
    datasetTitle.textContent = dataset.name ?? "首批固定用例";
    const cases = Array.isArray(dataset.cases) ? dataset.cases : [];
    datasetMeta.textContent = `${cases.length} 条用例 · ${dataset.id ?? "—"} · 版本 ${dataset.revision ?? "—"} · 严格 JSON 判分，字段顺序不影响结果。参考答案仅用于评分。`;
    caseList.replaceChildren();
    for (const item of cases) {
      const row = make("article", undefined, { class: "case" });
      const heading = make("div", undefined, { class: "case-heading" });
      heading.append(make("h4", item.title ?? item.id), make("code", item.id));
      const columns = make("div", undefined, { class: "case-columns" });
      columns.append(field("发送给 Agent 的输入", item.prompt), field("仅供评分器的参考答案", item.expected));
      row.append(heading, columns);
      caseList.append(row);
    }
  }

  function renderHistory() {
    const fingerprint = `${selectedId}:${JSON.stringify(experiments)}`;
    if (fingerprint === historyFingerprint) return;
    historyFingerprint = fingerprint;
    historyLife.abort();
    historyLife = new AbortController();
    history.replaceChildren();
    if (!experiments.length) {
      history.append(make("p", "尚未启动实验。点击上方“运行离线演示”后，才会出现实验记录、实际输出和评分。", { class: "empty" }));
      return;
    }
    for (const item of experiments) {
      const row = button("", () => {
        if (selectedId === item.id && selected) return;
        selectedId = item.id;
        selected = null;
        clearComparison();
        detail.textContent = "正在读取实验详情…";
        renderHistory();
        updateButtons();
        void refresh();
      }, `history-row${selectedId === item.id ? " selected" : ""}`, historyLife.signal);
      row.setAttribute("aria-pressed", String(selectedId === item.id));
      const identity = make("span", undefined, { class: "history-identity" });
      identity.append(make("strong", experimentName(item)), make("code", item.id));
      const info = make("span", undefined, { class: "history-info" });
      info.append(make("span", timestamp(item.created_at), { class: "muted" }), badge(label(item.status), item.status));
      if (item.summary) info.append(make("span", `通过 ${item.summary.passed ?? 0} / ${item.summary.total ?? 0}`));
      row.append(identity, info);
      history.append(row);
    }
    renderBaselineSelector();
  }

  function clearComparison() {
    ++comparisonGeneration;
    comparing = false;
    comparisonBody.replaceChildren(make("p", "选择一个已结束的历史实验作为基线，再点击“与当前实验对比”。", { class: "empty" }));
    updateButtons();
  }

  function renderBaselineSelector() {
    const candidates = experiments.filter((item) => item.id !== selectedId && !isBusy(item.status));
    const fingerprint = `${selectedId}:${JSON.stringify(candidates)}`;
    if (fingerprint === baselineFingerprint) return;
    baselineFingerprint = fingerprint;
    const previous = baselineSelect.value;
    baselineSelect.replaceChildren();
    if (!candidates.length) baselineSelect.append(make("option", "还没有其他已结束的实验", { value: "" }));
    for (const entry of candidates) {
      baselineSelect.append(make("option", `${experimentName(entry)} · ${entry.summary?.passed ?? 0}/${entry.summary?.total ?? 0} 通过 · ${entry.id}`, { value: entry.id }));
    }
    baselineSelect.value = candidates.some((entry) => entry.id === previous) ? previous : candidates[0]?.id ?? "";
    if (previous !== baselineSelect.value) clearComparison();
    updateButtons();
  }

  async function compareExperiments() {
    const baselineId = baselineSelect.value;
    const candidateId = selectedId;
    if (!active() || comparing || !baselineId || !selected || isBusy(selected.status)) return;
    const generation = ++comparisonGeneration;
    comparing = true;
    clearError();
    comparisonBody.textContent = "正在核对任务集版本，并比较逐项结果…";
    updateButtons();
    try {
      const result = await call("experiment_compare", { baseline_id: baselineId, candidate_id: candidateId });
      if (!active() || generation !== comparisonGeneration || candidateId !== selectedId || baselineId !== baselineSelect.value) return;
      comparisonBody.replaceChildren();
      if (result.compatible !== true) {
        const differentDataset = result.baseline_dataset_hash && result.candidate_dataset_hash && result.baseline_dataset_hash !== result.candidate_dataset_hash;
        comparisonBody.append(make("p", differentDataset
          ? "无法直接对比：两个实验的任务集内容不同。请先选择同一任务集、同一版本的实验。"
          : "无法直接对比：请确认两次实验都已结束，且使用同一版本的任务集。", { class: "attention" }));
        if (result.reason) comparisonBody.append(make("p", `原始诊断：${result.reason}`, { class: "muted" }));
        return;
      }
      comparisonBody.append(make("p", result.is_model_comparison === true
        ? "以下对比的是当前任务集上的观测结果。"
        : "包含离线演示、负向自检或未完整评分的结果，仅用于检查执行和判分变化，不代表模型能力差异。", { class: "mode-notice" }));
      if (result.baseline_scenario !== result.candidate_scenario) comparisonBody.append(make("p", "两次实验采用了不同响应场景；正常演示和故意总额 999 的负向自检可以用来验证评分器。", { class: "muted" }));
      if (result.notice) {
        const diagnostic = make("details", undefined, { class: "comparison-diagnostic" });
        diagnostic.append(make("summary", "查看原始对比诊断"), make("p", result.notice, { class: "muted" }));
        comparisonBody.append(diagnostic);
      }
      comparisonBody.append(make("p", `基线：${baselineId} → 当前：${candidateId}`, { class: "muted comparison-ids" }));
      const summary = result.summary ?? {};
      const metrics = make("dl", undefined, { class: "summary" });
      for (const [name, value] of [["改善", summary.improved ?? 0], ["退步", summary.regressed ?? 0], ["无变化", summary.unchanged ?? 0], ["未评分", summary.unscored ?? 0]]) {
        const metric = make("div");
        metric.append(make("dt", name), make("dd", value));
        metrics.append(metric);
      }
      comparisonBody.append(metrics);
      const changes = { improved: "改善", regressed: "退步", unchanged: "无变化", unscored: "未评分" };
      const duration = (value) => Number.isFinite(value) ? `${Math.round(value * 100) / 100} ms` : "未知";
      for (const item of Array.isArray(result.items) ? result.items : []) {
        const row = make("article", undefined, { class: "comparison-row" });
        const heading = make("div", undefined, { class: "section-heading" });
        heading.append(make("h4", item.title ?? item.case_id), badge(changes[item.change] ?? item.change ?? "未评分", item.change));
        row.append(heading, make("code", item.case_id));
        row.append(make("p", `判分：${item.baseline_verdict ? label(item.baseline_verdict) : "未评分"} → ${item.candidate_verdict ? label(item.candidate_verdict) : "未评分"}`));
        row.append(make("p", `耗时：${duration(item.baseline_latency_ms)} → ${duration(item.candidate_latency_ms)}`, { class: "muted" }));
        const delta = item.latency_delta_ms;
        row.append(make("p", Number.isFinite(delta)
          ? `耗时差：${delta > 0 ? "+" : ""}${duration(delta)}${delta === 0 ? "（无变化）" : delta < 0 ? "（当前更快）" : "（当前更慢）"}`
          : "耗时差：未知"));
        comparisonBody.append(row);
      }
      comparisonBody.append(make("p", "耗时差 = 当前实验 − 基线实验。点选历史实验可进一步查看原始输入、输出和评分证据。", { class: "footnote" }));
    } catch (cause) {
      if (active() && generation === comparisonGeneration) {
        comparisonBody.textContent = "本次对比未完成，可以重试。";
        showError(cause);
      }
    } finally {
      if (generation === comparisonGeneration) { comparing = false; if (active()) updateButtons(); }
    }
  }

  function renderDetail() {
    const fingerprint = `${selectedId}:${JSON.stringify(selected)}`;
    if (fingerprint === detailFingerprint) { updateButtons(); return; }
    detailFingerprint = fingerprint;
    detailLife.abort();
    detailLife = new AbortController();
    detail.replaceChildren();
    if (!selected) {
      detail.textContent = selectedId ? "正在读取实验详情…" : "尚未启动实验。点击上方“运行离线演示”后，才会出现实际输出和评分。";
      updateButtons();
      return;
    }
    const experiment = selected;
    const items = Array.isArray(experiment.items) ? experiment.items : [];
    const meta = make("div", undefined, { class: "experiment-meta" });
    meta.append(make("code", experiment.id), badge(label(experiment.status), experiment.status), make("span", timestamp(experiment.created_at), { class: "muted" }));
    detail.append(meta);
    detail.append(make("p", experiment.mode === "offline"
      ? (experiment.offline_scenario ?? experiment.scenario) === "wrong_total"
        ? "负向自检：故意把销售总额改为 999。预期 2 条通过、1 条未通过，用于确认评分器能发现错误，不代表模型成绩。"
        : "离线流程演示：输出为预置响应，仅用于验证执行和判分流程，不代表模型能力。"
      : "真实 Agent 评测：以下为各独立会话的实际执行结果与规则评分。", { class: "mode-notice" }));
    if (experiment.dataset) detail.append(make("p", `本实验任务集：${experiment.dataset.name ?? experiment.dataset.id} · ${experiment.dataset.id} · 版本 ${experiment.dataset.revision}`, { class: "muted" }));
    if (experiment.status === "needs_reconciliation") {
      detail.append(make("p", "服务重启后需要核对实验。请先检查各条原始会话，确认已有运行的结果，避免重复提交。", { class: "attention" }));
    }
    if (experiment.error) detail.append(make("p", String(experiment.error), { class: "error-line" }));
    const summary = experiment.summary ?? {};
    const metrics = make("dl", undefined, { class: "summary" });
    for (const [name, value] of [["用例数", summary.total ?? items.length], ["通过", summary.passed ?? 0], ["未通过", summary.failed ?? 0], ["未评分", summary.unscored ?? 0]]) {
      const cell = make("div");
      cell.append(make("dt", name), make("dd", value));
      metrics.append(cell);
    }
    detail.append(metrics);
    for (const item of items) {
      const article = make("article", undefined, { class: "result-item" });
      const heading = make("div", undefined, { class: "section-heading" });
      const title = make("div");
      title.append(make("h4", item.title ?? item.case_id), make("code", item.case_id));
      const state = make("div", undefined, { class: "button-row" });
      state.append(badge(`执行：${label(item.status)}`, item.status));
      if (item.verdict) state.append(badge(`判分：${label(item.verdict)}`, item.verdict));
      if (Number.isFinite(item.latency_ms)) state.append(make("span", `${Math.round(item.latency_ms)} ms`, { class: "muted" }));
      heading.append(title, state);
      article.append(heading);
      if (item.error) article.append(make("p", String(item.error), { class: "error-line" }));
      if (item.output !== null && item.output !== undefined) article.append(field("实际输出", item.output));
      else article.append(make("p", "尚无输出。", { class: "muted" }));
      if (Array.isArray(item.checks) && item.checks.length) {
        const checks = make("div", undefined, { class: "check-list" });
        checks.append(make("h4", "评分检查"));
        checks.append(make("p", "所有检查项均通过，用例才算通过。点击每一项可对照期望值和实际值。", { class: "muted check-help" }));
        for (const check of item.checks) {
          const row = make("details", undefined, { class: "check" });
          const checkKey = `${experiment.id}:${item.case_id}:${check.name}`;
          row.open = expandedChecks.has(checkKey);
          on(row, "toggle", () => {
            if (row.open) expandedChecks.add(checkKey);
            else expandedChecks.delete(checkKey);
          }, detailLife.signal);
          const explanation = CHECK_LABELS[check.name] ?? `字段校验：${check.name ?? "检查项"}`;
          row.append(make("summary", `${check.passed === true ? "通过" : "未通过"} · ${explanation}`));
          row.append(make("code", `原始检查标识：${check.name ?? "—"}`, { class: "check-raw" }));
          row.append(field("期望", check.expected), field("实际", check.actual));
          checks.append(row);
        }
        article.append(checks);
      }
      if (item.thread_id || item.run_id) {
        const provenance = make("div", undefined, { class: "provenance" });
        if (item.thread_id) provenance.append(make("p", `会话：${item.thread_id}`));
        if (item.run_id) provenance.append(make("p", `Run：${item.run_id}`));
        if (item.thread_id && typeof context.openConversation === "function") {
          const open = button("打开原始会话", () => {
            open.disabled = true;
            void Promise.resolve().then(() => context.openConversation(item.thread_id)).catch(showError).finally(() => {
              if (active()) open.disabled = false;
            });
          }, "", detailLife.signal);
          provenance.append(open);
        }
        article.append(provenance);
      }
      detail.append(article);
    }
    if (!items.length) detail.append(make("p", "实验已登记，正在准备各条用例。", { class: "empty" }));
    detail.append(make("p", "耗时为本工作台观测值。完整 Token 和费用在未提供权威记录时保持未知。", { class: "footnote" }));
    updateButtons();
  }

  function schedulePoll() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (active() && (isBusy(selected?.status) || experiments.some((item) => isBusy(item.status)))) {
      timer = setTimeout(() => { timer = null; void refresh(); }, 1500);
    }
  }

  async function refresh() {
    if (!active()) return;
    if (refreshing) { refreshRequested = true; return; }
    if (timer !== null) clearTimeout(timer);
    timer = null;
    refreshing = true;
    updateButtons();
    try {
      const generation = datasetGeneration;
      const requestDataset = !dataset;
      const requestCatalog = !catalogLoaded;
      const operations = [call("experiment_list")];
      if (requestDataset) operations.push(call("dataset_get"));
      if (requestCatalog) operations.push(call("dataset_list"));
      const results = await Promise.allSettled(operations);
      if (!active()) return;
      let offset = 1;
      if (requestDataset) {
        const result = results[offset++];
        if (result.status === "fulfilled" && generation === datasetGeneration && !dataset) {
          dataset = result.value;
          renderDataset();
        } else if (result.status === "rejected") showError(result.reason);
      }
      if (requestCatalog) {
        const result = results[offset];
        if (result.status === "fulfilled" && Array.isArray(result.value?.items)) {
          datasets = result.value.items;
          catalogLoaded = true;
        } else showError(result.status === "rejected" ? result.reason : new Error("任务集列表响应格式异常。"));
      }
      if (requestDataset || requestCatalog) renderDatasetSelector();
      if (results[0].status === "rejected") throw results[0].reason;
      const data = results[0].value;
      if (!data || !Array.isArray(data.items)) throw new Error("实验列表响应格式异常，请刷新或检查扩展版本。");
      experiments = data.items;
      if (!selectedId && experiments.length) selectedId = experiments[0].id;
      renderHistory();
      if (selectedId) {
        const requestedId = selectedId;
        const value = await call("experiment_get", { id: requestedId });
        if (!active()) return;
        if (selectedId === requestedId) {
          selected = value;
          renderDetail();
        }
      }
      notification.textContent = experiments.length
        ? `已读取 ${experiments.length} 次实验；执行中的实验会自动更新。`
        : builtinDataset()
          ? "固定用例已就绪。点击“运行离线演示”开始，完成后可查看实际输出和逐项评分。"
          : "任务集已就绪。请核对输入和参考答案；自定义任务集需使用真实 Agent。";
    } catch (cause) {
      if (active()) {
        notification.textContent = "读取未完成。可以点击刷新重试。";
        showError(cause);
      }
    } finally {
      refreshing = false;
      if (active()) {
        updateButtons();
        if (refreshRequested) { refreshRequested = false; void refresh(); }
        else schedulePoll();
      }
    }
  }

  async function start(mode, scenario) {
    if (!active() || starting || !dataset || loadingDataset || importingDataset) return;
    if (mode === "offline" && !builtinDataset()) { showError(new Error("自定义任务集请使用真实 Agent。")); return; }
    if (mode === "agent" && previewOnly) { showError(new Error("真实 Agent 仅在 DeerFlow 宿主中开放。")); return; }
    starting = true;
    clearError();
    const pendingKey = startKey(mode, scenario);
    const key = uncertainStarts.get(pendingKey) ?? `evalui-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
    uncertainStarts.set(pendingKey, key);
    const selectedDataset = dataset;
    notification.textContent = "正在登记实验…";
    updateButtons();
    try {
      const value = await call("experiment_start", {
        mode, idempotency_key: key,
        dataset_id: selectedDataset.id,
        dataset_revision: selectedDataset.revision,
        ...(scenario ? { offline_scenario: scenario } : {}),
      });
      if (!active()) return;
      if (!value?.id) throw new Error("启动响应未包含实验编号。重试会沿用同一个请求标识。");
      uncertainStarts.delete(pendingKey);
      selectedId = value.id;
      selected = value;
      clearComparison();
      renderDetail();
      notification.textContent = "实验已登记，正在读取执行进度。";
      await refresh();
    } catch (cause) {
      if (active()) {
        showError(cause);
        notification.textContent = "启动结果尚未确认。再次点击相同模式将沿用同一请求标识。";
      }
    } finally {
      starting = false;
      if (active()) updateButtons();
    }
  }

  async function cancel() {
    if (!active() || !selected || operationBusy || !isBusy(selected.status)) return;
    operationBusy = true;
    clearError();
    updateButtons();
    const requestedId = selected.id;
    try {
      const value = await call("experiment_cancel", { id: requestedId });
      if (!active()) return;
      if (selectedId === requestedId) { selected = value; renderDetail(); }
      notification.textContent = "已提交取消请求，正在核对最终状态。";
      await refresh();
    } catch (cause) { showError(cause); }
    finally { operationBusy = false; if (active()) updateButtons(); }
  }

  async function exportReport() {
    if (!active() || !selected || operationBusy) return;
    operationBusy = true;
    clearError();
    updateButtons();
    try {
      const report = await call("report_export", { id: selected.id });
      if (!active()) return;
      if (report?.content === undefined) throw new Error("报告内容缺失，请刷新后重试。");
      const blob = new Blob([pretty(report.content)], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      downloadURLs.add(url);
      const link = make("a", "下载", { href: url, download: String(report.filename ?? "eval-report.json").replace(/[\\/\u0000]/g, "_") });
      root.append(link);
      link.click();
      link.remove();
      setTimeout(() => { URL.revokeObjectURL(url); downloadURLs.delete(url); }, 1000);
      notification.textContent = "已生成 JSON 报告下载。";
    } catch (cause) { showError(cause); }
    finally { operationBusy = false; if (active()) updateButtons(); }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    life.abort();
    historyLife.abort();
    detailLife.abort();
    if (timer !== null) clearTimeout(timer);
    for (const url of downloadURLs) URL.revokeObjectURL(url);
    downloadURLs.clear();
    context.signal?.removeEventListener("abort", dispose);
    root.replaceChildren();
  }

  context.signal?.addEventListener("abort", dispose, { once: true });
  updateButtons();
  if (context.signal?.aborted) dispose();
  else void refresh();
  return { dispose };
}

export default {
  apiVersion: 1,
  module: "eval-center",
  icon: "activity",
  surfaces: [{
    id: "experiments",
    slot: "page",
    title: "Agent 评测",
    navigation: { label: "Agent Evaluation", labelZh: "Agent 评测", icon: "activity" },
    mount: mountEvaluation,
  }],
};
