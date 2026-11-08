// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ProjectsPage } from "./ProjectsPage";
import { getMessages } from "./i18n";

const created = "2026-11-05T19:27:00+08:00";

function projectRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "default",
    name: "业务数据",
    type: "warehouse",
    status: "active",
    created_at: created,
    my_role: "admin",
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

test("lists projects with type, status and creation time", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json([
        projectRow(),
        projectRow({
          id: "docs",
          name: "Platform Docs",
          type: "document",
        }),
      ]),
    ),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  expect(screen.getByText("数仓型")).toBeTruthy();
  expect(screen.getByText("文档型")).toBeTruthy();
  expect(screen.getAllByText("有效").length).toBe(2);
  expect(screen.getAllByText(new Date(created).toLocaleString()).length).toBe(
    2,
  );
});

test("creates a document project and refreshes the list", async () => {
  let rows = [projectRow()];
  const calls: Array<{ url: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({ url, body: options?.body as string | undefined });
      if (options?.method === "POST") {
        rows = [
          ...rows,
          projectRow({
            id: "platformdocs",
            name: "Platform Docs",
            type: "document",
          }),
        ];
        return Response.json(rows[rows.length - 1], { status: 201 });
      }
      return Response.json(rows);
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "创建项目" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Platform Docs" },
  });
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByTitle("文档型"));
  fireEvent.click(screen.getByRole("button", { name: /^创\s*建$/ }));

  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  const post = calls.find((call) => call.body);
  expect(JSON.parse(post?.body ?? "{}")).toEqual({
    name: "Platform Docs",
    type: "document",
  });
});

test("shows the duplicate-name message returned by the API", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, options?: RequestInit) =>
      options?.method === "POST"
        ? new Response(
            JSON.stringify({ detail: "Project name already exists" }),
            {
              status: 409,
            },
          )
        : Response.json([projectRow()]),
    ),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "创建项目" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "业务数据" },
  });
  fireEvent.click(screen.getByRole("button", { name: /^创\s*建$/ }));
  expect(await screen.findByText("已有同名项目")).toBeTruthy();
});
