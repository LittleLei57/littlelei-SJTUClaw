/*
 * Electron 主进程：创建透明桌宠窗口，约束拖动/缩放范围，
 * 管理托盘菜单，并把系统级操作通过 IPC 提供给网页层。
 */
const { app, BrowserWindow, ipcMain, Menu, nativeImage, Notification, screen, shell, Tray } = require("electron");
const fs = require("fs");
const path = require("path");

const GATEWAY = (process.env.SJTUCLAW_GATEWAY || "http://127.0.0.1:8000").replace(/\/+$/, "");
const APP_ID = "cn.sjtu.sjtuclaw.desktop-pet";
let petWindow = null;
let tray = null;
let quitting = false;
let dragState = null;
let resizeState = null;
let resizeTimer = null;
let gatewayProbeTimer = null;
let mouseRecoveryTimer = null;
let gatewayHealthy = null;
let gatewayLoadInFlight = false;
// Electron keeps ignore-mouse-events on the native BrowserWindow even when
// the renderer is reloaded.  Track it in the main process so a Gateway reload
// cannot leave a visible-but-unclickable pet behind.
let mousePassthroughEnabled = false;
const RESIZE_IDLE_TIMEOUT_MS = 1000;
const GATEWAY_HEALTH_INTERVAL_MS = 5000;
const PET_SIZE = { width: 190, height: 230 };
const MINIMIZED_SIZE = { width: 190, height: 50 };
const DEFAULT_CHAT_SIZE = { width: 390, height: 540 };
const MIN_CHAT_SIZE = { width: 340, height: 380 };
// `floating` can still be covered by some top-level Windows applications.
// `screen-saver` maps to a stronger topmost layer while keeping the pet as a
// normal, non-focus-stealing window.
const PET_ALWAYS_ON_TOP_LEVEL = "screen-saver";
let expandedMode = false;
let savedChatSize = { ...DEFAULT_CHAT_SIZE };
let saveWindowTimer = null;

if (process.platform === "win32") app.setAppUserModelId(APP_ID);

function gatewayHealthUrl() {
  return `${GATEWAY}/api/health`;
}

function sendGatewayStatus(healthy) {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.webContents.send("pet:gateway-status", { healthy, gateway: GATEWAY });
  }
}

function loadGatewayPage() {
  if (!petWindow || petWindow.isDestroyed() || gatewayLoadInFlight || quitting) return;
  gatewayLoadInFlight = true;
  petWindow.loadURL(`${GATEWAY}/pet.html`).catch(() => {}).finally(() => {
    gatewayLoadInFlight = false;
  });
}

async function probeGateway() {
  if (quitting || !petWindow || petWindow.isDestroyed()) return;
  let healthy = false;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2500);
    const response = await fetch(gatewayHealthUrl(), { signal: controller.signal });
    clearTimeout(timeout);
    healthy = response.ok;
  } catch (_error) {
    healthy = false;
  }
  if (gatewayHealthy !== healthy) {
    sendGatewayStatus(healthy);
    // A Gateway restart should recover the pet without requiring the user to
    // restart Electron.  Only reload on a false -> true transition; repeated
    // failures otherwise would create a reload storm while Gateway is down.
    if (healthy && gatewayHealthy === false) loadGatewayPage();
    gatewayHealthy = healthy;
  }
}

function startGatewayProbe() {
  if (gatewayProbeTimer) clearInterval(gatewayProbeTimer);
  void probeGateway();
  gatewayProbeTimer = setInterval(() => void probeGateway(), GATEWAY_HEALTH_INTERVAL_MS);
  gatewayProbeTimer.unref?.();
}

function supportsAutoStart() {
  return process.platform === "win32" || process.platform === "darwin";
}

function autoStartEnabled() {
  if (!supportsAutoStart()) return false;
  try {
    return Boolean(app.getLoginItemSettings().openAtLogin);
  } catch (_error) {
    return false;
  }
}

function setAutoStart(enabled) {
  if (!supportsAutoStart()) return false;
  try {
    const args = app.isPackaged ? [] : [app.getAppPath()];
    app.setLoginItemSettings({ openAtLogin: Boolean(enabled), path: process.execPath, args });
    rebuildTrayMenu();
    return autoStartEnabled() === Boolean(enabled);
  } catch (_error) {
    return false;
  }
}

function showNotification(title, body) {
  if (!Notification?.isSupported?.()) return;
  if (petWindow && !petWindow.isDestroyed() && petWindow.isVisible() && petWindow.isFocused()) return;
  const message = String(body || "").replace(/\s+/g, " ").trim().slice(0, 220);
  if (!message) return;
  new Notification({ title: String(title || "SJTUClaw"), body: message }).show();
}

