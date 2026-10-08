/** 渲染进程能用的接口：只暴露这几个函数，不暴露 Node（contextIsolation + sandbox）。 */
import { contextBridge, ipcRenderer, webUtils } from "electron";

contextBridge.exposeInMainWorld("fwr", {
  kind: "electron",
  send: (msg: unknown) => ipcRenderer.send("acp:send", msg),
  onMessage: (cb: (msg: unknown) => void) => {
    const h = (_e: unknown, m: unknown) => cb(m);
    ipcRenderer.on("acp:message", h);
    return () => ipcRenderer.removeListener("acp:message", h);
  },
  onHostEvent: (cb: (ev: unknown) => void) => {
    const h = (_e: unknown, m: unknown) => cb(m);
    ipcRenderer.on("host:event", h);
    return () => ipcRenderer.removeListener("host:event", h);
  },
  logs: () => ipcRenderer.invoke("host:logs"),
  pickFolder: () => ipcRenderer.invoke("native:pickFolder"),
  notify: (opts: { title: string; body: string; tag?: string }) => ipcRenderer.invoke("native:notify", opts),
  openPath: (p: string) => ipcRenderer.invoke("native:openPath", p),
  showItem: (p: string) => ipcRenderer.invoke("native:showItem", p),
  info: () => ipcRenderer.invoke("host:info"),
  openInEditor: (file: string, line?: number) => ipcRenderer.invoke("native:openInEditor", file, line),
  setTheme: (t: string) => ipcRenderer.invoke("native:setTheme", t),
  // drag & drop: File.path was removed in Electron 32; webUtils.getPathForFile gives the absolute path
  pathForFile: (f: File) => webUtils.getPathForFile(f),
});
