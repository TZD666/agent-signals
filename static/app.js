const SUPPORTED_SCHEMA = 1;

const STATUS_LABELS = {
  idle: "空闲",
  thinking: "思考中",
  completed: "已完成",
  needs_input: "需要输入",
  stalled: "疑似卡死",
  error: "错误",
};

const SOURCE_LABELS = { claude: "Claude", codex: "Codex" };
const SOURCE_STATE_LABELS = {
  live: "正常",
  unavailable: "数据源不可用",
  schema_mismatch: "数据结构已变",
  unknown: "未知",
};

const ALERT_STATUSES = new Set(["needs_input", "stalled"]);
const LOAD_HIGH_PCT = 80;
const MINUTE_MS = 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const POLL_WAIT_S = 8;
const POLL_FLOOR_MS = 250;
const BACKOFF_MAX_MS = 15000;

const grids = {
  claude: document.querySelector("#claudeGrid"),
  codex: document.querySelector("#codexGrid"),
};

const STORAGE_KEYS = {
  acknowledged: "agent-signals.acknowledged-completions",
  dismissedCodex: "agent-signals.dismissed-codex",
  lockedCodex: "agent-signals.locked-codex",
  theme: "agent-signals.theme",
  historyOpen: "agent-signals.history-open",
};

const HISTORY_REFRESH_MS = 30000;

let snapshotSignature = "";
let connected = false;
let toastTimer;
let pollTimer;
let backoff = 1000;
let etag = "";
let latestData = { claude: [], codex: [] };
let audioContext = null;
let audioUnlocked = false;
let pendingFlash = new Set();
let historyTimer = null;
let expandedHistoryKey = "";
const previousStatus = new Map();

function readStoredObject(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function readStoredSet(key) {
  const value = readStoredObject(key);
  return new Set(Array.isArray(value) ? value : []);
}

const acknowledgedCompletions = readStoredObject(STORAGE_KEYS.acknowledged);
const dismissedCodex = readStoredSet(STORAGE_KEYS.dismissedCodex);
const lockedCodex = readStoredSet(STORAGE_KEYS.lockedCodex);

function storeObject(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // The live panel still works when browser storage is unavailable.
  }
}

function storeSet(key, value) {
  storeObject(key, [...value]);
}

