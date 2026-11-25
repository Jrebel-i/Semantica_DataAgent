// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
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

function stubViewport(initialMatches: boolean, initialMediumMatches = false) {
  const narrowListeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediumListeners = new Set<(event: MediaQueryListEvent) => void>();
  const narrowMedia = {
    matches: initialMatches,
    media: "(max-width: 768px)",
    addListener(listener: (event: MediaQueryListEvent) => void) {
      narrowListeners.add(listener);
    },
    removeListener(listener: (event: MediaQueryListEvent) => void) {
      narrowListeners.delete(listener);
    },
    addEventListener(
      _type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) {
      narrowListeners.add(listener);
    },
    removeEventListener(
      _type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) {
      narrowListeners.delete(listener);
    },
  };
  const mediumMedia = {
    matches: initialMediumMatches,
    media: "(min-width: 769px) and (max-width: 1024px)",
    addListener(listener: (event: MediaQueryListEvent) => void) {
      mediumListeners.add(listener);
    },
    removeListener(listener: (event: MediaQueryListEvent) => void) {
      mediumListeners.delete(listener);
    },
    addEventListener(
      _type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) {
      mediumListeners.add(listener);
    },
    removeEventListener(
      _type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) {
      mediumListeners.delete(listener);
    },
  };
  const darkMedia = {
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  };
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => {
      if (query.includes("1024px")) return mediumMedia;
      if (query.includes("prefers-color-scheme")) return darkMedia;
      return narrowMedia;
    },
  });
  return {
    setMatches(matches: boolean) {
      narrowMedia.matches = matches;
      const event = {
        matches,
        media: narrowMedia.media,
      } as MediaQueryListEvent;
      narrowListeners.forEach((listener) => listener(event));
    },
    setMediumMatches(matches: boolean) {
      mediumMedia.matches = matches;
      const event = {
        matches,
        media: mediumMedia.media,
      } as MediaQueryListEvent;
      mediumListeners.forEach((listener) => listener(event));
    },
  };
}

function mockNarrowFetch(conversations: object[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/conversations") return Response.json(conversations);
      if (url === "/api/v1/conversations?archived=true")
        return Response.json([]);
      if (url === "/api/v1/agents" || url === "/api/v1/providers")
        return Response.json([]);
      return Response.json({});
    }),
  );
}

