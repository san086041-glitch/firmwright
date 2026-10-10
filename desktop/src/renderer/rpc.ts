/**
 * 渲染进程里的 JSON-RPC（ACP）客户端。
 * 传输层两种：Electron（preload 暴露的 window.fwr）或开发用的 WebSocket 桥。
 */
import { t } from "./i18n";

export type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

interface NativeApi {
  kind: "electron";
  send(msg: Json): void;
  onMessage(cb: (msg: Json) => void): () => void;
  onHostEvent(cb: (ev: Json) => void): () => void;
  logs(): Promise<string[]>;
  pickFolder(title?: string): Promise<string | null>;
  notify(opts: { title: string; body: string; tag?: string }): Promise<boolean>;
  openPath(p: string): Promise<string>;
  showItem(p: string): Promise<void>;
  pathForFile?(f: File): string;
  info?(): Promise<HostInfo>;
  openInEditor?(file: string, line?: number): Promise<string>;
  setTheme?(t: Theme): Promise<void>;
}

export type Theme = "system" | "light" | "dark";
export interface HostInfo { logFile: string; logDir: string; appVersion: string; electron: string; packaged: boolean }

/** 核心返回的 JSON-RPC 错误：保留 code 和 data（W5 的"不是 git 仓库""合并冲突"要按 code 处理） */
export class RpcError extends Error {
  constructor(message: string, readonly code: number, readonly data: Json) {
    super(message);
  }
}

declare global {
  interface Window {
    fwr?: NativeApi;
  }
}

type NotificationHandler = (method: string, params: Json) => void;
type RequestHandler = (method: string, params: Json, id: number | string) => Promise<Json> | void;
type HostHandler = (ev: Json) => void;

export class RpcClient {
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: Json) => void; reject: (e: Error) => void }>();
  private onNotification: NotificationHandler = () => {};
  private onRequest: RequestHandler = () => {};
  private onHost: HostHandler = () => {};
  private sendRaw: (msg: Json) => void = () => {};
  private ws?: WebSocket;
  private queue: Json[] = [];
  readonly mode: "electron" | "web";

  constructor() {
    this.mode = window.fwr ? "electron" : "web";
    if (window.fwr) {
      window.fwr.onMessage((m) => this.receive(m));
      window.fwr.onHostEvent((ev) => this.onHost(ev));
      this.sendRaw = (m) => window.fwr!.send(m);
    } else {
      this.connectWs();
    }
  }

  private connectWs(): void {
    const url = new URLSearchParams(location.search).get("ws") ?? `ws://${location.host}/ws`;
    const ws = new WebSocket(url);
    this.ws = ws;
    this.sendRaw = (m) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(m));
      else this.queue.push(m);
    };
    ws.onopen = () => {
      for (const m of this.queue.splice(0)) ws.send(JSON.stringify(m));
    };
    ws.onmessage = (e) => {
      const data = JSON.parse(String(e.data));
      if (data.channel === "acp") this.receive(data.msg);
      else if (data.channel === "host") this.onHost(data.msg);
    };
    ws.onclose = () => setTimeout(() => this.connectWs(), 1500);
  }

  handlers(h: { notification?: NotificationHandler; request?: RequestHandler; host?: HostHandler }): void {
    if (h.notification) this.onNotification = h.notification;
    if (h.request) this.onRequest = h.request;
    if (h.host) this.onHost = h.host;
  }

  request<T = Json>(method: string, params: Json = {}): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.sendRaw({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method: string, params: Json = {}): void {
    this.sendRaw({ jsonrpc: "2.0", method, params });
  }

  respond(id: number | string, result: Json): void {
    this.sendRaw({ jsonrpc: "2.0", id, result });
  }

  private receive(msg: Json): void {
    if (msg.method && msg.id !== undefined) {
      // 核心发来的请求（权限审批、人工操作卡片）：交给界面，用户操作后再 respond
      const r = this.onRequest(msg.method, msg.params, msg.id);
      if (r instanceof Promise) r.then((res) => this.respond(msg.id, res));
      return;
    }
    if (msg.method) {
      this.onNotification(msg.method, msg.params);
      return;
    }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if (msg.error) p.reject(new RpcError(msg.error.message ?? "RPC error", msg.error.code ?? 0, msg.error.data));
    else p.resolve(msg.result);
  }

  /** 核心重启后，之前没回的请求永远不会有响应了，全部以错误结束 */
  failAllPending(reason: string): void {
    for (const [, p] of this.pending) p.reject(new Error(reason));
    this.pending.clear();
  }
}

export const rpc = new RpcClient();

// 原生能力：Electron 里走主进程，浏览器里降级
export const native = {
  async pickFolder(title?: string): Promise<string | null> {
    if (window.fwr) return window.fwr.pickFolder(title ?? t("Choose a project folder"));
    return window.prompt(t("Full path of the folder (no folder picker in browser mode)"));
  },
  async notify(title: string, body: string, tag?: string): Promise<void> {
    if (window.fwr) {
      await window.fwr.notify({ title, body, tag });
      return;
    }
    if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body, tag });
  },
  /** 在资源管理器里显示文件（浏览器模式下只能把路径告诉用户） */
  /** drop file -> absolute path (Electron webUtils; empty string in browser mode) */
  pathForFile(f: File): string {
    try {
      return window.fwr?.pathForFile?.(f) ?? "";
    } catch {
      return "";
    }
  },
  /** 用资源管理器打开文件夹 / 用默认程序打开文件（浏览器模式下只能把路径告诉用户） */
  async openPath(p: string): Promise<void> {
    if (window.fwr) await window.fwr.openPath(p);
    else window.prompt(t("Location (Explorer cannot be opened in browser mode)"), p);
  },
  /** 调用栈的 file:line：VS Code 跳到那一行，没有就用默认程序打开（浏览器模式下只能把位置告诉用户） */
  async openInEditor(file: string, line?: number): Promise<string> {
    if (window.fwr?.openInEditor) return window.fwr.openInEditor(file, line);
    window.prompt(t("Source location (no editor in browser mode)"), line ? `${file}:${line}` : file);
    return "browser";
  },
  async hostInfo(): Promise<HostInfo | null> {
    return window.fwr?.info ? window.fwr.info() : null;
  },
  /** 外观：界面用 data-theme，Electron 里同时设 nativeTheme（窗口底色、系统控件） */
  applyTheme(t: Theme): void {
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    void window.fwr?.setTheme?.(t);
  },
  async showItem(p: string): Promise<void> {
    if (window.fwr) await window.fwr.showItem(p);
    else window.prompt(t("File location (Explorer cannot be opened in browser mode)"), p);
  },
};
