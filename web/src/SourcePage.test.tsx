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
  history.replaceState(null, "", "/");
});

test("gap upload refuses inaccessible project without falling back", async () => {
  history.replaceState(null, "", "/source?project=missing&gap=gap1");
  const fetcher = vi.fn(async (url: string) =>
    url === "/api/v1/projects" ? Response.json(projects) : Response.json([]),
  );
  vi.stubGlobal("fetch", fetcher);
  render(<SourcePage token="t" messages={getMessages("en")} />);
  expect(await screen.findByText(/project is unavailable/i)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
  expect(
    fetcher.mock.calls.some(([url]) => url.includes("project=default")),
  ).toBe(false);
});

test("gap upload refuses a gap belonging to another project", async () => {
  history.replaceState(null, "", "/source?project=docs&gap=gap1");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/projects" ? Response.json(projects) : Response.json([]),
    ),
  );
  render(<SourcePage token="t" messages={getMessages("en")} />);
  expect(await screen.findByText(/project is unavailable/i)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /preview/i })).toBeNull();
});

test("gap upload uses append-only mode on preview and submit", async () => {
  history.replaceState(null, "", "/source?project=docs&gap=gap1");
  const requests: { url: string; body: any }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url === "/api/v1/knowledge-gaps?project=docs")
        return Response.json([{ id: "gap1", status: "confirmed" }]);
      if (options?.method === "POST") {
        requests.push({ url, body: JSON.parse(String(options.body)) });
        if (url.includes("preview"))
          return Response.json({
            category: "metrics",
            target: "knowledge/metrics",
            added: ["knowledge/metrics/a.md"],
            overwritten: [],
            unchanged: [],
            deleted: [],
            counts: { added: 1, overwritten: 0, unchanged: 0, deleted: 0 },
            truncated: false,
          });
        return Response.json({ commit: "abc" });
      }
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  fireEvent.change(await screen.findByLabelText("选择文件"), {
    target: { files: [new File(["content"], "a.md")] },
  });
  fireEvent.change(screen.getByLabelText("新建知识类"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览" }));
  await screen.findByText("新增 1 个");
  fireEvent.click(screen.getByRole("button", { name: "确认上传" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(
    requests.every(
      (r) =>
        r.url.endsWith("project=docs") &&
        r.body.append_only === true &&
        !r.body.replace,
    ),
  ).toBe(true);
});

test("gap upload disables submit when project access is revoked after preview", async () => {
  history.replaceState(null, "", "/source?project=docs&gap=gap1");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url === "/api/v1/knowledge-gaps?project=docs")
        return Response.json([{ id: "gap1", status: "confirmed" }]);
      if (url.includes("upload/preview"))
        return Response.json({
          category: "metrics",
          target: "knowledge/metrics",
          added: ["a"],
          overwritten: [],
          unchanged: [],
          deleted: [],
          counts: { added: 1, overwritten: 0, unchanged: 0, deleted: 0 },
          truncated: false,
        });
      if (options?.method === "POST") return Response.json({}, { status: 403 });
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("en")} />);
  fireEvent.change(await screen.findByLabelText("Select files"), {
    target: { files: [new File(["x"], "a.md")] },
  });
  fireEvent.change(screen.getByLabelText("New knowledge category"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "Confirm upload",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "Confirm upload" }));
  expect(await screen.findByText(/project is unavailable/i)).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: "Confirm upload",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
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
      uploads.push(
        JSON.parse(String(options?.body)) as Record<string, unknown>,
      );
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
  expect(
    fetcher.mock.calls.filter(([url]) =>
      String(url).includes("/api/v1/source/files?project=default"),
    ).length,
  ).toBeGreaterThanOrEqual(2);
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
    (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["x"], "a.md")] },
  });
  expect(
    (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("新建知识类"), {
    target: { value: "metrics" },
  });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "预览" }) as HTMLButtonElement)
        .disabled,
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
        uploads.push(
          JSON.parse(String(options?.body)) as Record<string, unknown>,
        );
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
  fireEvent.click(screen.getByLabelText("替换整个知识类（删除未上传的文件）"));
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
  fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
  fireEvent.click(await screen.findByTitle("Platform Docs"));
  await waitFor(() =>
    expect(calls).toContain("/api/v1/source/files?project=docs"),
  );
  expect(calls).toContain("/api/v1/categories?project=docs");
});

