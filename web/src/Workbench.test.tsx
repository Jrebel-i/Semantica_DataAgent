// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "./App";

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
    writable: true,
    value: () => ({ getPropertyValue: () => "" }),
  });
});
afterEach(() => {
  cleanup();
  history.replaceState(null, "", "/");
  localStorage.clear();
  vi.unstubAllGlobals();
});

test("工作台提供独立的问答、知识、构建与审批和配置入口", async () => {
  localStorage.setItem("token", "admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      if (url === "/healthz") return Response.json({});
      return Response.json({});
    }),
  );
  render(<App />);
  expect(await screen.findByRole("button", { name: "新对话" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "用户菜单" }));
  const management = screen.getByRole("button", { name: "管理" });
  expect(management.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("link", { name: "构建记录" })).toBeNull();
  fireEvent.click(management);
  expect(management.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("link", { name: "构建记录" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "配置" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "用户菜单" }));
  fireEvent.click(screen.getByRole("button", { name: "用户菜单" }));
  expect(
    screen.getByRole("button", { name: "管理" }).getAttribute("aria-expanded"),
  ).toBe("false");
  expect(screen.queryByRole("link", { name: "构建记录" })).toBeNull();
});

test("普通用户只能看到问答、知识和个人设置，导航展开状态可恢复", async () => {
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (
        url === "/api/v1/agents" ||
        url === "/api/v1/providers" ||
        url === "/api/v1/conversations" ||
        url === "/api/v1/conversations?archived=true"
      )
        return Response.json([]);
      return Response.json({});
    }),
  );
  render(<App />);
  expect(await screen.findByRole("button", { name: "新对话" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "用户菜单" }));
  expect(screen.getByRole("link", { name: "个人设置" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "管理" })).toBeNull();
  expect(screen.queryByRole("link", { name: "构建记录" })).toBeNull();
  expect(screen.queryByRole("link", { name: "配置" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "收起导航" }));
  expect(localStorage.getItem("navigationExpanded")).toBe("false");
  expect(screen.getByRole("button", { name: "展开导航" })).toBeTruthy();
});

test("侧栏会话可从搜索页切换且只加载一次列表", async () => {
  history.replaceState(null, "", "/search");
  localStorage.setItem("token", "user");
  const conversations = ["first", "second"].map((id) => ({
    id,
    title: id === "first" ? "第一个会话" : "第二个会话",
    agent: "pi",
    provider_id: "p",
    model: "m",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: 0,
    resumable: true,
  }));
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/me") return Response.json({ role: "user" });
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    if (url === "/api/v1/conversations") return Response.json(conversations);
    if (url === "/api/v1/conversations?archived=true") return Response.json([]);
    if (url.startsWith("/api/v1/conversations/")) {
      const row = conversations.find((item) => url.endsWith(item.id));
      return Response.json({ ...row, turns: [] });
    }
    if (url === "/api/v1/agents" || url === "/api/v1/providers")
      return Response.json([]);
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "第二个会话" }));
  expect(
    await screen.findByText("第二个会话", {
      selector: ".chat-header-title strong",
    }),
  ).toBeTruthy();
  expect(
    fetcher.mock.calls.filter(([url]) => url === "/api/v1/conversations"),
  ).toHaveLength(1);
});

test("用户菜单可切换语言和刷新会话", async () => {
  localStorage.setItem("token", "user");
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/me") return Response.json({ role: "user" });
    if (
      ["/api/v1/agents", "/api/v1/providers", "/api/v1/conversations"].includes(
        url,
      )
    )
      return Response.json([]);
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  await screen.findByRole("button", { name: "新对话" });
  expect(screen.queryByRole("link", { name: "个人设置" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "用户菜单" }));
  expect(screen.getByRole("link", { name: "个人设置" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await vi.waitFor(() =>
    expect(
      fetcher.mock.calls.filter(([url]) => url === "/api/v1/conversations")
        .length,
    ).toBeGreaterThan(1),
  );
  fireEvent.click(screen.getByRole("button", { name: "English" }));
  expect(
    await screen.findByRole("link", { name: "Personal settings" }),
  ).toBeTruthy();
});