function applyTheme(theme, persist = false) {
  const isLight = theme === "light";
  document.documentElement.dataset.theme = isLight ? "light" : "dark";
  const toggle = document.querySelector("#themeToggle");
  const label = isLight ? "切换到暗色模式" : "切换到亮色模式";
  toggle.textContent = isLight ? "☾" : "☀︎";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  document.querySelector('meta[name="theme-color"]').content = isLight
    ? "#f4f7fb"
    : "#050607";
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEYS.theme, isLight ? "light" : "dark");
    } catch {
      // Theme switching still works for the current page.
    }
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function relativeTime(timestamp) {
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function durationNote(agent) {
  if (agent.status === "stalled") {
    const quiet = Math.floor((Date.now() - Number(agent.quietSince || 0)) / MINUTE_MS);
    return quiet >= 1 ? `已 ${quiet} 分无新事件` : "无新事件";
  }
  if (agent.status !== "thinking") return "";
  const busy = Math.floor((Date.now() - Number(agent.updatedAt || 0)) / MINUTE_MS);
  return busy >= 1 ? `已持续 ${busy} 分钟` : "";
}

function formatTokensK(value) {
  return `${Math.round(Number(value) / 1000)}k`;
}

function formatStepGap(ms) {
  const seconds = Math.round(Number(ms) / 1000);
  return seconds < 60 ? `~${seconds}秒/步` : `~${Math.round(seconds / 60)}分/步`;
}

function loadSig(load) {
  return [load.contextPct, load.contextTokens, load.contextWindow, load.stepGapMs].join("|");
}

function loadText(load) {
  const parts = [];
  if (Number.isFinite(load.contextPct) && load.contextWindow) {
    parts.push(
      `${load.contextPct}% · ${formatTokensK(load.contextTokens)}/${formatTokensK(load.contextWindow)}`,
    );
  } else if (Number.isFinite(load.contextTokens)) {
    parts.push(formatTokensK(load.contextTokens));
  } else {
    parts.push("—");
  }
  if (Number.isFinite(load.stepGapMs)) parts.push(formatStepGap(load.stepGapMs));
  return parts.join(" · ");
}

function loadMarkup(agent) {
  const load = agent.load;
  if (!load || typeof load !== "object") return "";
  const known = Number.isFinite(load.contextPct);
  const pct = known ? Math.min(100, Math.max(0, Number(load.contextPct))) : 0;
  const high = known && pct >= LOAD_HIGH_PCT;
  return `
    <span class="agent-load${high ? " is-high" : ""}" data-load-sig="${escapeHtml(loadSig(load))}">
      <span class="load-bar${known ? "" : " is-unknown"}">${known ? `<i style="width:${pct}%"></i>` : ""}</span>
      <span class="load-text">${escapeHtml(loadText(load))}</span>
    </span>`;
}

function patchLoads(visible) {
  for (const platform of ["claude", "codex"]) {
    for (const agent of visible[platform]) {
      if (!agent.load || typeof agent.load !== "object") continue;
      const card = document.querySelector(
        `.agent-card[data-platform="${CSS.escape(platform)}"][data-agent-id="${CSS.escape(agent.id)}"]`,
      );
      const element = card?.querySelector(".agent-load");
      if (!element || element.dataset.loadSig === loadSig(agent.load)) continue;
      element.outerHTML = loadMarkup(agent);
    }
  }
}

function satelliteMarkup(satellites) {
  if (!satellites?.length) return "";
  const dots = satellites
    .slice(0, 8)
    .map(
      (satellite) => `
        <span
          class="satellite status-${escapeHtml(satellite.status)}"
          title="${escapeHtml(satellite.name)} · ${escapeHtml(STATUS_LABELS[satellite.status] || satellite.status)}"
        ></span>`,
    )
    .join("");
  const overflow =
    satellites.length > 8
      ? `<span class="satellite-overflow">+${satellites.length - 8}</span>`
      : "";
  return `<span class="satellites" aria-label="${satellites.length} 个子代理">${dots}${overflow}</span>`;
}

function completionStorageKey(platform, agent) {
  return `${platform}:${agent.id}`;
}

function effectiveStatus(platform, agent) {
  if (agent.status !== "completed") return agent.status;
  const acknowledged = Number(
    acknowledgedCompletions[completionStorageKey(platform, agent)] || 0,
  );
  return acknowledged >= Number(agent.completionId || 0) ? "idle" : "completed";
}

function effectiveAgent(platform, agent) {
  return {
    ...agent,
    status: effectiveStatus(platform, agent),
    satellites: (agent.satellites || []).map((satellite) => ({
      ...satellite,
      status: effectiveStatus(platform, satellite),
    })),
    isLocked: platform === "codex" && lockedCodex.has(agent.id),
  };
}

function visibleAgents(platform, agents) {
  let preferencesChanged = false;
  const visible = [];

  for (const rawAgent of agents) {
    const agent = effectiveAgent(platform, rawAgent);
    if (platform === "codex" && agent.status !== "idle") {
      if (dismissedCodex.delete(agent.id)) preferencesChanged = true;
    }
    if (
      platform === "codex" &&
      agent.status === "idle" &&
      (dismissedCodex.has(agent.id) ||
        (!agent.isLocked && Date.now() - Number(agent.updatedAt) >= DAY_MS))
    ) {
      continue;
    }
    visible.push(agent);
  }

  if (preferencesChanged) {
    storeSet(STORAGE_KEYS.dismissedCodex, dismissedCodex);
  }
  return visible;
}

function cardMarkup(agent) {
  const status = STATUS_LABELS[agent.status] || agent.status;
  const note = durationNote(agent);
  const showIdleControls = agent.platform === "codex" && agent.status === "idle";
  const controls = showIdleControls
    ? `
      <button
        class="agent-control agent-lock${agent.isLocked ? " is-locked" : ""}"
        type="button"
        data-action="lock"
        data-agent-id="${escapeHtml(agent.id)}"
        aria-label="${agent.isLocked ? "取消锁定" : "锁定"} ${escapeHtml(agent.name)}"
        title="${agent.isLocked ? "取消锁定，24 小时后可自动隐藏" : "锁定，超过 24 小时仍显示"}"
      >🔒</button>
      <button
        class="agent-control agent-dismiss"
        type="button"
        data-action="dismiss"
        data-agent-id="${escapeHtml(agent.id)}"
        aria-label="隐藏 ${escapeHtml(agent.name)}"
        title="关闭显示"
      >×</button>`
    : "";
  return `
    <article
      class="agent-card status-${escapeHtml(agent.status)}"
      data-platform="${escapeHtml(agent.platform)}"
      data-agent-id="${escapeHtml(agent.id)}"
    >
      ${controls}
      <button
        class="agent-open"
        type="button"
        ${agent.openable ? "" : "disabled"}
        aria-label="打开 ${escapeHtml(agent.name)}，当前${escapeHtml(status)}"
      >
        <span class="signal-stage" aria-hidden="true">
          <span class="signal-ring ring-one"></span>
          <span class="signal-ring ring-two"></span>
          <span class="signal-ring ring-three"></span>
          <span class="signal-orb"></span>
          ${satelliteMarkup(agent.satellites)}
        </span>
        <span class="agent-copy">
          <span class="agent-name" title="${escapeHtml(agent.name)}">${escapeHtml(agent.name)}</span>
          <span class="agent-state">${escapeHtml(status)}</span>
          ${note ? `<span class="agent-note">${escapeHtml(note)}</span>` : ""}
          ${loadMarkup(agent)}
          <span class="agent-meta">
            <span>${escapeHtml(agent.cwdLabel)}</span>
            <span>·</span>
            <time data-timestamp="${Number(agent.updatedAt) || 0}">${relativeTime(agent.updatedAt)}</time>
          </span>
        </span>
      </button>
    </article>`;
}

function emptyMarkup(platform, health) {
  if (health && health.state && health.state !== "live") {
    const label = SOURCE_STATE_LABELS[health.state] || health.state;
    return `
      <div class="empty-state is-down">
        <span class="empty-orb"></span>
        <p>
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(health.detail || "面板无法读取这个数据源，下面不是“没有任务”")}</span>
        </p>
      </div>`;
  }
  return `
    <div class="empty-state">
      <span class="empty-orb"></span>
      <p>没有检测到${platform === "claude" ? "前台 Terminal 会话" : "近期 Codex 任务"}</p>
    </div>`;
}

function showBanner(message) {
  const banner = document.querySelector("#banner");
  banner.textContent = message;
  banner.hidden = false;
}

function hideBanner() {
  document.querySelector("#banner").hidden = true;
}

function renderSources(data) {
  const sources = data.sources || {};
  const parts = [];

  for (const platform of ["claude", "codex"]) {
    const health = sources[platform] || { state: "unknown", detail: "" };
    const live = health.state === "live";
    const label = SOURCE_STATE_LABELS[health.state] || health.state;
    parts.push(`
      <span class="source ${live ? "is-live" : "is-down"}" title="${escapeHtml(health.detail || label)}">
        <i class="source-dot"></i>${escapeHtml(SOURCE_LABELS[platform])} · ${escapeHtml(label)}
      </span>`);
  }

  const notifications = data.notifications || {};
  if (notifications.state === "error") {
    parts.push(`
      <span class="source is-down" title="${escapeHtml(notifications.detail || "")}">
        <i class="source-dot"></i>Mac 通知发送失败
      </span>`);
  }
  if (!audioUnlocked) {
    parts.push(`<span class="source is-hint">点一下页面开启提示音</span>`);
  }

  document.querySelector("#sources").innerHTML = parts.join("");
}

function unlockAudio() {
  if (audioUnlocked) return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;
  try {
    audioContext = new AudioContextClass();
    audioContext.resume();
    audioUnlocked = true;
    snapshotSignature = "";
    render(latestData);
  } catch {
    // A muted panel is still a working panel.
  }
}

function playChime(kind) {
  if (!audioContext) return;
  const notes = kind === "attention" ? [880, 1174.7] : [659.3, 987.8];
  const start = audioContext.currentTime;
  for (const [index, frequency] of notes.entries()) {
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = frequency;
    const at = start + index * 0.16;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.16, at + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.34);
    oscillator.connect(gain).connect(audioContext.destination);
    oscillator.start(at);
    oscillator.stop(at + 0.36);
  }
}

