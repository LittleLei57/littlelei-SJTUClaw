/*
 * SJTUClaw Web 客户端：维护页面状态，通过 REST/SSE 同步 Session 与 Turn，
 * 并渲染消息、Tool、审批、附件、引用和各功能面板。
 */
const REQUIRED_API_PROTOCOL_VERSION = 2;
const WEB_VERSION = "1.0.0";
const state = { currentSessionId: null, isDraft: true, draftTitle: "新会话", draftWorkspace: null, pendingDraftFiles: [], pendingDraftSelected: new Set(), sessions: [], busy: false, turnId: null, activeTurn: null, reconnectingTurnId: null, reconnectAbortController: null, turnAbortController: null, cancelRequestedTurnId: null, cancelNotice: null, detachedTurnIds: new Set(), pendingAttachments: [], pendingAttachmentSession: null, pendingQuote: null, selectedSkill: null, skillCatalog: [], compatible: false, gatewayEventsConnected: false, sessionSnapshots: {}, sessionRefreshInFlight: false, citationMap: {}, citationAliases: {}, messageCitationMaps: {}, liveCitationMap: null, citationNext: 1, attachmentMap: {}, turnEvents: new Map(), downloadExpiryTimer: null };
const $ = (selector) => document.querySelector(selector);
const sessionList = $("#session-list");
const messages = $("#messages");
const input = $("#message-input");
const sendButton = $("#send-button");
const modelSelect = $("#model-select");
const slashCommandMenu = $("#slash-command-menu");
const slashCommandList = $("#slash-command-list");
const errorBanner = $("#error-banner");
const taskPanel = $("#task-panel");
const skillPanel = $("#skill-panel");
const timelinePanel = $("#timeline-panel");
const stressPanel = $("#stress-panel");
const memoryPanel = $("#memory-panel");
const messageIndex = $("#message-index");
const messageIndexToggle = $("#message-index-toggle");
const messageIndexTitle = $("#message-index-title");
const messageIndexSearch = $("#message-index-search");
const messageIndexList = $("#message-index-list");
const avatarInput = document.createElement("input");
avatarInput.type = "file";
avatarInput.accept = "image/png,image/jpeg,image/webp,image/gif,image/svg+xml";
avatarInput.hidden = true;
document.body.append(avatarInput);
let taskPollTimer = null;
let taskLoading = false;
let taskSnapshot = "";
let errorHideTimer = null;
let avatarTargetRole = null;
let sessionRefreshTimer = null;
let gatewayEventSource = null;
let deferredGatewayRefreshTimer = null;
const deferredGatewayRefreshes = new Set();
const SESSION_REFRESH_MS = 3000;
const MESSAGE_ACTION_FEEDBACK_MS = 1200;
let visibleSlashCommands = [];
let activeSlashCommandIndex = 0;

const SLASH_COMMANDS = [
  {
    command: "/new", icon: "＋", label: "新建会话",
    description: "打开一个尚未落盘的新对话",
    keywords: "session 会话",
    run: () => $("#new-session").click(),
  },
  {
    command: "/compact", icon: "↻", label: "整理上下文",
    description: "压缩较早消息并展示 Summary 预览",
    keywords: "compress summary 压缩",
    run: () => runManualCompaction(),
  },
  {
    command: "/attachment", icon: "⌕", label: "添加附件",
    description: "上传文件、图片或选择已有附件",
    keywords: "file upload 文件 图片",
    run: () => $("#composer-add").click(),
  },
  {
    command: "/workspace", icon: "◇", label: "Workspace",
    description: "查看或修改当前会话的工作目录",
    keywords: "目录 工作区 folder",
    run: () => openCurrentWorkspacePicker(),
  },
  {
    command: "/skill", icon: "✦", label: "Skills",
    description: "浏览并调用专业工作流",
    keywords: "技能 workflow",
    run: () => toggleSkillPanel(true),
  },
  {
    command: "/memory", icon: "◈", label: "长期记忆",
    description: "查看和确认候选记忆",
    keywords: "记忆 candidate",
    run: () => toggleMemoryPanel(true),
  },
  {
    command: "/tasks", icon: "◷", label: "定时任务",
    description: "创建和管理 Scheduler 任务",
    keywords: "scheduler cron 提醒",
    run: () => toggleTaskPanel(true),
  },
  {
    command: "/timeline", icon: "≋", label: "运行记录",
    description: "查看模型、Tool 与审批事件",
    keywords: "timeline trace 调用",
    run: () => toggleTimeline(true),
  },
  {
    command: "/diagnose", icon: "▦", label: "Session 健康",
    description: "只读检查上下文、附件和工具状态",
    keywords: "diagnostic stress 健康",
    run: () => toggleStressPanel(true),
  },
  {
    command: "/model", icon: "◉", label: "切换模型",
    description: "选择当前 API 已配置的模型",
    keywords: "llm 模型",
    run: () => openModelPicker(),
  },
  {
    command: "/export", icon: "⇩", label: "导出会话",
    description: "确认后导出当前 Session",
    keywords: "download 下载",
    run: () => $("#export-session").click(),
  },
];

const AVATAR_STORAGE_KEYS = {
  user: "sjtuclaw.avatar.user",
  assistant: "sjtuclaw.avatar.assistant",
};
const USER_NAME_STORAGE_KEY = "sjtuclaw.displayName.user";
const DEFAULT_USER_NAME = "你";
const AVATAR_LABELS = {
  user: "你",
  assistant: "S",
};
const AVATAR_NAMES = {
  user: "你的头像",
  assistant: "SJTUClaw 头像",
};
const MAX_AVATAR_BYTES = 2 * 1024 * 1024;

const DAILY_QUOTES = [
  "每一次认真提问，都是抵达答案的开始。",
  "不必等待完美，先让此刻的灵感落地。",
  "答案并不总在远方，有时就藏在下一步里。",
  "把行动交给自己，将结果留给时间。",
  "汇集散落的灵感，拼凑出前行的轮廓。",
  "少一点猜测的内耗，多一次真实的运行。",
  "所有的豁然开朗，都来自一次次的重构与试错。"
]

function renderDailyQuote() {
  fetch("/api/daily-quote")
    .then((response) => response.ok ? response.json() : null)
    .then((payload) => {
      if (payload?.quote) $("#daily-quote p").textContent = payload.quote;
    })
    .catch(() => {
      // Welcome text is decorative: an unavailable gateway must not block the UI.
    });
}

renderDailyQuote();

// Keep the sidebar identity in sync with the user's message avatar.  The
// same picker is intentionally reused, so changing either one updates both.
const brandAvatar = $("#brand-avatar");
if (brandAvatar) setupAvatar(brandAvatar, "user");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const fallback = response.status === 405
      ? "当前 Gateway 尚未加载这项功能，请重启 python gateway.py 后重试。"
      : `请求失败 (${response.status})`;
    const error = new Error(body.detail && body.detail !== "Method Not Allowed" ? body.detail : body.error || fallback);
    error.status = response.status;
    throw error;
  }
  return body;
}

function showError(error) {
  errorBanner.textContent = error.message || String(error);
  errorBanner.title = "点击关闭";
  errorBanner.hidden = false;
  if (errorHideTimer) window.clearTimeout(errorHideTimer);
  errorHideTimer = window.setTimeout(clearError, 10000);
}
function clearError() {
  if (errorHideTimer) window.clearTimeout(errorHideTimer);
  errorHideTimer = null;
  errorBanner.hidden = true;
  errorBanner.textContent = "";
}
errorBanner.addEventListener("click", clearError);

function closeTopbarMoreMenu() {
  const menu = $("#topbar-more-menu");
  const toggle = $("#topbar-more-toggle");
  if (!menu || !toggle) return;
  menu.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
}

function closeOpenSidePanel() {
  const panels = [
    [memoryPanel, toggleMemoryPanel],
    [stressPanel, toggleStressPanel],
    [timelinePanel, toggleTimeline],
    [skillPanel, toggleSkillPanel],
    [taskPanel, toggleTaskPanel],
  ];
  const current = panels.find(([panel]) => panel?.classList.contains("open"));
  if (!current) return false;
  current[1](false);
  return true;
}

$("#topbar-more-toggle").onclick = (event) => {
  event.stopPropagation();
  const menu = $("#topbar-more-menu");
  const opening = menu.hidden;
  closeTopbarMoreMenu();
  menu.hidden = !opening;
  $("#topbar-more-toggle").setAttribute("aria-expanded", String(opening));
};
$("#topbar-more-menu").addEventListener("click", (event) => {
  const item = event.target.closest("[data-trigger]");
  if (!item) return;
  const target = document.getElementById(item.dataset.trigger);
  closeTopbarMoreMenu();
  target?.click();
});
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".topbar-more")) closeTopbarMoreMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if ($("#attachment-preview-dialog")?.open) return;
  if (!$("#composer-add-menu")?.hidden) return;
  if (!$("#topbar-more-menu")?.hidden) {
    closeTopbarMoreMenu();
  } else if (!errorBanner.hidden) {
    clearError();
  } else if (!closeOpenSidePanel()) {
    return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
});

async function checkGatewayCompatibility() {
  const status = $("#gateway-status");
  try {
    const response = await fetch(`/api/health?_=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Gateway health check failed (${response.status})`);
    const health = await response.json();
    if (health.apiProtocolVersion == null) {
      // Gateways started before protocol negotiation existed cannot declare a
      // version. Keep the UI usable and let individual unsupported endpoints
      // produce the existing, actionable restart message.
      state.compatible = true;
      status.textContent = "Gateway 旧版 · 已连接";
      return true;
    }
    if (health.apiProtocolVersion !== REQUIRED_API_PROTOCOL_VERSION) {
      const actual = health.apiProtocolVersion;
      showVersionBlocker(
        `当前网页 ${WEB_VERSION} 需要 API 协议 ${REQUIRED_API_PROTOCOL_VERSION}，` +
        `但 Gateway ${health.gatewayVersion || "未知版本"} 提供的是 ${actual}。请先重启 Gateway，再强制刷新页面。`
      );
      return false;
    }
    state.compatible = true;
    status.textContent = `Gateway ${health.gatewayVersion} · 已连接`;
    return true;
  } catch (error) {
    status.textContent = "Gateway 未连接";
    showVersionBlocker(`无法连接 Gateway：${error.message}。请确认 python gateway.py 正在运行。`);
    return false;
  }
}

async function loadModelSelection() {
  if (!modelSelect) return;
  const payload = await api("/api/model");
  const models = payload.models || [];
  modelSelect.replaceChildren();
  for (const item of models) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.shortLabel || item.label;
    option.title = [
      item.label,
      item.mode,
      item.strength,
      item.contextLength ? `上下文 ${item.contextLength}` : "",
    ].filter(Boolean).join(" · ");
    modelSelect.append(option);
  }
  modelSelect.value = payload.currentModel || "";
  modelSelect.disabled = false;
  const switcher = modelSelect.closest(".model-switcher");
  if (switcher) switcher.hidden = models.length <= 1;
  updateModelSwitcherDescription(payload);
}

function updateModelSwitcherDescription(payload) {
  if (!modelSelect) return;
  const current = (payload.models || []).find((item) => item.id === payload.currentModel);
  const description = current
    ? [
        current.label,
        current.mode,
        current.strength,
        current.contextLength ? `上下文 ${current.contextLength}` : "",
      ].filter(Boolean).join(" · ")
    : "切换当前 API 已配置的模型";
  modelSelect.title = description;
  const switcher = modelSelect.closest(".model-switcher");
  if (switcher) switcher.title = description;
}

if (modelSelect) {
  modelSelect.addEventListener("change", async () => {
    const requested = modelSelect.value;
    modelSelect.disabled = true;
    try {
      const payload = await api("/api/model", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: requested }),
      });
      modelSelect.value = payload.currentModel;
      updateModelSwitcherDescription(payload);
    } catch (error) {
      showError(error);
      await loadModelSelection().catch(() => {});
    } finally {
      modelSelect.disabled = false;
    }
  });
}

function showVersionBlocker(message) {
  state.compatible = false;
  $("#version-message").textContent = message;
  $("#version-blocker").hidden = false;
}

$("#version-reload").onclick = () => window.location.reload();

async function loadSessions(preferredId = null, options = {}) {
  const data = await api("/api/sessions");
  state.sessions = data.sessions;
  rememberSessionSnapshots(state.sessions);
  const rememberedId = preferredId || sessionStorage.getItem("sjtuclaw.lastSessionId");
  const recoverable = data.sessions.find((item) => item.activeTurn);
  // A brand-new browser tab starts as a local draft.  On a normal refresh,
  // sessionStorage points back to the last conversation so it can be
  // recovered without creating a duplicate Session.
  if (options.draft && !recoverable && !rememberedId) {
    const previous = data.sessions.find((item) => item.sessionId === data.currentSessionId);
    state.draftWorkspace = previous?.workspace || null;
    showDraftSession();
    renderSessionList();
    return;
  }
  state.currentSessionId = rememberedId || state.currentSessionId || recoverable?.sessionId || data.currentSessionId || data.sessions[0]?.sessionId;
  if (!state.sessions.some((item) => item.sessionId === state.currentSessionId)) {
    state.currentSessionId = state.sessions[0]?.sessionId || null;
  }
  renderSessionList();
  if (state.currentSessionId) await loadSession(state.currentSessionId);
}

function showDraftSession() {
  state.currentSessionId = null;
  state.isDraft = true;
  state.activeTurn = null;
  state.turnId = null;
  state.attachmentMap = {};
  state.pendingAttachments = [];
  state.pendingAttachmentSession = null;
  clearPendingQuote();
  state.citationMap = newCitationMap();
  state.citationAliases = {};
  state.messageCitationMaps = {};
  messages.replaceChildren();
  messages.append($("#empty-template").content.cloneNode(true));
  messages.querySelectorAll("[data-prompt]").forEach((button) => {
    button.onclick = () => { input.value = button.dataset.prompt; input.focus(); resizeInput(); };
  });
  $("#session-title").textContent = state.draftTitle || "新会话";
  $("#session-meta").replaceChildren();
  const draft = document.createElement("span");
  draft.className = "draft-session-chip";
  draft.textContent = "尚未创建，发送后保存";
  $("#session-meta").append(draft);
  const workspace = document.createElement("span");
  workspace.className = "workspace-chip draft-workspace-chip";
  workspace.role = "button";
  workspace.tabIndex = 0;
  workspace.textContent = state.draftWorkspace
    ? `Workspace · ${state.draftWorkspace}`
    : "Workspace · 未设置";
  workspace.title = state.draftWorkspace || "点击为新对话选择 Workspace";
  workspace.onclick = () => pickDraftWorkspace(workspace);
  workspace.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pickDraftWorkspace(workspace);
    }
  };
  $("#session-meta").append(workspace);
  renderAttachments([]);
  $("#download-strip").hidden = true;
  $("#approval-area").replaceChildren();
  $("#approval-area").hidden = true;
  renderMessageIndex();
  messageIndex.hidden = true;
  renderSessionList();
}

function sessionSnapshot(session) {
  return [
    session.updatedAt || "",
    session.messageCount ?? "",
    session.attachmentCount ?? "",
    session.title || "",
    session.activeTurn?.turnId || "",
    session.activeTurn?.phase || "",
    session.activeTurn?.updatedAt || "",
  ].join("|");
}

function rememberSessionSnapshots(sessions) {
  state.sessionSnapshots = Object.fromEntries(
    (sessions || []).map((session) => [session.sessionId, sessionSnapshot(session)])
  );
}

async function pollSessionUpdates() {
  if (!state.compatible || state.busy || state.reconnectingTurnId || state.sessionRefreshInFlight || document.hidden) return;
  state.sessionRefreshInFlight = true;
  try {
    const data = await api("/api/sessions");
    await applySessionSnapshot(data);
  } catch (error) {
    console.debug("session refresh skipped", error);
  } finally {
    state.sessionRefreshInFlight = false;
  }
}

async function applySessionSnapshot(data) {
  const previous = { ...state.sessionSnapshots };
  const sessions = data.sessions || [];
  const currentId = state.currentSessionId;
  const currentMeta = sessions.find((item) => item.sessionId === currentId);
  const currentChanged = Boolean(
    currentMeta && previous[currentId] && previous[currentId] !== sessionSnapshot(currentMeta)
  );
  const listChanged = sessions.length !== state.sessions.length
    || sessions.some((item, index) => item.sessionId !== state.sessions[index]?.sessionId
      || previous[item.sessionId] !== sessionSnapshot(item));

  if (!listChanged && !currentChanged) return;
  state.sessions = sessions;
  if (state.currentSessionId && !state.sessions.some((item) => item.sessionId === state.currentSessionId)) {
    state.currentSessionId = data.currentSessionId || state.sessions[0]?.sessionId || null;
  }
  renderSessionList();
  if (currentChanged && state.currentSessionId && !state.busy && !state.reconnectingTurnId) {
    await loadSession(state.currentSessionId);
  }
  rememberSessionSnapshots(state.sessions);
}

function startSessionFallbackLoop() {
  if (sessionRefreshTimer) return;
  sessionRefreshTimer = window.setInterval(() => pollSessionUpdates(), SESSION_REFRESH_MS);
}

function stopSessionFallbackLoop() {
  if (sessionRefreshTimer) window.clearInterval(sessionRefreshTimer);
  sessionRefreshTimer = null;
}

function startTaskFallbackLoop() {
  if (taskPollTimer || !taskPanel.classList.contains("open")) return;
  taskPollTimer = window.setInterval(() => {
    if (!state.busy) loadTasks().catch(showError);
  }, 3000);
}

function stopTaskFallbackLoop() {
  if (taskPollTimer) window.clearInterval(taskPollTimer);
  taskPollTimer = null;
}

function deferGatewayRefresh(kind) {
  deferredGatewayRefreshes.add(kind);
  if (deferredGatewayRefreshTimer) return;
  const flush = () => {
    if (state.busy || state.reconnectingTurnId) {
      deferredGatewayRefreshTimer = window.setTimeout(flush, 250);
      return;
    }
    deferredGatewayRefreshTimer = null;
    const pending = new Set(deferredGatewayRefreshes);
    deferredGatewayRefreshes.clear();
    if (pending.has("sessions")) pollSessionUpdates();
    if (pending.has("tasks") && taskPanel.classList.contains("open")) loadTasks().catch(showError);
    if (pending.has("approvals") && state.currentSessionId) loadApprovals().catch(showError);
  };
  deferredGatewayRefreshTimer = window.setTimeout(flush, 250);
}

function startGatewayEventStream() {
  startSessionFallbackLoop();
  if (!window.EventSource || gatewayEventSource) return;
  const source = new EventSource("/api/events/stream");
  gatewayEventSource = source;
  source.onopen = () => {
    state.gatewayEventsConnected = true;
    stopSessionFallbackLoop();
    stopTaskFallbackLoop();
  };
  source.addEventListener("sessions", async (event) => {
    if (state.busy || state.reconnectingTurnId) {
      deferGatewayRefresh("sessions");
      return;
    }
    if (state.sessionRefreshInFlight) return;
    state.sessionRefreshInFlight = true;
    try {
      await applySessionSnapshot(JSON.parse(event.data));
    } catch (error) {
      console.debug("session event skipped", error);
    } finally {
      state.sessionRefreshInFlight = false;
    }
  });
  source.addEventListener("tasks", () => {
    if (state.busy) deferGatewayRefresh("tasks");
    else if (taskPanel.classList.contains("open")) loadTasks().catch(showError);
  });
  source.addEventListener("approvals", () => {
    if (state.busy) deferGatewayRefresh("approvals");
    else if (state.currentSessionId) loadApprovals().catch(showError);
  });
  source.onerror = () => {
    state.gatewayEventsConnected = false;
    startSessionFallbackLoop();
    startTaskFallbackLoop();
  };
}

function startSessionRefreshLoop() {
  startGatewayEventStream();
}

function renderSessionList() {
  sessionList.replaceChildren();
  for (const session of state.sessions) {
    const row = document.createElement("div");
    row.className = `session-row${session.sessionId === state.currentSessionId ? " active" : ""}`;
    const button = document.createElement("button");
    button.className = "session-item";
    button.textContent = session.title;
    const countLabel = session.messageCount ?? "";
    button.title = `${session.title} · ${countLabel} messages`;
    button.onclick = async () => {
      if (state.busy) return;
      if (state.isDraft && state.pendingDraftFiles.length) {
        const discard = window.confirm("当前新对话中有尚未发送的附件，切换会话后将丢弃它们。继续吗？");
        if (!discard) return;
        state.pendingDraftFiles = [];
        state.pendingDraftSelected.clear();
      }
      if (state.currentSessionId !== session.sessionId) clearPendingQuote();
      state.currentSessionId = session.sessionId;
      renderSessionList();
      await loadSession(session.sessionId).catch(showError);
    };
    const rename = document.createElement("button");
    rename.className = "session-rename";
    rename.type = "button";
    rename.textContent = "✎";
    rename.title = `重命名会话：${session.title}`;
    rename.setAttribute("aria-label", `重命名会话 ${session.title}`);
    rename.onclick = async () => {
      if (state.busy) return;
      const title = window.prompt("输入新的会话名称", session.title);
      if (title === null || title.trim() === "" || title.trim() === session.title) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(session.sessionId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.trim() }),
        });
        await loadSessions(session.sessionId);
      } catch (error) { showError(error); }
    };
    const remove = document.createElement("button");
    remove.className = "session-delete";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `删除会话：${session.title}`;
    remove.setAttribute("aria-label", `删除会话 ${session.title}`);
    remove.onclick = async () => {
      if (state.busy) return;
      if (!window.confirm(`确定删除会话“${session.title}”吗？\n此操作会删除该会话的聊天记录，且无法撤销。`)) return;
      try {
        const result = await api(`/api/sessions/${encodeURIComponent(session.sessionId)}`, { method: "DELETE" });
        state.currentSessionId = result.currentSessionId;
        await loadSessions(result.currentSessionId);
      } catch (error) { showError(error); }
    };
    row.append(button, rename, remove);
    sessionList.append(row);
  }
}

function addMessage(role, content, metadata = {}) {
  // System protocol records are durable audit breadcrumbs, not conversation
  // turns.  Keep them out of the transcript even when an older Gateway did
  // not attach the internal metadata consistently.
  const isAssistantSegment = (
    role === "assistant"
    && (metadata?.kind === "assistant_segment" || metadata?.displayOnly)
  );
  // Process prose is internal to model context, but belongs in the visible
  // text -> Tool -> text transcript, including after a session refresh.
  if (role === "system" || (metadata?.internal === true && !isAssistantSegment)) return null;
  const rawContent = String(content || "");
  const selectedAttachments = role === "user"
    ? selectedAttachmentsForMessage(rawContent, metadata)
    : [];
  const protocol = parseProtocolMessage(content);
  if (protocol?.type === "final") {
    return addMessage(role, protocol.content || "", metadata);
  }
  if (protocol?.type === "tool_call") {
    insertToolCard(createToolCard({
      tool: protocol.tool, args: protocol.args, phase: "call", callId: protocol.id,
    }));
    return null;
  }
  if (protocol?.type === "tool_calls") {
    const hasCurrentTime = protocol.calls.some((call) => call.tool === "current_time");
    for (const call of protocol.calls) {
      if (hasCurrentTime && call.tool === "web_search") continue;
      insertToolCard(createToolCard({
        tool: call.tool, args: call.args, phase: "call", callId: call.id,
      }));
    }
    return null;
  }
  if (protocol?.type === "tool_results") {
    for (const result of protocol.results) {
      // Approval-gated Tools are written to the Session once before the
      // decision so the model knows execution is suspended.  That record is
      // only a pending placeholder (`success: false`), not an execution
      // failure.  The resolved approval later contributes the real Tool
      // result, so rendering both would misleadingly show
      // "调用失败" followed by "调用成功" for every approved action.
      if (result?.deferred || result?.approvalRequired === true) continue;
      insertToolCard(createToolCard({
        tool: result.tool, result, phase: "result", callId: result.callId,
      }));
    }
    return null;
  }
  if (protocol?.type === "protocol_error") {
    // Internal automatic retry; keep it out of the user-facing transcript.
    return null;
  }
  if (protocol?.type === "approval_retry_required") {
    // The approval history card is the user-facing representation.  The
    // durable retry marker itself must never appear as a raw JSON bubble.
    return null;
  }
  if (role === "assistant" && content.startsWith("[deferred_action_promise]")) return null;
  if (protocol?.type === "scheduler_failure") {
    messages.append(createSystemNotice(protocol));
    return null;
  }
  if (protocol?.type === "approval_required") {
    for (const approval of protocol.approvals) {
      // Compatibility with sessions written before mixed Tool batches were
      // split correctly: a legacy [approval_required] payload could also
      // contain successful read-only Tool results.  Render those as ordinary
      // Tool history rather than a misleading pending-approval card.
      if (
        approval
        && approval.approvalRequired !== true
        && typeof approval.success === "boolean"
        && !approval.approvalId
      ) {
        insertToolCard(createToolCard({
          tool: approval.tool,
          result: approval,
          phase: "result",
          callId: approval.callId,
        }));
      } else {
        messages.append(createApprovalHistoryCard(approval));
      }
    }
    return null;
  }
  if (protocol?.type === "approval_result") {
    const existing = [...messages.querySelectorAll(".approval-history")]
      .find((item) => item.dataset.approvalId === protocol.approval.approvalId);
    if (existing) updateApprovalHistoryCard(existing, protocol.approval);
    else messages.append(createApprovalHistoryCard(protocol.approval));
    return null;
  }
  if (role === "user" && content.includes("\n\n[quoted_message] ")) {
    content = content.split("\n\n[quoted_message] ", 1)[0];
  }
  if (role === "user" && content.includes("\n\n[attached_files] ")) {
    content = content.split("\n\n[attached_files] ", 1)[0];
  }
  const article = document.createElement("article");
  article.className = `message ${role}`;
  if (metadata?.progress) article.classList.add("progress");
  if (metadata?.kind === "assistant_segment" || metadata?.displayOnly) {
    article.classList.add("is-intermediate");
  }
  article.dataset.role = role;
  const messageIndex = Number.isInteger(metadata?.__messageIndex) ? metadata.__messageIndex : null;
  if (messageIndex !== null) article.dataset.sessionMessageIndex = String(messageIndex);
  const compactionAnchorIds = Array.isArray(metadata?.compactionAnchorIds)
    ? metadata.compactionAnchorIds.filter(Boolean).map(String)
    : [];
  if (compactionAnchorIds.length) {
    article.dataset.compactionAnchors = compactionAnchorIds.join(" ");
  }
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  setupAvatar(avatar, role === "user" ? "user" : "assistant");
  const body = document.createElement("div");
  body.className = "message-body";
  const heading = document.createElement("div");
  heading.className = "message-heading";
  const label = document.createElement("strong");
  label.textContent = role === "user" ? getUserDisplayName() : "SJTUClaw";
  if (role === "user") {
    label.className = "message-name editable";
    label.title = "点击设置用户名";
    label.tabIndex = 0;
    label.role = "button";
    label.onclick = editUserDisplayName;
    label.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        editUserDisplayName();
      }
    };
  } else {
    label.className = "message-name";
  }
  heading.append(label);
  if (role === "user") {
    const source = document.createElement("span");
    source.className = "message-source";
    source.textContent = sourceLabel(metadata?.source || "web");
    source.title = `消息来源：${source.textContent}`;
    heading.append(source);
  }
  const text = document.createElement("div");
  text.className = "message-content";
  text.dataset.raw = content;
  const citationMap = metadata?.__citationMap
    || (metadata?.__messageIndex === undefined && role === "assistant" ? state.liveCitationMap : null);
  text.__citationMap = citationMap;
  renderMessageContent(text, content, citationMap, { preserveLineBreaks: role === "user" });
  body.append(heading);
  const attachmentTray = createMessageAttachmentTray(selectedAttachments);
  if (attachmentTray) body.append(attachmentTray);
  body.append(text);
  if (!metadata?.progress) {
    body.append(createMessageActions(role, text, article));
  }
  article.append(avatar, body);
  messages.append(article);
  refreshTurnActionAvailability();
  return text;
}

