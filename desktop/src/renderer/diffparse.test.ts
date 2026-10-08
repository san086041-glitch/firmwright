import { describe, expect, it } from "vitest";
import { changedSpan, parseDiff, toSplit } from "./diffparse";

const GIT = [
  "diff --git a/main/blink.c b/main/blink.c",
  "index 1111111..2222222 100644",
  "--- a/main/blink.c",
  "+++ b/main/blink.c",
  "@@ -10,4 +10,3 @@ void app_main(void)",
  " int x = 0;",
  "-    vTaskDelay(pdMS_TO_TICKS(1000));",
  "+    vTaskDelay(pdMS_TO_TICKS(250));",
  "--- this deleted line starts with two dashes",
  " }",
  "diff --git a/main/new.h b/main/new.h",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/main/new.h",
  "@@ -0,0 +1,2 @@",
  "+#pragma once",
  "+void selftest(void);",
  "\\ No newline at end of file",
  "",
].join("\n");

describe("parseDiff", () => {
  it("git diff：多个文件、行号、新文件、以 -- 开头的删除行不被当成文件头", () => {
    const files = parseDiff(GIT);
    expect(files.map((f) => [f.path, f.status, f.added, f.deleted])).toEqual([
      ["main/blink.c", "modified", 1, 2],
      ["main/new.h", "added", 2, 0],
    ]);
    const lines = files[0].hunks[0].lines;
    expect(lines.map((l) => l.kind)).toEqual(["ctx", "del", "add", "del", "ctx"]);
    expect(lines[3].text).toBe("-- this deleted line starts with two dashes");
    // 上下文 10/10 → 删 11、12（旧）/ 加 11（新）→ 上下文 13/12
    expect([lines[0].old, lines[0].new, lines[2].new, lines[4].old, lines[4].new]).toEqual([10, 10, 11, 13, 12]);
    expect(files[1].hunks[0].lines.at(-1)).toMatchObject({ kind: "note" });
  });

  it("审批预览拼出来的伪 diff（没有 @@）", () => {
    const files = parseDiff(["--- main.c", "-int a = 1;", "--- old comment", "+int a = 2;"].join("\n"));
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("main.c");
    expect(files[0].hunks[0].lines.map((l) => [l.kind, l.text])).toEqual([
      ["del", "int a = 1;"], ["del", "-- old comment"], ["add", "int a = 2;"],
    ]);
  });

  it("并排：删除和新增配对；行内只标出改动的部分", () => {
    const rows = toSplit(parseDiff(GIT)[0].hunks[0]);
    expect(rows.map((r) => [r.left?.kind, r.right?.kind])).toEqual([
      ["ctx", "ctx"], ["del", "add"], ["del", undefined], ["ctx", "ctx"],
    ]);
    const a = "vTaskDelay(pdMS_TO_TICKS(1000));";
    const b = "vTaskDelay(pdMS_TO_TICKS(250));";
    const s = changedSpan(a, b);
    expect(a.slice(...s.a)).toBe("100");
    expect(b.slice(...s.b)).toBe("25");
  });
});
