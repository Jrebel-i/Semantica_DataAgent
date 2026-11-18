/// <reference types="node" />
// @vitest-environment jsdom
import { expect, test, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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

test("新对话空白态显示项目类型引导、知识库入口，并随项目类型切换示例问题", async () => {
  localStorage.clear();
  const messages = getMessages("zh");
  const onNavigateKnowledge = vi.fn();
  const projects = [
    {
      id: "default",
      name: "业务数据",
      type: "warehouse",
      status: "active",
      created_at: "2026-11-05T00:00:00Z",
      my_role: "member",
    },
    {
      id: "docs-proj",
      name: "文档规范库",
      type: "document",
      status: "active",
      created_at: "2026-11-05T00:00:00Z",
      my_role: "member",
    },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/projects") return json(projects);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations?archived=true") return json([]);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_provider_id: provider.id,
          assistant_model: provider.models[0],
        });
      return new Response("", { status: 404 });
    }),
  );

  const ChatPageWithNav = ChatPage as unknown as (props: {
    token: string;
    role: string;
    messages: typeof messages;
    onNavigateKnowledge?: () => void;
  }) => ReturnType<typeof ChatPage>;

  const { container } = render(
    <ChatPageWithNav
      token="token"
      role="user"
      messages={messages}
      onNavigateKnowledge={onNavigateKnowledge}
    />,
  );

  await waitFor(() =>
    expect(container.querySelector(".chat-project-guide")).toBeTruthy(),
  );
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    messages.newConversation,
  );
  expect(container.querySelector(".chat-header-project")?.textContent).toBe(
    "业务数据",
  );
  expect(container.querySelector(".chat-project-guide")?.textContent).toContain(
    "数仓型项目",
  );
  expect(
    screen.getByRole("button", { name: messages.examples[0] }),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("link", { name: "浏览该项目知识库 →" }));
  expect(onNavigateKnowledge).toHaveBeenCalledTimes(1);

  fireEvent.change(screen.getByLabelText("项目"), {
    target: { value: "docs-proj" },
  });

  await waitFor(() =>
    expect(
      container.querySelector(".chat-project-guide")?.textContent,
    ).toContain("文档型项目"),
  );
  expect(container.querySelector(".chat-header-project")?.textContent).toBe(
    "文档规范库",
  );
  expect(
    screen.getByRole("button", { name: "核心业务口径是如何定义的？" }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: messages.examples[0] }),
  ).toBeNull();
});

test("轮次底部 👍 准确 仅对通过 SQL 校验的回答开放并写入评测题集", async () => {
  const feedbackCalls: { url: string; body: unknown }[] = [];
  const conv = {
    id: "conv-fb",
    project_id: "default",
    project_name: "业务数据",
    title: "订单统计核对",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 2,
  };
  const detail = {
    ...conv,
    turns: [
      {
        id: 101,
        sequence: 1,
        question: "介绍订单系统",
        status: "done",
        answer: "订单系统记录客户交易。",
        sql: [],
        citations: [],
        error: null,
        started_at: "2026-11-05T00:00:00Z",
        finished_at: "2026-11-05T00:00:01Z",
        events: [],
        has_validated_sql: false,
        feedback_accurate: false,
      },
      {
        id: 102,
        sequence: 2,
        question: "昨日已支付订单数是多少",
        status: "done",
        answer: "共 42 单。",
        sql: [
          {
            sql: "SELECT COUNT(*) FROM shop.orders WHERE status = 'paid'",
            dialect: "hive",
          },
        ],
        citations: [{ ref: "table:shop.orders", type: "table" }],
        error: null,
        started_at: "2026-11-05T00:01:00Z",
        finished_at: "2026-11-05T00:01:01Z",
        events: [],
        has_validated_sql: true,
        feedback_accurate: false,
      },
    ],
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/projects")
        return json([
          {
            id: "default",
            name: "业务数据",
            type: "warehouse",
            status: "active",
            created_at: "2026-11-05T00:00:00Z",
            my_role: "member",
          },
        ]);
      if (url === "/api/v1/conversations") return json([conv]);
      if (url === "/api/v1/conversations/conv-fb") return json(detail);
      if (
        url === "/api/v1/conversations/conv-fb/turns/102/feedback" &&
        init?.method === "POST"
      ) {
        const body = JSON.parse(String(init.body));
        feedbackCalls.push({ url, body });
        return Response.json(
          {
            rating: "accurate",
            question_id: "eq-102",
            answer_confirmed: false,
            split: "dev",
            eval_question: { id: "eq-102", answer_confirmed: false },
          },
          { status: 201 },
        );
      }
      return new Response("", { status: 404 });
    }),
  );

  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "订单统计核对" }));
  expect(await screen.findByText("共 42 单。")).toBeTruthy();

  const accurateBtns = screen.getAllByRole("button", { name: /准确/ });
  expect(accurateBtns).toHaveLength(2);
  expect((accurateBtns[0] as HTMLButtonElement).disabled).toBe(true);
  expect((accurateBtns[1] as HTMLButtonElement).disabled).toBe(false);

  fireEvent.click(accurateBtns[1]);
  await waitFor(() =>
    expect(feedbackCalls).toEqual([
      {
        url: "/api/v1/conversations/conv-fb/turns/102/feedback",
        body: { rating: "accurate" },
      },
    ]),
  );
  expect(
    await screen.findByRole("button", { name: /已标为准确/ }),
  ).toBeTruthy();
});

