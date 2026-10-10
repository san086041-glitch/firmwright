import { useEffect, useMemo, useRef, useState } from "react";
import { identifyBoard, loadSerialTail, refreshSize, rescanBoards, resetBoard, simCrash, simPlug, updateBoard } from "../actions";
import { locale, t, tc, tk } from "../i18n";
import { useStore, type Board, type DeviceEvent } from "../store";
import { FrameRow, IdleCrashCard } from "./Cards";
import { Icon } from "./Icon";
import { Select } from "./Select";
import { fmtBytes } from "./util";

const STATE_TEXT: Record<string, string> = {
  disconnected: tk("Offline"), idle: tk("Idle"), flashing: tk("Flashing"), running: tk("Running"), crashed: tk("Crashed"), busy: tk("Busy / download mode"),
};
const STATE_CHIP: Record<string, string> = { crashed: "bad", disconnected: "warn", busy: "warn", flashing: "info", running: "ok", idle: "" };

/** 右栏：设备（I08 嵌入式特色元素；D12 / D13） */
export function DeviceColumn() {
  const boards = useStore((s) => s.boards);
  const current = useStore((s) => s.current);
  const sim = useStore((s) => !!s.coreInfo?._meta?.fwr?.sim);
  const allNotices = useStore((s) => s.notices);
  const notices = useMemo(() => allNotices.filter((n) => !n.dismissed), [allNotices]);
  const meta = useStore((s) => s.sessions.find((x) => x.id === s.current));
  const list = Object.values(boards).sort((a, b) => Number(b.id === meta?.board_id) - Number(a.id === meta?.board_id));
  const [focus, setFocus] = useState<string | null>(null);
  const focused = focus && boards[focus] ? focus : meta?.board_id ?? list[0]?.id ?? null;
  const [scanning, setScanning] = useState(false);
  const rescan = async () => {
    setScanning(true);
    try {
      await rescanBoards();
    } finally {
      setScanning(false);
    }
  };

  return (
    <aside className="devices">
      <header>
        <Icon name="chip" size={16} />{t("Devices")} <span className="count">{list.length}</span>
        <span className="spacer" />
        <button className="btn ghost sm" title={t("Scan the serial ports again")} onClick={() => void rescan()} disabled={scanning}>
          <Icon name="refresh" size={13} className={scanning ? "spin" : ""} />{t("Rescan")}
        </button>
        <button className="btn ghost sm icon-only" title={t("Hide devices")} onClick={() => useStore.setState({ devicesOpen: false })}>
          <Icon name="x" size={14} />
        </button>
      </header>
      {sim && <SimControls />}
      <div className="dev-list" style={{ marginTop: sim ? 10 : 0 }}>
        {notices.map((n) => <IdleCrashCard key={n.id} n={n} />)}
        {list.map((b) => (
          <BoardCard key={b.id} b={b} mine={b.owner_session === current && !!current} focused={b.id === focused}
                     onFocus={() => setFocus(b.id)} />
        ))}
      </div>
      {list.length === 0 && (
        <div className="dev-empty">
          <Icon name="usb" size={20} />
          {t("No boards detected. Plug an ESP32-series board (ESP32, S2, S3, C2, C3, C6, H2 or P4) into this PC with a data cable and it shows up here within a second. Boards with a USB-UART bridge (CP210x / CH340) need its driver installed. If it does not appear, click Rescan.")}
        </div>
      )}
      {focused && boards[focused] && <BoardDetail board={boards[focused]} sid={current} />}
    </aside>
  );
}

