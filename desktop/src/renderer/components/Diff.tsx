/** diff 视图（2026-10-05）：并排 / 统一两种显示、行号、语法高亮、行内改动标记、文件列表。
 *  不用 Monaco（几 MB、要配 worker）：自己解析 diff（diffparse.ts）+ highlight.js 核心和这个项目用得到的几种语言。 */
import hljs from "highlight.js/lib/core";
import { t, useLang } from "../i18n";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cmake from "highlight.js/lib/languages/cmake";
import cpp from "highlight.js/lib/languages/cpp";
import ini from "highlight.js/lib/languages/ini";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import yaml from "highlight.js/lib/languages/yaml";
import { memo, useMemo, useRef, useState } from "react";
import { changedSpan, parseDiff, toSplit, type DiffFile, type DiffLine } from "../diffparse";
import { Icon } from "./Icon";

hljs.registerLanguage("c", c);
hljs.registerLanguage("cpp", cpp);
hljs.registerLanguage("python", python);
hljs.registerLanguage("cmake", cmake);
hljs.registerLanguage("ini", ini);
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("markdown", markdown);

function languageOf(path: string): string | null {
  const name = path.split(/[\\/]/).pop()?.toLowerCase() ?? "";
  if (name === "cmakelists.txt" || name.endsWith(".cmake")) return "cmake";
  if (name.startsWith("sdkconfig") || /\.(ini|toml|cfg|conf|csv)$/.test(name)) return "ini";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  return ({ c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", ino: "cpp", py: "python",
            json: "json", sh: "bash", bash: "bash", yml: "yaml", yaml: "yaml", md: "markdown" } as Record<string, string>)[ext] ?? null;
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function hl(text: string, lang: string | null): string {
  if (!lang || !text) return esc(text);
  try {
    return hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
  } catch {
    return esc(text);
  }
}

/** 一行代码的 HTML：有配对行时把改动的部分包进 <mark>（三段分别高亮） */
function lineHtml(text: string, lang: string | null, span?: [number, number]): string {
  if (!span || (span[0] === 0 && span[1] === text.length) || span[0] >= span[1]) return hl(text, lang) || " ";
  return hl(text.slice(0, span[0]), lang) + `<mark>${hl(text.slice(span[0], span[1]), lang)}</mark>` + hl(text.slice(span[1]), lang);
}

const BIG = 600; // 超过这么多行的文件默认折叠

function readMode(): "split" | "unified" {
  try {
    return localStorage.getItem("fwr.diffMode") === "unified" ? "unified" : "split";
  } catch {
    return "split";
  }
}

/** 完整的 diff 查看器（收尾对话框）：文件列表 + 并排 / 统一切换 */
export function DiffViewer({ diff }: { diff: string }) {
  const files = useMemo(() => parseDiff(diff), [diff]);
  const [mode, setMode] = useState<"split" | "unified">(readMode);
  const body = useRef<HTMLDivElement>(null);
  const pick = (m: "split" | "unified") => {
    setMode(m);
    try { localStorage.setItem("fwr.diffMode", m); } catch { /* 隐私模式等：只是不记住 */ }
  };
  const jump = (i: number) => body.current?.querySelector(`[data-file="${i}"]`)?.scrollIntoView({ block: "start" });
  if (files.length === 0) return <div className="empty">{t("No textual changes.")}</div>;
  return (
    <div className="dv">
      <div className="dv-side">
        {files.map((f, i) => (
          <button key={i} className="dv-file-link" onClick={() => jump(i)} title={f.oldPath ? `${f.oldPath} → ${f.path}` : f.path}>
            <span className={`st ${f.status}`}>{STATUS_MARK[f.status]}</span>
            <span className="p">{f.path.split("/").pop()}<span className="dir">{dirOf(f.path)}</span></span>
            <span className="n"><span className="a">+{f.added}</span> <span className="d">−{f.deleted}</span></span>
          </button>
        ))}
      </div>
      <div className="dv-main">
        <div className="dv-bar">
          <span className="seg">
            <button className={mode === "split" ? "on" : ""} onClick={() => pick("split")}>{t("Side by side")}</button>
            <button className={mode === "unified" ? "on" : ""} onClick={() => pick("unified")}>{t("Unified")}</button>
          </span>
        </div>
        <div className="dv-body" ref={body}>
          {files.map((f, i) => <FileDiff key={`${i}:${f.path}`} f={f} idx={i} mode={mode} />)}
        </div>
      </div>
    </div>
  );
}

/** 工具卡片 / 审批预览里的小 diff：统一显示、没有文件列表 */
export function DiffInline({ diff }: { diff: string }) {
  const files = useMemo(() => parseDiff(diff), [diff]);
  if (files.length === 0) return <pre className="out">{diff}</pre>;
  return <div className="dv inline">{files.map((f, i) => <FileDiff key={i} f={f} idx={i} mode="unified" compact />)}</div>;
}

const STATUS_MARK: Record<DiffFile["status"], string> = { modified: "M", added: "A", deleted: "D", renamed: "R", binary: "B" };

function dirOf(p: string): string {
  const i = p.lastIndexOf("/");
  return i > 0 ? ` ${p.slice(0, i)}` : "";
}

const FileDiff = memo(function FileDiff({ f, idx, mode, compact = false }: { f: DiffFile; idx: number; mode: "split" | "unified"; compact?: boolean }) {
  useLang();
  const total = f.hunks.reduce((n, h) => n + h.lines.length, 0);
  const [open, setOpen] = useState(total <= BIG);
  const lang = languageOf(f.path);
  return (
    <section className="dv-file" data-file={idx}>
      <button className="dv-file-hd" onClick={() => setOpen(!open)}>
        <Icon name="chevron" size={12} className="chev" style={{ transform: open ? "rotate(90deg)" : "none" }} />
        <span className={`st ${f.status}`}>{STATUS_MARK[f.status]}</span>
        <span className="p mono">{f.oldPath && f.oldPath !== f.path ? `${f.oldPath} → ` : ""}{f.path || "(file)"}</span>
        {!compact && <span className="n"><span className="a">+{f.added}</span> <span className="d">−{f.deleted}</span></span>}
      </button>
      {open ? (
        f.status === "binary" ? <div className="dv-note">{t("Binary file; not shown.")}</div>
          : f.hunks.map((h, hi) => (
            <div key={hi} className="dv-hunk">
              {h.header && <div className="dv-hh mono">{h.header}</div>}
              {mode === "split" ? <SplitHunk lines={h.lines} lang={lang} /> : <UnifiedHunk lines={h.lines} lang={lang} />}
            </div>
          ))
      ) : (
        <button className="dv-note link-like" onClick={() => setOpen(true)}>{t("{n} lines changed; click to show", { n: total })}</button>
      )}
    </section>
  );
});

/** 统一显示：配对的删除 / 新增（按出现顺序）也标出行内改动 */
function UnifiedHunk({ lines, lang }: { lines: DiffLine[]; lang: string | null }) {
  const spans = useMemo(() => pairSpans(lines), [lines]);
  return (
    <table className="dv-t unified">
      <tbody>
        {lines.map((l, i) => (
          <tr key={i} className={l.kind}>
            <td className="no">{l.old ?? ""}</td>
            <td className="no">{l.new ?? ""}</td>
            <td className="sg">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</td>
            {/* hljs 的输出已经转义过；lineHtml 对没有语言的行也做了转义 */}
            <td className="code mono" dangerouslySetInnerHTML={{ __html: l.kind === "note" ? esc(l.text) : lineHtml(l.text, lang, spans.get(i)) }} />
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SplitHunk({ lines, lang }: { lines: DiffLine[]; lang: string | null }) {
  const rows = useMemo(() => toSplit({ header: "", lines }), [lines]);
  return (
    <table className="dv-t split">
      <colgroup><col className="no" /><col /><col className="no" /><col /></colgroup>
      <tbody>
        {rows.map((r, i) => {
          const paired = r.left?.kind === "del" && r.right?.kind === "add";
          const span = paired ? changedSpan(r.left!.text, r.right!.text) : null;
          if (r.left?.kind === "note") return <tr key={i} className="note"><td /><td className="code" colSpan={3}>{r.left.text}</td></tr>;
          return (
            <tr key={i}>
              <td className={`no ${r.left?.kind ?? "empty"}`}>{r.left?.old ?? ""}</td>
              <td className={`code mono ${r.left?.kind ?? "empty"}`}
                  dangerouslySetInnerHTML={{ __html: r.left ? lineHtml(r.left.text, lang, span?.a) : "" }} />
              <td className={`no ${r.right?.kind ?? "empty"}`}>{r.right?.new ?? ""}</td>
              <td className={`code mono ${r.right?.kind ?? "empty"}`}
                  dangerouslySetInnerHTML={{ __html: r.right ? lineHtml(r.right.text, lang, span?.b) : "" }} />
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 统一显示里给配对的删除 / 新增行算行内改动范围：一段连续的删除后面紧跟一段新增时，按顺序一一配对 */
function pairSpans(lines: DiffLine[]): Map<number, [number, number]> {
  const out = new Map<number, [number, number]>();
  for (let i = 0; i < lines.length;) {
    if (lines[i].kind !== "del") { i++; continue; }
    const ds = i;
    while (i < lines.length && lines[i].kind === "del") i++;
    const as = i;
    while (i < lines.length && lines[i].kind === "add") i++;
    const n = Math.min(as - ds, i - as);
    for (let k = 0; k < n; k++) {
      const s = changedSpan(lines[ds + k].text, lines[as + k].text);
      out.set(ds + k, s.a);
      out.set(as + k, s.b);
    }
  }
  return out;
}
