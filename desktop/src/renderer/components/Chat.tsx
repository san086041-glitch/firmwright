import { memo, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { answerPermission, bindBoard, cancelTurn, interruptStep, sendPrompt, setEffort, setMode, setModel, startGoal } from "../actions";
import { t, tc, tk, useLang } from "../i18n";
import { native, type Json } from "../rpc";
import { useStore, type Board, type Item, type PermissionAsk } from "../store";
import { CrashBlock, HumanCard } from "./Cards";
import { ContextMeter } from "./Context";
import { DiffInline } from "./Diff";
import { GoalBar, SubagentCard } from "./Goal";
import { Icon } from "./Icon";
import { PanelToggles } from "./PanelToggles";
import { Select } from "./Select";
import { fmtDuration, fmtK, TOOL_ICON, KIND_ICON, toolSummary } from "./util";
import { CheckpointStrip, FinishDialog } from "./Workspace";

export const MODES: [string, string, string][] = [
  ["default", tk("Ask before edits"), tk("File edits and non-read-only commands need your approval")],
  ["accept_edits", tk("Auto-accept edits"), tk("File edits are applied without asking; risky commands still ask")],
  ["plan", tk("Plan (read-only)"), tk("The agent can only read and investigate")],
  ["always_approve", tk("Approve all"), tk("Runs everything without asking, except dangerous hardware operations and writes outside the working directory")],
];

/** 标题栏板子胶囊的外观：圆点颜色、名字后面的状态字（正常时不写）、悬停说明 */
function boardAppearance(wanted: string | null | undefined, board: Board | undefined, sid: string) {
  const pick = t("Board bound to this session (a board belongs to one session at a time)");
  if (!wanted) return { cls: "", dot: "off", text: "", tip: pick };
  // 会话记着这块板子，但实际没绑上（恢复会话时板子不在 / 被占用）：如实显示，等板子连上后核心会自动绑回来
  if (!board || board.owner_session !== sid)
    return { cls: "warn", dot: "awaiting_human", text: t("waiting"),
             tip: t("This session's board is not bound right now (it was offline or in use when the session was restored). It is bound again automatically when it connects and is free; or pick it again from this menu.") };
  const name = board.port ? t("{board} on {port}", { board: board.alias, port: board.port }) : board.alias;
  switch (board.state) {
    case "crashed": return { cls: "bad", dot: "crashed", text: t("crashed"), tip: `${name}: ${t("crashed")}` };
    case "disconnected": return { cls: "warn", dot: "awaiting_human", text: t("offline"), tip: `${board.alias}: ${t("offline")}` };
    case "flashing": return { cls: "", dot: "running", text: t("flashing"), tip: `${name}: ${t("flashing")}` };
    case "busy": return { cls: "warn", dot: "awaiting_human", text: t("busy"), tip: `${name}: ${t("busy / download mode")}` };
    default: return { cls: "", dot: "ok", text: "", tip: `${name}: ${t(board.state)}. ${pick}` };
  }
}

export function ChatView({ sid }: { sid: string }) {
  const meta = useStore((s) => s.sessions.find((x) => x.id === sid));
  const view = useStore((s) => s.views[sid]);
  const models = useStore((s) => s.models);
  const boards = useStore((s) => s.boards);
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const [err, setErr] = useState("");
  const [finishing, setFinishing] = useState(false);

  if (!meta) return <div className="empty-chat">{t("Session not found")}</div>;
  const status = view?.status ?? "idle";
  const ended = meta.state === "merged" || meta.state === "applied" || meta.state === "discarded";
  const board = meta.board_id ? boards[meta.board_id] : undefined;
  const sessions = useStore.getState().sessions;
  const ownerTitle = (id: string) => sessions.find((x) => x.id === id)?.title ?? id;
  const boardLook = boardAppearance(meta.board_id, board, sid);

  return (
    <>
      <div className="chat-head">
        <span className="only-narrow-sidebar"><PanelToggles inline /></span>
        <div className="titles">
          <h2 title={meta.title}>{meta.title}</h2>
          <div className="meta">
            <Icon name="folder" size={13} />
            {/* 显示用户选的工程；worktree 副本的位置放在提示里（真机实测：用户以为会话"跳到了 C 盘"） */}
            <span className="path" title={meta.project_root}>{meta.project_root}</span>
            {meta.isolation === "worktree" && !ended && (
              <span className="chip" title={t("The agent works in an isolated copy at {path}.\nYour project folder is not touched until you finish the session.", { path: meta.cwd })}>
                {t("isolated copy")}
              </span>
            )}
            {meta.isolation === "worktree" && meta.branch && !ended && (
              <span className="branch" title={t("worktree {path}\nbranched from {branch} at {commit}", { path: meta.worktree, branch: meta.target_branch ?? t("detached HEAD"), commit: meta.base_commit?.slice(0, 8) ?? "" })}>
                {/* 来源分支只放提示里：标题栏窄的时候 "← master" 会把分支名本身挤没 */}
                <Icon name="branch" size={12} />{meta.branch}
              </span>
            )}
            {meta.isolation !== "worktree" && !ended && <span className="chip warn" title={t("The agent edits your project folder directly (no worktree isolation)")}>{t("in place")}</span>}
            {meta.state === "merged" && <span className="chip ok">{t("merged {commit}", { commit: meta.merged_commit?.slice(0, 8) ?? "" })}</span>}
            {meta.state === "applied" && <span className="chip ok">{t("applied")}</span>}
            {meta.state === "discarded" && <span className="chip">{t("discarded")}</span>}
          </div>
        </div>
        {!ended && (
          <div className="head-tools">
            {/* 板子选择和状态合成一个胶囊（原来是下拉框 + 单独的状态标签，两个 USB 图标、高度不一，挤掉了分支名）：
                前面的圆点是状态，只有不正常时才在名字后面写出来 */}
            {devicesAvailable && (
              <Select variant="pill" value={meta.board_id ?? ""} width={280} className={`board-pill ${boardLook.cls}`}
                      icon={meta.board_id ? <span className={`dot ${boardLook.dot}`} /> : <Icon name="usb" size={13} />}
                      renderValue={(o) => (
                        // 记着的板子现在根本没插着（不在列表里）：没有名字可显示，直接写"等板子"
                        !o && meta.board_id ? <>{t("Waiting for board")}</>
                          : <>{o?.label ?? t("No board")}{boardLook.text && <span className="board-state">{boardLook.text}</span>}</>
                      )}
                      title={boardLook.tip}
                      options={[{ value: "", label: t("No board") }, ...Object.values(boards).map((b) => ({
                        value: b.id, label: b.alias, badge: b.state === "disconnected" ? t("offline") : b.chip ?? undefined,
                        hint: b.owner_session && b.owner_session !== sid ? t("In use by “{title}”; pick to move it here", { title: ownerTitle(b.owner_session) }) : b.port ?? undefined,
                      }))]}
                      onChange={(id) => {
                        const b = id ? boards[id] : undefined;
                        // 板子在别的会话手里：确认后移过来（原会话正在执行时核心会拒绝并说明原因）
                        const take = !!b?.owner_session && b.owner_session !== sid;
                        if (take && !window.confirm(t("Move {board} from session “{title}” to this session?\nThat session will have no board until you bind one again.", { board: b!.alias, title: ownerTitle(b!.owner_session!) }))) return;
                        bindBoard(sid, id || null, take).catch((x) => setErr(String(x instanceof Error ? x.message : x)));
                      }} />
            )}
            {meta.isolation === "worktree" && (
              <button className="btn sm" onClick={() => setFinishing(true)} title={t("Apply the changes to your project folder, merge them as a commit, export a patch, or discard")}>
                <Icon name="merge" size={14} />{t("Finish")}
              </button>
            )}
          </div>
        )}
        <span className="hide-narrow-sidebar"><PanelToggles inline /></span>
      </div>
      <CheckpointStrip sid={sid} />
      <GoalBar sid={sid} />
      {err && <div className="callout error" style={{ margin: "10px 24px 0" }} onClick={() => setErr("")}><Icon name="alert" />{tc(err)}</div>}
      <Timeline sid={sid} items={view?.items ?? []} loaded={!!view?.loaded} />
      {ended ? (
        <div className="composer ended">
          {meta.state === "applied" ? t("This session was applied to the project folder and its worktree removed; the history is read-only. Start a new session to keep working.")
            : meta.state === "merged" ? t("This session was merged and its worktree removed; the history is read-only. Start a new session to keep working.")
              : t("This session was discarded and its worktree removed; the history is read-only. Start a new session to keep working.")}
        </div>
      ) : (
        <Composer sid={sid} status={status} modelId={meta.model_id} mode={view?.mode ?? meta.permission_mode} models={models.map((m) => m.id)} />
      )}
      {finishing && <FinishDialog sid={sid} onClose={() => setFinishing(false)} />}
    </>
  );
}

// ------------------------------------------------------------------ timeline

type ToolItem = Extract<Item, { kind: "tool" }>;
type AgentItem = Extract<Item, { kind: "agent" }>;
type GroupItem = ToolItem | AgentItem;  // 工具调用，或者夹在工具调用之间、只有思考没有正文的 agent 消息
type Row = { key: string; item?: Item; tools?: GroupItem[] };

const thoughtOnly = (it: Item): it is AgentItem => it.kind === "agent" && !it.text?.trim() && !!it.thought;

function Timeline({ sid, items, loaded }: { sid: string; items: Item[]; loaded: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // 连续的工具调用合并进一张卡片；中间只有思考、没有正文的 agent 消息也并进去（一行），不再把卡片切成好几段
  const rows = useMemo(() => {
    const out: Row[] = [];
    for (const it of items) {
      const last = out[out.length - 1];
      if (it.kind === "tool" || thoughtOnly(it)) {
        if (last?.tools) last.tools.push(it);
        else out.push({ key: `g${it.id}`, tools: [it] });
      } else out.push({ key: it.id, item: it });
    }
    // 结尾只有思考的组（模型还在想）不算工具组
    return out.map((r) => (r.tools && r.tools.every((t) => t.kind === "agent") ? { key: r.key, item: r.tools[0] } : r));
  }, [items]);
  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items]);
  return (
    <div className="timeline" ref={ref} onScroll={(e) => {
      const el = e.currentTarget;
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    }}>
      {items.length === 0 && (loaded ? <EmptyChat sid={sid} /> : <div className="empty-chat"><Icon name="refresh" className="spin" /> {t("Loading…")}</div>)}
      {rows.map((r) => (r.tools ? <ToolGroup key={r.key} sid={sid} tools={r.tools} /> : <ItemView key={r.key} sid={sid} it={r.item!} />))}
    </div>
  );
}

