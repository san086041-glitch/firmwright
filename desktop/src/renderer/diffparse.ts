/** 统一 diff（git diff / difflib）解析成 文件 → 块 → 行（2026-10-05，给新的 diff 视图用；纯函数，便于测试）。
 *  也接受审批预览里拼出来的伪 diff（只有 ---、-、+ 行，没有 @@ 块头）。
 *  块头里的行数决定块到哪里结束：块里还有没读完的行时，"--- …" 也是内容（删掉的一行可能正好以 "-- " 开头）。 */

export type LineKind = "ctx" | "add" | "del" | "note";

export interface DiffLine {
  kind: LineKind;
  text: string;
  old?: number; // 旧文件里的行号
  new?: number; // 新文件里的行号
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  oldPath?: string;
  status: "modified" | "added" | "deleted" | "renamed" | "binary";
  hunks: DiffHunk[];
  added: number;
  deleted: number;
}

const strip = (p: string) => p.split("\t")[0].replace(/^"|"$/g, "").replace(/^[ab]\//, "");

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let f: DiffFile | null = null;
  let h: DiffHunk | null = null;
  let oldNo = 0, newNo = 0, remOld = 0, remNew = 0;
  let numbered = false; // 当前块有 @@ 块头（有行号和行数）

  const startFile = (path: string): DiffFile => {
    const nf: DiffFile = { path, status: "modified", hunks: [], added: 0, deleted: 0 };
    files.push(nf);
    f = nf;
    h = null;
    numbered = false;
    return nf;
  };
  const content = (line: string): void => {
    const file = f ?? startFile("");
    if (!h) {
      h = { header: "", lines: [] };
      file.hunks.push(h);
    }
    const c = line[0];
    if (c === "+") {
      h.lines.push({ kind: "add", text: line.slice(1), new: numbered ? newNo++ : undefined });
      file.added++;
      remNew--;
    } else if (c === "-") {
      h.lines.push({ kind: "del", text: line.slice(1), old: numbered ? oldNo++ : undefined });
      file.deleted++;
      remOld--;
    } else if (c === "\\") {
      h.lines.push({ kind: "note", text: line.slice(1).trim() });
    } else {
      h.lines.push({ kind: "ctx", text: line.slice(1), old: oldNo++, new: newNo++ });
      remOld--;
      remNew--;
    }
  };

  for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
    // 有行数的块还没读完：一律是内容（"diff --git" 除外：块头行数不对时也不把下一个文件吞进来）
    if (h && numbered && (remOld > 0 || remNew > 0) && !line.startsWith("diff --git ")) {
      content(line === "" ? " " : line);
      continue;
    }
    if (h && numbered && line.startsWith("\\")) {
      content(line);
      continue;
    }
    let m: RegExpMatchArray | null;
    if ((m = line.match(/^diff --git a\/(.+?) b\/(.+)$/))) {
      const nf = startFile(m[2]);
      if (m[1] !== m[2]) nf.oldPath = m[1];
      continue;
    }
    if (f && !h) {
      const cur: DiffFile = f;
      if (/^new file mode/.test(line)) { cur.status = "added"; continue; }
      if (/^deleted file mode/.test(line)) { cur.status = "deleted"; continue; }
      if ((m = line.match(/^rename from (.+)$/))) { cur.oldPath = m[1]; cur.status = "renamed"; continue; }
      if (/^(rename to|index|similarity index|old mode|new mode) /.test(line)) continue;
      if (/^Binary files /.test(line)) { cur.status = "binary"; continue; }
      if ((m = line.match(/^\+\+\+ (.+)$/))) {
        if (strip(m[1]) === "/dev/null") cur.status = "deleted";
        else cur.path = strip(m[1]);
        continue;
      }
    }
    // "--- a/x"：文件开始（difflib 没有 diff --git 行）。伪 diff 里块已经开始后，"--- " 当内容
    if ((m = line.match(/^--- (.+)$/)) && !(h && !numbered)) {
      const p = strip(m[1]);
      const open = f as DiffFile | null; // startFile 在闭包里改 f，TS 推断不出来
      const cur = open && !h && open.hunks.length === 0 ? open : startFile(p === "/dev/null" ? "" : p);
      if (p === "/dev/null") cur.status = "added";
      else if (!cur.path) cur.path = p;
      h = null;
      continue;
    }
    if ((m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/))) {
      const file = f ?? startFile("");
      h = { header: line, lines: [] };
      file.hunks.push(h);
      numbered = true;
      oldNo = Number(m[1]);
      remOld = m[2] === undefined ? 1 : Number(m[2]);
      newNo = Number(m[3]);
      remNew = m[4] === undefined ? 1 : Number(m[4]);
      continue;
    }
    if (h && !numbered && /^[-+ \\]/.test(line)) { // 伪 diff：块一直延续
      content(line);
      continue;
    }
    if (!h && /^[-+]/.test(line) && f) { // 伪 diff 的第一行内容
      content(line);
    }
  }
  return files.filter((x) => x.path || x.hunks.length);
}

/** 并排视图的一行：左边旧、右边新。连续的删除和新增一一配对（配对的行再做行内对比）。 */
export interface SplitRow {
  left?: DiffLine;
  right?: DiffLine;
}

export function toSplit(h: DiffHunk): SplitRow[] {
  const rows: SplitRow[] = [];
  let dels: DiffLine[] = [];
  let adds: DiffLine[] = [];
  const flush = () => {
    const n = Math.max(dels.length, adds.length);
    for (let i = 0; i < n; i++) rows.push({ left: dels[i], right: adds[i] });
    dels = [];
    adds = [];
  };
  for (const l of h.lines) {
    if (l.kind === "del") dels.push(l);
    else if (l.kind === "add") adds.push(l);
    else {
      flush();
      rows.push(l.kind === "note" ? { left: l } : { left: l, right: l });
    }
  }
  flush();
  return rows;
}

/** 行内对比：去掉共同的前缀和后缀，中间就是改动的部分（够用，而且快）。 */
export function changedSpan(a: string, b: string): { a: [number, number]; b: [number, number] } {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  return { a: [p, a.length - s], b: [p, b.length - s] };
}