function BoardCard({ b, mine, focused, onFocus }: { b: Board; mine: boolean; focused: boolean; onFocus: () => void }) {
  const sessions = useStore((s) => s.sessions);
  const owner = b.owner_session ? sessions.find((s) => s.id === b.owner_session) : undefined;
  const [editing, setEditing] = useState(false);
  const [alias, setAlias] = useState(b.alias);
  const [identifying, setIdentifying] = useState(false);
  const [idErr, setIdErr] = useState("");
  const identify = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setIdentifying(true);
    setIdErr("");
    try {
      await identifyBoard(b.id);
    } catch (x) {
      setIdErr(String(x instanceof Error ? x.message : x));
    } finally {
      setIdentifying(false);
    }
  };
  return (
    <div className={`board ${focused ? "focused" : ""} ${b.state === "crashed" ? "crashed" : ""}`} onClick={onFocus}>
      <div className="top">
        <span className={`dot ${b.state === "running" ? "ok" : b.state === "crashed" ? "crashed" : b.state === "disconnected" ? "off" : "idle"}`} />
        {editing ? (
          <span className="name">
            <input className="input" autoFocus value={alias} onChange={(e) => setAlias(e.target.value)} onClick={(e) => e.stopPropagation()}
                   onBlur={() => { setEditing(false); if (alias.trim() && alias !== b.alias) void updateBoard(b.id, { alias: alias.trim() }); }}
                   onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
          </span>
        ) : (
          <span className="name" title={t("Double-click to rename")} onDoubleClick={() => setEditing(true)}>{b.alias}</span>
        )}
        <ChipBadge chip={b.chip} />
        <span className={`chip ${STATE_CHIP[b.state] ?? ""}`}>{STATE_TEXT[b.state] ? t(STATE_TEXT[b.state]) : b.state}</span>
      </div>
      <div className="sub">
        <span>{b.chip ?? t("unknown chip")}</span>·<span className="mono">{b.port ?? "—"}</span>
        {b.state !== "disconnected" && (
          <button className="btn ghost xs" onClick={identify} disabled={identifying}
                  title={t("Read the chip model, MAC and flash size with esptool. This resets the board.")}>
            {identifying ? t("Identifying…") : b.chip ? t("Re-identify") : t("Identify")}
          </button>
        )}
        {(b.link === "usb_serial_jtag" || (!b.link && b.usb_jtag)) && <span className="link-kind" title={t("Connected through the chip's own USB-Serial-JTAG: no driver needed; the port re-enumerates after flashing")}><Icon name="usb" size={12} />USB-JTAG</span>}
        {b.link === "usb_otg" && <span className="link-kind" title={t("Connected through the chip's own USB-OTG port (logs need the USB CDC console)")}><Icon name="usb" size={12} />USB-OTG</span>}
        {b.link === "uart_bridge" && <span className="link-kind" title={t("Connected through a USB-UART bridge chip (CP210x / CH340 / FTDI)")}><Icon name="bridge" size={12} />{t("UART bridge")}</span>}
        {!b.stable_id && <span title={b.mac ? t("No reliable USB serial number; moving it to another USB port makes it look like a new board") : t("No reliable USB serial number; moving it to another USB port makes it look like a new board. Identify it once so Firmwright can recognize it by MAC.")} style={{ color: "var(--amber)" }}>· {t("port-based ID")}</span>}
      </div>
      {idErr && <div className="sub err" onClick={(e) => { e.stopPropagation(); setIdErr(""); }}>{tc(idErr)}</div>}
      <div className="sub">
        <span className={`owner ${mine ? "mine" : ""}`}>{owner ? (mine ? t("This session") : t("Session “{title}”", { title: owner.title })) : t("Not bound")}</span>
        <Select value={b.idle_policy ?? ""} width={240}
                onChange={(v) => void updateBoard(b.id, { idle_policy: (v || null) as Board["idle_policy"] })}
                title={t("What to do when this board crashes while no task is running (overrides the global setting)")}
                options={[
                  { value: "", label: t("Idle crash: default"), hint: t("Follow the global setting") },
                  { value: "notify", label: t("Idle crash: notify"), hint: t("Show a card with the decoded backtrace") },
                  { value: "ignore", label: t("Idle crash: ignore"), hint: t("Only record it in the event list") },
                ]} />
      </div>
    </div>
  );
}

