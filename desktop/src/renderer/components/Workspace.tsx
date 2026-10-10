/** W5 工作区：checkpoint 时间线（D05）、会话收尾（D06：合并 / 导出补丁 / 丢弃）。 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  applySession,
  discardSession,
  exportPatch,
  listBranches,
  mergeSession,
  previewRestore,
  restoreCheckpoint,
  sessionDiff,
} from "../actions";
import { t, tc, tx } from "../i18n";
import { native, RpcError, type Json } from "../rpc";
import { useStore, type Checkpoint } from "../store";
import { DiffViewer } from "./Diff";
import { useOutside } from "./Context";
import { Icon } from "./Icon";
import { Select } from "./Select";

function label(e: Checkpoint): string {
  if (e.kind === "restore") return t("Restored to #{seq}", { seq: e.restored_to });
  if (e.kind === "base") return t("Session start");
  if (e.kind === "manual") return t("Unrecorded changes");
  return t("End of turn {n}", { n: e.turn });
}

/** checkpoint 时间线：每轮一个点，烧过固件的点是菱形。点一下可以回退代码，并选择要不要把当时的固件烧回去。 */
export function CheckpointStrip({ sid }: { sid: string }) {
  const state = useStore((s) => s.checkpoints[sid]);
  const status = useStore((s) => s.views[sid]?.status ?? "idle");
  const [pick, setPick] = useState<number | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  useOutside(ref, pick !== null, () => setPick(null));
  const entries = state?.entries ?? [];
  const last = entries[entries.length - 1];
  const at = last ? (last.kind === "restore" ? last.restored_to : last.seq) : null;
  if (!state || !state.enabled) return null;
  const shown = entries.filter((e) => e.kind !== "restore" && (e.kind !== "turn" || e.changed || e.firmware));
  return (
    <div className="subbar" ref={ref}>
      <span className="lbl" title={t("A checkpoint is recorded at the end of every turn. Only points with changes or a flash are shown. Diamonds mark flashed firmware.")}>
        <Icon name="history" size={13} />{t("Checkpoints")}
      </span>
      <div className="ckpt-dots">
        {shown.map((e, i) => (
          <span key={e.seq} className="seg">
            {i > 0 && <span className="line" />}
            <button
              className={`pt ${e.firmware ? "fw" : ""} ${e.seq === at ? "at" : ""} ${pick === e.seq ? "picked" : ""}`}
              title={`#${e.seq} · ${label(e)}${e.prompt ? ` · ${e.prompt}` : ""}${e.changed ? ` · ${t("{n} files", { n: e.files.length })} +${e.added} −${e.deleted}` : ""}${e.firmware ? ` · ${t("flashed {sha}", { sha: e.firmware.sha256?.slice(0, 8) ?? "" })}` : ""}`}
              onClick={() => setPick(pick === e.seq ? null : e.seq)}
            />
          </span>
        ))}
      </div>
      <span className="spacer" />
      <span className="faint" style={{ fontSize: 11.5 }}>{t(shown.length === 1 ? "{n} point" : "{n} points", { n: shown.length })}</span>
      {pick !== null && entries[pick] && (
        <RestorePopover sid={sid} e={entries[pick]} canReflash={!!state.canReflash} busy={status !== "idle"}
                        isCurrent={pick === at} onClose={() => setPick(null)} />
      )}
    </div>
  );
}

