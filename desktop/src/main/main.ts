/**
 * Electron 主进程（I14）：启动并管理 Python 核心，把 ACP 消息原样转给渲染进程。
 *
 * JSON-RPC 的客户端逻辑（请求 id、等待响应、处理核心发来的请求）放在渲染进程里，
 * 这样 Electron 和开发用的 WebSocket 桥（devbridge.ts）共用同一份实现；主进程只做转发和原生能力
 * （选文件夹、系统通知）。
 */
import { app, BrowserWindow, dialog, ipcMain, nativeTheme, Notification, shell } from "electron";
import fs from "node:fs";
import path from "node:path";
import { Sidecar } from "./sidecar";

const repoRoot = path.resolve(__dirname, "..", "..");
let win: BrowserWindow | null = null;
// 发布版（W8）：核心是 PyInstaller 打包的可执行文件，在 resources\core 里；开发期用 core\.venv 跑源码
const coreExe = app.isPackaged ? path.join(process.resourcesPath, "core", "firmwright-core.exe") : undefined;
const sidecar = new Sidecar({ repoRoot, coreExe, devHome: app.isPackaged ? undefined : path.join(repoRoot, "dev-home"),
                              packaged: app.isPackaged });
const logLines: string[] = [];

// 运行日志落盘（开发期写在仓库里，位置固定，不受 LOCALAPPDATA 影响），出问题时直接读这个文件
// 发布版写到 %LOCALAPPDATA%\Firmwright\logs（和核心的数据目录放一起，设置页"About & data"里能打开）
const logFile = app.isPackaged
  ? path.join(process.env.LOCALAPPDATA ?? app.getPath("userData"), "Firmwright", "logs", "desktop.log")
  : path.join(repoRoot, "desktop", "logs", "desktop.log");
fs.mkdirSync(path.dirname(logFile), { recursive: true });
function fileLog(line: string): void {
  fs.appendFileSync(logFile, `${new Date().toISOString()} ${line}\n`);
}
fileLog(`---- start · cwd=${process.cwd()} · LOCALAPPDATA=${process.env.LOCALAPPDATA ?? "(unset)"} · ` +
        `FIRMWRIGHT_HOME=${process.env.FIRMWRIGHT_HOME ?? "(unset)"} · USERPROFILE=${process.env.USERPROFILE}`);

function send(channel: string, payload: unknown): void {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    title: "Firmwright",
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#0c0e11" : "#f4f5f7", // 和界面的 --bg 一致，启动时不闪
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.setMenuBarVisibility(false);
  const devUrl = process.env.FWR_DEV_URL;
  if (devUrl) win.loadURL(devUrl);
  else win.loadFile(path.join(__dirname, "..", "dist", "renderer", "index.html"));  // 开发期和打包后（app.asar 里）都是这个相对位置

  // 外部链接用系统浏览器打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // 自测：FWR_CAPTURE=<png 路径> 时，加载后截图并退出（没有显示器也能检查界面）
  // FWR_SELFTEST=crash   先选中第一个会话，再让模拟板崩溃一次（检查空闲崩溃卡片 + 系统通知）
  // FWR_SELFTEST=restart 杀掉核心进程（检查自动重启 + 会话恢复）
  const capture = process.env.FWR_CAPTURE;
  if (capture) {
    win.webContents.once("did-finish-load", () => {
      const test = process.env.FWR_SELFTEST;
      // FWR_OPEN=<会话 id>：截图前先打开这个会话（看界面用）
      if (process.env.FWR_OPEN) {
        const sid = JSON.stringify(process.env.FWR_OPEN);
        setTimeout(() => win!.webContents.executeJavaScript(
          `[...document.querySelectorAll('.session-item')].find((b) => b.title.includes(${sid}))?.click()`), 2000);
      }
      // FWR_CAPTURE_JS=<脚本>：截图前在页面里执行（打开设置页、滚到某张卡片……README 截图用）
      if (process.env.FWR_CAPTURE_JS) {
        const js = process.env.FWR_CAPTURE_JS;
        const delay = Number(process.env.FWR_CAPTURE_DELAY ?? 4000);
        setTimeout(() => void win!.webContents.executeJavaScript(js).catch((e) => logLines.push(`[capture-js] ${e}`)),
                   Math.max(500, delay - 2500));
      }
      if (test === "crash") {
        setTimeout(() => {
          win!.webContents.executeJavaScript(
            `document.querySelector('.session-item')?.click(); setTimeout(() => window.__fwr.rpc.request("_fwr/sim/crash"), 1500)`,
          );
        }, 2500);
      } else if (test === "restart") {
        setTimeout(() => {
          win!.webContents.executeJavaScript(`document.querySelector('.session-item')?.click()`);
        }, 2000);
        setTimeout(() => {
          logLines.push(`[selftest] killing the core pid=${sidecar.pid}`);
          sidecar.killForTest();
        }, 3000);
      }
      setTimeout(async () => {
        try {
          const state = await win!.webContents.executeJavaScript(
            `JSON.stringify({connected: window.__fwr.store.getState().connected, sessions: window.__fwr.store.getState().sessions.length, pending: [...window.__fwr.rpc.pending.keys()]})`,
          );
          logLines.push(`[state] ${state}`);
        } catch (e) {
          logLines.push(`[state] read failed ${e}`);
        }
        const img = await win!.webContents.capturePage();
        fs.writeFileSync(capture, img.toPNG());
        fs.writeFileSync(capture + ".log", logLines.join("\n"));
        app.quit();
      }, Number(process.env.FWR_CAPTURE_DELAY ?? 4000));
    });
  }
}

