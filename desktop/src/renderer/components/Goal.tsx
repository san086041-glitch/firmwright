/** W7：goal 模式的状态条和子 agent 卡片。 */
import { useRef, useState } from "react";
import { sendPrompt, stopGoal, stopSubagent } from "../actions";
import type { Json } from "../rpc";
import { useStore, type GoalState, type Item, type RoundNote } from "../store";
import { useOutside } from "./Context";
import { Icon } from "./Icon";
import { fmtK } from "./util";

const GOAL_TEXT: Record<GoalState["status"], string> = {
  planning: "Planning", working: "Working", verifying: "Verifying", done: "Done", paused: "Paused · needs you",
  failed: "Not achieved", stopped: "Stopped",
};
const GOAL_CHIP: Record<GoalState["status"], string> = {
  planning: "info", working: "info", verifying: "violet", done: "ok", paused: "warn", failed: "bad", stopped: "bad",
};
const ACTIVE = new Set(["planning", "working", "verifying"]);

/** 2026-10-06（界面改进第 4 项）：对话上方只留一行摘要；详情（验证结论、逐条标准、逐轮记录、完整计划）
 *  放进右侧抽屉，不再展开后把对话区挤掉一半。暂停 / 未达成时这一行变色并给出"Review"。 */
export function GoalBar({ sid }: { sid: string }) {
  const goal = useStore((s) => s.views[sid]?.goal);
  const [open, setOpen] = useState(false);
  if (!goal) return null;
  const active = ACTIVE.has(goal.status);
  const needsYou = goal.status === "paused" || goal.status === "failed";
  const last: Json | undefined = goal.verdicts[goal.verdicts.length - 1];
  const crit = (i: number): Json | undefined => (last?.criteria ?? []).find((c: Json) => Number(c.id) === i + 1);
  const passed = goal.criteria.filter((_, i) => crit(i)?.pass).length;
  const cls = active ? "active" : goal.status;
  return (
    <>
      <div className={`goal-bar ${cls}`}>
        <button className="hd" onClick={() => setOpen(true)} title={goal.message || goal.objective}>
          <span className="kind">
            {active ? <Icon name="refresh" size={14} className="spin" /> : <Icon name="target" size={14} />}Goal
          </span>
          <span className="obj">{goal.objective}</span>
          {goal.criteria.length > 0 && (
            <span className="progress" title={last ? `${passed} of ${goal.criteria.length} acceptance criteria passed in the latest verification` : "Not verified yet"}>
              {goal.criteria.map((_, i) => {
                const v = crit(i);
                return <i key={i} className={v ? (v.pass ? "pass" : "fail") : ""} />;
              })}
            </span>
          )}
          {goal.round > 0 && <span className="chip hide-narrow">round {goal.round}/{goal.max_rounds}</span>}
          <span className={`chip ${GOAL_CHIP[goal.status]}`}>{GOAL_TEXT[goal.status]}</span>
          <span className={`btn xs ${needsYou ? "warn-btn" : "ghost"}`}>{needsYou ? "Review" : "Details"}</span>
        </button>
        {active && (
          <button className="btn xs danger stop" onClick={() => void stopGoal(sid)}>Stop</button>
        )}
      </div>
      {open && <GoalDrawer sid={sid} goal={goal} onClose={() => setOpen(false)} />}
    </>
  );
}

