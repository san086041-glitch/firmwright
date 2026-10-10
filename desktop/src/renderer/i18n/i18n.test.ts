/// <reference types="vite/client" />
import { afterEach, describe, expect, it } from "vitest";
import { _coreTemplates, getLang, setLang, t, tc, tk } from ".";
import { ZH } from "./zh";

// 界面源码原文（Vite 的 ?raw），用来核对 key 有没有漏翻
const SOURCES = import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.ts", "!./**"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/** t( / tk( / tx( 第一个参数里的字符串字面量（和 scratch 里的提取脚本同一个规则） */
function usedKeys(): Map<string, string> {
  const out = new Map<string, string>();
  for (const [file, src] of Object.entries(SOURCES)) {
    for (const m of src.matchAll(/(?<![\w.])(?:t|tk|tx)\(/g)) {
      let depth = 0, j = m.index! + m[0].length, q = "";
      const start = j;
      for (; j < src.length; j++) {
        const c = src[j];
        if (q) {
          if (c === "\\") j++;
          else if (c === q) q = "";
        } else if (c === '"' || c === "'" || c === "`") q = c;
        else if ("([{".includes(c)) depth++;
        else if (")]}".includes(c)) { if (depth-- === 0) break; }
        else if (c === "," && depth === 0) break;
      }
      for (const s of src.slice(start, j).matchAll(/"((?:[^"\\]|\\.)*)"/g)) {
        const key = JSON.parse(`"${s[1]}"`) as string;
        if (/[A-Za-z]/.test(key)) out.set(key, file);
      }
    }
  }
  return out;
}

const vars = (s: string) => [...s.matchAll(/\{~?(\w+)\}/g)].map((m) => m[1]).sort();

afterEach(() => setLang("en"));

describe("中文表", () => {
  it("源码里用到的每个 key 都有中文", () => {
    const keys = usedKeys();
    expect(keys.size).toBeGreaterThan(500);
    const missing = [...keys].filter(([k]) => ZH[k] === undefined).map(([k, f]) => `${f}: ${k}`);
    expect(missing).toEqual([]);
  });

  it("译文保留原文的全部变量", () => {
    const bad = Object.entries(ZH).filter(([en, zh]) => vars(en).join() !== vars(zh).join());
    expect(bad).toEqual([]);
  });

  it("核心模板：变量一致、没有重复", () => {
    const seen = new Set<string>();
    for (const [en, zh] of _coreTemplates()) {
      expect(vars(zh), en).toEqual(vars(en));
      expect(zh, en).not.toMatch(/\{~/);  // ~ 只写在英文一侧
      expect(seen.has(en), en).toBe(false);
      seen.add(en);
    }
  });
});

describe("t / tc", () => {
  it("默认英文，原样输出并填变量", () => {
    expect(getLang()).toBe("en");
    expect(t("{n} boards", { n: 2 })).toBe("2 boards");
    expect(tc("Not executed: The user denied this call")).toBe("Not executed: The user denied this call");
    expect(tk("Deny")).toBe("Deny");
  });

  it("切到中文：界面文字查表，查不到显示英文", () => {
    setLang("zh");
    expect(t("{n} boards", { n: 2 })).toBe("2 块开发板");
    expect(t("Not a real key {x}", { x: 1 })).toBe("Not a real key 1");
  });

  it("核心文字按模板翻译，{~x} 递归翻译", () => {
    setLang("zh");
    expect(tc("Not executed: The user denied this call")).toBe("未执行：用户拒绝了这次调用");
    expect(tc("Dangerous: A full chip erase loses all data, including NVS")).toBe("危险：整片擦除会丢失全部数据，包括 NVS");
    expect(tc("Allow once")).toBe("允许一次");
    expect(tc("Deny")).toBe("拒绝");  // 和界面表共用
    expect(tc("Flashing app → COM5")).toBe("正在烧录 app → COM5");
    expect(tc("Writing at 0x00010000 · 42%")).toBe("写入 0x00010000 · 42%");
    expect(tc("CPU exception LoadProhibited (core 0), address 0x00000000 (near 0, most likely a NULL pointer), PC=0x42001234"))
      .toBe("CPU 异常 LoadProhibited（核 0），地址 0x00000000（接近 0，很可能是空指针），PC=0x42001234");
    expect(tc("Loading models failed: Error: boom")).toBe("加载模型失败：错误：boom");
  });

  it("时间线里存的英文提示：带 / 不带未提交改动、清理问题", () => {
    setLang("zh");
    const base = "The agent works in a copy of your project at C:\\fwr\\wt\\a (branch fwr/a, from main";
    const tail = "Your project folder is not touched until you finish: apply the changes to it, merge them as a commit, or discard.";
    expect(tc(`${base}, plus your 2 uncommitted changes). ${tail}`)).toContain("并带上了你的 2 处未提交改动");
    expect(tc(`${base}). ${tail}`)).toMatch(/^agent 在你工程的副本 C:\\fwr\\wt\\a 中工作（分支 fwr\/a，基于 main）/);
    expect(tc("Session merged (abcd1234); the worktree was removed. Not fully cleaned up: branch busy"))
      .toBe("会话已合并（abcd1234）；worktree 已删除。没有完全清理：branch busy");
  });

  it("匹配不上的多行文字逐行翻译，其余原样", () => {
    setLang("zh");
    expect(tc("Brownout reset (RTCWDT_BROWN_OUT_RESET)\nsome raw log line")).toBe("欠压复位（RTCWDT_BROWN_OUT_RESET）\nsome raw log line");
    expect(tc("I am free text from the model, with commas, and more")).toBe("I am free text from the model, with commas, and more");
  });
});