function selectedAttachmentsForMessage(content, metadata = {}) {
  const explicit = Array.isArray(metadata?.__selectedAttachments)
    ? metadata.__selectedAttachments
    : null;
  let items = explicit;
  if (!items) {
    const match = String(content || "").match(/\[attached_files\]\s*(\[[^\r\n]*\])/);
    if (!match) return [];
    try {
      items = JSON.parse(match[1]);
    } catch {
      return [];
    }
  }
  const seen = new Set();
  return (items || []).flatMap((candidate) => {
    const attachmentId = String(candidate?.attachmentId || "").trim();
    if (!attachmentId || seen.has(attachmentId)) return [];
    seen.add(attachmentId);
    const storedExists = Boolean(state.attachmentMap?.[attachmentId]);
    const stored = state.attachmentMap?.[attachmentId] || {};
    return [{
      ...candidate,
      ...stored,
      attachmentId,
      filename: stored.filename || candidate.filename || "附件",
      contentType: stored.contentType || candidate.contentType || "application/octet-stream",
      available: storedExists ? stored.available !== false : candidate.available === true,
    }];
  });
}

function messageAttachmentKind(item) {
  const contentType = String(item?.contentType || "").toLowerCase();
  const filename = String(item?.filename || "").toLowerCase();
  if (contentType.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp)$/i.test(filename)) return "image";
  if (contentType === "application/pdf" || filename.endsWith(".pdf")) return "pdf";
  if (/\.(docx?|odt)$/i.test(filename)) return "word";
  if (/\.(pptx?|odp)$/i.test(filename)) return "slides";
  if (/\.(xlsx?|csv|tsv)$/i.test(filename)) return "sheet";
  return "file";
}

function messageAttachmentLabel(kind) {
  return {
    pdf: "PDF",
    word: "文档",
    slides: "演示文稿",
    sheet: "表格",
    file: "文件",
  }[kind] || "附件";
}

function createMessageAttachmentTray(items) {
  if (!items.length) return null;
  const tray = document.createElement("div");
  tray.className = "message-attachment-tray";
  tray.setAttribute("aria-label", `本轮选中的附件，共 ${items.length} 个`);
  for (const item of items) {
    const kind = messageAttachmentKind(item);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `message-attachment-card ${kind}`;
    card.dataset.attachmentId = item.attachmentId;
    card.title = item.available === false
      ? `${item.filename}（文件已不可用）`
      : `预览附件：${item.filename}`;
    card.disabled = item.available === false;
    if (kind === "image") {
      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = item.filename;
      image.src = `/api/sessions/${encodeURIComponent(state.currentSessionId)}/attachments/${encodeURIComponent(item.attachmentId)}/raw`;
      const caption = document.createElement("span");
      caption.textContent = item.filename;
      card.append(image, caption);
    } else {
      const type = document.createElement("span");
      type.className = "message-attachment-type";
      type.textContent = messageAttachmentLabel(kind);
      const name = document.createElement("strong");
      name.textContent = item.filename;
      const meta = document.createElement("small");
      meta.textContent = item.size ? formatBytes(item.size) : "点击预览";
      card.append(type, name, meta);
    }
    card.onclick = () => openAttachmentPreview(item);
    tray.append(card);
  }
  return tray;
}

function createMessageActions(role, contentElement, article = null) {
  const actions = document.createElement("div");
  actions.className = "message-actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "复制";
  copy.title = "复制这条消息";
  copy.onclick = () => copyMessageContent(contentElement, copy);
  const quote = document.createElement("button");
  quote.type = "button";
  quote.textContent = "引用";
  quote.title = "引用到输入框";
  quote.onclick = () => quoteMessageContent(role, contentElement);
  actions.append(copy, quote);
  const messageIndex = article?.dataset.sessionMessageIndex;
  if (role === "user" && messageIndex !== undefined) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "message-action-icon message-turn-action";
    edit.innerHTML = iconSvg("edit");
    edit.title = "修改这条问题并重新发送";
    edit.setAttribute("aria-label", "修改这条问题并重新发送");
    edit.onclick = () => editAndReplayMessage(Number(messageIndex), contentElement);
    actions.append(edit);
  } else if (role === "assistant") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "message-action-icon message-turn-action";
    retry.innerHTML = iconSvg("retry");
    retry.title = "从上一条问题重新生成回答";
    retry.setAttribute("aria-label", "从上一条问题重新生成回答");
    retry.onclick = () => regenerateFromAssistant(article);
    actions.append(retry);
  }
  return actions;
}

function iconSvg(name) {
  if (name === "edit") {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4.6L19.2 9.4a2.1 2.1 0 0 0 0-3l-1.6-1.6a2.1 2.1 0 0 0-3 0L4 15.4V20Z"/><path d="m13.4 6 4.6 4.6"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.6 8.2a7 7 0 0 0-11.5-.6A7 7 0 0 0 6 15.5a7 7 0 0 0 10.1 1.7"/><path d="M18.8 4.4v4.2h-4.2"/></svg>';
}

function refreshTurnActionAvailability() {
  const userMessages = [...messages.querySelectorAll(".message.user[data-session-message-index]")];
  const assistantMessages = [...messages.querySelectorAll(".message.assistant:not(.progress)")];
  const latestUser = userMessages.at(-1) || null;
  const latestAssistant = assistantMessages.at(-1) || null;
  const latestAssistantAnswersLatestUser = Boolean(
    latestAssistant
    && latestUser
    && (latestUser.compareDocumentPosition(latestAssistant) & Node.DOCUMENT_POSITION_FOLLOWING)
  );
  messages.querySelectorAll(".message.user .message-turn-action").forEach((button) => {
    button.hidden = button.closest(".message") !== latestUser;
  });
  messages.querySelectorAll(".message.assistant .message-turn-action").forEach((button) => {
    button.hidden = button.closest(".message") !== latestAssistant || !latestAssistantAnswersLatestUser;
  });
}

async function copyMessageContent(contentElement, trigger) {
  const raw = contentElement?.dataset.raw || contentElement?.innerText || "";
  if (!raw.trim()) return;
  try {
    await navigator.clipboard.writeText(raw);
    flashMessageAction(trigger, "已复制");
  } catch {
    fallbackCopyText(raw);
    flashMessageAction(trigger, "已复制");
  }
}

function fallbackCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function flashMessageAction(trigger, label) {
  if (!trigger) return;
  const original = trigger.textContent;
  trigger.textContent = label;
  trigger.disabled = true;
  window.setTimeout(() => {
    trigger.textContent = original;
    trigger.disabled = false;
  }, MESSAGE_ACTION_FEEDBACK_MS);
}

function quoteMessageContent(role, contentElement) {
  const raw = (contentElement?.dataset.raw || contentElement?.innerText || "").trim();
  if (!raw) return;
  const label = role === "user" ? "你" : "SJTUClaw";
  state.pendingQuote = {
    role,
    label,
    content: raw,
    summary: summarizeMessageForIndex(raw),
  };
  renderQuotePreview();
  input.focus();
}

function renderQuotePreview() {
  const preview = $("#quote-preview");
  const label = $("#quote-preview-label");
  const text = $("#quote-preview-text");
  if (!state.pendingQuote) {
    preview.hidden = true;
    label.textContent = "引用";
    text.textContent = "";
    return;
  }
  preview.hidden = false;
  label.textContent = `引用 ${state.pendingQuote.label}`;
  text.textContent = state.pendingQuote.summary;
}

function clearPendingQuote() {
  state.pendingQuote = null;
  renderQuotePreview();
}

function sourceLabel(source) {
  return ({
    web: "Web",
    cli: "CLI",
    feishu: "飞书",
    weixin: "微信",
    qqbot: "QQ",
    scheduler: "定时",
    desktop: "桌宠",
  })[source] || source;
}

function renderMessageIndex() {
  const allMessages = [...messages.querySelectorAll(".message")];
  const query = (messageIndexSearch.value || "").trim().toLowerCase();
  const userMessages = allMessages.filter((article) => article.classList.contains("user"));
  const indexedMessages = query
    ? allMessages.filter((article) => {
        const raw = article.querySelector(".message-content")?.dataset.raw || article.innerText || "";
        return raw.toLowerCase().includes(query);
      })
    : userMessages;
  messageIndexList.replaceChildren();
  messageIndex.hidden = userMessages.length === 0;
  messageIndexTitle.textContent = query ? "搜索结果" : "你的消息";
  messageIndexToggle.textContent = "索引";
  if (!userMessages.length) return;
  if (!indexedMessages.length) {
    const empty = document.createElement("div");
    empty.className = "message-index-empty";
    empty.textContent = "没有匹配的消息";
    messageIndexList.append(empty);
    return;
  }
  indexedMessages.forEach((article, index) => {
    const number = query
      ? [...allMessages].indexOf(article) + 1
      : index + 1;
    article.id = article.id || `message-anchor-${crypto.randomUUID().replaceAll("-", "").slice(0, 10)}`;
    article.dataset.messageIndex = String(number);
    const raw = article.querySelector(".message-content")?.dataset.raw || article.innerText || "";
    const summary = summarizeMessageForIndex(raw);
    const role = article.classList.contains("user") ? "你" : "S";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "message-index-item";
    button.title = raw.trim();
    button.innerHTML = `<span></span><em></em>`;
    button.querySelector("span").textContent = query ? role : "•";
    button.querySelector("em").textContent = query ? `${role} · ${summary}` : summary;
    button.onclick = () => jumpToMessage(article);
    messageIndexList.append(button);
  });
}

function summarizeMessageForIndex(content) {
  const clean = content
    .split("\n\n[attached_files] ", 1)[0]
    .replace(/\s+/g, " ")
    .trim();
  if (!clean) return "空消息";
  return clean.length > 34 ? `${clean.slice(0, 34)}…` : clean;
}

function jumpToMessage(article) {
  article.scrollIntoView({ behavior: "smooth", block: "center" });
  article.classList.remove("message-highlight");
  void article.offsetWidth;
  article.classList.add("message-highlight");
  window.setTimeout(() => article.classList.remove("message-highlight"), 1300);
}

messageIndexToggle.onclick = () => {
  const collapsed = messageIndex.classList.toggle("collapsed");
  messageIndexToggle.setAttribute("aria-expanded", String(!collapsed));
  if (!collapsed) messageIndexSearch.focus();
};

messageIndexSearch.addEventListener("input", renderMessageIndex);

function getUserDisplayName() {
  const stored = localStorage.getItem(USER_NAME_STORAGE_KEY) || "";
  const normalized = stored.replace(/\s+/g, " ").trim();
  return normalized || DEFAULT_USER_NAME;
}

function setUserDisplayName(name) {
  const normalized = String(name || "").replace(/\s+/g, " ").trim().slice(0, 20);
  if (normalized && normalized !== DEFAULT_USER_NAME) {
    localStorage.setItem(USER_NAME_STORAGE_KEY, normalized);
  } else {
    localStorage.removeItem(USER_NAME_STORAGE_KEY);
  }
  refreshUserDisplayNames();
}

function editUserDisplayName() {
  const current = getUserDisplayName();
  const next = window.prompt("设置用户名（留空恢复为“你”）", current === DEFAULT_USER_NAME ? "" : current);
  if (next === null) return;
  setUserDisplayName(next);
}

function refreshUserDisplayNames() {
  const name = getUserDisplayName();
  document.querySelectorAll('.message.user .message-name').forEach((element) => {
    element.textContent = name;
  });
}

function setupAvatar(element, role) {
  element.dataset.avatarRole = role;
  element.tabIndex = 0;
  element.role = "button";
  element.title = `点击更换${AVATAR_NAMES[role]}；右键恢复默认`;
  renderAvatarElement(element, role);
  element.onclick = () => chooseAvatar(role);
  element.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      chooseAvatar(role);
    }
  };
  element.oncontextmenu = (event) => {
    event.preventDefault();
    resetAvatar(role);
  };
}

function renderAvatarElement(element, role) {
  const image = localStorage.getItem(AVATAR_STORAGE_KEYS[role]);
  element.replaceChildren();
  if (image) {
    const img = document.createElement("img");
    img.src = image;
    img.alt = AVATAR_NAMES[role];
    element.append(img);
    element.classList.add("has-image");
  } else {
    element.textContent = AVATAR_LABELS[role];
    element.classList.remove("has-image");
  }
}

function refreshAvatars(role = null) {
  document.querySelectorAll(".avatar[data-avatar-role]").forEach((element) => {
    if (!role || element.dataset.avatarRole === role) {
      renderAvatarElement(element, element.dataset.avatarRole);
    }
  });
}

function chooseAvatar(role) {
  avatarTargetRole = role;
  avatarInput.value = "";
  avatarInput.click();
}

function resetAvatar(role) {
  localStorage.removeItem(AVATAR_STORAGE_KEYS[role]);
  refreshAvatars(role);
}

avatarInput.addEventListener("change", async () => {
  const file = avatarInput.files?.[0];
  const role = avatarTargetRole;
  avatarTargetRole = null;
  if (!file || !role) return;
  if (!file.type.startsWith("image/")) {
    showError(new Error("请选择图片文件作为头像。"));
    return;
  }
  if (file.size > MAX_AVATAR_BYTES) {
    showError(new Error("头像图片不能超过 2 MB。"));
    return;
  }
  try {
    const dataUrl = await readFileAsDataUrl(file);
    localStorage.setItem(AVATAR_STORAGE_KEYS[role], dataUrl);
    refreshAvatars(role);
  } catch (error) {
    showError(error);
  }
});

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("头像读取失败，请换一张图片重试。"));
    reader.readAsDataURL(file);
  });
}

function parseProtocolMessage(content) {
  const text = content.trim();
  if (text.startsWith("[approval_retry_required]")) {
    return { type: "approval_retry_required" };
  }
  if (text.startsWith("[approval_required]")) {
    try {
      const approvals = JSON.parse(text.slice("[approval_required]".length).trim());
      return { type: "approval_required", approvals: Array.isArray(approvals) ? approvals : [approvals] };
    } catch { return { type: "protocol_error", message: "审批请求无法解析" }; }
  }
  if (text.startsWith("[approval_result]")) {
    try {
      return { type: "approval_result", approval: JSON.parse(text.slice("[approval_result]".length).trim()) };
    } catch { return { type: "protocol_error", message: "审批结果无法解析" }; }
  }
  if (text.startsWith("[scheduler_task_failed")) {
    const firstBreak = text.indexOf("\n");
    const header = firstBreak >= 0 ? text.slice(0, firstBreak) : text;
    const taskId = header.match(/taskId=([^\]\s]+)/)?.[1] || "unknown";
    const detail = firstBreak >= 0 ? text.slice(firstBreak + 1) : "定时任务执行失败";
    return { type: "scheduler_failure", taskId, message: detail };
  }
  if (text.startsWith("[tool_results]")) {
    try {
      const results = JSON.parse(text.slice("[tool_results]".length).trim());
      return { type: "tool_results", results: Array.isArray(results) ? results : [results] };
    } catch { return { type: "protocol_error", message: "工具结果无法解析" }; }
  }
  if (text.startsWith("[protocol_error]")) {
    return { type: "protocol_error", message: text.slice("[protocol_error]".length).trim() };
  }
  if (text.startsWith("{")) {
    try {
      const value = JSON.parse(text);
      if (value?.type === "final" && typeof value.content === "string") return value;
      if (value?.type === "tool_call") return value;
      if (value?.type === "tool_calls" && Array.isArray(value.calls)) return value;
    } catch { /* 普通文本继续按 Markdown 展示 */ }
  }
  return null;
}

function createApprovalHistoryCard(approval) {
  const card = document.createElement("details");
  card.className = "approval-history";
  card.dataset.approvalId = approval.approvalId || "unknown";
  card.innerHTML = '<summary><span class="approval-history-icon">◇</span><strong></strong><span class="approval-history-status"></span><span class="tool-event-chevron">⌄</span></summary><div class="approval-history-body"><p></p><pre></pre></div>';
  updateApprovalHistoryCard(card, approval);
  return card;
}

function updateApprovalHistoryCard(card, approval) {
  const result = approval.toolResult;
  const decision = approval.decision;
  const success = result?.success !== false;
  const tool = approval.tool || result?.tool || "需要审批的操作";
  card.classList.toggle("approved", decision === "approved" && success);
  card.classList.toggle("rejected", decision === "rejected" || (result && !success));
  card.querySelector("strong").textContent = tool;
  const status = card.querySelector(".approval-history-status");
  const message = card.querySelector(".approval-history-body p");
  if (!decision) {
    status.textContent = "等待用户审批";
    message.textContent = "该操作需要明确批准后才会执行。";
  } else if (decision === "approved" && success) {
    status.textContent = "已批准 · 执行成功";
    message.textContent = result?.output?.message || "操作已获批准并成功执行。";
  } else if (decision === "rejected") {
    status.textContent = "已拒绝";
    message.textContent = approval.reason || "用户拒绝了该操作。";
  } else {
    status.textContent = "已批准 · 执行失败";
    message.textContent = result?.error || "操作执行失败。";
  }
  let retry = card.querySelector(".approval-retry-button");
  if (decision === "approved" && result && !success && approval.approvalId) {
    if (!retry) {
      retry = document.createElement("button");
      retry.type = "button";
      retry.className = "approval-retry-button";
      retry.onclick = () => retryApproval(approval.approvalId, retry);
      card.querySelector(".approval-history-body").append(retry);
    }
    retry.textContent = "重新申请审批";
    retry.hidden = false;
  } else if (retry) {
    retry.hidden = true;
  }
  card.querySelector("pre").textContent = JSON.stringify(approval, null, 2);
}

function createSystemNotice({ taskId, message }) {
  const card = document.createElement("details");
  card.className = "system-notice failed";
  const summary = document.createElement("summary");
  const title = document.createElement("strong");
  title.textContent = "定时任务执行失败";
  const id = document.createElement("span");
  id.textContent = taskId;
  summary.append(title, id);
  const detail = document.createElement("pre");
  detail.textContent = message;
  card.append(summary, detail);
  return card;
}

function isNearMessageBottom(threshold = 96) {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight <= threshold;
}

function maybeScrollMessagesToBottom(force = false) {
  if (force || isNearMessageBottom()) {
    messages.scrollTop = messages.scrollHeight;
  }
}

function finishStreamingMessages() {
  messages.querySelectorAll(".message-content.is-streaming").forEach((element) => {
    element.classList.remove("is-streaming");
  });
}

function resetStopButton() {
  const button = $("#stop-button");
  button.hidden = true;
  button.disabled = false;
  button.textContent = "■";
  button.setAttribute("aria-label", "停止");
  button.title = "停止本轮处理";
}

function markTurnCancelling(turnId) {
  if (!turnId || state.cancelRequestedTurnId === turnId) return;
  state.cancelRequestedTurnId = turnId;
  finishStreamingMessages();
  const button = $("#stop-button");
  button.disabled = true;
  button.textContent = "…";
  button.setAttribute("aria-label", "正在停止");
  button.title = "正在停止本轮处理";
  if (state.cancelNotice?.isConnected) state.cancelNotice.remove();
  const notice = document.createElement("div");
  notice.className = "tool-card turn-cancel-notice";
  notice.textContent = "正在停止本轮处理…";
  messages.append(notice);
  state.cancelNotice = notice;
  maybeScrollMessagesToBottom(true);
}

function finishCancelledTurn(turnId, stillRunning = false) {
  if (turnId && state.turnId && state.turnId !== turnId) return;
  if (state.cancelNotice?.isConnected) {
    state.cancelNotice.textContent = stillRunning
      ? "本轮已停止，正在释放后台请求…"
      : "本轮已停止";
  }
  if (stillRunning) state.detachedTurnIds.add(turnId);
  else state.detachedTurnIds.delete(turnId);
  state.busy = false;
  state.turnId = null;
  state.activeTurn = null;
  state.cancelRequestedTurnId = null;
  state.cancelNotice = null;
  state.turnAbortController = null;
  sendButton.disabled = false;
  sendButton.hidden = false;
  resetStopButton();
  input.disabled = false;
  input.focus();
}

async function waitForTurnInactive(turnId, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const payload = await api(`/api/turns/active?sessionId=${encodeURIComponent(state.currentSessionId || "")}`);
      if (!(payload.turns || []).some((turn) => turn.turnId === turnId)) return true;
    } catch {
      // A transient health/refresh error must not prevent the cancellation UI
      // from completing. The next refresh can reconcile the session state.
    }
    await new Promise((resolve) => window.setTimeout(resolve, 160));
  }
  return false;
}

