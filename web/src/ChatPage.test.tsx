/// <reference types="node" />
// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  ChatPage,
  citationLabel,
  mergeCitations,
  stripCitationRefs,
} from "./ChatPage";
import { getMessages } from "./i18n";
import { clearTableDetailCache } from "./tableDetail";

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

function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function openHistory(title: string) {
  fireEvent.click(await screen.findByRole("button", { name: title }));
}

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

test("新对话的消息区不显示占位查询计划", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json([])),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await screen.findByText(getMessages("zh").exampleQuestions);
  expect(container.querySelector(".chat-scroll .plan-placeholder")).toBeNull();
});

test("首次加载有历史会话时显示空状态且不请求会话详情", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  const fetcher = conversationFetcher(
    rows,
    () => new Response(null, { status: 204 }),
  );
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );

  expect(await screen.findByRole("button", { name: "订单分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    messages.newConversation,
  );
  expect(container.querySelector(".session-item.active")).toBeNull();
  expect(screen.getByText(messages.chatGreeting)).toBeTruthy();
  expect(
    fetcher.mock.calls.some(
      ([input]) => String(input) === "/api/v1/conversations/conv-1",
    ),
  ).toBe(false);
});

test("点击侧栏历史会话才请求详情并打开", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  const fetcher = conversationFetcher(
    rows,
    () => new Response(null, { status: 204 }),
  );
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );

  fireEvent.click(await screen.findByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );
  expect(container.querySelector(".session-item.active")).toBeTruthy();
  expect(fetcher).toHaveBeenCalledWith("/api/v1/conversations/conv-1", {
    headers: { Authorization: "Bearer token" },
  });
});

test("提问后新会话出现在列表顶部并选中", async () => {
  const messages = getMessages("zh");
  const previous = conversationRow("conv-old", "旧会话");
  const created = {
    ...conversationRow("conv-new", "新问题"),
    turn_count: 1,
  };
  const stream = [
    'event: conversation\ndata: {"conversation_id":"conv-new","turn_id":1}',
    'event: done\ndata: {"text":"新回答","sql":[],"citations":[]}',
  ].join("\n\n");
  let asked = false;
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations")
        return json(asked ? [created, previous] : [previous]);
      if (url === "/api/v1/conversations/ask" && init?.method === "POST") {
        asked = true;
        return new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url === "/api/v1/conversations/conv-new")
        return json({
          ...created,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "新问题",
              status: "completed",
              answer: "新回答",
              sql: [],
              citations: [],
              error: null,
              started_at: created.created_at,
              finished_at: created.updated_at,
              events: [],
            },
          ],
        });
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "新问题" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));

  expect(await screen.findByText("新回答")).toBeTruthy();
  await waitFor(() => {
    const items = container.querySelectorAll(".session-item");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("新问题");
    expect(items[0].classList.contains("active")).toBe(true);
  });
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    "新问题",
  );
  expect(screen.getByRole("button", { name: "旧会话" })).toBeTruthy();
});

test("查询计划可确认执行，结果和下载显示在对话中", async () => {
  const plan = {
    id: "query-81",
    sql: "SELECT sales\nFROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: 2,
    estimated_rows: 6,
    row_limit: 6,
  };
  const result = {
    id: plan.id,
    columns: ["sales"],
    rows: Array.from({ length: 25 }, (_, i) => [i + 1]),
    row_count: 25,
  };
  let executed = false;
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const conversation = {
    id: "conv-81",
    title: "销售额",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-81")
        return json({
          ...conversation,
          turns: [
            {
              id: 81,
              sequence: 1,
              question: "销售额",
              status: executed ? "completed" : "awaiting_confirmation",
              answer: executed ? "销售结论" : "",
              sql: [],
              citations: executed
                ? [{ ref: "table:shop.payments", type: "table" }]
                : [],
              error: null,
              started_at: conversation.created_at,
              finished_at: null,
              events: [
                { seq: 0, type: "query_plan", data: plan },
                ...(executed
                  ? [{ seq: 1, type: "query_result", data: result }]
                  : []),
              ],
            },
          ],
        });
      if (
        url ===
        "/api/v1/conversations/conv-81/turns/81/queries/query-81/execute"
      ) {
        expect(init?.headers).toMatchObject({
          Authorization: "Bearer personal-token",
        });
        executed = true;
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
              controller.enqueue(
                new TextEncoder().encode(
                  `event: query_result\ndata: ${JSON.stringify(result)}\n\n`,
                ),
              );
            },
          }),
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          },
        );
      }
      if (url.startsWith("/api/v1/tables/")) return json(table);
      if (url === "/api/v1/queries/query-81/download-links") {
        return json({
          csv: "/signed/csv",
          xlsx: "/signed/xlsx",
          json: "/signed/json",
        });
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );
  await openHistory("销售额");
  expect((await screen.findByText("确认执行这条 SQL？")) !== null).toBe(true);
  expect(container.querySelector(".sql-block")?.textContent).toContain(
    "SELECT sales",
  );
  expect(container.querySelector(".sql-block")?.textContent).toContain(
    "FROM shop.orders",
  );
  expect(screen.queryByText("查询计划")).toBeNull();
  expect(screen.queryByText(/命中分区/)).toBeNull();
  expect(screen.queryByText(/预计行数/)).toBeNull();
  expect(executed).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*执\s*行/ }));
  expect(
    (
      (await screen.findByRole("button", {
        name: /执\s*行\s*中/,
      })) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(await screen.findByText("结果可能被截断")).toBeTruthy();
  expect(
    container.querySelectorAll(".query-result-preview tbody tr"),
  ).toHaveLength(20);
  expect(screen.queryByText("销售结论")).toBeNull();
  streamController?.enqueue(
    new TextEncoder().encode('event: done\ndata: {"text":"销售结论"}\n\n'),
  );
  streamController?.close();
  expect(await screen.findByText("销售结论")).toBeTruthy();
  expect(screen.queryByRole("tab", { name: "结果" })).toBeNull();
  expect(screen.getByText("共 25 行，已显示前 20 行")).toBeTruthy();
  expect(
    screen.getByText("完整数据请通过下载链接获取（链接 30 分钟内有效）"),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
  await waitFor(() =>
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      `${window.location.origin}/signed/csv`,
    ),
  );
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  for (const label of ["下载 CSV", "下载 Excel", "下载 JSON"])
    fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(3));
  expect(
    fetcher.mock.calls.filter(([input]) =>
      String(input).includes("/download-links"),
    ),
  ).toHaveLength(4);
  expect(
    fetcher.mock.calls.some(([input]) => String(input).startsWith("/signed/")),
  ).toBe(false);
});

