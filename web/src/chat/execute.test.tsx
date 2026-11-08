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
