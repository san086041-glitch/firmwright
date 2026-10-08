/**
 * Python 核心（sidecar）的启动与管理（I03 / I14）。
 *
 * - 开发期：用 core\.venv 里的 python 跑源码（I15）；发布版换成 PyInstaller 打包的可执行文件（W8）
 * - stdout 一行一条 JSON-RPC 消息（ACP），stderr 是日志
 * - 核心意外退出时自动重启（退避 1s → 2s → … 上限 10s），并通知界面重新 initialize / session/load
 *   （参照 grok leader 崩溃后重放 initialize 和 session/load，方案 §6.4）
 */
import { spawn, type ChildProcess } from "node:child_process";
import { EventEmitter } from "node:events";
import path from "node:path";

export interface SidecarOptions {
  repoRoot: string;
  python?: string;
  coreExe?: string; // 发布版：打包好的核心可执行文件（有它就不用 Python）
  env?: NodeJS.ProcessEnv;
  devHome?: string; // 开发期的数据目录（仓库里的 dev-home）
  packaged?: boolean; // 发布版：去掉开发用的板子开关（模拟板 / QEMU），用户环境里碰巧有也不生效
}

// Git Bash / MSYS 带进来的变量会让 idf.py 拒绝工作，核心也会去掉，这里提前去掉更干净
const MSYS_VARS = ["MSYSTEM", "MSYSTEM_PREFIX", "MSYSTEM_CHOST", "MSYSTEM_CARCH", "MINGW_PREFIX", "MINGW_CHOST"];
// 开发 / 评测用的板子（2026-10-06：正式版隐藏模拟板）。核心在打包后的版本里也会忽略它们
export const DEV_BOARD_VARS = ["FIRMWRIGHT_SIM_BOARD", "FIRMWRIGHT_SIM_ONLY", "FIRMWRIGHT_SIM_CHIP", "FIRMWRIGHT_QEMU_BOARD",
                               "FIRMWRIGHT_QEMU_ONLY"];

export class Sidecar extends EventEmitter {
  private proc?: ChildProcess;
  private buf = "";
  private stopping = false;
  private backoff = 1000;
  restarts = 0;

  constructor(private opts: SidecarOptions) {
    super();
  }

  command(): { cmd: string; args: string[]; cwd: string } {
    if (this.opts.coreExe) return { cmd: this.opts.coreExe, args: [], cwd: path.dirname(this.opts.coreExe) };
    const core = path.join(this.opts.repoRoot, "core");
    const python =
      this.opts.python ?? process.env.FIRMWRIGHT_PYTHON ?? path.join(core, ".venv", "Scripts", "python.exe");
    return { cmd: python, args: ["-m", "firmwright.acp"], cwd: core };
  }

  start(): void {
    const { cmd, args, cwd } = this.command();
    const env: NodeJS.ProcessEnv = { ...process.env, ...this.opts.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
    // 开发期的数据目录放在仓库里（dev-home），不用 %LOCALAPPDATA%：开发工具的沙箱会把 AppData 下的写入重定向，
    // 导致"开发时看到的会话"和"用户自己打开时看到的"不是同一份。发布版（W8 打包）仍用 %LOCALAPPDATA%\Firmwright（D01）
    if (!env.FIRMWRIGHT_HOME && this.opts.devHome) env.FIRMWRIGHT_HOME = this.opts.devHome;
    for (const k of MSYS_VARS) delete env[k];
    if (this.opts.packaged) for (const k of DEV_BOARD_VARS) delete env[k];
    this.stopping = false;
    const proc = spawn(cmd, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    this.proc = proc;
    this.buf = "";
    const started = Date.now();
    proc.stdout!.setEncoding("utf8");
    proc.stdout!.on("data", (chunk: string) => {
      this.buf += chunk;
      let i: number;
      while ((i = this.buf.indexOf("\n")) >= 0) {
        const line = this.buf.slice(0, i).trim();
        this.buf = this.buf.slice(i + 1);
        if (!line) continue;
        try {
          this.emit("message", JSON.parse(line));
        } catch {
          this.emit("log", `[stdout non-JSON] ${line}`);
        }
      }
    });
    proc.stderr!.setEncoding("utf8");
    proc.stderr!.on("data", (chunk: string) => {
      for (const line of chunk.split(/\r?\n/)) if (line) this.emit("log", line);
    });
    proc.on("error", (err) => this.emit("log", `[spawn failed] ${err.message}`));
    proc.on("exit", (code) => {
      this.emit("exit", code);
      if (this.stopping) return;
      // 跑了一会儿才崩的，退避重置；一启动就崩的，逐步拉长间隔，避免疯狂重启
      if (Date.now() - started > 30_000) this.backoff = 1000;
      const delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, 10_000);
      setTimeout(() => {
        if (this.stopping) return;
        this.restarts += 1;
        this.start();
        this.emit("restarted", { code, restarts: this.restarts });
      }, delay);
    });
  }

  get pid(): number | undefined {
    return this.proc?.pid;
  }

  /** 自测用：模拟核心崩溃（不设 stopping，所以会走自动重启） */
  killForTest(): void {
    if (this.proc?.pid) spawn("taskkill", ["/PID", String(this.proc.pid), "/T", "/F"], { windowsHide: true });
  }

  send(msg: unknown): void {
    if (!this.proc?.stdin?.writable) return;
    this.proc.stdin.write(JSON.stringify(msg) + "\n");
  }

  stop(): void {
    this.stopping = true;
    if (this.proc && this.proc.exitCode === null) {
      this.proc.stdin?.end();
      const p = this.proc;
      setTimeout(() => {
        if (p.exitCode === null && p.pid) {
          // Windows 上要杀整棵进程树（核心可能正在跑 idf.py）
          spawn("taskkill", ["/PID", String(p.pid), "/T", "/F"], { windowsHide: true });
        }
      }, 3000);
    }
  }
}