test("结果达到执行上限时提示可能被截断", async () => {
  const plan = {
    id: "query-limit",
    sql: "SELECT value FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: 1,
    estimated_rows: 6,
    row_limit: 1000,
    execution_limit: 6,
  };
  const result = {
    id: plan.id,
    columns: ["value"],
    rows: [[1], [2], [3], [4], [5], [6]],
    row_count: 6,
  };
  const conversation = {
    id: "conv-limit",
    title: "上限",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-limit")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "上限",
              status: "completed",
              answer: "结论",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: [
                { seq: 0, type: "query_plan", data: plan },
                { seq: 1, type: "query_result", data: result },
              ],
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await openHistory("上限");
  expect(await screen.findByText("结果可能被截断")).toBeTruthy();
  expect(screen.queryByRole("tab", { name: "查询计划" })).toBeNull();
});

test("查看历史预览会打开对应查询结果", async () => {
  const conversation = {
    id: "conv-history",
    title: "历史",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 2,
    resumable: true,
  };
  const turns = ["old", "new"].map((id, index) => ({
    id: index + 1,
    sequence: index + 1,
    question: id,
    status: "completed",
    answer: "",
    sql: [],
    citations: [],
    error: null,
    started_at: conversation.created_at,
    finished_at: null,
    events: [
      {
        seq: 0,
        type: "query_plan",
        data: {
          id,
          sql: `SELECT '${id}'`,
          data_source: "doris_ai",
          tables: [],
          estimated_partitions: null,
          estimated_rows: null,
          row_limit: 10,
        },
      },
      {
        seq: 1,
        type: "query_result",
        data: { id, columns: ["value"], rows: [[`${id}-value`]], row_count: 1 },
      },
    ],
  }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-history")
        return json({ ...conversation, turns });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await openHistory("历史");
  await screen.findByText("old-value");
  expect(container.querySelectorAll(".query-result-preview")).toHaveLength(2);
  expect(screen.getByText("new-value")).toBeTruthy();
});

test.each([1, 20, 21])("结果预览在 %i 行时显示正确的说明", async (rowCount) => {
  const conversation = {
    id: `conv-preview-${rowCount}`,
    title: "预览",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  const plan = {
    id: `query-preview-${rowCount}`,
    sql: "SELECT value FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: null,
    estimated_rows: rowCount,
    row_limit: 1000,
  };
  const result = {
    id: plan.id,
    columns: ["value"],
    rows: Array.from({ length: rowCount }, (_, index) => [index + 1]),
    row_count: rowCount,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === `/api/v1/conversations/${conversation.id}`)
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "预览",
              status: "completed",
              answer: "结论",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: [
                { seq: 0, type: "query_plan", data: plan },
                { seq: 1, type: "query_result", data: result },
              ],
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await openHistory("预览");
  if (rowCount <= 20) {
    expect(await screen.findByText(`共 ${rowCount} 行`)).toBeTruthy();
    expect(screen.queryByText(`共 ${rowCount} 行，已显示前 20 行`)).toBeNull();
    expect(
      screen.getByText("可下载完整结果（链接 30 分钟内有效）"),
    ).toBeTruthy();
    expect(
      screen.queryByText("完整数据请通过下载链接获取（链接 30 分钟内有效）"),
    ).toBeNull();
  } else {
    expect(
      await screen.findByText(`共 ${rowCount} 行，已显示前 20 行`),
    ).toBeTruthy();
    expect(screen.queryByText(`共 ${rowCount} 行`)).toBeNull();
    expect(
      screen.getByText("完整数据请通过下载链接获取（链接 30 分钟内有效）"),
    ).toBeTruthy();
  }
  expect(
    container.querySelectorAll(".query-result-preview tbody tr"),
  ).toHaveLength(Math.min(rowCount, 20));
  expect(screen.getByRole("button", { name: "下载 CSV" })).toBeTruthy();
});

function toolStream(conversationId: string, ending: string) {
  return [
    `event: conversation\ndata: {"conversation_id":"${conversationId}","turn_id":1}`,
    'event: tool_start\ndata: {"id":"call-1","name":"mcp__dataagent__execute_query","summary":"SELECT 1"}',
    'event: tool_end\ndata: {"id":"call-1","name":"mcp__dataagent__execute_query","is_error":true,"summary":"invalid_input"}',
    'event: text_delta\ndata: {"text":"正在处理"}',
    ending,
  ].join("\n\n");
}

function toolTurnEvents() {
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

test("工具失败但回答成功时列表保持折叠并提示失败数", async () => {
  const conversation = {
    id: "conv-tools-ok",
    title: "工具",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(
          toolStream(
            conversation.id,
            'event: done\ndata: {"text":"这是最终回答","sql":[],"citations":[]}',
          ),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      if (url === `/api/v1/conversations/${conversation.id}`)
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "工具",
              status: "completed",
              answer: "这是最终回答",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: toolTurnEvents(),
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查一下" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  expect(await screen.findByText("这是最终回答")).toBeTruthy();
  const toggle = await screen.findByRole("button", {
    name: /已使用 1 个工具/,
  });
  await waitFor(() =>
    expect(toggle.getAttribute("aria-expanded")).toBe("false"),
  );
  expect(screen.getByText("其中 1 个失败")).toBeTruthy();
  expect(container.querySelectorAll(".tool-step")).toHaveLength(0);
});

test("回答整体失败时工具列表自动展开", async () => {
  const conversation = {
    id: "conv-tools-failed",
    title: "工具失败",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(
          toolStream(
            conversation.id,
            'event: error\ndata: {"message":"模型超时"}',
          ),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      if (url === `/api/v1/conversations/${conversation.id}`)
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "工具失败",
              status: "failed",
              answer: "",
              sql: [],
              citations: [],
              error: "模型超时",
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: toolTurnEvents(),
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查一下" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  expect((await screen.findAllByText("模型超时")).length).toBeGreaterThan(0);
  const toggle = await screen.findByRole("button", {
    name: /已使用 1 个工具/,
  });
  await waitFor(() =>
    expect(toggle.getAttribute("aria-expanded")).toBe("true"),
  );
  expect(container.querySelectorAll(".tool-step")).toHaveLength(1);
  expect(container.querySelector(".tool-step")?.hasAttribute("open")).toBe(
    true,
  );
  expect(screen.queryByText("其中 1 个失败")).toBeNull();
});

test("确认执行返回 410 后按钮禁用并显示已过期", async () => {
  const plan = {
    id: "query-expired",
    sql: "SELECT sales FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: null,
    estimated_rows: null,
    row_limit: 100,
  };
  const conversation = {
    id: "conv-expired",
    title: "过期",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-expired")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "过期",
              status: "awaiting_confirmation",
              answer: "",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: null,
              events: [{ seq: 0, type: "query_plan", data: plan }],
            },
          ],
        });
      if (
        url ===
        "/api/v1/conversations/conv-expired/turns/1/queries/query-expired/execute"
      )
        return new Response(JSON.stringify({ detail: "查询确认已过期" }), {
          status: 410,
          headers: { "Content-Type": "application/json" },
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await openHistory("过期");
  expect(await screen.findByText("确认执行这条 SQL？")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*执\s*行/ }));
  expect((await screen.findAllByText("查询确认已过期")).length).toBeGreaterThan(
    0,
  );
  const expired = screen.getByRole("button", {
    name: "已过期，请重新提问",
  }) as HTMLButtonElement;
  expect(expired.disabled).toBe(true);
});

test.each([
  [1, "1 row", "Download the full result above (valid for 30 minutes)."],
  [
    21,
    "Showing the first 20 of 21 rows",
    "Download the full data from the links above (valid for 30 minutes).",
  ],
])("英文界面显示 %i 行结果预览文案", async (rowCount, summary, hint) => {
  const conversation = {
    id: `conv-preview-en-${rowCount}`,
    title: "Preview",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  const plan = {
    id: `query-preview-en-${rowCount}`,
    sql: "SELECT value FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: null,
    estimated_rows: rowCount,
    row_limit: 1000,
  };
  const result = {
    id: plan.id,
    columns: ["value"],
    rows: Array.from({ length: rowCount }, (_, index) => [index + 1]),
    row_count: rowCount,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === `/api/v1/conversations/${conversation.id}`)
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "Preview",
              status: "completed",
              answer: "Done",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: [
                { seq: 0, type: "query_plan", data: plan },
                { seq: 1, type: "query_result", data: result },
              ],
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("en")} />);
  await openHistory("Preview");
  expect(await screen.findByText(summary)).toBeTruthy();
  expect(screen.getByText(hint)).toBeTruthy();
});

test("英文界面折叠工具行显示失败数", async () => {
  const conversation = {
    id: "conv-tools-en",
    title: "Tools",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(
          toolStream(
            conversation.id,
            'event: done\ndata: {"text":"Final answer","sql":[],"citations":[]}',
          ),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      if (url === `/api/v1/conversations/${conversation.id}`)
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "Tools",
              status: "completed",
              answer: "Final answer",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: conversation.updated_at,
              events: toolTurnEvents(),
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("en")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "Query" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("Final answer")).toBeTruthy();
  const toggle = await screen.findByRole("button", {
    name: /已使用 1 个工具/,
  });
  await waitFor(() =>
    expect(toggle.getAttribute("aria-expanded")).toBe("false"),
  );
  expect(screen.getByText("1 failed")).toBeTruthy();
});

test("英文界面 410 后显示过期文案", async () => {
  const plan = {
    id: "query-expired-en",
    sql: "SELECT sales FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: null,
    estimated_rows: null,
    row_limit: 100,
  };
  const conversation = {
    id: "conv-expired-en",
    title: "Expired",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-expired-en")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "Expired",
              status: "awaiting_confirmation",
              answer: "",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: null,
              events: [{ seq: 0, type: "query_plan", data: plan }],
            },
          ],
        });
      if (
        url ===
        "/api/v1/conversations/conv-expired-en/turns/1/queries/query-expired-en/execute"
      )
        return new Response(JSON.stringify({ detail: "查询确认已过期" }), {
          status: 410,
          headers: { "Content-Type": "application/json" },
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("en")} />);
  await openHistory("Expired");
  expect(await screen.findByText("Confirm running this SQL?")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Confirm and run" }));
  const expired = (await screen.findByRole("button", {
    name: "Expired; please ask again.",
  })) as HTMLButtonElement;
  expect(expired.disabled).toBe(true);
});

test("streams tools and text, copies SQL, and opens table and document citations", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/conversations/ask")
      return new Response(sse, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    if (url.startsWith("/api/v1/tables/")) return json(table);
    if (url.startsWith("/api/v1/document?")) return json(document);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "支付销售额怎么统计？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));

  expect(await screen.findByText("推荐 shop.payments。")).toBeTruthy();
  expect(screen.queryByText("mcp__dataagent__search_knowledge")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /已使用 1 个工具/ }));
  expect(screen.getByText("mcp__dataagent__search_knowledge")).toBeTruthy();
  expect(screen.getByText("完成")).toBeTruthy();
  expect(
    screen.getByText("SELECT dt, COUNT(*) AS n FROM shop.payments GROUP BY dt"),
  ).toBeTruthy();
  expect(screen.queryByText("personal-token")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "复制 SQL" }));
  await waitFor(() =>
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "SELECT dt, COUNT(*) AS n FROM shop.payments GROUP BY dt",
    ),
  );
  expect(await screen.findByText("SQL 已复制")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "table:shop.payments" }));
  expect(await screen.findByText("支付流水")).toBeTruthy();
  expect(screen.getByText(/日期分区/)).toBeTruthy();
  const schemaHead = window.document.querySelector(".context-schema-head");
  expect(schemaHead?.textContent).toContain("shop.payments");
  expect(schemaHead?.textContent).toContain("支付流水");
  expect(
    window.document.querySelector(".context-schema-body .schema-table"),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(window.document.querySelector(".context-schema-head")).toBeNull();
  expect(fetcher).toHaveBeenCalledWith("/api/v1/tables/shop/payments", {
    headers: { Authorization: "Bearer personal-token" },
  });

  fireEvent.click(
    screen.getByRole("button", {
      name: "document:metrics/definitions/sales.md#chunk-1",
    }),
  );
  expect(
    await screen.findByText("支付销售额按支付成功时间归属日期。"),
  ).toBeTruthy();
  expect(
    window.document.querySelector(".context-header strong")?.textContent,
  ).toBe("sales");
  expect(fetcher).toHaveBeenCalledWith(
    "/api/v1/document?doc_id=document%3Ametrics%2Fdefinitions%2Fsales.md",
    { headers: { Authorization: "Bearer personal-token" } },
  );
});

