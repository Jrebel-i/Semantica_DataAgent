// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ChatPage } from "./ChatPage";
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

test("查询计划可确认执行，结果和下载显示在对话中", async () => {
  const plan = {
    id: "query-81",
    sql: "SELECT sales FROM shop.orders",
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
  expect(
    (await screen.findAllByText("SELECT sales FROM shop.orders")).length,
  ).toBeGreaterThan(0);
  expect(executed).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: /执\s*行/ }));
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
  await screen.findByText("old-value");
  expect(container.querySelectorAll(".query-result-preview")).toHaveLength(2);
  expect(screen.getByText("new-value")).toBeTruthy();
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
  ).toBe("sales.md");
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
  render(
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

test("新会话发送所选 agent 与模型并在建立后锁定", async () => {
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
    true,
  );
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
  ).toBe("sales.md");
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

test("文档引用解码文件名并在抽屉渲染 Markdown，失败工具自动展开", async () => {
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
  expect(chip.textContent).toContain(name);
  expect(chip.getAttribute("title")).toBe(`document:指标/${name}#chunk-1`);
  expect(container.querySelector(".chat-context")).toBeNull();
  expect(container.querySelector(".tool-step")?.hasAttribute("open")).toBe(
    true,
  );
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "销售标题" })).tagName,
  ).toBe("H1");
  expect(screen.getByText("重要内容").tagName).toBe("STRONG");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    name,
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(
    `definitions/${name}`,
  );
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
  expect(chip.getAttribute("title")).toBe(ref);
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "非法编码" })).tagName,
  ).toBe("H1");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    "%E9%94.md",
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(path);
});
