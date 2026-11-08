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