function RestorePopover({ sid, e, canReflash, busy, isCurrent, onClose }:
  { sid: string; e: Checkpoint; canReflash: boolean; busy: boolean; isCurrent: boolean; onClose: () => void }) {
  const fw = e.boardFirmware;
  const fwOk = !!(fw && fw.archive);
  const [reflash, setReflash] = useState(false);
  const [working, setWorking] = useState(false);
  const [msg, setMsg] = useState("");
  // 回退前先看会删掉 / 改动哪些文件（真机实测：回退删了 267 个文件，弹层上只写了"回退只改文件"）
  const [preview, setPreview] = useState<Json | null>(null);
  useEffect(() => {
    setPreview(null);
    if (!isCurrent) previewRestore(sid, e.seq).then(setPreview).catch(() => setPreview(null));
  }, [sid, e.seq, isCurrent]);
  const go = async () => {
    setWorking(true);
    setMsg("");
    try {
      const res = await restoreCheckpoint(sid, e.seq, reflash && fwOk && canReflash);
      if (res.flash && !res.flash.ok) setMsg(t("Code restored; reflashing failed: {why}", { why: tc(res.flash.summary) }));
      else onClose();
    } catch (x) {
      setMsg(String(x instanceof Error ? x.message : x));
    } finally {
      setWorking(false);
    }
  };
  return (
    <div className="popover ckpt-pop">
      <div className="hd">
        <b>#{e.seq} · {label(e)}</b>
        <span className="spacer" />
        <span className="chip mono" title={e.commit}>{e.commit.slice(0, 8)}</span>
        <button className="btn ghost sm icon-only" onClick={onClose} aria-label={t("Close")}><Icon name="x" size={14} /></button>
      </div>
      {e.prompt && <div className="q">“{e.prompt}”</div>}
      {e.changed && (
        <div>
          {tx(e.files.length === 1 ? "{n} file changed in this turn ({stats}): {files}" : "{n} files changed in this turn ({stats}): {files}", {
            n: e.files.length,
            stats: <><span style={{ color: "var(--add)" }}>+{e.added}</span>{" "}<span style={{ color: "var(--del)" }}>−{e.deleted}</span></>,
            files: <span className="mono">{e.files.slice(0, 6).join(", ")}{e.files.length > 6 ? "…" : ""}</span>,
          })}
        </div>
      )}
      <div className="fw">
        <Icon name="bolt" size={12} />{" "}
        {fw ? (
          <>{tx("Firmware on the board at this point: flashed in turn {turn}, sha256 {sha}", { turn: fw.turn, sha: <span className="mono">{fw.sha256?.slice(0, 12) ?? t("unknown")}</span> })}
            {!fw.archive && ` ${t("(archive pruned)")}`}</>
        ) : t("Nothing had been flashed before this point.")}
      </div>
      {isCurrent ? (
        <>
          <div className="hint">{t("This is the current state.")}</div>
          {/* 代码已经是这个点，但板子上的固件可能不是（回退时重烧失败、板子掉线……）：允许只重烧 */}
          {fwOk && (
            <div className="actions">
              {!canReflash && <span className="err" style={{ flex: 1 }}>{t("No board is bound to this session, so it cannot be reflashed.")}</span>}
              {msg && <span className="err" style={{ flex: 1 }}>{tc(msg)}</span>}
              <button className="btn sm" disabled={!canReflash || busy || working}
                      onClick={async () => {
                        setWorking(true);
                        setMsg("");
                        try {
                          const res = await restoreCheckpoint(sid, e.seq, true);
                          if (res.flash && !res.flash.ok) setMsg(t("Reflashing failed: {why}", { why: tc(res.flash.summary) }));
                          else onClose();
                        } catch (x) {
                          setMsg(String(x instanceof Error ? x.message : x));
                        } finally {
                          setWorking(false);
                        }
                      }}>
                <Icon name="bolt" size={13} />{working ? t("Reflashing…") : t("Reflash this firmware")}
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <label className={`check ${!(fwOk && canReflash) ? "disabled" : ""}`}>
            <input type="checkbox" checked={reflash} disabled={!(fwOk && canReflash)} onChange={(x) => setReflash(x.target.checked)} />
            {t("Also reflash the board with that firmware")}
          </label>
          {/* 不能重烧时把原因写出来（原来只在鼠标悬停提示里，真机上用户以为点了没反应） */}
          {!(fwOk && canReflash) && (
            <div className="hint warn-text">
              {!canReflash ? t("Reflashing is unavailable: no board is bound to this session right now (pick the board in the header first).")
                : t("Reflashing is unavailable: no firmware was archived at this point.")}
            </div>
          )}
          {!preview ? (
            <div className="hint">{t("Checking what restoring would change…")}</div>
          ) : (
            <div className={preview.delete > 0 || preview.links?.length ? "restore-impact warn" : "restore-impact"}>
              {tx("Restoring will {del}, change {modify} and bring back {add}.", {
                del: <b>{t(preview.delete === 1 ? "delete {n} file" : "delete {n} files", { n: preview.delete })}</b>, modify: preview.modify, add: preview.add,
              })}
              {preview.delete > 0 && (
                <div className="mono files">{preview.deleteFiles.slice(0, 8).join(", ")}{preview.delete > 8 ? `, … ${t("({n} more)", { n: preview.delete - 8 })}` : ""}</div>
              )}
              {preview.links?.length > 0 && (
                <div>{tx("Directory links in the working directory are removed first (only the link, not what it points to): {links}", { links: <span className="mono">{preview.links.join(", ")}</span> })}</div>
              )}
            </div>
          )}
          <div className="hint">{t("Restoring changes files only; the conversation is kept and the agent is told on its next turn. The current state is recorded first, so you can come back. The build directory is not restored.")}</div>
          <div className="actions">
            {msg && <span className="err" style={{ flex: 1 }}>{tc(msg)}</span>}
            <button className={`btn sm ${preview?.delete > 0 ? "danger" : "primary"}`} onClick={() => void go()} disabled={busy || working || !preview}
                    title={busy ? t("The session is running; wait for it to finish") : ""}>
              <Icon name="history" size={13} />{working ? t("Restoring…") : t("Restore to here")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/** 会话收尾（§5.3 / D06）：合并前展示完整 diff；worktree 只在合并完成或确认丢弃后才删除 */
export function FinishDialog({ sid, onClose }: { sid: string; onClose: () => void }) {
  const meta = useStore((s) => s.sessions.find((x) => x.id === sid));
  const status = useStore((s) => s.views[sid]?.status ?? "idle");
  const [diff, setDiff] = useState<Json | null>(null);
  const [branches, setBranches] = useState<string[]>([]);
  const [target, setTarget] = useState(meta?.target_branch ?? "");
  const [message, setMessage] = useState(meta?.title ?? "");
  const [busy, setBusy] = useState<string>("");
  const [err, setErr] = useState<{ text: string; conflicts?: string[] } | null>(null);
  const [exported, setExported] = useState<Json | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [showPatch, setShowPatch] = useState(false);
  const running = status !== "idle";
  // 会话带着用户未提交的改动开始时，"应用到工程文件夹"是首选：起点相同，补丁一定套得上
  const carried = meta?.carried?.length ?? 0;

  useEffect(() => {
    sessionDiff(sid).then(setDiff).catch((e) => setErr({ text: String(e.message ?? e) }));
    listBranches(sid).then((r) => {
      setBranches(r.branches);
      if (!target) setTarget(r.default ?? r.branches[0] ?? "");
    }).catch(() => {});
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (what: string, fn: () => Promise<Json>) => {
    setBusy(what);
    setErr(null);
    try {
      const res = await fn();
      if (what === "export") setExported(res);
      else onClose();
    } catch (e) {
      const data = e instanceof RpcError ? e.data : null;
      setErr({ text: String(e instanceof Error ? e.message : e), conflicts: data?.conflicts });
    } finally {
      setBusy("");
    }
  };
  const files: string[] = useMemo(() => diff?.files ?? [], [diff]);

  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal wide ${showPatch ? "xwide" : ""}`} role="dialog" aria-label={t("Finish session")}>
        <div className="modal-head">
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3>{t("Finish “{title}”", { title: meta?.title })}</h3>
            <p>{tx("Branch {branch} in worktree {wt}. Everything changed since the session started:", { branch: <code>{meta?.branch}</code>, wt: <code>{meta?.worktree}</code> })}</p>
          </div>
          <button className="btn ghost sm icon-only" onClick={onClose} aria-label={t("Close")}><Icon name="x" size={15} /></button>
        </div>
        <div className="modal-body">
          {running && <div className="callout warn"><Icon name="alert" /><div>{t("The session is running. Wait for it to finish, or stop it, before merging or discarding.")}</div></div>}
          {!diff ? (
            <div className="empty">{t("Reading changes…")}</div>
          ) : files.length === 0 ? (
            <div className="empty">{t("No changes.")}</div>
          ) : (
            <div className="finish-files">
              <div className="bar">
                <b>{t(files.length === 1 ? "{n} file" : "{n} files", { n: files.length })}</b>
                <span style={{ color: "var(--add)" }}>+{diff.added}</span>
                <span style={{ color: "var(--del)" }}>−{diff.deleted}</span>
                <span className="spacer" />
                <button className="btn ghost xs" onClick={() => setShowPatch(!showPatch)}>{showPatch ? t("Show file list") : t("Show full diff")}</button>
              </div>
              {!showPatch && <ul>{files.slice(0, 40).map((f) => <li key={f}>{f}</li>)}</ul>}
              {showPatch && <div className="finish-diff"><DiffViewer diff={diff.patch} />{diff.truncated && <div className="hint" style={{ padding: 8 }}>{t("(Too long; only the first 2 MB is shown. The exported patch is complete.)")}</div>}</div>}
            </div>
          )}

          <div className="grid2" style={{ gridTemplateColumns: "1fr 2fr" }}>
            <label className="field">
              <span>{t("Merge into branch")}</span>
              <Select variant="field" value={target} onChange={setTarget} placeholder={t("Choose a branch")} icon={<Icon name="branch" size={13} />}
                      options={branches.map((b) => ({ value: b, label: b }))} />
            </label>
            <label className="field">
              <span>{t("Commit message")}</span>
              <input className="input" value={message} onChange={(e) => setMessage(e.target.value)} />
            </label>
          </div>
          <div className="hint">
            {tx("{apply} writes the changes into {root} as uncommitted edits, so you can review them in your editor and commit yourself.", { apply: <b>{t("Apply to project folder")}</b>, root: <code>{meta?.repo_root}</code> })}
            {carried > 0 && <> {t(carried === 1 ? "The session started from your {n} uncommitted change, so this is the recommended way to finish." : "The session started from your {n} uncommitted changes, so this is the recommended way to finish.", { n: carried })}</>}
            {" "}{tx("{merge} creates one commit on the target branch, authored by your git identity (fast-forward when possible).", { merge: <b>{t("Merge")}</b> })}
            {" "}{t("Either way, conflicts change nothing. Untracked files copied into the worktree (such as sdkconfig) are not merged; applying and the exported patch include them.")}
          </div>

          {err && (
            <div className="callout error">
              <Icon name="alert" />
              <div>
                {tc(err.text)}
                {err.conflicts?.length ? <div>{t("Conflicting files: {files}. Export a patch and resolve by hand, or resolve in your project and try again.", { files: err.conflicts.join(", ") })}</div> : null}
              </div>
            </div>
          )}
          {exported && (
            <div className="callout info">
              <Icon name="download" />
              <div>
                {tx("Patch exported ({n} files, {kb} KB): {path}", { n: exported.files.length, kb: Math.round(exported.bytes / 1024), path: <code>{exported.path}</code> })}{" "}
                <button className="btn ghost xs" onClick={() => void native.showItem(exported.path)}>{t("Show in folder")}</button>
                <div className="hint">{tx("Apply it from the repository root with {cmd}. The worktree is kept until you discard it.", { cmd: <code>git apply &lt;patch&gt;</code> })}</div>
              </div>
            </div>
          )}
        </div>
        <div className="modal-foot">
          {confirmDiscard ? (
            <>
              <span className="err" style={{ flex: 1 }}>{t("Discard? The worktree, branch {branch} and its checkpoints will be deleted. This cannot be undone.", { branch: meta?.branch })}</span>
              <button className="btn" onClick={() => setConfirmDiscard(false)}>{t("Keep")}</button>
              <button className="btn danger" disabled={!!busy || running} onClick={() => void act("discard", () => discardSession(sid))}>
                <Icon name="trash" size={14} />{busy === "discard" ? t("Deleting…") : t("Discard")}
              </button>
            </>
          ) : (
            <>
              <button className="btn danger" disabled={!!busy || running} onClick={() => setConfirmDiscard(true)}><Icon name="trash" size={14} />{t("Discard…")}</button>
              <span className="spacer" />
              <button className="btn" disabled={!!busy || files.length === 0} onClick={() => void act("export", () => exportPatch(sid))}>
                <Icon name="download" size={14} />{busy === "export" ? t("Exporting…") : t("Export patch")}
              </button>
              <button className={`btn ${carried ? "" : "primary"}`} disabled={!!busy || running || !target || files.length === 0}
                      onClick={() => void act("merge", () => mergeSession(sid, target, message.trim()))}>
                <Icon name="merge" size={14} />{busy === "merge" ? t("Merging…") : t("Merge into {branch}", { branch: target || "…" })}
              </button>
              <button className={`btn ${carried ? "primary" : ""}`} disabled={!!busy || running || files.length === 0}
                      onClick={() => void act("apply", () => applySession(sid))}>
                <Icon name="check" size={14} />{busy === "apply" ? t("Applying…") : t("Apply to project folder")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
