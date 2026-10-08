import React, { useState } from "react";
import { answerHuman, dismissNotice, handOffCrash } from "../actions";
import { useStore, type DeviceEvent, type HumanAsk, type IdleNotice } from "../store";
import { Icon } from "./Icon";
import { native } from "../rpc";
import { shortPath } from "./util";

const KIND_TEXT: Record<string, string> = {
  panic: "CPU exception",
  abort: "abort()",
  assert: "Assertion failed",
  stack_overflow: "Stack overflow",
  stack_smash: "Stack smashing",
  wdt_reset: "Watchdog reset",
  brownout: "Brownout",
  reboot_loop: "Reboot loop",
};

/** 崩溃内容（D21）：解码后的调用栈默认折叠 ESP-IDF / FreeRTOS 内部帧，突出用户代码 */
export function CrashBlock({ ev }: { ev: DeviceEvent }) {
  const [showAll, setShowAll] = useState(false);
  const frames = ev.backtrace?.frames ?? [];
  const rows: React.ReactElement[] = [];
  let folded = 0;
  const flush = (key: string) => {
    if (folded > 0) {
      const n = folded;
      rows.push(
        <button key={key} className="fold" onClick={() => setShowAll(true)} title="Show all frames">
          … {n} ESP-IDF / FreeRTOS / ROM frame{n > 1 ? "s" : ""}
        </button>,
      );
      folded = 0;
    }
  };
  frames.forEach((f, i) => {
    const text = `${f.function ?? "??"}  ${f.file ? `${shortPath(f.file)}:${f.line ?? "?"}` : f.pc}`;
    if (f.internal && !showAll) {
      folded += 1;
      return;
    }
    flush(`fold${i}`);
    rows.push(<FrameRow key={i} f={f} text={text} />);
  });
  flush("foldEnd");

  return (
    <div className="crash">
      <div className="hd">
        <Icon name="flame" size={15} />
        {KIND_TEXT[ev.kind] ?? ev.kind}
        {ev.detail?.exception ? ` · ${ev.detail.exception}` : ""}
        <span className="time">{new Date(ev.at).toLocaleTimeString("en-US", { hour12: false })}</span>
      </div>
      <div className="bd">
        <div>{ev.summary}</div>
        {frames.length > 0 && (
          <>
            <div className="section-title">
              Backtrace{ev.backtrace?.decoded ? " · decoded" : " · not decoded"}
              <span className="spacer" />
              {showAll && <button className="btn ghost xs" onClick={() => setShowAll(false)}>Fold internal frames</button>}
            </div>
            <div className="frames">{rows}</div>
          </>
        )}
        {ev.backtrace?.corrupted && <div className="chip warn">The backtrace ends with CORRUPTED</div>}
        {ev.log_ref && <div className="mono faint" style={{ fontSize: 11.5 }}>log_ref {ev.log_ref.board_id}@{ev.log_ref.start}-{ev.log_ref.end}</div>}
      </div>
    </div>
  );
}

type Frame = NonNullable<DeviceEvent["backtrace"]>["frames"][number];

/** 有源码位置的帧可以点：在编辑器里打开那一行（2026-10-06，界面改进第 12 项）。ROM / 没解码的帧照旧只显示 */
export function FrameRow({ f, text }: { f: Frame; text: string }) {
  const cls = `fr ${f.internal ? "internal" : "user"}`;
  const [miss, setMiss] = useState(false);
  if (!f.file || !f.line || f.file.startsWith("??")) return <div className={cls} title={f.pc}>{text}</div>;
  const open = async () => {
    const res = await native.openInEditor(f.file!, f.line ?? undefined);
    setMiss(res === "missing");
  };
  return (
    <button className={`${cls} link`} onClick={() => void open()}
            title={miss ? `File not found: ${f.file}` : `Open ${f.file}:${f.line} (${f.pc})`}>
      {text}{miss && <span className="miss"> · not found</span>}
    </button>
  );
}

/** 空闲时的崩溃通知（I04 选项 b）：只通知，"让 agent 处理"由用户决定 */
export function IdleCrashCard({ n }: { n: IdleNotice }) {
  const board = useStore((s) => s.boards[n.event.board_id]);
  const session = useStore((s) => s.sessions.find((x) => x.id === n.sessionId));
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <CrashBlock ev={n.event} />
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <span className="muted" style={{ fontSize: 12, flex: 1 }}>
          {board?.alias ?? n.event.board_id} crashed while idle{session ? ` (session “${session.title}”)` : " (no session bound)"}
          {n.event.detail?.crashes_since_last_notice ? ` · ${n.event.detail.crashes_since_last_notice} more crash(es) since the last notice; it may be crash-looping` : ""}
        </span>
        <button className="btn sm" onClick={() => dismissNotice(n.id)}>Dismiss</button>
        {n.sessionId && <button className="btn sm primary" onClick={() => void handOffCrash(n)}>Ask the agent</button>}
      </div>
    </div>
  );
}

/** 人工操作卡片（D13）：请用户做物理操作；会话状态显示"等你操作" */
export function HumanCard({ sid, itemId, ask }: { sid: string; itemId: string; ask: HumanAsk }) {
  const [note, setNote] = useState("");
  if (ask.answered) {
    return (
      <div className="human done">
        <div className="hd"><Icon name="hand" size={15} />{ask.title}</div>
        <div className="muted" style={{ fontSize: 13 }}>
          {ask.answered.done ? "Done" : "Could not do it"}{ask.answered.note ? `: ${ask.answered.note}` : ""}
        </div>
      </div>
    );
  }
  return (
    <div className="human">
      <div className="hd"><Icon name="hand" size={15} />Your hands needed · {ask.title}</div>
      <div className="ins">{ask.instructions}</div>
      <div className="row">
        <input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note, e.g. the LED is on but blinking fast" />
        <button className="btn" onClick={() => answerHuman(sid, itemId, false, note)}>Can't do it</button>
        <button className="btn primary" onClick={() => answerHuman(sid, itemId, true, note)}><Icon name="check" size={14} />Done</button>
      </div>
    </div>
  );
}
