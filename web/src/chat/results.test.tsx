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
