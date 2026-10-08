/** 界面动作：调用核心的 ACP 方法，并把核心推来的消息写进 store。 */
import { native, rpc, type Json } from "./rpc";
import {
  emptyView,
  findTool,
  reduceUpdate,
  updateView,
  useStore,
  type Board,
  type DeviceEvent,
  type HumanAsk,
  type PermissionAsk,
  type SessionMeta,
  type SessionView,
} from "./store";

const SERIAL_KEEP = 3000; // 每块板子在界面里保留的串口行数

// ------------------------------------------------------------------ 核心 → 界面

rpc.handlers({
  notification(method, params) {
    switch (method) {
      case "session/update": {
        const sid = params.sessionId as string;
        updateView(sid, (v) => reduceUpdate(v, params.update));
        const kind = params.update?.sessionUpdate;
        if (kind === "_fwr/status") patchSession(sid, { status: params.update.status });
        if (!params._meta?.replay) {
          if (kind === "_fwr/checkpoint") void refreshCheckpoints(sid);
          if (kind === "_fwr/session_ended") void refreshSessions().then(() => refreshCheckpoints(sid));
          // 后台子 agent 做完了、agent 却空着：系统通知，点开回到这个会话决定要不要继续
          if (kind === "_fwr/subagent" && params.update.awaitingParent)
            void native.notify("Background sub-agent finished", params.update.description ?? "", `subagent:${sid}`);
        }
        break;
      }
      case "_fwr/replay": {
        // session/load: the whole history arrives in one batch; fold it in a single pass and render once
        const sid = params.sessionId as string;
        const updates = (params.updates ?? []) as Json[];
        updateView(sid, (v) => updates.reduce((acc, u) => reduceUpdate(acc, u), v));
        const st = [...updates].reverse().find((u) => u.sessionUpdate === "_fwr/status");
        if (st) patchSession(sid, { status: st.status });
        break;
      }
      case "_fwr/boards/changed":
        putBoard(params.board);
        break;
      case "_fwr/serial/chunk": {
        const { serial } = useStore.getState();
        const lines = (serial[params.boardId] ?? []).concat(String(params.text).split("\n"));
        useStore.setState({ serial: { ...serial, [params.boardId]: lines.slice(-SERIAL_KEEP) } });
        break;
      }
      case "_fwr/event":
        pushEvent(params.event as DeviceEvent);
        break;
      case "_fwr/notify":
        onIdleCrash(params.event as DeviceEvent, params.sessionId ?? null);
        break;
    }
  },
  request(method, params, id) {
    if (method === "session/request_permission") {
      const sid = params.sessionId as string;
      const meta = params._meta?.fwr ?? {};
      const ask: PermissionAsk = {
        rpcId: id,
        title: params.toolCall?.title ?? "",
        reason: meta.reason ?? "",
        risk: meta.risk ?? "normal",
        subjects: meta.subjects ?? [],
        options: params.options ?? [],
      };
      updateView(sid, (v) => {
        const items = v.items.slice();
        const i = findTool(items, params.toolCall?.toolCallId);
        if (i >= 0) items[i] = { ...(items[i] as Json), permission: ask };
        return { ...v, items };
      });
      return; // 用户点按钮后由 answerPermission 回复
    }
    if (method === "_fwr/human/request") {
      const sid = params.sessionId as string;
      const ask: HumanAsk = { rpcId: id, title: params.title, instructions: params.instructions, kind: params.kind,
                              boardId: params.board_id };
      updateView(sid, (v) => ({ ...v, items: [...v.items, { kind: "human", id: `h${id}`, ask }] }));
      native.notify("Firmwright needs your hands", params.title, `human:${sid}`);
      return;
    }
    return Promise.resolve(null);
  },
  host(ev) {
    if (ev.type === "sidecar_restarted") void onCoreRestarted(ev.code);
    if (ev.type === "sidecar_exit") useStore.setState({ connected: false });
    if (ev.type === "notification_click" && typeof ev.tag === "string") {
      const sid = ev.tag.split(":")[1];
      if (sid) void selectSession(sid);
    }
  },
});