async function requestTurnCancellation(turnId) {
  if (!turnId || state.cancelRequestedTurnId === turnId) return;
  markTurnCancelling(turnId);
  // Closing the browser's reader is local and immediate. The Gateway also
  // receives an ``immediate`` request; the Runtime closes an in-flight model
  // stream where the provider SDK supports it, instead of waiting for a
  // long HTTP timeout.
  if (state.turnAbortController) state.turnAbortController.abort();
  if (state.reconnectingTurnId === turnId) stopActiveTurnReconnect();
  try {
    await api(`/api/turns/${encodeURIComponent(turnId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "immediate" }),
    });
  } catch (error) {
    if (error.status !== 404) showError(error);
  }
  // A refreshed page can lose the exact turn id while an approval worker is
  // still alive. Cancel by Session as an idempotent fallback.
  if (state.currentSessionId) {
    await fetch("/api/sessions/" + encodeURIComponent(state.currentSessionId) + "/turn/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "immediate" }),
    }).catch(() => {});
  }
  // Do not make the user wait for backend reconciliation. The old turn is
  // detached locally and ignored if its worker briefly remains registered.
  finishCancelledTurn(turnId, true);
  const stopped = await waitForTurnInactive(turnId);
  if (stopped) {
    state.detachedTurnIds.delete(turnId);
    await loadSessions(state.currentSessionId).catch(() => {});
  } else {
    // A provider may take longer than the short reconciliation window to
    // return from one in-flight HTTP request. Keep watching without blocking
    // the page; the controls were already restored above.
    window.setTimeout(async () => {
      const eventuallyStopped = await waitForTurnInactive(turnId, 60000);
      if (eventuallyStopped) {
        state.detachedTurnIds.delete(turnId);
        await loadSessions(state.currentSessionId).catch(() => {});
      }
    }, 1000);
  }
}

function finalDisplayText(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed.startsWith("{")) return value;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed?.type === "final" && typeof parsed.content === "string") {
      return parsed.content;
    }
  } catch { /* plain markdown */ }
  return unwrapLooseFinalMessage(value);
}

function stripProtocolTail(value) {
  // Some providers place a Tool JSON object after a streamed prose prefix.
  // Streaming may split that object after any character, so remove both a
  // complete object and prefixes such as `{"type":"tool_c` on reset.
  const text = String(value || "");
  const brace = text.lastIndexOf("{");
  if (brace < 0) return text;
  const compact = text.slice(brace).replace(/\s+/g, "");
  const targets = ['{"type":"tool_call"', '{"type":"tool_calls"'];
  if (
    compact === "{"
    || targets.some((target) => target.startsWith(compact) || compact.startsWith(target))
  ) {
    return text.slice(0, brace).trimEnd();
  }
  return text;
}

function unwrapLooseFinalMessage(value) {
  const text = String(value || "").trim();
  const match = text.match(/^\{\s*"type"\s*:\s*"final"\s*,\s*"content"\s*:\s*"/);
  if (!match) return value;
  let content = text.slice(match[0].length);
  content = content.replace(/"\s*\}\s*$/, "");
  content = content
    .replace(/\\"/g, '"')
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\\\/g, "\\");
  return content;
}

function buildCitationMap(trace = [], citationIndex = []) {
  const map = {};
  map.__aliases = {};
  state.citationNext = 1;
  for (const event of trace || []) {
    collectCitationsFromToolResult(event.result, map, { globalAliases: true });
  }
  // Compaction can remove the protocol messages and tool trace that carried
  // an older search result. The runtime keeps a bounded citation index so
  // historical [Wn] markers remain real links after the session is compacted.
  if (Array.isArray(citationIndex) && citationIndex.length) {
    collectCitationsFromToolResult(
      { tool: "web_search", output: { citations: citationIndex } },
      map,
    );
  }
  return map;
}

function collectCitationsFromToolResult(result, target = state.citationMap, options = {}) {
  const output = result?.output || result;
  const toolName = result?.tool || output?.tool;
  const setCitation = (label, item) => {
    if (!label) return;
    const citation = {
      label,
      kind: item?.kind || item?.type || "",
      url: item?.url || "",
      title: item?.title || item?.url || item?.filename || label,
      filename: item?.filename || "",
      page: item?.page,
      slide: item?.slide,
      section: item?.section,
      block: item?.block,
      confidence: item?.confidence,
      attachmentId: item?.attachmentId || output?.attachmentId || result?.attachmentId || "",
      path: item?.path || output?.path || "",
    };
    const aliases = target.__aliases || state.citationAliases || {};
    aliases[label] ||= [];
    const duplicate = aliases[label].some((existing) =>
      existing.url === citation.url
      && existing.filename === citation.filename
      && existing.attachmentId === citation.attachmentId
      && existing.page === citation.page
      && existing.slide === citation.slide
      && existing.section === citation.section
      && existing.block === citation.block
    );
    if (!duplicate) aliases[label].push(citation);
    if (target.__aliases) target.__aliases = aliases;
    else state.citationAliases = aliases;
    // Keep the latest direct mapping for legacy sessions where providers
    // reused [W1] on every request. Per-message maps below avoid ambiguity for
    // newly loaded sessions; latest-wins is the safest global fallback.
    target[label] = citation;
  };
  const citations = Array.isArray(output?.citations) && output.citations.length
    ? output.citations
    : synthesizeAttachmentCitations(output);
  for (const citation of citations) {
    setCitation(citation?.label, citation);
  }
  const results = Array.isArray(output?.results) ? output.results : [];
  if (!citations.length) {
    results.forEach((item, index) => {
      if (!item?.url) return;
      setCitation(`[W${index + 1}]`, item);
    });
  }
  const globalSources = citations.length ? citations : results;
  if (options.globalAliases && toolName === "web_search") {
    for (const item of globalSources) {
      if (!item?.url) continue;
      setCitation(`[W${state.citationNext}]`, item);
      state.citationNext += 1;
    }
  }
  return target;
}

function newCitationMap(source = null) {
  const target = { __aliases: {} };
  if (!source) return target;
  for (const [label, citation] of Object.entries(source || {})) {
    if (label === "__aliases") continue;
    target[label] = { ...citation };
  }
  for (const [label, citations] of Object.entries(source.__aliases || {})) {
    target.__aliases[label] = (citations || []).map((citation) => ({ ...citation }));
  }
  return target;
}

function cloneCitationMap(source) {
  const target = newCitationMap();
  for (const [label, citation] of Object.entries(source || {})) {
    if (label === "__aliases") continue;
    target[label] = { ...citation };
  }
  for (const [label, citations] of Object.entries(source?.__aliases || {})) {
    target.__aliases[label] = (citations || []).map((citation) => ({ ...citation }));
  }
  return target;
}

function citationArgsText(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, Object.keys(value).sort());
  } catch {
    return String(value);
  }
}

function buildTurnCitationMaps(activity = [], trace = []) {
  const maps = {};
  const traceItems = (trace || []).map((event, index) => ({ event, index, used: false }));
  const results = (activity || []).filter((event) => event?.type === "tool_result" && event.turnId);
  for (const activityEvent of results) {
    const data = activityEvent.data || {};
    const tool = String(data.tool || "");
    const argsPreview = String(data.args || "");
    const callId = String(data.callId || "");
    const candidates = traceItems.filter((item) => !item.used && (!tool || item.event?.tool === tool));
    if (!candidates.length) continue;
    // Prefer the durable call id. Matching only by tool/args is ambiguous
    // when a turn repeats web_search or when compact has removed the
    // intermediate protocol messages.
    const exactCall = callId
      ? candidates.find((item) => String(item.event?.callId || "") === callId)
      : null;
    const exact = exactCall || candidates.find((item) => {
      const fullArgs = citationArgsText(item.event?.args);
      return argsPreview && (fullArgs === argsPreview || fullArgs.startsWith(argsPreview.replace(/…$/, "")));
    });
    const match = exact || candidates[0];
    match.used = true;
    const result = match.event?.result;
    if (!result) continue;
    const target = maps[activityEvent.turnId] ||= newCitationMap();
    collectCitationsFromToolResult(result, target);
  }
  return maps;
}

function buildAssistantTurnIds(sessionMessages = [], activity = []) {
  const finals = (activity || []).filter((event) => event?.type === "assistant_final" && event.turnId);
  const ids = {};
  let cursor = 0;
  for (let index = 0; index < sessionMessages.length; index += 1) {
    const item = sessionMessages[index] || {};
    if (item.role !== "assistant") continue;
    const content = String(item.content || "");
    const matchIndex = finals.findIndex((event, eventIndex) => {
      if (eventIndex < cursor) return false;
      const preview = String(event.data?.contentPreview || "");
      if (!preview || !content) return false;
      return preview === content
        || (preview.length >= 40 && content.startsWith(preview))
        || (content.length >= 40 && preview.startsWith(content));
    });
    if (matchIndex < 0) continue;
    ids[index] = finals[matchIndex].turnId;
    cursor = matchIndex + 1;
  }
  return ids;
}

function buildMessageCitationMaps(sessionMessages = [], activity = [], trace = []) {
  const maps = {};
  const turnCitationMaps = buildTurnCitationMaps(activity, trace);
  const assistantTurnIds = buildAssistantTurnIds(sessionMessages, activity);
  // Very old/fixture sessions may contain a tool trace but no activity
  // events. Preserve their legacy labels for compatibility; once activity is
  // available, never use this session-wide fallback for an ambiguous turn.
  const hasRecordedToolActivity = (activity || []).some((event) => event?.type === "tool_result");
  const legacyMap = hasRecordedToolActivity ? null : buildCitationMap(trace, []);
  const turnStarts = (activity || []).filter((event) => event?.type === "turn_started" && event.turnId);
  let turnCursor = 0;
  let activeTurnId = null;
  let pending = newCitationMap();
  const hasCitations = (map) => Object.keys(map || {}).some((key) => key !== "__aliases");
  const appendResults = (results) => {
    for (const result of results || []) {
      if (!result?.deferred && result?.approvalRequired !== true) {
        collectCitationsFromToolResult(result, pending);
      }
    }
  };
  for (let index = 0; index < sessionMessages.length; index += 1) {
    const item = sessionMessages[index] || {};
    const content = String(item.content || "");
    if (item.role === "user") {
      const exactIndex = turnStarts.findIndex((event, eventIndex) =>
        eventIndex >= turnCursor && String(event.data?.message || "") === content
      );
      if (exactIndex >= 0) {
        turnCursor = exactIndex + 1;
        activeTurnId = turnStarts[exactIndex].turnId;
      }
    }
    const protocol = parseProtocolMessage(content);
    if (protocol?.type === "tool_results") {
      appendResults(protocol.results);
      continue;
    }
    if (protocol?.type === "approval_result" && protocol.approval?.toolResult) {
      appendResults([protocol.approval.toolResult]);
      continue;
    }
    // New turns carry their exact source list on the assistant message. This
    // is intentionally checked before the activity/trace reconstruction:
    // compaction can remove protocol messages, and global labels such as W1
    // may be reused by a later search.
    const persistedRefs = item.metadata?.citationRefs;
    if (
      item.role === "assistant"
      && Array.isArray(persistedRefs)
      && persistedRefs.length
    ) {
      const ownMap = newCitationMap();
      collectCitationsFromToolResult(
        { tool: "citation", success: true, output: { citations: persistedRefs } },
        ownMap,
      );
      if (hasCitations(ownMap)) maps[index] = ownMap;
      continue;
    }
    if (item.role !== "assistant" || protocol?.type === "tool_call" || protocol?.type === "tool_calls") {
      continue;
    }
    if (content.startsWith("[deferred_action_promise]")) continue;
    const turnId = assistantTurnIds[index] || activeTurnId;
    const turnMap = turnId ? turnCitationMaps[turnId] : null;
    if (hasCitations(turnMap)) {
      maps[index] = cloneCitationMap(turnMap);
    } else if (hasCitations(pending)) {
      maps[index] = cloneCitationMap(pending);
      pending = newCitationMap();
    } else if (legacyMap && hasCitations(legacyMap)) {
      maps[index] = cloneCitationMap(legacyMap);
    }
  }
  return maps;
}

function synthesizeAttachmentCitations(output) {
  if (!output || output.source !== "attachment" || !output.attachmentId) return [];
  const filename = output.filename || attachmentFilename(output.attachmentId) || "";
  const format = String(output.format || "").toLowerCase();
  const prefix = format === "pdf" ? "P" : format === "pptx" ? "S" : format === "docx" ? "D" : "A";
  const pageDetails = Array.isArray(output.pageDetails) ? output.pageDetails : [];
  const count = Number(output.pages || output.processedPages || pageDetails.length || 1);
  return Array.from({ length: Math.max(1, count) }, (_, index) => {
    const detail = pageDetails[index] || {};
    return {
      label: `[${prefix}${index + 1}]`,
      kind: output.source,
      filename,
      attachmentId: output.attachmentId,
      page: detail.page || index + 1,
    };
  });
}

function createToolCard({ tool, args = null, result = null, phase, approvalId = null, callId = null }) {
  const success = result?.success !== false;
  const details = document.createElement("details");
  details.className = `tool-event ${phase}${success ? " success" : " failed"}`;
  details.dataset.tool = tool || "Tool";
  details.dataset.phase = phase;
  details.dataset.success = String(success);
  details.dataset.callId = callId || result?.callId || "";
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "tool-event-icon";
  icon.textContent = phase === "call" ? "↗" : success ? "✓" : "!";
  const title = document.createElement("strong");
  title.textContent = tool || "Tool";
  const status = document.createElement("span");
  status.className = "tool-event-status";
  status.textContent = phase === "call" ? "正在调用" : success ? "调用成功" : "调用失败";
  const chevron = document.createElement("span");
  chevron.className = "tool-event-chevron";
  chevron.textContent = "⌄";
  summary.append(icon, title, status, chevron);
  details.append(summary);

  const body = document.createElement("div");
  body.className = "tool-event-body";
  const output = result?.output;
  if (phase === "result" && !success && result?.error) {
    const error = document.createElement("p");
    error.className = "tool-error-summary";
    error.textContent = result.error;
    body.append(error);
  }
  if (phase === "result" && output?.answer) {
    const answer = document.createElement("p");
    answer.className = "tool-answer";
    answer.textContent = output.answer;
    body.append(answer);
  }
  if (phase === "result" && Array.isArray(output?.results) && output.results.length) {
    const sources = document.createElement("div");
    sources.className = "tool-sources";
    for (const item of output.results.slice(0, 5)) {
      const link = document.createElement("a");
      link.href = item.url || "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = item.title || item.url || "来源";
      sources.append(link);
    }
    body.append(sources);
  }
  const raw = document.createElement("pre");
  raw.textContent = JSON.stringify(phase === "call" ? args : result, null, 2);
  body.append(raw);
  const failedApprovalId = approvalId || result?.approvalId;
  if (phase === "result" && !success && failedApprovalId) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "tool-retry-button";
    retry.textContent = "重新申请审批";
    retry.onclick = () => retryApproval(failedApprovalId, retry);
    body.append(retry);
  }
  details.append(body);
  return details;
}

function toolGroupSummary(group) {
  const cards = [...group.querySelectorAll(":scope > .tool-event-group-body > .tool-event")];
  const callCards = cards.filter((card) => card.dataset.phase === "call");
  const resultCards = cards.filter((card) => card.dataset.phase === "result");
  const uniqueCount = (items) => {
    const ids = new Set(items.map((item) => item.dataset.callId).filter(Boolean));
    const withoutId = items.filter((item) => !item.dataset.callId).length;
    return ids.size + withoutId;
  };
  const calls = uniqueCount(callCards);
  const results = uniqueCount(resultCards);
  const failures = uniqueCount(
    resultCards.filter((card) => card.dataset.success === "false"),
  );
  const status = group.querySelector(".tool-event-group-status");
  if (status) {
    const count = document.createElement("span");
    count.className = "tool-event-group-count";
    count.textContent = `调用 ${calls} 次`;
    const outcome = document.createElement("span");
    outcome.className = `tool-event-group-outcome${failures ? " failed" : results < calls ? " running" : ""}`;
    outcome.textContent = failures
      ? `失败 ${failures} 次`
      : results < calls ? "执行中" : "已完成";
    status.replaceChildren(count, outcome);
    const errors = resultCards
      .filter((card) => card.dataset.success === "false")
      .map((card) => card.querySelector(".tool-error-summary")?.textContent)
      .filter(Boolean);
    status.title = [...new Set(errors)].join("\n");
  }
  group.classList.toggle("failed", failures > 0);
  group.classList.toggle("running", results < calls);
  if (results < calls) group.open = true;
  else if (!group.dataset.userToggled) group.open = false;
}

function createToolGroup(tool, cards) {
  const group = document.createElement("details");
  group.className = "tool-event-group";
  group.dataset.tool = tool;
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "tool-event-group-icon";
  icon.textContent = "↗";
  const title = document.createElement("strong");
  title.textContent = tool || "Tool";
  const status = document.createElement("span");
  status.className = "tool-event-group-status";
  const chevron = document.createElement("span");
  chevron.className = "tool-event-chevron";
  chevron.textContent = "⌄";
  summary.append(icon, title, status, chevron);
  const body = document.createElement("div");
  body.className = "tool-event-group-body";
  body.append(...cards);
  group.append(summary, body);
  group.addEventListener("toggle", () => {
    if (group.isConnected) group.dataset.userToggled = "true";
  });
  toolGroupSummary(group);
  return group;
}

function insertToolCard(card, before = null) {
  const anchor = before || null;
  const previous = anchor ? anchor.previousElementSibling : messages.lastElementChild;
  if (previous?.classList.contains("tool-event-group") && previous.dataset.tool === card.dataset.tool) {
    previous.querySelector(".tool-event-group-body").append(card);
    toolGroupSummary(previous);
    return previous;
  }

  const consecutive = [];
  let cursor = previous;
  while (cursor?.classList.contains("tool-event") && cursor.dataset.tool === card.dataset.tool) {
    consecutive.unshift(cursor);
    cursor = cursor.previousElementSibling;
  }
  const callCount = consecutive.filter((item) => item.dataset.phase === "call").length
    + (card.dataset.phase === "call" ? 1 : 0);
  if (callCount >= 2) {
    const first = consecutive[0];
    const group = createToolGroup(card.dataset.tool, []);
    messages.insertBefore(group, first || anchor);
    group.querySelector(".tool-event-group-body").append(...consecutive, card);
    toolGroupSummary(group);
    return group;
  }
  messages.insertBefore(card, anchor);
  return card;
}

async function retryApproval(approvalId, trigger = null) {
  if (!approvalId || state.busy) return;
  if (trigger) {
    trigger.disabled = true;
    trigger.textContent = "正在申请…";
  }
  try {
    const response = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `重试审批失败 (${response.status})`);
    await loadSession(state.currentSessionId);
    await loadApprovals();
    maybeScrollMessagesToBottom();
  } catch (error) {
    showError(error);
    if (trigger) {
      trigger.disabled = false;
      trigger.textContent = "重新申请审批";
    }
  }
}

function createCompactionCard(payload = {}) {
  const card = document.createElement("details");
  card.className = "compaction-card success";
  // A completed compaction is durable conversation history, but its summary
  // is secondary information. Keep the compact row visible and let the user
  // expand it only when they want to inspect the generated summary.
  card.open = false;
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "compaction-icon";
  icon.textContent = "✦";
  const title = document.createElement("strong");
  title.textContent = "上下文整理完成";
  const status = document.createElement("span");
  status.className = "compaction-status";
  status.textContent = compactionStats(payload);
  const chevron = document.createElement("span");
  chevron.className = "compaction-chevron";
  chevron.textContent = "⌄";
  summary.append(icon, title, status, chevron);
  card.append(summary);

  const body = document.createElement("div");
  body.className = "compaction-body";
  const label = document.createElement("p");
  label.className = "compaction-label";
  label.textContent = "新的 Session Summary 预览";
  const preview = document.createElement("div");
  preview.className = "compaction-preview message-content";
  const summaryText = payload.summaryPreview || "（摘要预览为空）";
  preview.dataset.raw = summaryText;
  renderMessageContent(preview, summaryText, state.citationMap);
  body.append(label, preview);
  card.append(body);
  return card;
}

function compactionStats(payload = {}, { pending = false } = {}) {
  const hasOldMessages = Number.isFinite(Number(payload.oldMessages))
    && payload.oldMessages !== null && payload.oldMessages !== "";
  const hasRecentMessages = Number.isFinite(Number(payload.recentMessages))
    && payload.recentMessages !== null && payload.recentMessages !== "";
  // Manual compaction cannot know the exact split until the Compactor has
  // inspected semantic messages. Missing telemetry is "unknown", not zero.
  if (!hasOldMessages && !hasRecentMessages) {
    return pending ? "正在计算整理范围…" : "";
  }
  const chunks = Number(payload.chunks || 0);
  const parts = [];
  if (hasOldMessages) parts.push(`整理 ${Number(payload.oldMessages)} 条旧消息`);
  if (hasRecentMessages) parts.push(`保留最近 ${Number(payload.recentMessages)} 条`);
  if (chunks > 1) parts.push(`${chunks} 个摘要分片`);
  return parts.join(" · ");
}

function createCompactionProgressCard(payload = {}) {
  const card = document.createElement("article");
  card.className = "compaction-card pending";
  const header = document.createElement("div");
  header.className = "compaction-header";
  const icon = document.createElement("span");
  icon.className = "compaction-icon spinning";
  icon.textContent = "↻";
  const title = document.createElement("strong");
  title.textContent = "正在整理上下文";
  const status = document.createElement("span");
  status.className = "compaction-status";
  status.textContent = compactionStats(payload, { pending: true });
  header.append(icon, title, status);
  const hint = document.createElement("p");
  hint.className = "compaction-hint";
  hint.textContent = "对话太长了，我正在把早期内容整理成摘要，当前任务不会中断。";
  card.append(header, hint);
  return card;
}

function createCompactionErrorCard(payload = {}) {
  const card = document.createElement("article");
  card.className = "compaction-card failed";
  const header = document.createElement("div");
  header.className = "compaction-header";
  const icon = document.createElement("span");
  icon.className = "compaction-icon";
  icon.textContent = "!";
  const title = document.createElement("strong");
  title.textContent = "上下文整理失败";
  const status = document.createElement("span");
  status.className = "compaction-status";
  status.textContent = compactionStats(payload);
  header.append(icon, title, status);
  const detail = document.createElement("p");
  detail.className = "compaction-hint";
  detail.textContent = payload.error || "原有消息已保留，可以稍后重试。";
  card.append(header, detail);
  return card;
}

function updateCompactionConversationCard(current, anchor, eventName, payload = {}) {
  const next = eventName === "compaction_started"
    ? createCompactionProgressCard(payload)
    : eventName === "compaction_failed"
      ? createCompactionErrorCard(payload)
      : createCompactionCard(payload);
  if (current?.isConnected) current.replaceWith(next);
  else if (anchor?.parentNode === messages) messages.insertBefore(next, anchor);
  else messages.append(next);
  next.dataset.liveCompaction = "true";
  if (payload.turnId) next.dataset.compactionTurnId = payload.turnId;
  return next;
}

const PLAN_STATUS_LABELS = {
  pending: "待执行",
  in_progress: "执行中",
  awaiting_approval: "待审批",
  completed: "已验证",
  failed: "执行失败",
  blocked: "受阻",
};

function createExecutionPlanCard(goal = {}, options = {}) {
  const plan = goal?.plan;
  if (!plan || !Array.isArray(plan.steps) || !plan.steps.length) return null;
  const card = document.createElement("details");
  card.className = `execution-plan-card ${plan.status || "active"}`;
  card.open = options.live === true && !["completed", "blocked", "cancelled"].includes(plan.status);
  card.dataset.planId = plan.planId || "";

  const completed = plan.steps.filter((step) => step.status === "completed").length;
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "execution-plan-icon";
  icon.textContent = plan.status === "completed" ? "✓" : plan.status === "blocked" ? "!" : "↻";
  const heading = document.createElement("span");
  heading.className = "execution-plan-heading";
  const title = document.createElement("strong");
  title.textContent = plan.status === "completed" ? "任务执行完成" : "任务执行计划";
  const count = document.createElement("span");
  count.textContent = `${completed}/${plan.steps.length} 步`;
  heading.append(title, count);
  const phase = document.createElement("span");
  phase.className = "execution-plan-summary";
  phase.textContent = goal.currentStep || PLAN_STATUS_LABELS[plan.status] || "处理中";
  const chevron = document.createElement("span");
  chevron.className = "execution-plan-chevron";
  chevron.textContent = "⌄";
  summary.append(icon, heading, phase, chevron);
  card.append(summary);

  const body = document.createElement("div");
  body.className = "execution-plan-body";
  const rail = document.createElement("div");
  rail.className = "execution-plan-rail";
  const phaseNames = { plan: "计划", execute: "执行", verify: "验证" };
  for (const key of ["plan", "execute", "verify"]) {
    const phaseSteps = plan.steps.filter((step) => step.phase === key);
    const item = document.createElement("span");
    const allDone = phaseSteps.length && phaseSteps.every((step) => step.status === "completed");
    const active = phaseSteps.some((step) => ["in_progress", "awaiting_approval"].includes(step.status));
    item.className = allDone ? "done" : active ? "active" : "";
    item.textContent = phaseNames[key];
    rail.append(item);
  }
  body.append(rail);

  const list = document.createElement("ol");
  list.className = "execution-plan-steps";
  const statusIcons = {
    pending: "○", in_progress: "↻", awaiting_approval: "◇",
    completed: "✓", failed: "!", blocked: "×",
  };
  for (const step of plan.steps) {
    const row = document.createElement("li");
    row.className = `execution-plan-step ${step.status || "pending"}`;
    const marker = document.createElement("span");
    marker.className = "execution-plan-step-icon";
    marker.textContent = statusIcons[step.status] || "○";
    const content = document.createElement("div");
    const line = document.createElement("div");
    line.className = "execution-plan-step-title";
    const stepTitle = document.createElement("strong");
    stepTitle.textContent = step.title || "执行步骤";
    const state = document.createElement("span");
    state.textContent = PLAN_STATUS_LABELS[step.status] || step.status || "待执行";
    line.append(stepTitle, state);
    content.append(line);
    const evidenceItems = [...(step.evidence || [])].reverse();
    // A provider may repeat a read-only call after a successful result.  The
    // plan step remains completed; prefer its latest successful proof instead
    // of presenting a later deduplication/no-progress diagnostic as if the
    // whole step had failed.
    const latestEvidence = step.status === "completed"
      ? evidenceItems.find((item) => item?.label && item.success === true)
        || evidenceItems.find((item) => item?.label)
      : evidenceItems.find((item) => item?.label);
    const evidence = document.createElement("p");
    evidence.className = "execution-plan-evidence";
    if (latestEvidence) {
      evidence.classList.add(latestEvidence.success ? "verified" : "failed");
      evidence.textContent = `证据 · ${latestEvidence.label}`;
    } else {
      evidence.textContent = `预期 · ${(step.expectedEvidence || []).join("；") || "完成后由 Runtime 验证"}`;
    }
    content.append(evidence);
    row.append(marker, content);
    list.append(row);
  }
  body.append(list);
  card.append(body);
  return card;
}

function updateExecutionPlanCard(current, anchor, goal = {}, options = {}) {
  const next = createExecutionPlanCard(goal, options);
  if (!next) return current;
  if (current?.isConnected) {
    current.replaceWith(next);
  } else {
    // loadSession may already have restored the durable snapshot before an
    // active Turn reconnects. Move that same plan to the live anchor instead
    // of displaying a second card for one planId.
    const durable = [...messages.querySelectorAll(".execution-plan-card")]
      .find((item) => item.dataset.planId && item.dataset.planId === next.dataset.planId);
    durable?.remove();
    if (anchor?.parentNode === messages) messages.insertBefore(next, anchor);
    else messages.append(next);
  }
  return next;
}

function settleExecutionPlanCard(current, donePayload = {}) {
  if (!current) return null;
  // A paused Turn still needs the plan to explain what the approval will
  // resume.  Once a Turn has otherwise ended, the plan is transient process
  // UI and should not remain below the completed answer.
  if (donePayload?.status === "approval_required") {
    current.open = true;
    current.classList.remove("live");
    return current;
  }
  current.remove();
  return null;
}

function compactionAnchor(assistantText, fallback) {
  const message = assistantText?.closest(".message");
  if (message?.parentNode === messages) return message.nextSibling;
  return fallback;
}

function persistedCompactionRecords(session) {
  const activity = Array.isArray(session?.activity) ? session.activity : [];
  const records = [];
  const seen = new Set();
  activity.forEach((event, index) => {
    if (event?.type !== "compaction" || !event.data) return;
    const version = Number(event.data.summaryVersion || 0);
    const key = version
      ? `version:${version}`
      : `event:${event.eventId || event.timestamp || index}`;
    if (seen.has(key)) return;
    seen.add(key);
    records.push({
      ...event.data,
      turnId: event.turnId || null,
      eventId: event.eventId || "",
      timestamp: event.timestamp || "",
    });
  });

  // Compatibility fallback for sessions compacted by older Gateway versions:
  // they persisted summaryMeta but did not append a compaction activity event.
  const meta = session?.summaryMeta || {};
  const metaVersion = Number(meta.version || 0);
  if (metaVersion && !seen.has(`version:${metaVersion}`)) {
    records.push({
      oldMessages: meta.oldMessages,
      recentMessages: meta.recentMessages,
      oldTokens: meta.oldTokens,
      recentTokens: meta.recentTokens,
      chunks: meta.chunks,
      summaryPreview: meta.preview || session.summary || "",
      summaryVersion: metaVersion,
      coveredMessageStart: meta.coveredMessageStart,
      coveredMessageEnd: meta.coveredMessageEnd,
      qualityWarnings: meta.qualityWarnings || [],
      turnId: null,
      compatibilityFallback: true,
    });
  }
  return records;
}

function createCompactionHistoryCard(records = []) {
  const card = document.createElement("details");
  card.className = "compaction-card compaction-history-card";
  card.open = false;
  card.dataset.persistedCompactionHistory = "true";

  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "compaction-icon";
  icon.textContent = "↻";
  const title = document.createElement("strong");
  title.textContent = "历史上下文整理";
  const status = document.createElement("span");
  status.className = "compaction-status";
  status.textContent = `${records.length} 次较早的整理记录`;
  const chevron = document.createElement("span");
  chevron.className = "compaction-chevron";
  chevron.textContent = "⌄";
  summary.append(icon, title, status, chevron);
  card.append(summary);

  const body = document.createElement("div");
  body.className = "compaction-history-body";
  records.forEach((payload, index) => {
    const entry = document.createElement("details");
    entry.className = "compaction-history-entry";
    const entrySummary = document.createElement("summary");
    const label = document.createElement("strong");
    const version = Number(payload.summaryVersion || 0);
    label.textContent = version ? `第 ${version} 次整理` : `整理记录 ${index + 1}`;
    const stats = document.createElement("span");
    stats.textContent = compactionStats(payload);
    const entryChevron = document.createElement("span");
    entryChevron.className = "compaction-chevron";
    entryChevron.textContent = "⌄";
    entrySummary.append(label, stats, entryChevron);

    const preview = document.createElement("div");
    preview.className = "compaction-preview message-content";
    const summaryText = payload.summaryPreview || "（摘要预览为空）";
    preview.dataset.raw = summaryText;
    renderMessageContent(preview, summaryText, state.citationMap);
    entry.append(entrySummary, preview);
    body.append(entry);
  });
  card.append(body);
  return card;
}

function renderPersistedCompactionCards(session) {
  const records = persistedCompactionRecords(session);
  if (!records.length) return;

  const assistantTurnIds = buildAssistantTurnIds(
    session.messages || [],
    session.activity || [],
  );
  const turnAnchors = {};
  Object.entries(assistantTurnIds).forEach(([messageIndex, turnId]) => {
    const node = messages.querySelector(
      `[data-session-message-index="${messageIndex}"]`,
    );
    if (node && turnId) turnAnchors[turnId] = node;
  });

  // Cards whose triggering answer is still visible sit immediately after that
  // answer. If compaction has already removed the answer, group those older
  // records at the history boundary instead of filling the viewport with a
  // stack of nearly identical cards.
  const firstConversationNode = messages.firstElementChild;
  const turnCursors = {};
  const historicalRecords = [];
  records.forEach((payload) => {
    const anchor = payload.turnId
      ? (turnCursors[payload.turnId] || turnAnchors[payload.turnId])
      : null;
    const manualAnchor = payload.anchorMessageId
      ? messages.querySelector(
        `[data-compaction-anchors~="${CSS.escape(String(payload.anchorMessageId))}"]`,
      )
      : null;
    let legacyAnchor = null;
    if (!manualAnchor && !anchor && payload.manual) {
      // Compatibility for manual compactions persisted before durable anchor
      // IDs existed. The retained semantic tail approximates its old boundary.
      const semanticNodes = [...messages.querySelectorAll(
        ".message:not(.progress):not(.is-intermediate)",
      )];
      const retained = Math.max(0, Number(payload.recentMessages || 0));
      legacyAnchor = retained > 0 ? semanticNodes[retained - 1] : null;
    }

    const visibleAnchor = manualAnchor?.parentNode === messages
      ? manualAnchor
      : anchor?.parentNode === messages
        ? anchor
        : legacyAnchor?.parentNode === messages
          ? legacyAnchor
          : null;
    if (!visibleAnchor) {
      historicalRecords.push(payload);
      return;
    }

    const card = createCompactionCard(payload);
    card.dataset.persistedCompaction = "true";
    if (payload.summaryVersion) {
      card.dataset.summaryVersion = String(payload.summaryVersion);
    }
    if (payload.turnId) card.dataset.compactionTurnId = payload.turnId;

    if (manualAnchor?.parentNode === messages) {
      manualAnchor.after(card);
    } else if (anchor?.parentNode === messages) {
      anchor.after(card);
      turnCursors[payload.turnId] = card;
    } else {
      legacyAnchor.after(card);
    }
  });

  if (historicalRecords.length) {
    const historyCard = createCompactionHistoryCard(historicalRecords);
    if (firstConversationNode?.parentNode === messages) {
      messages.insertBefore(historyCard, firstConversationNode);
    } else {
      messages.append(historyCard);
    }
  }
}

async function loadSession(sessionId) {
  clearError();
  if (state.pendingAttachmentSession && state.pendingAttachmentSession !== sessionId) {
    state.pendingAttachments = [];
    state.pendingAttachmentSession = null;
  }
  const session = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  state.isDraft = false;
  state.currentSessionId = session.sessionId;
  sessionStorage.setItem("sjtuclaw.lastSessionId", session.sessionId);
  $("#session-title").textContent = session.title;
  renderSessionMeta(session);
  // Workspace paths are persisted as absolute paths.  After a project folder
  // is renamed the session can still load, so check the path asynchronously
  // and mark the chip without blocking conversation rendering.
  if (session.workspace) refreshWorkspaceStatus(sessionId).catch(() => {});
  state.attachmentMap = Object.fromEntries((session.attachments || []).map((item) => [item.attachmentId, item]));
  const citationMap = buildCitationMap(session.toolTrace || [], session.citationIndex || []);
  state.citationAliases = citationMap.__aliases || {};
  delete citationMap.__aliases;
  state.citationMap = citationMap;
  state.messageCitationMaps = buildMessageCitationMaps(
    session.messages || [],
    session.activity || [],
    session.toolTrace || [],
  );
  messages.replaceChildren();
  if (!session.messages.length) {
    messages.append($("#empty-template").content.cloneNode(true));
    messages.querySelectorAll("[data-prompt]").forEach((button) => {
      button.onclick = () => { input.value = button.dataset.prompt; input.focus(); resizeInput(); };
    });
    } else {
      session.messages.forEach((item, index) => {
        addMessage(item.role, item.content, {
          ...(item.metadata || {}),
          __messageIndex: index,
          // An old assistant message without a matching tool turn must not
          // fall back to the session-wide citation index. That fallback can
          // turn an unverified [W5] into a link from an unrelated search.
          __citationMap: state.messageCitationMaps[index] || newCitationMap(),
        });
      });
    }
    renderPersistedCompactionCards(session);
    if (
      session.activeTurn
      && session.goalState?.plan
      && !["completed", "blocked", "cancelled"].includes(session.goalState.plan.status)
    ) {
      const planCard = createExecutionPlanCard(session.goalState, { live: false });
      if (planCard) messages.append(planCard);
    }
    syncRecoveredActiveTurn(session.activeTurn || null);
    if (!session.activeTurn) await showInterruptedTurnCheckpoint(session.sessionId);
    updateSessionVisibleCount(session);
    renderMessageIndex();
    renderAttachments(session.attachments || []);
  renderDownloads(session.toolTrace || []);
  await loadApprovals();
  messages.scrollTop = messages.scrollHeight;
}

async function showInterruptedTurnCheckpoint(sessionId) {
  try {
    const data = await api(`/api/turns/history?sessionId=${encodeURIComponent(sessionId)}&limit=1`);
    const latest = data.turns?.[0];
    if (latest?.status !== "interrupted") return;
    const notice = document.createElement("div");
    notice.className = "tool-card active-turn-notice interrupted-turn-notice";
    notice.textContent = `${latest.message || "上一轮未完成"} · 可重新发送问题继续处理`;
    messages.append(notice);
  } catch {
    // Older Gateways do not expose the durable journal endpoint. The normal
    // Session view remains usable in that compatibility mode.
  }
}

function syncRecoveredActiveTurn(activeTurn) {
  if (state.busy) return;
  if (activeTurn && state.detachedTurnIds.has(activeTurn.turnId)) {
    activeTurn = null;
  }
  state.activeTurn = activeTurn || null;
  state.turnId = activeTurn?.turnId || null;
  messages.querySelectorAll(".active-turn-notice").forEach((item) => item.remove());
  if (activeTurn) {
    const notice = document.createElement("div");
    notice.className = "tool-card active-turn-notice";
    const label = activeTurn.message || "SJTUClaw 仍在后台处理上一轮请求";
    notice.textContent = `${label} · 刷新不会中断，本轮完成后会自动出现在对话中`;
    messages.append(notice);
    sendButton.disabled = true;
    sendButton.hidden = false;
    $("#stop-button").hidden = false;
    input.disabled = true;
    reconnectActiveTurn(activeTurn, notice).catch(async (error) => {
      if (error?.name === "AbortError") return;
      // The stream can legitimately end with an `error` event (most often
      // after an approved Tool fails).  Do not leave the reconnect notice
      // and disabled composer stranded on screen while the Gateway has
      // already closed the Turn.
      await waitForTurnInactive(activeTurn.turnId, 5000).catch(() => false);
      await loadSession(state.currentSessionId).catch(() => {});
      if (state.activeTurn?.turnId === activeTurn.turnId) {
        notice.remove();
        state.activeTurn = null;
        state.turnId = null;
        state.busy = false;
        sendButton.disabled = false;
        sendButton.hidden = false;
        input.disabled = false;
        resetStopButton();
      }
      showError(error);
    });
  } else {
    stopActiveTurnReconnect();
    sendButton.disabled = false;
    sendButton.hidden = false;
    resetStopButton();
    input.disabled = false;
  }
}

function stopActiveTurnReconnect() {
  if (state.reconnectAbortController) {
    state.reconnectAbortController.abort();
    state.reconnectAbortController = null;
  }
  state.reconnectingTurnId = null;
}

async function replayPersistedTurn(activeTurn, notice) {
  const turnId = activeTurn?.turnId;
  if (!turnId) return false;
  state.liveCitationMap = newCitationMap(state.citationMap);
  let data;
  try {
    // A race is possible between /active and /stream: the worker may finish
    // and disappear from the in-memory registry just before the stream is
    // opened. The durable journal is the source of truth in that window.
    data = await api(`/api/turns/${encodeURIComponent(turnId)}/events?after=0`);
  } catch {
    return false;
  }
  const events = data.events || [];
  if (!events.length) return false;
  let assistantText = null;
  let assistantRaw = "";
  let compactionCard = null;
  let planCard = null;
  let donePayload = null;
  for (const item of events) {
    const eventName = item.eventName || item.payload?.agentEvent?.type;
    const payload = item.payload || {};
    if (isDuplicateTurnEvent(turnId, payload)) continue;
    if (eventName === "goal_state") {
      planCard = updateExecutionPlanCard(planCard, notice, payload.goal, { live: true });
    } else if (eventName === "tool_call") {
      insertToolCard(createToolCard({
        tool: payload.tool, args: payload.args, phase: "call", callId: payload.callId,
      }), notice);
    } else if (eventName === "tool_result") {
      collectCitationsFromToolResult(payload.result, state.liveCitationMap || state.citationMap);
      insertToolCard(createToolCard({
        tool: payload.tool,
        result: payload.result,
        approvalId: payload.approvalId,
        phase: "result",
        callId: payload.callId || payload.result?.callId,
      }), notice);
    } else if (eventName === "assistant_note") {
      const note = addMessage("assistant", payload.content || "", { progress: true });
      note?.closest(".message") && messages.insertBefore(note.closest(".message"), notice);
    } else if (["compaction_started", "compaction", "compaction_failed"].includes(eventName)) {
      compactionCard = updateCompactionConversationCard(compactionCard, compactionAnchor(assistantText, notice), eventName, payload);
    } else if (eventName === "assistant_delta") {
      if (!assistantText) {
        assistantText = addMessage("assistant", "");
        assistantText?.classList.add("is-streaming");
        assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
      }
      assistantRaw += payload.delta || "";
      assistantText.dataset.raw = assistantRaw;
      renderMessageContent(assistantText, assistantRaw);
    } else if (eventName === "assistant_final") {
      if (payload.content) {
        if (!assistantText) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
        }
        assistantRaw = finalDisplayText(payload.content);
        assistantText.dataset.raw = assistantRaw;
        renderMessageContent(assistantText, assistantRaw);
      }
      assistantText?.classList.remove("is-streaming");
    } else if (eventName === "assistant_reset") {
      // The streamed prefix can briefly contain the opening brace of an
      // embedded Tool JSON object.  Clean that protocol framing before the
      // segment is frozen as an intermediate message (also on replay).
      const cleaned = stripProtocolTail(assistantRaw);
      if (assistantText && cleaned !== assistantRaw) {
        assistantRaw = cleaned;
        assistantText.dataset.raw = assistantRaw;
        renderMessageContent(assistantText, assistantRaw);
      }
      assistantText?.classList.remove("is-streaming");
      assistantText?.classList.add("is-intermediate");
      assistantText?.closest(".message")?.classList.add("is-intermediate");
      assistantRaw = "";
      assistantText = null;
    } else if (eventName === "done") {
      donePayload = payload;
      planCard = settleExecutionPlanCard(planCard, payload);
    }
  }
  if (donePayload) {
    if (donePayload.reply) {
      const finalText = finalDisplayText(donePayload.reply);
      if (!assistantText) {
        assistantText = addMessage("assistant", "");
        assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
      }
      assistantText.dataset.raw = finalText;
      renderMessageContent(assistantText, finalText);
    }
    finishStreamingMessages();
    notice.textContent = donePayload.status === "approval_required"
      ? "上一轮已恢复，正在等待审批"
      : "上一轮已从持久化事件恢复并完成";
    if (donePayload.status === "approval_required") await loadApprovals();
    if (donePayload.status !== "approval_required") {
      window.setTimeout(() => notice.remove(), 1800);
    }
    state.busy = false;
    state.turnId = null;
    state.activeTurn = null;
    state.cancelRequestedTurnId = null;
    sendButton.disabled = false;
    sendButton.hidden = false;
    input.disabled = false;
    resetStopButton();
    return true;
  }
  const checkpoint = await api(`/api/turns/${encodeURIComponent(turnId)}`).catch(() => null);
  if (checkpoint?.status === "interrupted") {
    notice.textContent = "上一轮在 Gateway 重启时中断，可点击重答继续";
    finishStreamingMessages();
    state.busy = false;
    state.turnId = null;
    state.activeTurn = null;
    sendButton.disabled = false;
    sendButton.hidden = false;
    input.disabled = false;
    resetStopButton();
    return true;
  }
  return false;
}

async function reconnectActiveTurn(activeTurn, notice) {
  if (!activeTurn?.turnId) return;
  if (state.reconnectingTurnId === activeTurn.turnId) return;
  stopActiveTurnReconnect();
  const controller = new AbortController();
  state.reconnectingTurnId = activeTurn.turnId;
  state.reconnectAbortController = controller;
  let assistantText = null;
  let assistantRaw = "";
  let compactionCard = null;
  let planCard = null;
  try {
    const after = Number(activeTurn.lastSeq || 0);
    const response = await fetch(
      `/api/turns/${encodeURIComponent(activeTurn.turnId)}/stream?after=${encodeURIComponent(after)}`,
      { signal: controller.signal },
    );
    if (!response.ok) {
      if (response.status === 404) {
        if (await replayPersistedTurn(activeTurn, notice)) return;
        await loadSessions(state.currentSessionId);
        return;
      }
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `重连 Turn 失败 (${response.status})`);
    }
    await consumeSse(response, async (eventName, payload) => {
      if (eventName === "turn_started") return;
      if (eventName === "status") {
        notice.textContent = `${payload.message || "SJTUClaw 正在处理上一轮请求"} · 已接回`;
      } else if (eventName === "goal_state") {
        planCard = updateExecutionPlanCard(planCard, notice, payload.goal, { live: true });
      } else if (eventName === "assistant_note") {
        const note = addMessage("assistant", payload.content || "", { progress: true });
        note?.closest(".message") && messages.insertBefore(note.closest(".message"), notice);
      } else if (eventName === "tool_call") {
        insertToolCard(createToolCard({
          tool: payload.tool, args: payload.args, phase: "call", callId: payload.callId,
        }), notice);
      } else if (eventName === "tool_result") {
        collectCitationsFromToolResult(payload.result, state.liveCitationMap || state.citationMap);
        insertToolCard(createToolCard({
          tool: payload.tool,
          result: payload.result,
          approvalId: payload.approvalId,
          phase: "result",
          callId: payload.callId || payload.result?.callId,
        }), notice);
      } else if (["compaction_started", "compaction", "compaction_failed"].includes(eventName)) {
        compactionCard = updateCompactionConversationCard(compactionCard, compactionAnchor(assistantText, notice), eventName, payload);
      } else if (eventName === "approval_required") {
        notice.textContent = `等待审批 · ${payload.approval?.tool || "工具"}`;
        await loadApprovals();
      } else if (eventName === "assistant_delta") {
        if (!assistantText) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantText?.classList.add("is-streaming");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
        }
        assistantRaw += payload.delta || "";
        assistantText.dataset.raw = assistantRaw;
        renderMessageContent(assistantText, assistantRaw);
      } else if (eventName === "assistant_final") {
        if (payload.content) {
          if (!assistantText) {
            assistantText = addMessage("assistant", "");
            assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
          }
          assistantRaw = finalDisplayText(payload.content);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        assistantText?.classList.remove("is-streaming");
        finishStreamingMessages();
      } else if (eventName === "assistant_reset") {
        const cleaned = stripProtocolTail(assistantRaw);
        if (assistantText && cleaned !== assistantRaw) {
          assistantRaw = cleaned;
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        // A reset means the previous streamed segment was provisional for a
        // following Tool call. Keep it in the conversation timeline instead
        // of deleting it; the next assistant segment will get its own card.
        assistantText?.classList.remove("is-streaming");
        assistantText?.classList.add("is-intermediate");
        assistantText?.closest(".message")?.classList.add("is-intermediate");
        assistantRaw = "";
        assistantText = null;
      } else if (eventName === "cancelled") {
        notice.textContent = "上一轮已取消";
        finishStreamingMessages();
      } else if (eventName === "error") {
        throw new Error(payload.message || "上一轮执行失败");
      } else if (eventName === "done") {
        assistantText?.classList.remove("is-streaming");
        finishStreamingMessages();
        if (payload.reply) {
          const finalText = finalDisplayText(payload.reply);
          if (!assistantText) {
            assistantText = addMessage("assistant", "");
            assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), notice);
          }
          assistantText.dataset.raw = finalText;
          renderMessageContent(assistantText, finalText);
        }
        await loadSessions(state.currentSessionId);
      }
      maybeScrollMessagesToBottom();
    }, { turnId: activeTurn.turnId });
  } catch (error) {
    if (error.name !== "AbortError") throw error;
  } finally {
    if (state.reconnectingTurnId === activeTurn.turnId) {
      state.reconnectingTurnId = null;
      state.reconnectAbortController = null;
    }
  }
}

