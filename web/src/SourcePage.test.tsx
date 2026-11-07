// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  history.pushState(null, "", "/");
});

test("admin can upload and browse source history and compare versions", async () => {
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
  localStorage.setItem("token", "token");
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/me") return Response.json({ role: "admin" });
    if (url === "/healthz") return Response.json({});
    if (url === "/api/v1/source/files")
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
    if (url === "/api/v1/source/upload" && options?.method === "POST")
      return Response.json({ commit: "c".repeat(40) });
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "管理" }));
  fireEvent.click(await screen.findByRole("link", { name: "构建记录" }));
  fireEvent.click(await screen.findByRole("link", { name: "源文件" }));
  fireEvent.change(screen.getByLabelText("目标目录"), {
    target: { value: "knowledge/sales" },
  });
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["new"], "new.md", { type: "text/markdown" })] },
  });
  fireEvent.click(screen.getByRole("button", { name: "上传" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, options]) =>
          url === "/api/v1/source/upload" && options?.method === "POST",
      ),
    ).toBe(true),
  );
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
    url.startsWith("/api/v1/source/diff?"),
  );
  const query = new URLSearchParams(diffCall![0].split("?")[1]);
  expect(query.get("before")).toBe("a".repeat(40));
  expect(query.get("after")).toBe("b".repeat(40));
});

test("uploads an archive and a selected category folder", async () => {
  const { SourcePage } = await import("./SourcePage");
  const { getMessages } = await import("./i18n");
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/source/upload") {
        requests.push(
          JSON.parse(String(options?.body)) as Record<string, unknown>,
        );
        return Response.json({ commit: "a".repeat(40) });
      }
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  fireEvent.change(screen.getByLabelText("目标目录"), {
    target: { value: "knowledge/cat" },
  });
  fireEvent.click(screen.getByLabelText("替换整个知识类（删除未上传的文件）"));
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["zip bytes"], "cat.zip")] },
  });
  fireEvent.click(screen.getByRole("button", { name: "上传" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({
    target: "knowledge/cat",
    replace: true,
    archive_base64: btoa("zip bytes"),
  });
  const item = new File(["document"], "file.md");
  Object.defineProperty(item, "webkitRelativePath", {
    value: "cat/sub/file.md",
  });
  fireEvent.change(screen.getByLabelText("选择文件夹"), {
    target: { files: [item] },
  });
  fireEvent.click(screen.getByRole("button", { name: "上传" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toMatchObject({
    files: [{ path: "sub/file.md", content_base64: btoa("document") }],
  });
});

test("allows downloading binary source files", async () => {
  const { SourcePage } = await import("./SourcePage");
  const { getMessages } = await import("./i18n");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/source/files")
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
