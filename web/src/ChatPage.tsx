import { createPortal } from "react-dom";
import { ColumnLineage } from "./ColumnLineage";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Input, Space, Tag } from "antd";
import type { components, paths } from "./api";
import type { Language, Messages } from "./i18n";
import { TaskLineage } from "./TaskLineage";
import {
  ArrowUpOutlined,
  CloseOutlined,
  DeleteOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { parseAgentProfiles, type AgentProfiles } from "./agentProfiles";
import { HighlightedSql, Markdown } from "./Markdown";
import { safeDecodePath } from "./paths";
import {
  loadTableDetail,
  tablePartsFromRef,
  type TableDetail,
} from "./tableDetail";

type Agent = components["schemas"]["AgentView"];
type Provider = components["schemas"]["ProviderView"];
type Conversation = components["schemas"]["ConversationView"];
type ConversationDetail = components["schemas"]["ConversationDetail"];
type Turn = components["schemas"]["TurnView"];
type EventView = components["schemas"]["EventView"];
type DocumentView =
  paths["/api/v1/document"]["get"]["responses"][200]["content"]["application/json"];

type SqlBlock = { sql: string; dialect?: string };
type Citation = { ref: string; type: string };
export type CitationLabel = {
  kind: "table" | "document" | "lineage" | "other";
  label: string;
  category?: string;
  title: string;
};
export type CitationGroup = {
  key: string;
  openRef: string;
  refs: string[];
  label: CitationLabel;
  fields: string[];
};
type ToolCall = {
  id: string;
  name: string;
  summary: string;
  status: "running" | "done" | "error";
  input?: string;
  result?: string;
};
type Clarification = {
  id?: string;
  question?: string;
  options?: { id?: string; label?: string; ref?: string }[];
};
type QueryPlan = {
  id: string;
  sql: string;
  data_source: string;
  tables: string[];
  estimated_partitions: number | null;
  estimated_rows: number | null;
  row_limit: number;
  execution_limit?: number;
};
type QueryResult = {
  id: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
};
type LiveTurn = {
  question: string;
  text: string;
  tools: ToolCall[];
  sql: SqlBlock[];
  citations: Citation[];
  clarifications: Clarification[][];
  status: "running" | "completed" | "failed";
  error?: string;
};
type CitationDetail =
  | { kind: "table"; ref: string; table: TableDetail }
  | { kind: "document"; ref: string; document: DocumentView }
  | { kind: "message"; ref: string; message: string };

const PROVIDER_TYPES = [
  "anthropic-messages",
  "openai-completions",
  "openai-responses",
  "google-generative-ai",
];

function sqlBlocks(values: unknown[] | undefined): SqlBlock[] {
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

function citationsOf(values: unknown[] | undefined): Citation[] {
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

function normalizeSql(sql: string): string {
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

function citationsFromRefs(refs: string[]): Citation[] {
  return refs.map((ref) => ({ ref, type: ref.split(":", 1)[0] }));
}

function stripMatchingSql(text: string, sql: string | undefined): string {
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

function clarificationsOf(value: unknown): Clarification[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Clarification => typeof item === "object" && item !== null,
  );
}

function applyToolStart(
  tools: ToolCall[],
  data: Record<string, unknown>,
): ToolCall[] {
  const id = String(data.id ?? data.name ?? `${Date.now()}-${Math.random()}`);
  return [
    ...tools,
    {
      id,
      name: String(data.name ?? "tool"),
      summary: String(data.summary ?? ""),
      status: "running",
      input: String(data.summary ?? ""),
    },
  ];
}

function applyToolEnd(
  tools: ToolCall[],
  data: Record<string, unknown>,
): ToolCall[] {
  const id = String(data.id ?? data.name ?? "");
  const next: ToolCall = {
    id,
    name: String(data.name ?? "tool"),
    summary: String(data.summary ?? ""),
    status: data.is_error ? "error" : "done",
    result: String(data.summary ?? ""),
  };
  const index = tools.findIndex((tool) => tool.id === id);
  const result = [...tools];
  if (index >= 0) result[index] = { ...result[index], ...next };
  else result.push(next);
  return result;
}

function toolsOfTurn(events: EventView[]): ToolCall[] {
  let tools: ToolCall[] = [];
  for (const event of events) {
    if (event.type === "tool_start") tools = applyToolStart(tools, event.data);
    else if (event.type === "tool_end") tools = applyToolEnd(tools, event.data);
  }
  return tools;
}

function clarificationsOfTurn(events: EventView[]): Clarification[][] {
  const groups: Clarification[][] = [];
  for (const event of events) {
    if (event.type !== "clarification") continue;
    const questions = clarificationsOf(event.data.questions);
    if (questions.length > 0) groups.push(questions);
  }
  return groups;
}

function queryEvent<T>(events: EventView[], type: string): T | undefined {
  return [...events].reverse().find((event) => event.type === type)?.data as
    | T
    | undefined;
}

function ResultTable({
  result,
  limit,
}: {
  result: QueryResult;
  limit?: number;
}) {
  return (
    <div className="result-table-scroll">
      <table>
        <thead>
          <tr>
            {result.columns.map((column, index) => (
              <th key={index}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.rows.slice(0, limit).map((row, index) => (
            <tr key={index}>
              {row.map((value, column) => (
                <td key={column}>{value == null ? "" : String(value)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Plain HTTP deployments may deny the async clipboard API.
    }
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    area.remove();
  }
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

async function readSse(
  response: Response,
  onEvent: (event: string, data: Record<string, unknown>) => void,
) {
  if (!response.body) throw new Error("Streaming is not supported");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const parsed = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
        onEvent(parsed.event, parsed.data as Record<string, unknown>);
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  const parsed = parseSseBlock(buffer);
  if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
    onEvent(parsed.event, parsed.data as Record<string, unknown>);
  }
}

function ToolList({
  tools,
  messages,
}: {
  tools: ToolCall[];
  messages: Messages;
}) {
  const [expanded, setExpanded] = useState(false);
  const failures = tools.filter((tool) => tool.status === "error").length;
  useEffect(() => {
    if (failures) setExpanded(true);
  }, [failures]);
  if (tools.length === 0) return null;
  const running = tools.find((tool) => tool.status === "running");
  return (
    <section aria-label={messages.toolCalls} className="tool-log">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        已使用 {tools.length} 个工具 {expanded ? "▾" : "▸"}
      </button>
      {running && <span>正在{toolLabel(running.name)}…</span>}
      {expanded && (
        <>
          {tools.map((tool) => (
            <details
              className="tool-step"
              key={tool.id}
              open={tool.status === "error" ? true : undefined}
            >
              <summary title={`${tool.name} ${tool.summary}`}>
                <span className="tool-name">{toolLabel(tool.name)}</span>
                <code className="tool-raw">{tool.name}</code>
                <span className="tool-summary">{tool.summary || "—"}</span>
                <span className={`tool-status ${tool.status}`}>
                  {tool.status === "running"
                    ? messages.toolRunning
                    : tool.status === "error"
                      ? messages.toolFailed
                      : messages.toolDone}
                </span>
              </summary>
              <dl className="tool-detail">
                <dt>{messages.parameters}</dt>
                <dd>{tool.input || tool.summary || "—"}</dd>
                <dt>{messages.result}</dt>
                <dd>{tool.result || tool.summary || "—"}</dd>
              </dl>
            </details>
          ))}
        </>
      )}
    </section>
  );
}

function toolLabel(name: string) {
  const base = name.replace(/^mcp__[^_]+__/, "");
  const labels: Record<string, string> = {
    search_knowledge: "检索知识",
    describe_tables: "查看表结构",
    get_lineage: "查看血缘",
    validate_sql: "校验 SQL",
    run_sql: "执行查询",
    get_capabilities: "读取能力",
    ask_dataagent: "委托问答",
  };
  return labels[base] ?? base.replace(/_/g, " ");
}

function SqlList({
  blocks,
  messages,
  copied,
  onCopy,
}: {
  blocks: SqlBlock[];
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
}) {
  if (blocks.length === 0) return null;
  return (
    <section aria-label={messages.sqlBlocks}>
      {blocks.map((block, index) => (
        <div className="sql-block" key={`${index}-${block.sql.slice(0, 40)}`}>
          <div className="sql-block-head">
            <span>{block.dialect || messages.sqlBlocks}</span>
            <Button type="text" onClick={() => onCopy(block.sql)}>
              {copied === block.sql ? messages.sqlCopied : messages.copySql}
            </Button>
          </div>
          <pre aria-label={messages.sqlBlocks}>
            <code>
              <HighlightedSql sql={block.sql} />
            </code>
          </pre>
          <span className="sr-only">{block.sql}</span>
        </div>
      ))}
    </section>
  );
}

function CitationList({
  citations,
  messages,
  onOpen,
}: {
  citations: Citation[];
  messages: Messages;
  onOpen: (ref: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const groups = mergeCitations(citations);
  if (groups.length === 0) return null;
  const visible = expanded ? groups : groups.slice(0, 6);
  const countOf = (citation: CitationGroup) =>
    citation.label.kind === "table"
      ? messages.dataTables
      : citation.label.kind === "document"
        ? messages.knowledgeDocuments
        : citation.label.kind === "lineage"
          ? messages.contextLineage
          : messages.citations;
  return (
    <section aria-label={messages.citations}>
      <div className="citation-list">
        {visible.map((citation) => (
          <button
            type="button"
            className={`citation-chip ${citation.label.kind}`}
            key={citation.key}
            aria-label={citation.openRef}
            title={citation.label.title}
            onClick={() => onOpen(citation.openRef)}
          >
            <span className="citation-kind">{countOf(citation)}</span>
            {citation.label.category && (
              <span className="citation-category">
                {citation.label.category}
              </span>
            )}
            <span className="citation-label">{citation.label.label}</span>
          </button>
        ))}
        {groups.length > 6 && (
          <button
            type="button"
            className="citation-chip citation-more"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded
              ? messages.close
              : `${messages.showMore} ${groups.length - 6} ${messages.moreCount}`}
          </button>
        )}
      </div>
    </section>
  );
}

function ClarificationList({
  groups,
  messages,
  disabled,
  onSelect,
}: {
  groups: Clarification[][];
  messages: Messages;
  disabled: boolean;
  onSelect: (
    question: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) => void;
}) {
  if (groups.length === 0) return null;
  return (
    <section aria-label={messages.clarification}>
      <h4 className="context-section-title">{messages.clarification}</h4>
      <p className="muted">{messages.clarificationHint}</p>
      {groups.map((questions, index) => (
        <div key={index}>
          {questions.map((question, questionIndex) => (
            <article key={question.id ?? questionIndex}>
              <p>{question.question}</p>
              <Space wrap>
                {(question.options ?? []).map((option, optionIndex) => (
                  <Button
                    key={
                      option.id ??
                      `${option.label}-${option.ref}-${optionIndex}`
                    }
                    disabled={disabled}
                    onClick={() => onSelect(question, option)}
                  >
                    <span>{option.label}</span>
                    {option.ref && (
                      <>
                        {" "}
                        <code>{safeDecodePath(option.ref)}</code>
                      </>
                    )}
                  </Button>
                ))}
              </Space>
            </article>
          ))}
        </div>
      ))}
    </section>
  );
}

function TurnBlock({
  turn,
  messages,
  copied,
  onCopy,
  onOpen,
  onClarify,
  onExecute,
  onDownload,
  onCopyLink,
  executing,
}: {
  turn: Turn;
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
  onOpen: (ref: string) => void;
  onClarify: (
    question: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) => void;
  onExecute: (turn: Turn, plan: QueryPlan) => void;
  onDownload: (id: string, format: string) => void;
  onCopyLink: (id: string) => void;
  executing: boolean;
}) {
  const plan = queryEvent<QueryPlan>(turn.events ?? [], "query_plan");
  const result = queryEvent<QueryResult>(turn.events ?? [], "query_result");
  const awaitingPlan =
    plan !== undefined && turn.status === "awaiting_confirmation";
  const strippedAnswer = stripCitationRefs(turn.answer ?? "");
  const answer = awaitingPlan
    ? stripMatchingSql(strippedAnswer.text, plan?.sql)
    : strippedAnswer.text;
  const citations = [
    ...citationsOf(turn.citations),
    ...citationsFromRefs(strippedAnswer.refs),
  ];
  const tools = toolsOfTurn(turn.events ?? []);
  const clarifications = clarificationsOfTurn(turn.events ?? []);
  const planSql = plan ? normalizeSql(plan.sql) : "";
  const sql = sqlBlocks(turn.sql).filter(
    (block) => !awaitingPlan || normalizeSql(block.sql) !== planSql,
  );
  return (
    <article className="turn">
      <div className="message">
        <span className="avatar user" aria-hidden="true">
          我
        </span>
        <div className="message-body">
          <QuestionText text={turn.question} messages={messages} />
        </div>
      </div>
      <div className="message">
        <span className="avatar ai" aria-hidden="true">
          AI
        </span>
        <div className="message-body">
          <ToolList tools={tools} messages={messages} />
          {awaitingPlan && plan && (
            <section className="card query-confirmation">
              <SqlList
                blocks={[{ sql: plan.sql }]}
                messages={messages}
                copied={copied}
                onCopy={onCopy}
              />
              <p className="query-confirmation-hint">
                {messages.confirmExecuteHint}
              </p>
              <Button
                type="primary"
                disabled={executing}
                onClick={() => onExecute(turn, plan)}
              >
                {executing
                  ? messages.confirmExecuting
                  : messages.confirmExecute}
              </Button>
            </section>
          )}
          {result && (
            <div className="card query-result-preview">
              <ResultTable result={result} limit={20} />
              <p>共 {result.row_count} 行，已显示前 20 行</p>
              {plan &&
                result.row_count >=
                  Math.min(
                    plan.row_limit,
                    plan.execution_limit ?? plan.row_limit,
                  ) && <p>结果可能被截断</p>}
              <Space wrap>
                {[
                  ["csv", "CSV"],
                  ["xlsx", "Excel"],
                  ["json", "JSON"],
                ].map(([fmt, label]) => (
                  <Button key={fmt} onClick={() => onDownload(result.id, fmt)}>
                    下载 {label}
                  </Button>
                ))}
                <Button onClick={() => onCopyLink(result.id)}>复制链接</Button>
              </Space>
              <p className="muted">{messages.downloadLinkHint}</p>
            </div>
          )}
          <div className="answer">
            <Markdown text={answer} />
          </div>
          {turn.error && <Alert type="error" title={turn.error} />}
          <SqlList
            blocks={sql}
            messages={messages}
            copied={copied}
            onCopy={onCopy}
          />
          <CitationList
            citations={citations}
            messages={messages}
            onOpen={onOpen}
          />
          <ClarificationList
            groups={clarifications}
            messages={messages}
            disabled={false}
            onSelect={onClarify}
          />
        </div>
      </div>
    </article>
  );
}

function QuestionText({
  text,
  messages,
}: {
  text: string;
  messages: Messages;
}) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(text.split("\n").length > 6);
  const textRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    setExpanded(false);
    setOverflowing(text.split("\n").length > 6);
  }, [text]);
  useEffect(() => {
    const element = textRef.current;
    if (!element || expanded) return;
    const measure = () => {
      if (element.scrollHeight > element.clientHeight + 1) setOverflowing(true);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text, expanded]);
  return (
    <>
      <p ref={textRef} className={`question-text${expanded ? "" : " clamped"}`}>
        {text}
      </p>
      {(overflowing || expanded) && (
        <button
          type="button"
          className="question-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? messages.collapseMessage : messages.expandMessage}
        </button>
      )}
    </>
  );
}

export function ChatPage({
  token,
  role,
  messages: t,
  settingsOnly = false,
  sessionTarget,
  newTarget,
  onNavigateChat,
  refreshKey = 0,
}: {
  token: string;
  role: string;
  messages: Messages;
  settingsOnly?: boolean;
  sessionTarget?: HTMLElement | null;
  newTarget?: HTMLElement | null;
  onNavigateChat?: () => void;
  language?: Language;
  onChangeLanguage?: () => void;
  refreshKey?: number;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<AgentProfiles>({});
  const openedRecent = useRef(false);
  const selectionTouched = useRef(false);
  const askSequence = useRef(0);
  const [defaults, setDefaults] = useState({
    agent: "pi",
    provider: "",
    model: "",
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [hoveredConversationId, setHoveredConversationId] = useState("");
  const [focusedConversationId, setFocusedConversationId] = useState("");
  const [confirmingDeleteId, setConfirmingDeleteId] = useState("");
  const [deletingConversationId, setDeletingConversationId] = useState("");
  const [sessionError, setSessionError] = useState("");
  const [agentId, setAgentId] = useState("pi");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [currentConversationId, setCurrentConversationId] = useState("");
  const [resumable, setResumable] = useState(true);
  const [sending, setSending] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [citationRef, setCitationRef] = useState("");
  const citationSequence = useRef(0);
  const [citationDetail, setCitationDetail] = useState<CitationDetail | null>(
    null,
  );
  const [citationError, setCitationError] = useState("");
  const [citationLoading, setCitationLoading] = useState(false);
  const [providerForm, setProviderForm] = useState({
    name: "",
    type: "anthropic-messages",
    base_url: "",
    api_key: "",
    models: "",
  });
  const [editingProviderId, setEditingProviderId] = useState("");
  const [providerError, setProviderError] = useState("");
  const [providerNotice, setProviderNotice] = useState("");

  async function executePlan(turn: Turn, plan: QueryPlan) {
    if (sending || !currentConversationId) return;
    setSending(true);
    setError("");
    setTurns((current) =>
      current.map((item) =>
        item.id === turn.id ? { ...item, error: null } : item,
      ),
    );
    try {
      const response = await fetch(
        `/api/v1/conversations/${encodeURIComponent(currentConversationId)}/turns/${turn.id}/queries/${encodeURIComponent(plan.id)}/execute`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        throw new Error(body?.detail || t.askError);
      }
      await readSse(response, (name, data) => {
        if (name === "query_result") {
          setTurns((current) =>
            current.map((item) => {
              if (item.id !== turn.id) return item;
              const events = item.events ?? [];
              return {
                ...item,
                status: "running",
                events: [...events, { seq: events.length, type: name, data }],
              };
            }),
          );
        } else if (name === "done") {
          setTurns((current) =>
            current.map((item) =>
              item.id === turn.id
                ? {
                    ...item,
                    status: "completed",
                    answer:
                      typeof data.text === "string" ? data.text : item.answer,
                  }
                : item,
            ),
          );
        } else if (name === "error") {
          const message = String(data.message ?? t.answerFailed);
          setError(message);
          setTurns((current) =>
            current.map((item) =>
              item.id === turn.id ? { ...item, error: message } : item,
            ),
          );
        }
      });
      await openConversation(currentConversationId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : t.askError;
      setError(message);
      setTurns((current) =>
        current.map((item) =>
          item.id === turn.id ? { ...item, error: message } : item,
        ),
      );
    } finally {
      setSending(false);
    }
  }

  async function downloadLinks(id: string): Promise<Record<string, string>> {
    const response = await fetch(
      `/api/v1/queries/${encodeURIComponent(id)}/download-links`,
      { method: "POST", headers: { Authorization: `Bearer ${token}` } },
    );
    if (!response.ok) throw new Error(t.askError);
    const paths = (await response.json()) as Record<string, string>;
    return Object.fromEntries(
      Object.entries(paths).map(([format, path]) => [
        format,
        new URL(path, window.location.origin).href,
      ]),
    );
  }
  async function downloadQuery(id: string, format: string) {
    try {
      const links = await downloadLinks(id);
      const anchor = document.createElement("a");
      anchor.href = links[format];
      anchor.rel = "noreferrer";
      anchor.click();
    } catch {
      setError(t.askError);
    }
  }
  async function copyDownloadLink(id: string) {
    try {
      const links = await downloadLinks(id);
      await copyText(links.csv);
    } catch {
      setError(t.copyError);
    }
  }

  const refreshConversations = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/conversations", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const rows = (await response.json()) as Conversation[];
      if (!Array.isArray(rows)) throw new Error("Invalid conversation list");
      setConversations(rows);
      if (!openedRecent.current && !settingsOnly) {
        if (rows.length) {
          openedRecent.current = true;
          void openConversation(rows[0].id);
        }
      }
    } catch {
      setError(t.askError);
    }
  }, [token, t.askError, settingsOnly]);

  const loadProviders = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const data = (await response.json()) as Provider[];
      setProviders(data);
      setProviderId((current) => current || data[0]?.id || "");
      setModel((current) => current || data[0]?.models[0] || "");
    } catch {
      setProviderError(t.providerError);
    }
  }, [token, t.providerError]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/v1/agents", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
      fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
    ])
      .then(async ([agentsResponse, providersResponse]) => {
        if (!agentsResponse.ok || !providersResponse.ok) throw new Error();
        const agentRows = (await agentsResponse.json()) as Agent[];
        const providerRows = (await providersResponse.json()) as Provider[];
        if (!Array.isArray(agentRows) || !Array.isArray(providerRows))
          throw new Error("Invalid agent or provider list");
        setAgents(agentRows);
        const available = agentRows.find((agent) => agent.available);
        if (available && !selectionTouched.current)
          setAgentId((current) =>
            agentRows.some((agent) => agent.id === current && agent.available)
              ? current
              : available.id,
          );
        setProviders(providerRows);
        setProviderId((current) => current || providerRows[0]?.id || "");
        setModel((current) => current || providerRows[0]?.models[0] || "");
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(t.askError);
      });
    return () => controller.abort();
  }, [token, t.askError]);

  useEffect(() => {
    if (!settingsOnly) void refreshConversations();
  }, [refreshConversations, settingsOnly, refreshKey]);

  useEffect(() => {
    void fetch("/api/v1/assistant-defaults", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as {
          assistant_agent?: string | null;
          assistant_provider_id?: string | null;
          assistant_model?: string | null;
          assistant_agent_profiles?: string | null;
        };
        const parsedProfiles = parseAgentProfiles(
          data.assistant_agent_profiles,
        );
        setProfiles(parsedProfiles);
        const agent = data.assistant_agent || "pi";
        const profile = parsedProfiles[agent];
        const next = {
          agent,
          provider:
            profile?.default_provider || data.assistant_provider_id || "",
          model: profile?.default_model || data.assistant_model || "",
        };
        setDefaults(next);
        if (!openedRecent.current && !selectionTouched.current) {
          setAgentId(next.agent);
          if (next.provider) setProviderId(next.provider);
          if (next.model) setModel(next.model);
        }
      })
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    if (selectionTouched.current || openedRecent.current || !providers.length)
      return;
    const profile = profiles[defaults.agent];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === defaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(defaults.model)
        ? defaults.model
        : chosen?.models[0] || "",
    );
  }, [providers, profiles, defaults]);

  const activeProvider = providers.find((item) => item.id === providerId);

  function selectAgent(value: string) {
    selectionTouched.current = true;
    setAgentId(value);
    const profile = profiles[value];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === profile?.default_provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      profile?.default_model && chosen?.models.includes(profile.default_model)
        ? profile.default_model
        : chosen?.models[0] || "",
    );
  }

  function selectProvider(value: string) {
    selectionTouched.current = true;
    setProviderId(value);
    const provider = providers.find((item) => item.id === value);
    setModel(provider?.models[0] || "");
  }

  function updateLive(change: (current: LiveTurn) => LiveTurn) {
    setLive((current) => (current ? change(current) : current));
  }

  async function submitPrompt(prompt: string) {
    const trimmed = prompt.trim();
    if (!trimmed || !providerId || !model || sending) return;
    selectionTouched.current = true;
    const sequence = ++askSequence.current;
    let conversationId = currentConversationId;
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setSending(true);
    setError("");
    setLive({
      question: trimmed,
      text: "",
      tools: [],
      sql: [],
      citations: [],
      clarifications: [],
      status: "running",
    });
    try {
      const response = await fetch("/api/v1/conversations/ask", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: prompt,
          agent: agentId,
          provider_id: providerId,
          model,
          ...(currentConversationId
            ? { conversation_id: currentConversationId }
            : {}),
        }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        throw new Error(body?.detail || t.askError);
      }
      await readSse(response, (name, data) => {
        if (
          name === "conversation" &&
          typeof data.conversation_id === "string"
        ) {
          // Keep the live answer mounted; reloading the list must not replace
          // it with a partially persisted turn.
          openedRecent.current = true;
          conversationId = data.conversation_id;
          setCurrentConversationId(data.conversation_id);
        } else if (name === "text_delta" && typeof data.text === "string") {
          updateLive((current) => ({
            ...current,
            text: current.text + String(data.text),
          }));
        } else if (name === "tool_start") {
          updateLive((current) => ({
            ...current,
            tools: applyToolStart(current.tools, data),
          }));
        } else if (name === "tool_end") {
          updateLive((current) => {
            return { ...current, tools: applyToolEnd(current.tools, data) };
          });
        } else if (name === "clarification") {
          const questions = clarificationsOf(data.questions);
          if (questions.length > 0) {
            updateLive((current) => ({
              ...current,
              clarifications: [...current.clarifications, questions],
            }));
          }
        } else if (name === "done") {
          updateLive((current) => ({
            ...current,
            text: typeof data.text === "string" ? data.text : current.text,
            sql: sqlBlocks(data.sql as unknown[] | undefined),
            citations: citationsOf(data.citations as unknown[] | undefined),
            status: "completed",
          }));
        } else if (name === "error") {
          updateLive((current) => ({
            ...current,
            status: "failed",
            error: String(data.message ?? t.answerFailed),
          }));
        }
      });
      await refreshConversations();
      if (conversationId) {
        await loadCompletedConversation(conversationId, sequence);
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : t.askError;
      setError(message);
      updateLive((current) => ({
        ...current,
        status: "failed",
        error: message,
      }));
    } finally {
      setSending(false);
    }
  }

  function ask(event: FormEvent) {
    event.preventDefault();
    const prompt = question.trim();
    setQuestion("");
    void submitPrompt(prompt);
  }

  function chooseClarification(
    clarification: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) {
    const label = option.label?.trim() ?? "";
    const ref = option.ref?.trim() ?? "";
    if (!label || !ref) return;
    const prompt = [
      "澄清选择：请只针对当前问题按所选口径继续回答，不要把它作为全局规则。",
      `问题：${clarification.question ?? ""}`,
      `选项：${label}`,
      `证据：${ref}`,
    ].join("\n");
    void submitPrompt(prompt);
  }

  async function openConversation(id: string) {
    setLoadingConversation(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/conversations/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const detail = (await response.json()) as ConversationDetail;
      applyConversation(detail);
    } catch {
      setError(t.askError);
    } finally {
      setLoadingConversation(false);
    }
  }

  function applyConversation(detail: ConversationDetail) {
    selectionTouched.current = true;
    if (detail.id !== currentConversationId) {
    }
    setCurrentConversationId(detail.id);
    setResumable(detail.resumable !== false);
    setTurns(detail.turns);
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setLive(null);
    setAgentId(detail.agent);
    setProviderId(detail.provider_id);
    setModel(detail.model);
  }

  async function loadCompletedConversation(id: string, sequence: number) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        const response = await fetch(`/api/v1/conversations/${id}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) return;
        const detail = (await response.json()) as ConversationDetail;
        if (sequence !== askSequence.current) return;
        const last = detail.turns.at(-1);
        if (last && last.status !== "running") {
          applyConversation(detail);
          return;
        }
      } catch {
        return;
      }
      await new Promise((resolve) =>
        window.setTimeout(resolve, 100 * (attempt + 1)),
      );
    }
  }

  function newConversation() {
    selectionTouched.current = true;
    setCurrentConversationId("");
    setAgentId(defaults.agent);
    const profile = profiles[defaults.agent];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === defaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(defaults.model)
        ? defaults.model
        : chosen?.models[0] || "",
    );
    setResumable(true);
    setTurns([]);
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setLive(null);
    setError("");
  }

  async function openCitation(ref: string) {
    const sequence = ++citationSequence.current;
    setCitationRef(ref);
    setCitationDetail(null);
    setCitationError("");
    setCitationLoading(true);
    const type = ref.split(":", 1)[0];
    const value = ref.slice(ref.indexOf(":") + 1);
    try {
      if (type === "table" || type === "field") {
        const parts = tablePartsFromRef(ref);
        if (!parts) throw new Error();
        const table = await loadTableDetail(token, parts.database, parts.name);
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "table", ref, table });
      } else if (type === "document") {
        const docId = `document:${value.split("#", 1)[0]}`;
        const response = await fetch(
          `/api/v1/document?${new URLSearchParams({ doc_id: docId })}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok) throw new Error();
        const document = (await response.json()) as DocumentView;
        if (sequence !== citationSequence.current) return;
        setCitationDetail({ kind: "document", ref, document });
      } else {
        setCitationDetail({
          kind: "message",
          ref,
          message: t.citationUnsupported,
        });
      }
    } catch {
      if (sequence === citationSequence.current)
        setCitationError(t.citationNotFound);
    } finally {
      if (sequence === citationSequence.current) setCitationLoading(false);
    }
  }

  async function copySql(sql: string) {
    try {
      await copyText(sql);
      setCopied(sql);
    } catch {
      setError(t.copyError);
    }
  }

  function editProvider(provider: Provider) {
    setEditingProviderId(provider.id);
    setProviderForm({
      name: provider.name,
      type: provider.type,
      base_url: provider.base_url,
      api_key: "",
      models: provider.models.join(", "),
    });
    setProviderError("");
    setProviderNotice("");
  }

  function resetProviderForm() {
    setEditingProviderId("");
    setProviderForm({
      name: "",
      type: "anthropic-messages",
      base_url: "",
      api_key: "",
      models: "",
    });
  }

  async function saveProvider(event: FormEvent) {
    event.preventDefault();
    setProviderError("");
    setProviderNotice("");
    const models = providerForm.models
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const payload: Record<string, unknown> = {
      name: providerForm.name.trim(),
      type: providerForm.type,
      base_url: providerForm.base_url.trim(),
      models,
    };
    if (providerForm.api_key.trim()) payload.api_key = providerForm.api_key;
    try {
      const response = await fetch(
        editingProviderId
          ? `/api/v1/providers/${editingProviderId}`
          : "/api/v1/providers",
        {
          method: editingProviderId ? "PUT" : "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) throw new Error();
      resetProviderForm();
      setProviderNotice(t.providerSaved);
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteProvider(provider: Provider) {
    if (!window.confirm(t.providerDeleteConfirm)) return;
    setProviderError("");
    try {
      const response = await fetch(`/api/v1/providers/${provider.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      if (providerId === provider.id) {
        setProviderId("");
        setModel("");
      }
      if (editingProviderId === provider.id) resetProviderForm();
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteConversation(id: string) {
    if (deletingConversationId) return;
    setDeletingConversationId(id);
    setSessionError("");
    try {
      const response = await fetch(
        `/api/v1/conversations/${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (response.status === 409) {
        setSessionError(t.deleteConversationRunning);
        return;
      }
      if (!response.ok) {
        setSessionError(t.deleteConversationFailed);
        return;
      }
      setConversations((current) =>
        current.filter((conversation) => conversation.id !== id),
      );
      if (id === currentConversationId) newConversation();
    } catch {
      setSessionError(t.deleteConversationFailed);
    } finally {
      setDeletingConversationId("");
      setConfirmingDeleteId("");
    }
  }

  const canAsk = Boolean(
    question.trim() && providerId && model && !sending && resumable,
  );

  const currentConversation = conversations.find(
    (conversation) => conversation.id === currentConversationId,
  );
  const selectedTable =
    citationDetail?.kind === "table" ? citationDetail.table : null;
  const citationHeading = citationRef
    ? citationLabel(citationDetail?.ref ?? citationRef)
    : null;
  const shortTime = (value: string) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString(undefined, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  };
  const closeCitation = () => {
    citationSequence.current += 1;
    setCitationRef("");
    setCitationDetail(null);
    setCitationError("");
  };
  const emptyConversation = !loadingConversation && turns.length === 0 && !live;
  const liveAnswer = live
    ? stripCitationRefs(live.text)
    : { text: "", refs: [] };
  const liveCitations = live
    ? [...live.citations, ...citationsFromRefs(liveAnswer.refs)]
    : [];

  return (
    <section style={{ height: "100%", minHeight: 0 }}>
      {!settingsOnly && (
        <div
          className={`chat-layout${!citationRef ? " context-hidden" : ""}${emptyConversation ? " chat-layout--empty" : ""}`}
        >
          {newTarget &&
            createPortal(
              <Button
                type="text"
                className="nav-item new-conversation"
                icon={<PlusOutlined />}
                aria-label={t.newConversation}
                title={t.newConversation}
                onClick={() => {
                  newConversation();
                  onNavigateChat?.();
                }}
                disabled={sending}
              >
                {t.newConversation}
              </Button>,
              newTarget,
            )}
          {(() => {
            const sessions = (
              <aside className="chat-sessions">
                <div className="session-head">
                  <h1 className="session-title">
                    {sessionTarget ? t.conversationHistory : t.chat}
                  </h1>
                  {!sessionTarget && (
                    <Button
                      size="small"
                      type="primary"
                      onClick={newConversation}
                      disabled={sending}
                    >
                      {t.newConversation}
                    </Button>
                  )}
                </div>
                {sessionError && (
                  <div className="session-delete-error" role="alert">
                    {sessionError}
                  </div>
                )}
                <div className="session-list">
                  {conversations.length === 0 ? (
                    <div className="session-empty">{t.noConversations}</div>
                  ) : (
                    conversations.map((conversation) => {
                      const active = conversation.id === currentConversationId;
                      const revealed =
                        hoveredConversationId === conversation.id ||
                        focusedConversationId === conversation.id;
                      const confirming = confirmingDeleteId === conversation.id;
                      const deleting =
                        deletingConversationId === conversation.id;
                      return (
                        <div
                          className={`session-item${active ? " active" : ""}`}
                          key={conversation.id}
                          onMouseEnter={() =>
                            setHoveredConversationId(conversation.id)
                          }
                          onMouseLeave={() => setHoveredConversationId("")}
                          onFocus={() =>
                            setFocusedConversationId(conversation.id)
                          }
                          onBlur={() => setFocusedConversationId("")}
                        >
                          <div className="session-row">
                            {confirming ? (
                              <span className="session-delete-actions">
                                <button
                                  type="button"
                                  className="session-delete-confirm"
                                  onClick={() =>
                                    void deleteConversation(conversation.id)
                                  }
                                  disabled={deleting}
                                >
                                  {t.confirmDeleteConversation}
                                </button>
                                <button
                                  type="button"
                                  className="session-delete-cancel"
                                  onClick={() => setConfirmingDeleteId("")}
                                  disabled={deleting}
                                >
                                  {t.cancelDeleteConversation}
                                </button>
                              </span>
                            ) : (
                              <>
                                <button
                                  type="button"
                                  className="session-open"
                                  title={conversation.title}
                                  onClick={() => {
                                    onNavigateChat?.();
                                    void openConversation(conversation.id);
                                  }}
                                  disabled={sending}
                                >
                                  {conversation.title}
                                  {conversation.resumable === false &&
                                    ` (${t.sessionExpired})`}
                                </button>
                                <button
                                  type="button"
                                  className={`session-delete${revealed ? " visible" : ""}`}
                                  aria-label={t.deleteConversation}
                                  title={t.deleteConversation}
                                  onClick={() => {
                                    setSessionError("");
                                    setConfirmingDeleteId(conversation.id);
                                  }}
                                  disabled={sending && active}
                                >
                                  <DeleteOutlined />
                                </button>
                              </>
                            )}
                          </div>
                          <span className="session-meta">
                            <span>{shortTime(conversation.updated_at)}</span>
                          </span>
                        </div>
                      );
                    })
                  )}
                </div>
              </aside>
            );
            return sessionTarget
              ? createPortal(sessions, sessionTarget)
              : sessions;
          })()}
          <div className="chat-conversation">
            <header className="chat-header">
              <div className="chat-header-title">
                <strong>
                  {currentConversation?.title || t.newConversation}
                </strong>
                <span>
                  {agentId} · {activeProvider?.name || providerId || "—"} ·{" "}
                  {model || "—"}
                </span>
              </div>
            </header>
            <div className="chat-scroll">
              {error && <Alert type="error" title={error} />}
              {!resumable && <Alert type="info" title={t.sessionExpired} />}
              {loadingConversation && (
                <p className="muted">{t.loadingConversation}</p>
              )}

              {turns.map((turn) => (
                <TurnBlock
                  key={turn.id}
                  turn={turn}
                  messages={t}
                  copied={copied}
                  onCopy={(sql) => void copySql(sql)}
                  onOpen={(ref) => {
                    void openCitation(ref);
                  }}
                  onClarify={chooseClarification}
                  onExecute={(turn, plan) => void executePlan(turn, plan)}
                  onDownload={(id, format) => void downloadQuery(id, format)}
                  onCopyLink={(id) => void copyDownloadLink(id)}
                  executing={sending}
                />
              ))}
              {live && (
                <article className="turn">
                  <div className="message">
                    <span className="avatar user" aria-hidden="true">
                      我
                    </span>
                    <div className="message-body">
                      <QuestionText text={live.question} messages={t} />
                    </div>
                  </div>
                  <div className="message">
                    <span className="avatar ai" aria-hidden="true">
                      AI
                    </span>
                    <div className="message-body">
                      <ToolList tools={live.tools} messages={t} />
                      <div className="answer">
                        {liveAnswer.text ? (
                          <Markdown text={liveAnswer.text} />
                        ) : (
                          <p className="muted">
                            {live.status === "running" ? t.asking : ""}
                          </p>
                        )}
                      </div>
                      <SqlList
                        blocks={live.sql}
                        messages={t}
                        copied={copied}
                        onCopy={(sql) => void copySql(sql)}
                      />
                      <CitationList
                        citations={liveCitations}
                        messages={t}
                        onOpen={(ref) => {
                          void openCitation(ref);
                        }}
                      />
                      <ClarificationList
                        groups={live.clarifications}
                        messages={t}
                        disabled={sending}
                        onSelect={chooseClarification}
                      />
                      {live.error && <Alert type="error" title={live.error} />}
                    </div>
                  </div>
                </article>
              )}
            </div>
            <div className="chat-composer">
              {emptyConversation && (
                <h1 className="chat-greeting">{t.chatGreeting}</h1>
              )}
              <form
                className="composer-box"
                onSubmit={(event) => void ask(event)}
              >
                <Input.TextArea
                  aria-label={t.askQuestion}
                  value={question}
                  placeholder={t.askPlaceholder}
                  autoSize={{ minRows: 1, maxRows: 5 }}
                  onChange={(event) => setQuestion(event.target.value)}
                  disabled={!resumable}
                />
                <div className="composer-controls">
                  <div className="chat-selectors">
                    <label>
                      <select
                        aria-label={t.askAgent}
                        title={t.askAgent}
                        value={agentId}
                        disabled={Boolean(currentConversationId)}
                        onChange={(event) => selectAgent(event.target.value)}
                      >
                        {agents.map((agent) => (
                          <option
                            key={agent.id}
                            value={agent.id}
                            disabled={!agent.available}
                          >
                            {agent.name}
                            {agent.available ? "" : ` (${t.unavailable})`}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <select
                        aria-label={t.askProvider}
                        title={t.askProvider}
                        value={providerId}
                        disabled={Boolean(currentConversationId)}
                        onChange={(event) => selectProvider(event.target.value)}
                      >
                        {providers.length === 0 && (
                          <option value="">{t.noProviders}</option>
                        )}
                        {providers
                          .filter(
                            (provider) =>
                              !profiles[agentId]?.providers.length ||
                              profiles[agentId].providers.includes(provider.id),
                          )
                          .map((provider) => (
                            <option key={provider.id} value={provider.id}>
                              {provider.name}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      <select
                        aria-label={t.askModel}
                        title={t.askModel}
                        value={model}
                        onChange={(event) => {
                          selectionTouched.current = true;
                          setModel(event.target.value);
                        }}
                        disabled={
                          !activeProvider || Boolean(currentConversationId)
                        }
                      >
                        {(activeProvider?.models ?? []).map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <Button
                    htmlType="submit"
                    type="primary"
                    className="send-button"
                    aria-label={t.askSubmit}
                    title={t.askSubmit}
                    disabled={!canAsk}
                  >
                    <ArrowUpOutlined />
                  </Button>
                </div>
              </form>
              {emptyConversation && (
                <div className="chat-examples">
                  <span className="example-title">{t.exampleQuestions}</span>
                  <div className="example-chips">
                    {t.examples.slice(0, 3).map((example) => (
                      <Button
                        key={example}
                        onClick={() => setQuestion(example)}
                      >
                        {example}
                      </Button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
          {citationRef && (
            <aside className="chat-context">
              <div className="context-header">
                <div className="context-heading">
                  <strong title={citationHeading?.title}>
                    {citationHeading?.label ?? t.citations}
                  </strong>
                  {citationDetail?.kind === "document" && (
                    <div className="context-ref">
                      {safeDecodePath(citationDetail.document.path)}
                    </div>
                  )}
                </div>
                <Button
                  type="text"
                  aria-label={t.close}
                  onClick={closeCitation}
                >
                  <CloseOutlined />
                </Button>
              </div>
              <div className="context-body">
                {citationLoading && <p>{t.loadingCitation}</p>}
                {citationError && <Alert type="error" title={citationError} />}
                {selectedTable ? (
                  <div className="context-schema">
                    <div className="context-schema-head">
                      <div className="context-schema-title">
                        <strong>
                          {safeDecodePath(
                            `${selectedTable.database}.${selectedTable.name}`,
                          )}
                        </strong>
                        {selectedTable.comment && (
                          <span className="muted">{selectedTable.comment}</span>
                        )}
                      </div>
                    </div>
                    <div className="context-schema-body">
                      <table className="schema-table">
                        <thead>
                          <tr>
                            <th>{t.field}</th>
                            <th>{t.dataType}</th>
                            <th>{t.comment}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedTable.fields.map((field) => (
                            <tr key={field.name}>
                              <td>{field.name}</td>
                              <td>{field.type}</td>
                              <td>
                                {field.comment}
                                {field.partition ? ` · ${t.partition}` : ""}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {citationRef.startsWith("field:") ? (
                        <ColumnLineage
                          token={token}
                          table={`${selectedTable.database}.${selectedTable.name}`}
                          column={citationRef
                            .slice(6)
                            .split(".")
                            .slice(2)
                            .join(".")}
                        />
                      ) : (
                        <TaskLineage
                          token={token}
                          messages={t}
                          table={`${selectedTable.database}.${selectedTable.name}`}
                          showTasks={false}
                        />
                      )}
                    </div>
                  </div>
                ) : citationDetail?.kind === "document" ? (
                  <article className="context-document">
                    <div className="doc-content">
                      {safeDecodePath(citationDetail.document.path)
                        .toLowerCase()
                        .endsWith(".md") ? (
                        <Markdown text={citationDetail.document.content} />
                      ) : (
                        <pre>{citationDetail.document.content}</pre>
                      )}
                    </div>
                  </article>
                ) : (
                  <div className="empty-state">{t.emptyContext}</div>
                )}
                {citationDetail?.kind === "message" && (
                  <Alert type="info" title={citationDetail.message} />
                )}
              </div>
            </aside>
          )}
        </div>
      )}
      {settingsOnly && (
        <div className="stack">
          <div className="page-header">
            <div>
              <h1 className="page-title">{t.providerSettings}</h1>
              <p className="page-subtitle">{t.providerApiKeyHint}</p>
            </div>
          </div>
          {providerError && <Alert type="error" title={providerError} />}
          {providerNotice && <Alert type="success" title={providerNotice} />}
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.providerSettings}</h2>
              <span className="muted">{providers.length}</span>
            </div>
            {providers.length === 0 ? (
              <p className="muted">{t.noProviders}</p>
            ) : (
              <div className="provider-list">
                {providers.map((provider) => (
                  <div className="provider-card" key={provider.id}>
                    <div className="provider-line">
                      <strong>{provider.name}</strong>
                      <span className="subtle">{provider.type}</span>
                      <Tag color={provider.has_api_key ? "green" : "default"}>
                        {provider.has_api_key
                          ? t.providerConfigured
                          : t.providerNotConfigured}
                      </Tag>
                      <code className="muted">{provider.base_url}</code>
                      {role === "admin" && (
                        <>
                          <Button
                            type="link"
                            onClick={() => editProvider(provider)}
                          >
                            {t.providerEdit}
                          </Button>
                          <Button
                            type="link"
                            danger
                            onClick={() => void deleteProvider(provider)}
                          >
                            {t.providerDelete}
                          </Button>
                        </>
                      )}
                    </div>
                    <div className="subtle mono">
                      {provider.models.join(" · ")}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
          {role === "admin" && (
            <section className="card section-card">
              <form onSubmit={(event) => void saveProvider(event)}>
                <div className="card-title-row">
                  <h2 className="card-title">
                    {editingProviderId ? t.providerEdit : t.providerAdd}
                  </h2>
                </div>
                <div className="grid-2">
                  <div className="field">
                    <label className="field-label" htmlFor="provider-name">
                      {t.providerName}
                    </label>
                    <Input
                      id="provider-name"
                      aria-label={t.providerName}
                      value={providerForm.name}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          name: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-type">
                      {t.providerType}
                    </label>
                    <select
                      id="provider-type"
                      aria-label={t.providerType}
                      value={providerForm.type}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          type: event.target.value,
                        })
                      }
                    >
                      {PROVIDER_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-url">
                      {t.providerBaseUrl}
                    </label>
                    <Input
                      id="provider-url"
                      aria-label={t.providerBaseUrl}
                      value={providerForm.base_url}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          base_url: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="provider-key">
                      {t.providerApiKey}
                    </label>
                    <Input.Password
                      id="provider-key"
                      aria-label={t.providerApiKey}
                      value={providerForm.api_key}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          api_key: event.target.value,
                        })
                      }
                    />
                    <span className="field-hint">{t.providerApiKeyHint}</span>
                  </div>
                  <div className="field" style={{ gridColumn: "1 / -1" }}>
                    <label className="field-label" htmlFor="provider-models">
                      {t.providerModels}
                    </label>
                    <Input
                      id="provider-models"
                      aria-label={t.providerModels}
                      value={providerForm.models}
                      onChange={(event) =>
                        setProviderForm({
                          ...providerForm,
                          models: event.target.value,
                        })
                      }
                      required
                    />
                  </div>
                </div>
                <div className="save-bar">
                  <Button htmlType="submit" type="primary">
                    {editingProviderId ? t.providerSave : t.providerAdd}
                  </Button>
                  {editingProviderId && (
                    <Button onClick={resetProviderForm}>{t.cancel}</Button>
                  )}
                </div>
              </form>
            </section>
          )}
        </div>
      )}
    </section>
  );
}