test("restores conversation history and renders its SQL and citations", async () => {
  const conversation = {
    id: "conv-1",
    title: "支付销售额怎么统计？",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
  };
  const detail = {
    ...conversation,
    turns: [
      {
        id: 1,
        sequence: 1,
        question: "支付销售额怎么统计？",
        status: "completed",
        answer: "使用 shop.payments 按 dt 聚合。",
        sql: [
          {
            sql: "SELECT dt, COUNT(*) AS n FROM shop.payments GROUP BY dt",
            dialect: "hive",
          },
        ],
        citations: [{ ref: "table:shop.payments", type: "table" }],
        error: null,
        started_at: "2026-11-05T00:00:00Z",
        finished_at: "2026-11-05T00:00:01Z",
        events: [
          {
            seq: 0,
            type: "tool_start",
            data: {
              id: "call-history",
              name: "mcp__dataagent__search_knowledge",
              summary: "query=支付销售额",
            },
          },
          {
            seq: 1,
            type: "tool_end",
            data: {
              id: "call-history",
              name: "mcp__dataagent__search_knowledge",
              is_error: false,
              summary: "found payment metric",
            },
          },
          {
            seq: 2,
            type: "clarification",
            data: {
              questions: [{ question: "按支付日还是下单日？", options: [] }],
            },
          },
        ],
      },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-1") return json(detail);
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("en")}
    />,
  );

  fireEvent.click(
    await screen.findByRole("button", { name: "支付销售额怎么统计？" }),
  );
  expect(
    await screen.findByText("使用 shop.payments 按 dt 聚合。"),
  ).toBeTruthy();
  expect(
    screen.getByText("SELECT dt, COUNT(*) AS n FROM shop.payments GROUP BY dt"),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "table:shop.payments" }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /已使用 1 个工具/ }));
  expect(screen.getByText("mcp__dataagent__search_knowledge")).toBeTruthy();
  expect(screen.getByText("按支付日还是下单日？")).toBeTruthy();
  const citationList = container.querySelector(".citation-list");
  const clarificationCard = container.querySelector(".clarification-card");
  expect(clarificationCard).toBeTruthy();
  expect(citationList).toBeTruthy();
  expect(citationList!.compareDocumentPosition(clarificationCard!)).toBe(
    Node.DOCUMENT_POSITION_FOLLOWING,
  );
  expect(
    clarificationCard!.querySelector(".context-section-title")?.textContent,
  ).toBe("Clarification needed");
});

test("admins configure providers without sending a blank API key", async () => {
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers")
        return json(init?.method === "POST" ? [provider] : [provider]);
      if (url === "/api/v1/conversations") return json([]);
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );

  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Company gateway" },
  });
  fireEvent.change(screen.getByLabelText("Base URL"), {
    target: { value: "https://gateway.example.test" },
  });
  fireEvent.change(screen.getByLabelText("模型列表（逗号分隔）"), {
    target: { value: "model-a, model-b" },
  });
  expect((screen.getByLabelText("API key") as HTMLInputElement).type).toBe(
    "password",
  );
  fireEvent.click(screen.getByRole("button", { name: /添加\s*provider/ }));

  await waitFor(() => {
    const call = fetcher.mock.calls.find(
      ([input, init]) =>
        String(input) === "/api/v1/providers" && init?.method === "POST",
    );
    expect(call).toBeTruthy();
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      name: "Company gateway",
      type: "anthropic-messages",
      base_url: "https://gateway.example.test",
      models: ["model-a", "model-b"],
    });
  });
  expect(await screen.findByText("provider 已保存")).toBeTruthy();
  expect(screen.queryByText("personal-token")).toBeNull();
});

test.each(["claude", "codex"])(
  "selects %s and sends the configured model",
  async (agent) => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json(
          ["pi", "claude", "codex"].map((id) => ({
            id,
            name: id,
            available: true,
          })),
        );
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(sse, { status: 200 });
      return new Response("", { status: 404 });
    });
    vi.stubGlobal("fetch", fetcher);
    render(
      <ChatPage
        token="personal-token"
        role="user"
        messages={getMessages("zh")}
      />,
    );
    await screen.findByRole("option", { name: agent });
    fireEvent.change(screen.getByLabelText("Agent"), {
      target: { value: agent },
    });
    fireEvent.change(screen.getByLabelText("问题"), {
      target: { value: "测试" },
    });
    fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(
        "/api/v1/conversations/ask",
        expect.objectContaining({
          body: expect.stringContaining(`"agent":"${agent}"`),
        }),
      ),
    );
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/conversations/ask",
      expect.objectContaining({
        body: expect.stringContaining(`"model":"${provider.models[0]}"`),
      }),
    );
  },
);
test("a clarification option continues the same conversation with its evidence", async () => {
  const firstSse = [
    'event: conversation\ndata: {"conversation_id":"conv-clarify","turn_id":1}',
    'event: clarification\ndata: {"questions":[{"id":"q1","question":"销售额按支付口径还是下单口径？","options":[{"id":"o1","label":"支付销售额（支付成功时间）","ref":"document:metrics/definitions/sales.md#chunk-1"},{"id":"o2","label":"下单销售额（订单创建时间）","ref":"document:metrics/sales_order.md#chunk-1"}]}]}',
    'event: done\ndata: {"text":"请选择口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const secondSse = [
    'event: conversation\ndata: {"conversation_id":"conv-clarify","turn_id":2}',
    'event: done\ndata: {"text":"按支付口径。","sql":[{"sql":"SELECT SUM(amount) FROM shop.payments","dialect":"hive"}],"citations":[{"ref":"document:metrics/definitions/sales.md#chunk-1","type":"document"}]}',
  ].join("\n\n");
  const askBodies: string[] = [];
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask") {
        askBodies.push(String(init?.body));
        return new Response(askBodies.length === 1 ? firstSse : secondSse, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "上个月销售额是多少？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: /支付销售额（支付成功时间）/ }),
  );

  expect(
    await screen.findByText("SELECT SUM(amount) FROM shop.payments"),
  ).toBeTruthy();
  await waitFor(() => expect(askBodies).toHaveLength(2));
  const followUp = JSON.parse(askBodies[1]) as {
    question: string;
    conversation_id: string;
  };
  expect(followUp.conversation_id).toBe("conv-clarify");
  expect(followUp.question).toContain("支付销售额（支付成功时间）");
  expect(followUp.question).toContain(
    "document:metrics/definitions/sales.md#chunk-1",
  );
  expect(followUp.question).toContain("不要把它作为全局规则");
});

