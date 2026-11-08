import type { Messages } from "../i18n";
import type { Clarification, EventView, ToolCall } from "./types";

export function clarificationsOf(value: unknown): Clarification[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Clarification => typeof item === "object" && item !== null,
  );
}

export function clarificationOptionIsLost(
  option: NonNullable<Clarification["options"]>[number],
): boolean {
  return [option.id, option.label, option.ref].some(
    (value) => value?.trim() === "…",
  );
}

export function clarificationOptionIsUsable(
  option: NonNullable<Clarification["options"]>[number],
): boolean {
  return (
    !clarificationOptionIsLost(option) &&
    Boolean(option.label?.trim()) &&
    Boolean(option.ref?.trim())
  );
}

export function applyToolStart(
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
      input: String(data.input ?? data.summary ?? ""),
    },
  ];
}

export function applyToolEnd(
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

export function toolsOfTurn(events: EventView[]): ToolCall[] {
  let tools: ToolCall[] = [];
  for (const event of events) {
    if (event.type === "tool_start") tools = applyToolStart(tools, event.data);
    else if (event.type === "tool_end") tools = applyToolEnd(tools, event.data);
  }
  return tools;
}

export function clarificationsOfTurn(events: EventView[]): Clarification[][] {
  const groups: Clarification[][] = [];
  for (const event of events) {
    if (event.type !== "clarification") continue;
    const questions = clarificationsOf(event.data.questions);
    if (questions.length > 0) groups.push(questions);
  }
  return groups;
}

export function queryEvent<T>(
  events: EventView[],
  type: string,
): T | undefined {
  return [...events].reverse().find((event) => event.type === type)?.data as
    | T
    | undefined;
}

export function turnFailed(turn: {
  status: string;
  answer?: string;
  error?: string | null;
}) {
  return (
    turn.status === "failed" ||
    Boolean(turn.error) ||
    (turn.status === "error" && !turn.answer)
  );
}

export function resultCountText(messages: Messages, count: number) {
  if (count > 20) {
    return messages.resultRowsLimited.replace("{count}", String(count));
  }
  return count === 1
    ? messages.resultRowSingle
    : messages.resultRows.replace("{count}", String(count));
}

export function toolLabel(name: string) {
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
