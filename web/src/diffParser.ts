export type InlineSegment = {
  kind: "equal" | "add" | "del";
  text: string;
};

export type DiffLine = {
  kind: "context" | "add" | "del";
  content: string;
  oldLine: number | null;
  newLine: number | null;
  segments?: InlineSegment[];
};

export type SplitDiffCell = {
  line: number | null;
  content: string;
  kind: "context" | "del" | "add" | "empty";
  segments?: InlineSegment[];
};

export type SplitDiffRow = {
  left: SplitDiffCell;
  right: SplitDiffCell;
};

export type DiffHunk = {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  section: string;
  explicitHeader: boolean;
  lines: DiffLine[];
  splitRows: SplitDiffRow[];
};

export type ParsedDiff = {
  oldFile: string;
  newFile: string;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
};

const TOKEN_RE = /[A-Za-z0-9_]+|\s+|./gu;
const MAX_LCS_CELLS = 250_000;

function tokenizeForInlineDiff(text: string): string[] {
  return text.match(TOKEN_RE) ?? [];
}

function coalesceSegments(segments: InlineSegment[]): InlineSegment[] {
  const merged: InlineSegment[] = [];
  for (const segment of segments) {
    if (!segment.text) continue;
    const prev = merged.at(-1);
    if (prev && prev.kind === segment.kind) {
      prev.text += segment.text;
    } else {
      merged.push({ ...segment });
    }
  }
  return merged;
}

/**
 * Compute token-level inline diff highlights between a removed line and an added line.
 */