test("轮次底部 🛠️ 纠错抽屉支持生成草稿预览、门禁拦截与提交提案", async () => {
  const requests: { url: string; method?: string; body?: unknown }[] = [];
  let gatePassed = false;
  const conv = {
    id: "conv-draft",
    project_id: "default",
    project_name: "业务数据",
    title: "GMV 纠错会话",
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 1,
  };
  const detail = {
    ...conv,
    turns: [
      {
        id: 201,
        sequence: 1,
        question: "GMV 是否包含退款？",
        status: "done",
        answer: "统计 orders 表 amount 字段。",
        sql: [{ sql: "SELECT SUM(amount) FROM shop.orders", dialect: "hive" }],
        citations: [{ ref: "table:shop.orders", type: "table" }],
        error: null,
        started_at: "2026-11-05T00:00:00Z",
        finished_at: "2026-11-05T00:00:01Z",
        events: [],
        has_validated_sql: true,
        feedback_accurate: false,
      },
    ],
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ url, method: init?.method, body });
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/projects")
        return json([
          {
            id: "default",
            name: "业务数据",
            type: "warehouse",
            status: "active",
            created_at: "2026-11-05T00:00:00Z",
            my_role: "admin",
          },
        ]);
      if (url === "/api/v1/conversations") return json([conv]);
      if (url === "/api/v1/conversations/conv-draft") return json(detail);
      if (url === "/api/v1/corrections/draft" && init?.method === "POST") {
        return json({
          target: "table:shop.orders",
          target_kind: "override",
          patch:
            "version: 1\ntables:\n  shop.orders:\n    instructions: 排除退款",
          structured_patch: {
            version: 1,
            tables: {
              "shop.orders": {
                database: "shop",
                name: "orders",
                instructions: "排除退款",
              },
            },
          },
          diff: "--- overrides/semantic_overrides.yaml\n+++ overrides/semantic_overrides.yaml\n+instructions: 排除退款",
          gate_report: gatePassed
            ? { passed: true, checks: [], errors: [] }
            : {
                passed: false,
                checks: [],
                errors: ["检测到非法 SQL 语句"],
              },
          resolved_path: "overrides/semantic_overrides.yaml",
          reason: "补充订单退款过滤规则",
          source_generated: true,
          used_llm: false,
        });
      }
      if (
        url === "/api/v1/corrections?project=default" &&
        init?.method === "POST"
      ) {
        return Response.json(
          { id: "prop-201", status: "pending" },
          { status: 201 },
        );
      }
      return new Response("", { status: 404 });
    }),
  );

  render(<ChatPage token="token" role="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "GMV 纠错会话" }));
  expect(await screen.findByText("统计 orders 表 amount 字段。")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "🛠️ 纠错" }));
  const drawer = await screen.findByRole("dialog");
  fireEvent.change(within(drawer).getByLabelText("纠错问题描述"), {
    target: { value: "统计 GMV 时需要排除退款订单" },
  });
  fireEvent.click(within(drawer).getByRole("button", { name: "生成草稿预览" }));

  expect(await within(drawer).findByText("L1 预检未通过")).toBeTruthy();
  expect(within(drawer).getByText("检测到非法 SQL 语句")).toBeTruthy();
  const submitDisabled = within(drawer).getByRole("button", {
    name: "提出纠错",
  }) as HTMLButtonElement;
  expect(submitDisabled.disabled).toBe(true);

  gatePassed = true;
  fireEvent.click(
    await within(drawer).findByRole("button", { name: "生成草稿预览" }),
  );
  expect(await within(drawer).findByText("L1 预检通过")).toBeTruthy();
  expect(
    within(drawer).getByText(/目标文件:\s*overrides\/semantic_overrides\.yaml/),
  ).toBeTruthy();

  const submitBtn = within(drawer).getByRole("button", {
    name: "提出纠错",
  }) as HTMLButtonElement;
  expect(submitBtn.disabled).toBe(false);
  fireEvent.click(submitBtn);

  await waitFor(() =>
    expect(
      requests.some(
        (req) =>
          req.url === "/api/v1/corrections?project=default" &&
          req.method === "POST",
      ),
    ).toBe(true),
  );
});
