/*
 * 桌宠页面交互：同步 Gateway 会话、渲染消息、管理待机动作，
 * 并通过 Electron preload 完成拖动、缩放、托盘和通知操作。
 */
const shell = document.querySelector("#pet-shell");
const petCharacter = document.querySelector("#pet-character");
const card = document.querySelector("#chat-card");
const statusText = document.querySelector("#pet-status");
const sessionTitle = document.querySelector("#pet-session-title");
const sessionSwitch = document.querySelector("#session-switch");
const sessionPanel = document.querySelector("#pet-session-panel");
const sessionList = document.querySelector("#pet-session-list");
const newSessionButton = document.querySelector("#pet-new-session");
const stateLabel = document.querySelector("#state-label");
const messages = document.querySelector("#pet-messages");
const input = document.querySelector("#pet-input");
const form = document.querySelector("#pet-form");
const effects = document.querySelector("#effects");
const resizeHandles = [...document.querySelectorAll("[data-resize-edge]")];

let sessionId = localStorage.getItem("sjtuclaw-pet-session");
let busy = false;
let externalBusy = false;
let clickTimer = null;
let ignorePetClickUntil = 0;
let idleActionTimer = null;
let lastReactionIndex = -1;
let mousePassthrough = false;
const USER_NAME_STORAGE_KEY = "sjtuclaw.displayName.user";
const petCitationMap = {};
const IDLE_NAP_DELAY_MS = 18_000;
const CLICK_REACTIONS = [
  {
    className: "pet-react-wave",
    label: () => {
      const name = localStorage.getItem(USER_NAME_STORAGE_KEY)?.trim();
      return name ? `嗨，${name}` : "嗨，你好";
    },
    icon: "✨",
    duration: 1_500,
  },
  { className: "pet-react-stretch", label: "伸个懒腰", icon: "☀️", duration: 1_650 },
  { className: "pet-react-think", label: "让我想想", icon: "💭", duration: 1_800 },
  { className: "pet-react-cheer", label: "今天也加油", icon: "★", duration: 1_500 },
  { className: "pet-react-pat", label: "摸摸很舒服", icon: "♡", duration: 1_350 },
  { className: "pet-react-hop", label: "轻轻蹦跶", icon: "♪", duration: 1_200 },
  { className: "pet-react-peek", label: "左看看右看看", icon: "·", duration: 1_700 },
  { className: "pet-react-twirl", label: "开心转一圈", icon: "✦", duration: 1_450 },
  { className: "pet-react-tail", label: "尾巴摇摇", icon: "〜", duration: 1_600 },
  { className: "pet-react-highfive", label: "击个掌吧", icon: "✋", duration: 1_400 },
  { className: "pet-react-clockin", label: "打卡开工", icon: "⌨", duration: 1_800 },
  { className: "pet-react-bow", label: "请多关照", icon: "❀", duration: 1_550 },
  { className: "pet-react-startle", label: "吓一跳", icon: "！", duration: 1_250 },
  { className: "pet-react-nod", label: "点点头", icon: "✓", duration: 1_450 },
  { className: "pet-react-shimmy", label: "跟着节拍摇", icon: "♪", duration: 1_650 },
  { className: "pet-react-dash", label: "冲刺一下", icon: "➤", duration: 1_350 },
  { className: "pet-react-heart", label: "送你一颗心", icon: "♥", duration: 1_550 },
  { className: "pet-react-curious", label: "歪头看看", icon: "？", duration: 1_700 },
];
const PET_ACTION_CLASSES = [
  "pet-idle-nap",
  ...CLICK_REACTIONS.map((action) => action.className),
];

