/** W6 上下文：标题栏里的用量环和"上下文"面板（压缩、skill、记忆、MCP）。 */
import { useEffect, useRef, useState } from "react";
import { compactSession, contextInfo } from "../actions";
import type { Json } from "../rpc";
import { t, tc, tk } from "../i18n";
import { useStore } from "../store";
import { Icon } from "./Icon";
import { fmtK } from "./util";

export function ContextMeter({ sid }: { sid: string }) {
  const ctx = useStore((s) => s.views[sid]?.context);
  const status = useStore((s) => s.views[sid]?.status ?? "idle");
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const pct = ctx && ctx.window ? Math.min(100, Math.round((ctx.used / ctx.window) * 100)) : null;
  const cls = pct == null ? "" : pct >= 80 ? "bad" : pct >= 60 ? "warn" : "";
  useOutside(wrap, open, () => setOpen(false));
  const c = 2 * Math.PI * 6.5;
  return (
    <span className="ctx-wrap" ref={wrap}>
      <button className={`ctx-meter ${cls}`} onClick={() => setOpen(!open)}
              title={ctx ? t("Context: {used} / {window} tokens (compacts automatically above 80%)", { used: fmtK(ctx.used), window: fmtK(ctx.window) }) : t("Context, skills, memory, MCP")}>
        <svg className="ring" viewBox="0 0 16 16">
          <circle className="bg" cx="8" cy="8" r="6.5" />
          <circle className="fg" cx="8" cy="8" r="6.5" strokeDasharray={`${((pct ?? 0) / 100) * c} ${c}`} />
        </svg>
        {pct == null ? t("Context") : pct === 0 && ctx && ctx.used > 0 ? "<1%" : `${pct}%`}
      </button>
      {open && <ContextPanel sid={sid} busy={status !== "idle"} onClose={() => setOpen(false)} />}
    </span>
  );
}

/** 点击弹层外面时关闭 */
export function useOutside(ref: React.RefObject<HTMLElement | null>, active: boolean, close: () => void): void {
  useEffect(() => {
    if (!active) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [ref, active, close]);
}

const SCOPE: Record<string, string> = { builtin: tk("built-in"), user: tk("user"), project: tk("project") };

function ContextPanel({ sid, busy, onClose }: { sid: string; busy: boolean; onClose: () => void }) {
  const [info, setInfo] = useState<Json | null>(null);
  const [err, setErr] = useState("");
  const [working, setWorking] = useState(false);
  const [note, setNote] = useState("");
  const load = () => contextInfo(sid).then(setInfo).catch((e) => setErr(String(e.message ?? e)));
  useEffect(() => {
    void load();
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps
  const compact = async () => {
    setWorking(true);
    setErr("");
    try {
      const res = await compactSession(sid, note.trim());
      if (res.skipped) setErr(res.skipped);
      await load();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setWorking(false);
    }
  };
  const pct = info?.window ? Math.min(100, Math.round((info.used / info.window) * 100)) : 0;
  return (
    <div className="popover ctx-pop" role="dialog" aria-label={t("Context")}>
      <div className="hd"><b>{t("Context")}</b><span className="spacer" /><button className="btn ghost sm icon-only" onClick={onClose} aria-label={t("Close")}><Icon name="x" size={14} /></button></div>
      {!info ? <div className="hint">{err ? tc(err) : t("Loading…")}</div> : (
        <>
          <div className="usage-bar"><span style={{ width: `${pct}%` }} /></div>
          <div className="muted">{t("~{used} of {window} tokens · {messages} messages · {tools} tools", { used: fmtK(info.used), window: fmtK(info.window), messages: info.messages, tools: info.tools })}</div>
          <div className="compact-row">
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("What to keep when compacting (optional)")} />
            <button className="btn sm" disabled={busy || working} onClick={() => void compact()}
                    title={busy ? t("The session is running; wait for it to finish") : t("Summarize the earlier conversation and archive the original")}>
              <Icon name="layers" size={13} />{working ? t("Compacting…") : t("Compact now")}
            </button>
          </div>
          {err && <div className="err">{tc(err)}</div>}
          {info.compactionDir && <div className="hint">{t("Archive:")} <code>{info.compactionDir}</code></div>}

          <div className="sec">{t("Skills")} · {info.skills.length}</div>
          {info.skills.length === 0 ? <div className="hint">{info.features?.context_skills === false ? t("No skills available (context.skills is off)") : t("No skills available")}</div> : (
            <ul className="list">
              {info.skills.map((s: Json) => (
                <li key={s.name} title={s.path}>
                  <b>{s.name}</b> <span className="chip" style={{ height: 18, fontSize: 11 }}>{SCOPE[s.scope] ? t(SCOPE[s.scope]) : s.scope}</span>
                  {s.loaded && <> <span className="chip ok" style={{ height: 18, fontSize: 11 }}>{t("loaded")}</span></>}
                  <div className="d">{s.description}</div>
                </li>
              ))}
            </ul>
          )}

          <div className="sec">{t("Memory")}</div>
          {!info.memory ? <div className="hint">{t("Memory is off (context.memory)")}</div> : (
            <>
              <div className="hint"><code>{info.memory.root}</code></div>
              <pre className="mem">{info.memory.index || t("Nothing remembered yet. Tell the agent \"remember …\", or it will record stable facts with remember.")}</pre>
            </>
          )}

          <div className="sec">{t("MCP servers")}</div>
          {info.mcp.length === 0 ? <div className="hint">{t("None configured ([mcp.servers.<name>] in config.toml)")}</div> : (
            <ul className="list">
              {info.mcp.map((m: Json) => (
                <li key={m.name} className="row">
                  <span className={`dot ${m.alive ? "ok" : "crashed"}`} /> <b>{m.name}</b>
                  <span className="muted">{m.alive ? `${t("{n} tools", { n: m.tools })}${m.server ? ` · ${m.server}` : ""}` : `${t("offline:")} ${tc(m.error ?? "")}`}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