function GoalDrawer({ sid, goal, onClose }: { sid: string; goal: GoalState; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useOutside(ref, true, onClose);
  const active = ACTIVE.has(goal.status);
  const last: Json | undefined = goal.verdicts[goal.verdicts.length - 1];
  const crit = (i: number): Json | undefined => (last?.criteria ?? []).find((c: Json) => Number(c.id) === i + 1);
  return (
    <div className={`goal-drawer ${active ? "active" : goal.status}`} ref={ref} role="dialog" aria-label="Goal details">
      <div className="dr-hd">
        <Icon name="target" size={15} />
        <b>Goal</b>
        <span className={`chip ${GOAL_CHIP[goal.status]}`}>{GOAL_TEXT[goal.status]}</span>
        {goal.round > 0 && <span className="chip">round {goal.round}/{goal.max_rounds}</span>}
        <span className="spacer" />
        {active && <button className="btn xs danger" onClick={() => void stopGoal(sid)}>Stop</button>}
        <button className="btn ghost sm icon-only" onClick={onClose} aria-label="Close"><Icon name="x" size={14} /></button>
      </div>
      <div className="dr-bd">
        <div className="objective">{goal.objective}</div>
        {goal.message && <div className={`callout ${goal.status === "paused" || goal.status === "failed" ? "warn" : "info"}`}><Icon name={goal.status === "done" ? "check" : "info"} /><div>{goal.message}</div></div>}
        <div className="sec">Acceptance criteria</div>
        {goal.criteria.length > 0 ? (
          <ol className="crit">
            {goal.criteria.map((c, i) => {
              const v = crit(i);
              return (
                <li key={i} className={v ? (v.pass ? "pass" : "fail") : ""}>
                  <span className="mark"><Icon name={v ? (v.pass ? "check" : "x") : "dot"} size={14} /></span>
                  <span>{c}</span>
                  {v?.evidence && <div className="ev">{v.evidence}</div>}
                </li>
              );
            })}
          </ol>
        ) : goal.status === "planning" ? <div className="hint">The planner is investigating the project and writing acceptance criteria…</div> : <div className="hint">None yet.</div>}
        {goal.device_steps && (
          <div className="hint">
            Device check required: the verifier must flash the board and see the expected output with await_marker itself, otherwise a "pass" is not accepted.
          </div>
        )}
        {last?.oracle_override && (
          <div className="msg warn">Round {last.round}: the verifier said pass without observing the device, so it was overruled.</div>
        )}
        {(goal.notes?.length ?? 0) > 0 && (
          <div className="rounds">
            <div className="sec">
              Rounds <span className="faint">measured from tool results, not the agent's own account</span>
              {goal.usage?.input_tokens ? <span className="faint"> · {fmtK(goal.usage.input_tokens)} tokens in so far</span> : null}
            </div>
            {goal.notes!.map((n) => <RoundRow key={n.round} n={n} total={goal.criteria.length} />)}
          </div>
        )}
        {goal.plan && (
          <details>
            <summary><Icon name="chevron" size={12} className="chev" />Full plan</summary>
            <pre className="plan">{goal.plan}</pre>
          </details>
        )}
      </div>
    </div>
  );
}

const DEVICE_TEXT: Record<string, string> = { pass: "expected line seen", fail: "failure line", crash: "crashed",
                                              timeout: "expected line did not appear", interrupted: "wait interrupted" };
const VERDICT_TEXT: Record<string, string> = { pass: "verifier: pass", fail: "verifier: not passed", unverifiable: "verifier: could not verify" };

/** goal 面板里的一轮：改了什么、设备上怎样、验证结果、有没有进展（系统测出来的，不是 agent 自己说的） */
function RoundRow({ n, total }: { n: RoundNote; total: number }) {
  const bits: { text: string; tone?: string; title?: string }[] = [];
  bits.push(n.files.length ? { text: `${n.files.length} file${n.files.length > 1 ? "s" : ""} changed`, title: n.files.join("\n") }
                           : { text: "no code changes", tone: "faint" });
  if (n.builds) bits.push({ text: n.build_ok ? "build ok" : "build failed", tone: n.build_ok ? "" : "bad" });
  if (n.flashes) bits.push({ text: n.flash_ok ? "flashed" : "flash failed", tone: n.flash_ok ? "" : "bad" });
  if (n.device !== "none") bits.push({ text: DEVICE_TEXT[n.device] ?? n.device, tone: n.device === "pass" ? "ok" : "bad", title: n.device_line });
  if (n.crashes) bits.push({ text: `${n.crashes} crash${n.crashes > 1 ? "es" : ""}`, tone: "bad" });
  if (n.verdict) bits.push({ text: `${VERDICT_TEXT[n.verdict] ?? n.verdict}${n.passed != null && total ? ` (${n.passed}/${total})` : ""}`,
                             tone: n.verdict === "pass" ? "ok" : "warn" });
  else if (!n.report) bits.push({ text: "no goal_report", tone: "faint" });
  return (
    <div className={`round-row ${n.progress ? "" : "stalled"}`} title={n.note || undefined}>
      <span className="rn">{n.round}</span>
      <span className="bits">
        {bits.map((b, i) => <span key={i} className={b.tone ?? ""} title={b.title}>{b.text}</span>)}
      </span>
      {!n.progress && <span className="chip warn" title={n.why}>no progress</span>}
    </div>
  );
}

const KIND_TEXT: Record<string, string> = {
  general: "Sub-agent", explore: "Sub-agent · explore", plan: "Sub-agent · plan", planner: "Goal planner", verifier: "Independent verifier",
};

const STOP_TEXT: Record<string, string> = { cancelled: "stopped", interrupted: "interrupted", max_steps: "step limit",
                                            loop_guard: "looping", model_error: "model error", error: "error" };

export function SubagentCard({ sid, it }: { sid: string; it: Extract<Item, { kind: "subagent" }> }) {
  const [open, setOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  const ok = it.stop === "end_turn";
  const running = it.status === "running";
  return (
    // 类型加前缀：原来直接用 "plan" 做类名，撞上了 goal 计划正文的 .plan 样式（等宽字体、边框）
    <div className={`subagent ${it.status} kind-${it.agentKind} ${it.awaitingParent ? "awaiting" : ""}`}>
      <div className="hd-row">
        <button className="hd" onClick={() => setOpen(!open)}>
          {running ? <Icon name="refresh" size={14} className="spin" style={{ color: "var(--violet)" }} />
            : <Icon name={ok ? "check" : "alert"} size={14} style={{ color: ok ? "var(--accent)" : "var(--red)" }} />}
          <Icon name={it.agentKind === "verifier" ? "shield" : it.agentKind === "planner" ? "target" : "bot"} size={14} style={{ color: "var(--violet)" }} />
          <span className="who">{KIND_TEXT[it.agentKind] ?? it.agentKind}</span>
          {it.background && <span className="bg-tag" title="Runs in the background while the agent keeps working">background</span>}
          <span className="d">{it.description}</span>
          {it.steps != null && (
            <span className="meta">
              {!ok && it.stop ? `${STOP_TEXT[it.stop] ?? it.stop} · ` : ""}
              {it.steps} steps{it.files?.length ? ` · ${it.files.length} files changed` : ""}{it.usage?.input_tokens ? ` · ${fmtK(it.usage.input_tokens)} in` : ""}
            </span>
          )}
          {it.text && <Icon name="chevron" size={13} className="faint" style={{ transform: open ? "rotate(90deg)" : "none" }} />}
        </button>
        {running && it.background && (
          <button className="btn xs" disabled={stopping} title="Stop this background sub-agent; its partial report still reaches the agent"
                  onClick={() => { setStopping(true); void stopSubagent(sid, it.id).catch(() => setStopping(false)); }}>
            <Icon name="stop" size={12} />{stopping ? "Stopping…" : "Stop"}
          </button>
        )}
      </div>
      {/* 后台子 agent 做完时 agent 已经空闲：报告在排队，不会自动开新一轮（和空闲时的崩溃一样由用户决定，I04） */}
      {it.awaitingParent && (
        <div className="awaiting-row">
          <span>The agent is idle. The report is queued and reaches it on your next message.</span>
          <button className="btn xs primary" onClick={() => void sendPrompt(sid, `The background sub-agent "${it.description}" finished. Continue with its report.`)}>
            Let the agent continue
          </button>
        </div>
      )}
      {open && it.text && <pre className="out">{it.text}</pre>}
    </div>
  );
}
