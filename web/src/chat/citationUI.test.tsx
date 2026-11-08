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
