/// <reference types="node" />
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { clearTableDetailCache } from "../tableDetail";

const provider = {
  id: "provider-1",
  name: "Local gateway",
  type: "anthropic-messages",
  base_url: "http://127.0.0.1:3425",
  models: ["deepseek-v4.1-flash#claude"],
  has_api_key: false,
  created_at: "2026-11-05T00:00:00Z",
  updated_at: "2026-11-05T00:00:00Z",
};

const table = {
  name: "payments",
  database: "shop",
  comment: "支付流水",
  ref: "table:shop.payments",
  fields: [
    {
      name: "dt",
      type: "string",
      comment: "日期分区",
      partition: true,
      dimension_count: null,
      dimension_too_many: null,
      dimension_samples: null,
    },
  ],
  documents: [],
};

const document = {
  ref: "document:metrics/definitions/sales.md",
  category: "metrics",
  path: "definitions/sales.md",
  content: "支付销售额按支付成功时间归属日期。",
  start_line: 1,
  end_line: 1,
  generated: false,
};

const sse = [
  'event: conversation\ndata: {"conversation_id":"conv-1","turn_id":1}',
  'event: text_delta\ndata: {"text":"推荐 "}',
  'event: tool_start\ndata: {"id":"call-1","name":"mcp__dataagent__search_knowledge","summary":"query=支付销售额"}',
  'event: tool_end\ndata: {"id":"call-1","name":"mcp__dataagent__search_knowledge","is_error":false,"summary":"found sell metric"}',
  'event: text_delta\ndata: {"text":"shop.payments"}',
  'event: done\ndata: {"text":"推荐 shop.payments。","sql":[{"sql":"SELECT dt, COUNT(*) AS n FROM shop.payments GROUP BY dt","dialect":"hive"}],"citations":[{"ref":"table:shop.payments","type":"table"},{"ref":"document:metrics/definitions/sales.md#chunk-1","type":"document"}]}',
].join("\n\n");

export { provider, table, document, sse };

export function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

export async function openHistory(title: string) {
  fireEvent.click(await screen.findByRole("button", { name: title }));
}

export function toolStream(conversationId: string, ending: string) {
  return [
    `event: conversation\ndata: {"conversation_id":"${conversationId}","turn_id":1}`,
    'event: tool_start\ndata: {"id":"call-1","name":"mcp__dataagent__execute_query","summary":"SELECT 1"}',
    'event: tool_end\ndata: {"id":"call-1","name":"mcp__dataagent__execute_query","is_error":true,"summary":"invalid_input"}',
    'event: text_delta\ndata: {"text":"正在处理"}',
    ending,
  ].join("\n\n");
}

export function toolTurnEvents() {
  return [
    {
      seq: 0,
      type: "tool_start",
      data: {
        id: "call-1",
        name: "mcp__dataagent__execute_query",
        summary: "SELECT 1",
      },
    },
    {
      seq: 1,
      type: "tool_end",
      data: {
        id: "call-1",
        name: "mcp__dataagent__execute_query",
        is_error: true,
        summary: "invalid_input",
      },
    },
  ];
}

export function cssDeclarations(selector: string) {
  const css = readFileSync(`${process.cwd()}/src/workbench.css`, "utf8");
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? ""
  );
}

export function composerSelectText(label: string) {
  const input = screen.getByLabelText(label);
  const root = input.closest(".ant-select");
  return (
    root
      ?.querySelector(".ant-select-content, .ant-select-selection-item")
      ?.textContent?.trim() ??
    root
      ?.querySelector(".ant-select-selection-placeholder")
      ?.textContent?.trim() ??
    ""
  );
}

export function composerSelectDisabled(label: string) {
  const input = screen.getByLabelText(label);
  return (
    (input as HTMLInputElement).disabled ||
    input.closest(".ant-select")?.classList.contains("ant-select-disabled") ===
      true
  );
}

export async function chooseComposerOption(label: string, option: RegExp) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

export function conversationRow(id: string, title: string) {
  return {
    id,
    project_id: "default",
    project_name: "业务数据",
    title,
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 0,
    resumable: true,
    pinned: false,
    archived: false,
  };
}

export function conversationFetcher(
  rows: ReturnType<typeof conversationRow>[],
  onDelete: (id: string) => Response,
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json(rows);
    if (url === "/api/v1/conversations?archived=true") return json([]);
    const match = url.match(/^\/api\/v1\/conversations\/([^/]+)$/);
    if (match) {
      const id = decodeURIComponent(match[1]);
      if (init?.method === "DELETE") {
        const response = onDelete(id);
        if (response.ok) {
          const index = rows.findIndex((row) => row.id === id);
          if (index >= 0) rows.splice(index, 1);
        }
        return response;
      }
      const row = rows.find((item) => item.id === id);
      if (row) return json({ ...row, turns: [] });
    }
    return new Response("", { status: 404 });
  });
}

export function setupChatTestEnv() {
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    cleanup();
    clearTableDetailCache();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
  });
}