test("a late preview from the previous project cannot be uploaded in the new project", async () => {
  let resolvePreview!: (value: Response) => void;
  const delayed = new Promise<Response>((resolve) => {
    resolvePreview = resolve;
  });
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url === "/api/v1/source/upload/preview?project=default") return delayed;
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["x"], "x.md")] },
  });
  fireEvent.change(screen.getByLabelText("新建知识类"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) => url === "/api/v1/source/upload/preview?project=default",
      ),
    ).toBe(true),
  );
  fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
  fireEvent.click(await screen.findByTitle("Platform Docs"));
  await act(async () =>
    resolvePreview(
      Response.json({
        category: "metrics",
        target: "knowledge/metrics",
        added: [],
        overwritten: [],
        unchanged: [],
        deleted: [],
        counts: { added: 0, overwritten: 0, unchanged: 0, deleted: 0 },
        truncated: false,
      }),
    ),
  );
  expect(
    (screen.getByRole("button", { name: "确认上传" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    fetcher.mock.calls.some(
      ([url]) => url === "/api/v1/source/upload?project=docs",
    ),
  ).toBe(false);
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

const pendingStatus = {
  state: "pending",
  due_in_seconds: 12,
  last_build_id: null,
  source_commit: null,
  auto_build: true,
  error: null,
};

test("shows pending source status and the auto build switch", async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    if (url === "/api/v1/source/files?project=default")
      return Response.json([]);
    if (url.startsWith("/api/v1/projects/default/source-status"))
      return Response.json(pendingStatus);
    if (url.startsWith("/api/v1/projects/default/auto-build"))
      return Response.json({ enabled: true });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SourcePage token="t" messages={getMessages("zh")} />);

  expect(await screen.findByText("待构建（约 12 秒后开始）")).toBeTruthy();
  const autoBuild = await screen.findByRole("switch", { name: "自动构建" });
  expect((autoBuild as HTMLButtonElement).getAttribute("aria-checked")).toBe(
    "true",
  );
});

test("turning auto build off sends the new setting", async () => {
  const updates: { method?: string; body?: string }[] = [];
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/projects") return Response.json(projects);
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    if (url === "/api/v1/source/files?project=default")
      return Response.json([]);
    if (url.startsWith("/api/v1/projects/default/source-status"))
      return Response.json({ ...pendingStatus, state: "effective" });
    if (url.startsWith("/api/v1/projects/default/auto-build")) {
      if (options?.method === "PUT") {
        updates.push({ method: options.method, body: String(options.body) });
        return Response.json({ enabled: false });
      }
      return Response.json({ enabled: true });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SourcePage token="t" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("switch", { name: "自动构建" }));
  await waitFor(() => expect(updates).toHaveLength(1));
  expect(updates[0]).toEqual({
    method: "PUT",
    body: JSON.stringify({ enabled: false }),
  });
});

test("shows the failure reason for a failed build", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url.startsWith("/api/v1/categories")) return Response.json([]);
      if (url === "/api/v1/source/files?project=default")
        return Response.json([]);
      if (url.startsWith("/api/v1/projects/default/source-status"))
        return Response.json({
          ...pendingStatus,
          state: "failed",
          due_in_seconds: null,
          error: "维度值读取失败",
        });
      if (url.startsWith("/api/v1/projects/default/auto-build"))
        return Response.json({ enabled: true });
      return Response.json([]);
    }),
  );
  render(<SourcePage token="t" messages={getMessages("zh")} />);
  expect(await screen.findByText("构建失败：维度值读取失败")).toBeTruthy();
});

