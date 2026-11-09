// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SearchPage } from "./SearchPage";
import { getMessages } from "./i18n";
import { PROJECT_STORAGE_KEY } from "./ProjectSelector";

type ProjectRow = {
  id: string;
  name: string;
  type: "warehouse" | "document";
  status: "active" | "archived" | "deleting";
  created_at: string;
  my_role: "admin" | "member";
};

function project(
  id: string,
  name: string,
  type: ProjectRow["type"] = "warehouse",
  status: ProjectRow["status"] = "active",
): ProjectRow {
  return {
    id,
    name,
    type,
    status,
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  };
}

const projects = [
  project("default", "业务数据"),
  project("docs", "Platform Docs", "document"),
];

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  localStorage.clear();
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
  vi.unstubAllGlobals();
});

test("searches selected scope and category and displays fusion evidence", async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    return url.startsWith("/api/v1/categories")
      ? Response.json([
          {
            name: "metrics",
            ref: "category:metrics",
            description: "",
            documents: [],
          },
        ])
      : Response.json([
          {
            ref: "document:metrics/a#chunk-1",
            type: "document",
            content: "销售额",
            score: 0.0321,
            sources: ["fulltext", "vector"],
            confidence: "high",
            ambiguous: true,
            ambiguity_refs: [
              "document:metrics/a#chunk-1",
              "document:metrics/b#chunk-1",
            ],
          },
        ]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);
  await screen.findByRole("option", { name: "metrics" });
  fireEvent.change(screen.getByLabelText("查询内容"), {
    target: { value: "销售额" },
  });
  fireEvent.change(screen.getByLabelText("检索范围"), {
    target: { value: "docs" },
  });
  fireEvent.change(screen.getByLabelText("知识类"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  expect(await screen.findByText(/0.0321/)).toBeTruthy();
  expect(screen.getByText(/全文.*向量/)).toBeTruthy();
  expect(screen.getByText("高")).toBeTruthy();
  expect(screen.getByText("可能存在歧义")).toBeTruthy();
  expect(screen.getByText("metrics · b")).toBeTruthy();
  expect(screen.queryByText(/document:metrics/)).toBeNull();
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining("scope=docs&category=metrics"),
      expect.objectContaining({ headers: { Authorization: "Bearer token" } }),
    ),
  );
});

test("clears category when changing scope and reports search failure", async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    return url.startsWith("/api/v1/categories")
      ? Response.json([
          {
            name: "metrics",
            ref: "category:metrics",
            description: "",
            documents: [],
          },
        ])
      : new Response("failure", { status: 503 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("en")} />);
  await screen.findByRole("option", { name: "metrics" });
  fireEvent.change(screen.getByLabelText("Categories"), {
    target: { value: "metrics" },
  });
  fireEvent.change(screen.getByLabelText("Scope"), {
    target: { value: "tables" },
  });
  expect(
    (screen.getByLabelText("Categories") as HTMLSelectElement).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("Query"), {
    target: { value: "orders" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(
    await screen.findByText("Search failed. Please try again."),
  ).toBeTruthy();
  expect(fetcher).toHaveBeenCalledWith(
    expect.stringContaining("scope=tables"),
    expect.anything(),
  );
  const searchUrl = fetcher.mock.calls.find(([url]) =>
    url.startsWith("/api/v1/search"),
  )?.[0];
  expect(searchUrl).not.toContain("category=");
});

test("切换项目清空检索结果、类别与范围选项，并给检索请求带项目", async () => {
  const projectsWithArchive = [
    ...projects,
    project("old", "Old Docs", "document", "archived"),
  ];
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projectsWithArchive);
    if (url.startsWith("/api/v1/categories")) {
      const projectId =
        new URL(url, "http://localhost").searchParams.get("project") ?? "";
      return Response.json([
        projectId === "docs"
          ? {
              name: "平台",
              ref: "category:platform",
              description: "",
              documents: [],
            }
          : {
              name: "metrics",
              ref: "category:metrics",
              description: "",
              documents: [],
            },
      ]);
    }
    if (url.startsWith("/api/v1/search")) {
      const projectId =
        new URL(url, "http://localhost").searchParams.get("project") ?? "";
      return Response.json([
        {
          ref:
            projectId === "docs"
              ? "document:platform/a#chunk-1"
              : "document:metrics/a#chunk-1",
          type: "document",
          name: projectId === "docs" ? "平台文档" : "销售额",
          content: "内容",
          score: 0.0321,
          sources: ["fulltext"],
          confidence: "high",
          ambiguous: false,
          ambiguity_refs: [],
        },
      ]);
    }
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);

  await screen.findByRole("option", { name: "metrics" });
  fireEvent.change(screen.getByLabelText("查询内容"), {
    target: { value: "销售额" },
  });
  fireEvent.change(screen.getByLabelText("检索范围"), {
    target: { value: "tables" },
  });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  expect(await screen.findByText("销售额")).toBeTruthy();
  expect(screen.getByRole("option", { name: "表" })).toBeTruthy();

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  expect(screen.queryByTitle("Old Docs")).toBeNull();
  fireEvent.click(await screen.findByTitle("Platform Docs"));

  await waitFor(() => expect(screen.queryByText("销售额")).toBeNull());
  expect(screen.queryByRole("option", { name: "表" })).toBeNull();
  expect(screen.queryByRole("option", { name: "字段" })).toBeNull();
  expect(screen.queryByRole("option", { name: "任务" })).toBeNull();
  expect(screen.getByRole("option", { name: "文档" })).toBeTruthy();
  expect((screen.getByLabelText("检索范围") as HTMLSelectElement).value).toBe(
    "all",
  );
  expect(await screen.findByRole("option", { name: "平台" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  expect(await screen.findByText("平台文档")).toBeTruthy();
  await waitFor(() => {
    const urls = fetcher.mock.calls.map(([url]) => String(url));
    expect(
      urls.some(
        (url) =>
          url.startsWith("/api/v1/search?") &&
          new URL(url, "http://localhost").searchParams.get("project") ===
            "docs",
      ),
    ).toBe(true);
    expect(
      urls.some(
        (url) =>
          url.startsWith("/api/v1/categories?") &&
          new URL(url, "http://localhost").searchParams.get("project") ===
            "docs",
      ),
    ).toBe(true);
  });
});

test("只有一个可访问项目时仍显示项目选择器，请求带该项目", async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects")
      return Response.json([project("default", "唯一项目")]);
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByRole("combobox", { name: "项目" })).toBeTruthy();
  expect(await screen.findByText("唯一项目")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("查询内容"), {
    target: { value: "订单" },
  });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/search?") &&
          String(url).includes("project=default"),
      ),
    ).toBe(true),
  );
});

