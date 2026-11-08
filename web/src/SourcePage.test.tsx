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
import { SourcePage } from "./SourcePage";
import { getMessages } from "./i18n";

const projects = [
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
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
];

const longName = `${"这是一个非常长的文件名".repeat(8)}.md`;
const manyFiles = Array.from(
  { length: 300 },
  (_, index) => `knowledge/架构文档/目录-${index}/document-${index}.md`,
);
manyFiles[299] = `knowledge/架构文档/${longName}`;

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
  vi.restoreAllMocks();
});

test("folder upload fills the category, previews, then confirms", async () => {
  const uploads: Record<string, unknown>[] = [];
  const previews: Record<string, unknown>[] = [];
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url.startsWith("/api/v1/categories"))
      return Response.json([
        { ref: "cat:metrics", name: "metrics", description: "", documents: [] },
      ]);
    if (url === "/api/v1/source/files?project=default")
      return Response.json(manyFiles);
    if (url === "/api/v1/source/upload/preview?project=default") {
      const body = JSON.parse(String(options?.body)) as Record<string, unknown>;
      previews.push(body);
      return Response.json({
        category: "架构文档",
        target: "knowledge/架构文档",
        added: manyFiles.slice(0, 200),
        overwritten: ["knowledge/架构文档/旧文件.md"],
        unchanged: [],
        deleted: [],
        counts: { added: 298, overwritten: 2, unchanged: 0, deleted: 0 },
        truncated: true,
      });
    }
    if (url === "/api/v1/source/upload?project=default") {
      uploads.push(JSON.parse(String(options?.body)) as Record<string, unknown>);
      return Response.json({ commit: "c".repeat(40) });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SourcePage token="t" messages={getMessages("zh")} />);

  const item = new File(["document"], "a.md");
  Object.defineProperty(item, "webkitRelativePath", {
    value: "架构文档/子目录/a.md",
  });
  fireEvent.change(screen.getByLabelText("选择文件夹"), {
    target: { files: [item] },
  });
  expect((screen.getByLabelText("新建知识类") as HTMLInputElement).value).toBe(
    "架构文档",
  );

  fireEvent.click(screen.getByRole("button", { name: "预览" }));
  await waitFor(() => expect(previews).toHaveLength(1));
  expect(previews[0]).toMatchObject({
    category: "架构文档",
    replace: false,
    files: [{ path: "子目录/a.md", content_base64: btoa("document") }],
  });
  expect(await screen.findByText("新增 298 个")).toBeTruthy();
  expect(screen.getByText("覆盖 2 个")).toBeTruthy();
  expect(screen.getByText("（未变化 0 个）")).toBeTruthy();
  expect(screen.getByText(longName)).toBeTruthy();
  expect(uploads).toHaveLength(0);

  fireEvent.click(screen.getByRole("button", { name: "确认上传" }));
  await waitFor(() => expect(uploads).toHaveLength(1));
  expect(uploads[0]).toMatchObject({
    category: "架构文档",
    replace: false,
    files: [{ path: "子目录/a.md", content_base64: btoa("document") }],
  });
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/api/v1/source/files?project=default")).length).toBeGreaterThanOrEqual(2);
});

test("requires a category and files before previewing", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  expect(
    (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["x"], "a.md")] },
  });
  expect(
    (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("新建知识类"), {
    target: { value: "metrics" },
  });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement).disabled,
    ).toBe(false),
  );
});