test("含中文编码 ref 的澄清选项显示友好名称并提交原 ref", async () => {
  const encodedRef =
    "document:%E6%8C%87%E6%A0%87/%E8%AE%A2%E5%8D%95/variant_001.md#chunk-1";
  const tableRef = "table:shop.orders";
  const firstSse = [
    'event: conversation\ndata: {"conversation_id":"conv-encoded","turn_id":1}',
    `event: clarification\ndata: ${JSON.stringify({
      questions: [
        {
          id: "q1",
          question: "销售额按哪个口径统计？",
          options: [
            { id: "o1", label: "支付销售额（支付成功时间）", ref: encodedRef },
            { id: "o2", label: "订单明细", ref: tableRef },
          ],
        },
      ],
    })}`,
    'event: done\ndata: {"text":"请选择口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const secondSse = [
    'event: conversation\ndata: {"conversation_id":"conv-encoded","turn_id":2}',
    'event: done\ndata: {"text":"按支付口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const askBodies: string[] = [];
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask") {
        askBodies.push(String(init?.body));
        return new Response(askBodies.length === 1 ? firstSse : secondSse, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "上个月销售额是多少？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const option = await screen.findByRole("button", {
    name: /支付销售额（支付成功时间）/,
  });
  expect(screen.getByText("指标 · 订单 · 口径 1")).toBeTruthy();
  expect(screen.getByText("shop.orders")).toBeTruthy();
  expect(screen.getByTitle("指标/订单/variant_001.md")).toBeTruthy();
  fireEvent.click(option);

  await waitFor(() => expect(askBodies).toHaveLength(2));
  const followUp = JSON.parse(askBodies[1]) as { question: string };
  expect(followUp.question).toContain(encodedRef);
});

function cssDeclarations(selector: string) {
  const css = readFileSync(`${process.cwd()}/src/workbench.css`, "utf8");
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? ""
  );
}

test("长澄清选项换行、来源另起一行，超过三行可展开且提交原 id", async () => {
  const longLabel =
    "销售额（GMV 口径）：SUM(gmv_cny)，来源 dm.dm_air_order_item_metrics_d，按 finished_time 过滤，覆盖订单项 GMV（含辅营等）";
  const longRef = "document:dm/dm_air_order_item_metrics_d.md#chunk-1";
  const questions = [
    {
      id: "q-long",
      question: "销售额按哪个 GMV 口径统计？",
      options: [
        { id: "o-long", label: longLabel, ref: longRef },
        {
          id: "o-short",
          label: "订单数",
          ref: "document:dm/dm_air_order_item_metrics_d.md#chunk-2",
        },
      ],
    },
  ];
  const firstSse = [
    'event: conversation\ndata: {"conversation_id":"conv-long","turn_id":1}',
    `event: clarification\ndata: ${JSON.stringify({ questions })}`,
    'event: done\ndata: {"text":"请选择口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const secondSse = [
    'event: conversation\ndata: {"conversation_id":"conv-long","turn_id":2}',
    'event: done\ndata: {"text":"按 GMV 口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const askBodies: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask") {
        askBodies.push(String(init?.body));
        return new Response(askBodies.length === 1 ? firstSse : secondSse, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return new Response("", { status: 404 });
    }),
  );
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(
    function (this: Element) {
      return (this.textContent?.length ?? 0) > 80 ? 160 : 0;
    },
  );
  vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(
    function (this: Element) {
      return (this.textContent?.length ?? 0) > 80 ? 40 : 0;
    },
  );
  const { container } = render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "上个月销售额是多少？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const option = await screen.findByRole("button", { name: /GMV 口径/ });

  const card = container.querySelector(".clarification-card") as HTMLElement;
  const stack = card.querySelector(".clarification-options") as HTMLElement;
  expect(stack).toBeTruthy();
  expect(stack.querySelectorAll(".clarification-option")).toHaveLength(2);
  const label = option.querySelector(
    ".clarification-option-label",
  ) as HTMLElement;
  expect(label.textContent).toBe(longLabel);
  const content = option.querySelector(
    ".clarification-option-content",
  ) as HTMLElement;
  const source = option.querySelector(".clarification-source") as HTMLElement;
  expect(Array.from(content.children)).toEqual([label, source]);
  expect(source.textContent).toBe("dm · dm_air_order_item_metrics_d");
  expect(source.title).toBe("dm/dm_air_order_item_metrics_d.md");

  expect(cssDeclarations(".clarification-options")).toContain(
    "flex-direction: column",
  );
  const optionCss = cssDeclarations(
    ".clarification-card .clarification-option",
  );
  expect(optionCss).toContain("white-space: normal");
  expect(optionCss).toContain("overflow-wrap: anywhere");
  expect(optionCss).toContain("max-width: 100%");
  expect(optionCss).toContain("text-align: left");
  const contentCss = cssDeclarations(
    ".clarification-card .clarification-option-content",
  );
  expect(contentCss).toContain("flex-direction: column");
  expect(contentCss).toContain("align-items: flex-start");
  const sourceCss = cssDeclarations(".clarification-source");
  expect(sourceCss).toContain("white-space: normal");
  expect(sourceCss).not.toContain("white-space: nowrap");
  expect(cssDeclarations(".clarification-card p")).toContain(
    "overflow-wrap: anywhere",
  );

  expect(
    option.querySelector(".clarification-option-label.is-expanded"),
  ).toBeNull();
  expect(screen.getAllByRole("button", { name: "展开" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "展开" }));
  expect(label.classList.contains("is-expanded")).toBe(true);
  expect(screen.getByRole("button", { name: "收起" })).toBeTruthy();

  fireEvent.click(option);
  await waitFor(() => expect(askBodies).toHaveLength(2));
  const followUp = JSON.parse(askBodies[1]) as { question: string };
  expect(followUp.question).toContain("选项 id：o-long");
  expect(followUp.question).toContain(longLabel);
  expect(followUp.question).toContain(longRef);
});

test("新会话发送所选 agent 与模型且仅锁定 agent", async () => {
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([
          { id: "pi", name: "pi", available: true },
          { id: "claude", name: "Claude", available: true },
        ]);
      if (url === "/api/v1/providers")
        return json([{ ...provider, models: ["model-a", "model-b"] }]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_provider_id: provider.id,
          assistant_model: "model-a",
        });
      if (url === "/api/v1/conversations/ask" && init?.method === "POST")
        return new Response(sse, {
          headers: { "Content-Type": "text/event-stream" },
        });
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );
  await screen.findByRole("option", { name: "Claude" });
  fireEvent.change(screen.getByLabelText("Agent"), {
    target: { value: "claude" },
  });
  fireEvent.change(screen.getByLabelText("模型"), {
    target: { value: "model-b" },
  });
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "订单在哪里？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) => String(url) === "/api/v1/conversations/ask",
      ),
    ).toBe(true),
  );
  const call = fetcher.mock.calls.find(
    ([url]) => String(url) === "/api/v1/conversations/ask",
  );
  expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
    agent: "claude",
    provider_id: provider.id,
    model: "model-b",
  });
  await waitFor(() =>
    expect((screen.getByLabelText("Agent") as HTMLSelectElement).disabled).toBe(
      true,
    ),
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).disabled).toBe(
    false,
  );
  expect((screen.getByLabelText("Provider") as HTMLSelectElement).disabled).toBe(
    false,
  );
});