// 芯片徽标（2026-10-06，界面改进第 12 项）：型号缩写 + 按 CPU 架构着色。和 core/firmwright/platform/esp_idf/chips.py 一致
const CHIP_INFO: Record<string, [string, "xtensa" | "riscv", string]> = {
  esp32: ["ESP32", "xtensa", tk("Xtensa LX6 dual-core · Wi-Fi + Bluetooth Classic/LE")],
  esp32s2: ["S2", "xtensa", tk("Xtensa LX7 single-core · Wi-Fi · USB-OTG")],
  esp32s3: ["S3", "xtensa", tk("Xtensa LX7 dual-core · Wi-Fi + BLE · USB-Serial-JTAG")],
  esp32c2: ["C2", "riscv", tk("RISC-V single-core · Wi-Fi + BLE")],
  esp32c3: ["C3", "riscv", tk("RISC-V single-core · Wi-Fi + BLE · USB-Serial-JTAG")],
  esp32c6: ["C6", "riscv", tk("RISC-V · Wi-Fi 6 + BLE + 802.15.4 · USB-Serial-JTAG")],
  esp32h2: ["H2", "riscv", tk("RISC-V · BLE + 802.15.4 (no Wi-Fi) · USB-Serial-JTAG")],
  esp32p4: ["P4", "riscv", tk("RISC-V dual-core · no radio · USB-Serial-JTAG")],
};

function ChipBadge({ chip }: { chip: string | null }) {
  const info = chip ? CHIP_INFO[chip] : undefined;
  if (!info) return <span className="chip-badge unknown" title={t("Chip not identified yet: shown after the next boot log, or click Identify")}>?</span>;
  return <span className={`chip-badge ${info[1]}`} title={`${chip} · ${t(info[2])}`}>{info[0]}</span>;
}

function BoardDetail({ board, sid }: { board: Board; sid: string | null }) {
  const [tab, setTab] = useState<"serial" | "events">("serial");
  const [frozen, setFrozen] = useState<string[] | null>(null);  // 暂停时冻结的画面；采集照常进行
  const lines = useStore((s) => s.serial[board.id]);
  const events = useStore((s) => s.events[board.id]);
  useEffect(() => {
    if (!lines) void loadSerialTail(board.id);
  }, [board.id, lines]);
  const mineSid = board.owner_session && board.owner_session === sid ? sid : null;
  const noticed = useStore((s) => s.notices.some((n) => !n.dismissed && n.event.board_id === board.id));
  const crash = [...(events ?? [])].reverse().find((e) => ["panic", "abort", "assert", "stack_overflow", "stack_smash"].includes(e.kind));

  return (
    <div className="dev-detail">
      {mineSid && <SizePanel sid={mineSid} />}
      {crash && board.state === "crashed" && !noticed && <CrashMini ev={crash} />}
      <div className="term">
        <div className="term-bar">
          <span className="tabs">
            <button className={tab === "serial" ? "on" : ""} onClick={() => setTab("serial")}>{t("Serial")}</button>
            <button className={tab === "events" ? "on" : ""} onClick={() => setTab("events")}>{t("Events")}{events?.length ? ` · ${events.length}` : ""}</button>
          </span>
          <span className="spacer" />
          <span className="faint ellipsis" style={{ fontSize: 11.5, marginRight: 4 }}>{board.alias}</span>
          {tab === "serial" && (
            <button className="btn ghost xs" onClick={() => setFrozen(frozen ? null : (lines ?? []).slice())}
                    title={frozen ? t("Show live output again") : t("Freeze the view to read it; capture and crash detection keep running")}>
              <Icon name={frozen ? "play" : "pause"} size={12} />{frozen ? t("Resume") : t("Pause view")}
            </button>
          )}
        </div>
        {tab === "serial" && frozen && (
          <div className="term-note">{t("View paused. Capture and crash detection keep running; Resume shows the latest output.")}</div>
        )}
        {tab === "serial" && !frozen && !(lines ?? []).some((l) => l.trim()) ? <SerialEmpty board={board} />
          : tab === "serial" ? <SerialView lines={frozen ?? lines ?? []} follow={!frozen} /> : <EventList events={events ?? []} />}
      </div>
    </div>
  );
}