test("advanced replacement shows the deletion warning and confirm label", async () => {
  const uploads: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url === "/api/v1/source/upload/preview?project=default")
        return Response.json({
          category: "metrics",
          target: "knowledge/metrics",
          added: [],
          overwritten: [],
          unchanged: [],
          deleted: ["knowledge/metrics/gone.md", "knowledge/metrics/old.md"],
          counts: { added: 0, overwritten: 0, unchanged: 0, deleted: 2 },
          truncated: false,
        });
      if (url === "/api/v1/source/upload?project=default") {
        uploads.push(JSON.parse(String(options?.body)) as Record<string, unknown>);
        return Response.json({ commit: "d".repeat(40) });
      }
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["new"], "new.md")] },
  });
  fireEvent.change(screen.getByLabelText("新建知识类"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByText("高级选项"));
  fireEvent.click(
    screen.getByLabelText("替换整个知识类（删除未上传的文件）"),
  );
  fireEvent.click(screen.getByRole("button", { name: "预览" }));
  expect(await screen.findByText(/将删除 2 个文件/)).toBeTruthy();
  const confirm = await screen.findByRole("button", {
    name: "确认并删除 2 个文件",
  });
  expect(confirm.className).toContain("danger");
  fireEvent.click(confirm);
  await waitFor(() => expect(uploads).toHaveLength(1));
  expect(uploads[0]).toMatchObject({ category: "metrics", replace: true });
});

test("changing the project switches every source request", async () => {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      if (url === "/api/v1/projects") return Response.json(projects);
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  expect(await screen.findByText("业务数据")).toBeTruthy();
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByTitle("Platform Docs"));
  await waitFor(() =>
    expect(calls).toContain("/api/v1/source/files?project=docs"),
  );
  expect(calls).toContain("/api/v1/categories?project=docs");
});

test("browses source history and compares versions", async () => {
  localStorage.setItem("token", "token");
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/me") return Response.json({ role: "admin" });
    if (url === "/healthz") return Response.json({});
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    if (url === "/api/v1/source/files?project=default")
      return Response.json(["knowledge/sales/a.md"]);
    if (url.startsWith("/api/v1/source/file?"))
      return Response.json({
        path: "knowledge/sales/a.md",
        content: "current content",
      });
    if (url.startsWith("/api/v1/source/history?"))
      return Response.json([
        {
          commit: "a".repeat(40),
          author: "Alice",
          date: "2026-01-01T00:00:00Z",
          message: "first",
        },
        {
          commit: "b".repeat(40),
          author: "Bob",
          date: "2026-01-02T00:00:00Z",
          message: "second",
        },
      ]);
    if (url.startsWith("/api/v1/source/diff?"))
      return Response.json({ diff: "-old\n+new" });
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "用户菜单" }));
  fireEvent.click(await screen.findByRole("button", { name: "管理" }));
  fireEvent.click(await screen.findByRole("link", { name: "构建记录" }));
  fireEvent.click(await screen.findByRole("link", { name: "源文件" }));
  fireEvent.click(await screen.findByRole("button", { name: "a.md" }));
  expect(await screen.findByText("current content")).toBeTruthy();
  expect((await screen.findAllByText(/Alice/)).length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText("旧版本"), {
    target: { value: "a".repeat(40) },
  });
  fireEvent.change(screen.getByLabelText("新版本"), {
    target: { value: "b".repeat(40) },
  });
  fireEvent.click(screen.getByRole("button", { name: "比较版本" }));
  expect(await screen.findByText(/\+new/)).toBeTruthy();
  const diffCall = fetcher.mock.calls.find(([url]) =>
    String(url).startsWith("/api/v1/source/diff?"),
  );
  const query = new URLSearchParams(String(diffCall![0]).split("?")[1]);
  expect(query.get("project")).toBe("default");
  expect(query.get("before")).toBe("a".repeat(40));
  expect(query.get("after")).toBe("b".repeat(40));
});

test("allows downloading binary source files", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url === "/api/v1/source/files?project=default")
        return Response.json(["warehouse/tables.xlsx"]);
      if (url.startsWith("/api/v1/source/file?"))
        return Response.json({
          path: "warehouse/tables.xlsx",
          content: "AP8=",
          encoding: "base64",
        });
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "tables.xlsx" }));
  const link = await screen.findByRole("link", { name: "下载二进制文件" });
  expect(link.getAttribute("href")).toBe(
    "data:application/octet-stream;base64,AP8=",
  );
  expect(link.getAttribute("download")).toBe("tables.xlsx");
});
