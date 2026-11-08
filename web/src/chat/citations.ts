import { safeDecodePath } from "../paths";
import { tablePartsFromRef } from "../tableDetail";
import type { Citation, CitationGroup, CitationLabel, SqlBlock } from "./types";

export function sqlBlocks(values: unknown[] | undefined): SqlBlock[] {
  const blocks: SqlBlock[] = [];
  for (const value of values ?? []) {
    if (
      typeof value === "object" &&
      value !== null &&
      "sql" in value &&
      typeof value.sql === "string"
    ) {
      const dialect = "dialect" in value ? value.dialect : undefined;
      blocks.push({
        sql: value.sql,
        dialect: typeof dialect === "string" ? dialect : undefined,
      });
    }
  }
  return blocks;
}

export function citationsOf(values: unknown[] | undefined): Citation[] {
  const result: Citation[] = [];
  for (const value of values ?? []) {
    if (
      typeof value === "object" &&
      value !== null &&
      "ref" in value &&
      typeof value.ref === "string"
    ) {
      const type = "type" in value ? value.type : undefined;
      result.push({
        ref: value.ref,
        type: typeof type === "string" ? type : value.ref.split(":", 1)[0],
      });
    }
  }
  return result;
}

export function normalizeSql(sql: string): string {
  return sql.replace(/\s+/g, " ").trim().toLowerCase();
}

function hasInvalidEncoding(value: string): boolean {
  return value.split("/").some((segment) => {
    try {
      decodeURIComponent(segment);
      return false;
    } catch {
      return true;
    }
  });
}

function middleEllipsis(value: string, max = 48): string {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

export function citationLabel(ref: string): CitationLabel {
  const separator = ref.indexOf(":");
  const type = (separator >= 0 ? ref.slice(0, separator) : "").toLowerCase();
  const value = separator >= 0 ? ref.slice(separator + 1) : ref;
  if (type === "document") {
    const rawPath = value.split("#", 1)[0];
    const fullPath = safeDecodePath(rawPath);
    const segments = fullPath.split("/").filter(Boolean);
    if (hasInvalidEncoding(rawPath)) {
      const rawSegments = rawPath.split("/").filter(Boolean);
      return {
        kind: "document",
        label: rawSegments.at(-1) ?? rawPath,
        category: rawSegments[0] ?? "",
        title: fullPath,
      };
    }
    const fileName = segments.at(-1) ?? fullPath;
    const parent = segments.at(-2) ?? "";
    const category = segments[0] ?? "";
    const stem = fileName.replace(/\.md$/i, "") || fileName;
    let label = stem;
    if (/^_overview$/i.test(stem)) {
      label = parent ? `${parent} · 概览` : "概览";
    } else {
      const variant = /^variant_(\d+)$/i.exec(stem);
      if (variant) {
        const number = variant[1].replace(/^0+(?=\d)/, "");
        label = parent ? `${parent} · 口径 ${number}` : `口径 ${number}`;
      }
    }
    return { kind: "document", label, category, title: fullPath };
  }
  if (type === "table" || type === "field") {
    const parts = tablePartsFromRef(ref);
    if (parts) {
      const full = `${parts.database}.${parts.name}`;
      let title = full;
      if (type === "field") {
        const column = safeDecodePath(value.split(".").slice(2).join("."));
        if (column) title = `${full} · ${column}`;
      }
      return { kind: "table", label: middleEllipsis(full), title };
    }
  }
  const decoded = safeDecodePath(value);
  return {
    kind: type === "lineage" ? "lineage" : "other",
    label: middleEllipsis(decoded || ref),
    title: decoded || ref,
  };
}

export function citationDisplayName(label: CitationLabel): string {
  return label.category ? `${label.category} · ${label.label}` : label.label;
}

function citationKey(ref: string): string {
  const separator = ref.indexOf(":");
  const type = separator >= 0 ? ref.slice(0, separator) : "";
  const value = separator >= 0 ? ref.slice(separator + 1) : ref;
  if (type === "document") return `document:${value.split("#", 1)[0]}`;
  if (type === "table" || type === "field") {
    const parts = tablePartsFromRef(ref);
    if (parts) return `table:${parts.database}.${parts.name}`;
  }
  return ref;
}

function fieldName(ref: string): string | undefined {
  if (!ref.startsWith("field:")) return undefined;
  const parts = ref.slice(6).split(".");
  if (parts.length < 3) return undefined;
  return safeDecodePath(parts.slice(2).join("."));
}

export function mergeCitations(citations: Citation[]): CitationGroup[] {
  const groups = new Map<string, CitationGroup>();
  for (const citation of citations) {
    const ref = citation.ref.trim();
    if (!ref) continue;
    const key = citationKey(ref);
    const field = fieldName(ref);
    const existing = groups.get(key);
    if (existing) {
      if (!existing.refs.includes(ref)) existing.refs.push(ref);
      if (ref.startsWith("table:")) existing.openRef = ref;
      if (field && !existing.fields.includes(field))
        existing.fields.push(field);
      continue;
    }
    const group: CitationGroup = {
      key,
      openRef: ref,
      refs: [ref],
      label: citationLabel(ref),
      fields: field ? [field] : [],
    };
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    if (group.label.kind !== "table" || group.fields.length === 0) continue;
    const parts = tablePartsFromRef(group.openRef);
    const table = parts ? `${parts.database}.${parts.name}` : group.label.title;
    group.label = {
      ...group.label,
      title: `${table} · 字段：${group.fields.join("、")}`,
    };
  }
  return [...groups.values()];
}

export function citationsFromRefs(refs: string[]): Citation[] {
  return refs.map((ref) => ({ ref, type: ref.split(":", 1)[0] }));
}

export function stripMatchingSql(
  text: string,
  sql: string | undefined,
): string {
  if (!sql) return text;
  const target = normalizeSql(sql);
  const lines = text.split(/\r?\n/);
  const kept: string[] = [];
  let index = 0;
  while (index < lines.length) {
    const fence = /^\s*```([\w+-]*)\s*$/.exec(lines[index]);
    if (!fence) {
      kept.push(lines[index]);
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < lines.length && !/^\s*```\s*$/.test(lines[end])) end += 1;
    const code = lines.slice(index + 1, end).join("\n");
    if (normalizeSql(code) === target) {
      index = end + 1;
      continue;
    }
    for (
      let cursor = index;
      cursor <= Math.min(end, lines.length - 1);
      cursor++
    )
      kept.push(lines[cursor]);
    index = end + 1;
  }
  return kept
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trimEnd();
}