test("对话中可切换 provider 与模型并在下一条消息使用", async () => {
  const messages = getMessages("zh");
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second gateway",
    models: ["model-b", "model-c"],
  };
  const row = conversationRow("conv-1", "订单分析");
  let askBody = "";
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations?archived=true") return json([]);
      if (url === "/api/v1/conversations") return json([row]);
      if (url === "/api/v1/assistant-defaults") return json({});
      if (url === "/api/v1/conversations/conv-1")
        return json({ ...row, turns: [] });
      if (url === "/api/v1/conversations/ask" && init?.method === "POST") {
        askBody = String(init.body);
        return new Response(sse, {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await screen.findByRole("button", { name: "订单分析" });
  fireEvent.click(screen.getByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(
      (screen.getByLabelText(messages.askProvider) as HTMLSelectElement).value,
    ).toBe(provider.id),
  );
  const agent = screen.getByLabelText(messages.askAgent) as HTMLSelectElement;
  expect(agent.disabled).toBe(true);
  expect(screen.getByText(messages.agentLockedHint)).toBeTruthy();
  fireEvent.change(screen.getByLabelText(messages.askProvider), {
    target: { value: second.id },
  });
  expect(
    (screen.getByLabelText(messages.askModel) as HTMLSelectElement).value,
  ).toBe("model-b");
  fireEvent.change(screen.getByLabelText(messages.askModel), {
    target: { value: "model-c" },
  });
  expect(
    await screen.findByText(
      messages.nextMessageModel.replace("{model}", "model-c"),
    ),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText(messages.askQuestion), {
    target: { value: "继续追问" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() => expect(askBody).not.toBe(""));
  expect(JSON.parse(askBody)).toMatchObject({
    question: "继续追问",
    conversation_id: "conv-1",
    agent: "pi",
    provider_id: second.id,
    model: "model-c",
  });
  expect(
    (screen.getByLabelText(messages.askProvider) as HTMLSelectElement).disabled,
  ).toBe(false);
  expect(
    (screen.getByLabelText(messages.askModel) as HTMLSelectElement).disabled,
  ).toBe(false);
});

test("新会话切换 agent 时使用该 agent 的 provider 和默认模型", async () => {
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second",
    models: ["model-b"],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([
          { id: "pi", name: "pi", available: true },
          { id: "claude", name: "Claude", available: true },
        ]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_agent_profiles: JSON.stringify({
            claude: {
              providers: [second.id],
              default_provider: second.id,
              default_model: "model-b",
            },
          }),
        });
      return new Response("", { status: 404 });
    }),
  );
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );
  await screen.findByRole("option", { name: "Claude" });
  await waitFor(() =>
    expect(screen.getByRole("option", { name: "Second" })).toBeTruthy(),
  );
  fireEvent.change(screen.getByLabelText("Agent"), {
    target: { value: "claude" },
  });
  expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
    "provider-2",
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
    "model-b",
  );
  expect(screen.queryByRole("option", { name: "Local gateway" })).toBeNull();
});

test("没有上下文时隐藏面板和开关", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (
        [
          "/api/v1/agents",
          "/api/v1/providers",
          "/api/v1/conversations",
        ].includes(url)
      )
        return json([]);
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  expect(container.querySelector(".chat-layout.context-hidden")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "收起面板" })).toBeNull();
  expect(screen.queryByRole("button", { name: "展开面板" })).toBeNull();
});

test("新建会话不会恢复不允许使用的默认 provider", async () => {
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second",
    models: ["model-b"],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_provider_id: provider.id,
          assistant_model: provider.models[0],
          assistant_agent_profiles: JSON.stringify({
            pi: {
              providers: [second.id],
              default_provider: "",
              default_model: "",
            },
          }),
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
      "provider-2",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "新对话" }));
  expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
    "provider-2",
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
    "model-b",
  );
});

test("后打开的引用不会被较晚返回的旧请求覆盖", async () => {
  let resolveTable!: (response: Response) => void;
  const delayedTable = new Promise<Response>((resolve) => {
    resolveTable = resolve;
  });
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/conversations/ask")
      return new Response(sse, {
        headers: { "Content-Type": "text/event-stream" },
      });
    if (url.startsWith("/api/v1/tables/")) return delayedTable;
    if (url.startsWith("/api/v1/document?")) return json(document);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "支付销售额？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: "table:shop.payments" }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "document:metrics/definitions/sales.md#chunk-1",
    }),
  );
  expect(
    await screen.findByText("支付销售额按支付成功时间归属日期。"),
  ).toBeTruthy();
  expect(
    window.document.querySelector(".context-header strong")?.textContent,
  ).toBe("sales");
  await act(async () => {
    resolveTable(json([table]));
    await delayedTable;
  });
  expect(screen.getByText("支付销售额按支付成功时间归属日期。")).toBeTruthy();
  expect(screen.queryByText("支付流水")).toBeNull();
});

test("keeps the previous answer when a second question is sent", async () => {
  const asks: string[] = [];
  const historyTurn = (sequence: number, question: string, answer: string) => ({
    id: sequence,
    sequence,
    question,
    status: "completed",
    answer,
    sql: [],
    citations: [],
    error: null,
    started_at: "2026-11-05T00:00:00Z",
    finished_at: "2026-11-05T00:00:01Z",
    events: [],
  });
  const answer = (text: string) =>
    [
      'event: conversation\ndata: {"conversation_id":"conv-1","turn_id":1}',
      `event: done\ndata: {"text":"${text}","sql":[],"citations":[]}`,
    ].join("\n\n");
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") {
        return json([
          {
            id: "conv-1",
            title: "第一问",
            agent: "pi",
            provider_id: provider.id,
            model: provider.models[0],
            created_at: "2026-11-05T00:00:00Z",
            updated_at: "2026-11-05T00:00:00Z",
            turn_count: asks.length,
          },
        ]);
      }
      if (url === "/api/v1/conversations/conv-1") {
        return json({
          id: "conv-1",
          resumable: true,
          agent: "pi",
          provider_id: provider.id,
          model: provider.models[0],
          turns:
            asks.length === 1
              ? [historyTurn(1, "第一问", "第一轮回答")]
              : [
                  historyTurn(1, "第一问", "第一轮回答"),
                  historyTurn(2, "第二问", "第二轮回答"),
                ],
        });
      }
      if (url === "/api/v1/conversations/ask") {
        asks.push(String(init?.body));
        return new Response(
          answer(asks.length === 1 ? "第一轮回答" : "第二轮回答"),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "第一问" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  expect(await screen.findByText("第一轮回答")).toBeTruthy();
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/conversations/conv-1",
      expect.anything(),
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "第二问" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  expect(await screen.findByText("第二轮回答")).toBeTruthy();
  expect(screen.getByText("第一轮回答")).toBeTruthy();
});

test("folds long user messages after six lines", async () => {
  const question = Array.from(
    { length: 8 },
    (_, index) => `第 ${index + 1} 行内容`,
  ).join("\n");
  const conversation = {
    id: "conv-long",
    title: "长问题",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-long")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question,
              status: "completed",
              answer: "已收到。",
              sql: [],
              citations: [],
              error: null,
              started_at: "2026-11-05T00:00:00Z",
              finished_at: "2026-11-05T00:00:01Z",
              events: [],
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "长问题" }));
  expect(await screen.findByText(/第 8 行内容/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "展开" }));
  expect(screen.getByRole("button", { name: "收起" })).toBeTruthy();
});

test("字段引用的血缘标签显示字段血缘", async () => {
  const fieldSse = sse.replace(
    '"ref":"table:shop.payments","type":"table"',
    '"ref":"field:shop.payments.dt","type":"field"',
  );
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/conversations/ask")
      return new Response(fieldSse, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    if (url.startsWith("/api/v1/tables/")) return json(table);
    if (url.startsWith("/api/v1/column-lineage?"))
      return json({ upstream: [], downstream: [] });
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查询字段" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: "field:shop.payments.dt" }),
  );
  await screen.findByText("支付流水");
  expect(await screen.findByText(/暂无字段级血缘/)).toBeTruthy();
  expect(
    fetcher.mock.calls.some(([url]) =>
      String(url).includes("table=shop.payments&column=dt"),
    ),
  ).toBe(true);
});

test("示例问题只填入输入框而不发送请求", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) => json([]));
  vi.stubGlobal("fetch", fetcher);
  const messages = getMessages("zh");
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(
    await screen.findByRole("button", { name: messages.examples[0] }),
  );
  expect(
    (screen.getByLabelText(messages.askQuestion) as HTMLTextAreaElement).value,
  ).toBe(messages.examples[0]);
  expect(
    fetcher.mock.calls.some(
      ([url]) => String(url) === "/api/v1/conversations/ask",
    ),
  ).toBe(false);
});

