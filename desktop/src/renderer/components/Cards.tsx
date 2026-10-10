import React, { useState } from "react";
import { answerHuman, dismissNotice, handOffCrash } from "../actions";
import { locale, t, tc, tk } from "../i18n";
import { useStore, type DeviceEvent, type HumanAsk, type IdleNotice } from "../store";
import { Icon } from "./Icon";
import { native } from "../rpc";
import { shortPath } from "./util";

const KIND_TEXT: Record<string, string> = {
  panic: tk("CPU exception"),
  abort: "abort()",
  assert: tk("Assertion failed"),
  stack_overflow: tk("Stack overflow"),
  stack_smash: tk("Stack smashing"),
  wdt_reset: tk("Watchdog reset"),
  brownout: tk("Brownout"),
  reboot_loop: tk("Reboot loop"),
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
        <button key={key} className="fold" onClick={() => setShowAll(true)} title={t("Show all frames")}>
          … {t(n > 1 ? "{n} ESP-IDF / FreeRTOS / ROM frames" : "{n} ESP-IDF / FreeRTOS / ROM frame", { n })}
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
        {KIND_TEXT[ev.kind] ? t(KIND_TEXT[ev.kind]) : ev.kind}
        {ev.detail?.exception ? ` · ${ev.detail.exception}` : ""}
        <span className="time">{new Date(ev.at).toLocaleTimeString(locale(), { hour12: false })}</span>
      </div>
      <div className="bd">
        <div>{ev.summary}</div>
        {frames.length > 0 && (
          <>
            <div className="section-title">
              {t("Backtrace")}{ev.backtrace?.decoded ? ` · ${t("decoded")}` : ` · ${t("not decoded")}`}
              <span className="spacer" />
              {showAll && <button className="btn ghost xs" onClick={() => setShowAll(false)}>{t("Fold internal frames")}</button>}
            </div>
            <div className="frames">{rows}</div>
          </>
        )}
        {ev.backtrace?.corrupted && <div className="chip warn">{t("The backtrace ends with CORRUPTED")}</div>}
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
            title={miss ? t("File not found: {file}", { file: f.file }) : t("Open {file}:{line} ({pc})", { file: f.file, line: f.line, pc: f.pc })}>
      {text}{miss && <span className="miss"> · {t("not found")}</span>}
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
          {session ? t("{board} crashed while idle (session “{title}”)", { board: board?.alias ?? n.event.board_id, title: session.title })
            : t("{board} crashed while idle (no session bound)", { board: board?.alias ?? n.event.board_id })}
          {n.event.detail?.crashes_since_last_notice ? ` · ${t("{n} more crash(es) since the last notice; it may be crash-looping", { n: n.event.detail.crashes_since_last_notice })}` : ""}
        </span>
        <button className="btn sm" onClick={() => dismissNotice(n.id)}>{t("Dismiss")}</button>
        {n.sessionId && <button className="btn sm primary" onClick={() => void handOffCrash(n)}>{t("Ask the agent")}</button>}
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
        <div className="hd"><Icon name="hand" size={15} />{tc(ask.title)}</div>
        <div className="muted" style={{ fontSize: 13 }}>
          {ask.answered.done ? t("Done") : t("Could not do it")}{ask.answered.note ? `: ${ask.answered.note}` : ""}
        </div>
      </div>
    );
  }
  return (
    <div className="human">
      <div className="hd"><Icon name="hand" size={15} />{t("Your hands needed")} · {tc(ask.title)}</div>
      <div className="ins">{tc(ask.instructions)}</div>
      <div className="row">
        <input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("Optional note, e.g. the LED is on but blinking fast")} />
        <button className="btn" onClick={() => answerHuman(sid, itemId, false, note)}>{t("Can't do it")}</button>
        <button className="btn primary" onClick={() => answerHuman(sid, itemId, true, note)}><Icon name="check" size={14} />{t("Done")}</button>
      </div>
    </div>
  );
}
