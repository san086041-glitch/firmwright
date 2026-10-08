/** 界面通用的小函数：数字 / 时间格式、工具卡片的参数摘要、路径缩写。 */
import type { Json } from "../rpc";
import type { IconName } from "./Icon";

export function fmtK(n: number | undefined | null): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function fmtBytes(n: number): string {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(2)} MB` : `${(n / 1024).toFixed(1)} KB`;
}

export function fmtDuration(ms: number | undefined): string {
  if (!ms) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

/** "3m ago" / "2h ago" / "Oct 3" */
export function ago(t: number | string | undefined): string {
  if (!t) return "";
  const ms = typeof t === "number" ? (t < 1e12 ? t * 1000 : t) : Date.parse(t);
  if (!Number.isFinite(ms)) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function baseName(p: string): string {
  const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

export function shortPath(p: string): string {
  const s = p.replace(/\\/g, "/");
  const i = s.lastIndexOf("/main/");
  if (i >= 0) return s.slice(i + 1);
  return s.split("/").slice(-2).join("/");
}

export const KIND_ICON: Record<string, IconName> = {
  read: "file", edit: "edit", search: "search", execute: "terminal", fetch: "download", think: "bug", other: "dot",
};

/** 工具名 → 图标（比 ACP 的 kind 更具体） */
export const TOOL_ICON: Record<string, IconName> = {
  read_file: "file", list_dir: "folder", grep: "search", write_file: "edit", edit_file: "edit", shell: "terminal",
  build: "hammer", clean: "eraser", set_target: "cpu", size: "layers", project_status: "info", flash: "bolt",
  reset: "power", await_marker: "pulse", read_log: "list", diagnose_crash: "bug", ask_human: "hand",
  spawn_subagent: "bot", goal_report: "target", skill: "sparkles", memory_search: "search", memory_get: "brain",
  remember: "brain", search_tool: "search", use_tool: "external", check_subagents: "bot", stop_subagent: "stop",
};

/** 工具调用的一行摘要：优先用参数里最能说明这次调用的那个字段 */
export function toolSummary(name: string, input: Json, title: string): string {
  const a = input ?? {};
  switch (name) {
    case "shell": return a.description || a.command || "";
    case "read_file": return [a.path, a.pages ? `pages ${a.pages}` : "", a.query ? `“${a.query}”` : ""].filter(Boolean).join(" · ");
    case "grep": return `/${a.pattern ?? ""}/${a.path && a.path !== "." ? ` in ${a.path}` : ""}${a.glob ? ` (${a.glob})` : ""}`;
    case "list_dir": return `${a.path ?? "."}${a.pattern ? ` ${a.pattern}` : ""}`;
    case "flash": return a.scope ? `scope ${a.scope}` : "app";
    case "await_marker": return a.expect ? `expect /${a.expect}/` : "expected marker from facts.toml";
    case "read_log": return [a.log_ref, a.grep ? `/${a.grep}/` : "", a.tail ? `tail ${a.tail}` : ""].filter(Boolean).join(" · ");
    case "set_target": return a.chip ?? "";
    case "diagnose_crash": return a.event_id ?? "latest crash";
    case "ask_human": return a.title ?? "";
    case "spawn_subagent": return `${a.subagent_type ?? "general"}${a.background ? " · background" : ""} · ${a.description ?? ""}`;
    case "check_subagents": return `${a.id ?? "all"}${a.wait_s ? ` · wait ${a.wait_s} s` : ""}`;
    case "stop_subagent": return a.id ?? "";
    case "goal_report": return a.status ?? "";
    case "skill": return a.name ?? "";
    case "remember": return `${a.scope ?? ""}/${a.topic ?? ""}`;
    case "memory_search": return a.query ?? "";
    default: {
      const rest = title.startsWith(name) ? title.slice(name.length).trim() : title;
      return rest;
    }
  }
}