function renderSessionMeta(session) {
  const meta = $("#session-meta");
  meta.replaceChildren();
  const id = document.createElement("span");
  const number = session.sessionId.match(/^session_(\d+)$/)?.[1];
  id.textContent = number ? `Session ${number}` : session.sessionId === "default" ? "默认会话" : session.sessionId;
  const count = document.createElement("span");
  count.id = "session-visible-count";
  const visible = session.messageCount ?? session.messages.length;
  count.textContent = session.summary
    ? `${visible} 条可见消息`
    : `${visible} 条消息`;
  if (session.pendingUserCount > 0) {
    count.title = `${session.pendingUserCount} 条用户消息尚未得到最终回复，可能是暂停、失败或仍在处理中。`;
  }
  if (session.summary) {
    count.title = "较早的消息已被压缩进当前 Session 摘要，用于继续提供上下文。";
  }
  const summary = document.createElement("span");
  summary.textContent = "已压缩历史";
  summary.title = session.summary || "";
  const workspace = document.createElement("span");
  workspace.className = "workspace-chip";
  workspace.dataset.sessionId = session.sessionId;
  workspace.role = "button";
  workspace.tabIndex = 0;
  if (session.workspace) {
    workspace.textContent = `Workspace · ${session.workspace}`;
    workspace.title = session.workspace;
  } else {
    workspace.textContent = "Workspace · 未设置";
    workspace.title = "点击设置 Workspace";
  }
  workspace.onclick = () => pickWorkspace(workspace);
  workspace.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pickWorkspace(workspace);
    }
  };
  meta.append(id, count);
  if (session.summary) meta.append(summary);
  if (session.activeTurn) {
    const running = document.createElement("span");
    running.className = "active-turn-chip";
    running.textContent = "正在处理";
    running.title = session.activeTurn.message || "SJTUClaw 正在后台处理上一轮请求";
    meta.append(running);
  }
  if (false && session.goalState?.objective) {
    const goal = document.createElement("span");
    goal.className = "goal-chip";
    const statusLabels = {
      active: "进行中",
      awaiting_approval: "待审批",
      completed: "已完成",
      blocked: "需处理",
    };
    goal.textContent = `目标 · ${statusLabels[session.goalState.status] || session.goalState.status || "进行中"}`;
    goal.title = `${session.goalState.objective}\n当前步骤：${session.goalState.currentStep || "继续处理"}`;
    meta.append(goal);
  }
  meta.append(workspace);
}

async function refreshWorkspaceStatus(sessionId) {
  const status = await api(`/api/sessions/${encodeURIComponent(sessionId)}/workspace`);
  if (state.currentSessionId !== sessionId) return;
  const workspace = document.querySelector(`.workspace-chip[data-session-id="${CSS.escape(sessionId)}"]`);
  if (!workspace || !status.workspace || status.exists) return;
  workspace.classList.add("invalid");
  workspace.textContent = "Workspace · 路径失效";
  workspace.title = `当前路径不可用：${status.workspace}。点击后可迁移到当前项目目录或重新选择。`;
}

function updateSessionVisibleCount(session) {
  const count = $("#session-visible-count");
  if (!count) return;
  // Progress notes are transient UI trace entries, not conversation
  // messages.  Excluding them keeps the live count consistent with the
  // semantic count returned by the Gateway (and avoids 15+15 becoming 31).
  const visible = messages.querySelectorAll(
    ".message:not(.progress):not(.is-intermediate)",
  ).length;
  count.textContent = session.summary
    ? `${visible} 条可见消息`
    : `${visible} 条消息`;
  if (session.pendingUserCount > 0) {
    count.title = `${session.pendingUserCount} 条用户消息尚未得到最终回复，可能是暂停、失败或仍在处理中。`;
  }
}

function renderDownloads(trace) {
  if (state.downloadExpiryTimer) {
    clearTimeout(state.downloadExpiryTimer);
    state.downloadExpiryTimer = null;
  }
  const now = Date.now();
  const items = trace
    .map((event) => event.result?.output)
    .filter((output) => {
      if (!output?.downloadUrl || !output?.downloadId || !output?.expiresAt) return false;
      const expiresAt = Date.parse(output.expiresAt);
      return Number.isFinite(expiresAt) && expiresAt > now;
    });
  const strip = $("#download-strip");
  const list = $("#download-list");
  list.replaceChildren();
  strip.hidden = items.length === 0;
  for (const item of items) {
    const link = document.createElement("a");
    link.className = "attachment-pill download-link";
    link.href = item.downloadUrl;
    link.textContent = `下载 ${item.filename || item.downloadId}`;
    link.title = `有效期至 ${new Date(item.expiresAt).toLocaleString()}`;
    list.append(link);
  }
  if (items.length) {
    const nextExpiry = Math.min(...items.map((item) => Date.parse(item.expiresAt)));
    const sessionId = state.currentSessionId;
    state.downloadExpiryTimer = setTimeout(() => {
      if (state.currentSessionId === sessionId) renderDownloads(trace);
    }, Math.max(50, Math.min(nextExpiry - Date.now() + 25, 2_147_000_000)));
  }
}

