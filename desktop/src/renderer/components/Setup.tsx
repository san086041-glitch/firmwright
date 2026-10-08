/** 首次启动向导（2026-10-06）：ESP-IDF → 模型 → 板子。
 *  缺 ESP-IDF 或没有模型时启动就显示（用户点过"以后再说"就不再自动弹）；欢迎页也有入口。
 *  没接板子不提供模拟板 / QEMU（用户决定，docs/decisions/2026-10-06-first-run-setup.md），只说明怎么接。 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  dismissSetup,
  restartCore,
  inspectIdfFolder,
  refreshModels,
  rescanBoards,
  selectIdf,
  setupStatus,
  type IdfCandidate,
  type SetupStatus,
} from "../actions";
import { native } from "../rpc";
import { useStore } from "../store";
import { Icon, Logo } from "./Icon";
import { EMPTY, ModelForm } from "./Settings";

const IDF_GUIDE = "https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32/get-started/windows-setup.html";
const SERIAL_GUIDE = "https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32/get-started/establish-serial-connection.html";
const SOURCE_TEXT: Record<string, string> = { eim: "Installation Manager", legacy: "ESP-IDF installer", folder: "folder" };

export function SetupPage() {
  const models = useStore((s) => s.models);
  const boardCount = useStore((s) => Object.values(s.boards).filter((b) => b.state !== "disconnected").length);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const [err, setErr] = useState("");

  const reload = async () => {
    try {
      setSt(await setupStatus(true));
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };
  useEffect(() => { void reload(); }, []);

  const idfOk = !!st?.idf.active;
  const ready = idfOk && models.length > 0;
  const close = (startSession: boolean) => {
    void dismissSetup().catch(() => undefined);
    useStore.setState({ showSetup: false, showNewSession: startSession });
  };

  return (
    <div className="start">
      <div className="start-inner setup">
        <div className="start-head">
          <Logo size={28} />
          <h2>Set up Firmwright</h2>
          <span className="spacer" />
          <button className="btn ghost sm" onClick={() => close(false)}>{ready ? "Close" : "Skip for now"}</button>
        </div>
        <div className="note">Two things are required before the first session, and a board is recommended. Everything here can be changed later in Settings.</div>
        {err && <div className="callout error"><Icon name="alert" /><div>{err}</div></div>}

        <ol className="setup-steps">
          <Step n={1} title="ESP-IDF" done={idfOk} summary={st?.idf.active ? `v${st.idf.active.version ?? "?"} · ${st.idf.active.path}` : undefined}>
            {st ? <IdfStep st={st} onChanged={setSt} /> : <div className="hint"><Icon name="refresh" size={13} className="spin" /> Looking for ESP-IDF…</div>}
          </Step>
          <Step n={2} title="Model" done={models.length > 0}
                summary={models.length ? `${models.length} model${models.length > 1 ? "s" : ""} · ${models.map((m) => m.id).join(", ")}` : undefined}>
            <ModelStep />
          </Step>
          <Step n={3} title="Board" optional done={idfOk && boardCount > 0}>
            <BoardStep idfOk={idfOk} />
          </Step>
        </ol>

        <div className="setup-foot">
          <span className="hint">{ready ? "All set." : !idfOk ? "ESP-IDF is still missing." : "Add a model to continue."}</span>
          <span className="spacer" />
          <button className="btn primary" disabled={!ready} onClick={() => close(true)}>
            <Icon name="plus" size={15} />Start the first session
          </button>
        </div>
      </div>
    </div>
  );
}

function Step({ n, title, done, optional, summary, children }:
  { n: number; title: string; done: boolean; optional?: boolean; summary?: string; children: ReactNode }) {
  return (
    <li className={`setup-step ${done ? "done" : ""}`}>
      <div className="step-hd">
        <span className="num">{done ? <Icon name="check" size={13} /> : n}</span>
        <b>{title}</b>
        {optional && <span className="chip">optional</span>}
        {summary && <span className="sum ellipsis" title={summary}>{summary}</span>}
      </div>
      <div className="step-bd">{children}</div>
    </li>
  );
}

// ------------------------------------------------------------------ 1. ESP-IDF

export function IdfStep({ st, onChanged }: { st: SetupStatus; onChanged: (s: SetupStatus) => void }) {
  const active = st.idf.active;
  const [picking, setPicking] = useState(!active);
  const [browsed, setBrowsed] = useState<IdfCandidate[] | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [restart, setRestart] = useState(false);
  const list = browsed ?? st.candidates ?? [];

  const use = async (c: IdfCandidate) => {
    setBusy(c.path);
    setErr("");
    try {
      const res = await selectIdf(c);
      setRestart(!!res.restartRequired);
      onChanged({ ...res, candidates: st.candidates });
      if (!res.restartRequired) setPicking(false);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };
  const browse = async () => {
    const dir = await native.pickFolder();
    if (!dir) return;
    setErr("");
    try {
      const found = await inspectIdfFolder(dir);
      if (found.length === 1 && !found[0].id) {  // 文件夹里没有 IDF：报错，不当成一个候选列出来
        setErr(found[0].problem ?? `No ESP-IDF found in ${dir}`);
        setBrowsed(null);
      } else {
        setBrowsed(found);
      }
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };
  const same = (c: IdfCandidate) => !!active && c.path.toLowerCase() === active.path.toLowerCase();

  return (
    <div className="step-body">
      {active ? (
        <div className="check-list">
          <div className="check-line ok">
            <Icon name="check" />
            <div>ESP-IDF <b>v{active.version ?? "?"}</b> <span className="faint">via {SOURCE_TEXT[active.source] ?? active.source}</span>
              <div className="mono faint small">{active.path}</div>
            </div>
          </div>
          {active.warning && <div className="check-line warn"><Icon name="alert" /><div>{active.warning}</div></div>}
        </div>
      ) : (
        <div className="callout warn">
          <Icon name="alert" />
          <div>
            <div>Firmwright needs ESP-IDF (v5.5) to build, flash and decode crashes.
              {list.length > 0 ? " Pick an installation below." : <> If it is not installed yet, install it with Espressif's Installation Manager (EIM), then come back here. <a href={IDF_GUIDE} target="_blank" rel="noreferrer">Installation guide <Icon name="external" size={12} /></a></>}
            </div>
            {st.idf.error && <div className="small faint">{st.idf.error}</div>}
          </div>
        </div>
      )}
      {restart && (
        <div className="callout info">
          <Icon name="info" />
          <div className="restart-row">
            <span>Saved. The core switches to this installation after a restart; open sessions are reloaded.</span>
            <RestartButton />
          </div>
        </div>
      )}

      {picking ? (
        <>
          {list.length > 0 && (
            <div className="idf-list">
              {browsed && <div className="hint">In the folder you picked:</div>}
              {!browsed && <div className="hint">Found on this computer:</div>}
              {list.map((c) => (
                <div key={c.source + c.path} className={`idf-row ${c.problem ? "bad" : ""}`}>
                  <Icon name={c.problem ? "alert" : "cpu"} size={16} />
                  <div className="main">
                    <div><b>{c.version ? `ESP-IDF v${c.version}` : "ESP-IDF"}</b> <span className="faint">· {SOURCE_TEXT[c.source]}</span>
                      {same(c) && <span className="chip ok" style={{ marginLeft: 6 }}>in use</span>}</div>
                    <div className="mono faint small ellipsis" title={c.path}>{c.path}</div>
                    {c.problem && <div className="small bad-text">{c.problem}</div>}
                    {!c.problem && c.warning && <div className="small warn-text">{c.warning}</div>}
                  </div>
                  <button className="btn sm" disabled={!!c.problem || !!busy || same(c)} onClick={() => void use(c)}>
                    {busy === c.path ? <Icon name="refresh" size={13} className="spin" /> : null}Use this
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="row-acts">
            <button className="btn sm" onClick={() => void browse()}><Icon name="folder" size={13} />Choose the ESP-IDF folder…</button>
            {active && <button className="btn sm ghost" onClick={() => { setPicking(false); setBrowsed(null); }}>Cancel</button>}
            <span className="spacer" />
            <a className="hint" href={IDF_GUIDE} target="_blank" rel="noreferrer">How to install ESP-IDF <Icon name="external" size={11} /></a>
          </div>
        </>
      ) : (
        <div className="row-acts">
          <button className="btn sm ghost" onClick={() => setPicking(true)}>Use a different installation</button>
        </div>
      )}
      {err && <div className="callout error"><Icon name="alert" /><div>{err}</div></div>}
    </div>
  );
}

// ------------------------------------------------------------------ 2. 模型

function ModelStep() {
  const models = useStore((s) => s.models);
  const defaultModel = useStore((s) => s.defaultModel);
  const [adding, setAdding] = useState(models.length === 0);
  const done = () => { setAdding(false); void refreshModels(); };

  return (
    <div className="step-body">
      {models.length > 0 && (
        <div className="check-list">
          {models.map((m) => (
            <div key={m.id} className="check-line ok">
              <Icon name="check" />
              <div><b>{m.id}</b> {m.id === defaultModel && <span className="chip ok">default</span>}</div>
            </div>
          ))}
        </div>
      )}
      {models.length === 0 && !adding && <div className="note">Firmwright works with any OpenAI-compatible API that supports tool calling.</div>}
      {adding
        ? <ModelForm draft={{ ...EMPTY }} isNew onDone={done} />
        : <div className="row-acts"><button className={`btn sm ${models.length ? "ghost" : "primary"}`} onClick={() => setAdding(true)}><Icon name="plus" size={13} />Add {models.length ? "another" : "a"} model</button></div>}
    </div>
  );
}

// ------------------------------------------------------------------ 3. 板子

function BoardStep({ idfOk }: { idfOk: boolean }) {
  const boards = useStore((s) => s.boards);
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const [scanning, setScanning] = useState(false);
  const list = Object.values(boards).filter((b) => b.state !== "disconnected");

  if (!idfOk || !devicesAvailable) {
    return <div className="note">Board detection starts once ESP-IDF is set up.</div>;
  }
  const rescan = async () => {
    setScanning(true);
    try { await rescanBoards(); } finally { setScanning(false); }
  };
  return (
    <div className="step-body">
      {list.length > 0 ? (
        <div className="check-list">
          {list.map((b) => (
            <div key={b.id} className="check-line ok">
              <Icon name="usb" />
              <div><b>{b.alias}</b> <span className="faint">· {b.chip ?? "chip not identified yet"} · {b.port}</span></div>
            </div>
          ))}
        </div>
      ) : (
        <div className="board-tips">
          <div className="waiting"><Icon name="usb" size={16} />No board connected. Plug one in; it shows up here automatically.</div>
          <ul>
            <li>Use a USB <b>data</b> cable; some cables only carry power.</li>
            <li>On chips with a built-in USB port (ESP32-S3, C3, C6, H2, P4), connect to the port marked <b>USB</b>: it needs no driver.</li>
            <li>Boards with a USB-to-UART chip (CP210x, CH340, FTDI) may need the vendor's driver.
              {" "}<a href={SERIAL_GUIDE} target="_blank" rel="noreferrer">Serial connection guide <Icon name="external" size={11} /></a></li>
          </ul>
          <div className="note">You can also skip this: sessions without a board can still edit and build.</div>
        </div>
      )}
      <div className="row-acts">
        <button className="btn sm ghost" onClick={() => void rescan()} disabled={scanning}>
          <Icon name="refresh" size={13} className={scanning ? "spin" : ""} />Rescan
        </button>
      </div>
    </div>
  );
}

/** 重启核心（换 ESP-IDF、改 MCP 服务器后）。有会话在执行时核心会拒绝并说明是哪些会话 */
export function RestartButton({ label = "Restart core now" }: { label?: string }) {
  const connected = useStore((s) => s.connected);
  const [state, setState] = useState<"" | "restarting" | string>("");
  const sawDown = useRef(false);
  useEffect(() => {  // 先断开、再连上 = 重启完成
    if (state !== "restarting") return;
    if (!connected) sawDown.current = true;
    else if (sawDown.current) {
      sawDown.current = false;
      setState("");
    }
  }, [connected, state]);
  const go = async () => {
    setState("restarting");
    try {
      await restartCore();
    } catch (e) {
      setState(String(e instanceof Error ? e.message : e));
    }
  };
  return (
    <span className="restart-btn">
      <button className="btn sm" disabled={state === "restarting"} onClick={() => void go()}>
        <Icon name="refresh" size={13} className={state === "restarting" ? "spin" : ""} />{state === "restarting" ? "Restarting…" : label}
      </button>
      {state && state !== "restarting" && <span className="small bad-text">{state}</span>}
    </span>
  );
}