test("工作台三栏壳层提供独立的问答、知识、构建与数据、项目和设置入口", async () => {
  localStorage.setItem("token", "admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "admin" });
      if (url === "/healthz") return Response.json({});
      if (url === "/api/v1/generations")
        return Response.json({ generations: [] });
      if (url === "/api/v1/settings") return Response.json({});
      return Response.json([]);
    }),
  );
  const { container } = render(<App />);
  expect(await screen.findByRole("button", { name: "新对话" })).toBeTruthy();
  expect(container.querySelector(".primary-rail")).not.toBeNull();
  expect(container.querySelector(".context-pane")).not.toBeNull();
  expect(container.querySelector(".workbench-topbar")).not.toBeNull();

  expect(screen.getByRole("link", { name: "对话" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "构建与数据" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "项目" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "设置" })).toBeTruthy();

  fireEvent.click(screen.getByRole("link", { name: "构建与数据" }));
  expect(await screen.findByRole("link", { name: "构建记录" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "源文件" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "纠错审批" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识缺口" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "自主演化" })).toBeTruthy();

  fireEvent.click(screen.getByRole("link", { name: "设置" }));
  expect(await screen.findByText("Agent 与模型")).toBeTruthy();
  expect(screen.getByText("系统与平台")).toBeTruthy();
  expect(screen.getByText("个人与接入")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Agent 配置" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "供应商与模型" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Agent 运行器" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "平台设置" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "插件配置" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Skill 管理" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "令牌管理" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "个人设置" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "MCP 接入" })).toBeTruthy();
});

test("普通用户只能看到问答、知识和个人设置，中栏折叠状态可恢复", async () => {
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (
        url === "/api/v1/agents" ||
        url === "/api/v1/providers" ||
        url === "/api/v1/conversations" ||
        url === "/api/v1/conversations?archived=true" ||
        url === "/api/v1/projects"
      )
        return Response.json([]);
      return Response.json({});
    }),
  );
  render(<App />);
  expect(await screen.findByRole("button", { name: "新对话" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "设置" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "构建与数据" })).toBeNull();
  expect(screen.queryByRole("link", { name: "项目" })).toBeNull();

  fireEvent.click(screen.getByRole("link", { name: "设置" }));
  expect(await screen.findByRole("link", { name: "个人设置" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "MCP 接入" })).toBeTruthy();
  expect(screen.queryByText("Agent 与模型")).toBeNull();
  expect(screen.queryByText("系统与平台")).toBeNull();
  expect(screen.queryByRole("link", { name: "构建记录" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Agent 配置" })).toBeNull();

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
  expect(await screen.findByRole("link", { name: "检索调试" })).toBeTruthy();
  fireEvent.click(screen.getByRole("link", { name: "对话" }));
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

test("窄屏下侧栏默认收起，顶部按钮打开覆盖式抽屉", async () => {
  localStorage.setItem("token", "user");
  localStorage.setItem("navigationExpanded", "true");
  stubViewport(true);
  mockNarrowFetch();
  const { container } = render(<App />);

  const open = await screen.findByRole("button", { name: "打开导航" });
  const sideNav = container.querySelector(".side-nav");
  expect(sideNav?.getAttribute("aria-hidden")).toBe("true");
  expect(sideNav?.className).not.toContain("expanded");
  expect(screen.queryByRole("link", { name: "知识库" })).toBeNull();

  fireEvent.click(open);

  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  expect(container.querySelector(".sidebar-scrim")).not.toBeNull();
  expect(sideNav?.getAttribute("aria-hidden")).toBeNull();
  expect(sideNav?.className).toContain("expanded");
});

test("窄屏抽屉可点遮罩关闭，也可点页面入口关闭", async () => {
  localStorage.setItem("token", "user");
  stubViewport(true);
  mockNarrowFetch();
  const { container } = render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "打开导航" }));
  fireEvent.click(screen.getByRole("button", { name: "关闭导航" }));
  expect(screen.queryByRole("link", { name: "知识库" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
  fireEvent.click(screen.getByRole("link", { name: "知识库" }));
  expect(screen.queryByRole("link", { name: "知识库" })).toBeNull();
  expect(
    container.querySelector(".side-nav")?.getAttribute("aria-hidden"),
  ).toBe("true");
});

test("窄屏选择会话后抽屉关闭", async () => {
  history.replaceState(null, "", "/chat");
  localStorage.setItem("token", "user");
  stubViewport(true);
  const conversations = [
    {
      id: "first",
      title: "窄屏会话",
      agent: "pi",
      provider_id: "p",
      model: "m",
      updated_at: "2026-11-05T00:00:00Z",
      turn_count: 0,
      resumable: true,
    },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/conversations") return Response.json(conversations);
      if (url === "/api/v1/conversations?archived=true")
        return Response.json([]);
      if (url === "/api/v1/conversations/first")
        return Response.json({ ...conversations[0], turns: [] });
      if (url === "/api/v1/agents" || url === "/api/v1/providers")
        return Response.json([]);
      return Response.json({});
    }),
  );
  const { container } = render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "打开导航" }));
  fireEvent.click(await screen.findByRole("button", { name: "窄屏会话" }));

  expect(screen.queryByRole("button", { name: "窄屏会话" })).toBeNull();
  expect(
    container.querySelector(".side-nav")?.getAttribute("aria-hidden"),
  ).toBe("true");
});

test("宽屏保持原有折叠状态与本地记忆", async () => {
  localStorage.setItem("token", "user");
  localStorage.setItem("navigationExpanded", "true");
  stubViewport(false);
  mockNarrowFetch();
  const { container } = render(<App />);

  expect(await screen.findByRole("link", { name: "知识库" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "打开导航" })).toBeNull();
  expect(container.querySelector(".side-nav")?.className).toContain("expanded");

  fireEvent.click(screen.getByRole("button", { name: "收起导航" }));
  expect(localStorage.getItem("navigationExpanded")).toBe("false");
  expect(container.querySelector(".side-nav")?.className).not.toContain(
    "expanded",
  );
});

test("跨过断点拉宽窗口时抽屉自动关闭，恢复窄屏仍默认收起", async () => {
  localStorage.setItem("token", "user");
  localStorage.setItem("navigationExpanded", "true");
  const viewport = stubViewport(true);
  mockNarrowFetch();
  const { container } = render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "打开导航" }));
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();

  act(() => viewport.setMatches(false));

  expect(screen.queryByRole("button", { name: "打开导航" })).toBeNull();
  expect(screen.getByRole("link", { name: "知识库" })).toBeTruthy();
  expect(container.querySelector(".side-nav")?.className).toContain("expanded");

  act(() => viewport.setMatches(true));

  expect(screen.getByRole("button", { name: "打开导航" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "知识库" })).toBeNull();
});