async function exportCurrentSession() {
  if (!state.currentSessionId) return;
  const confirmed = window.confirm("确定要导出当前会话为 Markdown 文件吗？");
  if (!confirmed) return;
  const session = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}`);
  const lines = [
    `# ${session.title || session.sessionId}`,
    "",
    `- Session: ${session.sessionId}`,
    `- 导出时间: ${new Date().toLocaleString()}`,
  ];
  if (session.workspace) lines.push(`- Workspace: ${session.workspace}`);
  lines.push("");
  for (const item of session.messages || []) {
    const exported = exportableMessageContent(item.role, item.content || "");
    if (!exported) continue;
    const source = item.metadata?.source && item.metadata.source !== "web" ? ` · ${sourceLabel(item.metadata.source)}` : "";
    lines.push(`## ${item.role === "user" ? "你" : "SJTUClaw"}${source}`);
    lines.push("");
    lines.push(exported);
    lines.push("");
  }
  const blob = new Blob([lines.join("\n").replace(/\n{3,}/g, "\n\n")], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${safeFilename(session.title || session.sessionId)}.md`;
  document.body.append(link);
  link.click();
  URL.revokeObjectURL(link.href);
  link.remove();
}

function exportableMessageContent(role, content) {
  const protocol = parseProtocolMessage(content);
  if (protocol?.type === "final") return protocol.content || "";
  if (protocol) return "";
  if (role === "assistant" && content.startsWith("[deferred_action_promise]")) {
    return content.replace("[deferred_action_promise]", "").trim();
  }
  if (role === "user" && content.includes("\n\n[quoted_message] ")) {
    content = content.split("\n\n[quoted_message] ", 1)[0];
  }
  if (role === "user" && content.includes("\n\n[attached_files] ")) {
    return content.split("\n\n[attached_files] ", 1)[0].trim();
  }
  return content.trim();
}

function safeFilename(name) {
  return String(name || "session")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "session";
}

async function loadApprovals() {
  const area = $("#approval-area");
  if (!state.currentSessionId) { area.hidden = true; return; }
  const data = await api(`/api/approvals?sessionId=${encodeURIComponent(state.currentSessionId)}&status=pending`);
  area.replaceChildren();
  area.hidden = data.approvals.length === 0;
  for (const approval of data.approvals) {
    const card = document.createElement("article");
    card.className = "approval-card";
    card.dataset.approvalId = approval.approvalId;
    const title = document.createElement("strong");
    title.textContent = `需要审批 · ${approval.tool}`;
    const args = document.createElement("pre");
    args.textContent = JSON.stringify(approval.args, null, 2);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const approve = document.createElement("button");
    approve.className = "approval-approve";
    approve.textContent = "批准执行";
    approve.onclick = () => decideApproval(approval, true, null, approve);
    const reject = document.createElement("button");
    reject.className = "approval-reject";
    reject.textContent = "拒绝";
    reject.onclick = () => {
      const reason = window.prompt("拒绝原因（会反馈给 SJTUClaw）", "暂不执行");
      if (reason !== null) decideApproval(approval, false, reason, reject);
    };
    actions.append(approve, reject);
    card.append(title, args, actions);
    area.append(card);
  }
}

async function decideApproval(approval, approved, reason = null, trigger = null) {
  // An approval decision owns a full Agent turn.  Keep the normal session
  // poller and active-turn recovery disabled until its SSE stream has
  // reached `done`; otherwise a poll can call loadSession() mid-decision and
  // open a second recovery stream for the same turn.  That produces duplicate
  // assistant deltas, stale approval cards and a caret that never settles.
  if (state.busy) return;
  state.busy = true;
  clearError();
  const card = trigger?.closest(".approval-card");
  const buttons = card ? [...card.querySelectorAll("button")] : [];
  buttons.forEach((button) => { button.disabled = true; });
  if (trigger) trigger.textContent = approved ? "正在执行并生成结果…" : "正在拒绝…";
  // The approval has already been decided at this point.  Remove the
  // top-level pending card immediately instead of keeping a disabled card
  // visible while the approved tool and the resumed Agent loop run.  If the
  // request fails and the approval is still pending, the catch path reloads
  // it from the durable ApprovalStore.
  if (card) {
    const area = card.parentElement;
    card.remove();
    if (area?.id === "approval-area" && !area.children.length) area.hidden = true;
  }
  const waiting = document.createElement("div");
  waiting.className = "tool-card";
  waiting.textContent = approved ? "正在执行已批准的工具…" : "正在处理拒绝结果…";
  messages.append(waiting);
  messages.scrollTop = messages.scrollHeight;
  let assistantText = null;
  let assistantRaw = "";
  let compactionCard = null;
  let planCard = null;
  let donePayload = null;
  let recoveredApprovalTurn = null;
  const approvalId = typeof approval === "string" ? approval : approval.approvalId;
  // Resume the original logical Turn. A browser-only id would break
  // cancellation and durable replay after refresh.
  let turnId = (typeof approval === "object" && approval?.turnId)
    || `turn_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
  state.turnId = turnId;
  state.liveCitationMap = newCitationMap(state.citationMap);
  const turnAbortController = new AbortController();
  state.turnAbortController = turnAbortController;
  state.activeTurn = null;
  $("#stop-button").hidden = false;
  sendButton.disabled = true;
  input.disabled = true;
  try {
    const response = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/decision/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, reason, turnId }),
      signal: turnAbortController.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail;
      if (response.status === 409 && detail?.code === "approval_in_progress" && detail.activeTurn) {
        const conflict = new Error(detail.message || "Approval is already running" );
        conflict.activeTurn = detail.activeTurn;
        throw conflict;
      }
      throw new Error(typeof detail === "string" ? detail : (detail?.message || body.error || `Approval request failed (${response.status})`));
    }
    const authoritativeTurnId = response.headers.get("X-SJTUClaw-Turn-Id");
    if (authoritativeTurnId) {
      turnId = authoritativeTurnId;
      state.turnId = authoritativeTurnId;
    }
    await consumeSse(response, async (eventName, payload) => {
      if (eventName === "turn_started" && payload.turnId) {
        turnId = payload.turnId;
        state.turnId = payload.turnId;
      } else if (eventName === "status") {
        waiting.textContent = payload.message || "SJTUClaw 正在继续任务…";
      } else if (eventName === "goal_state") {
        planCard = updateExecutionPlanCard(planCard, waiting, payload.goal, { live: true });
      } else if (eventName === "assistant_note") {
        const note = addMessage("assistant", payload.content || "", { progress: true });
        note?.closest(".message") && messages.insertBefore(note.closest(".message"), waiting);
      } else if (eventName === "tool_call") {
        insertToolCard(createToolCard({
          tool: payload.tool, args: payload.args, phase: "call", callId: payload.callId,
        }), waiting);
      } else if (eventName === "tool_result") {
        collectCitationsFromToolResult(payload.result, state.liveCitationMap || state.citationMap);
        insertToolCard(createToolCard({
          tool: payload.tool,
          result: payload.result,
          approvalId: payload.approvalId,
          phase: "result",
          callId: payload.callId || payload.result?.callId,
        }), waiting);
      } else if (["compaction_started", "compaction", "compaction_failed"].includes(eventName)) {
        compactionCard = updateCompactionConversationCard(compactionCard, compactionAnchor(assistantText, waiting), eventName, payload);
      } else if (eventName === "approval_required") {
        waiting.textContent = `等待新的审批 · ${payload.approval.tool}`;
      } else if (eventName === "assistant_delta") {
        if (!assistantText) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantText?.classList.add("is-streaming");
        }
        assistantRaw += payload.delta || "";
        assistantText.dataset.raw = assistantRaw;
        renderMessageContent(assistantText, assistantRaw);
      } else if (eventName === "assistant_final") {
        if (payload.content) {
          if (!assistantText) {
            assistantText = addMessage("assistant", "");
            assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          }
          assistantRaw = finalDisplayText(payload.content);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        assistantText?.classList.remove("is-streaming");
        finishStreamingMessages();
      } else if (eventName === "assistant_reset") {
        const cleaned = stripProtocolTail(assistantRaw);
        if (assistantText && cleaned !== assistantRaw) {
          assistantRaw = cleaned;
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        assistantText?.classList.remove("is-streaming");
        assistantText?.classList.add("is-intermediate");
        assistantText?.closest(".message")?.classList.add("is-intermediate");
        assistantRaw = "";
        assistantText = null;
      } else if (eventName === "error") {
        throw new Error(payload.message || "审批后的 Agent 执行失败");
      } else if (eventName === "done") {
        assistantText?.classList.remove("is-streaming");
        finishStreamingMessages();
        donePayload = payload;
        planCard = settleExecutionPlanCard(planCard, payload);
        if (assistantText && payload.reply) {
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        } else if (payload.reply) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        }
      }
      maybeScrollMessagesToBottom();
    }, { turnId });
    await loadSessions(state.currentSessionId);
    await loadApprovals();
    if (donePayload?.status === "approval_required") {
      maybeScrollMessagesToBottom();
    }
  } catch (error) {
    if (error.activeTurn) {
      recoveredApprovalTurn = error.activeTurn;
    } else if (error.name !== "AbortError") {
      // The Gateway may finish the approved turn just before an SSE
      // connection drops (or after a handler reports a real tool failure).
      // Reconcile from durable Session/Approval state so the old disabled
      // approval card and partial assistant bubble cannot remain on screen.
      await loadSession(state.currentSessionId).catch(() => {});
      await loadApprovals().catch(() => {});
      showError(error);
    }
    buttons.forEach((button) => { button.disabled = false; });
    if (trigger) trigger.textContent = approved ? "批准执行" : "拒绝";
  } finally {
    waiting.remove();
    finishStreamingMessages();
    if (state.cancelRequestedTurnId === turnId) {
      state.turnAbortController = null;
    } else {
      state.turnId = null;
      state.activeTurn = null;
      state.turnAbortController = null;
      resetStopButton();
      sendButton.disabled = false;
      input.disabled = false;
    }
    state.busy = false;
  }
  if (recoveredApprovalTurn) {
    syncRecoveredActiveTurn(recoveredApprovalTurn);
  }
}

function renderDraftAttachments() {
  const strip = $("#attachment-strip");
  const list = $("#attachment-list");
  const mode = $("#attachment-mode");
  const actions = $("#attachment-actions");
  list.replaceChildren();
  const files = state.pendingDraftFiles || [];
  strip.hidden = files.length === 0;
  mode.hidden = true;
  mode.textContent = "";
  actions.hidden = files.length <= 1;
  files.forEach((file, index) => {
    const entry = document.createElement("span");
    entry.className = "attachment-entry";
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "attachment-pill";
    const selected = state.pendingDraftSelected.has(index);
    pill.classList.toggle("pending", selected);
    pill.textContent = `${file.name} · ${formatBytes(file.size)}`;
    pill.title = selected ? "本轮将上传并读取；点击取消选择" : "点击选择本轮要上传的附件";
    pill.setAttribute("aria-pressed", String(selected));
    pill.onclick = () => {
      if (selected) state.pendingDraftSelected.delete(index);
      else state.pendingDraftSelected.add(index);
      renderDraftAttachments();
    };
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.textContent = "×";
    remove.title = `移除附件 ${file.name}`;
    remove.setAttribute("aria-label", `移除附件 ${file.name}`);
    remove.onclick = () => {
      state.pendingDraftFiles.splice(index, 1);
      state.pendingDraftSelected = new Set(
        [...state.pendingDraftSelected]
          .filter((item) => item !== index)
          .map((item) => item > index ? item - 1 : item),
      );
      renderDraftAttachments();
    };
    entry.append(pill, remove);
    list.append(entry);
  });
  $("#attachment-select-all").onclick = () => {
    state.pendingDraftSelected = new Set(files.map((_, index) => index));
    renderDraftAttachments();
  };
  $("#attachment-clear-selection").onclick = () => {
    state.pendingDraftSelected.clear();
    renderDraftAttachments();
  };
  renderComposerAttachmentPreview();
}

function renderAttachments(items) {
  if (state.isDraft) {
    renderDraftAttachments();
    return;
  }
  const strip = $("#attachment-strip");
  const list = $("#attachment-list");
  const mode = $("#attachment-mode");
  const actions = $("#attachment-actions");
  list.replaceChildren();
  strip.hidden = items.length === 0;
  if (items.length === 0) {
    mode.textContent = "";
    mode.hidden = true;
    actions.hidden = true;
    renderComposerAttachmentPreview();
    return;
  }
  const availableItems = items.filter((item) => item.available !== false);
  const selectedCount = state.pendingAttachments.filter((pending) =>
    availableItems.some((item) => item.attachmentId === pending.attachmentId)
  ).length;
  mode.textContent = selectedCount ? `本轮将读取 ${selectedCount} 个` : "";
  mode.hidden = selectedCount === 0;
  mode.classList.toggle("active", selectedCount > 0);
  actions.hidden = availableItems.length <= 1;
  for (const item of items) {
    const entry = document.createElement("span");
    entry.className = "attachment-entry";
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "attachment-pill";
    if (item.available === false) pill.classList.add("missing");
    const selected = state.pendingAttachments.some((pending) => pending.attachmentId === item.attachmentId);
    if (selected) {
      pill.classList.add("pending");
      pill.title = "本轮新上传 · " + item.contentType;
    }
    pill.textContent = `${item.filename} · ${formatBytes(item.size)}`;
    pill.title = item.available === false
      ? "附件文件已丢失，可删除记录后重新上传"
      : `${selected ? "本轮必须读取（点击取消）" : "可按需使用（点击设为必须读取）"} · ${item.contentType}`;
    pill.disabled = item.available === false;
    pill.setAttribute("aria-pressed", String(selected));
    pill.onclick = () => {
      const index = state.pendingAttachments.findIndex((pending) => pending.attachmentId === item.attachmentId);
      if (index >= 0) state.pendingAttachments.splice(index, 1);
      else state.pendingAttachments.push(item);
      state.pendingAttachmentSession = state.pendingAttachments.length ? state.currentSessionId : null;
      renderAttachments(items);
    };
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "attachment-preview-button";
    preview.textContent = "⌕";
    preview.title = `预览附件 ${item.filename}`;
    preview.setAttribute("aria-label", `预览附件 ${item.filename}`);
    preview.disabled = item.available === false;
    preview.onclick = () => openAttachmentPreview(item);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.textContent = "×";
    remove.title = `删除附件 ${item.filename}`;
    remove.setAttribute("aria-label", `删除附件 ${item.filename}`);
    remove.onclick = () => deleteAttachment(item, items);
    entry.append(pill, preview, remove);
    list.append(entry);
  }
  $("#attachment-select-all").onclick = () => {
    state.pendingAttachments = [...availableItems];
    state.pendingAttachmentSession = state.pendingAttachments.length ? state.currentSessionId : null;
    renderAttachments(items);
  };
  $("#attachment-clear-selection").onclick = () => {
    state.pendingAttachments = [];
    state.pendingAttachmentSession = null;
    renderAttachments(items);
  };
  renderComposerAttachmentPreview();
}

let composerPreviewObjectUrls = [];

function clearComposerPreviewObjectUrls() {
  composerPreviewObjectUrls.forEach((url) => URL.revokeObjectURL(url));
  composerPreviewObjectUrls = [];
}

function attachmentExtension(name = "") {
  const match = String(name).match(/\.([^.]+)$/);
  return match ? match[1].slice(0, 5).toUpperCase() : "FILE";
}

