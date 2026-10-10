import { createRoot } from "react-dom/client";
import { App } from "./App";
import { getLang } from "./i18n";
import { native, rpc } from "./rpc";
import { useStore } from "./store";
import "./styles.css";

// 调试入口：开发者工具里可以用 __fwr.rpc / __fwr.store 查看状态
(window as unknown as { __fwr: unknown }).__fwr = { rpc, store: useStore };

// 外观（设置页，2026-10-06）：每台机器自己的偏好，存 localStorage；读不到就跟随系统
try {
  const t = localStorage.getItem("fwr.theme");
  if (t === "light" || t === "dark") native.applyTheme(t);
} catch { /* 隐私模式等：跟随系统 */ }
document.documentElement.lang = getLang() === "zh" ? "zh-CN" : "en";

createRoot(document.getElementById("root")!).render(<App />);
