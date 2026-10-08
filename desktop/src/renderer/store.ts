/**
 * 界面状态（Zustand）。核心推来的 session/update 由 applyUpdate 折叠成时间线条目；
 * 回放（session/load）走同一个函数，所以重启后看到的界面和原来一样。
 */
import { create } from "zustand";
import type { Json } from "./rpc";

export type SessionStatus = "idle" | "running" | "awaiting_approval" | "awaiting_human" | "error";

export interface SessionMeta {
  id: string;
  title: string;
  project_root: string;
  cwd: string;
  board_id: string | null;
  model_id: string;
  effort?: string | null;
  permission_mode: string;
  created_at: string;
  status?: SessionStatus;
  loaded?: boolean;
  mtime?: number;
  // W5 工作区
  isolation?: "worktree" | "in_place";
  worktree?: string | null;
  branch?: string | null;
  repo_root?: string | null;
  base_commit?: string | null;
  target_branch?: string | null;
  carried?: string[];
  state?: "active" | "merged" | "applied" | "discarded";
  merged_commit?: string | null;
}

export interface FirmwareRecord {
  seq: number;
  turn: number;
  sha256: string | null;
  scope: string;
  chip: string | null;
  board_id: string | null;
  at: string;
  archive: string | null;
  source: "agent" | "restore";
}

/** checkpoint 时间线上的一个点（D05） */
export interface Checkpoint {
  seq: number;
  kind: "base" | "turn" | "manual" | "restore";
  turn: number;
  commit: string;
  changed: boolean;
  files: string[];
  added: number;
  deleted: number;
  prompt: string;
  stop: string | null;
  firmware: FirmwareRecord | null;
  restored_to: number | null;
  at: string;
  boardFirmware?: FirmwareRecord | null; // 这个点上板子跑的固件（往前找最近一次烧录）
}

export interface CheckpointState {
  enabled: boolean;
  reason?: string;
  entries: Checkpoint[];
  canReflash?: boolean;
}

export interface PermissionAsk {
  rpcId: number | string;
  title: string;
  reason: string;
  risk: string;
  subjects: string[];
  options: { optionId: string; name: string; kind: string }[];
  answered?: string;
}

export interface HumanAsk {
  rpcId: number | string;
  title: string;
  instructions: string;
  kind: string;
  boardId?: string | null;
  answered?: { done: boolean; note: string };
}

export type Item =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string; thought: string; open: boolean }
  | {
      kind: "tool";
      id: string;
      title: string;
      toolKind: string;
      status: "pending" | "in_progress" | "completed" | "failed";
      input: Json;
      output: string;
      progress: string;
      startedAt?: number; // 开始执行的时刻（界面上显示已用时间）
      meta: Json;
      permission?: PermissionAsk;
      sub?: string; // W7：子 agent 的工具调用，显示是哪个子 agent
    }
  | { kind: "injected"; id: string; source: string; text: string }
  | { kind: "note"; id: string; text: string; now?: boolean } // 执行中发的插话，等当前这一步做完再送达（2026-10-05）
  | { kind: "human"; id: string; ask: HumanAsk }
  | { kind: "turn_end"; id: string; stopReason: string; usage: Json; error: string | null }
  | { kind: "restore"; id: string; entry: Checkpoint; flash: Json }
  | { kind: "prebuild"; id: string; status: "running" | "ok" | "failed"; text: string; durationMs?: number }
  | { kind: "subagent"; id: string; agentKind: string; description: string; status: "running" | "done";
      stop?: string; steps?: number; files?: string[]; text?: string; usage?: Json;
      background?: boolean; // 2026-10-05：后台运行的子 agent
      awaitingParent?: boolean } // 后台子 agent 做完时父会话空闲：报告在排队，等用户决定要不要让 agent 继续
  | { kind: "compaction"; id: string; status: "running" | "ok" | "failed"; reason: string; before?: number;
      after?: number; segment?: string | null; error?: string; durationMs?: number }
  | { kind: "notice"; id: string; text: string; tone: "info" | "warn" | "error" };