function patchSession(sid: string, patch: Partial<SessionMeta>): void {
  const { sessions } = useStore.getState();
  useStore.setState({ sessions: sessions.map((s) => (s.id === sid ? { ...s, ...patch } : s)) });
}

function putBoard(b: Board): void {
  const { boards } = useStore.getState();
  useStore.setState({ boards: { ...boards, [b.id]: b } });
}

function pushEvent(ev: DeviceEvent): void {
  const { events } = useStore.getState();
  const list = (events[ev.board_id] ?? []).concat(ev).slice(-200);
  useStore.setState({ events: { ...events, [ev.board_id]: list } });
}

function onIdleCrash(ev: DeviceEvent, sessionId: string | null): void {
  const { notices } = useStore.getState();
  useStore.setState({ notices: [...notices, { id: ev.id, event: ev, sessionId }] });
  // 替换已有的同 id 事件（通知里的调用栈已经解码）
  const { events } = useStore.getState();
  const list = (events[ev.board_id] ?? []).map((e) => (e.id === ev.id ? ev : e));
  useStore.setState({ events: { ...events, [ev.board_id]: list } });
  const board = useStore.getState().boards[ev.board_id];
  void native.notify(`${board?.alias ?? ev.board_id} crashed`, ev.summary, sessionId ? `crash:${sessionId}` : undefined);
}

// ------------------------------------------------------------------ 启动 / 重启

export async function boot(): Promise<void> {
  const info = await rpc.request("initialize", {
    protocolVersion: 1,
    clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
    clientInfo: { name: "firmwright-desktop", version: "0.1.0" },
  });
  useStore.setState({ connected: true, coreInfo: info, bootErrors: [] });
  // 各项分别加载：一项失败不影响别的，失败原因显示在界面上（不再静默显示"还没有会话"）
  const steps: [string, () => Promise<void>][] = [
    ["Models", refreshModels], ["Sessions", refreshSessions], ["Boards", refreshBoards], ["Settings", refreshSettings],
  ];
  const results = await Promise.allSettled(steps.map(([, fn]) => fn()));
  const errors = results.flatMap((r, i) => (r.status === "rejected" ? [`Loading ${steps[i][0].toLowerCase()} failed: ${String(r.reason)}`] : []));
  useStore.setState({ bootErrors: errors });
  // 首次启动向导（2026-10-06）：缺 ESP-IDF 或者一个模型都没有，而且用户没点过"以后再说"
  const fwr = info?._meta?.fwr;
  if (needsSetup(fwr?.idf, useStore.getState().models.length) && !fwr?.setupDismissed) {
    useStore.setState({ showSetup: true, showNewSession: false, showSettings: false });
  }
}

// ------------------------------------------------------------------ 首次启动向导（2026-10-06）

export interface IdfCandidate {
  source: "eim" | "legacy" | "folder";
  id: string;
  path: string;
  version: string | null;
  python: string | null;
  tools_path: string | null;
  eim_json: string | null;
  problem: string | null;
  warning: string | null;
}

export interface SetupStatus {
  idf: { active: { source: string; id: string; path: string; version: string | null; warning: string | null } | null; error: string | null };
  models: number;
  devices: boolean;
  dismissed: boolean;
  candidates?: IdfCandidate[];
  restartRequired?: boolean;
}

/** 还缺东西：没有可用的 ESP-IDF，或者没有模型 */
export function needsSetup(idf: SetupStatus["idf"] | undefined, models: number): boolean {
  return !idf?.active || models === 0;
}

export async function setupStatus(scan = false): Promise<SetupStatus> {
  return rpc.request("_fwr/setup/status", { scan });
}

