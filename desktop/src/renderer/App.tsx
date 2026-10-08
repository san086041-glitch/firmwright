import { useEffect, useMemo, useState } from "react";
import { boot, needsSetup, selectSession } from "./actions";
import { IdleCrashCard } from "./components/Cards";
import { ChatView } from "./components/Chat";
import { DeviceColumn } from "./components/Devices";
import { Icon, Logo } from "./components/Icon";
import { NewSessionPage } from "./components/NewSession";
import { PanelToggles } from "./components/PanelToggles";
import { SettingsPage } from "./components/Settings";
import { SetupPage } from "./components/Setup";
import { Sidebar } from "./components/Sidebar";
import { ago, baseName } from "./components/util";
import { useStore } from "./store";

export function App() {
  const current = useStore((s) => s.current);
  const showNew = useStore((s) => s.showNewSession);
  const showSettings = useStore((s) => s.showSettings);
  const showSetup = useStore((s) => s.showSetup);
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const notices = useStore((s) => s.notices);
  const devicesOpen = useStore((s) => s.devicesOpen);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const toasts = useMemo(() => notices.filter((n) => !n.dismissed && n.sessionId !== current).slice(-2), [notices, current]);
  const [err, setErr] = useState("");

  useEffect(() => {
    boot().catch((e) => setErr(String(e instanceof Error ? e.message : e)));
    // 文件拖到输入框以外的地方：别让窗口跳去打开那个文件（Electron 默认行为）
    const noDrop = (e: DragEvent) => e.preventDefault();
    window.addEventListener("dragover", noDrop);
    window.addEventListener("drop", noDrop);
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        useStore.setState({ showNewSession: true, showSettings: false });
      }
    };
    window.addEventListener("keydown", onKey);
    // 跨过断点时重置抽屉：变窄时收起设备栏，变宽时恢复三栏
    const narrow = window.matchMedia("(max-width: 1180px)");
    const onBreak = () => useStore.setState({ devicesOpen: !narrow.matches, sidebarOpen: false });
    narrow.addEventListener("change", onBreak);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("dragover", noDrop);
      window.removeEventListener("drop", noDrop);
      narrow.removeEventListener("change", onBreak);
    };
  }, []);

  return (
    <div className={`app ${devicesAvailable ? "" : "no-devices"} ${devicesOpen ? "devices-open" : "devices-hidden"} ${sidebarOpen ? "sidebar-open" : ""}`}>
      <Sidebar />
      <main className="center">
        {(!current || showNew || showSettings || showSetup) && <PanelToggles />}
        {showSetup ? <SetupPage /> : showSettings ? <SettingsPage /> : showNew ? <NewSessionPage />
          : current ? <ChatView key={current} sid={current} /> : <Welcome err={err} />}
      </main>
      {devicesAvailable && <DeviceColumn />}
      {(sidebarOpen || devicesOpen) && (
        <div className="scrim" onClick={() => useStore.setState({ sidebarOpen: false, devicesOpen: false })} />
      )}
      {!devicesAvailable && toasts.length > 0 && (
        <div className="toast-stack">{toasts.map((n) => <IdleCrashCard key={n.id} n={n} />)}</div>
      )}
    </div>
  );
}

const FEATURES: [string, string, string][] = [
  ["bolt", "Device in the loop", "Builds, flashes and reads the serial port to verify every change."],
  ["branch", "Isolated sessions", "Each session works in its own git worktree, with checkpoints per turn."],
  ["shield", "You stay in control", "Edits and risky hardware operations ask first; eFuse writes never run."],
];

function Welcome({ err }: { err: string }) {
  const connected = useStore((s) => s.connected);
  const coreInfo = useStore((s) => s.coreInfo);
  const bootErrors = useStore((s) => s.bootErrors);
  const sessions = useStore((s) => s.sessions);
  const models = useStore((s) => s.models);
  const boards = useStore((s) => s.boards);
  const recent = useMemo(() => [...sessions].filter((s) => s.state !== "merged" && s.state !== "discarded")
    .sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0)).slice(0, 5), [sessions]);
  const fwr = coreInfo?._meta?.fwr;

  return (
    <div className="welcome">
      <div className="hero">
        <Logo size={44} />
        <h1>Firmwright</h1>
        <div className="tag">An agent console for embedded development. Keep writing code in your own editor; here you watch the agent work, approve what it does, and keep an eye on your boards.</div>
        <button className="btn primary" style={{ marginTop: 8, height: 36, padding: "0 16px" }} onClick={() => useStore.setState({ showNewSession: true })}>
          <Icon name="plus" size={15} />New session <span className="kbd">Ctrl N</span>
        </button>
      </div>
      {!connected && (
        <div className={`callout ${err ? "error" : "info"}`}>
          <Icon name={err ? "alert" : "refresh"} className={err ? "" : "spin"} />
          <div>{err ? `Could not connect to the core: ${err}` : "Starting the core…"}</div>
        </div>
      )}
      {bootErrors.map((e) => <div key={e} className="callout error"><Icon name="alert" /><div>{e}</div></div>)}
      {connected && needsSetup(fwr?.idf, models.length) && (
        <div className="callout warn">
          <Icon name="alert" />
          <div style={{ flex: 1 }}>
            {!fwr?.idf?.active ? "ESP-IDF was not found" : "No models yet"}. Finish the setup to start a session.
          </div>
          <button className="btn sm primary" onClick={() => useStore.setState({ showSetup: true, showSettings: false, showNewSession: false })}>Finish setup</button>
        </div>
      )}
      {recent.length > 0 && (
        <div className="recent">
          <div className="hd">Recent sessions</div>
          {recent.map((s) => (
            <button key={s.id} onClick={() => void selectSession(s.id)}>
              <Icon name="folder" size={14} className="faint" />
              <span className="ellipsis" style={{ flex: 1 }}>{s.title}</span>
              <span className="faint" style={{ fontSize: 12 }}>{baseName(s.project_root)} · {ago(s.mtime ?? s.created_at)}</span>
            </button>
          ))}
        </div>
      )}
      {recent.length === 0 && (
        <div className="cards">
          {FEATURES.map(([icon, title, text]) => (
            <div className="card" key={title}><b><Icon name={icon as "bolt"} size={14} />{title}</b>{text}</div>
          ))}
        </div>
      )}
      {connected && (
        <div className="core-status">
          <span><span className="dot ok" />Core v{fwr?.version}</span>
          <span title={fwr?.idf?.active?.path}><Icon name="cpu" size={13} />{fwr?.idf?.active ? `ESP-IDF v${fwr.idf.active.version ?? "?"}` : "ESP-IDF not found"}</span>
          <span><Icon name="usb" size={13} />{Object.keys(boards).length} board{Object.keys(boards).length === 1 ? "" : "s"}</span>
          <span title={fwr?.home}><Icon name="folder" size={13} />{sessions.length} sessions · {models.length} models</span>
        </div>
      )}
    </div>
  );
}