test("文档引用解码文件名并在抽屉渲染 Markdown", async () => {
  const name = "销售 50% #报告.md";
  const ref = `document:指标/${encodeURIComponent(name)}#chunk-1`;
  const response = sse
    .replace('"is_error":false', '"is_error":true')
    .replace("document:metrics/definitions/sales.md#chunk-1", ref);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(response, {
          headers: { "Content-Type": "text/event-stream" },
        });
      if (url.startsWith("/api/v1/document?"))
        return json({
          ...document,
          path: `definitions/${encodeURIComponent(name)}`,
          content: "# 销售标题\n\n**重要内容**",
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查看" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const chip = await screen.findByRole("button", { name: ref });
  expect(chip.textContent).toContain(name.replace(/\.md$/, ""));
  expect(chip.getAttribute("title")).toBe(`指标/${name}`);
  expect(container.querySelector(".chat-context")).toBeNull();
  expect(
    container
      .querySelector(".tool-log > button")
      ?.getAttribute("aria-expanded"),
  ).toBe("false");
  expect(screen.getByText("其中 1 个失败")).toBeTruthy();
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "销售标题" })).tagName,
  ).toBe("H1");
  expect(screen.getByText("重要内容").tagName).toBe("STRONG");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    name.replace(/\.md$/, ""),
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(
    `definitions/${name}`,
  );
  const drawer = container.querySelector(".chat-context");
  const header = drawer?.querySelector(":scope > .context-header");
  const body = drawer?.querySelector(":scope > .context-body");
  expect(header).toBeTruthy();
  expect(body).toBeTruthy();
  expect(header?.contains(container.querySelector(".context-ref"))).toBe(true);
  expect(container.querySelector(".context-body .doc-content")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(container.querySelector(".chat-context")).toBeNull();
  expect(container.querySelector(".chat-layout.context-hidden")).toBeTruthy();
});

test("文档路径编码非法时抽屉和悬停提示原样显示", async () => {
  const path = "definitions/%E9%94.md";
  const ref = `document:${path}#chunk-1`;
  const response = sse
    .replace('"is_error":false', '"is_error":true')
    .replace("document:metrics/definitions/sales.md#chunk-1", ref);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(response, {
          headers: { "Content-Type": "text/event-stream" },
        });
      if (url.startsWith("/api/v1/document?"))
        return json({
          ...document,
          path,
          content: "# 非法编码\n\n文件仍可查看。",
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查看" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const chip = await screen.findByRole("button", { name: ref });
  expect(chip.textContent).toContain("%E9%94.md");
  expect(chip.getAttribute("title")).toBe(path);
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "非法编码" })).tagName,
  ).toBe("H1");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    "%E9%94.md",
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(path);
});

function conversationRow(id: string, title: string) {
  return {
    id,
    title,
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 0,
    resumable: true,
  };
}

function conversationFetcher(
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

test("会话项悬停或聚焦时显示菜单按钮", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(rows, () => new Response(null, { status: 204 })),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  const deleteButton = await screen.findByRole("button", {
    name: messages.conversationMenu,
  });
  const item = container.querySelector(".session-item") as HTMLElement;
  expect(deleteButton.className).not.toContain("visible");
  fireEvent.mouseEnter(item);
  expect(deleteButton.className).toContain("visible");
  fireEvent.mouseLeave(item);
  expect(deleteButton.className).not.toContain("visible");
  fireEvent.focusIn(item);
  expect(deleteButton.className).toContain("visible");
  fireEvent.focusOut(item);
  expect(deleteButton.className).not.toContain("visible");
});

test("删除会话需要弹窗确认，取消不发送请求", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  const onDelete = vi.fn(() => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", conversationFetcher(rows, onDelete));
  render(<ChatPage token="token" role="user" messages={messages} />);

  fireEvent.click(
    await screen.findByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  expect(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: /取\s*消/ })).toBeTruthy();
  expect(onDelete).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
  expect(onDelete).not.toHaveBeenCalled();

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );
  await waitFor(() => expect(onDelete).toHaveBeenCalledWith("conv-1"));
});

test("删除当前会话后从列表移除并回到新对话空状态", async () => {
  const messages = getMessages("zh");
  const rows = [
    conversationRow("conv-1", "订单分析"),
    conversationRow("conv-2", "客户分析"),
  ];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(rows, () => new Response(null, { status: 204 })),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getAllByRole("button", { name: messages.conversationMenu })[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "订单分析" })).toBeNull(),
  );
  expect(screen.getByRole("button", { name: "客户分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    messages.newConversation,
  );
  expect(container.querySelector(".session-item.active")).toBeNull();
  expect(screen.getByText(messages.chatGreeting)).toBeTruthy();
});

test("删除运行中的会话提示稍后再试且列表不变", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      rows,
      () =>
        new Response(JSON.stringify({ detail: "running" }), { status: 409 }),
    ),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  expect(
    await screen.findByText(messages.deleteConversationRunning),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "订单分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    "订单分析",
  );
  expect(container.querySelectorAll(".session-item")).toHaveLength(1);
});

test("删除他人会话返回 404 时提示失败且列表不变", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      rows,
      () =>
        new Response(JSON.stringify({ detail: "not found" }), { status: 404 }),
    ),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  expect(await screen.findByText(messages.operationFailed)).toBeTruthy();
  expect(screen.getByRole("button", { name: "订单分析" })).toBeTruthy();
  expect(container.querySelectorAll(".session-item")).toHaveLength(1);
});

test("末尾纯引用行被移除，引用回收后按对象合并", () => {
  const answer = [
    "结论如下。",
    "",
    "引用：`document:metrics/definitions/sales.md#chunk-1`、`document:metrics/definitions/sales.md#chunk-2`",
    "`table:shop.payments`，`field:shop.payments.dt`",
  ].join("\n");
  expect(stripCitationRefs(answer)).toEqual({
    text: "结论如下。",
    refs: [
      "document:metrics/definitions/sales.md#chunk-1",
      "document:metrics/definitions/sales.md#chunk-2",
      "table:shop.payments",
      "field:shop.payments.dt",
    ],
  });
  expect(
    stripCitationRefs(
      "结论\n\n引用：\n\n- `document:a/b.md#chunk-1`\n- `field:x.y.z`",
    ),
  ).toEqual({
    text: "结论",
    refs: ["document:a/b.md#chunk-1", "field:x.y.z"],
  });
  expect(
    stripCitationRefs("结论\nCitations: document:a.md, document:b.md#chunk-2"),
  ).toEqual({
    text: "结论",
    refs: ["document:a.md", "document:b.md#chunk-2"],
  });
});

test("引用行识别只移除末尾的纯引用块", () => {
  expect(stripCitationRefs("引用：这份文档说明了口径")).toEqual({
    text: "引用：这份文档说明了口径",
    refs: [],
  });
  expect(stripCitationRefs("正文\n引用：document:a.md\n后面还有正文")).toEqual({
    text: "正文\n引用：document:a.md\n后面还有正文",
    refs: [],
  });
  expect(stripCitationRefs("正文\ndocument:a.md")).toEqual({
    text: "正文\ndocument:a.md",
    refs: [],
  });
  expect(
    stripCitationRefs("正文\nReferences: document:a.md\n引用：table:x.y"),
  ).toEqual({
    text: "正文",
    refs: ["document:a.md", "table:x.y"],
  });
});