export interface SessionView {
  items: Item[];
  status: SessionStatus;
  mode: string;
  loaded: boolean;
  promptPending: boolean;
  context?: { used: number; window: number }; // W6：上一次请求的上下文用量
  goal?: GoalState | null; // W7：goal 模式
}

export interface GoalState {
  objective: string;
  status: "planning" | "working" | "verifying" | "done" | "paused" | "failed" | "stopped";
  plan: string;
  criteria: string[];
  device_steps: boolean;
  round: number;
  max_rounds: number;
  verdicts: Json[];
  report: Json | null;
  message: string;
  usage: Json;
  notes?: RoundNote[]; // 2026-10-05：每轮的进展记录（系统测出来的）
  stalled?: number;
}

export interface RoundNote {
  round: number; files: string[]; builds: number; build_ok: boolean | null; flashes: number; flash_ok: boolean | null;
  device: "pass" | "fail" | "crash" | "timeout" | "interrupted" | "none"; device_line: string; crashes: number;
  report: string | null; verdict: string | null; passed: number | null; note: string; progress: boolean; why: string;
}

export interface Board {
  id: string;
  chip: string | null;
  alias: string;
  port: string | null;
  state: string;
  owner_session: string | null;
  idle_policy: "ignore" | "notify" | null;
  usb_jtag: boolean;
  /** 2026-10-06：usb_serial_jtag / usb_otg / uart_bridge / unknown（旧核心没有这个字段） */
  link?: string;
  mac?: string | null;
  stable_id: boolean;
  description: string;
}

export interface DeviceEvent {
  id: string;
  board_id: string;
  kind: string;
  severity: "info" | "warn" | "critical";
  at: string;
  summary: string;
  detail: Json;
  backtrace: { frames: { pc: string; function?: string; file?: string; line?: number; internal: boolean }[]; decoded: boolean; raw: string; corrupted: boolean } | null;
  log_ref: { board_id: string; file: string; start: number; end: number } | null;
}

export interface IdleNotice {
  id: string;
  event: DeviceEvent;
  sessionId: string | null;
  dismissed?: boolean;
}

export interface ModelInfo {
  id: string;
  model: string;
  vision: boolean;
  contextWindow: number;
  efforts?: string[]; // 可选的思考程度（不支持推理的模型为空）
}

interface State {
  connected: boolean;
  coreInfo: Json | null;
  sessions: SessionMeta[];
  current: string | null;
  views: Record<string, SessionView>;
  models: ModelInfo[];
  defaultModel: string | null;
  boards: Record<string, Board>;
  devicesAvailable: boolean;
  serial: Record<string, string[]>;
  events: Record<string, DeviceEvent[]>;
  notices: IdleNotice[];
  sizes: Record<string, Json>;
  checkpoints: Record<string, CheckpointState>;
  settings: Json;
  showNewSession: boolean;
  showSettings: boolean; // 2026-10-05：设置页（模型、默认值）
  showSetup: boolean; // 2026-10-06：首次启动向导（ESP-IDF、模型、板子）
  bootErrors: string[];
  // 窄窗口下设备栏 / 会话栏变成抽屉；宽窗口下 devicesOpen=false 表示用户把设备栏收起来了
  devicesOpen: boolean;
  sidebarOpen: boolean;
  set: (p: Partial<State>) => void;
}

export const useStore = create<State>((set) => ({
  connected: false,
  coreInfo: null,
  sessions: [],
  current: null,
  views: {},
  models: [],
  defaultModel: null,
  boards: {},
  devicesAvailable: false,
  serial: {},
  events: {},
  notices: [],
  sizes: {},
  checkpoints: {},
  settings: {},
  showNewSession: false,
  showSettings: false,
  showSetup: false,
  bootErrors: [],
  devicesOpen: typeof window === "undefined" || window.innerWidth > 1180,
  sidebarOpen: false,
  set: (p) => set(p),
}));

let seq = 0;
const uid = () => `i${Date.now().toString(36)}${(seq++).toString(36)}`;

export function emptyView(mode = "default"): SessionView {
  return { items: [], status: "idle", mode, loaded: false, promptPending: false };
}