function renderComposerAttachmentPreview() {
  const tray = $("#composer-attachment-preview");
  if (!tray) return;
  clearComposerPreviewObjectUrls();
  tray.replaceChildren();

  const selected = state.isDraft
    ? [...state.pendingDraftSelected]
      .sort((a, b) => a - b)
      .map((index) => ({
        kind: "draft",
        index,
        file: state.pendingDraftFiles[index],
      }))
      .filter((item) => item.file)
    : state.pendingAttachments.map((attachment) => ({
      kind: "stored",
      attachment,
    }));

  tray.hidden = selected.length === 0;
  selected.forEach((item) => {
    const file = item.file;
    const attachment = item.attachment;
    const filename = file?.name || attachment?.filename || "附件";
    const contentType = file?.type || attachment?.contentType || "";
    const card = document.createElement("article");
    card.className = "composer-attachment-card";
    card.title = filename;

    const visual = document.createElement("button");
    visual.type = "button";
    visual.className = "composer-attachment-visual";
    visual.setAttribute("aria-label", `预览附件 ${filename}`);
    if (contentType.startsWith("image/")) {
      const image = document.createElement("img");
      if (file) {
        image.src = URL.createObjectURL(file);
        composerPreviewObjectUrls.push(image.src);
      } else {
        image.src = `/api/sessions/${encodeURIComponent(state.currentSessionId)}/attachments/${encodeURIComponent(attachment.attachmentId)}/raw`;
      }
      image.alt = filename;
      visual.append(image);
    } else {
      const type = document.createElement("span");
      type.className = "composer-attachment-type";
      type.textContent = attachmentExtension(filename);
      visual.append(type);
    }
    if (attachment) visual.onclick = () => openAttachmentPreview(attachment);
    else visual.disabled = !contentType.startsWith("image/");

    const label = document.createElement("span");
    label.className = "composer-attachment-label";
    label.textContent = filename;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "composer-attachment-clear";
    remove.textContent = "×";
    remove.title = `取消本轮附件 ${filename}`;
    remove.setAttribute("aria-label", `取消本轮附件 ${filename}`);
    remove.onclick = async () => {
      if (item.kind === "draft") {
        state.pendingDraftSelected.delete(item.index);
        renderDraftAttachments();
        return;
      }
      state.pendingAttachments = state.pendingAttachments.filter(
        (pending) => pending.attachmentId !== attachment.attachmentId,
      );
      if (!state.pendingAttachments.length) state.pendingAttachmentSession = null;
      renderComposerAttachmentPreview();
      try {
        const session = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}`);
        renderAttachments(session.attachments || []);
      } catch (error) {
        showError(error);
      }
    };

    card.append(visual, label, remove);
    tray.append(card);
  });
}

async function openAttachmentPreview(item) {
  const dialog = $("#attachment-preview-dialog");
  const body = $("#attachment-preview-body");
  body.classList.remove("pdf-preview-mode");
  body.scrollTop = 0;
  $("#attachment-preview-title").textContent = item.filename;
  $("#attachment-preview-download").removeAttribute("href");
  body.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "attachment-preview-message";
  loading.textContent = "正在准备预览…";
  body.append(loading);
  if (!dialog.open) dialog.showModal();
  try {
    const base = `/api/sessions/${encodeURIComponent(state.currentSessionId)}/attachments/${encodeURIComponent(item.attachmentId)}`;
    const data = await api(`${base}/preview`);
    $("#attachment-preview-download").href = data.downloadUrl;
    body.replaceChildren();
    if (data.kind === "image") {
      const image = document.createElement("img");
      image.src = data.rawUrl;
      image.alt = data.filename;
      body.append(image);
    } else if (data.kind === "pdf") {
      body.classList.add("pdf-preview-mode");
      const pages = await api(`${base}/pdf-pages`);
      const container = document.createElement("div");
      container.className = "pdf-preview-pages";
      for (const page of pages.pages || []) {
        const figure = document.createElement("figure");
        figure.className = "pdf-preview-page";
        const caption = document.createElement("figcaption");
        caption.textContent = `第 ${page.page} 页`;
        const image = document.createElement("img");
        image.src = page.src;
        image.alt = `${data.filename} 第 ${page.page} 页`;
        figure.append(caption, image);
        container.append(figure);
      }
      if (pages.truncated) {
        const note = document.createElement("p");
        note.className = "attachment-preview-message";
        note.textContent = `仅预览前 ${pages.renderedPages} 页，共 ${pages.pageCount} 页；可下载原文件查看完整内容。`;
        container.append(note);
      }
      body.append(container);
      body.scrollTop = 0;
    } else if (data.kind === "audio" || data.kind === "video") {
      const media = document.createElement(data.kind);
      media.src = data.rawUrl;
      media.controls = true;
      body.append(media);
    } else if (data.kind === "text" || data.kind === "document") {
      const pre = document.createElement("pre");
      pre.textContent = data.content || "（没有可预览的文字）";
      body.append(pre);
      if (data.truncated) {
        const note = document.createElement("p");
        note.className = "attachment-preview-message";
        note.textContent = "内容较长，预览已截断；可下载原文件查看完整内容。";
        body.append(note);
      }
    } else {
      const message = document.createElement("p");
      message.className = "attachment-preview-message";
      message.textContent = "该格式暂不支持在线预览，可以下载原文件后打开。";
      body.append(message);
    }
  } catch (error) {
    body.replaceChildren();
    const message = document.createElement("p");
    message.className = "attachment-preview-message error";
    message.textContent = error.message || String(error);
    body.append(message);
  }
}

$("#attachment-preview-close").onclick = () => $("#attachment-preview-dialog").close();
$("#attachment-preview-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

async function deleteAttachment(item) {
  if (state.busy) return;
  const confirmed = window.confirm(
    `确定删除附件“${item.filename}”吗？\n\n这会删除当前 Session 中的附件记录和 SJTUClaw 保存的上传副本，但不会删除 Workspace 文件或你电脑上的原文件。`
  );
  if (!confirmed) return;
  try {
    await api(
      `/api/sessions/${encodeURIComponent(state.currentSessionId)}/attachments/${encodeURIComponent(item.attachmentId)}`,
      { method: "DELETE" },
    );
    state.pendingAttachments = state.pendingAttachments.filter(
      (pending) => pending.attachmentId !== item.attachmentId
    );
    if (!state.pendingAttachments.length) state.pendingAttachmentSession = null;
    await loadSession(state.currentSessionId);
  } catch (error) { showError(error); }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const FILE_WORK_TOOLS = new Set([
  "create_file", "overwrite_file", "edit_file", "copy_file",
  "copy_attachment_to_workspace", "run_command", "use_skill",
]);

function expectsLongTurn(text = "", options = {}) {
  if (options.skillName) return true;
  const normalized = String(text || "").replace(/\s+/g, " ");
  return /(?:写|生成|制作|创建|整理|改写|完成|导出|保存|编译|实现|修改).{0,16}(?:长文|文件|文档|报告|论文|答案|代码|项目|网页|PPT|幻灯片|程序)|(?:完整|详细|逐题|全部).{0,12}(?:分析|回答|实现|内容)|(?:course-report|presentation|material-summary|document-reader)/i.test(normalized);
}

function createWaitingIndicator(initialLabel, options = {}) {
  const element = document.createElement("div");
  element.className = "tool-card turn-waiting";
  element.setAttribute("role", "status");
  element.setAttribute("aria-live", "polite");
  const pulse = document.createElement("span");
  pulse.className = "turn-waiting-pulse";
  pulse.setAttribute("aria-hidden", "true");
  const copy = document.createElement("span");
  copy.className = "turn-waiting-copy";
  const headline = document.createElement("strong");
  headline.className = "turn-waiting-headline";
  const detail = document.createElement("span");
  detail.className = "turn-waiting-detail";
  copy.append(headline, detail);
  element.append(pulse, copy);
  // Use wall-clock time instead of incrementing a counter. Browsers throttle
  // timers in background tabs, but the elapsed time must still include that
  // period and catch up immediately when the page becomes visible again.
  let startedAt = Date.now();
  let label = initialLabel;
  let pausedAt = null;
  let pausedDuration = 0;
  let phase = options.phase || "starting";
  let longWork = Boolean(options.longWork);

  const explanatoryText = (seconds) => {
    if (pausedAt !== null) return "";
    if (["approval_required", "cancelled", "completed", "compaction"].includes(phase)) return "";
    if (phase === "file_work") {
      return "正在执行文件操作；长文件写入和校验可能需要一些时间。";
    }
    if (seconds < 12) return "";
    if (seconds < 30) return "连接正常，模型仍在组织内容。";
    if (longWork) {
      return seconds < 60
        ? "正在准备较长内容，生成完毕后会继续写入或调用工具。"
        : "仍在处理长内容；这类生成与写入通常比普通回复更久，并非页面卡死。";
    }
    return seconds < 60
      ? "复杂请求可能需要多轮规划，当前仍在处理中。"
      : "连接仍然正常；模型正在处理较复杂的内容，可继续等待或点击停止。";
  };

  const render = () => {
    if (document.hidden) {
      headline.textContent = `${label} · 正在后台处理`;
      detail.textContent = "返回页面后会继续显示最新阶段。";
      detail.hidden = false;
      return;
    }
    const now = Date.now();
    const paused = pausedAt === null ? 0 : Math.max(0, now - pausedAt);
    const seconds = Math.max(0, Math.floor((now - startedAt - pausedDuration - paused) / 1000));
    headline.textContent = seconds > 0 ? `${label} · ${seconds}s` : label;
    const explanation = explanatoryText(seconds);
    detail.textContent = explanation;
    detail.hidden = !explanation;
  };
  const handleVisibility = () => {
    // setInterval may be heavily throttled in a background tab. A visibility
    // event gives us an immediate wall-clock recalculation on return.
    if (!document.hidden) render();
  };
  document.addEventListener("visibilitychange", handleVisibility);
  const timer = window.setInterval(render, 1000);
  render();

  return {
    element,
    setLabel(nextLabel, options = {}) {
      label = nextLabel;
      if (options.phase) phase = options.phase;
      if (options.longWork !== undefined) longWork = Boolean(options.longWork);
      const now = Date.now();
      if (options.resetClock) {
        startedAt = now;
        pausedAt = null;
        pausedDuration = 0;
      }
      if (options.resume && pausedAt !== null) {
        pausedDuration += Math.max(0, now - pausedAt);
        pausedAt = null;
      }
      if (options.pause) pausedAt ??= now;
      render();
    },
    pauseTimer() {
      pausedAt ??= Date.now();
      render();
    },
    resumeTimer(resetClock = false) {
      const now = Date.now();
      if (resetClock) {
        startedAt = now;
        pausedDuration = 0;
      } else if (pausedAt !== null) {
        pausedDuration += Math.max(0, now - pausedAt);
      }
      pausedAt = null;
      render();
    },
    destroy() {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    },
  };
}

async function sendMessage(text, displayText = text, options = {}) {
  if (!state.currentSessionId || state.busy) return;
  clearError();
  state.busy = true;
  sendButton.disabled = true;
  sendButton.hidden = true;
  $("#stop-button").hidden = false;
  input.disabled = true;
  state.turnId = `turn_${crypto.randomUUID().replaceAll("-", "")}`;
  const turnId = state.turnId;
  state.liveCitationMap = newCitationMap(state.citationMap);
  const turnAbortController = new AbortController();
  state.turnAbortController = turnAbortController;
  state.activeTurn = null;
  messages.querySelector(".empty-state")?.remove();
  addMessage("user", displayText, {
    __selectedAttachments: options.selectedAttachments || [],
  });
  // Once the user turn is visibly accepted, the selected attachments belong
  // to that immutable message. Clear the composer immediately instead of
  // keeping stale chips around until the whole Agent turn has finished.
  if (typeof options.onSubmitted === "function") options.onSubmitted();
  renderMessageIndex();
  maybeScrollMessagesToBottom(true);
  const waitingIndicator = createWaitingIndicator("SJTUClaw 正在思考…", {
    longWork: expectsLongTurn(text, options),
  });
  const waiting = waitingIndicator.element;
  const setWaiting = (label, waitingOptions = {}) => waitingIndicator.setLabel(label, waitingOptions);
  messages.append(waiting);
  maybeScrollMessagesToBottom(true);
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: state.currentSessionId,
        message: text,
        turnId,
        skillName: options.skillName || null,
      }),
      signal: turnAbortController.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    }
    let assistantText = null;
    let assistantRaw = "";
    let compactionCard = null;
    let planCard = null;
    let pendingCompactionEvents = [];
    let donePayload = null;
    const flushPendingCompaction = () => {
      if (!pendingCompactionEvents.length) return;
      for (const item of pendingCompactionEvents) {
        compactionCard = updateCompactionConversationCard(
          compactionCard, compactionAnchor(assistantText, waiting), item.eventName, item.payload,
        );
      }
      pendingCompactionEvents = [];
    };
    await consumeSse(response, async (eventName, payload) => {
      if (eventName === "status") {
        setWaiting(payload.message || "SJTUClaw 正在处理…", { phase: payload.phase });
      } else if (eventName === "goal_state") {
        planCard = updateExecutionPlanCard(planCard, waiting, payload.goal, { live: true });
      } else if (eventName === "assistant_note") {
        const note = addMessage("assistant", payload.content || "", { progress: true });
        note?.closest(".message") && messages.insertBefore(note.closest(".message"), waiting);
      } else if (eventName === "tool_call") {
        const isFileWork = FILE_WORK_TOOLS.has(payload.tool);
        setWaiting(
          isFileWork ? "正在执行文件操作…" : `正在调用工具 · ${payload.tool}`,
          { phase: isFileWork ? "file_work" : "tool_call", longWork: isFileWork || undefined },
        );
        const card = createToolCard({
          tool: payload.tool, args: payload.args, phase: "call", callId: payload.callId,
        });
        insertToolCard(card, waiting);
      } else if (eventName === "tool_result") {
        setWaiting("工具已返回，正在继续处理…", { phase: "tool_result" });
        collectCitationsFromToolResult(payload.result, state.liveCitationMap || state.citationMap);
        const card = createToolCard({
          tool: payload.tool,
          result: payload.result,
          approvalId: payload.approvalId,
          phase: "result",
          callId: payload.callId || payload.result?.callId,
        });
        insertToolCard(card, waiting);
      } else if (["compaction_started", "compaction", "compaction_failed"].includes(eventName)) {
        if (eventName === "compaction_started") {
          waitingIndicator.setLabel("正在整理上下文…", {
            phase: "compaction", resetClock: true, resume: true,
          });
        } else {
          waitingIndicator.setLabel(
            eventName === "compaction_failed" ? "上下文整理失败" : "上下文整理完成",
            { phase: "compaction", pause: true },
          );
        }
        // If compaction starts before the final answer event, keep its visual
        // card pending.  It is inserted only after the assistant bubble so a
        // summary never interrupts the current answer in the transcript.
        if (assistantText && !assistantText.classList.contains("is-streaming")) {
          compactionCard = updateCompactionConversationCard(compactionCard, compactionAnchor(assistantText, waiting), eventName, payload);
        } else {
          pendingCompactionEvents.push({ eventName, payload });
        }
      } else if (eventName === "approval_required") {
        setWaiting(`等待审批 · ${payload.approval.tool}`, { phase: "approval_required" });
      } else if (eventName === "assistant_delta") {
        if (!assistantText) {
          setWaiting("正在生成回答…", { phase: "model_stream" });
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantText?.classList.add("is-streaming");
        }
        const characters = Array.from(payload.delta || "");
        // Providers are allowed to place a long passage in one SSE delta. Split
        // that passage into a bounded number of visual frames so the UI remains
        // progressively readable instead of replacing an empty bubble at once.
        // Keep the UI progressive for an unusually large SSE delta, but do
        // not make the browser trail the model by 90 animation frames. Normal
        // provider-sized deltas are rendered immediately.
        const frameCount = Math.min(24, Math.max(1, Math.ceil(characters.length / 90)));
        const chunkSize = Math.max(1, Math.ceil(characters.length / frameCount));
        for (let index = 0; index < characters.length; index += chunkSize) {
          assistantRaw += characters.slice(index, index + chunkSize).join("");
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
          if (index + chunkSize < characters.length) {
            await new Promise((resolve) => requestAnimationFrame(resolve));
          }
        }
      } else if (eventName === "assistant_final") {
        if (payload.content) {
          if (!assistantText) {
            assistantText = addMessage("assistant", "");
            assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          }
          assistantRaw = finalDisplayText(payload.content);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        assistantText?.classList.remove("is-streaming");
        flushPendingCompaction();
        waitingIndicator.pauseTimer();
        finishStreamingMessages();
      } else if (eventName === "assistant_reset") {
        if (assistantText) {
          const cleaned = stripProtocolTail(assistantRaw);
          if (cleaned !== assistantRaw) {
            assistantRaw = cleaned;
            assistantText.dataset.raw = assistantRaw;
            renderMessageContent(assistantText, assistantRaw);
          }
          assistantText.classList.remove("is-streaming");
          assistantText.classList.add("is-intermediate");
          assistantText.closest(".message")?.classList.add("is-intermediate");
          assistantText = null;
        }
        assistantRaw = "";
      } else if (eventName === "metrics") {
        const first = payload.timeToFirstTokenMs ? ` · 首字 ${Math.round(payload.timeToFirstTokenMs)} ms` : "";
        setWaiting(`模型返回 · ${payload.totalTokens} tokens${first}`, { phase: "model_returned" });
      } else if (eventName === "cancelled") {
        setWaiting("本轮已取消", { phase: "cancelled" });
        finishStreamingMessages();
      } else if (eventName === "error") {
        throw new Error(payload.message || "流式请求失败");
      } else if (eventName === "done") {
        assistantText?.classList.remove("is-streaming");
        waitingIndicator.pauseTimer();
        finishStreamingMessages();
        donePayload = payload;
        planCard = settleExecutionPlanCard(planCard, payload);
        if (assistantText && payload.reply) {
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        } else if (payload.reply) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        }
        flushPendingCompaction();
      }
      maybeScrollMessagesToBottom();
    }, { turnId });
    await loadSessions(state.currentSessionId);
    if (donePayload?.status === "approval_required") await loadApprovals();
  } catch (error) {
    // Stop closes the local stream before the Runtime finishes its safe
    // cancellation checkpoint. Do not surface that intentional AbortError as
    // a failed chat request.
    if (error.name !== "AbortError") throw error;
  } finally {
    waitingIndicator.destroy();
    waiting.remove();
    finishStreamingMessages();
    if (state.cancelRequestedTurnId === turnId) {
      // requestTurnCancellation owns the final UI reset after the active Turn
      // disappears from the Gateway registry.
      state.turnAbortController = null;
    } else {
      state.busy = false;
      state.turnId = null;
      state.activeTurn = null;
      state.turnAbortController = null;
      sendButton.disabled = false;
      sendButton.hidden = false;
      resetStopButton();
      input.disabled = false;
      input.focus();
    }
  }
}

function findUserArticleByMessageIndex(messageIndex) {
  return [...messages.querySelectorAll(".message.user")]
    .find((article) => Number(article.dataset.sessionMessageIndex) === messageIndex) || null;
}

function findPreviousUserArticle(article) {
  let cursor = article?.previousElementSibling;
  while (cursor) {
    if (cursor.classList?.contains("message") && cursor.classList.contains("user")) return cursor;
    cursor = cursor.previousElementSibling;
  }
  return null;
}

function trimConversationAfter(article) {
  if (!article) return;
  let cursor = article.nextSibling;
  while (cursor) {
    const next = cursor.nextSibling;
    cursor.remove();
    cursor = next;
  }
}

function editAndReplayMessage(messageIndex, contentElement) {
  if (state.busy) return;
  const current = (contentElement?.dataset.raw || contentElement?.innerText || "").trim();
  const next = window.prompt("修改问题后重新发送", current);
  if (next === null) return;
  const normalized = next.trim();
  if (!normalized || normalized === current) return;
  replayMessage(messageIndex, normalized).catch(showError);
}

function regenerateFromAssistant(article) {
  if (state.busy) return;
  const userArticle = findPreviousUserArticle(article);
  const messageIndex = Number(userArticle?.dataset.sessionMessageIndex);
  if (!Number.isInteger(messageIndex)) {
    showError(new Error("找不到可重新回答的上一条问题。"));
    return;
  }
  replayMessage(messageIndex, null).catch(showError);
}

async function replayMessage(messageIndex, replacementMessage = null) {
  if (!state.currentSessionId || state.busy) return;
  clearError();
  const userArticle = findUserArticleByMessageIndex(messageIndex);
  if (replacementMessage !== null) {
    const content = userArticle?.querySelector(".message-content");
    if (content) {
      content.dataset.raw = replacementMessage;
      renderMessageContent(content, replacementMessage, null, { preserveLineBreaks: true });
    }
  }
  trimConversationAfter(userArticle);
  state.busy = true;
  sendButton.disabled = true;
  sendButton.hidden = true;
  $("#stop-button").hidden = false;
  input.disabled = true;
  state.turnId = `turn_${crypto.randomUUID().replaceAll("-", "")}`;
  const turnId = state.turnId;
  state.liveCitationMap = newCitationMap(state.citationMap);
  const turnAbortController = new AbortController();
  state.turnAbortController = turnAbortController;
  const replayText = replacementMessage
    || userArticle?.querySelector(".message-content")?.dataset.raw
    || userArticle?.querySelector(".message-content")?.innerText
    || "";
  const waitingIndicator = createWaitingIndicator("SJTUClaw 正在重新回答…", {
    longWork: expectsLongTurn(replayText),
  });
  const waiting = waitingIndicator.element;
  const setWaiting = (label, waitingOptions = {}) => waitingIndicator.setLabel(label, waitingOptions);
  messages.append(waiting);
  maybeScrollMessagesToBottom(true);
  try {
    const response = await fetch("/api/chat/replay/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: state.currentSessionId,
        messageIndex,
        message: replacementMessage,
        turnId,
        signal: turnAbortController.signal,
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    }
    let assistantText = null;
    let assistantRaw = "";
    let compactionCard = null;
    let planCard = null;
    let donePayload = null;
    await consumeSse(response, async (eventName, payload) => {
      if (eventName === "status") {
        setWaiting(payload.message || "SJTUClaw 正在处理…", { phase: payload.phase });
      } else if (eventName === "goal_state") {
        planCard = updateExecutionPlanCard(planCard, waiting, payload.goal, { live: true });
      } else if (eventName === "assistant_note") {
        const note = addMessage("assistant", payload.content || "", { progress: true });
        note?.closest(".message") && messages.insertBefore(note.closest(".message"), waiting);
      } else if (eventName === "tool_call") {
        const isFileWork = FILE_WORK_TOOLS.has(payload.tool);
        setWaiting(
          isFileWork ? "正在执行文件操作…" : `正在调用工具 · ${payload.tool}`,
          { phase: isFileWork ? "file_work" : "tool_call", longWork: isFileWork || undefined },
        );
        const card = createToolCard({
          tool: payload.tool, args: payload.args, phase: "call", callId: payload.callId,
        });
        insertToolCard(card, waiting);
      } else if (eventName === "tool_result") {
        setWaiting("工具已返回，正在继续处理…", { phase: "tool_result" });
        collectCitationsFromToolResult(payload.result, state.liveCitationMap || state.citationMap);
        const card = createToolCard({
          tool: payload.tool,
          result: payload.result,
          approvalId: payload.approvalId,
          phase: "result",
          callId: payload.callId || payload.result?.callId,
        });
        insertToolCard(card, waiting);
      } else if (["compaction_started", "compaction", "compaction_failed"].includes(eventName)) {
        if (eventName === "compaction_started") {
          waitingIndicator.setLabel("正在整理上下文…", {
            phase: "compaction", resetClock: true, resume: true,
          });
        } else {
          waitingIndicator.setLabel(
            eventName === "compaction_failed" ? "上下文整理失败" : "上下文整理完成",
            { phase: "compaction", pause: true },
          );
        }
        compactionCard = updateCompactionConversationCard(compactionCard, compactionAnchor(assistantText, waiting), eventName, payload);
      } else if (eventName === "approval_required") {
        setWaiting(`等待审批 · ${payload.approval.tool}`, { phase: "approval_required" });
      } else if (eventName === "assistant_delta") {
        if (!assistantText) {
          setWaiting("正在生成回答…", { phase: "model_stream" });
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantText?.classList.add("is-streaming");
        }
        const characters = Array.from(payload.delta || "");
        const frameCount = Math.min(24, Math.max(1, Math.ceil(characters.length / 90)));
        const chunkSize = Math.max(1, Math.ceil(characters.length / frameCount));
        for (let index = 0; index < characters.length; index += chunkSize) {
          assistantRaw += characters.slice(index, index + chunkSize).join("");
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
          if (index + chunkSize < characters.length) {
            await new Promise((resolve) => requestAnimationFrame(resolve));
          }
        }
      } else if (eventName === "assistant_final") {
        if (payload.content) {
          if (!assistantText) {
            assistantText = addMessage("assistant", "");
            assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          }
          assistantRaw = finalDisplayText(payload.content);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
        }
        assistantText?.classList.remove("is-streaming");
        waitingIndicator.pauseTimer();
        finishStreamingMessages();
      } else if (eventName === "assistant_reset") {
        if (assistantText) {
          const cleaned = stripProtocolTail(assistantRaw);
          if (cleaned !== assistantRaw) {
            assistantRaw = cleaned;
            assistantText.dataset.raw = assistantRaw;
            renderMessageContent(assistantText, assistantRaw);
          }
          assistantText.classList.remove("is-streaming");
          assistantText.classList.add("is-intermediate");
          assistantText.closest(".message")?.classList.add("is-intermediate");
          assistantText = null;
        }
        assistantRaw = "";
      } else if (eventName === "metrics") {
        const first = payload.timeToFirstTokenMs ? ` · 首字 ${Math.round(payload.timeToFirstTokenMs)} ms` : "";
        setWaiting(`模型返回 · ${payload.totalTokens} tokens${first}`, { phase: "model_returned" });
      } else if (eventName === "cancelled") {
        setWaiting("本轮已取消", { phase: "cancelled" });
        finishStreamingMessages();
      } else if (eventName === "error") {
        throw new Error(payload.message || "流式请求失败");
      } else if (eventName === "done") {
        assistantText?.classList.remove("is-streaming");
        waitingIndicator.pauseTimer();
        finishStreamingMessages();
        donePayload = payload;
        planCard = settleExecutionPlanCard(planCard, payload);
        if (assistantText && payload.reply) {
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        } else if (payload.reply) {
          assistantText = addMessage("assistant", "");
          assistantText?.closest(".message") && messages.insertBefore(assistantText.closest(".message"), waiting);
          assistantRaw = finalDisplayText(payload.reply);
          assistantText.dataset.raw = assistantRaw;
          renderMessageContent(assistantText, assistantRaw);
          maybeScrollMessagesToBottom();
        }
      }
      maybeScrollMessagesToBottom();
    }, { turnId });
    await loadSessions(state.currentSessionId);
    if (donePayload?.status === "approval_required") await loadApprovals();
  } catch (error) {
    if (error.name !== "AbortError") throw error;
  } finally {
    waitingIndicator.destroy();
    waiting.remove();
    finishStreamingMessages();
    if (state.cancelRequestedTurnId === turnId) {
      state.turnAbortController = null;
    } else {
      state.busy = false;
      state.turnId = null;
      state.activeTurn = null;
      state.turnAbortController = null;
      sendButton.disabled = false;
      sendButton.hidden = false;
      resetStopButton();
      input.disabled = false;
      input.focus();
    }
  }
}

$("#stop-button").addEventListener("click", () => {
  const turnId = state.turnId;
  if (!turnId || state.cancelRequestedTurnId === turnId) return;
  // Do not make the click wait for the provider's current HTTP request. The
  // helper updates the UI synchronously, aborts the local SSE reader, then
  // sends the Runtime cancellation request and reconciles in the background.
  requestTurnCancellation(turnId).catch(showError);
});

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function renderMessageContent(element, source, citationMap = null, options = {}) {
  const activeCitationMap = citationMap || element.__citationMap || state.citationMap;
  element.__citationMap = activeCitationMap;
  // Some attachments/models emit HTML whitespace entities in otherwise plain
  // Markdown (for example `&nbsp;`).  The renderer escapes HTML before
  // parsing, so those entities would otherwise appear literally as
  // "&nbsp;" in the conversation.  Decode only whitespace entities here;
  // do not treat arbitrary response text as HTML.
  const text = normalizeWhitespaceEntities(finalDisplayText(source));
  element.innerHTML = renderMarkdown(
    normalizeMathSource(text),
    activeCitationMap,
    { preserveLineBreaks: Boolean(options.preserveLineBreaks) },
  );
  renderMath(element);
  bindCitationPreviewButtons(element);
}

function normalizeWhitespaceEntities(value) {
  return String(value || "")
    .replace(/&nbsp;?/gi, " ")
    .replace(/&#160;|&#xA0;/gi, " ");
}

function inlineMarkdown(value, citationMap = null) {
  const context = value;
  let output = escapeHtml(value);
  output = output.replace(/`([^`]+)`/g, "<code>$1</code>");
  output = output.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // Gateway download links intentionally use same-origin relative URLs.
  // Restrict relative Markdown links to the download endpoint instead of
  // accepting arbitrary paths, while continuing to support normal web URLs.
  // Older/model-authored replies sometimes omit the Gateway's `/api` prefix.
  // Canonicalize these links while rendering so persisted conversations remain
  // downloadable after upgrading the Runtime.
  output = output.replace(
    /\[([^\]]+)\]\((?:(?:https?:\/\/[^\s)]+)?\/?(?:api\/)?downloads\/(dl_[A-Za-z0-9_-]+))\)/g,
    '<a href="/api/downloads/$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  output = output.replace(
    /\[([^\]]+)\]\(((?:https?:\/\/|\/api\/downloads\/)[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  const seenAttachments = new Set();
  output = output.replace(/\[([A-Z])(\d+)(?:\s*[-–~]\s*\1?(\d+))?\]/g, (match, prefix, startNumber, endNumber) => {
    const citation = citationForToken(prefix, startNumber, endNumber, context, citationMap);
    if (!citation) {
      const label = `${prefix}${startNumber}${endNumber ? `-${prefix}${endNumber}` : ""}`;
      return `<span class="citation-chip citation-chip-local citation-chip-unresolved" title="未找到对应来源链接">${label}</span>`;
    }
    const title = escapeHtml(citationTitle(citation));
    if (citation.url) {
      const url = escapeHtml(citation.url);
      const label = `${prefix}${startNumber}${endNumber ? `-${prefix}${endNumber}` : ""}`;
      return `<a class="citation-chip" href="${url}" target="_blank" rel="noopener noreferrer" title="${title}">${label}</a>`;
    }
    const attachment = attachmentForCitation(citation);
    const label = attachment ? attachmentCitationLabel(prefix, attachment.attachmentId) : `${prefix}${startNumber}`;
    if (!attachment) return `<span class="citation-chip citation-chip-local" title="${title}">${label}</span>`;
    const dedupeKey = `${prefix}:${attachment.attachmentId}`;
    if (seenAttachments.has(dedupeKey)) return "";
    seenAttachments.add(dedupeKey);
    return `<button class="citation-chip citation-chip-attachment" type="button" data-attachment-id="${escapeHtml(attachment.attachmentId)}" title="${title}">${escapeHtml(label)}</button>`;
  });
  return output;
}

function citationForToken(prefix, startNumber, endNumber = null, context = "", citationMap = null) {
  const start = Number(startNumber);
  const end = endNumber ? Number(endNumber) : start;
  const candidates = [];
  const map = citationMap || state.citationMap || {};
  const aliasesMap = map.__aliases || (citationMap ? {} : state.citationAliases) || {};
  for (let number = start; number <= end; number += 1) {
    const label = `[${prefix}${number}]`;
    const aliases = aliasesMap?.[label] || [];
    if (aliases.length) candidates.push(...aliases);
    else if (map?.[label]) candidates.push(map[label]);
  }
  if (!candidates.length) return null;
  // Page/slide/section labels are attachment-local. Multiple records from
  // one file (for example [P4-P5]) still have one unambiguous preview target.
  if (["P", "S", "D", "A", "O"].includes(prefix)) {
    const attachmentCandidates = candidates
      .map((citation) => ({ citation, attachment: attachmentForCitation(citation) }))
      .filter((item) => item.attachment);
    const attachmentIds = new Set(
      attachmentCandidates.map((item) => item.attachment.attachmentId),
    );
    if (attachmentIds.size === 1) return attachmentCandidates[0].citation;
  }
  // When several turns reused the same provider label (for example every
  // search starts at [W1]), an ambiguous label is safer as an unresolved chip
  // than as a confidently wrong link to a previous turn.
  return chooseCitationCandidate(candidates, context);
}

function chooseCitationCandidate(candidates, context) {
  const unique = [];
  for (const citation of candidates) {
    if (!citation) continue;
    const key = `${citation.url || ""}|${citation.attachmentId || ""}|${citation.filename || ""}`;
    if (unique.some((item) => item.__key === key)) continue;
    unique.push({ ...citation, __key: key });
  }
  if (unique.length <= 1) return unique[0] || null;
  const normalizedContext = normalizeCitationText(context);
  let best = null;
  let bestScore = -1;
  for (const citation of unique) {
    const attachment = attachmentForCitation(citation);
    const haystack = normalizeCitationText([
      citation.title,
      citation.filename,
      attachment?.filename,
      citation.kind,
    ].filter(Boolean).join(" "));
    let score = 0;
    for (const token of citationKeywords(haystack)) {
      if (normalizedContext.includes(token)) score += token.length >= 4 ? 3 : 1;
    }
    if (normalizedContext.includes("北大") && haystack.includes("智能科学")) score += 8;
    if (normalizedContext.includes("北京大学") && haystack.includes("智能科学")) score += 8;
    if ((normalizedContext.includes("交大") || normalizedContext.includes("上海交通大学")) && haystack.includes("计算机科学与技术")) score += 6;
    if (normalizedContext.includes("acm") && haystack.includes("acm")) score += 8;
    if (score > bestScore) {
      best = citation;
      bestScore = score;
    }
  }
  return bestScore > 0 ? best : null;
}

function normalizeCitationText(value) {
  return String(value || "").toLowerCase().replace(/\s+/g, "");
}

function citationKeywords(text) {
  const value = normalizeCitationText(text)
    .replace(/\.(pdf|docx|pptx|xlsx|png|jpe?g|webp)$/i, "")
    .replace(/[()[\]（）【】《》_\-—–~·.,，。:：;；/\\]+/g, " ");
  return value.split(/\s+/).filter((token) =>
    token.length >= 2
    && !/^\d+$/.test(token)
    && !/^20\d{2}$/.test(token)
    && token !== "pdf"
  );
}

function citationTitle(citation) {
  const attachment = attachmentForCitation(citation);
  if (attachment) return `预览附件：${attachment.filename}`;
  const details = [];
  if (citation.title && citation.title !== citation.label) details.push(citation.title);
  if (citation.filename) details.push(citation.filename);
  if (citation.path) details.push(`Workspace：${citation.path}`);
  if (citation.page) details.push(`第 ${citation.page} 页`);
  if (citation.slide) details.push(`第 ${citation.slide} 页幻灯片`);
  if (citation.section) details.push(`第 ${citation.section} 段`);
  if (citation.block) details.push(`OCR 块 ${citation.block}`);
  if (citation.confidence !== undefined && citation.confidence !== null) details.push(`置信度 ${citation.confidence}`);
  if (citation.kind) details.push(citation.kind);
  if (citation.url) details.push(citation.url);
  return details.join(" · ") || citation.label || "来源";
}

function attachmentForCitation(citation) {
  if (citation?.attachmentId && state.attachmentMap[citation.attachmentId]) {
    return state.attachmentMap[citation.attachmentId];
  }
  if (citation?.filename) {
    return Object.values(state.attachmentMap).find((item) => item.filename === citation.filename) || null;
  }
  return null;
}

function attachmentFilename(attachmentId) {
  return state.attachmentMap?.[attachmentId]?.filename || "";
}

function attachmentCitationLabel(prefix, attachmentId) {
  const attachmentIds = [];
  const sorted = allCitationCandidates().sort((left, right) => {
    const leftNumber = Number(left.label?.match(/\d+/)?.[0] || 0);
    const rightNumber = Number(right.label?.match(/\d+/)?.[0] || 0);
    return leftNumber - rightNumber;
  });
  for (const citation of sorted) {
    if (!citation?.label?.startsWith(`[${prefix}`)) continue;
    const attachment = attachmentForCitation(citation);
    if (!attachment || attachmentIds.includes(attachment.attachmentId)) continue;
    attachmentIds.push(attachment.attachmentId);
  }
  const index = Math.max(0, attachmentIds.indexOf(attachmentId));
  return `${prefix}${index + 1}`;
}

function allCitationCandidates() {
  const items = [];
  const seen = new Set();
  for (const citation of Object.values(state.citationMap || {})) {
    if (citation?.label) {
      const key = `${citation.label}|${citation.url || ""}|${citation.attachmentId || ""}|${citation.filename || ""}|${citation.page || ""}`;
      if (!seen.has(key)) {
        seen.add(key);
        items.push(citation);
      }
    }
  }
  for (const aliases of Object.values(state.citationAliases || {})) {
    for (const citation of aliases || []) {
      if (!citation?.label) continue;
      const key = `${citation.label}|${citation.url || ""}|${citation.attachmentId || ""}|${citation.filename || ""}|${citation.page || ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(citation);
    }
  }
  return items;
}

function bindCitationPreviewButtons(root) {
  root.querySelectorAll(".citation-chip-attachment").forEach((button) => {
    button.onclick = () => {
      const attachment = state.attachmentMap[button.dataset.attachmentId];
      if (attachment) openAttachmentPreview(attachment);
    };
  });
}

function renderMath(element) {
  if (!element || typeof window.renderMathInElement !== "function") return;
  try {
    window.renderMathInElement(element, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      throwOnError: false,
      strict: "ignore",
      trust: false,
    });
  } catch (error) {
    console.warn("LaTeX render skipped", error);
  }
}

function normalizeMathSource(value) {
  let text = String(value || "");
  // Models occasionally nest inline delimiters inside a display block, e.g.
  // `$$ J(\(\\theta\)) $$`. KaTeX correctly handles either form alone, but
  // not the mixed form. Keep the outer display delimiter and remove only the
  // redundant inner wrappers.
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, (match, body) =>
    `$$${body.replace(/\\\(/g, "").replace(/\\\)/g, "").replace(/\\\[/g, "").replace(/\\\]/g, "")}$$`
  );
  // A few OpenAI-compatible models still wrap plain mathematical results in
  // ```math``` / ```calc``` fences.  Convert those known math-only blocks to
  // display LaTeX before the Markdown parser turns them into code blocks.
  return text.replace(/```(?:math|calc|latex|tex)\s*\n([\s\S]*?)```/gi, (_match, body) => {
    const lines = body.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!lines.length) return "";
    const latexLines = lines.map(plainMathLineToLatex);
    const latex = latexLines.length === 1
      ? latexLines[0]
      : `\\begin{aligned}${latexLines.join(" \\\\ ")}\\end{aligned}`;
    return `\\[\n${latex}\n\\]`;
  });
}

function plainMathLineToLatex(value) {
  const superscripts = { "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9" };
  const subscripts = { "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9" };
  let text = String(value || "").replace(/[✅❌]/g, "").trim();
  text = text.replace(/\bC\(\s*([^,()]+)\s*,\s*([^()]+)\s*\)/g, "\\binom{$1}{$2}");
  text = text.replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹]+/g, (match) =>
    `^{${[...match].map((char) => superscripts[char]).join("")}}`);
  text = text.replace(/[₀₁₂₃₄₅₆₇₈₉]+/g, (match) =>
    `_{${[...match].map((char) => subscripts[char]).join("")}}`);
  text = text
    .replace(/π/g, "\\pi")
    .replace(/√/g, "\\sqrt")
    .replace(/∫/g, "\\int")
    .replace(/≈/g, "\\approx")
    .replace(/·/g, "\\cdot ")
    .replace(/−/g, "-")
    .replace(/(\d+(?:\.\d+)?)°/g, "$1^{\\circ}")
    .replace(/(?<!\\)\b(sin|cos|tan|log|ln|exp)\b/g, "\\$1");
  return text;
}

function normalizeCollapsedMarkdownTables(source) {
  let inCode = false;
  return String(source || "").replace(/\r/g, "").split("\n").map((line) => {
    if (line.trimStart().startsWith("```")) {
      inCode = !inCode;
      return line;
    }
    if (inCode) return line;
    // Some models emit a whole table on one line and use an empty cell
    // (`| |` or `||`) as a row boundary. Restore those boundaries before
    // parsing, but only when the same line contains a Markdown divider.
    if (!/\|\s*:?-{2,}:?\s*\|/.test(line) || !/\|\s*\|/.test(line)) return line;
    return line.replace(/\|\s*\|/g, "|\n|");
  }).join("\n");
}

function renderMarkdown(source, citationMap = null, options = {}) {
  const preserveLineBreaks = Boolean(options.preserveLineBreaks);
  const lines = normalizeCollapsedMarkdownTables(source).split("\n");
  const output = [];
  let inCode = false;
  let code = [];
  let listType = null;
  let listItem = "";
  let paragraph = [];
  const smartJoin = (parts) => parts.reduce((text, part) => {
    if (!text) return part;
    const startsWithPunctuation = /^[，。！？；：、）】,.!?;:]/.test(part);
    const joinsChinese = /[\u3400-\u9fff]$/.test(text) && /^[\u3400-\u9fff]/.test(part);
    const noSpace = startsWithPunctuation || joinsChinese;
    return `${text}${noSpace ? "" : " "}${part}`;
  }, "");
  const closeParagraph = () => {
    if (!paragraph.length) return;
    const paragraphHtml = preserveLineBreaks
      ? paragraph.map((line) => inlineMarkdown(line, citationMap)).join("<br>")
      : inlineMarkdown(smartJoin(paragraph), citationMap);
    output.push(`<p>${paragraphHtml}</p>`);
    paragraph = [];
  };
  const closeListItem = () => {
    if (!listItem) return;
    output.push(`<li>${inlineMarkdown(listItem, citationMap)}</li>`);
    listItem = "";
  };
  const closeList = () => {
    if (!listType) return;
    closeListItem();
    output.push(`</${listType}>`);
    listType = null;
  };
  const tableCells = (line) => {
    let text = line.trim();
    const isEscapedPipe = (index) => {
      let slashCount = 0;
      for (let cursor = index - 1; cursor >= 0 && text[cursor] === "\\"; cursor -= 1) slashCount += 1;
      return slashCount % 2 === 1;
    };
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|") && !isEscapedPipe(text.length - 1)) text = text.slice(0, -1);
    const cells = [];
    let cell = "";
    for (let index = 0; index < text.length; index += 1) {
      if (text[index] === "|" && !isEscapedPipe(index)) {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += text[index];
      }
    }
    cells.push(cell.trim());
    return cells;
  };
  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell));
  };
  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex];
    if (line.startsWith("```")) {
      closeParagraph();
      closeList();
      if (inCode) {
        output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { code.push(line); continue; }
    if (/^\s*-{3,}\s*$/.test(line)) {
      closeParagraph();
      closeList();
      continue;
    }
    if (/^\s*>/.test(line)) {
      closeParagraph();
      closeList();
      const quoteLines = [];
      while (lineIndex < lines.length && /^\s*>/.test(lines[lineIndex])) {
        quoteLines.push(lines[lineIndex].replace(/^\s*>\s?/, ""));
        lineIndex += 1;
      }
      lineIndex -= 1;
      output.push(`<blockquote>${renderMarkdown(
        quoteLines.join("\n"),
        citationMap,
        { preserveLineBreaks },
      )}</blockquote>`);
      continue;
    }
    if (line.includes("|") && lineIndex + 1 < lines.length && isTableDivider(lines[lineIndex + 1])) {
      closeParagraph();
      closeList();
      const headers = tableCells(line);
      const alignments = tableCells(lines[lineIndex + 1]).map((cell) => {
        if (cell.startsWith(":") && cell.endsWith(":")) return "center";
        if (cell.endsWith(":")) return "right";
        return "left";
      });
      const rows = [];
      lineIndex += 2;
      while (lineIndex < lines.length && lines[lineIndex].trim() && lines[lineIndex].includes("|")) {
        rows.push(tableCells(lines[lineIndex]));
        lineIndex += 1;
      }
      lineIndex -= 1;
      const head = headers.map((cell, index) => `<th style="text-align:${alignments[index] || "left"}">${inlineMarkdown(cell, citationMap)}</th>`).join("");
      const body = rows.map((row) => `<tr>${headers.map((_, index) => `<td style="text-align:${alignments[index] || "left"}">${inlineMarkdown(row[index] || "", citationMap)}</td>`).join("")}</tr>`).join("");
      output.push(`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      closeParagraph();
      closeList();
      const level = Math.min(heading[1].length, 4);
      output.push(`<h${level}>${inlineMarkdown(heading[2], citationMap)}</h${level}>`);
      continue;
    }
    const item = line.match(/^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$/);
    if (item) {
      closeParagraph();
      const nextType = item[2] ? "ol" : "ul";
      if (listType !== nextType) {
        closeList();
        const start = nextType === "ol" && Number(item[2]) !== 1 ? ` start="${Number(item[2])}"` : "";
        output.push(`<${nextType}${start}>`);
        listType = nextType;
      } else {
        closeListItem();
      }
      listItem = item[3];
      continue;
    }
    if (!line.trim()) {
      closeParagraph();
      closeList();
      continue;
    }
    if (listType) {
      listItem = smartJoin([listItem, line.trim()]);
    } else {
      paragraph.push(line.trim());
    }
  }
  closeParagraph();
  closeList();
  if (inCode) output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  return output.join("");
}

function isDuplicateTurnEvent(turnId, payload) {
  const sequence = Number(payload?.eventSeq ?? payload?.agentEvent?.seq);
  if (!turnId || !Number.isFinite(sequence) || sequence < 1) return false;
  let seen = state.turnEvents.get(turnId);
  if (!seen) {
    seen = new Set();
    state.turnEvents.set(turnId, seen);
  }
  if (seen.has(sequence)) return true;
  seen.add(sequence);
  // Keep reconnect bookkeeping bounded even if a long-running turn emits
  // many deltas. Sequence numbers are monotonic, so dropping the oldest
  // entries cannot make a newer replay duplicate visible.
  if (seen.size > 2000) {
    const oldest = [...seen].sort((a, b) => a - b).slice(0, seen.size - 1500);
    oldest.forEach((item) => seen.delete(item));
  }
  return false;
}

async function consumeSse(response, onEvent, options = {}) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary).replace(/\r/g, "");
      buffer = buffer.slice(boundary + 2);
      if (!block || block.startsWith(":")) continue;
      let eventName = "message";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (dataLines.length) {
        const payload = JSON.parse(dataLines.join("\n"));
        const eventTurnId = payload?.turnId || payload?.agentEvent?.turnId || options.turnId;
        if (isDuplicateTurnEvent(eventTurnId, payload)) continue;
        await onEvent(eventName, payload);
      }
    }
    if (done) break;
  }
}

async function materializeDraftSession() {
  if (!state.isDraft && state.currentSessionId) return state.currentSessionId;
  const selectedFiles = (state.pendingDraftFiles || []).filter((_, index) => state.pendingDraftSelected.has(index));
  const session = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: state.draftTitle || "新会话",
      ...(state.draftWorkspace ? { workspace: state.draftWorkspace } : {}),
    }),
  });
  state.currentSessionId = session.sessionId;
  state.isDraft = false;
  sessionStorage.setItem("sjtuclaw.lastSessionId", session.sessionId);
  state.pendingDraftFiles = [];
  state.pendingDraftSelected.clear();
  await loadSessions(session.sessionId);
  for (const file of selectedFiles) await uploadFiles([file]);
  return session.sessionId;
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
}

function slashQuery() {
  if (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) return null;
  const match = input.value.match(/^\/([^\s/]*)$/);
  return match ? match[1].toLowerCase() : null;
}

function closeSlashCommandMenu() {
  slashCommandMenu.hidden = true;
  slashCommandList.replaceChildren();
  visibleSlashCommands = [];
  activeSlashCommandIndex = 0;
  input.removeAttribute("aria-activedescendant");
  input.setAttribute("aria-expanded", "false");
}

function setActiveSlashCommand(index) {
  if (!visibleSlashCommands.length) return;
  activeSlashCommandIndex = (index + visibleSlashCommands.length) % visibleSlashCommands.length;
  const buttons = [...slashCommandList.querySelectorAll(".slash-command-item")];
  buttons.forEach((button, itemIndex) => {
    const active = itemIndex === activeSlashCommandIndex;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    if (active) {
      input.setAttribute("aria-activedescendant", button.id);
      button.scrollIntoView({ block: "nearest" });
    }
  });
}

function updateSlashCommandMenu() {
  const query = slashQuery();
  if (query === null || state.busy) {
    closeSlashCommandMenu();
    return;
  }
  visibleSlashCommands = SLASH_COMMANDS.filter((item) => {
    const text = `${item.command.slice(1)} ${item.label} ${item.description} ${item.keywords || ""}`.toLowerCase();
    return !query || text.includes(query);
  });
  slashCommandList.replaceChildren();
  if (!visibleSlashCommands.length) {
    const empty = document.createElement("p");
    empty.className = "slash-command-empty";
    empty.textContent = "没有匹配的快捷功能";
    slashCommandList.append(empty);
  } else {
    visibleSlashCommands.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.id = `slash-command-${index}`;
      button.className = "slash-command-item";
      button.setAttribute("role", "option");
      const icon = document.createElement("span");
      icon.className = "slash-command-icon";
      icon.textContent = item.icon;
      const name = document.createElement("span");
      name.className = "slash-command-name";
      name.textContent = item.command;
      const description = document.createElement("span");
      description.className = "slash-command-description";
      description.textContent = `${item.label} · ${item.description}`;
      button.append(icon, name, description);
      button.onpointerdown = (event) => event.preventDefault();
      button.onclick = () => executeSlashCommand(item);
      slashCommandList.append(button);
    });
  }
  slashCommandMenu.hidden = false;
  input.setAttribute("aria-expanded", "true");
  activeSlashCommandIndex = Math.min(activeSlashCommandIndex, Math.max(visibleSlashCommands.length - 1, 0));
  setActiveSlashCommand(activeSlashCommandIndex);
}

async function executeSlashCommand(item) {
  if (!item) return false;
  closeSlashCommandMenu();
  input.value = "";
  resizeInput();
  try {
    await item.run();
  } catch (error) {
    showError(error);
  } finally {
    if (!state.busy) input.focus();
  }
  return true;
}

function exactSlashCommand(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return SLASH_COMMANDS.find((item) => item.command === normalized) || null;
}

function openCurrentWorkspacePicker() {
  if (state.isDraft || !state.currentSessionId) {
    const chip = document.querySelector(".draft-workspace-chip");
    if (!chip) throw new Error("新对话 Workspace 入口尚未加载，请稍后重试。");
    chip.click();
    return;
  }
  const chip = document.querySelector(
    `.workspace-chip[data-session-id="${CSS.escape(state.currentSessionId)}"]`,
  );
  if (!chip) throw new Error("当前 Workspace 信息尚未加载，请稍后重试。");
  chip.click();
}

function openModelPicker() {
  if (!modelSelect || modelSelect.disabled) throw new Error("模型列表尚未加载。");
  modelSelect.focus();
  if (typeof modelSelect.showPicker === "function") modelSelect.showPicker();
}

function createCompactionNoticeCard(message) {
  const card = document.createElement("article");
  card.className = "compaction-card success";
  const header = document.createElement("div");
  header.className = "compaction-header";
  const icon = document.createElement("span");
  icon.className = "compaction-icon";
  icon.textContent = "i";
  const title = document.createElement("strong");
  title.textContent = "本次未进行整理";
  header.append(icon, title);
  const detail = document.createElement("p");
  detail.className = "compaction-hint";
  detail.textContent = message;
  card.append(header, detail);
  return card;
}

async function runManualCompaction() {
  if (state.isDraft || !state.currentSessionId) {
    throw new Error("新对话还没有历史消息，无需整理上下文。");
  }
  if (state.busy) throw new Error("当前会话仍在生成回复，请稍后再整理。");
  const sessionId = state.currentSessionId;
  let card = createCompactionProgressCard({});
  messages.append(card);
  maybeScrollMessagesToBottom(true);
  try {
    const result = await api(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, {
      method: "POST",
    });
    if (state.currentSessionId !== sessionId) return;
    if (result.compacted) {
      // The Gateway persisted a compaction activity event. Reload once and let
      // the normal history renderer restore exactly one collapsed card.
      await loadSession(sessionId);
    } else {
      card.replaceWith(createCompactionNoticeCard(result.message || "当前会话无需整理。"));
    }
    renderMessageIndex();
    maybeScrollMessagesToBottom(true);
    // A no-op result exists only in the current transcript. Reloading the
    // Session here used to erase it immediately, producing a confusing flash.
    // Successful compaction already reloaded durable history above; neither
    // branch needs a second full Session reload.
    const sessions = await api("/api/sessions");
    state.sessions = sessions.sessions || state.sessions;
    rememberSessionSnapshots(state.sessions);
    renderSessionList();
  } catch (error) {
    if (card?.isConnected) card.replaceWith(createCompactionErrorCard({ error: error.message }));
    throw error;
  }
}

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const slashCommand = exactSlashCommand(input.value);
  if (slashCommand) {
    await executeSlashCommand(slashCommand);
    return;
  }
  const hasDraftAttachments = state.pendingDraftFiles.some((_, index) => state.pendingDraftSelected.has(index));
  const fallbackText = (state.pendingAttachments.length || hasDraftAttachments)
    ? "请读取并分析这些附件。"
    : state.pendingQuote ? "请结合引用继续回答。" : "";
  const visibleText = input.value.trim() || fallbackText;
  if (!visibleText) return;
  try {
    await materializeDraftSession();
  } catch (error) {
    showError(error);
    return;
  }
  const attached = state.pendingAttachments.map(({ attachmentId, filename, contentType }) => ({
    attachmentId, filename, contentType,
  }));
  let text = visibleText;
  let displayText = visibleText;
  if (state.pendingQuote) {
    const quoted = {
      role: state.pendingQuote.role,
      label: state.pendingQuote.label,
      content: state.pendingQuote.content,
    };
    text = `${text}\n\n[quoted_message] ${JSON.stringify(quoted)}\n以上是用户本轮引用的历史消息。请优先结合该引用理解用户意图；不要在最终回答中原样复读整段引用，除非用户要求。`;
    displayText = `> 引用 ${state.pendingQuote.label}：${state.pendingQuote.summary}\n\n${visibleText}`;
  }
  if (attached.length) {
    text = `${text}\n\n[attached_files] ${JSON.stringify(attached)}\n以上是用户本轮明确选中的附件。图片会在模型支持视觉时直接随请求发送；其他文件必须先使用合适的附件读取工具获取真实内容。不得跳过，也不要假装已经读取。未列在这里但属于当前 Session 的其他附件仍可按任务需要读取。`;
  }
  input.value = "";
  resizeInput();
  try {
    const selectedSkill = state.selectedSkill;
    await sendMessage(text, displayText, {
      skillName: selectedSkill?.name,
      selectedAttachments: attached,
      onSubmitted: () => {
        state.pendingAttachments = [];
        state.pendingAttachmentSession = null;
        renderAttachments(Object.values(state.attachmentMap));
      },
    });
    if (selectedSkill && state.selectedSkill?.name === selectedSkill.name) {
      setComposerSkill(null);
    }
    clearPendingQuote();
    renderAttachments((await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}`)).attachments || []);
  } catch (error) { showError(error); }
});
input.addEventListener("input", () => {
  resizeInput();
  updateSlashCommandMenu();
});
$("#quote-preview-clear").onclick = clearPendingQuote;
input.addEventListener("keydown", (event) => {
  if (!slashCommandMenu.hidden) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActiveSlashCommand(activeSlashCommandIndex + (event.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if ((event.key === "Enter" || event.key === "Tab") && visibleSlashCommands.length) {
      event.preventDefault();
      executeSlashCommand(visibleSlashCommands[activeSlashCommandIndex]);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeSlashCommandMenu();
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});

function clipboardFiles(clipboardData) {
  const direct = [...(clipboardData?.files || [])];
  const files = direct.length
    ? direct
    : [...(clipboardData?.items || [])]
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  return files.map((file, index) => {
    if (file.name && file.name.trim()) return file;
    const extensions = {
      "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
      "image/gif": "gif", "application/pdf": "pdf",
    };
    const extension = extensions[file.type] || "bin";
    const prefix = file.type.startsWith("image/") ? "粘贴图片" : "粘贴文件";
    return new File([file], `${prefix}-${stamp}${index ? `-${index + 1}` : ""}.${extension}`, {
      type: file.type || "application/octet-stream",
      lastModified: file.lastModified || Date.now(),
    });
  });
}

input.addEventListener("paste", async (event) => {
  const files = clipboardFiles(event.clipboardData);
  if (!files.length) return;
  event.preventDefault();
  if (state.busy) {
    showError(new Error("请等待当前回复完成后再粘贴附件。"));
    return;
  }
  try {
    await uploadFiles(files);
  } catch (error) {
    showError(error);
  }
});

$("#new-session").addEventListener("click", async () => {
  if (state.busy) return;
  const title = window.prompt("新会话标题", "新会话");
  if (title === null) return;
  state.draftTitle = title.trim() || "新会话";
  const previous = state.sessions.find((item) => item.sessionId === state.currentSessionId);
  state.draftWorkspace = previous?.workspace || null;
  sessionStorage.removeItem("sjtuclaw.lastSessionId");
  state.pendingDraftFiles = [];
  state.pendingDraftSelected.clear();
  showDraftSession();
  return;
});

function stageDraftFiles(files) {
  const incoming = files.filter(Boolean);
  if (!incoming.length) return;
  const start = state.pendingDraftFiles.length;
  state.pendingDraftFiles.push(...incoming);
  incoming.forEach((_, index) => state.pendingDraftSelected.add(start + index));
  renderDraftAttachments();
}

async function uploadFiles(files) {
  if (!files.length) return;
  if (!state.currentSessionId) {
    stageDraftFiles(files);
    return;
  }
  const uploaded = [];
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    const result = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/attachments`, { method: "POST", body: form });
    uploaded.push(result.attachment);
  }
  state.pendingAttachmentSession = state.currentSessionId;
  const selected = new Set(state.pendingAttachments.map((item) => item.attachmentId));
  state.pendingAttachments.push(...uploaded.filter((item) => !selected.has(item.attachmentId)));
  await loadSession(state.currentSessionId);
}

$("#file-input").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  try { await uploadFiles(files); } catch (error) { showError(error); }
  event.target.value = "";
});

let dragDepth = 0;
const workspaceDropTarget = $(".workspace");
const dropOverlay = $("#drop-overlay");
const hasDraggedFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");

workspaceDropTarget.addEventListener("dragenter", (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  dropOverlay.classList.add("visible");
});
workspaceDropTarget.addEventListener("dragover", (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
workspaceDropTarget.addEventListener("dragleave", (event) => {
  if (!hasDraggedFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropOverlay.classList.remove("visible");
});
workspaceDropTarget.addEventListener("drop", async (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  dropOverlay.classList.remove("visible");
  if (state.busy) return showError(new Error("请等待当前回复完成后再上传附件。"));
  try { await uploadFiles([...event.dataTransfer.files]); } catch (error) { showError(error); }
});

async function pickWorkspace(trigger = null) {
  if (!state.currentSessionId || state.busy) return;
  const original = trigger?.textContent;
  if (trigger) {
    trigger.classList.add("loading");
    trigger.textContent = "Workspace · 正在选择…";
  }
  try {
    // A renamed project is a common, recoverable case.  Ask before changing
    // the persisted path; declining falls through to the normal picker.
    try {
      const status = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/workspace`);
      if (status.canMigrate) {
        const migrate = window.confirm(
          `检测到 Workspace 路径已失效：\n${status.workspace}\n\n是否迁移到当前项目目录？\n${status.projectRoot}`
        );
        if (migrate) {
          await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/workspace/migrate`, { method: "POST" });
          await loadSession(state.currentSessionId);
          return;
        }
      }
    } catch (_) {
      // Keep the existing picker/prompt fallback if the status check fails.
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 125000);
    let result;
    try {
      result = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/workspace/pick`, {
        method: "POST",
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timeout);
    }
    if (result.cancelled) return;
    await loadSession(state.currentSessionId);
  } catch (pickerError) {
    try {
      const current = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/workspace`);
      const path = window.prompt("系统目录选择器不可用，请输入 Workspace 绝对路径", current.workspace || "");
      if (path === null || !path.trim()) return;
      await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/workspace`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path.trim() }),
      });
      await loadSession(state.currentSessionId);
    } catch (error) { showError(error); }
  } finally {
    if (trigger) {
      trigger.classList.remove("loading");
      if (original) trigger.textContent = original;
    }
  }
}

