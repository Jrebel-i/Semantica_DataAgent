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