function enableWindowDrag(element, onDragged = () => {}, allowInteractive = false) {
  let dragging = false;
  let dragMoveFrame = null;
  let startPoint = null;
  let moved = false;

  element.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || (!allowInteractive && event.target.closest("button,.no-drag"))) return;
    dragging = true;
    moved = false;
    startPoint = { x: event.clientX, y: event.clientY };
    element.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  element.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    if ((event.buttons & 1) === 0) { finish(event); return; }
    if (!moved && startPoint && Math.hypot(event.clientX - startPoint.x, event.clientY - startPoint.y) > 4) {
      moved = true;
      element.classList.add("is-dragging");
      window.sjtuPet?.beginDrag({ x: event.screenX, y: event.screenY });
    }
    if (!moved || dragMoveFrame !== null) return;
    const screenPoint = { x: event.screenX, y: event.screenY };
    dragMoveFrame = requestAnimationFrame(() => {
      dragMoveFrame = null;
      if (dragging) window.sjtuPet?.dragTo(screenPoint);
    });
  });

  const finish = (event) => {
    if (!dragging) return;
    if (dragMoveFrame !== null) {
      cancelAnimationFrame(dragMoveFrame);
      dragMoveFrame = null;
    }
    dragging = false;
    element.classList.remove("is-dragging");
    if (moved) window.sjtuPet?.endDrag();
    if (moved) onDragged();
    startPoint = null;
    if (element.hasPointerCapture?.(event.pointerId)) element.releasePointerCapture(event.pointerId);
  };

  document.addEventListener("pointerup", finish);
  element.addEventListener("lostpointercapture", () => {
    if (!dragging) return;
    if (dragMoveFrame !== null) {
      cancelAnimationFrame(dragMoveFrame);
      dragMoveFrame = null;
    }
    dragging = false;
    element.classList.remove("is-dragging");
    startPoint = null;
    if (moved) window.sjtuPet?.endDrag();
    if (moved) onDragged();
    // Windows 上移动透明 BrowserWindow 时可能丢失 pointer capture；
    // 主进程会继续轮询原生鼠标位置，直到 pointerup/blur。
  });
  window.addEventListener("blur", () => {
    if (!dragging) return;
    if (dragMoveFrame !== null) {
      cancelAnimationFrame(dragMoveFrame);
      dragMoveFrame = null;
    }
    dragging = false;
    element.classList.remove("is-dragging");
    startPoint = null;
    if (moved) window.sjtuPet?.endDrag();
    if (moved) onDragged();
  });
}

function enableWindowResize(element) {
  if (!element) return;
  let resizing = false;
  element.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    resizing = true;
    element.setPointerCapture(event.pointerId);
    window.sjtuPet?.beginResize(element.dataset.resizeEdge);
    event.preventDefault();
    event.stopPropagation();
  });
  const finish = (event) => {
    if (!resizing) return;
    resizing = false;
    window.sjtuPet?.endResize();
    if (element.hasPointerCapture?.(event.pointerId)) element.releasePointerCapture(event.pointerId);
  };
  document.addEventListener("pointerup", finish);
  element.addEventListener("lostpointercapture", () => {
    if (!resizing) return;
    // Resizing a transparent BrowserWindow can move the active edge out from
    // under the pointer and make Chromium drop pointer capture. Keep the main
    // process resize session alive; a real pointerup ends it immediately, and
    // the native cursor-idle fallback ends it after a short grace period.
  });
}

function setMinimized(value) {
  shell.classList.toggle("minimized", value);
  shell.classList.remove(...PET_ACTION_CLASSES);
  resetIdleActionTimer();
}

function setState(state, label) {
  shell.classList.remove(...PET_ACTION_CLASSES);
  shell.dataset.state = state;
  stateLabel.textContent = label;
  statusText.textContent = label;
  resetIdleActionTimer();
}

function clearIdleActionTimer() {
  if (idleActionTimer !== null) {
    clearTimeout(idleActionTimer);
    idleActionTimer = null;
  }
}

function runIdleAction() {
  idleActionTimer = null;
  if (busy || shell.dataset.state !== "idle" || shell.classList.contains("minimized") || !card.hidden) {
    resetIdleActionTimer();
    return;
  }
  shell.classList.remove(...PET_ACTION_CLASSES);
  void shell.offsetWidth;
  shell.classList.add("pet-idle-nap");
  stateLabel.textContent = "眯一会儿";
  statusText.textContent = "正在休息";
}