sidecar.on("message", (m) => send("acp:message", m));
sidecar.on("message", (m) => {
  // 记下核心实际用的数据目录，以及出错的响应
  if (m?.result?._meta?.fwr?.home) fileLog(`[core] data dir ${m.result._meta.fwr.home}`);
  if (m?.result?.sessions) fileLog(`[core] ${m.result.sessions.length} sessions`);
  if (m?.error) fileLog(`[core] request ${m.id} failed: ${m.error.message}`);
});
sidecar.on("log", (line: string) => {
  fileLog(`[core] ${line}`);
  logLines.push(line);
  if (logLines.length > 2000) logLines.splice(0, 500);
  send("host:log", line);
});
sidecar.on("exit", (code) => fileLog(`[host] core exited code=${code}`));
sidecar.on("restarted", (info) => {
  logLines.push(`[host] core restarted automatically (restart #${info.restarts})`);
  send("host:event", { type: "sidecar_restarted", ...info });
});
sidecar.on("exit", (code) => send("host:event", { type: "sidecar_exit", code }));

ipcMain.on("acp:send", (_e, msg) => sidecar.send(msg));
ipcMain.handle("host:logs", () => logLines.slice(-500));
// 对话框标题由界面传（界面语言，2026-10-10）
ipcMain.handle("native:pickFolder", async (_e, title?: string) => {
  const res = await dialog.showOpenDialog(win!, { properties: ["openDirectory"], title: title || "Choose a project folder" });
  return res.canceled ? null : res.filePaths[0];
});
ipcMain.handle("native:notify", (_e, opts: { title: string; body: string; tag?: string }) => {
  logLines.push(`[notify] ${opts.title} | ${opts.body}`);
  if (!Notification.isSupported()) return false;
  const n = new Notification({ title: opts.title, body: opts.body });
  n.on("click", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
    send("host:event", { type: "notification_click", tag: opts.tag });
  });
  n.show();
  return true;
});
ipcMain.handle("native:openPath", (_e, p: string) => shell.openPath(p));
ipcMain.handle("native:showItem", (_e, p: string) => shell.showItemInFolder(p));
// 设置页（2026-10-06）："关于"里的版本和日志位置；外观（跟随系统 / 浅色 / 深色，标题栏和窗口底色也跟着变）
ipcMain.handle("host:info", () => ({ logFile, logDir: path.dirname(logFile), appVersion: app.getVersion(),
                                     electron: process.versions.electron, packaged: app.isPackaged }));
// 调用栈里的 file:line（2026-10-06，界面改进第 12 项）：装了 VS Code（注册了 vscode:// 协议）就跳到那一行，
// 否则用系统默认程序打开文件。不经过 shell（路径来自固件的调试信息，不拼命令行）；只打开存在的绝对路径
ipcMain.handle("native:openInEditor", async (_e, file: string, line?: number) => {
  if (typeof file !== "string" || !path.isAbsolute(file) || !fs.existsSync(file)) return "missing";
  if (app.getApplicationNameForProtocol("vscode://")) {
    const target = `vscode://file/${encodeURI(file.split(path.sep).join("/"))}${line ? `:${Math.max(1, Math.floor(line))}` : ""}`;
    await shell.openExternal(target);
    return "vscode";
  }
  const err = await shell.openPath(file);
  return err ? `error: ${err}` : "default";
});
ipcMain.handle("native:setTheme", (_e, t: "system" | "light" | "dark") => {
  nativeTheme.themeSource = t === "light" || t === "dark" ? t : "system";
});

app.setAppUserModelId("Firmwright"); // Windows 系统通知需要
// 只允许一个实例：两个核心同时写同一个数据目录、抢同一块板子的串口会出问题。再次打开时把已有窗口提到前面
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  app.whenReady().then(() => {
    sidecar.start();
    createWindow();
  });
}
app.on("window-all-closed", () => {
  sidecar.stop();
  app.quit();
});