test("citationLabel 生成文档与数据表的友好名称", () => {
  expect(citationLabel("document:%E6%8C%87%E6%A0%87/sales.md#chunk-1")).toEqual(
    {
      kind: "document",
      label: "sales",
      category: "指标",
      title: "指标/sales.md",
    },
  );
  expect(
    citationLabel("document:%E6%8C%87%E6%A0%87/%E5%BA%93/_overview.md#chunk-1"),
  ).toEqual({
    kind: "document",
    label: "库 · 概览",
    category: "指标",
    title: "指标/库/_overview.md",
  });
  expect(citationLabel("document:指标/订单/variant_001.md#chunk-2")).toEqual({
    kind: "document",
    label: "订单 · 口径 1",
    category: "指标",
    title: "指标/订单/variant_001.md",
  });
  expect(citationLabel("document:指标/订单/variant_012.md#chunk-2")).toEqual({
    kind: "document",
    label: "订单 · 口径 12",
    category: "指标",
    title: "指标/订单/variant_012.md",
  });
  const tricky = citationLabel(
    `document:指标/${encodeURIComponent("销售 50% #报告.md")}#chunk-1`,
  );
  expect(tricky).toEqual({
    kind: "document",
    label: "销售 50% #报告",
    category: "指标",
    title: "指标/销售 50% #报告.md",
  });
  expect(citationLabel("document:definitions/%E9%94.md#chunk-1")).toEqual({
    kind: "document",
    label: "%E9%94.md",
    category: "definitions",
    title: "definitions/%E9%94.md",
  });
  expect(citationLabel("table:shop.orders")).toEqual({
    kind: "table",
    label: "shop.orders",
    title: "shop.orders",
  });
  expect(citationLabel("field:shop.orders.amount")).toEqual({
    kind: "table",
    label: "shop.orders",
    title: "shop.orders · amount",
  });
  const long = citationLabel(
    "table:warehouse.a_very_long_table_name_that_should_be_middle_ellipsized",
  );
  expect(long.label).toContain("…");
  expect(long.title).toBe(
    "warehouse.a_very_long_table_name_that_should_be_middle_ellipsized",
  );
});

test("同一文档的多个 chunk 与同表字段合并为同一个引用", () => {
  const groups = mergeCitations([
    { ref: "document:metrics/a.md#chunk-1", type: "document" },
    { ref: "document:metrics/a.md#chunk-9", type: "document" },
    { ref: "table:shop.orders", type: "table" },
    { ref: "field:shop.orders.amount", type: "field" },
    { ref: "field:shop.orders.status", type: "field" },
  ]);
  expect(groups).toHaveLength(2);
  const document = groups.find((group) => group.label.kind === "document");
  const table = groups.find((group) => group.label.kind === "table");
  expect(document?.refs).toHaveLength(2);
  expect(document?.label.label).toBe("a");
  expect(table?.refs).toHaveLength(3);
  expect(table?.openRef).toBe("table:shop.orders");
  expect(table?.label.label).toBe("shop.orders");
  expect(table?.fields).toEqual(["amount", "status"]);
  expect(table?.label.title).toBe("shop.orders · 字段：amount、status");
});

test("引用标签合并后点击打开文档与数据表抽屉", async () => {
  const citations = [
    "document:metrics/definitions/sales.md#chunk-1",
    "document:metrics/definitions/sales.md#chunk-2",
    "table:shop.payments",
    "field:shop.payments.dt",
  ];
  const done = JSON.stringify({
    text: `结论如下。\n\n引用：${citations.map((ref) => `\`${ref}\``).join("、")}`,
    sql: [],
    citations: [],
  });
  const response = [
    'event: conversation\ndata: {"conversation_id":"conv-cite","turn_id":1}',
    `event: done\ndata: ${done}`,
  ].join("\n\n");
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/conversations/ask")
      return new Response(response, {
        headers: { "Content-Type": "text/event-stream" },
      });
    if (url.startsWith("/api/v1/tables/")) return json(table);
    if (url.startsWith("/api/v1/document?")) return json(document);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "引用测试" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await screen.findByText("结论如下。");
  expect(container.querySelector(".answer")?.textContent).not.toContain(
    "引用：",
  );
  expect(
    container.querySelectorAll(".citation-chip:not(.citation-more)"),
  ).toHaveLength(2);
  const documentChip = screen.getByRole("button", {
    name: "document:metrics/definitions/sales.md#chunk-1",
  });
  expect(documentChip.textContent).toContain("sales");
  expect(documentChip.getAttribute("title")).toBe(
    "metrics/definitions/sales.md",
  );
  const tableChip = screen.getByRole("button", {
    name: "table:shop.payments",
  });
  expect(tableChip.textContent).toContain("shop.payments");
  expect(tableChip.getAttribute("title")).toBe("shop.payments · 字段：dt");

  fireEvent.click(documentChip);
  expect(
    await screen.findByText("支付销售额按支付成功时间归属日期。"),
  ).toBeTruthy();
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    "sales",
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(
    "definitions/sales.md",
  );
  expect(fetcher).toHaveBeenCalledWith(
    "/api/v1/document?doc_id=document%3Ametrics%2Fdefinitions%2Fsales.md",
    { headers: { Authorization: "Bearer token" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));

  fireEvent.click(tableChip);
  expect(await screen.findByText("支付流水")).toBeTruthy();
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    "shop.payments",
  );
});

test("超过六个引用时折叠为更多 N 个并可展开", async () => {
  const refs = Array.from(
    { length: 8 },
    (_, index) => `document:metrics/doc${index + 1}.md#chunk-1`,
  );
  const done = JSON.stringify({
    text: "结论",
    sql: [],
    citations: refs.map((ref) => ({ ref, type: "document" })),
  });
  const response = [
    'event: conversation\ndata: {"conversation_id":"conv-many","turn_id":1}',
    `event: done\ndata: ${done}`,
  ].join("\n\n");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(response, {
          headers: { "Content-Type": "text/event-stream" },
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "很多引用" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const more = await screen.findByRole("button", { name: "更多 2 个" });
  expect(
    container.querySelectorAll(".citation-chip:not(.citation-more)"),
  ).toHaveLength(6);
  fireEvent.click(more);
  expect(
    container.querySelectorAll(".citation-chip:not(.citation-more)"),
  ).toHaveLength(8);
  expect(screen.getByRole("button", { name: "关闭" })).toBeTruthy();
});

test("确认卡片与正文 SQL 相同时只显示一次", async () => {
  const plan = {
    id: "query-dup",
    sql: "SELECT sales FROM shop.orders",
    data_source: "doris_ai",
    tables: ["shop.orders"],
    estimated_partitions: null,
    estimated_rows: null,
    row_limit: 100,
  };
  const conversation = {
    id: "conv-dup",
    title: "重复 SQL",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-dup")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "重复 SQL",
              status: "awaiting_confirmation",
              answer:
                "请确认。\n\n```sql\nselect   sales\nfrom   SHOP.orders\n```",
              sql: [
                {
                  sql: "select   sales\nfrom   SHOP.orders",
                  dialect: "hive",
                },
              ],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: null,
              events: [{ seq: 0, type: "query_plan", data: plan }],
            },
          ],
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await openHistory("重复 SQL");
  expect(await screen.findByText("确认执行这条 SQL？")).toBeTruthy();
  expect(container.querySelectorAll(".sql-block")).toHaveLength(1);
  expect(container.querySelectorAll(".code-block")).toHaveLength(0);
  expect(container.querySelector(".sql-block")?.textContent).toContain(
    "SELECT sales FROM shop.orders",
  );
});

