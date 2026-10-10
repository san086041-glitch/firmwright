import { useEffect, useMemo, useRef, useState } from "react";
import { bindBoard, initGit, inspectProject, newSession, sendPrompt } from "../actions";
import { t, tc, tx } from "../i18n";
import { native, RpcError, type Json } from "../rpc";
import { updateView, useStore } from "../store";
import { EffortSelect, MODES } from "./Chat";
import { Icon } from "./Icon";
import { Select } from "./Select";
import { baseName } from "./util";

/** 新建会话页（2026-10-05 重做，参考 Codex 桌面版 / Claude Desktop 的 Code 标签页）：
 *  选工程 → 下面立刻显示检查结果（是不是工程、git 状态、未提交改动、副本在哪）→ 直接写第一个任务，回车开始。
 *  原来的弹窗要填 6 项，不是 git 仓库时还要再弹一个对话框；真机实测里用户选了工程的上一级、
 *  工程还没拷进来就初始化了 git，界面都没有提示。 */
export function NewSessionPage() {
  const models = useStore((s) => s.models);
  const defaultModel = useStore((s) => s.defaultModel);
  const boards = useStore((s) => s.boards);
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const sessions = useStore((s) => s.sessions);
  const platform = useStore((s) => s.coreInfo?._meta?.fwr?.platform ?? null);
  const recent = useMemo(() => [...new Set([...sessions].sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0))
    .map((x) => x.project_root))].slice(0, 6), [sessions]);
  const [cwd, setCwd] = useState(recent[0] ?? "");
  const freeBoard = Object.values(boards).find((b) => !b.owner_session && b.state !== "disconnected");
  const [boardId, setBoardId] = useState(freeBoard?.id ?? "");
  const [modelId, setModelId] = useState(defaultModel ?? models[0]?.id ?? "");
  // 设置页的"新会话默认权限模式"（2026-10-06）
  const [mode, setModeVal] = useState<string>(() => useStore.getState().settings?.permissionMode ?? "default");
  const [effort, setEffortVal] = useState("default");
  const [text, setText] = useState("");
  const [carry, setCarry] = useState(true);
  const [inPlace, setInPlace] = useState(false);
  const [newProject, setNewProject] = useState(false);  // 空文件夹：用户明确选了"在这里新建工程"
  const [info, setInfo] = useState<Json | null>(null);
  const [checking, setChecking] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const ta = useRef<HTMLTextAreaElement>(null);
  const close = () => useStore.setState({ showNewSession: false });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && close();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy]);

  // 文件夹一变就检查（防抖 300 ms；旧的结果晚到就丢掉）
  useEffect(() => {
    const path = cwd.trim();
    setInfo(null);
    setInPlace(false);
    setNewProject(false);
    if (!path) return;
    let stale = false;
    setChecking(true);
    const timer = setTimeout(() => {
      inspectProject(path).then((r) => { if (!stale) setInfo(r); })
        .catch((e) => { if (!stale) setInfo({ exists: false, error: String(e instanceof Error ? e.message : e) }); })
        .finally(() => { if (!stale) setChecking(false); });
    }, 300);
    return () => { stale = true; clearTimeout(timer); };
  }, [cwd]);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const repo = info?.repo;
  const worktree = !!info?.worktreeEnabled && !inPlace;
  const needsInit = worktree && repo && (!repo.is_git || !repo.head);
  const empty = needsInit && info?.scan && info.scan.files === 0;
  const canStart = !!cwd.trim() && !!modelId && !busy && !checking && info?.exists && (!empty || newProject);

  const start = async () => {
    if (!canStart) return;
    const path = cwd.trim();
    const task = text.trim();
    setErr("");
    try {
      if (needsInit) {
        setBusy(repo?.is_git ? t("Committing your files…") : t("Initializing git…"));
        await initGit(path, !!empty && newProject);
      }
      setBusy(t("Creating the isolated copy…"));
      // 别的会话占着的板子：先建会话（不带板子），再移过来；移不了（那个会话刚开始执行）也不影响新会话本身
      const sid = await newSession({ cwd: path, boardId: pickedOwner ? null : boardId || null, modelId, mode, carryDirty: carry, effort,
                                     isolation: inPlace ? "in_place" : undefined, title: titleFrom(task) });
      if (pickedOwner && boardId) {
        try {
          await bindBoard(sid, boardId, true);
        } catch (e) {
          const text = `The board could not be moved to this session: ${e instanceof Error ? e.message : String(e)}`; // 存英文，显示时 tc()
          updateView(sid, (v) => ({ ...v, items: [...v.items, { kind: "notice", id: `mv${Date.now()}`, tone: "warn", text }] }));
        }
      }
      if (task) void sendPrompt(sid, task);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
      if (e instanceof RpcError) inspectProject(path).then(setInfo).catch(() => {});
    } finally {
      setBusy("");
    }
  };

  const sessionTitle = (id: string) => sessions.find((x) => x.id === id)?.title ?? id;
  // 2026-10-06（界面改进第 6 项）：全部板子都列出来。别的会话占着的也能选（新会话建好后移过来）；
  // 那个会话正在执行时不能移（核心也会拒绝），离线的不能选
  const views = useStore((s) => s.views);
  const boardOptions = Object.values(boards)
    .sort((a, b) => Number(!!a.owner_session) - Number(!!b.owner_session) || a.alias.localeCompare(b.alias))
    .map((b) => {
      const owner = b.owner_session;
      const ownerBusy = !!owner && (views[owner]?.status ?? "idle") !== "idle";
      return {
        value: b.id, label: b.alias,
        badge: b.state === "disconnected" ? t("offline") : owner ? t("in use") : b.chip ?? undefined,
        disabled: b.state === "disconnected" || ownerBusy,
        hint: b.state === "disconnected" ? t("Offline: plug it in to use it")
          : owner ? (ownerBusy ? t("In use by “{title}”, which is working right now", { title: sessionTitle(owner) })
                               : t("In use by “{title}”; it will be moved to the new session", { title: sessionTitle(owner) }))
          : [b.chip, b.port].filter(Boolean).join(" · ") || undefined,
      };
    });
  const pickedOwner = boardId ? boards[boardId]?.owner_session : null;

  return (
    <div className="start">
      <div className="start-inner">
        <div className="start-head">
          <h2>{t("New session")}</h2>
          <span className="spacer" />
          <button className="btn ghost sm" onClick={close} disabled={!!busy}>{t("Cancel")} <span className="kbd">Esc</span></button>
        </div>

        <section className="start-project">
          <div className="start-label">{t("Project folder")}</div>
          <div className="row">
            <input className="input mono" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="D:\projects\blink"
                   spellCheck={false} autoFocus={!cwd} />
            <button type="button" className="btn" onClick={async () => { const p = await native.pickFolder(); if (p) setCwd(p); }}>
              <Icon name="folder" size={14} />{t("Browse…")}
            </button>
          </div>
          {recent.length > 0 && (
            <div className="recent-chips">
              {recent.map((r) => (
                <button key={r} className={`chip ${r === cwd.trim() ? "ok" : ""}`} title={r} onClick={() => setCwd(r)}>
                  <Icon name="folder" size={12} />{baseName(r)}
                </button>
              ))}
            </div>
          )}
          {cwd.trim() && (
            <ProjectCheck info={info} checking={checking} platform={platform} worktree={worktree} carry={carry}
                          setCarry={setCarry} inPlace={inPlace} setInPlace={setInPlace} pickFolder={setCwd}
                          newProject={newProject} setNewProject={setNewProject} />
          )}
        </section>

        <div className="composer start-composer">
          <div className="composer-card">
            <textarea
              ref={ta}
              value={text}
              rows={2}
              autoFocus={!!cwd}
              placeholder={newProject && empty
                ? t("Describe the project to create, e.g. “An ESP32-S3 project that blinks the LED on GPIO 2 and prints TEST:blink:PASS”")
                : t("What should the agent do first? Optional; you can also start empty.  (Enter to start, Shift+Enter for a new line)")}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void start();
                }
              }}
            />
            <div className="composer-row">
              {devicesAvailable && (
                <Select value={boardId} onChange={setBoardId} width={280} icon={<Icon name="usb" size={13} />}
                        title={t("Board bound to the session (one session per board)")}
                        options={[{ value: "", label: t("No board") }, ...boardOptions]} />
              )}
              <span className="hint">{busy}</span>
              <Select value={mode} onChange={setModeVal} width={300} icon={<Icon name="shield" size={13} />}
                      options={MODES.map(([k, v, h]) => ({ value: k, label: t(v), hint: t(h) }))} />
              <Select value={modelId} onChange={(v) => { setModelId(v); setEffortVal("default"); }} width={220} icon={<Icon name="sparkles" size={13} />}
                      options={models.map((m) => ({ value: m.id, label: m.id, badge: m.vision ? t("vision") : undefined }))} />
              <EffortSelect modelId={modelId} value={effort} onChange={setEffortVal} />
              <button className="btn primary sm" onClick={() => void start()} disabled={!canStart}>
                {busy ? <Icon name="refresh" size={14} className="spin" /> : <Icon name="send" size={14} />}
                {text.trim() ? t("Start") : t("Start empty")}
              </button>
            </div>
          </div>
        </div>
        {models.length === 0 && (
          <div className="callout error">
            <Icon name="alert" /><div style={{ flex: 1 }}>{t("No models configured yet.")}</div>
            <button className="btn sm" onClick={() => useStore.setState({ showSettings: true, showNewSession: false })}>{t("Add a model")}</button>
          </div>
        )}
        {err && <div className="callout error"><Icon name="alert" /><div>{tc(err)}</div></div>}
      </div>
    </div>
  );
}

