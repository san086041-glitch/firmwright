/** 设置页：2026-10-05 模型和空闲崩溃策略；2026-10-06 补全（外观、默认权限模式、ESP-IDF 和编译并行数、
 *  worktree 根目录、权限规则、MCP 服务器、关于 / 数据位置、重启核心）。
 *  界面里的修改存在 settings.json；config.toml 是用户手写的，那里定义的模型 / 规则 / MCP 服务器在这里只读。
 *  API Key 只往核心发，存 Windows 凭据管理器；界面拿不回来，只显示"有没有"。 */
import { useEffect, useState, type ReactNode } from "react";
import {
  coreAbout,
  deleteMcp,
  deleteModel,
  loadModelDetail,
  refreshModels,
  refreshSettings,
  saveMcp,
  saveModel,
  saveSettings,
  setDefaultModel,
  setIdlePolicy,
  setupStatus,
  testModel,
  type ModelDetail,
  type SetupStatus,
} from "../actions";
import { t, tc, tk, tx } from "../i18n";
import { native, type HostInfo, type Json, type Theme } from "../rpc";
import { useStore } from "../store";
import { MODES } from "./Chat";
import { Icon } from "./Icon";
import { LangSwitch } from "./LangSwitch";
import { Select } from "./Select";
import { IdfStep, RestartButton } from "./Setup";

type Effort = "reasoning_effort" | "enable_thinking" | "thinking_budget" | "thinking_type";

export interface Draft {
  id: string; model: string; baseUrl: string; apiKey: string; contextWindow: string; vision: boolean; reasoning: boolean;
  effortStyle: Effort;
}

