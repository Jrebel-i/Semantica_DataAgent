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
  expect(await screen.findByRole("link", { name: "问答" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "构建与审批" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "配置" })).toBeTruthy();
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
        url === "/api/v1/conversations"
      )
        return Response.json([]);
      return Response.json({});
    }),
  );
  render(<App />);
  expect(await screen.findByRole("link", { name: "问答" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "个人设置" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "构建与审批" })).toBeNull();
  expect(screen.queryByRole("link", { name: "配置" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
  expect(localStorage.getItem("navigationExpanded")).toBe("true");
  expect(screen.getByRole("button", { name: "收起导航" })).toBeTruthy();
});