const IDEAS: [string, string][] = [
  ["bug", tk("The board crashes right after boot. Find out why and fix it.")],
  ["pulse", tk("Make the LED blink at 2 Hz and confirm the timing on the device.")],
  ["search", tk("Explain how this project initializes its peripherals.")],
];

function EmptyChat({ sid }: { sid: string }) {
  return (
    <div className="empty-chat">
      <Icon name="sparkles" size={22} style={{ color: "var(--accent)" }} />
      <div>{t("Describe a task. Firmwright edits code, builds, flashes the board and reads its output to check the result.")}</div>
      <div className="ideas">
        {/* 示例任务按界面语言发出去：agent 跟随用户语言回答 */}
        {IDEAS.map(([icon, text]) => (
          <button key={text} onClick={() => void sendPrompt(sid, t(text))}><Icon name={icon as "bug"} />{t(text)}</button>
        ))}
      </div>
    </div>
  );
}

const ItemView = memo(function ItemView({ sid, it }: { sid: string; it: Item }) {
  useLang();  // memo 组件：切换语言时自己重新渲染
  switch (it.kind) {
    case "user":
      return <UserMessage text={it.text} />;
    case "agent":
      return (
        <div className="msg-agent">
          {it.thought && (
            <details className="thought">
              <summary><Icon name="chevron" size={12} className="chev" /><Icon name="brain" size={13} />{t("Thinking · {n} chars", { n: fmtK(it.thought.length) })}</summary>
              <pre>{it.thought}</pre>
            </details>
          )}
          {it.text && <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{it.text}</ReactMarkdown></div>}
        </div>
      );
    case "injected": {
      // 回退时界面上已经有"Restored"卡片；交给 agent 的那段说明是给模型看的，不再显示一遍（2026-10-05：重复了 3 张）
      if (it.source === "checkpoint") return null;
      const s = SOURCE[it.source] ?? { label: it.source, icon: "info" as const, tone: "info" };
      // 核心注入的说明是英文（也给模型看），显示时翻译；用户自己的插话原样显示
      return (
        <div className={`callout ${it.source === "device_event" || it.source === "checkpoint" || it.source === "interjection" ? it.source : s.tone}`}>
          <Icon name={s.icon} />
          <div><span className="src">{t(s.label)}</span>{it.source === "interjection" ? it.text : tc(it.text)}</div>
        </div>
      );
    }
    case "human":
      return <HumanCard sid={sid} itemId={it.id} ask={it.ask} />;
    case "turn_end":
      return (
        <div className={`turn-end ${it.error ? "err" : ""}`}>
          {STOP_TEXT[it.stopReason] ? t(STOP_TEXT[it.stopReason]) : it.stopReason}
          {it.usage ? ` · ${t("{in} in ({cached} cached) · {out} out", { in: fmtK(it.usage.input_tokens), cached: fmtK(it.usage.cached_tokens), out: fmtK(it.usage.output_tokens) })}` : ""}
          {it.error ? ` · ${tc(it.error)}` : ""}
        </div>
      );
    case "note":
      return <QueuedNote sid={sid} text={it.text} now={!!it.now} />;
    case "notice":
      return <div className={`callout ${it.tone}`}><Icon name={it.tone === "info" ? "info" : "alert"} /><div>{tc(it.text)}</div></div>;
    case "restore": {
      const f = it.flash;
      return (
        <div className="callout checkpoint">
          <Icon name="history" />
          <div>
            <span className="src">{t("Restored")}</span>
            {t("Working directory restored to checkpoint #{n} ({commit})", { n: it.entry.restored_to, commit: it.entry.commit.slice(0, 8) })}
            {f ? (f.ok ? t("; the board was reflashed with that point's firmware (sha256 {sha})", { sha: String(f.image_sha256 ?? it.entry.firmware?.sha256 ?? t("unknown")).slice(0, 8) })
                       : t("; reflashing failed: {reason}", { reason: tc(f.summary) })) : ""}{t(". The agent will be told on its next turn.")}
          </div>
        </div>
      );
    }
    case "subagent":
      return <SubagentCard sid={sid} it={it} />;
    case "compaction":
      return (
        <div className={`status-line compaction ${it.status}`}>
          {it.status === "running" ? <Icon name="refresh" className="spin" size={14} /> : <Icon name={it.status === "ok" ? "layers" : "alert"} size={14} />}
          {it.status === "running"
            ? (it.reason === "manual" ? t("Compacting the conversation…") : t("Compacting the conversation (context almost full)…"))
            : it.status === "ok"
              ? t("Compacted ({reason}): ~{before} → {after} tokens", { reason: COMPACT_REASON[it.reason] ? t(COMPACT_REASON[it.reason]) : it.reason, before: fmtK(it.before), after: fmtK(it.after) })
                + `${it.segment ? ` · ${t("archived as {name}", { name: it.segment })}` : ""}${it.durationMs ? ` · ${fmtDuration(it.durationMs)}` : ""}`
              : t("Compaction failed: {error} (the conversation continues)", { error: tc(it.error ?? "") })}
        </div>
      );
    case "prebuild":
      return (
        <div className={`status-line ${it.status}`}>
          {it.status === "running" ? <Icon name="refresh" className="spin" size={14} /> : <Icon name={it.status === "ok" ? "check" : "alert"} size={14} />}
          {it.status === "running" ? (/^initial background build/i.test(it.text) || !it.text ? t("Initial background build…") : t("Initial background build: {progress}", { progress: tc(it.text) }))
            : it.status === "ok" ? (it.durationMs ? t("Initial build finished in {time}; later builds are incremental", { time: fmtDuration(it.durationMs) }) : t("Initial build finished; later builds are incremental"))
              : it.text.startsWith("cancelled") ? t("Initial build {status}", { status: tc(it.text) })
                : t("Initial build failed: {error} (the agent will see the details when it builds)", { error: tc(it.text) })}
        </div>
      );
    case "tool":
      return null;
  }
});