function keepPetOnTop() {
  if (!petWindow || petWindow.isDestroyed()) return;
  petWindow.setAlwaysOnTop(true, PET_ALWAYS_ON_TOP_LEVEL);
  // Bring it to the front of the topmost group without activating the window.
  petWindow.moveTop?.();
}

function showPetWindow() {
  if (!petWindow || petWindow.isDestroyed()) return;
  setMousePassthrough(false, { force: true });
  keepPetOnTop();
  petWindow.showInactive();
  keepPetOnTop();
}

function dragPoint(payload) {
  const x = Number(payload?.x);
  const y = Number(payload?.y);
  if (Number.isFinite(x) && Number.isFinite(y)) return { x, y };
  return screen.getCursorScreenPoint();
}

function beginDrag(payload) {
  if (!petWindow || petWindow.isDestroyed()) return;
  // Dragging must always own the mouse until the gesture finishes.
  setMousePassthrough(false, { force: true });
  const point = dragPoint(payload);
  const [windowX, windowY] = petWindow.getPosition();
  dragState = {
    mouseX: point.x, mouseY: point.y, windowX, windowY,
  };
}

function moveDrag(payload) {
  if (!dragState || !petWindow || petWindow.isDestroyed()) return;
  const point = dragPoint(payload);
  const [width, height] = petWindow.getSize();
  const desiredX = Math.round(dragState.windowX + point.x - dragState.mouseX);
  const desiredY = Math.round(dragState.windowY + point.y - dragState.mouseY);
  const { x, y } = clampWindowPosition(desiredX, desiredY, width, height);
  const [currentX, currentY] = petWindow.getPosition();
  if (x !== currentX || y !== currentY) petWindow.setPosition(x, y, false);
}

/**
 * Keep the pet inside the current display whenever possible.  If a window is
 * larger than the work area, keep a generous visible strip so that the drag
 * surface can always be reached again (especially after expanding near the
 * top edge of the screen).
 */
function clampWindowPosition(x, y, width, height) {
  const display = screen.getDisplayNearestPoint({ x: x + width / 2, y: y + height / 2 });
  const area = display.workArea;
  const inset = 8;
  const minimumVisible = 96;
  const clampAxis = (value, start, size, windowSize) => {
    const fullyVisibleMax = start + size - windowSize - inset;
    const fullyVisibleMin = start + inset;
    if (fullyVisibleMax >= fullyVisibleMin) {
      return Math.min(Math.max(value, fullyVisibleMin), fullyVisibleMax);
    }
    const partiallyVisibleMax = start + size - minimumVisible;
    return Math.min(Math.max(value, fullyVisibleMin), partiallyVisibleMax);
  };
  return {
    x: clampAxis(x, area.x, area.width, width),
    y: clampAxis(y, area.y, area.height, height),
  };
}

function endDrag() {
  dragState = null;
}

function beginResize(edge) {
  if (!expandedMode || !petWindow || petWindow.isDestroyed()) return;
  const normalizedEdge = String(edge || "").toLowerCase();
  if (!/^(n|s|e|w|ne|nw|se|sw)$/.test(normalizedEdge)) return;
  const point = screen.getCursorScreenPoint();
  const bounds = petWindow.getBounds();
  const display = screen.getDisplayNearestPoint(point);
  resizeState = {
    edge: normalizedEdge,
    mouseX: point.x, mouseY: point.y,
    x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
    workArea: display.workArea,
    lastCursorX: point.x, lastCursorY: point.y, lastMovementAt: Date.now(),
  };
  if (resizeTimer) clearInterval(resizeTimer);
  resizeTimer = setInterval(moveResize, 16);
  resizeTimer.unref?.();
}