async function pickDraftWorkspace(trigger = null) {
  if (!state.isDraft || state.currentSessionId || state.busy) return;
  if (trigger) {
    trigger.classList.add("loading");
    trigger.textContent = "Workspace · 正在选择…";
  }
  try {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 125000);
    let result;
    try {
      result = await api("/api/workspace/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initialPath: state.draftWorkspace || null }),
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timeout);
    }
    if (result.cancelled) return;
    state.draftWorkspace = result.workspace;
    if (trigger) {
      trigger.textContent = `Workspace · ${state.draftWorkspace}`;
      trigger.title = state.draftWorkspace;
    }
  } catch (pickerError) {
    const path = window.prompt(
      "系统目录选择器不可用，请输入 Workspace 绝对路径",
      state.draftWorkspace || "",
    );
    if (path === null || !path.trim()) return;
    state.draftWorkspace = path.trim();
    if (trigger) {
      trigger.textContent = `Workspace · ${state.draftWorkspace}`;
      trigger.title = state.draftWorkspace;
    }
  } finally {
    if (trigger?.isConnected) {
      trigger.classList.remove("loading");
      trigger.textContent = state.draftWorkspace
        ? `Workspace · ${state.draftWorkspace}`
        : "Workspace · 未设置";
      trigger.title = state.draftWorkspace || "点击为新对话选择 Workspace";
    }
  }
}

function toggleTaskPanel(open) {
  taskPanel.classList.toggle("open", open);
  taskPanel.setAttribute("aria-hidden", String(!open));
  $("#task-backdrop").hidden = !open;
  stopTaskFallbackLoop();
  if (open) {
    loadTasks(true).catch(showError);
    if (!state.gatewayEventsConnected) startTaskFallbackLoop();
  }
}

async function loadTasks(refreshConversation = false) {
  if (taskLoading) return;
  taskLoading = true;
  try {
    const data = await api("/api/tasks");
    const nextSnapshot = JSON.stringify(data.tasks.map((task) => ({
      id: task.taskId,
      status: task.status,
      next: task.nextRunAt,
      runCount: task.runCount,
      history: task.history.map((run) => run.finishedAt),
    })));
    const changed = nextSnapshot !== taskSnapshot;
    if (changed) {
      taskSnapshot = nextSnapshot;
      const list = $("#task-list");
      list.replaceChildren();
      if (!data.tasks.length) {
        const empty = document.createElement("div");
        empty.className = "task-empty";
        empty.textContent = "还没有定时任务";
        list.append(empty);
      } else {
        for (const task of data.tasks) list.append(renderTask(task));
      }
    }
    // Scheduler turns happen outside this page's chat SSE request. Keep the
    // bound conversation in sync whenever task history is refreshed.
    if ((refreshConversation || changed) && state.currentSessionId && !state.busy) {
      await loadSessions(state.currentSessionId);
    }
  } finally {
    taskLoading = false;
  }
}

function renderTask(task) {
  const card = document.createElement("article");
  card.className = "task-card";
  const top = document.createElement("div");
  top.className = "task-card-top";
  const type = document.createElement("span");
  type.className = "task-badge";
  type.textContent = task.taskType === "once" ? "一次性" : task.taskType === "cron" ? "Cron" : "周期";
  const status = document.createElement("span");
  status.className = `task-badge ${task.status}`;
  status.textContent = ({
    pending: "待执行",
    running: "执行中",
    waiting_approval: "等待审批",
    failed: "上次失败",
    paused: "已暂停",
    completed: "已完成",
    expired: "已到期",
    cancelled: "已取消",
  })[task.status] || task.status;
  top.append(type, status);
  const content = document.createElement("p");
  content.textContent = task.content;
  const meta = document.createElement("div");
  meta.className = "task-meta";
  const sessionTitle = state.sessions.find((item) => item.sessionId === task.sessionId)?.title || task.sessionId;
  const ruleText = task.taskType === "once"
    ? "未来时间点执行一次"
    : task.taskType === "cron"
      ? `Cron ${task.cronExpression || "未设置"}（${task.timezone || "Asia/Shanghai"}）`
      : `每 ${formatDuration(task.intervalSeconds)} 执行一次`;
  meta.append(textLine(`规则 · ${ruleText}`));
  if (task.executionContext && task.executionContext !== "main") {
    meta.append(textLine(`执行上下文 · ${task.executionContext}`));
  }
  if (task.deliveryMode && task.deliveryMode !== "session") {
    const channels = task.deliveryChannels?.length ? task.deliveryChannels.join(", ") : task.deliveryChannel;
    meta.append(textLine(`结果投递 · ${task.deliveryMode}${channels ? ` · ${channels}` : ""}`));
  }
  if (["interval", "cron"].includes(task.taskType) && task.startsAt) {
    meta.append(textLine(`开始 · ${new Date(task.startsAt).toLocaleString()}`));
  }
  if (task.endsAt) {
    meta.append(textLine(`结束 · ${new Date(task.endsAt).toLocaleString()}`));
  }
  if (task.maxRuns) {
    meta.append(textLine(`执行次数 · ${task.runCount || 0} / ${task.maxRuns}`));
  } else if (["interval", "cron"].includes(task.taskType)) {
    meta.append(textLine(`执行次数 · ${task.runCount || 0} / 不限`));
  }
  meta.append(textLine(`Session · ${sessionTitle}`));
  meta.append(textLine(`下次执行 · ${task.nextRunAt ? new Date(task.nextRunAt).toLocaleString() : "无"}`));
  meta.append(textLine(`创建 · ${task.createdAt ? new Date(task.createdAt).toLocaleString() : "未知"}`));
  meta.append(textLine(`更新 · ${task.updatedAt ? new Date(task.updatedAt).toLocaleString() : "未知"}`));
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `执行历史 (${task.history.length})`;
  details.append(summary);
  for (const run of [...task.history].reverse()) {
    const row = document.createElement("div");
    row.className = "task-history";
    row.textContent = `${run.success ? "成功" : "失败"} · ${new Date(run.finishedAt).toLocaleString()}\n${run.assistantReply || run.error || "无结果"}`;
    details.append(row);
  }
  card.append(top, content, meta, details);
  const controls = document.createElement("div");
  controls.className = "task-controls";
  if (["pending", "failed"].includes(task.status)) {
    const run = document.createElement("button");
    run.className = "task-run-now";
    run.textContent = "立即执行";
    run.onclick = async () => {
      run.disabled = true;
      try {
        await api(`/api/tasks/${encodeURIComponent(task.taskId)}/run`, { method: "POST" });
        await loadTasks();
      } catch (error) { showError(error); run.disabled = false; }
    };
    controls.append(run);
  }
  if (["pending", "failed"].includes(task.status)) {
    const pause = document.createElement("button");
    pause.className = "task-cancel";
    pause.textContent = "暂停";
    pause.onclick = async () => {
      try {
        await api(`/api/tasks/${encodeURIComponent(task.taskId)}/pause`, { method: "POST" });
        await loadTasks();
      } catch (error) { showError(error); }
    };
    controls.append(pause);
  }
  if (task.status === "paused") {
    const resume = document.createElement("button");
    resume.className = "task-cancel";
    resume.textContent = "恢复";
    resume.onclick = async () => {
      try {
        await api(`/api/tasks/${encodeURIComponent(task.taskId)}/resume`, { method: "POST" });
        await loadTasks();
      } catch (error) { showError(error); }
    };
    controls.append(resume);
  }
  if (!["completed", "cancelled", "expired"].includes(task.status)) {
    const cancel = document.createElement("button");
    cancel.className = "task-cancel";
    cancel.textContent = "取消未来触发";
    cancel.onclick = async () => {
      try {
        await api(`/api/tasks/${encodeURIComponent(task.taskId)}/cancel`, { method: "POST" });
        await loadTasks();
      } catch (error) { showError(error); }
    };
    controls.append(cancel);
  }
  if (controls.children.length) card.append(controls);
  return card;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value > 0 && value % 3600 === 0) return `${value / 3600} 小时`;
  if (value > 0 && value % 60 === 0) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

