import { describe, expect, it } from "vitest";
import { emptyView, reduceUpdate } from "./store";

const entry = (seq: number, kind: string, extra: object = {}) => ({
  seq, kind, turn: 1, commit: `c${seq}`, changed: true, files: ["main/blink.c"], added: 2, deleted: 1, prompt: "",
  stop: "end_turn", firmware: null, restored_to: null, at: "", ...extra,
});

describe("reduceUpdate：W5 的 session/update", () => {
  it("每轮的 checkpoint 只画在时间线上，对话里只出现回退", () => {
    let v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/checkpoint", entry: entry(1, "turn") });
    expect(v.items).toHaveLength(0);
    v = reduceUpdate(v, { sessionUpdate: "_fwr/checkpoint", entry: entry(2, "restore", { restored_to: 0 }),
                          flash: { ok: true, image_sha256: "abc" } });
    expect(v.items).toHaveLength(1);
    expect(v.items[0]).toMatchObject({ kind: "restore", entry: { restored_to: 0 }, flash: { ok: true } });
  });

  it("后台首次编译只占一条，进度原地更新", () => {
    let v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/prebuild", status: "running", text: "编译 1/10" });
    v = reduceUpdate(v, { sessionUpdate: "user_message_chunk", content: { type: "text", text: "修一下" } });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/prebuild", status: "running", text: "编译 9/10" });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/prebuild", status: "ok", text: "", durationMs: 85000 });
    const pre = v.items.filter((it) => it.kind === "prebuild");
    expect(pre).toHaveLength(1);
    expect(pre[0]).toMatchObject({ status: "ok", durationMs: 85000 });
    expect(v.items[0].kind).toBe("prebuild"); // 位置不变，仍在用户消息前面
  });

  it("会话结束：合并 / 丢弃各有一条说明，没删干净的东西要告诉用户", () => {
    const merged = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/session_ended", state: "merged",
                                               mergedCommit: "0ba56e98ffff", problems: [] });
    expect(merged.items[0]).toMatchObject({ kind: "notice", tone: "info" });
    expect((merged.items[0] as { text: string }).text).toContain("0ba56e98");
    const discarded = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/session_ended", state: "discarded",
                                                  problems: ["目录没能完全删除"] });
    expect(discarded.items[0]).toMatchObject({ kind: "notice", tone: "warn" });
    expect((discarded.items[0] as { text: string }).text).toContain("目录没能完全删除");
  });
});

describe("reduceUpdate：W6 的压缩和上下文用量", () => {
  it("正在压缩 → 已压缩，同一条原地更新；用量跟着变", () => {
    let v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/context", used: 90000, window: 100000 });
    expect(v.context).toEqual({ used: 90000, window: 100000 });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/compacting", reason: "auto", used: 90000 });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/compacted", ok: true, reason: "auto", before: 90000, after: 6000,
                          segment: "segment_001.md" });
    expect(v.items).toHaveLength(1);
    expect(v.items[0]).toMatchObject({ kind: "compaction", status: "ok", after: 6000, segment: "segment_001.md" });
    expect(v.context).toEqual({ used: 6000, window: 100000 });
  });

  it("压缩失败也要显示，用量不变", () => {
    const v = reduceUpdate({ ...emptyView(), context: { used: 5, window: 10 } },
                           { sessionUpdate: "_fwr/compacted", ok: false, reason: "manual", error: "模型没有输出摘要" });
    expect(v.items[0]).toMatchObject({ kind: "compaction", status: "failed", error: "模型没有输出摘要" });
    expect(v.context).toEqual({ used: 5, window: 10 });
  });
});

describe("reduceUpdate：W7 子 agent 和 goal", () => {
  it("子 agent 一张卡片原地更新；它的工具调用带标记进时间线", () => {
    let v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/subagent", subagentId: "s.exp1", kind: "explore",
                                        description: "看 main.c", status: "running" });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/subagent_update", subagentId: "s.exp1", label: "explore·看 main.c",
                          update: { sessionUpdate: "tool_call", toolCallId: "s.exp1:c1", title: "read_file main.c", kind: "read" } });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/subagent", subagentId: "s.exp1", kind: "explore", description: "看 main.c",
                          status: "done", stop: "end_turn", steps: 2, text: "结论" });
    expect(v.items.map((i) => i.kind)).toEqual(["subagent", "tool"]);
    expect(v.items[0]).toMatchObject({ status: "done", steps: 2, text: "结论" });
    expect(v.items[1]).toMatchObject({ id: "s.exp1:c1", sub: "explore·看 main.c" });
  });

  it("goal 状态存在会话视图里", () => {
    const v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/goal", goal: { objective: "x", status: "working", round: 1 } });
    expect(v.goal).toMatchObject({ status: "working", round: 1 });
  });

  it("后台子 agent 做完时 agent 空闲：卡片等用户决定；用户发话后收起", () => {
    let v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/subagent", subagentId: "s.exp2", kind: "explore",
                                        description: "watch serial", status: "running", background: true });
    expect(v.items[0]).toMatchObject({ background: true, awaitingParent: false });
    v = reduceUpdate(v, { sessionUpdate: "_fwr/subagent", subagentId: "s.exp2", kind: "explore", description: "watch serial",
                          status: "done", background: true, awaitingParent: true, stop: "end_turn", steps: 3 });
    expect(v.items[0]).toMatchObject({ status: "done", awaitingParent: true });
    v = reduceUpdate(v, { sessionUpdate: "user_message_chunk", content: { type: "text", text: "continue" } });
    expect(v.items[0]).toMatchObject({ awaitingParent: false });
  });

  it("子 agent 的回合结束不画分隔线", () => {
    const v = reduceUpdate(emptyView(), { sessionUpdate: "_fwr/subagent_update", subagentId: "s.pla1", label: "planner",
                                          update: { sessionUpdate: "_fwr/turn_end", stopReason: "end_turn", usage: null, error: null } });
    expect(v.items).toHaveLength(0);
  });
});

describe("回放：核心合并后的流式片段", () => {
  it("合并成一条的片段和逐条回放折叠出相同的时间线", () => {
    const t = (text: string) => ({ type: "text", text });
    const pieces = [
      { sessionUpdate: "user_message_chunk", content: t("hi") },
      ...[..."think"].map((c) => ({ sessionUpdate: "agent_thought_chunk", content: t(c) })),
      ...[..."ok!"].map((c) => ({ sessionUpdate: "agent_message_chunk", content: t(c) })),
    ];
    const merged = [pieces[0], { sessionUpdate: "agent_thought_chunk", content: t("think") },
                    { sessionUpdate: "agent_message_chunk", content: t("ok!") }];
    const strip = (v: ReturnType<typeof emptyView>) => v.items.map(({ id: _id, ...rest }) => rest);
    expect(strip(merged.reduce(reduceUpdate, emptyView()))).toEqual(strip(pieces.reduce(reduceUpdate, emptyView())));
  });
});