/** 串口还没有输出（2026-10-06，界面改进第 5 项）：说明为什么是空的，给一个能马上看到输出的操作 */
function SerialEmpty({ board }: { board: Board }) {
  const ownerBusy = useStore((s) => !!board.owner_session && (s.views[board.owner_session]?.status ?? "idle") !== "idle");
  const [state, setState] = useState<"" | "resetting" | string>("");
  if (board.state === "disconnected") {
    return (
      <div className="serial empty-state">
        <Icon name="usb" size={20} />
        <b>{t("Board is offline")}</b>
        <span>{t("Plug it back in or check the cable. Output resumes as soon as it reconnects.")}</span>
      </div>
    );
  }
  const reset = async () => {
    setState("resetting");
    try {
      await resetBoard(board.id);
      setState("");
    } catch (e) {
      setState(String(e instanceof Error ? e.message : e));
    }
  };
  return (
    <div className="serial empty-state">
      <Icon name="terminal" size={20} />
      <b>{t("No output yet")}</b>
      <span>{t("Firmware that is already running prints nothing new until something happens. Reset the board to see its boot log.")}</span>
      {board.link === "usb_otg" && (
        <span className="faint">{t("This board is connected through the chip's own USB-OTG port: logs appear only if the firmware uses the USB CDC console.")}</span>
      )}
      <button className="btn sm" onClick={() => void reset()} disabled={state === "resetting" || ownerBusy}
              title={ownerBusy ? t("The session using this board is working; reset it when the session is idle") : t("Pulse the reset line (EN). The firmware restarts.")}>
        <Icon name="power" size={13} className={state === "resetting" ? "spin" : ""} />{state === "resetting" ? t("Resetting…") : t("Reset board")}
      </button>
      {state && state !== "resetting" && <span className="err">{tc(state)}</span>}
    </div>
  );
}

function CrashMini({ ev }: { ev: DeviceEvent }) {
  const user = (ev.backtrace?.frames ?? []).filter((f) => !f.internal).slice(0, 3);
  return (
    <div className="crash">
      <div className="hd"><Icon name="flame" size={14} />{t("Last crash")} · {ev.detail?.exception ?? ev.kind}</div>
      <div className="bd">
        <div style={{ fontSize: 12.5 }}>{ev.summary}</div>
        {user.length > 0 && (
          <div className="frames">
            {user.map((f, i) => <FrameRow key={i} f={f} text={`${f.function ?? f.pc} ${f.file ? `${f.file.split(/[\\/]/).pop()}:${f.line}` : ""}`} />)}
          </div>
        )}
      </div>
    </div>
  );
}

