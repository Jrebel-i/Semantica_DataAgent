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
import { clearTableDetailCache } from "./tableDetail";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
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
  clearTableDetailCache();
  vi.restoreAllMocks();
});

test("sign in and render dependency states", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/me"
        ? Response.json({ role: "admin" })
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
  expect(await screen.findByText(/neo4j: 正常.*1 ms/)).toBeTruthy();
  expect(screen.getByText(/milvus: 不可用.*2 ms/)).toBeTruthy();
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
  fireEvent.click(await screen.findByRole("button", { name: "用户菜单" }));
  fireEvent.click(await screen.findByRole("button", { name: "管理" }));
  fireEvent.click(await screen.findByRole("link", { name: "配置" }));
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
      if (url.startsWith("/api/v1/document?"))
        return Response.json({ path: "sales.md", content: "订单定义" });
      if (url.startsWith("/api/v1/table-lineage"))
        return Response.json({ upstream: [], downstream: [], tasks: [] });
      if (url.startsWith("/api/v1/column-lineage"))
        return Response.json({ upstream: [], downstream: [] });
      if (url === "/api/v1/tables/shop/orders")
        return new Response(
          JSON.stringify({
            database: "shop",
            name: "orders",
            comment: "交易订单",
            ref: "table:shop.orders",
            documents: [
              {
                ref: "document:metrics/sales.md",
                category: "metrics",
                path: "sales.md",
              },
            ],
            fields: [
              {
                name: "dt",
                type: "string",
                comment: "日期分区",
                partition: true,
              },
            ],
          }),
          { status: 200 },
        );
      if (url.startsWith("/api/v1/tables"))
        return new Response(
          JSON.stringify([
            {
              database: "shop",
              name: "orders",
              comment: "交易订单",
              ref: "table:shop.orders",
              field_count: 1,
              document_count: 1,
              has_partition: true,
            },
          ]),
          { status: 200 },
        );
      return new Response("{}", { status: 200 });
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByRole("link", { name: "知识库" }));
  fireEvent.click(await screen.findByText("orders"));
  expect(await screen.findByText("日期分区")).toBeTruthy();
  expect(screen.getByText("string")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "dt" }));
  expect(await screen.findByText(/暂无字段级血缘.*未能解析/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "dt" }));
  expect(screen.queryByText(/暂无字段级血缘/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "sales.md" }));
  expect(await screen.findByText("订单定义")).toBeTruthy();
});

test("search debug is accessible from navigation", async () => {
  history.replaceState(null, "", "/");
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/categories" || url.startsWith("/api/v1/tables"))
        return Response.json([]);
      return Response.json({});
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByRole("link", { name: "知识库" }));
  fireEvent.click(await screen.findByRole("link", { name: "检索调试" }));
  expect(screen.getByRole("heading", { name: "检索调试" })).toBeTruthy();
  expect(screen.getByLabelText("查询内容")).toBeTruthy();
});

test("MCP access page is reachable from navigation", async () => {
  history.replaceState(null, "", "/");
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
  fireEvent.click(await screen.findByRole("button", { name: "用户菜单" }));
  fireEvent.click(await screen.findByRole("link", { name: "个人设置" }));
  fireEvent.click(await screen.findByRole("link", { name: "MCP 接入" }));
  expect(screen.getByRole("heading", { name: "MCP 接入" })).toBeTruthy();
  expect(screen.getByLabelText("MCP 连接地址")).toBeTruthy();
  history.replaceState(null, "", "/");
});

test("user cannot access correction review", async () => {
  localStorage.setItem("token", "reader");
  history.replaceState(null, "", "/corrections");
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
  expect(screen.queryByRole("button", { name: "批准" })).toBeNull();
  expect(screen.queryByText("纠错审批")).toBeNull();
});

test("status bar distinguishes knowledge generations by suffix and tooltip", async () => {
  history.replaceState(null, "", "/");
  const name = "default_4177a9e7c8a844aa9a889b5166999f";
  localStorage.setItem("token", "admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      if (url === "/api/v1/generations")
        return Response.json({
          generations: [
            {
              name,
              current: true,
              created_at: "2026-11-07T03:51:00Z",
              source_commit: null,
              counts: {},
            },
            {
              name: "default_4177a9e7c8a844aa9a889b5166999e",
              current: false,
              created_at: null,
              source_commit: null,
              counts: {},
            },
          ],
          source_head: null,
          behind: false,
        });
      return Response.json({});
    }),
  );
  render(<App />);

  const suffix = await screen.findByText(/5166999f/);
  expect(suffix.textContent).toContain("5166999f");
  expect(suffix.textContent).toMatch(/· \d{2}-\d{2} \d{2}:\d{2}/);
  const item = suffix.closest(".status-item");
  expect(item?.getAttribute("title")).toBe(name);
  expect(screen.queryByText(/default_4177a9e7c8a844aa9a…/)).toBeNull();
});

test("status bar is rendered for admins", async () => {
  history.replaceState(null, "", "/");
  localStorage.setItem("token", "admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      return Response.json({});
    }),
  );
  const { container } = render(<App />);
  await screen.findByRole("button", { name: "用户菜单" });
  expect(container.querySelector(".status-bar")).not.toBeNull();
  expect(screen.getByText(/当前代/)).toBeTruthy();
  expect(screen.getByText(/DTB token/)).toBeTruthy();
});

test("status bar is hidden for regular users", async () => {
  history.replaceState(null, "", "/");
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      return Response.json({});
    }),
  );
  const { container } = render(<App />);
  await screen.findByRole("button", { name: "用户菜单" });
  expect(container.querySelector(".status-bar")).toBeNull();
  expect(screen.queryByText(/当前代/)).toBeNull();
  expect(screen.queryByText(/DTB token/)).toBeNull();
});

test("admin opens the projects page from the management menu", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "default",
            name: "业务数据",
            type: "warehouse",
            status: "active",
            created_at: "2026-01-01T00:00:00Z",
            my_role: "admin",
          },
          {
            id: "docs",
            name: "Platform Docs",
            type: "document",
            status: "active",
            created_at: "2026-01-02T00:00:00Z",
            my_role: "admin",
          },
        ]);
      if (url === "/healthz") return Response.json({});
      return Response.json({});
    }),
  );
  localStorage.setItem("token", "admin");
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "用户菜单" }));
  fireEvent.click(await screen.findByRole("button", { name: "管理" }));
  fireEvent.click(await screen.findByRole("link", { name: "项目" }));
  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  expect(screen.getByText("数仓型")).toBeTruthy();
  expect(screen.getByText("文档型")).toBeTruthy();
  history.replaceState(null, "", "/");
});

test("regular users cannot see or open the projects page", async () => {
  history.replaceState(null, "", "/projects");
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
  expect(screen.queryByRole("link", { name: "项目" })).toBeNull();
  history.replaceState(null, "", "/");
});
