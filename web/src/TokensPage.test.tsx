// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { TokensPage } from "./TokensPage";
import { getMessages } from "./i18n";

const created = "2026-11-05T19:27:00+08:00";

function tokenRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "token-1",
    name: "Alice",
    role: "user",
    created_at: created,
    last_used_at: null,
    revoked: false,
    ...overrides,
  };
}

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
    }),
  });
  Object.defineProperty(window, "getComputedStyle", {
    configurable: true,
    value: () => ({ getPropertyValue: () => "" }),
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("renders tokens with antd and formats the server UTC value directly", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json([tokenRow()])),
  );
  render(<TokensPage token="admin" messages={getMessages("zh")} />);

  expect(await screen.findByText("Alice")).toBeTruthy();
  expect(screen.getByText("用户")).toBeTruthy();
  expect(screen.getByText(new Date(created).toLocaleString())).toBeTruthy();
  expect(screen.queryByText(/Invalid Date/)).toBeNull();
  expect(screen.getByText("从未使用")).toBeTruthy();
  expect(document.querySelector(".ant-table")).toBeTruthy();
  expect(document.querySelector(".ant-btn")).toBeTruthy();
});

test("issues a token from an antd modal and copies it", async () => {
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, options?: RequestInit) => {
      if (options?.method === "POST")
        return Response.json(
          { ...tokenRow(), token: "one-time-secret" },
          { status: 201 },
        );
      return Response.json([tokenRow()]);
    }),
  );
  render(<TokensPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "发放令牌" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Alice" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建" }));
  expect(await screen.findByText("one-time-secret")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "复制" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith("one-time-secret"),
  );

  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByText("one-time-secret")).toBeNull());
  expect(dialog).toBeTruthy();
});

test("revokes a token through the antd confirmation modal", async () => {
  let rows = [tokenRow()];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        rows = [tokenRow({ revoked: true })];
        return new Response(null, { status: 204 });
      }
      return Response.json(rows);
    }),
  );
  render(<TokensPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "吊销" }));
  expect(await screen.findByRole("dialog")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "确认吊销" }));
  expect(await screen.findByText("已吊销")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "吊销" })).toBeNull();
});
