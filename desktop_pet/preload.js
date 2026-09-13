/* 只暴露桌宠所需的最小 IPC 接口，避免渲染页面直接访问 Node.js。 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sjtuPet", {
  setExpanded: (expanded) => ipcRenderer.send("pet:expanded", Boolean(expanded)),
  openWeb: () => ipcRenderer.send("pet:open-web"),
  notify: (payload) => ipcRenderer.send("pet:notify", payload || {}),
  hide: () => ipcRenderer.send("pet:hide"),
  quit: () => ipcRenderer.send("pet:quit"),
  showContextMenu: () => ipcRenderer.send("pet:context-menu"),
  onMinimized: (callback) => ipcRenderer.on("pet:minimized", (_event, value) => callback(Boolean(value))),
  onGatewayStatus: (callback) => ipcRenderer.on("pet:gateway-status", (_event, value) => callback(Boolean(value?.healthy))),
  beginDrag: (point) => ipcRenderer.send("pet:drag-start", point || {}),
  dragTo: (point) => ipcRenderer.send("pet:drag-move", point || {}),
  endDrag: () => ipcRenderer.send("pet:drag-end"),
  beginResize: (edge) => ipcRenderer.send("pet:resize-start", String(edge || "")),
  endResize: () => ipcRenderer.send("pet:resize-end"),
  setMousePassthrough: (enabled) => ipcRenderer.send("pet:mouse-passthrough", Boolean(enabled)),
});
