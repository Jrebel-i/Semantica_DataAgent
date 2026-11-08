import type { components, paths } from "../api";
import type { TableDetail } from "../tableDetail";

export type Agent = components["schemas"]["AgentView"];
export type Provider = components["schemas"]["ProviderView"];
export type ModelCapability = components["schemas"]["ModelCapability"];
export type EffortLevel = NonNullable<ModelCapability["effort_levels"]>[number];
export type ProviderProbeResult = components["schemas"]["ProviderProbeView"];
export type Conversation = components["schemas"]["ConversationView"];
export type Project = components["schemas"]["ProjectView"];
export type ConversationDetail = components["schemas"]["ConversationDetail"];
export type Turn = components["schemas"]["TurnView"];
export type EventView = components["schemas"]["EventView"];
export type DocumentView =
  paths["/api/v1/document"]["get"]["responses"][200]["content"]["application/json"];

export type SqlBlock = { sql: string; dialect?: string };
export type Citation = { ref: string; type: string };
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
export type ToolCall = {
  id: string;
  name: string;
  summary: string;
  status: "running" | "done" | "error";
  input?: string;
  result?: string;
};
export type Clarification = {
  id?: string;
  question?: string;
  options?: { id?: string; label?: string; ref?: string }[];
};
export type QueryPlan = {
  id: string;
  sql: string;
  data_source: string;
  tables: string[];
  estimated_partitions: number | null;
  estimated_rows: number | null;
  row_limit: number;
  execution_limit?: number;
};
export type QueryResult = {
  id: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
};
export type LiveTurn = {
  question: string;
  text: string;
  tools: ToolCall[];
  sql: SqlBlock[];
  citations: Citation[];
  clarifications: Clarification[][];
  status: "running" | "completed" | "failed";
  error?: string;
};
export type CitationDetail =
  | { kind: "table"; ref: string; table: TableDetail }
  | { kind: "document"; ref: string; document: DocumentView }
  | { kind: "message"; ref: string; message: string };

export const PROVIDER_TYPES = [
  "anthropic-messages",
  "openai-completions",
  "openai-responses",
  "google-generative-ai",
];

export const EFFORT_LEVELS: EffortLevel[] = [
  "low",
  "medium",
  "high",
  "extra",
  "max",
];