/** 执行中发的插话：等当前这一步做完才送达。可以"立即送达"（停掉当前这一步）。送达后这张卡片消失。 */
function QueuedNote({ sid, text, now }: { sid: string; text: string; now: boolean }) {
  const [stopping, setStopping] = useState(now);
  const [msg, setMsg] = useState("");
  const deliver = async () => {
    setStopping(true);
    const stopped = await interruptStep(sid).catch(() => []);
    if (!stopped.length) setMsg(tk("Nothing to stop right now (or the board is being flashed); it will be delivered at the next step."));
  };
  return (
    <div className="callout note-queued">
      <Icon name={stopping ? "refresh" : "edit"} className={stopping ? "spin" : ""} />
      <div>
        <span className="src">{stopping ? t("Your note · stopping the current step to deliver it") : t("Your note · queued for the agent's next step")}</span>
        {text}
        {msg && <div className="hint">{t(msg)}</div>}
      </div>
      {!stopping && <button className="btn xs" onClick={() => void deliver()} title={t("Stop the current step and deliver the note now")}>{t("Send now")}</button>}
    </div>
  );
}

/** goal 模式发给执行者的指令很长（含整份计划和规则），时间线上只显示目标本身，原文可展开 */
function UserMessage({ text }: { text: string }) {
  const goal = /^(?:\[Goal mode\] Goal: |【goal 模式】目标：)([\s\S]*?)(?:\n\n(?:Plan|计划)[\s\S]*)?$/.exec(text);
  const round = /^(?:\[Goal mode · round (\d+) of (\d+)\]|【goal 模式 · 第 (\d+) 轮 \/ 最多 (\d+) 轮】)\s*([\s\S]*)$/.exec(text);
  if (goal || round) {
    const label = goal ? t("Goal") : t("Goal · round {n} of {max}", { n: round![1] ?? round![3], max: round![2] ?? round![4] });
    const body = goal ? goal[1] : round![5];
    return (
      <div className="msg-user goal">
        <div className="bubble">
          <div className="goal-tag"><Icon name="target" size={12} />{label}</div>
          {goal ? body : <details><summary><Icon name="chevron" size={12} className="chev" />{t("Feedback to the implementer")}</summary><div style={{ marginTop: 6 }}>{body}</div></details>}
        </div>
      </div>
    );
  }
  return <div className="msg-user"><div className="bubble">{text}</div></div>;
}