export function computeInlineDiff(
  before: string,
  after: string,
): { left: InlineSegment[]; right: InlineSegment[] } {
  if (before === after) {
    const equal = before ? [{ kind: "equal" as const, text: before }] : [];
    return { left: equal, right: equal.map((seg) => ({ ...seg })) };
  }

  const leftTokens = tokenizeForInlineDiff(before);
  const rightTokens = tokenizeForInlineDiff(after);

  let prefix = 0;
  const minLen = Math.min(leftTokens.length, rightTokens.length);
  while (prefix < minLen && leftTokens[prefix] === rightTokens[prefix]) {
    prefix += 1;
  }

  let suffix = 0;
  while (
    suffix < leftTokens.length - prefix &&
    suffix < rightTokens.length - prefix &&
    leftTokens[leftTokens.length - 1 - suffix] ===
      rightTokens[rightTokens.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const leftMid = leftTokens.slice(prefix, leftTokens.length - suffix);
  const rightMid = rightTokens.slice(prefix, rightTokens.length - suffix);
  const prefixText = leftTokens.slice(0, prefix).join("");
  const suffixText =
    suffix > 0 ? leftTokens.slice(leftTokens.length - suffix).join("") : "";

  const leftSegments: InlineSegment[] = [];
  const rightSegments: InlineSegment[] = [];
  if (prefixText) {
    leftSegments.push({ kind: "equal", text: prefixText });
    rightSegments.push({ kind: "equal", text: prefixText });
  }

  const m = leftMid.length;
  const n = rightMid.length;
  if (m === 0 && n > 0) {
    rightSegments.push({ kind: "add", text: rightMid.join("") });
  } else if (n === 0 && m > 0) {
    leftSegments.push({ kind: "del", text: leftMid.join("") });
  } else if (m > 0 && n > 0) {
    if (m * n <= MAX_LCS_CELLS) {
      const cols = n + 1;
      const dp = new Int32Array((m + 1) * cols);
      for (let i = m - 1; i >= 0; i -= 1) {
        for (let j = n - 1; j >= 0; j -= 1) {
          if (leftMid[i] === rightMid[j]) {
            dp[i * cols + j] = dp[(i + 1) * cols + (j + 1)] + 1;
          } else {
            const down = dp[(i + 1) * cols + j];
            const right = dp[i * cols + (j + 1)];
            dp[i * cols + j] = down >= right ? down : right;
          }
        }
      }

      let i = 0;
      let j = 0;
      while (i < m && j < n) {
        if (leftMid[i] === rightMid[j]) {
          leftSegments.push({ kind: "equal", text: leftMid[i] });
          rightSegments.push({ kind: "equal", text: rightMid[j] });
          i += 1;
          j += 1;
        } else if (dp[(i + 1) * cols + j] >= dp[i * cols + (j + 1)]) {
          leftSegments.push({ kind: "del", text: leftMid[i] });
          i += 1;
        } else {
          rightSegments.push({ kind: "add", text: rightMid[j] });
          j += 1;
        }
      }
      while (i < m) {
        leftSegments.push({ kind: "del", text: leftMid[i] });
        i += 1;
      }
      while (j < n) {
        rightSegments.push({ kind: "add", text: rightMid[j] });
        j += 1;
      }
    } else {
      leftSegments.push({ kind: "del", text: leftMid.join("") });
      rightSegments.push({ kind: "add", text: rightMid.join("") });
    }
  }

  if (suffixText) {
    leftSegments.push({ kind: "equal", text: suffixText });
    rightSegments.push({ kind: "equal", text: suffixText });
  }

  return {
    left: coalesceSegments(leftSegments),
    right: coalesceSegments(rightSegments),
  };
}

function cleanDiffFilePath(raw: string): string {
  const withoutTimestamp = raw.split("\t", 1)[0].trim();
  if (!withoutTimestamp || withoutTimestamp === "/dev/null") return "";
  if (withoutTimestamp.startsWith("a/") || withoutTimestamp.startsWith("b/")) {
    return withoutTimestamp.slice(2);
  }
  return withoutTimestamp;
}

function buildSplitRows(lines: DiffLine[]): SplitDiffRow[] {
  const splitRows: SplitDiffRow[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (line.kind === "context") {
      splitRows.push({
        left: { line: line.oldLine, content: line.content, kind: "context" },
        right: { line: line.newLine, content: line.content, kind: "context" },
      });
      index += 1;
      continue;
    }

    const deletions: DiffLine[] = [];
    while (index < lines.length && lines[index].kind === "del") {
      deletions.push(lines[index]);
      index += 1;
    }

    const additions: DiffLine[] = [];
    while (index < lines.length && lines[index].kind === "add") {
      additions.push(lines[index]);
      index += 1;
    }

    const pairCount = Math.max(deletions.length, additions.length);
    for (let pairIdx = 0; pairIdx < pairCount; pairIdx += 1) {
      const delLine = deletions[pairIdx];
      const addLine = additions[pairIdx];
      let leftSegments: InlineSegment[] | undefined;
      let rightSegments: InlineSegment[] | undefined;
      if (delLine && addLine) {
        const inline = computeInlineDiff(delLine.content, addLine.content);
        leftSegments = inline.left;
        rightSegments = inline.right;
        delLine.segments = leftSegments;
        addLine.segments = rightSegments;
      }

      splitRows.push({
        left: delLine
          ? {
              line: delLine.oldLine,
              content: delLine.content,
              kind: "del",
              segments: leftSegments,
            }
          : { line: null, content: "", kind: "empty" },
        right: addLine
          ? {
              line: addLine.newLine,
              content: addLine.content,
              kind: "add",
              segments: rightSegments,
            }
          : { line: null, content: "", kind: "empty" },
      });
    }
  }

  return splitRows;
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/**
 * Parse a unified diff string into structured hunks, line numbers, inline segments, and split rows.
 */
export function parseUnifiedDiff(raw: string): ParsedDiff {
  const normalized = raw.replace(/\r\n/g, "\n");
  if (!normalized.trim()) {
    return { oldFile: "", newFile: "", additions: 0, deletions: 0, hunks: [] };
  }

  const rawLines = normalized.split("\n");
  if (rawLines.length > 0 && rawLines[rawLines.length - 1] === "") {
    rawLines.pop();
  }

  let oldFile = "";
  let newFile = "";
  let additions = 0;
  let deletions = 0;
  const hunks: DiffHunk[] = [];
  let current: DiffHunk | null = null;
  let oldLine = 1;
  let newLine = 1;

  const ensureImplicitHunk = () => {
    if (current) return current;
    oldLine = 1;
    newLine = 1;
    current = {
      oldStart: 1,
      oldCount: 0,
      newStart: 1,
      newCount: 0,
      section: "",
      explicitHeader: false,
      lines: [],
      splitRows: [],
    };
    hunks.push(current);
    return current;
  };

  for (let idx = 0; idx < rawLines.length; idx += 1) {
    const line = rawLines[idx];

    if (
      line.startsWith("diff --git ") ||
      line.startsWith("index ") ||
      line.startsWith("new file mode ") ||
      line.startsWith("deleted file mode ") ||
      line.startsWith("old mode ") ||
      line.startsWith("new mode ") ||
      line === "\\ No newline at end of file"
    ) {
      continue;
    }

    if (
      line.startsWith("--- ") &&
      (current === null ||
        ((rawLines[idx + 1] ?? "").startsWith("+++ ") &&
          (idx + 2 >= rawLines.length ||
            (rawLines[idx + 2] ?? "").startsWith("@@ "))))
    ) {
      oldFile = cleanDiffFilePath(line.slice(4));
      current = null;
      continue;
    }

    if (
      line.startsWith("+++ ") &&
      (current === null || (rawLines[idx + 1] ?? "").startsWith("@@ "))
    ) {
      newFile = cleanDiffFilePath(line.slice(4));
      current = null;
      continue;
    }

    const hunkMatch = HUNK_HEADER_RE.exec(line);
    if (hunkMatch) {
      const oldStart = Number(hunkMatch[1]);
      const oldCount = hunkMatch[2] !== undefined ? Number(hunkMatch[2]) : 1;
      const newStart = Number(hunkMatch[3]);
      const newCount = hunkMatch[4] !== undefined ? Number(hunkMatch[4]) : 1;
      oldLine = oldStart;
      newLine = newStart;
      current = {
        oldStart,
        oldCount,
        newStart,
        newCount,
        section: hunkMatch[5].trim(),
        explicitHeader: true,
        lines: [],
        splitRows: [],
      };
      hunks.push(current);
      continue;
    }

    if (line.startsWith("+")) {
      const hunk = ensureImplicitHunk();
      hunk.lines.push({
        kind: "add",
        content: line.slice(1),
        oldLine: null,
        newLine,
      });
      newLine += 1;
      additions += 1;
      if (!hunk.explicitHeader) hunk.newCount += 1;
      continue;
    }

    if (line.startsWith("-")) {
      const hunk = ensureImplicitHunk();
      hunk.lines.push({
        kind: "del",
        content: line.slice(1),
        oldLine,
        newLine: null,
      });
      oldLine += 1;
      deletions += 1;
      if (!hunk.explicitHeader) hunk.oldCount += 1;
      continue;
    }

    if (line.startsWith(" ") || (current !== null && line === "")) {
      const hunk = ensureImplicitHunk();
      const content = line.startsWith(" ") ? line.slice(1) : "";
      hunk.lines.push({
        kind: "context",
        content,
        oldLine,
        newLine,
      });
      oldLine += 1;
      newLine += 1;
      if (!hunk.explicitHeader) {
        hunk.oldCount += 1;
        hunk.newCount += 1;
      }
    }
  }

  for (const hunk of hunks) {
    hunk.splitRows = buildSplitRows(hunk.lines);
  }

  return { oldFile, newFile, additions, deletions, hunks };
}