function updateTitle(visible) {
  const alerts = [...visible.claude, ...visible.codex].filter((agent) =>
    ALERT_STATUSES.has(agent.status),
  );
  if (!alerts.length) {
    document.title = "Agent Signals";
    return;
  }
  const label = alerts.some((agent) => agent.status === "needs_input")
    ? "需要输入"
    : "疑似卡死";
  document.title = `(${alerts.length}) ${label} · Agent Signals`;
}

function handleAlerts(visible) {
  const live = new Set();
  let attention = false;
  let done = false;
  const flash = new Set();

  for (const platform of ["claude", "codex"]) {
    for (const agent of visible[platform]) {
      const key = `${platform}:${agent.id}`;
      live.add(key);
      const before = previousStatus.get(key);
      previousStatus.set(key, agent.status);
      if (before === undefined || before === agent.status) continue;
      if (ALERT_STATUSES.has(agent.status)) {
        attention = true;
        flash.add(key);
      } else if (agent.status === "completed") {
        done = true;
        flash.add(key);
      }
    }
  }

  for (const key of [...previousStatus.keys()]) {
    if (!live.has(key)) previousStatus.delete(key);
  }

  if (attention) playChime("attention");
  else if (done) playChime("done");
  pendingFlash = flash;
}

function applyFlash() {
  if (!pendingFlash.size) return;
  for (const key of pendingFlash) {
    const [platform, ...rest] = key.split(":");
    const id = rest.join(":");
    const card = document.querySelector(
      `.agent-card[data-platform="${CSS.escape(platform)}"][data-agent-id="${CSS.escape(id)}"]`,
    );
    if (!card) continue;
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
    card.addEventListener(
      "animationend",
      () => card.classList.remove("flash"),
      { once: true },
    );
  }
  pendingFlash = new Set();
}