export async function inspectIdfFolder(path: string): Promise<IdfCandidate[]> {
  return (await rpc.request("_fwr/setup/idf/inspect", { path })).candidates;
}

export async function selectIdf(c: IdfCandidate): Promise<SetupStatus> {
  const res: SetupStatus = await rpc.request("_fwr/setup/idf/select", {
    source: c.source, path: c.path, id: c.id, eimJson: c.eim_json, toolsPath: c.tools_path,
  });
  // 欢迎页、新建会话页读的是 initialize 时的 coreInfo：把新的 IDF 状态写回去；设备层可能刚启动
  const info = useStore.getState().coreInfo;
  if (info?._meta?.fwr) {
    useStore.setState({ coreInfo: { ...info, _meta: { ...info._meta, fwr: { ...info._meta.fwr, idf: res.idf, platform: res.idf.active ? "esp-idf" : null } } } });
  }
  await refreshBoards();
  return res;
}

export async function dismissSetup(): Promise<void> {
  await rpc.request("_fwr/setup/dismiss");
  const info = useStore.getState().coreInfo;
  if (info?._meta?.fwr) {
    useStore.setState({ coreInfo: { ...info, _meta: { ...info._meta, fwr: { ...info._meta.fwr, setupDismissed: true } } } });
  }
}

const RESTART_EXIT_CODE = 75; // 设置页"重启核心"：核心有意退出（server.py）

async function onCoreRestarted(code?: number): Promise<void> {
  rpc.failAllPending("The core restarted");
  const { views, current } = useStore.getState();
  // 已加载的会话全部重新加载（回放 ui-events），时间线先清空，避免重复
  const loaded = Object.keys(views).filter((sid) => views[sid].loaded);
  const fresh: Record<string, ReturnType<typeof emptyView>> = {};
  for (const sid of loaded) fresh[sid] = emptyView(views[sid].mode);
  useStore.setState({ views: fresh, serial: {}, connected: false });
  await boot();
  for (const sid of loaded) await loadSession(sid);
  if (current) {
    updateView(current, (v) => ({
      ...v,
      items: [...v.items, code === RESTART_EXIT_CODE
        ? { kind: "notice", id: `restart${Date.now()}`, tone: "info", text: "The core was restarted to apply settings; sessions were restored." }
        : { kind: "notice", id: `restart${Date.now()}`, tone: "warn",
            text: "The core process exited unexpectedly; it was restarted and sessions were restored. The running task was interrupted; ask the agent to continue." }],
    }));
  }
}

export async function refreshSessions(): Promise<void> {
  const res = await rpc.request<{ sessions: SessionMeta[] }>("_fwr/sessions/list");
  useStore.setState({ sessions: res.sessions });
}

export async function refreshModels(): Promise<void> {
  const res = await rpc.request("_fwr/models/list");
  useStore.setState({ models: res.models, defaultModel: res.default });
}

export async function refreshBoards(): Promise<void> {
  const res = await rpc.request("_fwr/boards/list");
  const boards: Record<string, Board> = {};
  for (const b of res.boards as Board[]) boards[b.id] = b;
  useStore.setState({ boards, devicesAvailable: res.available });
}

/** 设置页（2026-10-06）：改哪项就只传哪项；返回完整设置 */
export async function saveSettings(fields: Json): Promise<Json> {
  const res = await rpc.request("_fwr/settings/set", fields);
  useStore.setState({ settings: res });
  return res;
}

export async function saveMcp(name: string, server: Json): Promise<Json> {
  const res = await rpc.request("_fwr/mcp/save", { name, server });
  useStore.setState({ settings: res });
  return res;
}

export async function deleteMcp(name: string): Promise<Json> {
  const res = await rpc.request("_fwr/mcp/delete", { name });
  useStore.setState({ settings: res });
  return res;
}

export async function coreAbout(): Promise<Json> {
  return rpc.request("_fwr/core/about");
}