function moveResize() {
  if (!resizeState || !expandedMode || !petWindow || petWindow.isDestroyed()) return;
  const point = screen.getCursorScreenPoint();
  const now = Date.now();
  const cursorChanged = point.x !== resizeState.lastCursorX || point.y !== resizeState.lastCursorY;
  if (!cursorChanged) {
    if (now - resizeState.lastMovementAt >= RESIZE_IDLE_TIMEOUT_MS) endResize();
    return;
  }
  resizeState.lastCursorX = point.x;
  resizeState.lastCursorY = point.y;
  resizeState.lastMovementAt = now;
  const dx = point.x - resizeState.mouseX;
  const dy = point.y - resizeState.mouseY;
  const edge = resizeState.edge;
  const area = resizeState.workArea;
  const inset = 8;
  const areaLeft = area.x + inset;
  const areaTop = area.y + inset;
  const areaRight = area.x + area.width - inset;
  const areaBottom = area.y + area.height - inset;
  const initialRight = resizeState.x + resizeState.width;
  const initialBottom = resizeState.y + resizeState.height;
  let left = resizeState.x;
  let top = resizeState.y;
  let right = initialRight;
  let bottom = initialBottom;

  if (edge.includes("e")) {
    right = Math.min(areaRight, Math.max(left + MIN_CHAT_SIZE.width, initialRight + dx));
  }
  if (edge.includes("w")) {
    left = Math.max(areaLeft, Math.min(right - MIN_CHAT_SIZE.width, resizeState.x + dx));
  }
  if (edge.includes("s")) {
    bottom = Math.min(areaBottom, Math.max(top + MIN_CHAT_SIZE.height, initialBottom + dy));
  }
  if (edge.includes("n")) {
    top = Math.max(areaTop, Math.min(bottom - MIN_CHAT_SIZE.height, resizeState.y + dy));
  }

  const bounds = {
    x: Math.round(left),
    y: Math.round(top),
    width: Math.round(right - left),
    height: Math.round(bottom - top),
  };
  const current = petWindow.getBounds();
  if (
    bounds.x !== current.x || bounds.y !== current.y
    || bounds.width !== current.width || bounds.height !== current.height
  ) {
    petWindow.setBounds(bounds, false);
  }
}

function endResize() {
  resizeState = null;
  if (resizeTimer) clearInterval(resizeTimer);
  resizeTimer = null;
}

function anchoredBounds(width, height) {
  const area = screen.getPrimaryDisplay().workArea;
  return { x: area.x + area.width - width - 24, y: area.y + area.height - height - 24, width, height };
}

function windowStatePath() {
  return path.join(app.getPath("userData"), "pet-window.json");
}

function loadWindowState() {
  try {
    const state = JSON.parse(fs.readFileSync(windowStatePath(), "utf8"));
    const width = Number(state?.chatWidth);
    const height = Number(state?.chatHeight);
    if (Number.isFinite(width) && Number.isFinite(height)) {
      savedChatSize = {
        width: Math.max(MIN_CHAT_SIZE.width, Math.round(width)),
        height: Math.max(MIN_CHAT_SIZE.height, Math.round(height)),
      };
    }
  } catch (_error) {
    savedChatSize = { ...DEFAULT_CHAT_SIZE };
  }
}

function saveWindowStateSoon() {
  if (saveWindowTimer) clearTimeout(saveWindowTimer);
  saveWindowTimer = setTimeout(() => {
    saveWindowTimer = null;
    try {
      fs.mkdirSync(path.dirname(windowStatePath()), { recursive: true });
      fs.writeFileSync(windowStatePath(), JSON.stringify({
        chatWidth: savedChatSize.width,
        chatHeight: savedChatSize.height,
      }, null, 2));
    } catch (_error) {
      // Remembering the size is a convenience; resizing must still work when
      // the user-data directory is temporarily unavailable.
    }
  }, 180);
}

function chatSizeLimits(constrainToCurrentPosition = false) {
  const [x, y] = petWindow?.getPosition?.() || [0, 0];
  const display = screen.getDisplayNearestPoint({ x, y });
  const area = display.workArea;
  return {
    maxWidth: Math.max(
      MIN_CHAT_SIZE.width,
      constrainToCurrentPosition ? area.x + area.width - x - 8 : area.width - 16,
    ),
    maxHeight: Math.max(
      MIN_CHAT_SIZE.height,
      constrainToCurrentPosition ? area.y + area.height - y - 8 : area.height - 16,
    ),
  };
}

function resizePet(expanded) {
  if (!petWindow || petWindow.isDestroyed()) return;
  if (expanded) {
    setMousePassthrough(false, { force: true });
    expandedMode = true;
    const limits = chatSizeLimits();
    const width = Math.min(Math.max(savedChatSize.width, MIN_CHAT_SIZE.width), limits.maxWidth);
    const height = Math.min(Math.max(savedChatSize.height, MIN_CHAT_SIZE.height), limits.maxHeight);
    resizeTo(width, height);
    keepPetOnTop();
    return;
  }
  endResize();
  expandedMode = false;
  resizeTo(PET_SIZE.width, PET_SIZE.height);
  keepPetOnTop();
}