function render(data) {
  latestData = data;

  if (Number(data.schemaVersion) !== SUPPORTED_SCHEMA) {
    showBanner(
      `面板不认识这个数据格式（schemaVersion ${data.schemaVersion}），已停止渲染以免显示错误状态。请更新面板。`,
    );
    grids.claude.innerHTML = "";
    grids.codex.innerHTML = "";
    return;
  }
  hideBanner();

  const sources = data.sources || {};
  const visible = {
    claude: visibleAgents("claude", data.claude || []),
    codex: visibleAgents("codex", data.codex || []),
  };

  // Load values churn during every turn; keep them out of the rebuild
  // signature and patch them in place so orbit animations keep their phase.
  const stripLoad = (agents) => agents.map(({ load, ...rest }) => rest);
  const signature = JSON.stringify({
    visible: { claude: stripLoad(visible.claude), codex: stripLoad(visible.codex) },
    sources,
    notifications: data.notifications,
    audioUnlocked,
  });
  if (signature === snapshotSignature) {
    patchLoads(visible);
    return;
  }
  snapshotSignature = signature;

  document.querySelector("#lastUpdated").textContent =
    `状态更新于 ${new Date(data.generatedAt).toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })}`;

  renderSources(data);
  handleAlerts(visible);
  updateTitle(visible);

  for (const platform of ["claude", "codex"]) {
    const agents = visible[platform];
    grids[platform].innerHTML = agents.length
      ? agents.map(cardMarkup).join("")
      : emptyMarkup(platform, sources[platform]);
    document.querySelector(`#${platform}Count`).textContent = agents.length;
  }

  applyFlash();
}

function updateConnection(isConnected) {
  if (connected === isConnected) return;
  connected = isConnected;
  document.querySelector("#connectionLabel").textContent = isConnected
    ? "实时连接"
    : "连接中断 · 显示最后已知状态";
  document.body.classList.toggle("disconnected", !isConnected);
}

function schedule(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, delay);
}

async function poll() {
  if (document.hidden) return;
  try {
    const query = new URLSearchParams();
    for (const id of lockedCodex) query.append("locked", id);
    query.set("wait", String(POLL_WAIT_S));
    const response = await fetch(`/api/agents?${query}`, {
      cache: "no-store",
      headers: etag ? { "If-None-Match": etag } : {},
    });

    if (response.status === 304) {
      updateConnection(true);
      backoff = 1000;
      schedule(POLL_FLOOR_MS);
      return;
    }
    if (!response.ok) throw new Error("状态接口不可用");

    etag = response.headers.get("ETag") || "";
    render(await response.json());
    updateConnection(true);
    backoff = 1000;
    schedule(POLL_FLOOR_MS);
  } catch {
    updateConnection(false);
    schedule(backoff);
    backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
  }
}

function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
}

function findRawAgent(platform, id) {
  return (latestData[platform] || []).find((agent) => agent.id === id);
}