const CITATION_HEADER =
  /^(?:引用\s*[:：]|citations?\s*[:：]|references?\s*[:：])\s*/i;
const CITATION_REF = /^(?:document|table|field|lineage|task):\S+$/i;

function normalizeCitationLine(line: string): string {
  return line
    .trim()
    .replace(/^>\s*/, "")
    .replace(/^(?:[-*+•]\s+)+/, "")
    .replace(/^`+|`+$/g, "")
    .replace(/^\*\*(.*?)\*\*$/, "$1")
    .trim();
}

function citationTokens(value: string): string[] | null {
  const tokens = value
    .replace(/`/g, "")
    .split(/[、,，;；\s]+/)
    .map((token) => token.trim())
    .filter(Boolean);
  if (tokens.length === 0) return null;
  return tokens.every((token) => CITATION_REF.test(token)) ? tokens : null;
}

export function stripCitationRefs(text: string): {
  text: string;
  refs: string[];
} {
  const lines = text.split(/\r?\n/);
  let cursor = lines.length;
  while (cursor > 0 && !lines[cursor - 1].trim()) cursor -= 1;
  const removed = new Set<number>();
  const entries: { index: number; tokens: string[] }[] = [];
  while (cursor > 0) {
    const continuation: number[] = [];
    const continuationEntries: { index: number; tokens: string[] }[] = [];
    let scan = cursor;
    while (scan > 0) {
      if (!lines[scan - 1].trim()) {
        continuation.unshift(scan - 1);
        scan -= 1;
        continue;
      }
      const tokens = citationTokens(normalizeCitationLine(lines[scan - 1]));
      if (!tokens) break;
      continuation.unshift(scan - 1);
      continuationEntries.unshift({ index: scan - 1, tokens });
      scan -= 1;
    }
    if (scan === 0) break;
    const normalized = normalizeCitationLine(lines[scan - 1]);
    const header = CITATION_HEADER.exec(normalized);
    if (!header) break;
    const rest = normalized.slice(header[0].length);
    const headerTokens = rest.trim() ? citationTokens(rest) : [];
    if (rest.trim() && !headerTokens) break;
    removed.add(scan - 1);
    for (const index of continuation) removed.add(index);
    entries.push({ index: scan - 1, tokens: headerTokens ?? [] });
    entries.push(...continuationEntries);
    cursor = scan - 1;
  }
  if (removed.size === 0) return { text, refs: [] };
  entries.sort((left, right) => left.index - right.index);
  const kept = lines.filter((_, index) => !removed.has(index));
  while (kept.length > 0 && !kept[kept.length - 1].trim()) kept.pop();
  return {
    text: kept.join("\n"),
    refs: entries.flatMap((entry) => entry.tokens),
  };
}