// eslint-disable-next-line no-control-regex -- 去掉 ESP-IDF 日志里的 ANSI 颜色码
const LEVEL = /^(?:\x1b\[[0-9;]*m)?([EWI]) \(\d+\)/;
const ALERT = /Guru Meditation|abort\(\) was called|assert failed|stack overflow|Brownout|Backtrace:|Rebooting\.\.\./;

/** 实时串口（_fwr/serial/chunk，每 100ms 合并一次）：按 ESP_LOG 级别着色，崩溃相关行高亮 */
function SerialView({ lines, follow }: { lines: string[]; follow: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const shown = useMemo(() => lines.slice(-600), [lines]);
  useEffect(() => {
    const el = ref.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [shown, follow]);
  return (
    <div className="serial" ref={ref}>
      {shown.length === 0 && <span className="empty-term">{t("(no output yet)")}</span>}
      {shown.map((l, i) => {
        // eslint-disable-next-line no-control-regex -- ANSI 颜色码
        const clean = l.replace(/\x1b\[[0-9;]*m/g, "");
        const m = LEVEL.exec(l);
        const cls = [m ? m[1].toLowerCase() : "", ALERT.test(clean) ? "hl e" : ""].join(" ");
        return <div key={i} className={cls}>{clean || " "}</div>;
      })}
    </div>
  );
}

const EV_TEXT: Record<string, string> = {
  boot: tk("boot"), panic: tk("panic"), abort: tk("abort"), assert: tk("assert"), stack_overflow: tk("stack ovf"), stack_smash: tk("stack smash"),
  wdt_reset: tk("watchdog"), brownout: tk("brownout"), reboot_loop: tk("boot loop"), download_mode: tk("download"), marker: tk("marker"),
  disconnect: tk("unplugged"), reconnect: tk("replugged"),
};

function EventList({ events }: { events: DeviceEvent[] }) {
  if (events.length === 0) return <div className="evt-list"><div className="empty" style={{ padding: 10 }}>{t("No events yet")}</div></div>;
  return (
    <div className="evt-list">
      {[...events].reverse().slice(0, 80).map((e) => (
        <div key={e.id} className={`evt ${e.severity}`} title={new Date(e.at).toLocaleString(locale(), { hour12: false })}>
          <span className="k">{EV_TEXT[e.kind] ? t(EV_TEXT[e.kind]) : e.kind}</span>
          <span className="s">{tc(e.summary)}</span>
        </div>
      ))}
    </div>
  );
}

/** 固件大小条（D13）：每次编译成功后刷新 Flash（app / 分区）、IRAM、DRAM 占用 */
function SizePanel({ sid }: { sid: string }) {
  const size = useStore((s) => s.sizes[sid]);
  const lastBuild = useStore((s) => {
    const items = s.views[sid]?.items ?? [];
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      // 编译后刷新大小；烧录后也刷新（"比上次烧录"的基准变了）
      if (it.kind === "tool" && /^(build|flash)/.test(it.title) && it.status === "completed") return it.id;
    }
    return null;
  });
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      await refreshSize(sid);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (lastBuild) void refresh();
  }, [lastBuild]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="card sizebar">
      <div className="section-title">
        {t("Firmware size")}
        <span className="spacer" />
        <button className="btn ghost xs icon-only" onClick={refresh} disabled={busy} title={t("Refresh (idf.py size)")}>
          <Icon name="refresh" size={12} className={busy ? "spin" : ""} />
        </button>
      </div>
      {!size ? (
        <div className="empty">{t("Shown after a successful build")}</div>
      ) : (
        <>
          <Bar label="Flash" used={size.app_bin_size} total={size.app_partition_size} note={t("app / partition")} />
          <FlashDelta now={size.app_bin_size} flashed={size.flashed} />
          <Bar label="IRAM" used={size.iram_used} total={size.iram_total} />
          <Bar label={size.ram_label === "内部RAM" ? t("Internal RAM") : size.ram_label ?? "DRAM"} used={size.dram_used} total={size.dram_total} />
        </>
      )}
    </div>
  );
}

/** 当前编译出的 app 比板子上那份（本会话最近一次烧录）大了 / 小了多少（2026-10-06，界面改进第 12 项） */
function FlashDelta({ now, flashed }: { now?: number; flashed?: { appBinSize: number; turn: number; source: string } | null }) {
  if (now == null || !flashed) return null;
  const d = now - flashed.appBinSize;
  const when = flashed.source === "restore" ? t("the firmware restored from a checkpoint") : t("the firmware flashed in turn {n}", { n: flashed.turn });
  const abs = Math.abs(d);
  const text = d === 0 ? t("same size as on the board") : t("{delta} vs. the board", { delta: `${d > 0 ? "+" : "−"}${abs < 1024 ? `${abs} B` : fmtBytes(abs)}` });
  return (
    <div className={`size-delta ${d > 0 ? "up" : d < 0 ? "down" : ""}`} title={t("Current build: {now} · on the board ({when}): {flashed}", { now: fmtBytes(now), when, flashed: fmtBytes(flashed.appBinSize) })}>
      {text}
    </div>
  );
}

function Bar({ label, used, total, note }: { label: string; used?: number; total?: number; note?: string }) {
  if (used == null || !total) return null;
  const pct = Math.min(100, Math.round((used / total) * 100));
  const cls = pct >= 95 ? "bad" : pct >= 80 ? "warn" : "";
  return (
    <div className="bar" title={`${label}${note ? ` (${note})` : ""}: ${fmtBytes(used)} / ${fmtBytes(total)}`}>
      <span className="ellipsis">{label}</span>
      <span className="track"><span className={`fill ${cls}`} style={{ width: `${pct}%` }} /></span>
      <span className="v">{pct}%</span>
    </div>
  );
}

/** 模拟板控制条：只在核心以 FIRMWRIGHT_SIM_BOARD=1 启动时出现（开发 / 演示用） */
function SimControls() {
  const [present, setPresent] = useState(true);
  return (
    <div className="sim-box">
      <Icon name="cpu" size={14} /><span style={{ flex: 1 }}>{t("Simulated board")}</span>
      <button className="btn xs" onClick={() => void simCrash()}>{t("Crash it")}</button>
      <button className="btn xs" onClick={() => { void simPlug(!present); setPresent(!present); }}>{present ? t("Unplug") : t("Plug in")}</button>
    </div>
  );
}
