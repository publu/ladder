const state = {
  data: null,
  view: "cascade",
  policy: "active",
  stage: "cheap_cv",
  signal: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const number = new Intl.NumberFormat("en-US");
const pct = (value, total, digits = 1) => `${((100 * value) / Math.max(total, 1)).toFixed(digits)}%`;

function setText(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value;
}

function stageById(id) {
  return state.data.stages.find((stage) => stage.id === id);
}

function signalForStage(stageId) {
  return state.data.signals.filter((signal) => signal.stage === stageId);
}

function renderStageIndex() {
  const root = $("[data-stage-index]");
  root.innerHTML = state.data.stages
    .map(
      (stage) => `
        <button class="stage-button ${stage.id === state.stage ? "is-active" : ""}" type="button" data-stage="${stage.id}">
          <span>${stage.level}</span>
          <strong>${stage.label}</strong>
          <small>${stage.version || "—"}</small>
        </button>`,
    )
    .join("");
  $$('[data-stage]', root).forEach((button) => {
    button.addEventListener("click", () => selectStage(button.dataset.stage));
  });
}

function cheapStages() {
  return state.data.stages.filter((stage) => stage.id !== "vlm");
}

function renderCascade() {
  const total = state.data.total;
  const active = state.data.active_threshold_funnel;
  const persisted = state.data.persisted_verdicts;
  const stages = cheapStages();
  let survivor = total;

  const cards = stages.map((stage) => {
    const incoming = survivor;
    const fail = stage.cascade_exit.fail;
    const defer = stage.cascade_exit.defer;
    survivor = Math.max(0, survivor - fail - defer);
    const displayed = state.policy === "active" ? incoming : stage.processed;
    const fill = Math.max(2, (100 * displayed) / total);
    return `
      <article class="cascade-stage ${stage.id === state.stage ? "is-active" : ""}" data-cascade-stage="${stage.id}">
        <div class="stage-cap"><span>${stage.level}</span><b>${stage.version}</b></div>
        <button class="flow-column" type="button" data-stage-select="${stage.id}" aria-label="Inspect ${stage.label}">
          <i class="flow-fill" style="height:${fill}%"></i>
          <strong class="stage-count">${number.format(displayed)}</strong>
        </button>
        <div class="stage-detail">
          <strong>${stage.label}</strong>
          <span>${state.policy === "active" ? "enter rung" : `${stage.coverage_pct}% processed`}</span>
        </div>
        ${
          state.policy === "active"
            ? `<div class="exit-pair">
                <div class="fail"><span>FAIL EXIT</span><strong>${number.format(fail)}</strong></div>
                <div class="deferred"><span>DEFER</span><strong>${number.format(defer)}</strong></div>
              </div>`
            : ""
        }
      </article>`;
  });

  $("[data-cascade-chart]").innerHTML = cards.join("");
  $$('[data-stage-select]').forEach((button) => button.addEventListener("click", () => selectStage(button.dataset.stageSelect)));

  const outcomes =
    state.policy === "active"
      ? [
          ["good", "CLEARED", active.clear, "good through every cheap layer"],
          ["defer", "TO JUDGE", active.defer, "uncertain under active bands"],
          ["bad", "DECIDED FAIL", active.fail, "confident exit under active bands"],
        ]
      : [
          ["good", "PASS", persisted.PASS, "materialized verdict row"],
          ["defer", "BORDERLINE", persisted.BDLN, "materialized verdict row"],
          ["bad", "FAIL", persisted.FAIL, "materialized verdict row"],
        ];
  $("[data-cascade-outcomes]").innerHTML = outcomes
    .map(
      ([tone, label, value, note]) => `
        <article class="outcome-card ${tone}">
          <span>${label}</span>
          <strong>${number.format(value)}</strong>
          <small>${pct(value, total)} · ${note}</small>
          <i></i>
        </article>`,
    )
    .join("");

  const notice = $("[data-policy-notice]");
  if (state.policy === "active") {
    notice.textContent = state.data.interpretation.active_thresholds;
    notice.style.borderColor = "var(--good-dim)";
  } else {
    notice.textContent = `${state.data.interpretation.persisted} Rungs show processing coverage; a per-rung persisted flow is not reconstructed.`;
    notice.style.borderColor = "var(--defer)";
  }
}

function chartBars(signal, className = "") {
  const maximum = Math.max(...signal.bins, 1);
  return signal.bins
    .map((count) => {
      const height = Math.max(1, (100 * Math.log1p(count)) / Math.log1p(maximum));
      return `<b class="${className}" style="--h:${height.toFixed(2)}%" title="${number.format(count)} clips"></b>`;
    })
    .join("");
}

function renderSignals() {
  setText("[data-signal-count]", state.data.signals.length);
  $("[data-signal-grid]").innerHTML = state.data.signals
    .map(
      (signal) => `
        <article class="signal-card" data-signal-card="${signal.id}" tabindex="0">
          <header><span>${stageById(signal.stage).level} / ${signal.id.toUpperCase()}</span><small>${number.format(signal.samples)} SAMPLES</small></header>
          <h3>${signal.label}</h3>
          <div class="signal-mini-chart">
            ${chartBars(signal)}
            <i style="--x:${(100 * signal.band[0]) / signal.maximum}%"></i>
            <i style="--x:${(100 * signal.band[1]) / signal.maximum}%"></i>
          </div>
          <footer><span>P50 <strong>${signal.quantiles.p50}</strong></span><span>BAND <strong>${signal.band.join(" → ")}</strong></span><span>LOG HEIGHT</span></footer>
        </article>`,
    )
    .join("");
  $$('[data-signal-card]').forEach((card) => {
    const activate = () => {
      state.signal = card.dataset.signalCard;
      selectStage(state.data.signals.find((signal) => signal.id === state.signal).stage);
    };
    card.addEventListener("click", activate);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") activate();
    });
  });
}