const SOURCE: Record<string, { label: string; icon: "bolt" | "edit" | "refresh" | "history" | "sparkles" | "brain" | "file" | "info"; tone: string }> = {
  device_event: { label: tk("Device event · sent to the agent"), icon: "bolt", tone: "error" },
  interjection: { label: tk("Your note"), icon: "edit", tone: "info" },
  loop_guard: { label: tk("Loop guard"), icon: "refresh", tone: "warn" },
  checkpoint: { label: tk("Restored"), icon: "history", tone: "violet" },
  skills: { label: tk("Skills"), icon: "sparkles", tone: "info" },
  memory: { label: tk("Memory"), icon: "brain", tone: "info" },
  rules: { label: tk("Project rules"), icon: "file", tone: "info" },
  new_project: { label: tk("New project"), icon: "sparkles", tone: "info" },
  subagent: { label: tk("Background sub-agent report"), icon: "info", tone: "violet" },
};
const COMPACT_REASON: Record<string, string> = { auto: tk("context over 80%"), manual: tk("manual"), overflow: tk("the model reported an overflow") };
const STOP_TEXT: Record<string, string> = {
  end_turn: tk("Turn complete"), cancelled: tk("Cancelled"), max_steps: tk("Step limit reached"), loop_guard: tk("Stopped: the agent was looping"),
  model_error: tk("Model error"), readonly: tk("Session ended"), error: tk("Error"), interrupted: tk("Interrupted"),
};