/** 重启核心（换 ESP-IDF、改 MCP 服务器后）。有会话在执行时核心会拒绝（RpcError，信息里有会话名） */
export async function restartCore(): Promise<void> {
  await rpc.request("_fwr/core/restart");
}

export async function refreshSettings(): Promise<void> {
  useStore.setState({ settings: await rpc.request("_fwr/settings/get") });
}

// ------------------------------------------------------------------ 会话

/** §5.2 新建会话。不是 git 仓库 / 没有提交时抛 RpcError（-32020 / -32021），由对话框问用户怎么办。 */
export async function newSession(opts: { cwd: string; boardId?: string | null; modelId?: string; title?: string;
                                        mode?: string; isolation?: "worktree" | "in_place"; carryDirty?: boolean;
                                        effort?: string }): Promise<string> {
  const res = await rpc.request("session/new", {
    cwd: opts.cwd,
    mcpServers: [],
    _meta: { fwr: { boardId: opts.boardId || null, modelId: opts.modelId, title: opts.title, mode: opts.mode,
                    isolation: opts.isolation, carryDirty: opts.carryDirty ?? true, effort: opts.effort } },
  });
  const sid = res.sessionId as string;
  const meta = res._meta?.fwr ?? {};
  const items: SessionView["items"] = [];
  if (meta.isolation === "worktree") {
    const carried: string[] = meta.carried ?? [];
    items.push({ kind: "notice", id: `wt${Date.now()}`, tone: "info",
                 text: `The agent works in a copy of your project at ${meta.worktree} (branch ${meta.branch}, from ${meta.target_branch ?? "the current commit"}`
                   + `${carried.length ? `, plus your ${carried.length} uncommitted change${carried.length === 1 ? "" : "s"}` : ""}). `
                   + "Your project folder is not touched until you finish: apply the changes to it, merge them as a commit, or discard." });
    if (meta.dirty?.length && !carried.length) {
      const n = meta.dirtyCount ?? meta.dirty.length;
      items.push({ kind: "notice", id: `dirty${Date.now()}`, tone: "warn",
                   text: `Your project has ${n} uncommitted change${n === 1 ? "" : "s"} (${meta.dirty.slice(0, 3).join(", ")}${n > 3 ? "…" : ""}) that were left out of the copy; the agent does not see them.` });
    }
  }
  updateView(sid, () => ({ ...emptyView(res.modes?.currentModeId), loaded: true, items }));
  await refreshSessions();
  useStore.setState({ current: sid, showNewSession: false, showSettings: false });
  void refreshCheckpoints(sid);
  return sid;
}

export async function inspectProject(cwd: string): Promise<Json> {
  return rpc.request("_fwr/project/inspect", { cwd });
}

export async function initGit(cwd: string, newProject = false): Promise<Json> {
  return rpc.request("_fwr/project/init_git", { cwd, newProject });
}

// ------------------------------------------------------------------ goal 模式（W7）

export async function startGoal(sid: string, objective: string, maxRounds: number): Promise<void> {
  const res = await rpc.request("_fwr/goal/start", { sessionId: sid, objective, maxRounds });
  updateView(sid, (v) => ({ ...v, goal: res.goal }));
}

export async function stopGoal(sid: string): Promise<void> {
  await rpc.request("_fwr/goal/stop", { sessionId: sid });
}

export async function loadGoal(sid: string): Promise<void> {
  try {
    const res = await rpc.request("_fwr/goal/get", { sessionId: sid });
    updateView(sid, (v) => ({ ...v, goal: res.goal }));
  } catch {
    /* 会话还没加载完 */
  }
}

// ------------------------------------------------------------------ 上下文（W6）

export async function compactSession(sid: string, instructions = ""): Promise<Json> {
  return rpc.request("_fwr/session/compact", { sessionId: sid, instructions });
}

export async function contextInfo(sid: string): Promise<Json> {
  return rpc.request("_fwr/context/info", { sessionId: sid });
}

