// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ProjectSelector } from "./ProjectSelector";
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
  {
    id: "old",
    name: "Old Docs",
    type: "document",
    status: "archived",
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
];

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

test("lists manageable projects and reports the selected project", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json(projects)),
  );
  const changes: string[] = [];
  render(
    <ProjectSelector
      token="token"
      value="default"
      onChange={(value) => changes.push(value)}
      messages={getMessages("zh")}
    />,
  );
  expect(await screen.findByText("业务数据")).toBeTruthy();
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByTitle("Platform Docs"));
  await waitFor(() => expect(changes).toEqual(["docs"]));
  expect(screen.queryByTitle("Old Docs")).toBeNull();
});

test("当前值不在可用项目列表时自动切换到首个项目，且支持 includeMemberProjects", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json([
        {
          id: "alpha",
          name: "主题域 Alpha",
          type: "warehouse",
          status: "active",
          created_at: "2026-01-01T00:00:00Z",
          my_role: "admin",
        },
        {
          id: "beta",
          name: "成员项目 Beta",
          type: "warehouse",
          status: "active",
          created_at: "2026-01-01T00:00:00Z",
          my_role: "member",
        },
      ]),
    ),
  );
  const changes: string[] = [];
  const { rerender } = render(
    <ProjectSelector
      token="token"
      value="default"
      onChange={(value) => changes.push(value)}
      messages={getMessages("zh")}
    />,
  );
  await waitFor(() => expect(changes).toEqual(["alpha"]));

  rerender(
    <ProjectSelector
      token="token"
      value="alpha"
      includeMemberProjects
      onChange={(value) => changes.push(value)}
      messages={getMessages("zh")}
    />,
  );
  fireEvent.mouseDown(screen.getByRole("combobox"));
  expect(await screen.findByTitle("成员项目 Beta")).toBeTruthy();
});