function resetIdleActionTimer() {
  clearIdleActionTimer();
  if (busy || shell.dataset.state !== "idle" || shell.classList.contains("minimized") || !card.hidden) return;
  idleActionTimer = setTimeout(runIdleAction, IDLE_NAP_DELAY_MS);
}

function wakeFromNap() {
  if (!shell.classList.contains("pet-idle-nap")) return;
  shell.classList.remove("pet-idle-nap");
  if (!busy && shell.dataset.state === "idle") {
    stateLabel.textContent = "待命";
    statusText.textContent = "准备好了";
  }
}

function activeTurnLabel(turn) {
  if (!turn) return "努力工作中";
  if (turn.phase === "approval_required") return "等待网页审批";
  if (turn.phase === "tool_call") return "正在使用工具";
  if (turn.phase === "compaction") return "整理上下文";
  return turn.message || "努力工作中";
}

async function pollAgentActivity() {
  if (busy) return;
  try {
    const response = await fetch("/api/turns/active", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    const turns = Array.isArray(payload.turns) ? payload.turns : [];
    if (turns.length) {
      externalBusy = true;
      setState("working", activeTurnLabel(turns[turns.length - 1]));
    } else if (externalBusy) {
      externalBusy = false;
      setState("idle", "待命");
    }
  } catch {
    // Gateway 健康探针会单独处理离线状态，这里静默等待下次轮询。
  }
}

function expand(value) {
  setMousePassthrough(false);
  card.hidden = !value;
  petCharacter.hidden = value;
  window.sjtuPet?.setExpanded(value);
  resetIdleActionTimer();
  if (value) {
    if (sessionId) void loadPetSession(sessionId, { closePanel: false });
    setTimeout(() => input.focus(), 180);
  } else {
    sessionPanel.hidden = true;
  }
}

function setMousePassthrough(enabled, { force = false } = {}) {
  const next = Boolean(enabled);
  if (!force && mousePassthrough === next) return;
  mousePassthrough = next;
  window.sjtuPet?.setMousePassthrough(next);
}

function pointInEllipse(x, y, centerX, centerY, radiusX, radiusY) {
  const dx = (x - centerX) / radiusX;
  const dy = (y - centerY) / radiusY;
  return dx * dx + dy * dy <= 1;
}

function isPetInteractivePoint(clientX, clientY) {
  if (!card.hidden) return true;
  if (shell.classList.contains("minimized")) {
    const controlsRect = document.querySelector("#compact-controls").getBoundingClientRect();
    return clientX >= controlsRect.left && clientX <= controlsRect.right
      && clientY >= controlsRect.top && clientY <= controlsRect.bottom;
  }
  const labelRect = stateLabel.getBoundingClientRect();
  if (clientX >= labelRect.left && clientX <= labelRect.right
      && clientY >= labelRect.top && clientY <= labelRect.bottom) return true;

  const spriteRect = document.querySelector(".pet-sprite").getBoundingClientRect();
  const x = clientX - spriteRect.left;
  const y = clientY - spriteRect.top;
  // Approximate the visible work-cat silhouette rather than the rectangular
  // transparent sprite cell. The ellipses intentionally overlap and include
  // a small margin so clicking and dragging the ears, body or tail stays easy.
  return pointInEllipse(x, y, 87, 52, 65, 51)
    || pointInEllipse(x, y, 84, 117, 58, 65)
    || pointInEllipse(x, y, 149, 124, 27, 42);
}

document.addEventListener("mousemove", (event) => {
  setMousePassthrough(!isPetInteractivePoint(event.clientX, event.clientY));
}, { passive: true });

resizeHandles.forEach(enableWindowResize);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function normalizeAssistantText(text) {
  const trimmed = String(text ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
    try {
      const payload = JSON.parse(trimmed);
      if (payload?.type === "final" && typeof payload.content === "string") return payload.content;
    } catch {
      // 普通文本，不处理。
    }
  }
  return String(text ?? "");
}

function inlineMarkdown(value) {
  let output = escapeHtml(value);
  output = output.replace(/`([^`]+)`/g, "<code>$1</code>");
  output = output.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  output = output.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  output = output.replace(/\[(W\d+)\]/g, (marker, label) => {
    const citation = petCitationMap[`[${label}]`];
    if (!citation?.url) return marker;
    const title = citation.title || citation.url;
    return `<a class="pet-citation-chip" href="${escapeHtml(citation.url)}" `
      + `title="${escapeHtml(title)}" target="_blank" rel="noopener noreferrer">${marker}</a>`;
  });
  return output;
}

function collectPetCitations(result) {
  const output = result?.output || result || {};
  const citations = Array.isArray(output.citations) && output.citations.length
    ? output.citations
    : Array.isArray(output.results) ? output.results : [];
  citations.forEach((citation, index) => {
    const label = citation?.label || `[W${index + 1}]`;
    if (!/^\[W\d+\]$/.test(label) || !citation?.url) return;
    petCitationMap[label] = {
      url: String(citation.url),
      title: String(citation.title || citation.url),
    };
  });
}

function normalizeCollapsedMarkdownTables(source) {
  let inCode = false;
  return String(source || "").replace(/\r/g, "").split("\n").map((line) => {
    if (line.trimStart().startsWith("```")) {
      inCode = !inCode;
      return line;
    }
    if (inCode) return line;
    if (!/\|\s*:?-{2,}:?\s*\|/.test(line) || !/\|\s*\|/.test(line)) return line;
    return line.replace(/\|\s*\|/g, "|\n|");
  }).join("\n");
}