function textLine(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

function syncTaskDeliveryChannelVisibility() {
  const mode = $("#delivery-mode")?.value;
  const wrap = $("#delivery-channel-wrap");
  if (wrap) wrap.hidden = mode !== "channel";
}

$("#task-toggle").onclick = () => toggleTaskPanel(true);
$("#task-close").onclick = () => toggleTaskPanel(false);
$("#task-backdrop").onclick = () => toggleTaskPanel(false);
$("#task-refresh").onclick = () => loadTasks(true).catch(showError);
$("#task-type").onchange = (event) => {
  const once = event.target.value === "once";
  const cron = event.target.value === "cron";
  $("#task-time-wrap").hidden = !once;
  $("#task-interval-wrap").hidden = once || cron;
  $("#task-start-wrap").hidden = once;
  $("#task-end-wrap").hidden = once;
  $("#task-max-runs-wrap").hidden = once;
  $("#task-cron-wrap").hidden = !cron;
  $("#task-timezone-wrap").hidden = !cron;
};

$("#delivery-mode").onchange = syncTaskDeliveryChannelVisibility;
syncTaskDeliveryChannelVisibility();

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.currentSessionId) return;
  const taskType = $("#task-type").value;
  const payload = {
    content: $("#task-content").value.trim(),
    sessionId: state.currentSessionId,
    taskType,
    executionContext: $("#exec-context").value,
    deliveryMode: $("#delivery-mode").value,
  };
  if (payload.deliveryMode === "channel") {
    payload.deliveryChannels = [...$("#delivery-channels").selectedOptions].map((option) => option.value);
    if (!payload.deliveryChannels.length) return showError(new Error("请选择至少一个目标渠道"));
  }
  if (taskType === "once") {
    const value = $("#task-time").value;
    if (!value) return showError(new Error("请选择触发时间"));
    payload.runAt = new Date(value).toISOString();
  } else if (taskType === "interval") {
    payload.intervalSeconds = Number($("#task-interval").value) * 60;
    const start = $("#task-start").value;
    const end = $("#task-end").value;
    const maxRuns = $("#task-max-runs").value;
    if (start) payload.startAt = new Date(start).toISOString();
    if (end) payload.endAt = new Date(end).toISOString();
    if (maxRuns) payload.maxRuns = Number(maxRuns);
  } else {
    payload.cronExpression = $("#task-cron").value.trim();
    payload.timezone = $("#task-timezone").value.trim() || "Asia/Shanghai";
    const start = $("#task-start").value;
    const end = $("#task-end").value;
    const maxRuns = $("#task-max-runs").value;
    if (!payload.cronExpression) return showError(new Error("请填写 Cron 表达式"));
    if (start) payload.startAt = new Date(start).toISOString();
    if (end) payload.endAt = new Date(end).toISOString();
    if (maxRuns) payload.maxRuns = Number(maxRuns);
  }
  try {
    await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#task-content").value = "";
    await loadTasks();
  } catch (error) { showError(error); }
});

const defaultTaskTime = new Date(Date.now() + 5 * 60 * 1000);
defaultTaskTime.setSeconds(0, 0);
$("#task-time").value = new Date(defaultTaskTime.getTime() - defaultTaskTime.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

function toggleSkillPanel(open) {
  skillPanel.classList.toggle("open", open);
  skillPanel.setAttribute("aria-hidden", String(!open));
  $("#skill-backdrop").hidden = !open;
  if (open) loadSkills().catch(showError);
}

async function loadSkillCatalog(force = false) {
  if (state.skillCatalog.length && !force) return state.skillCatalog;
  const data = await api("/api/skills");
  state.skillCatalog = data.skills || [];
  return state.skillCatalog;
}

function skillHealthPresentation(health = {}) {
  const status = health.status || "unverified";
  const labels = {
    ready: "可用",
    needs_configuration: "需配置",
    unavailable: "不可用",
    unverified: "未验证",
  };
  return {
    status,
    label: labels[status] || "待检查",
    summary: health.summary || "尚无体检结果",
    blocked: status === "unavailable" || status === "needs_configuration",
  };
}

async function loadSkills() {
  const skills = await loadSkillCatalog(true);
  const usageData = state.currentSessionId
    ? await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}/skill-usage`)
    : { usage: [] };
  const list = $("#skill-list");
  const select = $("#skill-select");
  list.replaceChildren();
  select.replaceChildren();
  for (const skill of skills) {
    const card = document.createElement("article");
    card.className = "skill-card";
    const heading = document.createElement("div");
    heading.className = "skill-card-heading";
    const name = document.createElement("strong");
    name.textContent = skill.name;
    const health = skillHealthPresentation(skill.health);
    const badge = document.createElement("span");
    badge.className = `skill-health-badge ${health.status}`;
    badge.textContent = health.label;
    badge.title = health.summary;
    heading.append(name, badge);
    const description = document.createElement("p");
    description.textContent = skill.description;
    const healthDetail = document.createElement("small");
    healthDetail.className = "skill-health-detail";
    healthDetail.textContent = health.summary;
    card.classList.toggle("skill-unavailable", health.blocked);
    card.append(heading, description, healthDetail);
    list.append(card);
    const option = document.createElement("option");
    option.value = skill.name;
    option.textContent = `${skill.name} · ${health.label}`;
    option.disabled = health.blocked;
    select.append(option);
  }
  const firstAvailable = [...select.options].find((option) => !option.disabled);
  if (firstAvailable && (!select.value || select.selectedOptions[0]?.disabled)) {
    select.value = firstAvailable.value;
  }
  const submit = $("#skill-form .task-submit");
  if (submit) submit.disabled = !firstAvailable;
  if (
    state.selectedSkill
    && skillHealthPresentation(
      skills.find((item) => item.name === state.selectedSkill.name)?.health,
    ).blocked
  ) {
    setComposerSkill(null);
  }
  renderSkillUsage(usageData.usage);
}

function setComposerSkill(skill) {
  state.selectedSkill = skill || null;
  const chip = $("#composer-skill-chip");
  if (!state.selectedSkill) {
    chip.hidden = true;
    $("#composer-skill-name").textContent = "";
    return;
  }
  $("#composer-skill-name").textContent = state.selectedSkill.name;
  chip.title = state.selectedSkill.description || state.selectedSkill.name;
  chip.hidden = false;
}

function closeComposerAddMenu() {
  const menu = $("#composer-add-menu");
  menu.hidden = true;
  $("#composer-add").setAttribute("aria-expanded", "false");
  $("#composer-skill-menu").hidden = true;
  $("#composer-skill-menu-toggle").setAttribute("aria-expanded", "false");
}

async function renderComposerSkillOptions() {
  const options = $("#composer-skill-options");
  options.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "composer-skill-loading";
  loading.textContent = "正在读取 Skills…";
  options.append(loading);
  try {
    const skills = await loadSkillCatalog();
    options.replaceChildren();
    if (!skills.length) {
      const empty = document.createElement("p");
      empty.className = "composer-skill-loading";
      empty.textContent = "暂无可用 Skill";
      options.append(empty);
      return;
    }
    for (const skill of skills) {
      const health = skillHealthPresentation(skill.health);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "composer-skill-option";
      button.setAttribute("role", "menuitem");
      button.disabled = health.blocked;
      button.classList.toggle("selected", state.selectedSkill?.name === skill.name);
      const name = document.createElement("strong");
      name.textContent = `${skill.name} · ${health.label}`;
      const description = document.createElement("small");
      description.textContent = health.blocked
        ? health.summary
        : (skill.description || "专业工作流");
      button.append(name, description);
      button.onclick = () => {
        setComposerSkill(skill);
        closeComposerAddMenu();
        input.focus();
      };
      options.append(button);
    }
  } catch (error) {
    options.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "composer-skill-loading error";
    failed.textContent = "Skills 加载失败";
    options.append(failed);
    throw error;
  }
}

$("#composer-add").onclick = async () => {
  const menu = $("#composer-add-menu");
  const opening = menu.hidden;
  if (!opening) {
    closeComposerAddMenu();
    return;
  }
  menu.hidden = false;
  $("#composer-add").setAttribute("aria-expanded", "true");
  const hasAttachments = state.isDraft
    ? state.pendingDraftFiles.length > 0
    : Object.keys(state.attachmentMap || {}).length > 0;
  $("#composer-select-attachments").disabled = !hasAttachments;
};

$("#composer-upload-files").onclick = () => {
  closeComposerAddMenu();
  $("#file-input").click();
};

$("#composer-select-attachments").onclick = () => {
  closeComposerAddMenu();
  const strip = $("#attachment-strip");
  if (strip.hidden) return;
  strip.scrollIntoView({ behavior: "smooth", block: "nearest" });
  strip.classList.remove("attention");
  window.requestAnimationFrame(() => strip.classList.add("attention"));
  window.setTimeout(() => strip.classList.remove("attention"), 900);
};

$("#composer-skill-menu-toggle").onclick = async () => {
  const submenu = $("#composer-skill-menu");
  const opening = submenu.hidden;
  submenu.hidden = !opening;
  $("#composer-skill-menu-toggle").setAttribute("aria-expanded", String(opening));
  if (opening) await renderComposerSkillOptions().catch(showError);
};

$("#composer-manage-skills").onclick = () => {
  closeComposerAddMenu();
  toggleSkillPanel(true);
};

$("#composer-skill-clear").onclick = () => {
  setComposerSkill(null);
  input.focus();
};

document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".composer-add-shell")) closeComposerAddMenu();
  if (!event.target.closest("#composer")) closeSlashCommandMenu();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !slashCommandMenu.hidden) {
    closeSlashCommandMenu();
    input.focus();
  }
  if (event.key === "Escape" && !$("#composer-add-menu").hidden) {
    closeComposerAddMenu();
    $("#composer-add").focus();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "u") {
    event.preventDefault();
    closeComposerAddMenu();
    $("#file-input").click();
  }
});

function renderSkillUsage(items) {
  const list = $("#skill-usage");
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "task-empty";
    empty.textContent = "当前 Session 尚未使用 Skill";
    list.append(empty);
    return;
  }
  for (const usage of [...items].reverse()) {
    const card = document.createElement("article");
    card.className = "skill-usage-card";
    const title = document.createElement("strong");
    title.textContent = `${usage.skillName} · ${usage.source} · ${usage.status}`;
    const detail = document.createElement("p");
    detail.textContent = `任务：${usage.task}\n选择原因：${usage.reason || "用户显式调用"}\n保存路径：${usage.savePath || "未保存"}`;
    card.append(title, detail);
    list.append(card);
  }
}

$("#skill-toggle").onclick = () => toggleSkillPanel(true);
$("#skill-close").onclick = () => toggleSkillPanel(false);
$("#skill-backdrop").onclick = () => toggleSkillPanel(false);
$("#skill-refresh").onclick = () => loadSkills().catch(showError);
$("#skill-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const skillName = $("#skill-select").value;
  const task = $("#skill-task").value.trim();
  if (!skillName || !task || !state.currentSessionId) return;
  try {
    const result = await api(`/api/skills/${encodeURIComponent(skillName)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.currentSessionId, task }),
    });
    $("#skill-task").value = "";
    toggleSkillPanel(false);
    await loadSessions(state.currentSessionId);
    if (result.status === "approval_required") await loadApprovals();
  } catch (error) { showError(error); }
});

function toggleTimeline(open) {
  timelinePanel.classList.toggle("open", open);
  timelinePanel.setAttribute("aria-hidden", String(!open));
  $("#timeline-backdrop").hidden = !open;
  if (open) loadTimeline().catch(showError);
}

function toggleStressPanel(open) {
  stressPanel.classList.toggle("open", open);
  stressPanel.setAttribute("aria-hidden", String(!open));
  $("#stress-backdrop").hidden = !open;
  if (open) loadStressReport().catch(showError);
}

function toggleMemoryPanel(open) {
  memoryPanel.classList.toggle("open", open);
  memoryPanel.setAttribute("aria-hidden", String(!open));
  $("#memory-backdrop").hidden = !open;
  if (open) loadMemoryCandidates().catch(showError);
}

async function loadMemoryCandidates() {
  const list = $("#memory-candidate-list");
  if (!state.currentSessionId) return;
  const data = await api(`/api/memory-candidates?sessionId=${encodeURIComponent(state.currentSessionId)}&status=pending`);
  list.replaceChildren();
  if (!data.candidates.length) {
    const empty = document.createElement("p");
    empty.className = "memory-empty";
    empty.textContent = "当前没有等待确认的候选记忆。";
    list.append(empty);
    return;
  }
  const labels = { preference: "偏好", profile: "个人信息", project: "长期项目", course: "课程", fact: "事实" };
  for (const candidate of data.candidates) {
    const card = document.createElement("article");
    card.className = "memory-candidate-card";
    const type = document.createElement("span");
    type.className = "memory-type";
    type.textContent = labels[candidate.type] || candidate.type;
    const content = document.createElement("p");
    content.textContent = candidate.content;
    const source = document.createElement("small");
    source.textContent = `来源：${candidate.sourceText}`;
    const reason = document.createElement("small");
    reason.className = "memory-reason";
    reason.textContent = `${candidate.modelAssisted ? "AI 已整理 · " : ""}${candidate.reason || "检测到稳定信息"}`;
    const conflicts = document.createElement("small");
    conflicts.className = "memory-conflicts";
    const conflictIds = candidate.conflictMemoryIds || [];
    conflicts.hidden = conflictIds.length === 0;
    conflicts.textContent = conflictIds.length
      ? `将替换冲突记忆：${conflictIds.join("、")}`
      : "";
    const actions = document.createElement("div");
    actions.className = "memory-actions";
    const accept = document.createElement("button");
    accept.className = "memory-accept";
    accept.textContent = "记住";
    accept.onclick = () => decideMemoryCandidate(candidate.candidateId, true, card);
    const reject = document.createElement("button");
    reject.className = "memory-reject";
    reject.textContent = "忽略";
    reject.onclick = () => decideMemoryCandidate(candidate.candidateId, false, card);
    const edit = document.createElement("button");
    edit.className = "memory-edit";
    edit.textContent = "编辑";
    edit.onclick = async () => {
      const next = window.prompt("编辑确认后写入的长期记忆", candidate.content);
      if (next === null || !next.trim() || next.trim() === candidate.content) return;
      try {
        await api(`/api/memory-candidates/${encodeURIComponent(candidate.candidateId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: next.trim(), type: candidate.type }),
        });
        await loadMemoryCandidates();
      } catch (error) { showError(error); }
    };
    actions.append(accept, edit, reject);
    card.append(type, content, reason, source, conflicts, actions);
    list.append(card);
  }
}

async function decideMemoryCandidate(candidateId, accepted, card) {
  card.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    await api(`/api/memory-candidates/${encodeURIComponent(candidateId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accepted }),
    });
    await loadMemoryCandidates();
  } catch (error) {
    showError(error);
    card.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
}

async function loadTimeline() {
  const session = await api(`/api/sessions/${encodeURIComponent(state.currentSessionId)}`);
  const activity = session.activity || [];
  const modelCalls = activity.filter((item) => item.type === "model_call");
  const tokens = modelCalls.reduce((sum, item) => sum + Number(item.data.totalTokens || 0), 0);
  const duration = modelCalls.reduce((sum, item) => sum + Number(item.data.durationMs || 0), 0);
  const summary = $("#metrics-summary");
  summary.replaceChildren(
    metricBox(tokens.toLocaleString(), "Token 总量"),
    metricBox(`${Math.round(duration)} ms`, "LLM 延迟"),
    metricBox(String(modelCalls.length), "模型调用"),
  );
  const list = $("#timeline-list");
  list.replaceChildren();
  if (!activity.length) {
    const empty = document.createElement("div");
    empty.className = "task-empty";
    empty.textContent = "当前 Session 暂无 Activity";
    list.append(empty);
    return;
  }
  for (const item of [...activity].reverse()) {
    const event = document.createElement("article");
    event.className = "timeline-event";
    const title = document.createElement("strong");
    const deliveryStatusLabels = {
      delivered: "全部成功",
      partial: "部分成功",
      pending: "等待重试",
      failed: "全部失败",
      unknown: "结果未知",
    };
    const deliveryStatus = item.type === "outbound_delivery" && item.data?.deliveryKind === "broadcast"
      ? deliveryStatusLabels[item.data.status] || item.data.status
      : "";
    title.textContent = deliveryStatus ? `${timelineLabel(item.type)} · ${deliveryStatus}` : timelineLabel(item.type);
    const time = document.createElement("time");
    time.textContent = new Date(item.timestamp).toLocaleString();
    const detail = document.createElement("pre");
    detail.textContent = JSON.stringify(item.data, null, 2);
    event.append(title, time, detail);
    list.append(event);
  }
}

const stressScenarioTitles = {
  oversized_context: "对话即将整理",
  single_huge_message: "单条消息过长",
  tool_timeout: "工具缺少超时保护",
  agent_loop_limit: "工具调用缺少轮次上限",
  attachment_flood: "附件接近数量上限",
  attachment_missing: "附件文件不可用",
  concurrent_session_writes: "多端同时访问",
  protocol_pollution: "检测到内部流程异常",
};

const stressScenarioAdvice = {
  oversized_context: "当前回复结束后，系统会自动整理较早的对话。",
  single_huge_message: "建议将超长内容改为附件，或拆成几条消息发送。",
  tool_timeout: "建议启用 Tool 超时，避免一次调用长时间占住会话。",
  agent_loop_limit: "建议保留单轮循环上限，避免工具被反复调用。",
  attachment_flood: "建议移除当前 Session 中已经不再使用的附件。",
  attachment_missing: "请重新上传缺失文件，或删除已经失效的附件记录。",
  protocol_pollution: "可以先重答上一问；若重复出现，再到运行轨迹中查看详情。",
};

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 ** 2)).toFixed(bytes % (1024 ** 2) ? 1 : 0)} MB`;
}

function readableStressDetail(scenario) {
  const detail = String(scenario?.detail || "");
  if (scenario?.id !== "attachment_flood") return detail;
  // Remain compatible with an already-running older Gateway that returned
  // the attachment ceiling as a raw byte count.
  return detail.replace(
    /单文件上限\s+(\d+)\s*字节/g,
    (_match, bytes) => `单文件上限 ${formatBytes(Number(bytes))}`,
  );
}

function createStressCard(scenario, { technical = false } = {}) {
  const card = document.createElement("article");
  card.className = `stress-card ${scenario.status}${technical ? " technical" : ""}`;
  const mark = document.createElement("span");
  mark.className = "stress-mark";
  mark.textContent = scenario.status === "pass" ? "✓" : scenario.status === "warn" ? "!" : "×";
  const body = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = stressScenarioTitles[scenario.id] || scenario.title;
  const detail = document.createElement("p");
  detail.textContent = readableStressDetail(scenario);
  body.append(title, detail);
  if (!technical && stressScenarioAdvice[scenario.id]) {
    const advice = document.createElement("p");
    advice.className = "stress-card-advice";
    advice.textContent = stressScenarioAdvice[scenario.id];
    body.append(advice);
  }
  card.append(mark, body);
  return card;
}

async function loadStressReport() {
  if (!state.currentSessionId) return;
  const data = await api(`/api/stress/report?sessionId=${encodeURIComponent(state.currentSessionId)}`);
  const scenarios = data.scenarios || [];
  const attention = scenarios.filter((scenario) => scenario.status !== "pass");
  const health = $("#stress-health");
  const healthStatus = attention.some((scenario) => scenario.status === "failed")
    ? "failed"
    : attention.length ? "warn" : "pass";
  health.className = `stress-health ${healthStatus}`;
  health.replaceChildren();
  const healthMark = document.createElement("span");
  healthMark.className = "stress-health-mark";
  healthMark.textContent = healthStatus === "failed" ? "×" : healthStatus === "warn" ? "!" : "✓";
  const healthBody = document.createElement("div");
  const healthTitle = document.createElement("strong");
  healthTitle.textContent = healthStatus === "failed"
    ? "当前会话需要处理"
    : healthStatus === "warn" ? `当前会话有 ${attention.length} 项提醒` : "当前会话运行正常";
  const healthDetail = document.createElement("p");
  healthDetail.textContent = healthStatus === "pass"
    ? "没有发现影响当前对话的风险，可以继续使用。"
    : "下面只列出需要关注的项目，其余检查均已通过。";
  const checkedAt = document.createElement("time");
  checkedAt.className = "stress-checked-at";
  checkedAt.textContent = `刚刚检查 · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  healthBody.append(healthTitle, healthDetail, checkedAt);
  health.append(healthMark, healthBody);

  const summary = $("#stress-summary");
  summary.replaceChildren(
    metricBox(`${data.messages.total}`, "当前消息"),
    metricBox(data.compaction.shouldCompact ? "即将整理" : "充足", "上下文"),
    metricBox(`${data.attachments.count}/${data.attachments.maxPerSession}`, "附件"),
  );

  const list = $("#stress-list");
  list.replaceChildren();
  if (attention.length) {
    for (const scenario of attention) list.append(createStressCard(scenario));
  } else {
    const empty = document.createElement("div");
    empty.className = "stress-empty";
    const mark = document.createElement("span");
    mark.className = "stress-empty-mark";
    mark.textContent = "✓";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "暂无风险项";
    const detail = document.createElement("p");
    detail.textContent = "上下文、附件和工具状态均正常。";
    body.append(title, detail);
    empty.append(mark, body);
    list.append(empty);
  }

  const technicalSummary = $("#stress-technical-summary");
  technicalSummary.replaceChildren();
  const technicalItems = [
    ["对话中可见", `${data.messages.total} 条`, "用户与 SJTUClaw 的正文消息"],
    ["后台共存储", `${data.messages.stored} 条`, "另含 Tool、审批和内部状态记录"],
    ["已注册 Tool", `${data.tools.registered}`, ""],
    ["Tool 超时", data.tools.timeoutEnabled ? `${data.tools.timeoutSeconds} 秒` : "未启用", ""],
    ["单文件上限", formatBytes(data.attachments.maxBytes), ""],
    ["缺失引用", `${data.storage.missingAttachmentReferences}`, ""],
    ["孤儿文件", `${data.storage.orphanBlobs}`, ""],
  ];
  for (const [label, value, hint] of technicalItems) {
    const item = document.createElement("div");
    item.className = "stress-technical-item";
    const copy = document.createElement("div");
    const term = document.createElement("span");
    term.textContent = label;
    copy.append(term);
    if (hint) {
      const note = document.createElement("small");
      note.textContent = hint;
      copy.append(note);
    }
    const definition = document.createElement("strong");
    definition.textContent = value;
    item.append(copy, definition);
    technicalSummary.append(item);
  }
  const technicalList = $("#stress-technical-list");
  technicalList.replaceChildren(...scenarios.map((scenario) => createStressCard(scenario, { technical: true })));
}

function metricBox(value, label) {
  const box = document.createElement("div");
  box.className = "metric-box";
  const strong = document.createElement("strong");
  strong.textContent = value;
  const span = document.createElement("span");
  span.textContent = label;
  box.append(strong, span);
  return box;
}

function timelineLabel(type) {
  return ({
    turn_started: "Turn 开始",
    model_call: "模型调用",
    tool_call: "Tool Call",
    tool_result: "Tool Result",
    approval_required: "等待审批",
    assistant_final: "最终回答",
    turn_paused: "Turn 暂停",
    turn_completed: "Turn 完成",
    approval_resolved: "审批结果",
    compaction_started: "开始整理上下文",
    compaction: "上下文压缩",
    compaction_failed: "上下文整理失败",
    agent_loop_limit: "Agent Loop 达到安全上限",
    skill_activated: "Skill 激活",
    skill_completed: "Skill 完成",
    scheduler_triggered: "定时任务触发",
    outbound_delivery: "主动投递",
    turn_failed: "Turn 失败",
  })[type] || type;
}

$("#timeline-toggle").onclick = () => toggleTimeline(true);
$("#export-session").onclick = () => exportCurrentSession().catch(showError);
$("#timeline-close").onclick = () => toggleTimeline(false);
$("#timeline-backdrop").onclick = () => toggleTimeline(false);
$("#stress-toggle").onclick = () => toggleStressPanel(true);
$("#stress-close").onclick = () => toggleStressPanel(false);
$("#stress-backdrop").onclick = () => toggleStressPanel(false);
$("#stress-refresh").onclick = () => loadStressReport().catch(showError);
$("#memory-toggle").onclick = () => toggleMemoryPanel(true);
$("#memory-close").onclick = () => toggleMemoryPanel(false);
$("#memory-backdrop").onclick = () => toggleMemoryPanel(false);
$("#memory-refresh").onclick = () => loadMemoryCandidates().catch(showError);

checkGatewayCompatibility().then((compatible) => {
  if (compatible) {
    Promise.all([
      loadModelSelection().catch((error) => {
        if (modelSelect) {
          modelSelect.replaceChildren(new Option("重启 Gateway 后可用", ""));
          modelSelect.disabled = true;
          modelSelect.title = error.message || "当前 Gateway 暂不支持模型切换";
        }
      }),
      loadSessions(null, { draft: true }),
    ])
      .then(startSessionRefreshLoop)
      .catch(showError);
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) pollSessionUpdates();
});
window.addEventListener("focus", () => {
  pollSessionUpdates();
});