test("polls pending status every three seconds", async () => {
  vi.useFakeTimers();
  try {
    let statusCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/v1/projects") return Response.json(projects);
        if (url.startsWith("/api/v1/categories")) return Response.json([]);
        if (url === "/api/v1/source/files?project=default")
          return Response.json([]);
        if (url.startsWith("/api/v1/projects/default/source-status")) {
          statusCalls += 1;
          return Response.json(pendingStatus);
        }
        if (url.startsWith("/api/v1/projects/default/auto-build"))
          return Response.json({ enabled: true });
        return Response.json([]);
      }),
    );
    render(<SourcePage token="t" messages={getMessages("zh")} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(statusCalls).toBe(1);
    await act(async () => {
      vi.advanceTimersByTime(3000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(statusCalls).toBe(2);
  } finally {
    vi.useRealTimers();
  }
});

test("supports semantic model target upload, path filtering, and quick incremental build", async () => {
  const previews: Record<string, unknown>[] = [];
  const uploads: Record<string, unknown>[] = [];
  const builds: Record<string, unknown>[] = [];
  const onNavigateBuilds = vi.fn();

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json(projects);
      if (url.startsWith("/api/v1/categories")) return Response.json([]);
      if (url === "/api/v1/source/files?project=default")
        return Response.json([
          "knowledge/metrics/sales.md",
          "ossie/city_domain.yaml",
          "dimension_values.json",
        ]);
      if (url === "/api/v1/source/upload/preview?project=default") {
        const body = JSON.parse(String(options?.body)) as Record<
          string,
          unknown
        >;
        previews.push(body);
        return Response.json({
          category: "ossie",
          target: "ossie",
          added: ["ossie/topic_city.yaml"],
          overwritten: [],
          unchanged: [],
          deleted: [],
          counts: { added: 1, overwritten: 0, unchanged: 0, deleted: 0 },
          truncated: false,
        });
      }
      if (url === "/api/v1/source/upload?project=default") {
        uploads.push(
          JSON.parse(String(options?.body)) as Record<string, unknown>,
        );
        return Response.json({ commit: "e".repeat(40) });
      }
      if (
        url === "/api/v1/builds?project=default" &&
        options?.method === "POST"
      ) {
        builds.push(
          JSON.parse(String(options?.body)) as Record<string, unknown>,
        );
        return Response.json({ id: 99 }, { status: 202 });
      }
      return Response.json([]);
    }),
  );

  render(
    <SourcePage
      token="t"
      messages={getMessages("zh")}
      onNavigateBuilds={onNavigateBuilds}
    />,
  );

  expect(await screen.findByText("city_domain.yaml")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("筛选源文件路径"), {
    target: { value: "city_domain" },
  });
  expect(screen.getByText("city_domain.yaml")).toBeTruthy();
  expect(screen.queryByText("sales.md")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "语义模型 / 指定目录" }));
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["name: city\n"], "topic_city.yaml")] },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览" }));
  await waitFor(() => expect(previews).toHaveLength(1));
  expect(previews[0]).toMatchObject({
    target: "ossie",
    replace: false,
    files: [{ path: "topic_city.yaml" }],
  });
  expect(previews[0]).not.toHaveProperty("category");

  fireEvent.click(await screen.findByRole("button", { name: "确认上传" }));
  await waitFor(() => expect(uploads).toHaveLength(1));
  expect(uploads[0]).toMatchObject({ target: "ossie" });

  fireEvent.click(screen.getByRole("button", { name: "立即增量构建" }));
  await waitFor(() => expect(builds).toEqual([{ build_type: "incremental" }]));

  fireEvent.click(screen.getByRole("button", { name: "查看构建记录" }));
  expect(onNavigateBuilds).toHaveBeenCalledTimes(1);
});