function textOf(content: Json): string {
  if (!content) return "";
  if (Array.isArray(content)) return content.map((c) => textOf(c.content ?? c)).join("\n");
  if (content.type === "text") return content.text ?? "";
  if (content.content) return textOf(content.content);
  return "";
}

/** 把一条 session/update 折叠进时间线（纯函数，便于测试） */
export function reduceUpdate(view: SessionView, u: Json): SessionView {
  const items = view.items.slice();
  const last = items[items.length - 1];
  switch (u.sessionUpdate) {
    case "user_message_chunk":
      // 用户发了话：排队的后台子 agent 报告会在这一轮交给 agent，卡片上的"继续"按钮收起
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        if (it.kind === "subagent" && it.awaitingParent) items[i] = { ...it, awaitingParent: false };
      }
      items.push({ kind: "user", id: uid(), text: textOf(u.content) });
      break;
    case "agent_message_chunk":
    case "agent_thought_chunk": {
      const piece = textOf(u.content);
      const thought = u.sessionUpdate === "agent_thought_chunk";
      if (last && last.kind === "agent" && last.open) {
        items[items.length - 1] = {
          ...last,
          text: thought ? last.text : last.text + piece,
          thought: thought ? last.thought + piece : last.thought,
        };
      } else {
        items.push({ kind: "agent", id: uid(), text: thought ? "" : piece, thought: thought ? piece : "", open: true });
      }
      break;
    }
    case "tool_call": {
      closeAgent(items);
      items.push({
        kind: "tool",
        id: u.toolCallId,
        title: u.title ?? "",
        toolKind: u.kind ?? "other",
        status: u.status ?? "pending",
        input: u.rawInput ?? {},
        output: "",
        progress: "",
        meta: {},
      });
      break;
    }
    case "tool_call_update": {
      const i = findTool(items, u.toolCallId);
      if (i < 0) break;
      const t = items[i] as Extract<Item, { kind: "tool" }>;
      const text = textOf(u.content);
      const done = u.status === "completed" || u.status === "failed";
      items[i] = {
        ...t,
        status: u.status ?? t.status,
        startedAt: t.startedAt ?? (u.status === "in_progress" ? Date.now() : undefined),
        output: done ? text : t.output,
        progress: !done && text ? text : t.progress,
        meta: u.rawOutput ?? t.meta,
      };
      break;
    }
    case "_fwr/injected":
      closeAgent(items);
      if (u.source === "interjection") {
        // 排队的插话送到了：去掉"排队中"的卡片（下面的 injected 卡片就是送达的那条）
        for (let i = items.length - 1; i >= 0; i--) if (items[i].kind === "note") items.splice(i, 1);
      }
      items.push({ kind: "injected", id: uid(), source: u.source, text: u.text });
      break;
    case "_fwr/turn_end":
      closeAgent(items);
      // 回合结束时还没完成的工具（取消、中断）不会再有结果，收尾成失败，别让它一直转圈
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        if (it.kind === "tool" && (it.status === "pending" || it.status === "in_progress")) {
          items[i] = { ...it, status: "failed", output: it.output || `Not finished (${u.stopReason})`,
                       permission: it.permission && !it.permission.answered ? { ...it.permission, answered: "reject" } : it.permission };
        }
        if (it.kind === "note") {
          // 回合结束前没来得及送达：核心留着它，下一条消息时一起交给 agent
          items[i] = { kind: "notice", id: it.id, tone: "info",
                       text: `The agent finished before your note was delivered; it will be included with your next message: ${it.text}` };
        }
      }
      items.push({ kind: "turn_end", id: uid(), stopReason: u.stopReason, usage: u.usage, error: u.error });
      break;
    case "_fwr/checkpoint":
      // 每轮的点画在时间线条上；对话里只标出"回退"
      if (u.entry?.kind !== "restore") return view;
      closeAgent(items);
      items.push({ kind: "restore", id: uid(), entry: u.entry, flash: u.flash ?? null });
      break;
    case "_fwr/prebuild": {
      // 后台首次编译：只占一条，进度原地更新
      const i = items.findIndex((it) => it.kind === "prebuild");
      const it = { kind: "prebuild" as const, id: i >= 0 ? items[i].id : uid(), status: u.status, text: u.text ?? "",
                   durationMs: u.durationMs };
      if (i >= 0) items[i] = it;
      else items.push(it);
      break;
    }
    case "_fwr/subagent": {
      const i = items.findIndex((it) => it.kind === "subagent" && it.id === u.subagentId);
      const it = { kind: "subagent" as const, id: u.subagentId, agentKind: u.kind, description: u.description,
                   status: u.status, stop: u.stop, steps: u.steps, files: u.files, text: u.text, usage: u.usage,
                   background: !!u.background, awaitingParent: !!u.awaitingParent };
      if (i >= 0) items[i] = it;
      else {
        closeAgent(items);
        items.push(it);
      }
      break;
    }
    case "_fwr/subagent_update": {
      // 子 agent 的回合结束不画分隔线（子 agent 卡片上已经有步数和用量），否则看起来像主会话结束了
      if (u.update?.sessionUpdate === "_fwr/turn_end") return view;
      // 子 agent 的工具调用：和普通工具卡片一样折叠进时间线，标出是哪个子 agent（它的审批请求要找得到这张卡片）
      const inner = reduceUpdate({ ...view, items }, u.update);
      const out = inner.items.map((it) => (it.kind === "tool" && it.id.startsWith(`${u.subagentId}:`) ? { ...it, sub: u.label } : it));
      return { ...view, items: out };
    }
    case "_fwr/goal":
      return { ...view, items, goal: u.goal };
    case "_fwr/compacting": {
      closeAgent(items);
      items.push({ kind: "compaction", id: uid(), status: "running", reason: u.reason, before: u.used });
      break;
    }
    case "_fwr/compacted": {
      // 更新最近一条"正在压缩"；手动压缩失败等没有前一条时新加一条
      let i = -1;
      for (let j = items.length - 1; j >= 0; j--) {
        const it = items[j];
        if (it.kind === "compaction" && it.status === "running") { i = j; break; }
      }
      const it = { kind: "compaction" as const, id: i >= 0 ? items[i].id : uid(), status: u.ok ? "ok" as const : "failed" as const,
                   reason: u.reason, before: u.before, after: u.after, segment: u.segment, error: u.error,
                   durationMs: u.durationMs };
      if (i >= 0) items[i] = it;
      else items.push(it);
      return { ...view, items, context: u.ok && u.after ? { used: u.after, window: view.context?.window ?? 0 } : view.context };
    }
    case "_fwr/context":
      return { ...view, context: { used: u.used, window: u.window } };
    case "_fwr/session_ended":
      closeAgent(items);
      items.push({ kind: "notice", id: uid(), tone: u.problems?.length ? "warn" : "info",
                   text: (u.state === "merged" ? `Session merged${u.mergedCommit ? ` (${String(u.mergedCommit).slice(0, 8)})` : ""}; the worktree was removed.`
                          : u.state === "applied" ? "Changes applied to your project folder (uncommitted); the worktree was removed."
                            : "Session discarded; the worktree and branch were removed.")
                         + (u.problems?.length ? ` Not fully cleaned up: ${u.problems.join("; ")}` : "") });
      break;
    case "_fwr/notice":
      items.push({ kind: "notice", id: uid(), tone: u.tone ?? "info", text: String(u.text ?? "") });
      return { ...view, items };
    case "_fwr/status":
      return { ...view, items, status: u.status };
    case "current_mode_update":
      return { ...view, items, mode: u.currentModeId };
    default:
      return view;
  }
  return { ...view, items };
}

function closeAgent(items: Item[]): void {
  const last = items[items.length - 1];
  if (last && last.kind === "agent" && last.open) items[items.length - 1] = { ...last, open: false };
}

export function findTool(items: Item[], id: string): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "tool" && it.id === id) return i;
  }
  return -1;
}

export function updateView(sid: string, fn: (v: SessionView) => SessionView): void {
  const { views } = useStore.getState();
  const v = views[sid] ?? emptyView();
  useStore.setState({ views: { ...views, [sid]: fn(v) } });
}
