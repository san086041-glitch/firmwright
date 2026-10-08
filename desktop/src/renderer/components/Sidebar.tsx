import { useMemo, useState } from "react";
import { selectSession, setIdlePolicy } from "../actions";
import { useStore, type SessionMeta } from "../store";
import { Icon, Logo } from "./Icon";
import { Select } from "./Select";
import { ago, baseName } from "./util";

const STATUS_TEXT: Record<string, [string, string]> = {
  running: ["Working", ""],
  awaiting_approval: ["Needs approval", "warn"],
  awaiting_human: ["Needs you", "warn"],
  error: ["Error", "bad"],
};

/** 左栏：会话列表，按工程分组（I05 多会话；D12 三栏布局） */
export function Sidebar() {
  const sessions = useStore((s) => s.sessions);
  const current = useStore((s) => s.current);
  const views = useStore((s) => s.views);
  const connected = useStore((s) => s.connected);
  const boards = useStore((s) => s.boards);
  const coreInfo = useStore((s) => s.coreInfo);
  const settings = useStore((s) => s.settings);
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const [q, setQ] = useState("");
  const [closed, setClosed] = useState<Record<string, boolean>>({});

  // 工程按最近活动排序，工程内的会话也按最近活动排序
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const m = new Map<string, SessionMeta[]>();
    for (const s of sessions) {
      if (needle && !`${s.title} ${s.id} ${s.branch ?? ""} ${baseName(s.project_root)}`.toLowerCase().includes(needle)) continue;
      if (!m.has(s.project_root)) m.set(s.project_root, []);
      m.get(s.project_root)!.push(s);
    }
    const t = (s: SessionMeta) => s.mtime ?? Date.parse(s.created_at) / 1000;
    return [...m.entries()]
      .map(([root, list]) => [root, list.sort((a, b) => t(b) - t(a))] as const)
      .sort((a, b) => t(b[1][0]) - t(a[1][0]));
  }, [sessions, q]);

  const fwr = coreInfo?._meta?.fwr;
  return (
    <aside className="sidebar">
      <div className="brand">
        <Logo />
        <b>Firmwright</b>
        <span className="spacer" />
        <span className={`dot ${connected ? "ok" : "off"}`} title={connected ? "Core connected" : "Core not connected"} />
      </div>
      <button className="btn primary new-btn" onClick={() => useStore.setState({ showNewSession: true, showSettings: false })}>
        <Icon name="plus" size={15} />New session<span className="kbd">Ctrl N</span>
      </button>
      {sessions.length > 6 && (
        <div className="side-search">
          <Icon name="search" size={14} />
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter sessions" />
        </div>
      )}
      <div className="session-groups">
        {sessions.length === 0 && <div className="empty" style={{ padding: "8px 8px" }}>{connected ? "No sessions yet" : "Connecting…"}</div>}
        {groups.map(([root, list]) => (
          <div key={root}>
            <button className={`group-title ${closed[root] ? "closed" : ""}`} title={root}
                    onClick={() => setClosed({ ...closed, [root]: !closed[root] })}>
              <Icon name="chevronDown" size={12} className="chev" />
              <span className="ellipsis">{baseName(root)}</span>
              <span className="count">{list.length}</span>
            </button>
            {!closed[root] && list.map((s) => {
              const status = views[s.id]?.status ?? s.status ?? "idle";
              const board = s.board_id ? boards[s.board_id] : undefined;
              const crashed = board?.state === "crashed" && board.owner_session === s.id;
              const ended = s.state === "merged" || s.state === "applied" || s.state === "discarded";
              const [label, tone] = crashed ? ["Crashed", "bad"] : ended ? [s.state === "merged" ? "Merged" : s.state === "applied" ? "Applied" : "Discarded", ""]
                : STATUS_TEXT[status] ?? [ago(s.mtime ?? s.created_at), ""];
              return (
                <button key={s.id} className={`session-item ${current === s.id ? "active" : ""} ${ended ? "ended" : ""}`}
                        onClick={() => { useStore.setState({ sidebarOpen: false }); void selectSession(s.id); }} title={`${s.title} · ${s.id}${s.branch ? ` · ${s.branch}` : ""}`}>
                  <span className={`dot ${crashed ? "crashed" : status}`} />
                  <span className="t">{s.title}</span>
                  <span className={`s ${tone}`}>{label}</span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="side-foot">
        {devicesAvailable && (
          <div className="row" title="When a board crashes while no task is running (each board can override this)">
            <Icon name="bolt" size={13} />
            <Select value={settings.idlePolicy ?? "notify"} onChange={(v) => void setIdlePolicy(v as "ignore" | "notify")} width={260}
                    options={[
                      { value: "notify", label: "Idle crash: notify me", hint: "When no task is running, a crash shows a card with the decoded backtrace" },
                      { value: "ignore", label: "Idle crash: ignore", hint: "Crashes outside tasks are only recorded" },
                    ]} />
          </div>
        )}
        <div className="row">
          <Icon name="cpu" size={13} />
          <span className="ellipsis">{connected ? `Core v${fwr?.version ?? "?"} · ${fwr?.platform ?? "no ESP-IDF"}` : "Starting core…"}</span>
          {fwr?.sim && <span className="chip warn" style={{ height: 18, fontSize: 10.5, marginLeft: "auto" }}>SIM</span>}
          <button className="btn ghost xs icon-only settings-btn" title="Settings: models and defaults" aria-label="Settings"
                  onClick={() => useStore.setState({ showSettings: true, showNewSession: false, sidebarOpen: false })}>
            <Icon name="sliders" size={14} />
          </button>
        </div>
      </div>
    </aside>
  );
}