// ------------------------------------------------------------------ checkpoint 与收尾（W5）

export async function refreshCheckpoints(sid: string): Promise<void> {
  try {
    const res = await rpc.request("_fwr/checkpoints/list", { sessionId: sid });
    const { checkpoints } = useStore.getState();
    useStore.setState({ checkpoints: { ...checkpoints, [sid]: res } });
  } catch {
    /* 会话还没加载完：下次再取 */
  }
}

export async function stopSubagent(sid: string, subagentId: string): Promise<boolean> {
  const res = await rpc.request("_fwr/subagent/stop", { sessionId: sid, subagentId });
  return !!res?.stopped;
}

export async function previewRestore(sid: string, seq: number): Promise<Json> {
  return rpc.request("_fwr/checkpoints/preview", { sessionId: sid, seq });
}

export async function restoreCheckpoint(sid: string, seq: number, reflash: boolean): Promise<Json> {
  const res = await rpc.request("_fwr/checkpoints/restore", { sessionId: sid, seq, reflash });
  await refreshCheckpoints(sid);
  return res;
}

export async function sessionDiff(sid: string): Promise<Json> {
  return rpc.request("_fwr/session/diff", { sessionId: sid });
}

export async function listBranches(sid: string): Promise<{ branches: string[]; default: string | null }> {
  return rpc.request("_fwr/project/branches", { sessionId: sid });
}

export async function exportPatch(sid: string): Promise<Json> {
  return rpc.request("_fwr/session/export_patch", { sessionId: sid });
}

export async function mergeSession(sid: string, targetBranch: string, message: string): Promise<Json> {
  const res = await rpc.request("_fwr/session/merge", { sessionId: sid, targetBranch, message });
  await refreshSessions();
  return res;
}

export async function applySession(sid: string): Promise<Json> {
  const res = await rpc.request("_fwr/session/apply", { sessionId: sid });
  await refreshSessions();
  return res;
}

export async function discardSession(sid: string): Promise<Json> {
  const res = await rpc.request("_fwr/session/discard", { sessionId: sid });
  await refreshSessions();
  return res;
}


export async function loadSession(sid: string): Promise<void> {
  const meta = useStore.getState().sessions.find((s) => s.id === sid);
  updateView(sid, () => emptyView());
  const res = await rpc.request("session/load", { sessionId: sid, cwd: meta?.cwd ?? "", mcpServers: [] });
  updateView(sid, (v) => ({ ...v, loaded: true, mode: res.modes?.currentModeId ?? v.mode,
                            status: res._meta?.fwr?.status ?? v.status }));
  await refreshSessions();
}

export async function selectSession(sid: string): Promise<void> {
  useStore.setState({ current: sid, showNewSession: false, showSettings: false });
  const v = useStore.getState().views[sid];
  if (!v || !v.loaded) await loadSession(sid);
  void refreshCheckpoints(sid);
  void loadGoal(sid);
}

export async function sendPrompt(sid: string, text: string, images: { mimeType: string; data: string }[] = [],
                                 now = false): Promise<void> {
  const prompt: Json[] = [{ type: "text", text }];
  for (const img of images) prompt.push({ type: "image", mimeType: img.mimeType, data: img.data });
  const running = useStore.getState().views[sid]?.status !== "idle";
  if (running) {
    // 运行中再发 = 插话：核心在下一步注入；now = 停掉当前这一步马上送达（真机实测：一条命令跑了 10 分钟，插话一直送不到）
    updateView(sid, (v) => ({ ...v, items: [...v.items, { kind: "note", id: `ij${Date.now()}`, text, now }] }));
    await rpc.request("session/prompt", { sessionId: sid, prompt, _meta: { fwr: { now } } });
    return;
  }
  updateView(sid, (v) => ({ ...v, promptPending: true }));
  try {
    const res = await rpc.request("session/prompt", { sessionId: sid, prompt });
    const err = res?._meta?.fwr?.error;
    if (err) {
      updateView(sid, (v) => ({ ...v, items: [...v.items, { kind: "notice", id: `e${Date.now()}`, tone: "error",
                                                             text: `Error: ${err}` }] }));
    }
  } catch (e) {
    updateView(sid, (v) => ({ ...v, items: [...v.items, { kind: "notice", id: `e${Date.now()}`, tone: "error",
                                                           text: String(e) }] }));
  } finally {
    updateView(sid, (v) => ({ ...v, promptPending: false }));
  }
}

