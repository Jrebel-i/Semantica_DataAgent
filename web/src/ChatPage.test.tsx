// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ChatPage } from "./ChatPage";
import { getMessages } from "./i18n";

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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: undefined,
  });
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
    if (url.startsWith("/api/v1/tables?")) return json([table]);
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

  fireEvent.click(
    screen.getByRole("button", {
      name: "document:metrics/definitions/sales.md#chunk-1",
    }),
  );
  expect(
    await screen.findByText("支付销售额按支付成功时间归属日期。"),
  ).toBeTruthy();
  expect(fetcher).toHaveBeenCalledWith(
    "/api/v1/document?doc_id=metrics%2Fdefinitions%2Fsales.md",
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
  expect(screen.getByText("mcp__dataagent__search_knowledge")).toBeTruthy();
  expect(screen.getByText("按支付日还是下单日？")).toBeTruthy();
  expect(screen.getByText("1 turns")).toBeTruthy();
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
