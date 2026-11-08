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

test("会话项悬停或聚焦时显示菜单按钮", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(rows, () => new Response(null, { status: 204 })),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  const deleteButton = await screen.findByRole("button", {
    name: messages.conversationMenu,
  });
  const item = container.querySelector(".session-item") as HTMLElement;
  expect(deleteButton.className).not.toContain("visible");
  fireEvent.mouseEnter(item);
  expect(deleteButton.className).toContain("visible");
  fireEvent.mouseLeave(item);
  expect(deleteButton.className).not.toContain("visible");
  fireEvent.focusIn(item);
  expect(deleteButton.className).toContain("visible");
  fireEvent.focusOut(item);
  expect(deleteButton.className).not.toContain("visible");
});

test("删除会话需要弹窗确认，取消不发送请求", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  const onDelete = vi.fn(() => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", conversationFetcher(rows, onDelete));
  render(<ChatPage token="token" role="user" messages={messages} />);

  fireEvent.click(
    await screen.findByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  expect(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: /取\s*消/ })).toBeTruthy();
  expect(onDelete).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
  expect(onDelete).not.toHaveBeenCalled();

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );
  await waitFor(() => expect(onDelete).toHaveBeenCalledWith("conv-1"));
});

test("删除当前会话后从列表移除并回到新对话空状态", async () => {
  const messages = getMessages("zh");
  const rows = [
    conversationRow("conv-1", "订单分析"),
    conversationRow("conv-2", "客户分析"),
  ];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(rows, () => new Response(null, { status: 204 })),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getAllByRole("button", { name: messages.conversationMenu })[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "订单分析" })).toBeNull(),
  );
  expect(screen.getByRole("button", { name: "客户分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    messages.newConversation,
  );
  expect(container.querySelector(".session-item.active")).toBeNull();
  expect(screen.getByText(messages.chatGreeting)).toBeTruthy();
});

test("删除运行中的会话提示稍后再试且列表不变", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      rows,
      () =>
        new Response(JSON.stringify({ detail: "running" }), { status: 409 }),
    ),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  expect(
    await screen.findByText(messages.deleteConversationRunning),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "订单分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    "订单分析",
  );
  expect(container.querySelectorAll(".session-item")).toHaveLength(1);
});

test("删除他人会话返回 404 时提示失败且列表不变", async () => {
  const messages = getMessages("zh");
  const rows = [conversationRow("conv-1", "订单分析")];
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      rows,
      () =>
        new Response(JSON.stringify({ detail: "not found" }), { status: 404 }),
    ),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  await openHistory("订单分析");
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.deleteAction }));
  fireEvent.click(
    screen.getByRole("button", { name: messages.confirmDeleteConversation }),
  );

  expect(await screen.findByText(messages.operationFailed)).toBeTruthy();
  expect(screen.getByRole("button", { name: "订单分析" })).toBeTruthy();
  expect(container.querySelectorAll(".session-item")).toHaveLength(1);
});

test("会话菜单提供重命名、置顶、归档和删除", async () => {
  const messages = getMessages("zh");
  vi.stubGlobal(
    "fetch",
    conversationFetcher(
      [conversationRow("conv-1", "订单分析")],
      () => new Response(null, { status: 204 }),
    ),
  );
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(await screen.findByRole("button", { name: "会话菜单" }));
  for (const name of ["重命名", "置顶", "归档", "删除"]) {
    expect(screen.getByText(name)).toBeTruthy();
  }
});

test("重命名支持保存和取消", async () => {
  const messages = getMessages("zh");
  const row = {
    ...conversationRow("conv-1", "订单分析"),
    pinned: false,
    archived: false,
  };
  const requests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([row]);
      if (url === "/api/v1/conversations?archived=true") return json([]);
      if (url.endsWith("/conv-1") && init?.method === "PATCH") {
        const patch = JSON.parse(String(init.body));
        requests.push(patch);
        row.title = patch.title.trim();
        return json(row);
      }
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(
    await screen.findByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.rename }));
  let editor = screen.getByDisplayValue("订单分析");
  fireEvent.change(editor, { target: { value: "新标题" } });
  fireEvent.keyDown(editor, { key: "Escape" });
  expect(requests).toEqual([]);
  fireEvent.click(
    screen.getByRole("button", { name: messages.conversationMenu }),
  );
  fireEvent.click(screen.getByRole("button", { name: messages.rename }));
  editor = screen.getByDisplayValue("订单分析");
  fireEvent.change(editor, { target: { value: " 新标题 " } });
  fireEvent.keyDown(editor, { key: "Enter" });
  await screen.findByRole("button", { name: "新标题" });
  expect(requests).toEqual([{ title: " 新标题 " }]);
});

test("置顶分组与归档阅读、恢复", async () => {
  const messages = getMessages("zh");
  const rows = [
    {
      ...conversationRow("conv-1", "订单分析"),
      pinned: false,
      archived: false,
    },
    {
      ...conversationRow("conv-2", "客户分析"),
      pinned: false,
      archived: false,
    },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations")
        return json(
          rows
            .filter((row) => !row.archived)
            .sort((a, b) => Number(b.pinned) - Number(a.pinned)),
        );
      if (url === "/api/v1/conversations?archived=true")
        return json(rows.filter((row) => row.archived));
      const id = url.match(/^\/api\/v1\/conversations\/([^/]+)$/)?.[1];
      const row = rows.find((item) => item.id === id);
      if (row && init?.method === "PATCH") {
        Object.assign(row, JSON.parse(String(init.body)));
        return json(row);
      }
      if (row) return json({ ...row, turns: [] });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={messages} />,
  );
  fireEvent.click(
    (
      await screen.findAllByRole("button", { name: messages.conversationMenu })
    )[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.pin }));
  await waitFor(() =>
    expect(container.querySelector(".session-group")?.textContent).toBe(
      messages.pin,
    ),
  );
  fireEvent.click(
    screen.getAllByRole("button", { name: messages.conversationMenu })[0],
  );
  fireEvent.click(screen.getByRole("button", { name: messages.archive }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "订单分析" })).toBeNull(),
  );
  fireEvent.click(screen.getByRole("button", { name: /已归档 \(1\)/ }));
  fireEvent.click(await screen.findByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(
      (
        screen.getByRole("textbox", {
          name: messages.askQuestion,
        }) as HTMLTextAreaElement
      ).disabled,
    ).toBe(true),
  );
  expect(screen.getByPlaceholderText(messages.restoreFirst)).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: messages.examples[0],
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  fireEvent.click(screen.getAllByRole("button", { name: messages.restore })[0]);
  await waitFor(() =>
    expect(container.querySelector(".session-group")?.textContent).toBe(
      messages.pin,
    ),
  );
});