function policyColumn(title, subtitle, values) {
  const total = state.data.total;
  return `
    <article class="policy-column">
      <header><span>${title.kicker}</span><h3>${title.name}</h3><p>${subtitle}</p></header>
      <div class="outcome-stack">
        ${values.map(([tone, , value]) => `<i class="${tone}" style="width:${pct(value, total, 3)}"></i>`).join("")}
      </div>
      <div class="policy-values">
        ${values
          .map(
            ([tone, label, value]) => `<div class="${tone}"><i></i><span>${label}</span><strong>${number.format(value)}</strong></div>`,
          )
          .join("")}
      </div>
    </article>`;
}

function renderOutcomes() {
  const persisted = state.data.persisted_verdicts;
  const active = state.data.active_threshold_funnel;
  $("[data-comparison]").innerHTML =
    policyColumn(
      { kicker: "MATERIALIZED / VERDICT V1", name: "Persisted database" },
      state.data.interpretation.persisted,
      [
        ["good", "PASS", persisted.PASS],
        ["defer", "BORDERLINE", persisted.BDLN],
        ["bad", "FAIL", persisted.FAIL],
      ],
    ) +
    policyColumn(
      { kicker: "REPLAY / CURRENT CODE", name: "Active thresholds" },
      state.data.interpretation.active_thresholds,
      [
        ["good", "CLEARED", active.clear],
        ["defer", "DEFERRED", active.defer],
        ["bad", "FAIL", active.fail],
      ],
    );

  const routes = Object.entries(state.data.persisted_routes).sort((a, b) => b[1] - a[1]);
  $("[data-route-table]").innerHTML = routes
    .map(
      ([route, value]) => `<div><span>${route}</span><strong>${number.format(value)}</strong><small>${pct(value, state.data.total)}</small></div>`,
    )
    .join("");
}

function renderDecisionBars(stage) {
  const entries = Object.entries(stage.decisions);
  const maximum = Math.max(...entries.map(([, value]) => value), 1);
  $("[data-decision-bars]").innerHTML = entries
    .map(([key, value]) => {
      const tone = key === "good" || key === "PASS" ? "good" : key === "unsure" || key === "BORDERLINE" ? "unsure" : "bad";
      return `
        <div class="decision-row ${tone}">
          <span>${key.toUpperCase()}</span>
          <i><b style="--w:${(100 * value) / maximum}%"></b></i>
          <strong>${number.format(value)}</strong>
        </div>`;
    })
    .join("");
}

function renderHistogram(signal) {
  if (!signal) {
    setText("[data-histogram-name]", "no scalar signal");
    $("[data-histogram-bars]").innerHTML = "";
    $("[data-threshold-low]").style.display = "none";
    $("[data-threshold-high]").style.display = "none";
    $("[data-quantiles]").innerHTML = "";
    return;
  }
  setText("[data-histogram-name]", signal.label);
  setText("[data-histogram-max]", signal.maximum);
  $("[data-histogram-bars]").innerHTML = chartBars(signal).replaceAll("<b", "<i").replaceAll("</b>", "</i>");
  const low = $("[data-threshold-low]");
  const high = $("[data-threshold-high]");
  low.style.display = "block";
  high.style.display = "block";
  low.style.setProperty("--x", `${(100 * signal.band[0]) / signal.maximum}%`);
  high.style.setProperty("--x", `${(100 * signal.band[1]) / signal.maximum}%`);
  $("[data-quantiles]").innerHTML = Object.entries(signal.quantiles)
    .map(([key, value]) => `<div><dt>${key.toUpperCase()}</dt><dd>${value ?? "—"}</dd></div>`)
    .join("");
}