function ProjectCheck({ info, checking, platform, worktree, carry, setCarry, inPlace, setInPlace, pickFolder,
                        newProject, setNewProject }: {
  info: Json | null; checking: boolean; platform: string | null; worktree: boolean; carry: boolean;
  setCarry: (v: boolean) => void; inPlace: boolean; setInPlace: (v: boolean) => void; pickFolder: (p: string) => void;
  newProject: boolean; setNewProject: (v: boolean) => void;
}) {
  const emptyFolder = !!info?.exists && (!info.repo?.is_git || !info.repo?.head) && info.scan?.files === 0;
  if (checking || !info) return <div className="check-list"><Line icon="refresh" spin>{t("Checking the folder…")}</Line></div>;
  if (!info.exists) return <div className="check-list"><Line icon="alert" tone="bad">{info.error ? tc(info.error) : t("This folder does not exist.")}</Line></div>;
  const repo = info.repo ?? {};
  const scan = info.scan;
  const proj = info.project;
  const cands: string[] = info.candidates ?? [];
  const n = repo.dirty_count ?? 0;

  return (
    <div className="check-list">
      {platform && !emptyFolder && (proj ? (
        <Line icon="check" tone="ok">{tx("ESP-IDF project {name}", { name: <b>{proj.name ?? baseName(proj.root)}</b> })}{proj.target ? <> · {t("target {chip}", { chip: proj.target })}</> : null}</Line>
      ) : cands.length > 0 ? (
        <Line icon="alert" tone="warn">
          {cands.length === 1 ? t("This folder is not an ESP-IDF project, but one was found inside it:") : t("This folder is not an ESP-IDF project, but some were found inside it:")}
          <span className="cands">
            {cands.map((c) => <button key={c} className="btn xs" onClick={() => pickFolder(c)}>{t("Use {path}", { path: c.slice(repoRel(c, info)) })}</button>)}
          </span>
        </Line>
      ) : (
        <Line icon="alert" tone="warn">{t("No ESP-IDF project here (no CMakeLists.txt that includes project.cmake). You can still start, but build and flash will not work.")}</Line>
      ))}

      {/* 路径里有空格：ESP-IDF 编不了（真机实测：编译到链接阶段才失败，agent 还想用 subst 绕过） */}
      {(() => {
        const bad: string | null = (worktree ? info.spaces?.worktree : info.spaces?.inPlace) ?? null;
        return bad ? (
          <Line icon="alert" tone="bad">
            {tx("The folder {name} has a space in its name. ESP-IDF cannot build in paths with spaces (some components pass the path to the linker unquoted, so linking fails). Rename it, e.g. to {fixed}, before starting.",
                { name: <b>“{bad}”</b>, fixed: <code>{bad.replace(/\s+/g, "_")}</code> })}
          </Line>
        ) : null;
      })()}

      {inPlace || !info.worktreeEnabled ? (
        <Line icon="alert" tone="warn">
          {repo.head ? t("The agent will edit this folder directly, with no isolated copy.") : t("The agent will edit this folder directly, with no isolated copy and no checkpoints.")}
          {info.worktreeEnabled && <button className="link" onClick={() => setInPlace(false)}>{t("Use an isolated copy")}</button>}
        </Line>
      ) : !repo.is_git || !repo.head ? (
        scan?.files === 0 ? (
          // 空文件夹（2026-10-05）：要么工程还没拷进来，要么是想从零新建——让用户明确选
          newProject ? (
            <Line icon="sparkles" tone="ok">
              <b>{t("New project.")}</b> {t("The agent creates the project in an isolated copy, builds and flashes it there; when you finish, “Apply to project folder” writes it into this folder.")}
              <button className="link" onClick={() => setNewProject(false)}>{t("Undo")}</button>
            </Line>
          ) : (
            <Line icon="alert" tone="warn">
              {t("This folder is empty. If your project is still being copied in, wait and check again; the session starts from what is in the folder now.")}
              <span className="cands"><button className="btn xs" onClick={() => setNewProject(true)}>{t("Start a new project here")}</button></span>
            </Line>
          )
        ) : (
          // 文件夹在一个还没有提交的上层仓库里：第一个提交会包含整个上层仓库，用警告色提醒
          <Line icon={repo.subdir ? "alert" : "info"} tone={repo.subdir ? "warn" : "info"}>
            {repo.is_git ? tx(repo.subdir ? "This folder is inside another repository, {root}, which has no commits yet." : "The repository {root} has no commits yet.", { root: <code>{repo.repo_root}</code> })
              : t("Not a git repository yet.")}
            {" "}{tx(repo.is_git ? "Starting will commit {files} ({size}) as the first commit, so the session has an isolated copy and checkpoints. Build output is ignored."
                                 : "Starting will run git init and commit {files} ({size}) as the first commit, so the session has an isolated copy and checkpoints. Build output is ignored.",
                     { files: <b>{t("{n} files", { n: `${scan?.files?.toLocaleString()}${scan?.truncated ? "+" : ""}` })}</b>, size: fmtBytes(scan?.bytes ?? 0) })}
            <button className="link" onClick={() => setInPlace(true)}>{t("Work in place instead")}</button>
          </Line>
        )
      ) : (
        <>
          <Line icon="branch" tone="ok">
            git · {repo.branch ?? t("detached HEAD")} · {n === 0 ? t("no uncommitted changes") : t(n === 1 ? "{n} uncommitted change" : "{n} uncommitted changes", { n })}
            {repo.subdir && <span className="faint"> · {t("repository root {root}", { root: repo.repo_root })}</span>}
          </Line>
          {n > 0 && (
            <label className="check-line toggle" title={(repo.dirty ?? []).join("\n")}>
              <input type="checkbox" checked={carry} onChange={(e) => setCarry(e.target.checked)} />
              <span>{t("Include my uncommitted changes in the session ({files})", { files: `${(repo.dirty ?? []).slice(0, 3).join(", ")}${n > 3 ? ", …" : ""}` })}</span>
            </label>
          )}
        </>
      )}

      {worktree && (
        <Line icon="shield" tone="">
          {tx(repo.head && n > 0 && !carry ? "The agent works in an isolated copy at {path} made from the last commit." : "The agent works in an isolated copy at {path}.", { path: <code>{info.worktreeRoot}\…</code> })}
          {" "}{t("This folder is not touched until you finish the session and apply or merge the changes.")}
        </Line>
      )}
    </div>
  );
}

function Line({ icon, tone = "", spin, children }: { icon: string; tone?: string; spin?: boolean; children: React.ReactNode }) {
  return (
    <div className={`check-line ${tone}`}>
      <Icon name={icon as "check"} size={14} className={spin ? "spin" : ""} />
      <div>{children}</div>
    </div>
  );
}

/** 候选工程相对当前文件夹的路径（按钮上只显示子目录部分） */
function repoRel(candidate: string, info: Json): number {
  const base = String(info.repo?.project ?? "");
  return candidate.toLowerCase().startsWith(base.toLowerCase()) ? base.length + 1 : 0;
}

function titleFrom(task: string): string | undefined {
  const line = task.split("\n")[0].trim();
  if (!line) return undefined;
  return line.length > 60 ? `${line.slice(0, 57)}…` : line;
}

function fmtBytes(n: number): string {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}