test("执行失败后显示错误并可重试", async () => {
  const plan = {
    id: "query-retry",
    sql: "SELECT 1",
    data_source: "doris_ai",
    tables: [],
    estimated_partitions: null,
    estimated_rows: null,
    row_limit: 100,
  };
  const result = {
    id: plan.id,
    columns: ["value"],
    rows: [[1]],
    row_count: 1,
  };
  let attempts = 0;
  let executed = false;
  const conversation = {
    id: "conv-retry",
    title: "重试",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
    resumable: true,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([conversation]);
      if (url === "/api/v1/conversations/conv-retry")
        return json({
          ...conversation,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "重试",
              status: executed ? "completed" : "awaiting_confirmation",
              answer: executed ? "执行完成" : "",
              sql: [],
              citations: [],
              error: null,
              started_at: conversation.created_at,
              finished_at: executed ? conversation.updated_at : null,
              events: [
                { seq: 0, type: "query_plan", data: plan },
                ...(executed
                  ? [{ seq: 1, type: "query_result", data: result }]
                  : []),
              ],
            },
          ],
        });
      if (
        url ===
        "/api/v1/conversations/conv-retry/turns/1/queries/query-retry/execute"
      ) {
        attempts += 1;
        if (attempts === 1) {
          return new Response(JSON.stringify({ detail: "执行失败" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        executed = true;
        return new Response(
          `event: query_result\ndata: ${JSON.stringify(result)}\n\nevent: done\ndata: {"text":"执行完成"}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await openHistory("重试");
  expect(await screen.findByText("确认执行这条 SQL？")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*执\s*行/ }));
  expect((await screen.findAllByText("执行失败")).length).toBeGreaterThan(0);
  expect(
    screen.getByRole("button", { name: /确\s*认\s*执\s*行/ }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*执\s*行/ }));
  expect(await screen.findByText("执行完成")).toBeTruthy();
  expect(container.querySelector(".query-result-preview")).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: /确\s*认\s*执\s*行/ }),
  ).toBeNull();
  expect(attempts).toBe(2);
});

test("直播澄清完成后重载历史仍显示选项并提交原 id", async () => {
  const questions = [
    {
      id: "q-live",
      question: "销售额按支付口径还是下单口径？",
      options: [
        {
          id: "o-live-payment",
          label: "支付销售额（支付成功时间）",
          ref: "document:metrics/definitions/sales.md#chunk-1",
        },
        {
          id: "o-live-order",
          label: "下单销售额（订单创建时间）",
          ref: "document:metrics/sales_order.md#chunk-1",
        },
      ],
    },
  ];
  const row = conversationRow("conv-reloaded", "重载后的澄清");
  const firstSse = [
    'event: conversation\ndata: {"conversation_id":"conv-reloaded","turn_id":1}',
    `event: clarification\ndata: ${JSON.stringify({ questions })}`,
    'event: done\ndata: {"text":"请选择口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const secondSse = [
    'event: conversation\ndata: {"conversation_id":"conv-reloaded","turn_id":2}',
    'event: done\ndata: {"text":"按支付口径。","sql":[],"citations":[]}',
  ].join("\n\n");
  const askBodies: string[] = [];
  let detailFetches = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([row]);
      if (url === "/api/v1/conversations/ask") {
        askBodies.push(String(init?.body));
        return new Response(askBodies.length === 1 ? firstSse : secondSse, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url === "/api/v1/conversations/conv-reloaded") {
        detailFetches += 1;
        return json({
          ...row,
          turns: [
            {
              id: 1,
              sequence: 1,
              question: "上个月销售额是多少？",
              status: "completed",
              answer: "请选择口径。",
              sql: [],
              citations: [],
              error: null,
              started_at: row.created_at,
              finished_at: row.updated_at,
              events: [{ seq: 0, type: "clarification", data: { questions } }],
            },
          ],
        });
      }
      return new Response("", { status: 404 });
    }),
  );
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
      provider.models[0],
    ),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "上个月销售额是多少？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() => expect(detailFetches).toBeGreaterThan(0));
  const option = await screen.findByRole("button", {
    name: /支付销售额（支付成功时间）/,
  });
  expect(
    screen.getAllByRole("button", { name: /销售额（.*时间）/ }),
  ).toHaveLength(2);
  fireEvent.click(option);

  await waitFor(() => expect(askBodies).toHaveLength(2));
  const followUp = JSON.parse(askBodies[1]) as {
    question: string;
    conversation_id: string;
  };
  expect(followUp.conversation_id).toBe("conv-reloaded");
  expect(followUp.question).toContain("选项 id：o-live-payment");
  expect(followUp.question).not.toContain("选项 id：o-live-order");
  expect(followUp.question).toContain("支付销售额（支付成功时间）");
  expect(followUp.question).toContain(
    "document:metrics/definitions/sales.md#chunk-1",
  );
});

test.each([
  ["zh", "旧澄清", "该澄清的选项内容已丢失，请重新提问"],
  [
    "en",
    "Old clarification",
    "The options for this clarification were lost. Please ask again.",
  ],
] as const)(
  "%s 历史澄清的选项损坏时显示提示且不渲染按钮",
  async (lang, title, hint) => {
    const row = conversationRow("conv-corrupt", title);
    const messages = getMessages(lang);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/v1/agents")
          return json([{ id: "pi", name: "pi", available: true }]);
        if (url === "/api/v1/providers") return json([provider]);
        if (url === "/api/v1/conversations") return json([row]);
        if (url === "/api/v1/conversations/conv-corrupt")
          return json({
            ...row,
            turns: [
              {
                id: 1,
                sequence: 1,
                question: "销售额按哪个口径？",
                status: "completed",
                answer: "请选择口径。",
                sql: [],
                citations: [],
                error: null,
                started_at: row.created_at,
                finished_at: row.updated_at,
                events: [
                  {
                    seq: 0,
                    type: "clarification",
                    data: {
                      questions: [
                        {
                          id: "q1",
                          question: "销售额按支付口径还是下单口径？",
                          options: [
                            { id: "…", label: "…", ref: "…" },
                            { id: "…", label: "…", ref: "…" },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
            ],
          });
        return new Response("", { status: 404 });
      }),
    );
    const { container } = render(
      <ChatPage token="token" role="user" messages={messages} />,
    );
    await openHistory(title);
    expect(await screen.findByText(hint)).toBeTruthy();
    expect(screen.getByText("销售额按支付口径还是下单口径？")).toBeTruthy();
    const card = container.querySelector(".clarification-card") as HTMLElement;
    expect(card).toBeTruthy();
    expect(card.querySelectorAll("button")).toHaveLength(0);
  },
);
test("会话菜单提供重命名、置顶、归档和删除", async () => {
  const messages = getMessages("zh");
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      [conversationRow("conv-1", "订单分析")],
      () => new Response(null, { status: 204 }),
    ),
  );
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(await screen.findByRole("button", { name: "会话菜单" }));
  for (const name of ["重命名", "置顶", "归档", "删除"]) {
    expect(screen.getByText(name)).toBeTruthy();
  }
});

test("重命名支持保存和取消", async () => {
  const messages = getMessages("zh");
  const row = {
    ...conversationRow("conv-1", "订单分析"),
    pinned: false,
    archived: false,
  };
  const requests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([row]);
      if (url === "/api/v1/conversations?archived=true") return json([]);
      if (url.endsWith("/conv-1") && init?.method === "PATCH") {
        const patch = JSON.parse(String(init.body));
        requests.push(patch);
        row.title = patch.title.trim();
        return json(row);
      }
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(
    await screen.findByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.rename }));
  let editor = screen.getByDisplayValue("订单分析");
  fireEvent.change(editor, { target: { value: "新标题" } });
  fireEvent.keyDown(editor, { key: "Escape" });
  expect(requests).toEqual([]);
  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.rename }));
  editor = screen.getByDisplayValue("订单分析");
  fireEvent.change(editor, { target: { value: " 新标题 " } });
  fireEvent.keyDown(editor, { key: "Enter" });
  await screen.findByRole("button", { name: "新标题" });
  expect(requests).toEqual([{ title: " 新标题 " }]);
});

test("置顶分组与归档阅读、恢复", async () => {
  const messages = getMessages("zh");
  const rows = [
    {
      ...conversationRow("conv-1", "订单分析"),
      pinned: false,
      archived: false,
    },
    {
      ...conversationRow("conv-2", "客户分析"),
      pinned: false,
      archived: false,
    },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations")
        return json(
          rows
            .filter((row) => !row.archived)
            .sort((a, b) => Number(b.pinned) - Number(a.pinned)),
        );
      if (url === "/api/v1/conversations?archived=true")
        return json(rows.filter((row) => row.archived));
      const id = url.match(/^\/api\/v1\/conversations\/([^/]+)$/)?.[1];
      const row = rows.find((item) => item.id === id);
      if (row && init?.method === "PATCH") {
        Object.assign(row, JSON.parse(String(init.body)));
        return json(row);
      }
      if (row) return json({ ...row, turns: [] });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  fireEvent.click(
    (
      await screen.findAllByRole("button", { name: messages.conversationMenu })
    )[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.pin }));
  await waitFor(() =>
    expect(container.querySelector(".session-group")?.textContent).toBe(
      messages.pin,
    ),
  );
  fireEvent.click(
    screen.getAllByRole("button", { name: messages.conversationMenu })[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.archive }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "订单分析" })).toBeNull(),
  );
  fireEvent.click(screen.getByRole("button", { name: /已归档 \(1\)/ }));
  fireEvent.click(await screen.findByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(
      (
        screen.getByRole("textbox", {
          name: messages.askQuestion,
        }) as HTMLTextAreaElement
      ).disabled,
    ).toBe(true),
  );
  expect(screen.getByPlaceholderText(messages.restoreFirst)).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: messages.examples[0],
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  fireEvent.click(screen.getAllByRole("button", { name: messages.restore })[0]);
  await waitFor(() =>
    expect(container.querySelector(".session-group")?.textContent).toBe(
      messages.pin,
    ),
  );
});
