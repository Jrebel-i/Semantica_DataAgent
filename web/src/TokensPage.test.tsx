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
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
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

test("shows each token's projects and their roles", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") {
        return Response.json([
          {
            id: "a",
            name: "Alpha",
            type: "document",
            status: "active",
            created_at: created,
            my_role: "admin",
          },
        ]);
      }
      return Response.json([
        tokenRow({
          projects: [
            { project_id: "a", role: "admin" },
            { project_id: "missing", role: "member" },
          ],
        }),
      ]);
    }),
  );
  render(<TokensPage token="admin" messages={getMessages("zh")} />);

  expect(await screen.findByText("Alpha · 项目管理员")).toBeTruthy();
  expect(screen.getByText("missing · 成员")).toBeTruthy();
});

test("creates a token with selected projects and per-project roles", async () => {
  let posted: Record<string, unknown> | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") {
        return Response.json([
          {
            id: "a",
            name: "Alpha",
            type: "document",
            status: "active",
            created_at: created,
            my_role: "admin",
          },
          {
            id: "b",
            name: "Beta",
            type: "document",
            status: "active",
            created_at: created,
            my_role: "admin",
          },
        ]);
      }
      if (url === "/api/v1/tokens" && options?.method === "POST") {
        posted = JSON.parse(options.body as string);
        return Response.json(
          { ...tokenRow(), token: "one-time-secret" },
          { status: 201 },
        );
      }
      return Response.json([]);
    }),
  );
  render(<TokensPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "发放令牌" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Bob" },
  });
  fireEvent.mouseDown(screen.getByLabelText("选择项目"));
  fireEvent.click(
    await waitFor(() => {
      const option = document.querySelector(
        '.ant-select-item-option[title="Alpha"]',
      );
      if (!option) throw new Error("missing Alpha option");
      return option;
    }),
  );
  fireEvent.click(
    document.querySelector('.ant-select-item-option[title="Beta"]')!,
  );
  fireEvent.mouseDown(screen.getByLabelText("项目角色: Alpha"));
  fireEvent.click(
    await waitFor(() => {
      const option = document.querySelector(
        '.ant-select-item-option[title="项目管理员"]',
      );
      if (!option) throw new Error("missing admin option");
      return option;
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "创建" }));

  await waitFor(() => expect(posted).toBeTruthy());
  expect(posted).toEqual({
    name: "Bob",
    role: "user",
    projects: [
      { project_id: "a", role: "admin" },
      { project_id: "b", role: "member" },
    ],
  });
});