test("项目管理员可通过侧栏一键进入构建与数据及项目列表", async () => {
  localStorage.setItem("token", "pa-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "alpha",
            name: "Alpha 主题域",
            type: "warehouse",
            status: "active",
            created_at: "2026-01-01T00:00:00Z",
            my_role: "admin",
          },
        ]);
      if (url === "/api/v1/builds?project=alpha") return Response.json([]);
      if (url === "/api/v1/generations?project=alpha")
        return Response.json({
          generations: [],
          behind: false,
          source_head: null,
        });
      return Response.json([]);
    }),
  );
  render(<App />);
  const buildsShortcut = await screen.findByRole("link", {
    name: "构建与数据",
  });
  expect(screen.getByRole("link", { name: "项目" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Agent 配置" })).toBeNull();

  fireEvent.click(buildsShortcut);
  expect(
    await screen.findByRole("heading", { level: 1, name: "构建记录" }),
  ).toBeTruthy();
  expect(await screen.findByRole("link", { name: "源文件" })).toBeTruthy();
});

test("中屏(769-1024px)支持中栏收起与展开，顶部栏展示面包屑与全局项目选择", async () => {
  localStorage.setItem("token", "pa-token");
  history.replaceState(null, "", "/knowledge");
  stubViewport(false, true);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "alpha",
            name: "Alpha 主题域",
            type: "warehouse",
            status: "active",
            created_at: "2026-01-01T00:00:00Z",
            my_role: "admin",
          },
        ]);
      if (
        url.startsWith("/api/v1/categories") ||
        url.startsWith("/api/v1/tables")
      )
        return Response.json([]);
      return Response.json([]);
    }),
  );
  const { container } = render(<App />);

  expect(await screen.findByLabelText("当前项目")).toBeTruthy();
  expect(container.querySelector(".workbench-crumb")?.textContent).toContain(
    "知识库 / 知识浏览",
  );
  expect(container.querySelector(".context-pane")?.className).toContain(
    "context-pane--collapsed",
  );

  fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
  expect(container.querySelector(".context-pane")?.className).not.toContain(
    "context-pane--collapsed",
  );
  fireEvent.click(screen.getByRole("button", { name: "收起导航" }));
  expect(container.querySelector(".context-pane")?.className).toContain(
    "context-pane--collapsed",
  );
});

test("用户菜单支持浅色/深色主题切换并同步 data-theme", async () => {
  localStorage.setItem("token", "user");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/projects") return Response.json([]);
      return Response.json([]);
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "用户菜单" }));
  fireEvent.click(screen.getByRole("button", { name: "深色主题" }));
  expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  expect(localStorage.getItem("theme")).toBe("dark");

  fireEvent.click(screen.getByRole("button", { name: "浅色主题" }));
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem("theme")).toBe("light");
  document.documentElement.removeAttribute("data-theme");
});

test("顶部全局项目选择器可切换并同步当前页面项目", async () => {
  localStorage.setItem("token", "pa-token");
  history.replaceState(null, "", "/search");
  const searchedProjects: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "alpha",
            name: "Alpha 主题域",
            type: "warehouse",
            status: "active",
            created_at: "2026-01-01T00:00:00Z",
            my_role: "admin",
          },
          {
            id: "beta",
            name: "Beta 文档库",
            type: "document",
            status: "active",
            created_at: "2026-01-02T00:00:00Z",
            my_role: "admin",
          },
        ]);
      if (url.startsWith("/api/v1/categories")) {
        searchedProjects.push(url);
        return Response.json([]);
      }
      return Response.json([]);
    }),
  );
  render(<App />);
  const globalSelector = await screen.findByRole("combobox", {
    name: "当前项目",
  });
  await vi.waitFor(() =>
    expect(searchedProjects.some((url) => url.includes("project=alpha"))).toBe(
      true,
    ),
  );
  fireEvent.mouseDown(globalSelector);
  fireEvent.click(await screen.findByTitle("Beta 文档库"));
  await vi.waitFor(() =>
    expect(searchedProjects.some((url) => url.includes("project=beta"))).toBe(
      true,
    ),
  );
  expect(localStorage.getItem("dataagent.project")).toBe("beta");
});