function resizeTo(width, height) {
  if (!petWindow || petWindow.isDestroyed()) return;
  const [oldWidth, oldHeight] = petWindow.getSize();
  const [x, y] = petWindow.getPosition();
  const next = clampWindowPosition(x + oldWidth - width, y + oldHeight - height, width, height);
  petWindow.setBounds({ ...next, width, height }, true);
  keepPetOnTop();
}

function setMinimized(value) {
  setMousePassthrough(false, { force: true });
  resizeTo(PET_SIZE.width, value ? MINIMIZED_SIZE.height : PET_SIZE.height);
  petWindow?.webContents.send("pet:minimized", Boolean(value));
}

function setMousePassthrough(enabled, { force = false } = {}) {
  if (!petWindow || petWindow.isDestroyed()) return;
  const next = expandedMode ? false : Boolean(enabled);
  if (!force && mousePassthroughEnabled === next) return;
  mousePassthroughEnabled = next;
  petWindow.setIgnoreMouseEvents(next, next ? { forward: true } : undefined);
}

function recoverMouseInteraction() {
  if (!mousePassthroughEnabled || expandedMode || !petWindow || petWindow.isDestroyed() || !petWindow.isVisible()) return;
  const bounds = petWindow.getBounds();
  const point = screen.getCursorScreenPoint();
  const x = point.x - bounds.x;
  const y = point.y - bounds.y;
  const minimized = bounds.height <= MINIMIZED_SIZE.height + 4;
  const overVisiblePet = minimized
    ? x >= 0 && x <= bounds.width && y >= 0 && y <= bounds.height
    : ((x - 87) ** 2) / (70 ** 2) + ((y - 52) ** 2) / (56 ** 2) <= 1
      || ((x - 84) ** 2) / (64 ** 2) + ((y - 117) ** 2) / (72 ** 2) <= 1
      || ((x - 149) ** 2) / (32 ** 2) + ((y - 124) ** 2) / (48 ** 2) <= 1
      || (x >= 45 && x <= 145 && y >= 175 && y <= 225);
  if (overVisiblePet) setMousePassthrough(false, { force: true });
}

function startMouseRecoveryWatchdog() {
  if (mouseRecoveryTimer) clearInterval(mouseRecoveryTimer);
  mouseRecoveryTimer = setInterval(recoverMouseInteraction, 120);
  mouseRecoveryTimer.unref?.();
}
function quitPet() {
  quitting = true;
  app.quit();
}

function showPetContextMenu() {
  if (!petWindow || petWindow.isDestroyed()) return;
  Menu.buildFromTemplate([
    { label:"最小化为控制条", click:() => setMinimized(true) },
    { type:"separator" },
    { label:"关闭桌宠", click:quitPet },
  ]).popup({ window:petWindow });
}

function createWindow() {
  loadWindowState();
  petWindow = new BrowserWindow({
    ...anchoredBounds(PET_SIZE.width, PET_SIZE.height), frame: false, transparent: true, alwaysOnTop: true,
    skipTaskbar: true, resizable: false, maximizable: false, fullscreenable: false,
    hasShadow: false, show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"), contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false,
    },
  });
  keepPetOnTop();
  // Windows can reorder topmost windows after display/workspace transitions
  // and hide/show cycles, so reassert the level at those boundaries.
  petWindow.on("show", keepPetOnTop);
  petWindow.on("restore", keepPetOnTop);
  petWindow.on("resize", () => {
    if (!expandedMode || !petWindow || petWindow.isDestroyed()) return;
    const [width, height] = petWindow.getSize();
    savedChatSize = { width, height };
    saveWindowStateSoon();
  });
  petWindow.webContents.on("did-fail-load", () => {
    if (!quitting && petWindow && !petWindow.isDestroyed()) {
      setTimeout(loadGatewayPage, 2000);
    }
  });
  petWindow.webContents.on("did-start-loading", () => {
    // `setIgnoreMouseEvents` belongs to BrowserWindow rather than the page.
    // A fresh renderer must never inherit the old page's pass-through state.
    setMousePassthrough(false, { force: true });
  });
  petWindow.webContents.on("did-finish-load", () => {
    setMousePassthrough(false, { force: true });
  });
  petWindow.webContents.on("render-process-gone", (_event, details) => {
    if (quitting || details?.reason === "clean-exit") return;
    gatewayLoadInFlight = false;
    setTimeout(loadGatewayPage, 1000);
  });
  // Citation links in the pet chat should open in the user's browser instead
  // of replacing the transparent Electron surface or creating an unmanaged
  // BrowserWindow.
  petWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  loadGatewayPage();
  petWindow.once("ready-to-show", showPetWindow);
  petWindow.on("close", (event) => {
    if (!quitting) { event.preventDefault(); petWindow.hide(); }
  });
}

function rebuildTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "\u663e\u793a\u684c\u5ba0", click: showPetWindow },
    { label: "\u5f00\u673a\u542f\u52a8", type: "checkbox", enabled: supportsAutoStart(), checked: autoStartEnabled(), click: (item) => setAutoStart(item.checked) },
    { label: "\u6253\u5f00 SJTUClaw", click: () => shell.openExternal(GATEWAY) },
    { type: "separator" },
    { label: "\u9000\u51fa", click: quitPet },
  ]));
}

function createTray() {
  tray = new Tray(createTrayBitmap());
  tray.setToolTip("SJTUClaw 打工喵桌宠");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "显示桌宠", click: showPetWindow },
    { label: "打开 SJTUClaw", click: () => shell.openExternal(GATEWAY) },
    { type: "separator" },
    { label: "退出", click: quitPet },
  ]));
  tray.on("click", () => petWindow?.isVisible() ? petWindow.hide() : showPetWindow());
}

function createTrayBitmap() {
  // createFromBitmap avoids Windows' inconsistent SVG/emoji rendering in the
  // notification area. Electron expects BGRA bytes on Windows.
  const size = 32;
  const pixels = Buffer.alloc(size * size * 4);
  const paint = (x, y, b, g, r, a = 255) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    const offset = (y * size + x) * 4;
    pixels[offset] = b; pixels[offset + 1] = g;
    pixels[offset + 2] = r; pixels[offset + 3] = a;
  };
  const circle = (cx, cy, radius, color) => {
    for (let y = cy - radius; y <= cy + radius; y += 1) {
      for (let x = cx - radius; x <= cx + radius; x += 1) {
        if ((x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2) paint(x, y, ...color);
      }
    }
  };
  const outline = [39, 64, 101, 255];
  const orange = [43, 157, 245, 255];
  const cream = [214, 244, 255, 255];
  const blush = [149, 157, 255, 255];
  // A small cat face remains legible in Windows' 16–32 px tray slots.
  circle(9, 9, 6, outline);
  circle(23, 9, 6, outline);
  circle(16, 17, 13, outline);
  circle(9, 10, 4, orange);
  circle(23, 10, 4, orange);
  circle(16, 17, 11, cream);
  circle(12, 16, 2, outline);
  circle(20, 16, 2, outline);
  circle(9, 21, 2, blush);
  circle(23, 21, 2, blush);
  circle(16, 20, 2, orange);
  paint(15, 23, ...outline);
  paint(16, 24, ...outline);
  paint(17, 23, ...outline);
  return nativeImage.createFromBitmap(pixels, { width: size, height: size, scaleFactor: 1 });
}

app.whenReady().then(() => {
  createWindow();
  createTray();
  rebuildTrayMenu();
  startGatewayProbe();
  startMouseRecoveryWatchdog();
  ipcMain.on("pet:expanded", (_event, expanded) => resizePet(Boolean(expanded)));
  ipcMain.on("pet:open-web", () => shell.openExternal(GATEWAY));
  ipcMain.on("pet:notify", (_event, payload) => showNotification(payload?.title, payload?.body));
  ipcMain.on("pet:hide", () => petWindow?.hide());
  ipcMain.on("pet:quit", quitPet);
  ipcMain.on("pet:context-menu", showPetContextMenu);
  ipcMain.on("pet:drag-start", (_event, point) => beginDrag(point));
  ipcMain.on("pet:drag-move", (_event, point) => moveDrag(point));
  ipcMain.on("pet:drag-end", endDrag);
  ipcMain.on("pet:resize-start", (_event, edge) => beginResize(edge));
  ipcMain.on("pet:resize-end", endResize);
  ipcMain.on("pet:mouse-passthrough", (_event, enabled) => setMousePassthrough(enabled));
});
app.on("window-all-closed", () => {});
app.on("before-quit", () => {
  quitting = true;
  endDrag();
  endResize();
  if (gatewayProbeTimer) clearInterval(gatewayProbeTimer);
  gatewayProbeTimer = null;
  if (mouseRecoveryTimer) clearInterval(mouseRecoveryTimer);
  mouseRecoveryTimer = null;
  if (saveWindowTimer) clearTimeout(saveWindowTimer);
  saveWindowTimer = null;
});
