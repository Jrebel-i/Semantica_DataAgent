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

test("没有上下文时隐藏面板和开关", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (
        [
          "/api/v1/agents",
          "/api/v1/providers",
          "/api/v1/conversations",
        ].includes(url)
      )
        return json([]);
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  expect(container.querySelector(".chat-layout.context-hidden")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "收起面板" })).toBeNull();
  expect(screen.queryByRole("button", { name: "展开面板" })).toBeNull();
});

test("后打开的引用不会被较晚返回的旧请求覆盖", async () => {
  let resolveTable!: (response: Response) => void;
  const delayedTable = new Promise<Response>((resolve) => {
    resolveTable = resolve;
  });
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/conversations/ask")
      return new Response(sse, {
        headers: { "Content-Type": "text/event-stream" },
      });
    if (url.startsWith("/api/v1/tables/")) return delayedTable;
    if (url.startsWith("/api/v1/document?")) return json(document);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect(composerSelectText("模型")).toBe(provider.models[0]),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "支付销售额？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: "table:shop.payments" }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "document:metrics/definitions/sales.md#chunk-1",
    }),
  );
  expect(
    await screen.findByText("支付销售额按支付成功时间归属日期。"),
  ).toBeTruthy();
  expect(
    window.document.querySelector(".context-header strong")?.textContent,
  ).toBe("sales");
  await act(async () => {
    resolveTable(json([table]));
    await delayedTable;
  });
  expect(screen.getByText("支付销售额按支付成功时间归属日期。")).toBeTruthy();
  expect(screen.queryByText("支付流水")).toBeNull();
});

test("文档引用解码文件名并在抽屉渲染 Markdown", async () => {
  const name = "销售 50% #报告.md";
  const ref = `document:指标/${encodeURIComponent(name)}#chunk-1`;
  const response = sse
    .replace('"is_error":false', '"is_error":true')
    .replace("document:metrics/definitions/sales.md#chunk-1", ref);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(response, {
          headers: { "Content-Type": "text/event-stream" },
        });
      if (url.startsWith("/api/v1/document?"))
        return json({
          ...document,
          path: `definitions/${encodeURIComponent(name)}`,
          content: "# 销售标题\n\n**重要内容**",
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect(composerSelectText("模型")).toBe(provider.models[0]),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查看" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const chip = await screen.findByRole("button", { name: ref });
  expect(chip.textContent).toContain(name.replace(/\.md$/, ""));
  expect(chip.getAttribute("title")).toBe(`指标/${name}`);
  expect(container.querySelector(".chat-context")).toBeNull();
  expect(
    container
      .querySelector(".tool-log > button")
      ?.getAttribute("aria-expanded"),
  ).toBe("false");
  expect(screen.getByText("其中 1 个失败")).toBeTruthy();
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "销售标题" })).tagName,
  ).toBe("H1");
  expect(screen.getByText("重要内容").tagName).toBe("STRONG");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    name.replace(/\.md$/, ""),
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(
    `definitions/${name}`,
  );
  const drawer = container.querySelector(".chat-context");
  const header = drawer?.querySelector(":scope > .context-header");
  const body = drawer?.querySelector(":scope > .context-body");
  expect(header).toBeTruthy();
  expect(body).toBeTruthy();
  expect(header?.contains(container.querySelector(".context-ref"))).toBe(true);
  expect(container.querySelector(".context-body .doc-content")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(container.querySelector(".chat-context")).toBeNull();
  expect(container.querySelector(".chat-layout.context-hidden")).toBeTruthy();
});

test("文档路径编码非法时抽屉和悬停提示原样显示", async () => {
  const path = "definitions/%E9%94.md";
  const ref = `document:${path}#chunk-1`;
  const response = sse
    .replace('"is_error":false', '"is_error":true')
    .replace("document:metrics/definitions/sales.md#chunk-1", ref);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/conversations/ask")
        return new Response(response, {
          headers: { "Content-Type": "text/event-stream" },
        });
      if (url.startsWith("/api/v1/document?"))
        return json({
          ...document,
          path,
          content: "# 非法编码\n\n文件仍可查看。",
        });
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );
  await waitFor(() =>
    expect(composerSelectText("模型")).toBe(provider.models[0]),
  );
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "查看" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  const chip = await screen.findByRole("button", { name: ref });
  expect(chip.textContent).toContain("%E9%94.md");
  expect(chip.getAttribute("title")).toBe(path);
  fireEvent.click(chip);
  expect(
    (await screen.findByRole("heading", { name: "非法编码" })).tagName,
  ).toBe("H1");
  expect(container.querySelector(".context-header strong")?.textContent).toBe(
    "%E9%94.md",
  );
  expect(container.querySelector(".context-ref")?.textContent).toBe(path);
});