// ------------------------------------------------------------------ tool calls

/** 一组连续的工具调用。做完的、超过 3 个调用的组折叠成一行摘要（"Read 4 files · ran 3 commands · build, flash"），
 *  点开看全部；正在执行、等审批的组保持展开（2026-10-05：用户觉得工具调用占的地方太大）。 */
function ToolGroup({ sid, tools }: { sid: string; tools: GroupItem[] }) {
  const calls = tools.filter((t): t is ToolItem => t.kind === "tool");
  const live = (t: ToolItem) => t.status === "in_progress" || t.status === "pending" || (!!t.permission && !t.permission.answered);
  const active = calls.some(live);
  const [open, setOpen] = useState<boolean | null>(null);
  // 原来执行中整组展开、做完再折叠：几个子 agent 并行时不停有新调用，整组在"展开上百行"和"一行摘要"之间
  // 来回切，页面上下乱跳（2026-10-05 真机）。现在超过 3 个调用就一直折叠；执行中在摘要下面固定显示
  // 最近 3 个调用 + 所有正在执行 / 等审批的调用，高度基本不变
  const collapsible = calls.length > 3;
  const isOpen = !collapsible || (open ?? false);
  const tail = new Set(active ? calls.slice(-3).map((t) => t.id) : []);
  const shown = isOpen ? tools : calls.filter((t) => tail.has(t.id) || live(t));
  const allSub = calls.length > 0 && calls.every((t) => t.sub);
  return (
    <div className={`tools ${allSub ? "sub" : ""}`}>
      {collapsible && (
        <button className={`tool-group-head ${isOpen ? "open" : ""}`} onClick={() => setOpen(!isOpen)}>
          <Icon name="chevron" size={12} className="chev" />
          {active && <Icon name="refresh" size={12} className="spin" />}
          <span className="what">{groupSummary(calls)}</span>
          {calls.some((c) => c.status === "failed") && <span className="err">{t("{n} failed", { n: calls.filter((c) => c.status === "failed").length })}</span>}
          <span className="meta">{t("{n} calls", { n: calls.length })} · {fmtDuration(calls.reduce((a, c) => a + (c.meta?.duration_ms ?? 0), 0))}</span>
        </button>
      )}
      {shown.map((t, i) => {
        if (t.kind === "agent") return <ThoughtRow key={t.id} text={t.thought ?? ""} />;
        // 子 agent 标签只在换了子 agent 时显示（和上一个工具调用比，跳过中间的思考行）
        const prev = shown.slice(0, i).reverse().find((x): x is ToolItem => x.kind === "tool");
        const showSub = !!t.sub && t.sub !== prev?.sub;
        return <ToolRow key={t.id} sid={sid} t={t} showSub={showSub} />;
      })}
    </div>
  );
}

const READS = new Set(["read_file", "list_dir", "grep", "read_log", "memory_get", "memory_search", "project_status", "size", "skill", "search_tool"]);
const EDITS = new Set(["edit_file", "write_file"]);

function groupSummary(calls: ToolItem[]): string {
  let reads = 0, edits = 0, cmds = 0;
  const others: string[] = [];
  for (const c of calls) {
    const name = c.title.split(" ")[0] || "tool";
    if (READS.has(name)) reads++;
    else if (EDITS.has(name)) edits++;
    else if (name === "shell") cmds++;
    else if (!others.includes(name)) others.push(name);
  }
  const parts = [];
  if (reads) parts.push(t(reads === 1 ? "{n} read" : "{n} reads", { n: reads }));
  if (edits) parts.push(t(edits === 1 ? "{n} edit" : "{n} edits", { n: edits }));
  if (cmds) parts.push(t(cmds === 1 ? "{n} command" : "{n} commands", { n: cmds }));
  if (others.length) parts.push(others.slice(0, 4).join(", ") + (others.length > 4 ? ", …" : ""));
  return parts.join(" · ");
}

function ThoughtRow({ text }: { text: string }) {
  return (
    <details className="tool-thought">
      <summary><Icon name="brain" size={12} />{t("Thinking · {n} chars", { n: fmtK(text.length) })}</summary>
      <pre>{text}</pre>
    </details>
  );
}