function renderReasons(stage) {
  $("[data-reasons]").innerHTML = stage.reasons.length
    ? stage.reasons.map(({ reason, count }) => `<div class="reason-row"><span>${reason}</span><strong>${number.format(count)}</strong></div>`).join("")
    : '<p class="reason-empty">No reason strings recorded for this active version.</p>';
}

function renderVideoEvidence(stage) {
  const evidence = state.data.public_video_sets;
  if (!evidence) return;
  setText("[data-video-relationship]", evidence.relationship);
  const preferred = stage.id === "meta" || stage.id === "cheap_cv" ? "egodex" : "egodex_fold";
  const sets = [...evidence.sets].sort((a) => (a.id === preferred ? -1 : 1));
  $("[data-video-sets]").innerHTML = sets
    .map(
      (item, index) => `
        <a class="video-set-link ${index === 0 ? "is-primary" : ""}" href="${item.url}" target="_blank" rel="noopener">
          <span><small>${index === 0 ? "RELATED TO SELECTED RUNG" : "ALSO AVAILABLE"}</small><strong>${item.label}</strong></span>
          <b>${number.format(item.episodes)} EP <i>↗</i></b>
        </a>`,
    )
    .join("");
}

function selectStage(id) {
  state.stage = id;
  const stage = stageById(id);
  const signals = signalForStage(id);
  if (!signals.some((signal) => signal.id === state.signal)) state.signal = signals[0]?.id || null;

  setText("[data-inspector-level]", stage.level);
  setText("[data-inspector-version]", stage.version || "—");
  setText("[data-inspector-engine]", stage.engine.toUpperCase());
  setText("[data-inspector-label]", stage.label);
  setText("[data-inspector-checks]", stage.checks);
  setText("[data-inspector-coverage]", `${stage.coverage_pct}%`);
  setText("[data-inspector-processed]", number.format(stage.processed));
  setText("[data-inspector-total]", number.format(state.data.total));
  $("[data-coverage-bar]").style.width = `${stage.coverage_pct}%`;
  renderDecisionBars(stage);
  renderReasons(stage);
  renderVideoEvidence(stage);

  const select = $("[data-signal-select]");
  select.innerHTML = signals.length
    ? signals.map((signal) => `<option value="${signal.id}" ${signal.id === state.signal ? "selected" : ""}>${signal.id}</option>`).join("")
    : '<option value="">none</option>';
  select.disabled = !signals.length;
  renderHistogram(state.data.signals.find((signal) => signal.id === state.signal));

  renderStageIndex();
  renderCascade();
}

function switchView(view) {
  state.view = view;
  $$('[data-view-button]').forEach((button) => button.classList.toggle("is-active", button.dataset.viewButton === view));
  $$('[data-view]').forEach((panel) => panel.classList.toggle("is-active", panel.dataset.view === view));
  const labels = {
    cascade: ["ACTIVE THRESHOLD REPLAY", "Cascade topology"],
    signals: ["MEASURED RAW SCORES", "Signal distributions"],
    outcomes: ["POLICY STATE COMPARISON", "Persisted vs. active"],
  };
  setText("[data-view-kicker]", labels[view][0]);
  setText("[data-view-title]", labels[view][1]);
  $(".policy-switch").style.visibility = view === "cascade" ? "visible" : "hidden";
}

function wireControls() {
  $$('[data-view-button]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewButton)));
  $$('[data-policy]').forEach((button) => {
    button.addEventListener("click", () => {
      state.policy = button.dataset.policy;
      $$('[data-policy]').forEach((node) => node.classList.toggle("is-active", node === button));
      renderCascade();
    });
  });
  $("[data-signal-select]").addEventListener("change", (event) => {
    state.signal = event.target.value || null;
    renderHistogram(state.data.signals.find((signal) => signal.id === state.signal));
  });
}

async function boot() {
  try {
    const response = await fetch("./ladder-snapshot.json");
    if (!response.ok) throw new Error(`snapshot request returned ${response.status}`);
    state.data = await response.json();
    state.signal = signalForStage(state.stage)[0]?.id || null;
    setText("[data-dataset]", state.data.dataset.toUpperCase());
    setText("[data-snapshot]", state.data.source.sha256.slice(7, 19).toUpperCase());
    setText("[data-total]", number.format(state.data.total));
    renderStageIndex();
    renderSignals();
    renderOutcomes();
    wireControls();
    selectStage(state.stage);
    switchView(state.view);
    $("[data-loading]").classList.add("is-done");
  } catch (error) {
    const loading = $("[data-loading]");
    loading.innerHTML = `<strong>SNAPSHOT ERROR / ${String(error.message).toUpperCase()}</strong>`;
  }
}

boot();