/** 停掉正在执行的那一步（轮次继续，排队的插话在下一步送达）。烧录不会被停掉。 */
export async function interruptStep(sid: string): Promise<string[]> {
  const res = await rpc.request("_fwr/session/interrupt", { sessionId: sid });
  return res?.stopped ?? [];
}

export function cancelTurn(sid: string): void {
  rpc.notify("session/cancel", { sessionId: sid });
}

export async function setMode(sid: string, modeId: string): Promise<void> {
  await rpc.request("session/set_mode", { sessionId: sid, modeId });
  patchSession(sid, { permission_mode: modeId });
}

export async function setModel(sid: string, modelId: string): Promise<void> {
  await rpc.request("_fwr/session/set_model", { sessionId: sid, modelId });
  const efforts = useStore.getState().models.find((m) => m.id === modelId)?.efforts ?? [];
  const cur = useStore.getState().sessions.find((s) => s.id === sid)?.effort;
  patchSession(sid, { model_id: modelId, ...(cur && !efforts.includes(cur) ? { effort: null } : {}) });
}

/** 思考程度（2026-10-05）："default" = 用服务商默认，不发参数 */
export async function setEffort(sid: string, effort: string): Promise<void> {
  const res = await rpc.request("_fwr/session/set_effort", { sessionId: sid, effort });
  patchSession(sid, { effort: res?.effort ?? null });
}

/** 串口面板空着时的"复位板子"（2026-10-06）：看启动日志。板子的会话正在执行时核心会拒绝 */
export async function resetBoard(boardId: string): Promise<void> {
  await rpc.request("_fwr/boards/reset", { boardId });
}

export async function bindBoard(sid: string, boardId: string | null, take = false): Promise<void> {
  const prevOwner = take && boardId ? useStore.getState().boards[boardId]?.owner_session : null;
  await rpc.request("_fwr/session/bind_board", { sessionId: sid, boardId, take });
  patchSession(sid, { board_id: boardId });
  if (prevOwner && prevOwner !== sid) patchSession(prevOwner, { board_id: null });  // 板子从那个会话移走了
}

/** 设备栏：重新扫描串口；识别芯片（esptool flash_id，会复位板子） */
export async function rescanBoards(): Promise<void> {
  await rpc.request("_fwr/boards/rescan", {});
  await refreshBoards();
}

export async function identifyBoard(boardId: string): Promise<Json> {
  return rpc.request("_fwr/boards/identify", { boardId });
}

export function answerPermission(sid: string, toolCallId: string, optionId: string): void {
  const v = useStore.getState().views[sid];
  const i = v ? findTool(v.items, toolCallId) : -1;
  if (i < 0) return;
  const t = v.items[i] as Json;
  if (!t.permission || t.permission.answered) return;
  rpc.respond(t.permission.rpcId, { outcome: { outcome: "selected", optionId } });
  updateView(sid, (view) => {
    const items = view.items.slice();
    items[i] = { ...t, permission: { ...t.permission, answered: optionId } };
    return { ...view, items };
  });
}

export function answerHuman(sid: string, itemId: string, done: boolean, note: string): void {
  updateView(sid, (v) => {
    const items = v.items.map((it) => {
      if (it.kind !== "human" || it.id !== itemId || it.ask.answered) return it;
      rpc.respond(it.ask.rpcId, { done, note });
      return { ...it, ask: { ...it.ask, answered: { done, note } } };
    });
    return { ...v, items };
  });
}