const ToolRow = memo(function ToolRow({ sid, t: tool, showSub }: { sid: string; t: ToolItem; showSub: boolean }) {
  useLang();  // memo 组件：切换语言时自己重新渲染
  const diff: string | undefined = tool.meta?.diff;
  const op: Json = tool.meta?.op;
  const event: Json = tool.meta?.event;
  const asking = !!tool.permission && !tool.permission.answered;
  const [open, setOpen] = useState<boolean | null>(null);
  const isOpen = open ?? (asking || tool.status === "failed" || !!event);
  const name = tool.title.split(" ")[0] || "tool";
  const summary = toolSummary(name, tool.input, tool.title);
  const dur = fmtDuration(tool.meta?.duration_ms);
  const hasBody = !!(tool.output || diff || op || event);

  return (
    <div className={`tool ${tool.status} ${asking ? "ask" : ""} ${isOpen ? "open" : ""}`}>
      <button className="tool-head" onClick={() => setOpen(!isOpen)}>
        <StatusIcon status={asking ? "ask" : tool.status} />
        <Icon name={TOOL_ICON[name] ?? KIND_ICON[tool.toolKind] ?? "dot"} size={14} className="kicon" />
        {showSub && <span className="sub-tag" title={t("Sub-agent: {name}", { name: tool.sub ?? "" })}>{tool.sub}</span>}
        <span className="name">{name}</span>
        <span className="args" title={summary}>{summary}</span>
        <span className="meta">
          {tool.permission?.answered && (
            <span className={tool.permission.answered === "reject" ? "err" : "approved"}
                  title={tool.permission.answered === "reject" ? t("You denied this call") : t("You approved: {option}", { option: tc(tool.permission.options.find((o) => o.optionId === tool.permission!.answered)?.name ?? tool.permission.answered) })}>
              <Icon name={tool.permission.answered === "reject" ? "x" : "shield"} size={11} />
            </span>
          )}
          {tool.meta?.added !== undefined && <><span style={{ color: "var(--add)" }}>+{tool.meta.added}</span><span style={{ color: "var(--del)" }}>−{tool.meta.removed}</span></>}
          {op && <span className={op.ok ? "" : "err"}>{op.ok ? "ok" : op.error_class ?? t("failed")}</span>}
          {dur}
        </span>
        {hasBody && <Icon name="chevron" size={13} className="chev" />}
      </button>
      {tool.status === "in_progress" && <ToolProgress sid={sid} name={name} text={tool.progress} startedAt={tool.startedAt} />}
      {tool.permission && !tool.permission.answered && <PermissionBar sid={sid} toolId={tool.id} p={tool.permission} input={tool.input} />}
      {isOpen && hasBody && (
        <div className="tool-body">
          {op && <OpSummary op={op} />}
          {event ? <div style={{ padding: "10px 14px" }}><CrashBlock ev={event} /></div> : null}
          {diff ? <DiffView diff={diff} /> : !event && tool.output && <pre className="out">{tool.output}</pre>}
        </div>
      )}
    </div>
  );
});

/** 正在执行的工具：进度文字（esptool 的阶段 / 百分比、编译的 [n/N]）、进度条、已用时间。
 *  真机实测：烧录时只看到 "flash 1/2"；长时间的 shell 命令看不出是不是卡住了。 */