function renderMarkdown(source) {
  const lines = normalizeCollapsedMarkdownTables(normalizeAssistantText(source)).split("\n");
  const output = [];
  let inCode = false;
  let code = [];
  let listType = null;
  let listItem = "";
  let paragraph = [];

  const smartJoin = (parts) => parts.reduce((text, part) => {
    if (!text) return part;
    const startsWithPunctuation = /^[，。！？；：、）】》,.!?;:]/.test(part);
    const joinsChinese = /[\u3400-\u9fff]$/.test(text) && /^[\u3400-\u9fff]/.test(part);
    return `${text}${startsWithPunctuation || joinsChinese ? "" : " "}${part}`;
  }, "");
  const closeParagraph = () => {
    if (!paragraph.length) return;
    output.push(`<p>${inlineMarkdown(smartJoin(paragraph))}</p>`);
    paragraph = [];
  };
  const closeListItem = () => {
    if (!listItem) return;
    output.push(`<li>${inlineMarkdown(listItem)}</li>`);
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
      output.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"))}</blockquote>`);
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
      const head = headers.map((cell, index) => `<th style="text-align:${alignments[index] || "left"}">${inlineMarkdown(cell)}</th>`).join("");
      const body = rows.map((row) => `<tr>${headers.map((_, index) => `<td style="text-align:${alignments[index] || "left"}">${inlineMarkdown(row[index] || "")}</td>`).join("")}</tr>`).join("");
      output.push(`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeParagraph();
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
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
  const text = String(value || "");
  return text.replace(/\$\$([\s\S]*?)\$\$/g, (match, body) =>
    `$$${body.replace(/\\\(/g, "").replace(/\\\)/g, "").replace(/\\\[/g, "").replace(/\\\]/g, "")}$$`
  );
}

function setMessageContent(item, text) {
  const content = item.querySelector(".pet-message-content");
  if (!content) return;
  content.innerHTML = renderMarkdown(normalizeMathSource(text));
  renderMath(content);
}

function append(role, text) {
  messages.querySelector(".welcome")?.remove();
  const item = document.createElement("div");
  item.className = `pet-message ${role}`;
  const label = document.createElement("strong");
  label.className = "pet-message-role";
  label.textContent = role === "user"
    ? (localStorage.getItem(USER_NAME_STORAGE_KEY)?.trim() || "你")
    : "SJTUClaw";
  const content = document.createElement("div");
  content.className = "pet-message-content";
  item.append(label, content);
  messages.append(item);
  setMessageContent(item, text);
  messages.scrollTop = messages.scrollHeight;
  return item;
}

function updatePetCompactionNotice(current, eventName, payload = {}) {
  const notice = current || document.createElement("section");
  notice.className = `pet-compaction ${eventName === "compaction_started" ? "pending" : eventName === "compaction_failed" ? "failed" : "success"}`;
  notice.replaceChildren();

  const heading = document.createElement("strong");
  const oldMessages = Number(payload.oldMessages || 0);
  const recentMessages = Number(payload.recentMessages || 0);
  if (eventName === "compaction_started") {
    heading.textContent = "↻ 正在整理上下文";
  } else if (eventName === "compaction_failed") {
    heading.textContent = "! 上下文整理失败";
  } else {
    heading.textContent = "✓ 上下文整理完成";
  }
  notice.append(heading);

  const detail = document.createElement("span");
  if (eventName === "compaction") {
    detail.textContent = oldMessages
      ? `已整理 ${oldMessages} 条旧消息，保留最近 ${recentMessages} 条。`
      : "后续对话将使用更新后的摘要。";
  } else if (eventName === "compaction_failed") {
    detail.textContent = payload.error || "原消息已保留，可以稍后重试。";
  } else {
    detail.textContent = "对话较长，正在生成摘要；当前回答已经完成。";
  }
  notice.append(detail);

  if (eventName === "compaction" && payload.summaryPreview) {
    const preview = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "查看摘要";
    const content = document.createElement("div");
    content.className = "pet-compaction-preview";
    content.innerHTML = renderMarkdown(payload.summaryPreview);
    renderMath(content);
    preview.append(summary, content);
    notice.append(preview);
  }

  if (!current) messages.append(notice);
  messages.scrollTop = messages.scrollHeight;
  return notice;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error || `请求失败 (${response.status})`);
  return body;
}

function clearPetCitationMap() {
  Object.keys(petCitationMap).forEach((key) => delete petCitationMap[key]);
}

function visiblePetMessages(session) {
  const protocolPrefix = /^\[(?:tool_calls?|tool_results?|approval_[^\]]+|protocol_error|deferred_action_promise)\]/;
  return (session?.messages || []).filter((item) => {
    if (!["user", "assistant"].includes(item?.role)) return false;
    if (item.metadata?.internal) return false;
    const content = String(item.content || "").trim();
    return content && !protocolPrefix.test(content);
  }).slice(-30);
}

function renderPetHistory(session) {
  messages.replaceChildren();
  clearPetCitationMap();
  const history = visiblePetMessages(session);
  if (!history.length) {
    const welcome = document.createElement("p");
    welcome.className = "welcome";
    welcome.textContent = "今天想一起做点什么？";
    messages.append(welcome);
    return;
  }
  history.forEach((item) => append(item.role, normalizeAssistantText(item.content)));
  messages.scrollTop = messages.scrollHeight;
}

async function loadPetSession(targetSessionId, options = {}) {
  if (!targetSessionId) return null;
  const session = await api(`/api/sessions/${encodeURIComponent(targetSessionId)}`);
  sessionId = session.sessionId;
  localStorage.setItem("sjtuclaw-pet-session", sessionId);
  sessionTitle.textContent = session.title || "SJTUClaw";
  sessionTitle.title = session.title || "";
  renderPetHistory(session);
  if (options.closePanel !== false) sessionPanel.hidden = true;
  return session;
}

function renderPetSessionList(items = []) {
  sessionList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "pet-session-empty";
    empty.textContent = "还没有会话";
    sessionList.append(empty);
    return;
  }
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `pet-session-item${item.sessionId === sessionId ? " active" : ""}`;
    const title = document.createElement("strong");
    title.textContent = item.title || item.sessionId;
    title.title = item.title || item.sessionId;
    const count = document.createElement("small");
    count.textContent = `${Number(item.messageCount || 0)} 条`;
    button.append(title, count);
    button.onclick = async () => {
      if (busy) {
        setState("working", "本轮完成后再切换会话");
        return;
      }
      try {
        await loadPetSession(item.sessionId);
      } catch (error) {
        setState("error", error.message);
      }
    };
    sessionList.append(button);
  });
}

async function openPetSessionPanel() {
  if (busy) {
    setState("working", "本轮完成后再切换会话");
    return;
  }
  sessionPanel.hidden = !sessionPanel.hidden;
  if (sessionPanel.hidden) return;
  sessionList.innerHTML = '<p class="pet-session-empty">正在加载…</p>';
  try {
    const data = await api("/api/sessions");
    renderPetSessionList(data.sessions || []);
  } catch (error) {
    sessionList.innerHTML = `<p class="pet-session-empty">${escapeHtml(error.message)}</p>`;
  }
}

async function ensureSession() {
  if (sessionId) {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (response.ok) {
      const session = await response.json();
      sessionTitle.textContent = session.title || "SJTUClaw";
      return sessionId;
    }
  }
  const session = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "桌宠会话" }),
  });
  sessionId = session.sessionId;
  localStorage.setItem("sjtuclaw-pet-session", sessionId);
  sessionTitle.textContent = session.title || "桌宠会话";
  return sessionId;
}

async function consumeSse(response, onEvent) {
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
      let name = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) onEvent(name, JSON.parse(data.join("\n")));
    }
    if (done) break;
  }
}

async function send(text) {
  busy = true;
  append("user", text);
  setState("working", "开始工作");
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId: await ensureSession(), message: text }),
  });
  if (!response.ok) throw new Error("Gateway 请求失败");
  let answer = "";
  let answerNode = null;
  let compactionNotice = null;
  await consumeSse(response, (name, payload) => {
    if (name === "status") setState("working", payload.message || "努力工作中");
    if (name === "tool_call") setState("working", `正在使用 ${payload.tool}`);
    if (name === "tool_result") collectPetCitations(payload.result);
    if (name === "approval_required") setState("working", "等待网页审批");
    if (["compaction_started", "compaction", "compaction_failed"].includes(name)) {
      compactionNotice = updatePetCompactionNotice(compactionNotice, name, payload);
    }
    if (name === "assistant_reset") {
      answer = "";
      answerNode?.remove();
      answerNode = null;
    }
    if (name === "assistant_delta") {
      setState("replying", "正在回复");
      answer += payload.delta || "";
      if (!answerNode) answerNode = append("assistant", "");
      setMessageContent(answerNode, answer);
      messages.scrollTop = messages.scrollHeight;
    }
    if (name === "done" && !answer && payload.reply) {
      answer = payload.reply;
      answerNode = append("assistant", answer);
    }
  });
  busy = false;
  setState("idle", "待命");
  if (answer) {
    window.sjtuPet?.notify({ title: "SJTUClaw", body: answer });
  }
}

function react() {
  const reactionIndex = lastReactionIndex < 0
    ? Math.floor(Math.random() * CLICK_REACTIONS.length)
    : (lastReactionIndex + 1 + Math.floor(Math.random() * (CLICK_REACTIONS.length - 1)))
      % CLICK_REACTIONS.length;
  lastReactionIndex = reactionIndex;
  const reaction = CLICK_REACTIONS[reactionIndex];
  shell.classList.remove(...PET_ACTION_CLASSES);
  resetIdleActionTimer();
  void shell.offsetWidth;
  shell.classList.add(reaction.className);
  effects.replaceChildren();
  for (let index = 0; index < 6; index += 1) {
    const particle = document.createElement("span");
    particle.textContent = reaction.icon;
    effects.append(particle);
  }
  if (!busy) {
    stateLabel.textContent = typeof reaction.label === "function"
      ? reaction.label()
      : reaction.label;
  }
  setTimeout(() => {
    shell.classList.remove(reaction.className);
    effects.replaceChildren();
    if (!busy) setState("idle", "待命");
  }, reaction.duration);
}

function hidePet() {
  window.sjtuPet?.hide();
}

petCharacter.onclick = () => {
  if (Date.now() < ignorePetClickUntil) return;
  clearTimeout(clickTimer);
  clickTimer = setTimeout(react, 220);
};
petCharacter.ondblclick = () => {
  clearTimeout(clickTimer);
  expand(true);
};
petCharacter.oncontextmenu = (event) => {
  event.preventDefault();
  clearTimeout(clickTimer);
  window.sjtuPet?.showContextMenu();
};

document.querySelector("#collapse").onclick = () => expand(false);
document.querySelector("#open-compact").onclick = () => { setMinimized(false); expand(true); };
document.querySelector("#close-compact").onclick = () => window.sjtuPet?.quit();
document.querySelector("#close-expanded").onclick = hidePet;
document.querySelector("#open-web").onclick = () => window.sjtuPet?.openWeb();
sessionSwitch.onclick = () => void openPetSessionPanel();
newSessionButton.onclick = async () => {
  if (busy) return;
  newSessionButton.disabled = true;
  try {
    const session = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "桌宠会话" }),
    });
    await loadPetSession(session.sessionId);
  } catch (error) {
    setState("error", error.message);
  } finally {
    newSessionButton.disabled = false;
  }
};
document.addEventListener("pointerdown", (event) => {
  if (sessionPanel.hidden) return;
  if (sessionPanel.contains(event.target) || sessionSwitch.contains(event.target)) return;
  sessionPanel.hidden = true;
});

// The chat header and compact drag handle use Electron's native drag regions.
// This avoids the pointerup loss and post-release drift seen when polling the
// cursor while a transparent BrowserWindow is moving.
enableWindowDrag(petCharacter, () => {
  ignorePetClickUntil = Date.now() + 300;
  clearTimeout(clickTimer);
}, true);

for (const eventName of ["pointerdown", "keydown", "input"]) {
  document.addEventListener(eventName, () => {
    wakeFromNap();
    resetIdleActionTimer();
  }, { passive: true });
}

// 聊天输入采用常见的聊天软件习惯：Enter 发送，Ctrl/⌘+Enter 换行。
// 中文输入法组合期间不拦截 Enter，避免候选词还没确认就被发送。
input.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
  if (event.ctrlKey || event.metaKey || event.shiftKey) {
    event.preventDefault();
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.value = `${input.value.slice(0, start)}\n${input.value.slice(end)}`;
    input.selectionStart = start + 1;
    input.selectionEnd = start + 1;
    return;
  }
  event.preventDefault();
  if (!busy && input.value.trim()) form.requestSubmit();
});

window.sjtuPet?.onMinimized(setMinimized);
window.sjtuPet?.onGatewayStatus((healthy) => {
  if (busy || externalBusy) return;
  setState(healthy ? "idle" : "offline", healthy ? "待命" : "Gateway 连接中断");
});

// The native Electron window survives Gateway restarts while this renderer is
// replaced.  Force an initial handshake instead of trusting the new page's
// default variable, otherwise an old pass-through=true can make the pet look
// present but ignore every click and drag.
setMousePassthrough(false, { force: true });
form.onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try {
    await send(text);
  } catch (error) {
    append("assistant", `出错了：${error.message}`);
    setState("error", "连接失败");
    busy = false;
  }
};

setState("idle", "待命");
void pollAgentActivity();
setInterval(() => void pollAgentActivity(), 1_500);
