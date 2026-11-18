// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PullsPanel } from "./PullsPanel";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubDomEnvironment() {
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
}

test("拉取器可用时显示记录并能手动拉取", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/plugins")
      return Response.json([{ category: "puller", id: "dtb" }]);
    if (url === "/api/v1/pulls/dtb" && init?.method === "POST")
      return Response.json({ id: 2 });
    if (url === "/api/v1/pulls")
      return Response.json([
        {
          id: 1,
          status: "failed",
          started_at: "2026-11-06T00:00:00Z",
          tables: 5,
          tasks: 2,
          error: "timeout",
        },
      ]);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PullsPanel token="token" messages={getMessages("zh")} />);
  expect(await screen.findByText("timeout")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "立即拉取" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) => url === "/api/v1/pulls/dtb" && init?.method === "POST",
      ),
    ).toBe(true),
  );
});

test("puller/ossie 启用时立即拉取调用 /api/v1/pulls/ossie", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/plugins")
      return Response.json([
        { category: "puller", id: "dtb", enabled: false },
        { category: "puller", id: "ossie", enabled: true },
      ]);
    if (url === "/api/v1/pulls/ossie" && init?.method === "POST")
      return Response.json({ id: 3 });
    if (url === "/api/v1/pulls")
      return Response.json([
        {
          id: 1,
          status: "succeeded",
          started_at: "2026-11-18T00:00:00Z",
          tables: 3,
          tasks: 1,
          error: null,
        },
      ]);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PullsPanel token="token" messages={getMessages("zh")} />);
  const button = await screen.findByRole("button", { name: "立即拉取" });
  fireEvent.click(button);
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          url === "/api/v1/pulls/ossie" && init?.method === "POST",
      ),
    ).toBe(true),
  );
  expect(
    fetcher.mock.calls.some(
      ([url, init]) => url === "/api/v1/pulls/dtb" && init?.method === "POST",
    ),
  ).toBe(false);
});

test("仅有未启用的 puller/ossie 时显示面板且默认触发 /api/v1/pulls/dtb", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/plugins")
      return Response.json([
        { category: "puller", id: "ossie", enabled: false },
      ]);
    if (url === "/api/v1/pulls/dtb" && init?.method === "POST")
      return Response.json({ id: 4 });
    if (url === "/api/v1/pulls") return Response.json([]);
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PullsPanel token="token" messages={getMessages("zh")} />);
  const button = await screen.findByRole("button", { name: "立即拉取" });
  fireEvent.click(button);
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) => url === "/api/v1/pulls/dtb" && init?.method === "POST",
      ),
    ).toBe(true),
  );
});

test("指定 project 时从项目适配器读取模式并按项目拉取与列举记录", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/projects/topic/adapters")
      return Response.json([
        {
          id: "ossie",
          name: "Apache Ossie",
          project_types: ["warehouse"],
          enabled: true,
          parameters: {},
          parameters_schema: { type: "object", properties: {} },
          compatible: true,
          reason: null,
          pull: {
            enabled: true,
            parameters: { url: "https://dtb.example" },
            parameters_schema: { type: "object", properties: {} },
            token_configured: true,
          },
        },
      ]);
    if (url === "/api/v1/pulls?project=topic")
      return Response.json([
        {
          id: 9,
          status: "succeeded",
          started_at: "2026-11-18T01:00:00Z",
          finished_at: "2026-11-18T01:01:00Z",
          tables: 4,
          tasks: 2,
          error: null,
          commit: null,
          build_id: 12,
        },
      ]);
    if (url === "/api/v1/pulls/ossie?project=topic" && init?.method === "POST")
      return Response.json({ id: 10 });
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <PullsPanel token="token" project="topic" messages={getMessages("zh")} />,
  );
  expect(await screen.findByText("DTB 拉取为 Ossie")).toBeTruthy();
  const button = await screen.findByRole("button", { name: "立即拉取" });
  fireEvent.click(button);
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          url === "/api/v1/pulls/ossie?project=topic" &&
          init?.method === "POST",
      ),
    ).toBe(true),
  );
});
