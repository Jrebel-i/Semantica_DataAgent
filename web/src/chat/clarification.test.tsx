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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
    expect(composerSelectText("模型")).toBe(provider.models[0]),
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
