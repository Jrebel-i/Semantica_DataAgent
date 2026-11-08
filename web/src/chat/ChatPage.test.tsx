/// <reference types="node" />
// @vitest-environment jsdom
import { expect, test, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ChatPage } from "../ChatPage";
import { getMessages } from "../i18n";
import {
  composerSelectText,
  conversationFetcher,
  conversationRow,
  cssDeclarations,
  document,
  json,
  openHistory,
  provider,
  setupChatTestEnv,
  sse,
  table,
  toolStream,
  toolTurnEvents,
} from "./testUtils";

setupChatTestEnv();

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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