function ToolProgress({ sid, name, text, startedAt }: { sid: string; name: string; text: string; startedAt?: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const secs = startedAt ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : null;
  const pct = /(\d{1,3})\s?%/.exec(text)?.[1];
  return (
    <div className="tool-progress">
      <span className="txt">{text ? tc(text) : t("Running…")}</span>
      {pct && <span className="pbar"><span style={{ width: `${Math.min(100, Number(pct))}%` }} /></span>}
      {secs !== null && <span className={`el ${secs >= 60 ? "long" : ""}`}>{secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`}</span>}
      {name !== "flash" && secs !== null && secs >= 5 && (
        <button className="btn ghost xs" onClick={() => void interruptStep(sid)} title={t("Stop this step; the agent continues and is told you stopped it")}>
          <Icon name="stop" size={10} />{t("Stop step")}
        </button>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: string }) {
  if (status === "completed") return <span className="st completed"><Icon name="check" size={14} /></span>;
  if (status === "failed") return <span className="st failed"><Icon name="x" size={14} /></span>;
  if (status === "ask") return <span className="st ask"><Icon name="shield" size={14} /></span>;
  if (status === "in_progress") return <span className="st in_progress"><Icon name="refresh" size={13} className="spin" /></span>;
  return <span className="st pending"><Icon name="dot" size={14} /></span>;
}

/** 编译 / 烧录的结构化结果：错误类别、next_actions、诊断条数 */
function OpSummary({ op }: { op: Json }) {
  const errs = (op.diagnostics ?? []).filter((d: Json) => d.severity === "error").length;
  const warns = (op.diagnostics ?? []).filter((d: Json) => d.severity === "warning").length;
  return (
    <div className="op-summary">
      <span className={`chip ${op.ok ? "ok" : "bad"}`}>{t(op.ok ? "{op} succeeded" : "{op} failed", { op: op.op })}</span>
      {/* "flash failed" 和 "flash_failed" 重复显示过（真机实测第 5 条）：笼统的类别不再单独显示 */}
      {op.error_class && op.error_class !== `${op.op}_failed` && <span className="chip bad mono">{op.error_class}</span>}
      {(errs > 0 || warns > 0) && <span className="chip">{t("{errors} errors · {warnings} warnings", { errors: errs, warnings: warns })}</span>}
      {op.size?.app_bin_size && op.size?.app_partition_size && (
        <span className="chip">app {Math.round(op.size.app_bin_size / 1024)} KB / {Math.round(op.size.app_partition_size / 1024)} KB</span>
      )}
      {(op.next_actions ?? []).map((a: Json) => (
        <span key={a.kind} className={`chip ${a.human ? "warn" : "info"}`} title={tc(a.description)}>→ {a.kind}{a.human ? ` ${t("(needs you)")}` : ""}</span>
      ))}
    </div>
  );
}

/** 工具卡片和审批预览里的 diff（2026-10-05 起用 Diff.tsx：行号、语法高亮、行内改动） */
export function DiffView({ diff }: { diff: string }) {
  return <DiffInline diff={diff} />;
}

/** 审批前先让人看到"要做什么"：编辑给出改前 / 改后，写文件给出内容开头，命令给出原文 */
function Preview({ input }: { input: Json }) {
  if (!input) return null;
  if (typeof input.old_string === "string" && typeof input.new_string === "string") {
    const diff = [
      `--- ${input.path}`,
      ...String(input.old_string).split("\n").map((l: string) => `-${l}`),
      ...String(input.new_string).split("\n").map((l: string) => `+${l}`),
    ].join("\n");
    return <div className="preview"><DiffView diff={diff} /></div>;
  }
  if (typeof input.content === "string") {
    const lines = String(input.content).split("\n");
    const head = lines.slice(0, 30).join("\n") + (lines.length > 30 ? `\n… ${t("({n} lines)", { n: lines.length })}` : "");
    return <div className="preview"><pre>{`${input.path}\n${head}`}</pre></div>;
  }
  if (typeof input.command === "string") return <div className="preview"><pre>{input.command}</pre></div>;
  return null;
}

function PermissionBar({ sid, toolId, p, input }: { sid: string; toolId: string; p: PermissionAsk; input?: Json }) {
  return (
    <div className={`perm ${p.risk}`}>
      <div className="why"><b>{p.risk === "dangerous" ? t("Dangerous") : t("Approval needed")}</b>{tc(p.reason.replace(/^Dangerous: /, ""))}</div>
      <Preview input={input} />
      {p.subjects.length > 0 && !input?.command && <div className="subj">{p.subjects.join("  ·  ")}</div>}
      <div className="actions">
        {p.options.map((o) => (
          <button key={o.optionId}
                  className={`btn sm ${o.kind === "allow_once" ? "primary" : o.kind.startsWith("allow") ? "" : "danger"}`}
                  onClick={() => answerPermission(sid, toolId, o.optionId)}>
            {tc(o.name)}
          </button>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ composer

function Composer({ sid, status, modelId, mode, models }:
  { sid: string; status: string; modelId: string; mode: string; models: string[] }) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<{ mimeType: string; data: string; url: string }[]>([]);
  const [goalMode, setGoalMode] = useState(false);
  const [rounds, setRounds] = useState(5);
  const ta = useRef<HTMLTextAreaElement>(null);
  const goalActive = useStore((s) => ["planning", "working", "verifying"].includes(s.views[sid]?.goal?.status ?? ""));
  const busy = status !== "idle" || goalActive;
  const vision = useStore((s) => {
    const meta = s.sessions.find((x) => x.id === sid);
    return s.models.find((m) => m.id === meta?.model_id)?.vision ?? false;
  });
  const effort = useStore((s) => s.sessions.find((x) => x.id === sid)?.effort ?? null);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  const submit = (now = false) => {
    const msg = text.trim();
    if (!msg) return;
    setText("");
    const imgs = images.map(({ mimeType, data }) => ({ mimeType, data }));
    setImages([]);
    if (goalMode && !busy) {
      // goal 模式：规划者写验收标准 → 执行者一轮轮做 → 独立验证者在设备上检查（W7）
      setGoalMode(false);
      startGoal(sid, msg, rounds).catch((e) => alert(tc(String(e instanceof Error ? e.message : e))));
      return;
    }
    void sendPrompt(sid, msg, imgs, now && busy);
  };

  // 拖文件进来（2026-10-05）：图片作为附件；其他文件把路径插进输入框，agent 用 read_file 读
  const [dragging, setDragging] = useState(false);
  const addImage = (f: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      setImages((xs) => [...xs, { mimeType: f.type, data: url.split(",")[1], url }]);
    };
    reader.readAsDataURL(f);
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const paths: string[] = [];
    for (const f of Array.from(e.dataTransfer.files)) {
      if (f.type.startsWith("image/")) addImage(f);
      else paths.push(native.pathForFile(f) || f.name);
    }
    if (paths.length) {
      const joined = paths.map((x) => (/\s/.test(x) ? `"${x}"` : x)).join(" ");
      setText((cur) => `${cur}${cur && !/\s$/.test(cur) ? " " : ""}${joined} `);
      ta.current?.focus();
    }
  };

  const onPaste = (e: React.ClipboardEvent) => {
    for (const item of e.clipboardData.items) {
      if (!item.type.startsWith("image/")) continue;
      const f = item.getAsFile();
      if (!f) continue;
      addImage(f);
      e.preventDefault();
    }
  };

  const hint = busy ? t(STATUS_HINT[status] ?? (goalActive ? "Goal in progress" : "Working…"))
    : images.length > 0 && !vision ? t("This model has no vision; images will be replaced by a text note") : "";

  return (
    <div className="composer">
      <div className={`composer-card ${goalMode && !busy ? "goal" : ""} ${dragging ? "drop" : ""}`}
           onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragging(true); } }}
           onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false); }}
           onDrop={onDrop}>
        {dragging && <div className="drop-hint"><Icon name="file" size={16} />{t("Drop files: images are attached, other files are referenced by path")}</div>}
        {images.length > 0 && (
          <div className="attach">
            {images.map((im, i) => (
              <img key={i} src={im.url} alt={t("pasted image")} title={t("Click to remove")} onClick={() => setImages((xs) => xs.filter((_, j) => j !== i))} />
            ))}
          </div>
        )}
        <textarea
          ref={ta}
          value={text}
          rows={1}
          placeholder={busy ? t("The agent is working. Enter queues a note for its next step; Ctrl+Enter stops the current step and delivers it now.")
            : goalMode ? t("Describe the goal. A planner writes acceptance criteria, and an independent verifier checks them on the device.")
              : t("Ask Firmwright to do something…  (Enter to send, Shift+Enter for a new line, paste screenshots)")}
          onChange={(e) => setText(e.target.value)}
          onPaste={onPaste}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit(e.ctrlKey || e.metaKey);
            }
          }}
        />
        <div className="composer-row">
          {!busy && (
            <label className="goal-toggle" title={t("Goal mode: a planner writes acceptance criteria; after the agent finishes, an independent verifier checks them on the device and sends it back until they pass or the rounds run out")}>
              <button type="button" className={`switch ${goalMode ? "on" : ""}`} onClick={() => setGoalMode(!goalMode)} aria-pressed={goalMode} aria-label={t("Goal mode")} />
              <span onClick={() => setGoalMode(!goalMode)}>{t("Goal")}</span>
              {goalMode && (
                <Select value={String(rounds)} onChange={(v) => setRounds(Number(v))} title={t("Maximum rounds")}
                        options={[2, 3, 5, 8, 10].map((n) => ({ value: String(n), label: t("up to {n} rounds", { n }) }))} />
              )}
            </label>
          )}
          <span className={`hint ${busy ? "busy" : ""}`}>
            {busy && <span className="dot running" style={{ width: 6, height: 6 }} />}{hint}
          </span>
          <Select value={mode} onChange={(v) => void setMode(sid, v)} width={300} icon={<Icon name="shield" size={13} />}
                  options={MODES.map(([k, v, h]) => ({ value: k, label: t(v), hint: t(h) }))} />
          <Select value={modelId} onChange={(v) => void setModel(sid, v)} width={220} icon={<Icon name="sparkles" size={13} />}
                  options={models.map((m) => ({ value: m, label: m }))} />
          <EffortSelect modelId={modelId} value={effort} onChange={(v) => void setEffort(sid, v)} />
          <ContextMeter sid={sid} />
          {status !== "idle" && <button className="btn sm danger" onClick={() => cancelTurn(sid)}><Icon name="stop" size={12} />{t("Stop")}</button>}
          {busy && text.trim() && (
            <button className="btn sm" onClick={() => submit(true)}
                    title={t("Stop the current step and deliver this note now (Ctrl+Enter). Flashing is never interrupted.")}>
              {t("Send now")}
            </button>
          )}
          <button className="btn primary send-btn" onClick={() => submit()} disabled={!text.trim()}
                  title={busy ? t("Queue as a note for the agent's next step (Enter)") : goalMode ? t("Start goal") : t("Send")}>
            <Icon name={goalMode && !busy ? "target" : "send"} size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

/** 思考程度（2026-10-05）。只有声明了 reasoning 的模型才显示；Default = 不发参数，用服务商默认。 */
export const EFFORT_TEXT: Record<string, [string, string]> = {
  default: [tk("Thinking: default"), tk("Use the provider's default")],
  off: [tk("Thinking: off"), tk("Answer without a reasoning phase (fastest)")],
  on: [tk("Thinking: on"), tk("Reason before answering")],
  low: [tk("Thinking: low"), tk("Short reasoning; faster and cheaper")],
  medium: [tk("Thinking: medium"), tk("Balanced")],
  high: [tk("Thinking: high"), tk("Longest reasoning; slower, for hard bugs")],
};

export function EffortSelect({ modelId, value, onChange }: { modelId: string; value: string | null | undefined; onChange: (v: string) => void }) {
  const efforts = useStore((s) => s.models.find((m) => m.id === modelId)?.efforts);
  if (!efforts || efforts.length === 0) return null;
  return (
    <Select value={value ?? "default"} onChange={onChange} width={240} icon={<Icon name="brain" size={13} />} className="no-shrink"
            title={t("Thinking effort: how much the model reasons before answering")}
            renderValue={(o) => (o?.value === "default" || !o ? t("Auto") : t(o.value[0].toUpperCase() + o.value.slice(1)))}
            options={["default", ...efforts].map((k) => ({ value: k, label: EFFORT_TEXT[k] ? t(EFFORT_TEXT[k][0]) : k, hint: EFFORT_TEXT[k] ? t(EFFORT_TEXT[k][1]) : undefined }))} />
  );
}

const STATUS_HINT: Record<string, string> = {
  running: tk("Working…"),
  awaiting_approval: tk("Needs your approval"),
  awaiting_human: tk("Needs your hands"),
};