// ------------------------------------------------------------------ 设备（W4）

export async function loadSerialTail(boardId: string): Promise<void> {
  const res = await rpc.request("_fwr/serial/tail", { boardId, lines: 400 });
  const { serial } = useStore.getState();
  useStore.setState({ serial: { ...serial, [boardId]: res.text ? String(res.text).split("\n") : [] } });
  const evs = await rpc.request("_fwr/events/recent", { boardId, limit: 50 });
  const { events } = useStore.getState();
  useStore.setState({ events: { ...events, [boardId]: evs.events } });
}

export async function updateBoard(boardId: string, fields: Partial<Board>): Promise<void> {
  const res = await rpc.request("_fwr/boards/update", { boardId, ...fields });
  putBoard(res.board);
}

export async function refreshSize(sid: string): Promise<void> {
  const res = await rpc.request("_fwr/firmware/size", { sessionId: sid });
  const { sizes } = useStore.getState();
  // flashed：板子上那份固件的 app 大小（核心从烧录存档里读），用来显示"比上次烧录大了多少"
  useStore.setState({ sizes: { ...sizes, [sid]: res.size ? { ...res.size, flashed: res.flashed ?? null } : null } });
}

/** 模拟板控制（FIRMWRIGHT_SIM_BOARD=1）：触发一次崩溃 / 拔插 */
export async function simCrash(): Promise<void> {
  await rpc.request("_fwr/sim/crash");
}
export async function simPlug(present: boolean): Promise<void> {
  await rpc.request("_fwr/sim/plug", { present });
}

export async function setIdlePolicy(policy: "ignore" | "notify"): Promise<void> {
  useStore.setState({ settings: await rpc.request("_fwr/settings/set", { idlePolicy: policy }) });
}

// ---- 设置页：模型管理（2026-10-05）。密钥只发给核心，核心不回传

export interface ModelDetail {
  id: string; source: "config.toml" | "settings"; model: string; baseUrl: string; contextWindow: number;
  vision: boolean; reasoning: boolean; effortStyle: string; keyRef: string; hasKey: boolean;
}

export async function loadModelDetail(): Promise<{ default: string | null; models: ModelDetail[]; configPath: string }> {
  return rpc.request("_fwr/models/detail");
}

export async function saveModel(id: string, fields: Json, apiKey: string): Promise<ModelDetail> {
  const res = await rpc.request("_fwr/models/save", { id, fields, apiKey: apiKey || null });
  await refreshModels();
  return res.model;
}

export async function deleteModel(id: string): Promise<void> {
  await rpc.request("_fwr/models/delete", { id });
  await refreshModels();
}

export async function testModel(id: string, fields: Json | null, apiKey: string): Promise<Json> {
  return rpc.request("_fwr/models/test", { id, fields, apiKey: apiKey || null });
}

export async function setDefaultModel(id: string): Promise<void> {
  useStore.setState({ settings: await rpc.request("_fwr/settings/set", { defaultModel: id }) });
  await refreshModels();
}

export function dismissNotice(id: string): void {
  const { notices } = useStore.getState();
  useStore.setState({ notices: notices.map((n) => (n.id === id ? { ...n, dismissed: true } : n)) });
}

/** 空闲崩溃卡片上的"让 agent 处理"：由用户决定，按下才发起任务（I04：不自动处理） */
export async function handOffCrash(notice: { id: string; event: DeviceEvent; sessionId: string | null }): Promise<void> {
  if (!notice.sessionId) return;
  dismissNotice(notice.id);
  await selectSession(notice.sessionId);
  const ev = notice.event;
  const text = `The board crashed while idle (event ${ev.id}): ${ev.summary}\nUse diagnose_crash(event_id="${ev.id}") to look at it, find the cause and fix it, then flash and confirm with the device output.`;
  void sendPrompt(notice.sessionId, text);
}