function acknowledgeCompleted(agent, platform) {
  let changed = false;
  for (const item of [agent, ...(agent.satellites || [])]) {
    if (item.status !== "completed") continue;
    acknowledgedCompletions[completionStorageKey(platform, item)] =
      Number(item.completionId) || Date.now();
    changed = true;
  }
  if (changed) {
    storeObject(STORAGE_KEYS.acknowledged, acknowledgedCompletions);
  }
  return changed;
}

async function openAgent(button) {
  if (button.disabled) return;
  const card = button.closest(".agent-card");
  card.classList.add("opening");
  button.setAttribute("aria-busy", "true");
  showToast("正在请求 Mac 打开对应会话…");
  try {
    const response = await fetch("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: card.dataset.platform,
        id: card.dataset.agentId,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "无法打开会话");
    const agent = findRawAgent(card.dataset.platform, card.dataset.agentId);
    const acknowledged =
      agent && acknowledgeCompleted(agent, card.dataset.platform);
    if (acknowledged) {
      snapshotSignature = "";
      render(latestData);
    }
    showToast(
      acknowledged ? "已进入任务并确认完成状态" : "已在 Mac 前台打开对应会话",
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    card.classList.remove("opening");
    button.removeAttribute("aria-busy");
  }
}

function toggleLock(id) {
  const agent = findRawAgent("codex", id);
  if (!agent || effectiveStatus("codex", agent) !== "idle") return;
  const isLocked = lockedCodex.has(id);
  if (isLocked) {
    lockedCodex.delete(id);
  } else {
    lockedCodex.add(id);
    dismissedCodex.delete(id);
    storeSet(STORAGE_KEYS.dismissedCodex, dismissedCodex);
  }
  storeSet(STORAGE_KEYS.lockedCodex, lockedCodex);
  snapshotSignature = "";
  render(latestData);
  showToast(isLocked ? "已取消锁定" : "已锁定，24 小时后仍会显示");
}

function dismissAgent(id) {
  dismissedCodex.add(id);
  lockedCodex.delete(id);
  storeSet(STORAGE_KEYS.dismissedCodex, dismissedCodex);
  storeSet(STORAGE_KEYS.lockedCodex, lockedCodex);
  snapshotSignature = "";
  render(latestData);
  showToast("已关闭这个空闲任务的显示");
}

// ---- 费用与历史面板（数据来自 /api/history，独立于呼吸灯的 304 契约） ----

function formatMicroUsd(value) {
  if (!Number.isFinite(value)) return "—";
  if (value <= 0) return "$0.00";
  if (value < 10000) return "<$0.01";
  return `$${(value / 1e6).toFixed(2)}`;
}

function formatTokensAny(value) {
  if (!Number.isFinite(value)) return "—";
  return value < 1000 ? String(value) : formatTokensK(value);
}

function formatClock(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatSpan(startMs, endMs) {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
    return "—";
  }
  const minutes = Math.round((endMs - startMs) / MINUTE_MS);
  if (minutes < 1) return "<1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function tokensTitle(tokens) {
  if (!tokens) return "";
  const parts = [];
  const push = (label, value) => {
    if (Number.isFinite(value)) parts.push(`${label} ${formatTokensAny(value)}`);
  };
  push("输入", tokens.input);
  push("输出", tokens.output);
  push("缓存读", tokens.cacheRead);
  push("缓存写5m", tokens.cacheWrite5m);
  push("缓存写1h", tokens.cacheWrite1h);
  push("缓存命中", tokens.cachedInput);
  push("思考", tokens.reasoningOutput);
  return parts.join(" · ");
}

function costMarkup(session) {
  const estimate = session.costLabel === "估算";
  return `<span class="cost-badge${estimate ? " is-estimate" : ""}" title="${escapeHtml(session.costLabel || "")}">${escapeHtml(formatMicroUsd(session.costMicroUsd))}</span>`;
}

function historyRowMarkup(session) {
  const live = session.live === true;
  const statusLabel = live
    ? STATUS_LABELS[session.status] || "运行中"
    : "已结束";
  const model = session.model
    ? `${session.model}${session.effort ? ` · ${session.effort}` : ""}${session.mixedModels ? "（混用）" : ""}`
    : "模型未知";
  const context = Number.isFinite(session.contextPeakPct)
    ? `峰值 ${session.contextPeakPct}%`
    : Number.isFinite(session.contextPeakTokens)
      ? `峰值 ${formatTokensAny(session.contextPeakTokens)}`
      : "—";
  const endMs = live
    ? Date.now()
    : Number(session.endedAtMs || session.lastActiveAtMs);
  const subagents = session.subagents || {};
  const subLine = subagents.count
    ? `<span class="history-sub">子代理 ×${subagents.count}${
        Number.isFinite(subagents.totalTokens)
          ? ` · ${formatTokensAny(subagents.totalTokens)}`
          : ""
      }${
        Number.isFinite(subagents.costMicroUsd)
          ? ` · ${formatMicroUsd(subagents.costMicroUsd)}`
          : ""
      }</span>`
    : "";
  const name = session.name || session.cwdLabel || String(session.id).slice(0, 8);
  return `
    <button
      class="history-row${live ? " is-live" : ""}"
      type="button"
      data-action="history-detail"
      data-session-key="${escapeHtml(session.sessionKey)}"
      aria-expanded="${expandedHistoryKey === session.sessionKey}"
      title="点击展开轮次时间线"
    >
      <span class="history-main">
        <span class="history-dot status-${escapeHtml(session.status || "idle")}"></span>
        <span class="history-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="history-tag">${session.platform === "claude" ? "Claude" : "Codex"}</span>
        <span class="history-tag">${escapeHtml(statusLabel)}</span>
        ${costMarkup(session)}
      </span>
      <span class="history-info">
        <span>${escapeHtml(formatClock(session.startedAtMs))} 启动</span>
        <span>${escapeHtml(formatSpan(session.startedAtMs, endMs))}</span>
        <span>${Number.isFinite(session.turnCount) ? `${session.turnCount} 轮` : "—"}</span>
        <span title="${escapeHtml(tokensTitle(session.tokens))}">${escapeHtml(formatTokensAny(session.tokens?.total))} tok</span>
        <span>${escapeHtml(context)}</span>
        <span class="history-model">${escapeHtml(model)}</span>
      </span>
      ${subLine}
    </button>
    <div class="history-turns" data-turns-for="${escapeHtml(session.sessionKey)}" hidden></div>`;
}

function turnsMarkup(detail) {
  const turns = detail.turns || [];
  if (!turns.length) {
    return `<p class="history-empty">还没有记录到轮次</p>`;
  }
  const rows = turns
    .map((turn) => {
      const tokens = turn.tokens || {};
      const io = [
        Number.isFinite(tokens.input) ? `进 ${formatTokensAny(tokens.input)}` : "",
        Number.isFinite(tokens.output) ? `出 ${formatTokensAny(tokens.output)}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      return `
        <div class="turn-row">
          <span>${escapeHtml(formatClock(turn.startedAtMs))}</span>
          <span>${escapeHtml(formatSpan(turn.startedAtMs, turn.endedAtMs))}</span>
          <span title="${escapeHtml(tokensTitle(tokens))}">${escapeHtml(io || "—")}</span>
          <span>${Number.isFinite(turn.contextTokens) ? `${formatTokensAny(turn.contextTokens)} ctx` : "—"}</span>
          <span>${escapeHtml(formatMicroUsd(turn.costMicroUsd))}</span>
        </div>`;
    })
    .join("");
  return `<div class="turn-table">${rows}</div>`;
}

function renderHistory(data) {
  const liveBox = document.querySelector("#historyLive");
  const listBox = document.querySelector("#historyList");
  if (!data || data.state === "initializing") {
    liveBox.innerHTML = "";
    listBox.innerHTML = `<p class="history-empty">正在建立历史库…${escapeHtml(data?.detail || "")}</p>`;
    return;
  }
  const notes = [];
  if (data.pricing?.claude === "fallback") {
    notes.push("Claude 价格镜像不可用，使用内置兜底价目表");
  }
  if (data.detail) notes.push(data.detail);
  const noteMarkup = notes.length
    ? `<p class="history-empty">${escapeHtml(notes.join(" · "))}</p>`
    : "";

  const sessions = data.sessions || [];
  const liveSessions = sessions.filter((session) => session.live);
  const pastSessions = sessions.filter((session) => !session.live);
  liveBox.innerHTML = liveSessions.length
    ? `<h3>正在运行</h3>${liveSessions.map(historyRowMarkup).join("")}`
    : "";
  listBox.innerHTML =
    noteMarkup +
    (pastSessions.length
      ? `<h3>近 7 天</h3>${pastSessions.map(historyRowMarkup).join("")}`
      : liveSessions.length
        ? ""
        : `<p class="history-empty">近 7 天没有会话记录</p>`);

  if (expandedHistoryKey) {
    const key = expandedHistoryKey;
    expandedHistoryKey = "";
    toggleHistoryDetail(key);
  }
}

async function fetchHistory() {
  const panel = document.querySelector("#historyPanel");
  if (panel.hidden) return;
  try {
    const response = await fetch("/api/history?days=7&limit=50", {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("历史接口不可用");
    renderHistory(await response.json());
  } catch {
    document.querySelector("#historyList").innerHTML =
      `<p class="history-empty">历史数据暂不可用（面板其余功能不受影响）</p>`;
  }
}

async function toggleHistoryDetail(key) {
  const container = document.querySelector(
    `.history-turns[data-turns-for="${CSS.escape(key)}"]`,
  );
  const row = document.querySelector(
    `.history-row[data-session-key="${CSS.escape(key)}"]`,
  );
  if (!container) return;
  if (expandedHistoryKey === key && !container.hidden) {
    container.hidden = true;
    expandedHistoryKey = "";
    row?.setAttribute("aria-expanded", "false");
    return;
  }
  document.querySelectorAll(".history-turns").forEach((element) => {
    element.hidden = true;
  });
  document.querySelectorAll(".history-row").forEach((element) => {
    element.setAttribute("aria-expanded", "false");
  });
  expandedHistoryKey = key;
  container.hidden = false;
  row?.setAttribute("aria-expanded", "true");
  container.innerHTML = `<p class="history-empty">载入轮次时间线…</p>`;
  try {
    const response = await fetch(
      `/api/history/session?key=${encodeURIComponent(key)}&turns=100`,
      { cache: "no-store" },
    );
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.error || "轮次数据暂不可用");
    container.innerHTML = turnsMarkup(detail);
  } catch (error) {
    container.innerHTML = `<p class="history-empty">${escapeHtml(error.message || "轮次数据暂不可用")}</p>`;
  }
}

function toggleHistory(open, persist = false) {
  const panel = document.querySelector("#historyPanel");
  const toggle = document.querySelector("#historyToggle");
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.classList.toggle("is-active", open);
  clearInterval(historyTimer);
  historyTimer = null;
  if (open) {
    fetchHistory();
    historyTimer = setInterval(fetchHistory, HISTORY_REFRESH_MS);
  }
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEYS.historyOpen, open ? "1" : "0");
    } catch {
      // 折叠状态记不住也不影响使用。
    }
  }
}

document.addEventListener("click", (event) => {
  unlockAudio();
  const control = event.target.closest("[data-action]");
  if (control?.dataset.action === "history-detail") {
    toggleHistoryDetail(control.dataset.sessionKey);
    return;
  }
  if (control?.dataset.action === "lock") {
    toggleLock(control.dataset.agentId);
    return;
  }
  if (control?.dataset.action === "dismiss") {
    dismissAgent(control.dataset.agentId);
    return;
  }
  const button = event.target.closest(".agent-open");
  if (button) openAgent(button);
});

document.querySelector("#themeToggle").addEventListener("click", () => {
  const nextTheme =
    document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(nextTheme, true);
});

document.querySelector("#historyToggle").addEventListener("click", () => {
  toggleHistory(document.querySelector("#historyPanel").hidden, true);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
    clearInterval(historyTimer);
    historyTimer = null;
    return;
  }
  backoff = 1000;
  schedule(0);
  if (!document.querySelector("#historyPanel").hidden) {
    fetchHistory();
    historyTimer = setInterval(fetchHistory, HISTORY_REFRESH_MS);
  }
});

setInterval(() => {
  const clock = document.querySelector("#clock");
  clock.textContent = new Date().toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
  document.querySelectorAll("[data-timestamp]").forEach((element) => {
    element.textContent = relativeTime(Number(element.dataset.timestamp));
  });
}, 1000);

applyTheme(document.documentElement.dataset.theme);
try {
  if (localStorage.getItem(STORAGE_KEYS.historyOpen) === "1") {
    toggleHistory(true);
  }
} catch {
  // 无本地存储时保持默认折叠。
}
poll();