// 常见的 OpenAI 兼容服务：只预填地址和思考参数的写法，模型名以服务商文档为准
const PRESETS: { id: string; label: string; baseUrl: string; effortStyle: Effort; reasoning: boolean; hint: string }[] = [
  { id: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com", effortStyle: "thinking_type", reasoning: true, hint: tk("e.g. deepseek-chat") },
  { id: "siliconflow", label: "SiliconFlow", baseUrl: "https://api.siliconflow.cn/v1", effortStyle: "thinking_budget", reasoning: true, hint: tk("e.g. zai-org/GLM-5.3") },
  { id: "dashscope", label: tk("Qwen (DashScope, international)"), baseUrl: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", effortStyle: "enable_thinking", reasoning: true, hint: tk("e.g. qwen-plus") },
  { id: "zai", label: "GLM (Z.ai)", baseUrl: "https://api.z.ai/api/paas/v4", effortStyle: "thinking_type", reasoning: true, hint: tk("e.g. glm-4.6") },
  { id: "moonshot", label: tk("Kimi (Moonshot, international)"), baseUrl: "https://api.moonshot.ai/v1", effortStyle: "thinking_type", reasoning: true, hint: tk("e.g. kimi-k2") },
  { id: "custom", label: tk("Other OpenAI-compatible"), baseUrl: "", effortStyle: "reasoning_effort", reasoning: false, hint: tk("model name the API expects") },
];

const EFFORT_TEXT: Record<Effort, string> = {
  reasoning_effort: tk("reasoning_effort (OpenAI style)"), enable_thinking: tk("enable_thinking + budget (Qwen)"),
  thinking_budget: tk("thinking_budget (SiliconFlow)"), thinking_type: tk("thinking: enabled / disabled"),
};

export const EMPTY: Draft = { id: "", model: "", baseUrl: "", apiKey: "", contextWindow: "128000", vision: false, reasoning: false,
                       effortStyle: "reasoning_effort" };

function toFields(d: Draft): Json {
  return { model: d.model.trim() || null, base_url: d.baseUrl.trim(), context_window: Number(d.contextWindow) || 128000,
           vision: d.vision, reasoning: d.reasoning, effort_style: d.effortStyle };
}

const SECTIONS: [string, string][] = [
  ["general", tk("General")], ["models", tk("Models")], ["idf", tk("ESP-IDF & builds")], ["workspaces", tk("Workspaces")],
  ["permissions", tk("Permissions")], ["mcp", tk("MCP servers")], ["about", tk("About & data")],
];

const errText = (e: unknown) => String(e instanceof Error ? e.message : e);

export function SettingsPage() {
  const connected = useStore((s) => s.connected);
  const [err, setErr] = useState("");
  const close = () => useStore.setState({ showSettings: false });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 输入框里按 Esc 不关整页（和新建会话页一致）
      if (e.key === "Escape" && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // 打开时、以及核心重启后重新连上时，读一次最新设置
  useEffect(() => { if (connected) void refreshSettings().catch((e) => setErr(errText(e))); }, [connected]);
  const jump = (id: string) => document.getElementById(`set-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="start">
      <div className="start-inner settings">
        <div className="start-head">
          <h2>{t("Settings")}</h2>
          <span className="spacer" />
          <button className="btn ghost sm" onClick={() => useStore.setState({ showSettings: false, showSetup: true })}>{t("Setup guide")}</button>
          <button className="btn ghost sm" onClick={close}>{t("Close")} <span className="kbd">Esc</span></button>
        </div>
        <nav className="settings-nav">
          {SECTIONS.map(([id, label]) => <button key={id} className="btn xs ghost" onClick={() => jump(id)}>{t(label)}</button>)}
        </nav>
        {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
        <GeneralSection />
        <ModelsSection />
        <IdfSection />
        <WorkspaceSection />
        <PermissionsSection />
        <McpSection />
        <AboutSection />
      </div>
    </div>
  );
}

function Section({ id, title, note, actions, children }: { id: string; title: string; note?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="settings-sec" id={`set-${id}`}>
      <div className="sec-head">
        <div>
          <div className="sec-title">{t(title)}</div>
          {note && <div className="note">{note}</div>}
        </div>
        <span className="spacer" />
        {actions}
      </div>
      {children}
    </section>
  );
}

/** 保存失败时在那一行下面显示原因 */
function useSaver() {
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async (fields: Json): Promise<boolean> => {
    setBusy(true);
    setErr("");
    try {
      await saveSettings(fields);
      return true;
    } catch (e) {
      setErr(errText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { err, busy, save, setErr };
}

// ------------------------------------------------------------------ 通用

const THEMES: { value: Theme; label: string }[] = [
  { value: "system", label: tk("System") }, { value: "light", label: tk("Light") }, { value: "dark", label: tk("Dark") },
];

function readTheme(): Theme {
  try {
    const th = localStorage.getItem("fwr.theme");
    return th === "light" || th === "dark" ? th : "system";
  } catch {
    return "system";
  }
}

function GeneralSection() {
  const settings = useStore((s) => s.settings);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const { err, save } = useSaver();
  const pickTheme = (th: Theme) => {
    setTheme(th);
    native.applyTheme(th);
    try { localStorage.setItem("fwr.theme", th); } catch { /* 存不了就只在这次生效 */ }
  };
  return (
    <Section id="general" title="General">
      <div className="set-card">
        <div className="setting-line">
          <span>{t("Appearance")}</span>
          <div className="theme-seg" role="radiogroup" aria-label={t("Appearance")}>
            {THEMES.map((th) => (
              <button key={th.value} role="radio" aria-checked={theme === th.value} className={theme === th.value ? "on" : ""} onClick={() => pickTheme(th.value)}>
                {t(th.label)}
              </button>
            ))}
          </div>
        </div>
        <div className="setting-line">
          <span>{t("Language")}<div className="note">{t("Interface language. The agent replies in the language you write to it.")}</div></span>
          <LangSwitch />
        </div>
        <div className="setting-line">
          <span>{t("Permission mode for new sessions")}<div className="note">{t("Each session can still switch modes from its composer.")}</div></span>
          <Select variant="field" value={settings.permissionMode ?? "default"} width={260} onChange={(v) => void save({ permissionMode: v })}
                  options={MODES.map(([k, v, h]) => ({ value: k, label: t(v), hint: t(h) }))} />
        </div>
        <div className="setting-line">
          <span>{t("When a board crashes while no task is running")}</span>
          <Select variant="field" value={settings.idlePolicy ?? "notify"} onChange={(v) => void setIdlePolicy(v as "ignore" | "notify")} width={260}
                  options={[
                    { value: "notify", label: t("Notify me"), hint: t("A card with the decoded backtrace; you decide whether the agent handles it") },
                    { value: "ignore", label: t("Ignore"), hint: t("Crashes outside tasks are only recorded") },
                  ]} />
        </div>
      </div>
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ 模型

function ModelsSection() {
  const [models, setModels] = useState<ModelDetail[]>([]);
  const [def, setDef] = useState<string | null>(null);
  const [configPath, setConfigPath] = useState("");
  const [editing, setEditing] = useState<{ draft: Draft; isNew: boolean } | null>(null);
  const [tests, setTests] = useState<Record<string, Json | "running">>({});
  const [err, setErr] = useState("");

  const reload = async () => {
    const r = await loadModelDetail();
    setModels(r.models);
    setDef(r.default);
    setConfigPath(r.configPath);
  };
  useEffect(() => { void reload().catch((e) => setErr(errText(e))); }, []);

  const runTest = async (id: string) => {
    setTests((m) => ({ ...m, [id]: "running" }));
    const res = await testModel(id, null, "").catch((e) => ({ ok: false, error: errText(e) }));
    setTests((m) => ({ ...m, [id]: res }));
  };
  const act = (fn: () => Promise<unknown>) => { setErr(""); void fn().then(reload).catch((e) => setErr(errText(e))); };

  return (
    <Section id="models" title="Models" note={t("Any OpenAI-compatible API. Keys are stored in the Windows Credential Manager, never in files.")}
             actions={!editing && <button className="btn sm primary" onClick={() => setEditing({ draft: { ...EMPTY }, isNew: true })}><Icon name="plus" size={14} />{t("Add model")}</button>}>
      {editing?.isNew && <ModelForm draft={editing.draft} isNew onDone={() => { setEditing(null); void reload(); void refreshModels(); }} />}
      <div className="model-list">
        {models.length === 0 && <div className="empty">{t("No models yet. Add one to start a session.")}</div>}
        {models.map((m) => {
          const tr = tests[m.id];
          const readonly = m.source === "config.toml";
          if (editing && !editing.isNew && editing.draft.id === m.id) {
            return <ModelForm key={m.id} draft={editing.draft} onDone={() => { setEditing(null); void reload(); }} />;
          }
          return (
            <div key={m.id} className="model-row">
              <div className="main">
                <div className="name">
                  <b>{m.id}</b>
                  {m.id === def && <span className="chip ok">{t("default")}</span>}
                  {m.vision && <span className="chip">{t("vision")}</span>}
                  {m.reasoning && <span className="chip">{t("reasoning")}</span>}
                  {readonly && <span className="chip" title={t("Defined in {path}; edit it there", { path: configPath })}>config.toml</span>}
                </div>
                <div className="sub mono">{m.model} · {hostOf(m.baseUrl)} · {t("{n}k context", { n: Math.round(m.contextWindow / 1000) })}</div>
                <div className={`sub ${m.hasKey ? "" : "warn-text"}`}>
                  <Icon name="key" size={12} />{m.hasKey ? t("API key set") : t("No API key found ({ref})", { ref: m.keyRef })}
                </div>
                {tr && tr !== "running" && <TestResult res={tr} />}
              </div>
              <div className="acts">
                <button className="btn xs" onClick={() => void runTest(m.id)} disabled={tr === "running" || !m.hasKey}>
                  {tr === "running" ? <Icon name="refresh" size={12} className="spin" /> : <Icon name="pulse" size={12} />}{t("Test")}
                </button>
                {m.id !== def && <button className="btn xs" onClick={() => act(() => setDefaultModel(m.id))}>{t("Make default")}</button>}
                {!readonly && (
                  <>
                    <button className="btn xs" disabled={!!editing} onClick={() => setEditing({ isNew: false, draft: {
                      id: m.id, model: m.model === m.id ? "" : m.model, baseUrl: m.baseUrl, apiKey: "", contextWindow: String(m.contextWindow),
                      vision: m.vision, reasoning: m.reasoning, effortStyle: m.effortStyle as Effort } })}>
                      <Icon name="edit" size={12} />{t("Edit")}
                    </button>
                    <button className="btn xs danger" title={t("Delete")} onClick={() => { if (window.confirm(t("Delete model {id} and its stored API key?", { id: m.id }))) act(() => deleteModel(m.id)); }}>
                      <Icon name="trash" size={12} />
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {configPath && models.some((m) => m.source === "config.toml") && <div className="note">{tx("Models marked config.toml are defined in {path} and are read-only here.", { path: <span className="mono">{configPath}</span> })}</div>}
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ ESP-IDF 和编译

function IdfSection() {
  const settings = useStore((s) => s.settings);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const { err, save } = useSaver();
  useEffect(() => { void setupStatus(true).then(setSt).catch(() => undefined); }, []);
  const cpus: number = settings.cpuCount ?? 8;
  const jobs = [{ value: "0", label: t("Automatic ({n})", { n: settings.buildJobsAuto ?? Math.max(2, Math.floor(cpus / 2)) }), hint: t("Half the logical cores, at least 2") },
                ...Array.from({ length: cpus }, (_, i) => ({ value: String(i + 1), label: `${i + 1}` }))];
  return (
    <Section id="idf" title="ESP-IDF & builds">
      <div className="set-card pad">
        {st ? <IdfStep st={st} onChanged={setSt} /> : <div className="hint"><Icon name="refresh" size={13} className="spin" /> {t("Looking for ESP-IDF…")}</div>}
      </div>
      <div className="set-card">
        <div className="setting-line">
          <span>{t("Parallel build jobs")}<div className="note">{t("Builds started by the agent only. Lower it if the machine becomes unresponsive while building; takes effect on the next build.")}</div></span>
          <Select variant="field" value={String(settings.buildJobs ?? 0)} width={200} onChange={(v) => void save({ buildJobs: Number(v) })} options={jobs} />
        </div>
      </div>
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ 工作区

function WorkspaceSection() {
  const settings = useStore((s) => s.settings);
  const [root, setRoot] = useState<string>(settings.worktreeRoot ?? "");
  const [saved, setSaved] = useState(false);
  const { err, busy, save, setErr } = useSaver();
  useEffect(() => { setRoot(settings.worktreeRoot ?? ""); }, [settings.worktreeRoot]);
  const changed = root.trim() !== "" && root.trim() !== settings.worktreeRoot;
  const browse = async () => {
    const dir = await native.pickFolder(t("Choose the worktree folder"));
    if (dir) { setRoot(dir); setSaved(false); setErr(""); }
  };
  return (
    <Section id="workspaces" title="Workspaces"
             note={t("Each session works in its own git worktree under this folder, so your project folder stays untouched until you merge or apply.")}>
      <div className="set-card pad">
        <div className="path-row">
          <input className="input mono" value={root} spellCheck={false} onChange={(e) => { setRoot(e.target.value); setSaved(false); }}
                 onKeyDown={(e) => { if (e.key === "Enter" && changed) void save({ worktreeRoot: root }).then(setSaved); }} />
          <button className="btn sm" onClick={() => void browse()}><Icon name="folder" size={13} />{t("Browse…")}</button>
          <button className="btn sm primary" disabled={!changed || busy} onClick={() => void save({ worktreeRoot: root }).then(setSaved)}>{t("Save")}</button>
        </div>
        <div className="note">
          {saved ? <span className="ok-text"><Icon name="check" size={12} /> {t("Saved. New sessions use this folder; existing sessions stay where they are.")}</span>
            : t("Keep it short and without spaces: ESP-IDF cannot build in paths with spaces, and Windows limits path length.")}
        </div>
      </div>
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ 权限

type RuleKind = "deny" | "ask" | "allow";
const RULE_KINDS: { kind: RuleKind; label: string; hint: string }[] = [
  { kind: "deny", label: tk("Deny"), hint: tk("Never run. Wins over everything else.") },
  { kind: "ask", label: tk("Ask"), hint: tk("Always ask, even in Approve all.") },
  { kind: "allow", label: tk("Allow"), hint: tk("Run without asking.") },
];

function PermissionsSection() {
  const settings = useStore((s) => s.settings);
  const rules = settings.rules as Record<RuleKind, { config: string[]; settings: string[] }> | undefined;
  const [kind, setKind] = useState<RuleKind>("allow");
  const [text, setText] = useState("");
  const { err, busy, save } = useSaver();
  const mine = (k: RuleKind) => rules?.[k]?.settings ?? [];
  const write = (next: Record<RuleKind, string[]>) => save({ rules: next });
  const all = (): Record<RuleKind, string[]> => ({ allow: mine("allow"), ask: mine("ask"), deny: mine("deny") });
  const add = async () => {
    const r = text.trim();
    if (!r) return;
    const next = all();
    next[kind] = [...next[kind], r];
    if (await write(next)) setText("");
  };
  const remove = (k: RuleKind, r: string) => {
    const next = all();
    next[k] = next[k].filter((x) => x !== r);
    void write(next);
  };
  return (
    <Section id="permissions" title="Permissions"
             note={<>{tx("Rules are {a} or {b}, for example {c}, {d}, {e}, {f}.", {
               a: <span className="mono">Tool</span>, b: <span className="mono">Tool(pattern)</span>, c: <span className="mono">shell(idf.py build)</span>,
               d: <span className="mono">shell(git:*)</span>, e: <span className="mono">edit(main/*.c)</span>, f: <span className="mono">flash</span> })}
               {" "}{t("Dangerous hardware operations always ask, and eFuse writes never run, whatever the rules say.")}</>}>
      <div className="set-card">
        {RULE_KINDS.map(({ kind: k, label, hint }) => {
          const fromToml = rules?.[k]?.config ?? [];
          const list = mine(k);
          return (
            <div key={k} className="rule-group">
              <div className="rule-hd"><b>{t(label)}</b><span className="note">{t(hint)}</span></div>
              {fromToml.length + list.length === 0 ? <div className="empty">{t("No rules")}</div> : (
                <div className="rule-chips">
                  {fromToml.map((r) => <span key={`t${r}`} className="chip mono" title={t("Defined in config.toml (read-only here)")}>{r}<span className="src">config.toml</span></span>)}
                  {list.map((r) => (
                    <span key={r} className="chip mono">{r}
                      <button className="x" title={t("Remove")} disabled={busy} onClick={() => remove(k, r)}><Icon name="x" size={11} /></button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        <div className="rule-add">
          <Select variant="field" value={kind} width={110} onChange={(v) => setKind(v as RuleKind)}
                  options={RULE_KINDS.map((x) => ({ value: x.kind, label: t(x.label) }))} />
          <input className="input mono" placeholder="shell(idf.py build)" value={text} spellCheck={false}
                 onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void add(); }} />
          <button className="btn sm" disabled={!text.trim() || busy} onClick={() => void add()}><Icon name="plus" size={13} />{t("Add rule")}</button>
        </div>
      </div>
      <div className="note">{t("Changes apply right away, including to open sessions.")}</div>
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ MCP

interface McpDraft { name: string; command: string; args: string; env: string; enabled: boolean; isNew: boolean }
const NO_MCP: McpDraft = { name: "", command: "", args: "", env: "", enabled: true, isNew: true };

function McpSection() {
  const settings = useStore((s) => s.settings);
  const servers: Json[] = settings.mcp ?? [];
  const [draft, setDraft] = useState<McpDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const connected = useStore((s) => s.connected);
  const pending = dirty || servers.some((s) => s.pending);
  // 重启核心时（断开）：改动随重启生效，提示条收起
  useEffect(() => { if (!connected) setDirty(false); }, [connected]);
  // 服务器在后台连接：还没握手的，过一会儿再读一次状态（最多约 30 秒）
  const connecting = servers.some((s) => s.enabled && s.status?.alive && !s.status.server && !s.status.error);
  const [polls, setPolls] = useState(0);
  useEffect(() => {
    if (!connecting || polls >= 15) return;
    const timer = setTimeout(() => { setPolls((n) => n + 1); void refreshSettings().catch(() => undefined); }, 2000);
    return () => clearTimeout(timer);
  }, [connecting, polls, servers]);

  const submit = async () => {
    if (!draft) return;
    setBusy(true);
    setErr("");
    try {
      const env: Record<string, string> = {};
      for (const line of draft.env.split(/\r?\n/)) {
        const s = line.trim();
        if (!s) continue;
        const i = s.indexOf("=");
        if (i <= 0) throw new Error(t("Environment line \"{line}\" should be NAME=value", { line: s }));
        env[s.slice(0, i).trim()] = s.slice(i + 1);
      }
      await saveMcp(draft.name, { command: draft.command.trim(), args: draft.args.split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
                                  env, enabled: draft.enabled });
      setDraft(null);
      setDirty(true);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async (name: string) => {
    if (!window.confirm(t("Remove MCP server {name}?", { name }))) return;
    try {
      await deleteMcp(name);
      setDirty(true);
    } catch (e) {
      setErr(errText(e));
    }
  };
  const status = (s: Json) => {
    if (!s.enabled) return <span className="chip">{t("disabled")}</span>;
    if (s.pending || !s.status) return <span className="chip warn">{t("after restart")}</span>;
    if (s.status.alive && !s.status.server && !s.status.error) return <span className="chip info">{t("connecting…")}</span>;
    if (s.status.alive) return <span className="chip ok">{t(s.status.tools === 1 ? "{n} tool" : "{n} tools", { n: s.status.tools })}</span>;
    return <span className="chip bad" title={s.status.error ?? ""}>{s.status.error ? t("error") : t("not connected")}</span>;
  };

  return (
    <Section id="mcp" title="MCP servers" note={t("Extra tools from Model Context Protocol servers (stdio). Servers connect when the core starts.")}
             actions={!draft && <button className="btn sm" onClick={() => setDraft({ ...NO_MCP })}><Icon name="plus" size={14} />{t("Add server")}</button>}>
      {pending && (
        <div className="callout info">
          <Icon name="info" />
          <div className="restart-row"><span>{t("Server changes take effect after the core restarts.")}</span><RestartButton /></div>
        </div>
      )}
      {draft && (
        <div className="model-form">
          <div className="grid">
            <label className="field">
              <span>{t("Name")}</span>
              <input className="input mono" value={draft.name} disabled={!draft.isNew} placeholder="espressif-docs" spellCheck={false}
                     onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </label>
            <label className="field">
              <span>{t("Command")}</span>
              <input className="input mono" value={draft.command} placeholder="uvx / npx / C:\path\server.exe" spellCheck={false}
                     onChange={(e) => setDraft({ ...draft, command: e.target.value })} />
            </label>
            <label className="field">
              <span>{t("Arguments")} <span className="faint">{t("(one per line)")}</span></span>
              <textarea className="input mono" rows={3} value={draft.args} spellCheck={false} onChange={(e) => setDraft({ ...draft, args: e.target.value })} />
            </label>
            <label className="field">
              <span>{t("Environment")} <span className="faint">{t("(NAME=value per line)")}</span></span>
              <textarea className="input mono" rows={3} value={draft.env} spellCheck={false} onChange={(e) => setDraft({ ...draft, env: e.target.value })} />
            </label>
            <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />{t("Enabled")}</label>
          </div>
          <div className="form-acts">
            <span className="spacer" />
            <button className="btn sm ghost" onClick={() => { setDraft(null); setErr(""); }} disabled={busy}>{t("Cancel")}</button>
            <button className="btn sm primary" onClick={() => void submit()} disabled={busy || !draft.name.trim() || !draft.command.trim()}>
              <Icon name="check" size={13} />{draft.isNew ? t("Add server") : t("Save")}
            </button>
          </div>
        </div>
      )}
      <div className="model-list">
        {servers.length === 0 && <div className="empty">{t("No MCP servers. Firmwright's built-in tools cover building, flashing and the serial port.")}</div>}
        {servers.map((s) => {
          const readonly = s.source === "config.toml";
          return (
            <div key={s.name} className="model-row">
              <div className="main">
                <div className="name"><b>{s.name}</b>{status(s)}{readonly && <span className="chip">config.toml</span>}</div>
                <div className="sub mono ellipsis" title={[s.command, ...s.args].join(" ")}>{[s.command, ...s.args].join(" ")}</div>
                {s.status?.error && <div className="sub bad-text">{tc(s.status.error)}</div>}
              </div>
              {!readonly && (
                <div className="acts">
                  <button className="btn xs" disabled={!!draft} onClick={() => setDraft({ name: s.name, command: s.command, args: (s.args ?? []).join("\n"),
                    env: Object.entries(s.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"), enabled: s.enabled, isNew: false })}>
                    <Icon name="edit" size={12} />{t("Edit")}
                  </button>
                  <button className="btn xs danger" title={t("Remove")} onClick={() => void remove(s.name)}><Icon name="trash" size={12} /></button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
    </Section>
  );
}

// ------------------------------------------------------------------ 关于

function AboutSection() {
  const connected = useStore((s) => s.connected);
  const [about, setAbout] = useState<Json | null>(null);
  const [host, setHost] = useState<HostInfo | null>(null);
  useEffect(() => {
    if (!connected) return;
    void coreAbout().then(setAbout).catch(() => undefined);
    void native.hostInfo().then(setHost).catch(() => undefined);
  }, [connected]);
  const idf = about?.idf?.active;
  const rows: [string, string][] = [
    ["Firmwright", host ? `${host.appVersion}${host.packaged ? "" : ` ${t("(development)")}`}` : t("browser mode")],
    [t("Core"), about ? `${about.version} · Python ${about.python}` : "…"],
    ["ESP-IDF", idf ? `v${idf.version ?? "?"} · ${idf.path}` : (about?.idf?.error ? tc(about.idf.error) : t("not found"))],
    ...(host ? [["Electron", host.electron] as [string, string]] : []),
  ];
  const paths: [string, string | undefined][] = [
    [t("Data folder"), about?.paths?.data], [t("Sessions"), about?.paths?.sessions], [t("Worktrees"), about?.paths?.worktrees],
    [t("Logs"), host?.logDir], ["config.toml", about?.paths?.config],
  ];
  return (
    <Section id="about" title="About & data" actions={<RestartButton label={t("Restart core")} />}>
      <div className="set-card">
        <table className="kv">
          <tbody>
            {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td className="mono"><div className="kv-val"><span className="ellipsis" title={v}>{v}</span></div></td></tr>)}
            {paths.map(([k, v]) => (
              <tr key={k}>
                <th>{k}</th>
                <td className="mono">
                  <div className="kv-val">
                    <span className="ellipsis" title={v}>{v ?? "—"}</span>
                    {v && <button className="btn xs ghost" onClick={() => void native.openPath(v)}><Icon name="external" size={12} />{t("Open")}</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="note">{t("Sessions, memory and settings live in the data folder. API keys are not stored there: they are in the Windows Credential Manager.")}</div>
    </Section>
  );
}

/** 模型连通性测试结果（核心给的 warning / error 是英文，显示时 tc） */
function TestResult({ res }: { res: Json }) {
  return (
    <div className={`test-res ${res.ok ? (res.toolCalls ? "ok" : "warn") : "bad"}`}>
      <Icon name={res.ok && res.toolCalls ? "check" : "alert"} size={13} />
      {res.ok ? (res.toolCalls ? t("Connected, tool calling works · {ms} ms", { ms: res.latencyMs }) : tc(res.warning ?? ""))
        : t("Failed: {error}", { error: tc(String(res.error ?? "")) })}
    </div>
  );
}

function hostOf(url: string): string {
  try { return new URL(url).host; } catch { return url; }
}

export function ModelForm({ draft: initial, isNew = false, onDone }: { draft: Draft; isNew?: boolean; onDone: () => void }) {
  const [d, setD] = useState<Draft>(initial);
  const [preset, setPreset] = useState(isNew ? "deepseek" : "");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [test, setTest] = useState<Json | null>(null);
  const set = (patch: Partial<Draft>) => { setD({ ...d, ...patch }); setTest(null); };
  const p = PRESETS.find((x) => x.id === preset);

  useEffect(() => {
    if (!isNew || !p) return;
    setD((cur) => ({ ...cur, baseUrl: p.baseUrl, effortStyle: p.effortStyle, reasoning: p.reasoning }));
  }, [preset]); // eslint-disable-line react-hooks/exhaustive-deps

  const canSave = !!d.id.trim() && !!d.baseUrl.trim() && (!isNew || !!d.apiKey.trim()) && !busy;
  const save = async () => {
    setBusy(t("Saving…"));
    setErr("");
    try {
      await saveModel(d.id.trim(), toFields(d), d.apiKey.trim());
      onDone();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };
  const runTest = async () => {
    setBusy(t("Testing…"));
    setTest(null);
    try {
      setTest(await testModel(d.id.trim() || "draft", toFields(d), d.apiKey.trim()));
    } catch (e) {
      setTest({ ok: false, error: String(e instanceof Error ? e.message : e) });
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="model-form">
      <div className="grid">
        {isNew && (
          <label className="field wide">
            <span>{t("Provider")}</span>
            <Select variant="field" value={preset} onChange={setPreset} width={320}
                    options={PRESETS.map((x) => ({ value: x.id, label: t(x.label), hint: x.baseUrl || t("Enter the base URL yourself") }))} />
          </label>
        )}
        <label className="field">
          <span>{t("Name in Firmwright")}</span>
          <input className="input mono" value={d.id} disabled={!isNew} onChange={(e) => set({ id: e.target.value })} placeholder="deepseek-chat" spellCheck={false} />
        </label>
        <label className="field">
          <span>{t("Model name sent to the API")}</span>
          <input className="input mono" value={d.model} onChange={(e) => set({ model: e.target.value })} placeholder={p ? t(p.hint) : t("defaults to the name")} spellCheck={false} />
        </label>
        <label className="field wide">
          <span>Base URL</span>
          <input className="input mono" value={d.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} placeholder="https://…/v1" spellCheck={false} />
        </label>
        <label className="field wide">
          <span>{t("API key")}</span>
          <input className="input mono" type="password" value={d.apiKey} onChange={(e) => set({ apiKey: e.target.value })}
                 placeholder={isNew ? "sk-…" : t("Leave empty to keep the stored key")} autoComplete="off" spellCheck={false} />
          <span className="note">{t("Stored in the Windows Credential Manager as {ref}.", { ref: `firmwright/${d.id.trim() || t("<name>")}` })}</span>
        </label>
        <label className="field">
          <span>{t("Context window (tokens)")}</span>
          <input className="input mono" inputMode="numeric" value={d.contextWindow} onChange={(e) => set({ contextWindow: e.target.value.replace(/\D/g, "") })} />
        </label>
        <div className="field">
          <span>{t("Capabilities")}</span>
          <div className="checks">
            <label className="check"><input type="checkbox" checked={d.vision} onChange={(e) => set({ vision: e.target.checked })} />{t("Vision (images)")}</label>
            <label className="check"><input type="checkbox" checked={d.reasoning} onChange={(e) => set({ reasoning: e.target.checked })} />{t("Reasoning")}</label>
          </div>
        </div>
        {d.reasoning && (
          <label className="field wide">
            <span>{t("How the API controls thinking")}</span>
            <Select variant="field" value={d.effortStyle} onChange={(v) => set({ effortStyle: v as Effort })} width={320}
                    options={(Object.keys(EFFORT_TEXT) as Effort[]).map((k) => ({ value: k, label: t(EFFORT_TEXT[k]) }))} />
          </label>
        )}
      </div>
      {test && <TestResult res={test} />}
      {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
      <div className="form-acts">
        <span className="hint">{busy}</span>
        <span className="spacer" />
        <button className="btn sm ghost" onClick={onDone} disabled={!!busy}>{t("Cancel")}</button>
        <button className="btn sm" onClick={() => void runTest()} disabled={!!busy || !d.baseUrl.trim() || (isNew && !d.apiKey.trim())}>
          <Icon name="pulse" size={13} />{t("Test connection")}
        </button>
        <button className="btn sm primary" onClick={() => void save()} disabled={!canSave}>
          <Icon name="check" size={13} />{isNew ? t("Add model") : t("Save")}
        </button>
      </div>
    </div>
  );
}
