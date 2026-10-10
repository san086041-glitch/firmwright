/**
 * 界面语言（2026-10-10，docs/decisions/2026-10-10-ui-language-switch.md）：英文 / 中文，默认英文。
 *
 * - 源文字就是英文原文：t("New session")，中文表里查不到就原样显示英文
 * - 变量用 {name}：t("{n} boards", { n })
 * - 核心发来的文字（工具标题、审批理由、错误……）核心照旧发英文（很多也进模型上下文），
 *   在这里按"英文模板 → 中文模板"匹配后显示：tc(text)。切换语言立即生效，回放的历史也跟着变
 * - 选择存 localStorage（和外观一样，是每台机器的偏好）
 */
import { createElement, Fragment, type ReactNode } from "react";
import { create } from "zustand";
import { CORE_ZH } from "./zh-core";
import { ZH } from "./zh";

export type Lang = "en" | "zh";
const KEY = "fwr.lang";

function readLang(): Lang {
  try {
    return localStorage.getItem(KEY) === "zh" ? "zh" : "en";
  } catch {
    return "en";
  }
}

export const useLangStore = create<{ lang: Lang }>(() => ({ lang: readLang() }));

/** 组件里订阅语言：切换后重新渲染（memo 组件要自己调一次） */
export function useLang(): Lang {
  return useLangStore((s) => s.lang);
}

export function getLang(): Lang {
  return useLangStore.getState().lang;
}

export function setLang(lang: Lang): void {
  try {
    localStorage.setItem(KEY, lang);
  } catch { /* 存不了就只在这次生效 */ }
  if (typeof document !== "undefined") document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  useLangStore.setState({ lang });
}

/** 日期时间格式用的 locale */
export function locale(): string {
  return getLang() === "zh" ? "zh-CN" : "en-US";
}

type Vars = Record<string, string | number | null | undefined>;

function fill(s: string, vars?: Vars): string {
  if (!vars) return s;
  return s.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k] ?? "") : m));
}

/** 只做标记：常量表里的英文原文（渲染时再 t()），让键值检查能找到它 */
export const tk = (src: string): string => src;

/** 界面自己的文字 */
export function t(src: string, vars?: Vars): string {
  if (getLang() === "zh") {
    const zh = ZH[src];
    if (zh !== undefined) return fill(zh, vars);
  }
  return fill(src, vars);
}

/** 带格式的句子：变量可以是元素（<b>、<code>……），整句一起翻译，语序由译文决定 */
export function tx(src: string, vars: Record<string, ReactNode>): ReactNode[] {
  const tmpl = getLang() === "zh" ? ZH[src] ?? src : src;
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of tmpl.matchAll(/\{(\w+)\}/g)) {
    if (m.index! > last) out.push(tmpl.slice(last, m.index));
    out.push(m[1] in vars ? createElement(Fragment, { key: `v${m.index}` }, vars[m[1]]) : m[0]);
    last = m.index! + m[0].length;
  }
  if (last < tmpl.length) out.push(tmpl.slice(last));
  return out;
}

// ---- 核心文字：模板匹配

interface Compiled { re: RegExp; names: string[]; zh: string; weight: number }

let compiled: Compiled[] | null = null;

function compile(): Compiled[] {
  const out: Compiled[] = [];
  for (const [en, zh] of CORE_ZH) {
    const names: string[] = [];
    let pattern = "";
    let literal = 0;
    let last = 0;
    for (const m of en.matchAll(/\{(~?\w+)\}/g)) {
      const lit = en.slice(last, m.index);
      pattern += lit.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "([\\s\\S]*?)";
      literal += lit.length;
      names.push(m[1]);
      last = (m.index ?? 0) + m[0].length;
    }
    const tail = en.slice(last);
    pattern += tail.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    literal += tail.length;
    out.push({ re: new RegExp(`^${pattern}$`), names, zh, weight: literal });
  }
  // 字面部分越长越具体，先试
  return out.sort((a, b) => b.weight - a.weight);
}

function translateCore(text: string, depth: number): string {
  if (!text || depth > 3) return text;
  const exact = ZH[text];
  if (exact !== undefined) return exact;
  compiled ??= compile();
  for (const c of compiled) {
    const m = c.re.exec(text);
    if (!m) continue;
    const vars: Record<string, string> = {};
    c.names.forEach((n, i) => {
      // {~name}：这个位置的内容本身也是核心文字，再翻一次
      vars[n.replace(/^~/, "")] = n.startsWith("~") ? translateCore(m[i + 1], depth + 1) : m[i + 1];
    });
    return c.zh.replace(/\{~?(\w+)\}/g, (s, k: string) => (k in vars ? vars[k] : s));
  }
  if (text.includes("\n")) {
    const lines = text.split("\n");
    if (lines.length > 1) return lines.map((l) => translateCore(l, depth + 1)).join("\n");
  }
  return text;
}

/** 核心发来的、或者存进时间线的动态文字：中文时按模板翻译，匹配不上原样显示 */
export function tc(text: string | null | undefined): string {
  if (text == null) return "";
  if (getLang() !== "zh") return text;
  return translateCore(text, 0);
}

/** 测试用：列出所有模板（检查格式） */
export const _coreTemplates = (): readonly (readonly [string, string])[] => CORE_ZH;
