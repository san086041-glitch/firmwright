/**
 * 开发用的 WebSocket 桥：不开 Electron，在普通浏览器里调界面。
 *
 *   npm run bridge   → http://localhost:5198/        （静态文件：dist/renderer）
 *                      ws://localhost:5198/ws         （转发 ACP 消息给同一个核心）
 *
 * 只监听 127.0.0.1。主进程的原生能力（选文件夹、系统通知）在浏览器里由渲染进程自己降级处理。
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { WebSocketServer, type WebSocket } from "ws";
import { Sidecar } from "./sidecar";

const repoRoot = path.resolve(__dirname, "..", "..");
const staticRoot = path.join(repoRoot, "desktop", "dist", "renderer");
const port = Number(process.env.FWR_BRIDGE_PORT ?? 5198);
const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  let file = path.normalize(path.join(staticRoot, decodeURIComponent(url.pathname)));
  if (!file.startsWith(staticRoot)) {
    res.writeHead(403).end();
    return;
  }
  if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
  if (!fs.existsSync(file)) {
    res.writeHead(404).end("not found");
    return;
  }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] ?? "application/octet-stream", "cache-control": "no-store" });
  fs.createReadStream(file).pipe(res);
});

const wss = new WebSocketServer({ server, path: "/ws" });
const clients = new Set<WebSocket>();
// --sim：启用模拟板（core/firmwright/device/sim.py），没接真板子时调设备面板用
const sidecar = new Sidecar({
  repoRoot,
  devHome: path.join(repoRoot, "dev-home"),
  env: process.argv.includes("--sim") ? { FIRMWRIGHT_SIM_BOARD: "1" } : {},
});

function broadcast(payload: unknown): void {
  const data = JSON.stringify(payload);
  for (const c of clients) if (c.readyState === c.OPEN) c.send(data);
}

sidecar.on("message", (m) => broadcast({ channel: "acp", msg: m }));
sidecar.on("log", (line: string) => process.stderr.write(`[core] ${line}\n`));
sidecar.on("restarted", (info) => broadcast({ channel: "host", msg: { type: "sidecar_restarted", ...info } }));

wss.on("connection", (ws) => {
  clients.add(ws);
  ws.on("message", (data) => {
    try {
      sidecar.send(JSON.parse(String(data)));
    } catch {
      /* 忽略非 JSON */
    }
  });
  ws.on("close", () => clients.delete(ws));
});

sidecar.start();
server.listen(port, "127.0.0.1", () => {
  process.stderr.write(`Firmwright dev bridge: http://127.0.0.1:${port}/\n`);
});
process.on("SIGINT", () => {
  sidecar.stop();
  process.exit(0);
});
