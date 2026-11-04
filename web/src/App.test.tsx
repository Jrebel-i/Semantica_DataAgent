// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "./App";

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: () => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
    }),
  });
  Object.defineProperty(window, "getComputedStyle", {
    value: () => ({ getPropertyValue: () => "" }),
    writable: true,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("sign in and render dependency states", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/me"
        ? new Response("{}", { status: 200 })
        : new Response(
            JSON.stringify({
              neo4j: { status: "up", elapsed_ms: 1 },
              milvus: { status: "down", elapsed_ms: 2 },
            }),
            { status: 200 },
          ),
    ),
  );
  render(<App />);
  fireEvent.change(screen.getByLabelText("粘贴个人令牌"), {
    target: { value: "token" },
  });
  fireEvent.click(screen.getByRole("button", { name: /登\s*录/ }));
  expect(await screen.findByText("正常")).toBeTruthy();
  expect(screen.getByText("不可用")).toBeTruthy();
  expect(localStorage.getItem("token")).toBe("token");
});

test("401 returns to login", async () => {
  localStorage.setItem("token", "bad");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("", { status: 401 })),
  );
  render(<App />);
  expect(await screen.findByText("令牌无效")).toBeTruthy();
  await waitFor(() => expect(localStorage.getItem("token")).toBeNull());
  expect(screen.getByRole("button", { name: /登\s*录/ })).toBeTruthy();
});

test("language selection persists", () => {
  render(<App />);
  fireEvent.click(screen.getByText("English"));
  expect(screen.getByText("System status")).toBeTruthy();
  expect(localStorage.getItem("language")).toBe("en");
});

test("admin issues and revokes a token", async () => {
  let rows: object[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      if (url === "/healthz") return Response.json({});
      if (url === "/api/v1/tokens" && options?.method === "POST") {
        rows = [
          {
            id: "1",
            name: "Alice",
            role: "user",
            created_at: "2026-01-01T00:00:00Z",
            last_used_at: null,
            revoked: false,
          },
        ];
        return Response.json(
          { ...rows[0], token: "one-time-secret" },
          { status: 201 },
        );
      }
      if (url === "/api/v1/tokens/1" && options?.method === "DELETE") {
        rows = [{ ...rows[0], revoked: true }];
        return new Response(null, { status: 204 });
      }
      return Response.json(rows);
    }),
  );
  localStorage.setItem("token", "admin");
  render(<App />);
  fireEvent.click(await screen.findByRole("link", { name: "令牌管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "发放令牌" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Alice" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建" }));
  expect(await screen.findByText("one-time-secret")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(screen.queryByText("one-time-secret")).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "吊销" }));
  fireEvent.click(screen.getByRole("button", { name: "确认吊销" }));
  expect(await screen.findByText("已吊销")).toBeTruthy();
});

test("user cannot see token navigation or open its page", async () => {
  history.replaceState(null, "", "/tokens");
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/me"
        ? Response.json({ role: "user" })
        : Response.json({}),
    ),
  );
  render(<App />);
  expect(await screen.findByText("无权限访问")).toBeTruthy();
  expect(screen.queryByRole("link", { name: "令牌管理" })).toBeNull();
  history.replaceState(null, "", "/");
});
test("browse tables and view field details", async () => {
  localStorage.setItem("token", "token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return new Response("{}", { status: 200 });
      if (url === "/api/v1/categories") return Response.json([]);
      if (url.startsWith("/api/v1/table-lineage"))
        return Response.json({ upstream: [], downstream: [], tasks: [] });
      if (url.startsWith("/api/v1/tables"))
        return new Response(
          JSON.stringify([
            {
              database: "shop",
              name: "orders",
              comment: "交易订单",
              ref: "table:shop.orders",
              fields: [
                {
                  name: "dt",
                  type: "string",
                  comment: "日期分区",
                  partition: true,
                },
              ],
            },
          ]),
          { status: 200 },
        );
      return new Response("{}", { status: 200 });
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByRole("link", { name: "知识浏览" }));
  fireEvent.click(await screen.findByText("orders"));
  expect(await screen.findByText("日期分区")).toBeTruthy();
  expect(screen.getByText("string")).toBeTruthy();
});