test("切换项目清空旧类别并丢弃迟到的类别响应", async () => {
  const pendingCategories = deferredResponse();
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects")
      return Response.json([
        project("default", "业务数据"),
        project("finance", "财务数据"),
      ]);
    if (url.startsWith("/api/v1/categories")) {
      const projectId =
        new URL(url, "http://localhost").searchParams.get("project") ?? "";
      return projectId === "default"
        ? pendingCategories.promise
        : Response.json([
            {
              name: "财务",
              ref: "category:finance",
              description: "",
              documents: [],
            },
          ]);
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);

  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/categories?") &&
          String(url).includes("project=default"),
      ),
    ).toBe(true),
  );
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("财务数据"));
  expect(await screen.findByRole("option", { name: "财务" })).toBeTruthy();

  await act(async () => {
    pendingCategories.resolve(
      Response.json([
        {
          name: "旧类别",
          ref: "category:old",
          description: "",
          documents: [],
        },
      ]),
    );
    await pendingCategories.promise;
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  expect(screen.queryByRole("option", { name: "旧类别" })).toBeNull();
  expect(screen.getByRole("option", { name: "财务" })).toBeTruthy();
});

test("切换项目后不用旧项目的检索词向新项目自动发起检索", async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url.startsWith("/api/v1/categories")) {
      const projectId =
        new URL(url, "http://localhost").searchParams.get("project") ?? "";
      return Response.json([
        {
          name: projectId === "docs" ? "平台" : "指标",
          ref: projectId === "docs" ? "category:platform" : "category:metrics",
          description: "",
          documents: [],
        },
      ]);
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);

  await screen.findByRole("option", { name: "指标" });
  fireEvent.change(screen.getByLabelText("查询内容"), {
    target: { value: "app_air_abtest_markup_di" },
  });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/search?") &&
          new URL(String(url), "http://localhost").searchParams.get(
            "project",
          ) === "default",
      ),
    ).toBe(true),
  );

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("Platform Docs"));
  expect(await screen.findByRole("option", { name: "平台" })).toBeTruthy();

  await waitFor(() => {
    const stale = fetcher.mock.calls
      .map(([url]) => String(url))
      .filter(
        (url) =>
          url.startsWith("/api/v1/search?") &&
          new URL(url, "http://localhost").searchParams.get("project") ===
            "docs",
      );
    expect(stale).toEqual([]);
  });
});
