// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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

function makeInstance(overrides: Record<string, unknown> = {}) {
  return {
    id: "dtb-main",
    project_id: "default",
    plugin_id: "dtb",
    display_name: "主数仓 DTB",
    enabled: true,
    parameters: {},
    configured_secrets: ["token"],
    secret_configured: true,
    schedule_cron: "30 2 * * *",
    timezone: "Asia/Shanghai",
    catalog_prefix: null,
    task_namespace: null,
    drop_threshold: 0.5,
    last_table_count: 10,
    last_triggered_at: "2026-11-18T00:00:00Z",
    created_at: "2026-11-18T00:00:00Z",
    updated_at: "2026-11-18T00:00:00Z",
    ...overrides,
  };
}

test("默认项目渲染多实例拉取器并按实例手动触发 { force: false }", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/projects/default/pull-instances") {
      return Response.json([
        makeInstance(),
        makeInstance({
          id: "dtb-dim",
          display_name: "维度库",
          schedule_cron: null,
        }),
      ]);
    }
    if (url === "/api/v1/pulls?project=default") {
      return Response.json([
        {
          id: 1,
          instance_id: "dtb-main",
          plugin_id: "dtb",
          trigger_type: "manual",
          status: "failed",
          started_at: "2026-11-06T00:00:00Z",
          finished_at: "2026-11-06T00:01:00Z",
          tables: 5,
          tasks: 2,
          error: "timeout",
          skipped_reason: null,
          warnings: [],
          commit: null,
          build_id: null,
        },
      ]);
    }
    if (
      url === "/api/v1/projects/default/pull-instances/dtb-dim/pull" &&
      init?.method === "POST"
    ) {
      return Response.json({ id: 2 });
    }
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PullsPanel token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("timeout")).toBeTruthy();
  expect(screen.getByText("主数仓 DTB")).toBeTruthy();
  const dimCard = screen.getByTestId("pull-instance-dtb-dim");
  fireEvent.click(within(dimCard).getByRole("button", { name: "立即拉取" }));

  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          url === "/api/v1/projects/default/pull-instances/dtb-dim/pull" &&
          init?.method === "POST" &&
          init?.body === JSON.stringify({ force: false }),
      ),
    ).toBe(true),
  );
});

test("文档型项目或无实例无历史时安静隐藏且无报错", async () => {
  stubDomEnvironment();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects/docs/pull-instances")
        return Response.json([]);
      if (url === "/api/v1/pulls?project=docs") return Response.json([]);
      return new Response("", { status: 404 });
    }),
  );
  const { container } = render(
    <PullsPanel token="token" project="docs" messages={getMessages("zh")} />,
  );
  await waitFor(() => expect(container.textContent).toBe(""));
  expect(screen.queryByRole("alert")).toBeNull();
});

test("展示 skipped_reason=no_changes、warnings 与 blocked 熔断强制拉取确认", async () => {
  stubDomEnvironment();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/projects/topic/pull-instances") {
      return Response.json([makeInstance({ project_id: "topic" })]);
    }
    if (url === "/api/v1/pulls?project=topic") {
      return Response.json([
        {
          id: 12,
          instance_id: "dtb-main",
          plugin_id: "dtb",
          trigger_type: "scheduled",
          status: "blocked",
          started_at: "2026-11-18T02:00:00Z",
          finished_at: "2026-11-18T02:00:10Z",
          tables: 2,
          tasks: 1,
          error: "表数量从 100 骤降至 2，触发熔断保护",
          skipped_reason: null,
          warnings: ["维表 dim_city 拉取失败，已沿用上次快照"],
          commit: null,
          build_id: null,
        },
        {
          id: 11,
          instance_id: "dtb-main",
          plugin_id: "dtb",
          trigger_type: "manual",
          status: "succeeded",
          started_at: "2026-11-18T01:00:00Z",
          finished_at: "2026-11-18T01:00:05Z",
          tables: 100,
          tasks: 20,
          error: null,
          skipped_reason: "no_changes",
          warnings: [],
          commit: null,
          build_id: null,
        },
      ]);
    }
    if (
      url === "/api/v1/projects/topic/pull-instances/dtb-main/pull" &&
      init?.method === "POST"
    ) {
      return Response.json({ id: 13 });
    }
    return new Response("", { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <PullsPanel token="token" project="topic" messages={getMessages("zh")} />,
  );

  expect(await screen.findByText("无变更跳过")).toBeTruthy();
  expect(
    screen.getByText("维表 dim_city 拉取失败，已沿用上次快照"),
  ).toBeTruthy();
  expect(screen.getByText("拉取触发熔断保护 (dtb-main)")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "强制拉取" }));
  const dialog = await screen.findByRole("dialog");
  expect(
    within(dialog).getByText(/确认继续对实例 dtb-main 执行强制拉取/),
  ).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "强制拉取" }));

  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          url === "/api/v1/projects/topic/pull-instances/dtb-main/pull" &&
          init?.method === "POST" &&
          init?.body === JSON.stringify({ force: true }),
      ),
    ).toBe(true),
  );
});

test("快速切换项目时丢弃旧项目迟到的 pulls 响应", async () => {
  stubDomEnvironment();
  let resolveSlowPulls: ((response: Response) => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects/alpha/pull-instances")
        return Response.json([makeInstance({ project_id: "alpha" })]);
      if (url === "/api/v1/projects/beta/pull-instances")
        return Response.json([makeInstance({ project_id: "beta" })]);
      if (url === "/api/v1/pulls?project=alpha") {
        return new Promise<Response>((resolve) => {
          resolveSlowPulls = resolve;
        });
      }
      if (url === "/api/v1/pulls?project=beta") {
        return Response.json([
          {
            id: 22,
            instance_id: "dtb-main",
            plugin_id: "dtb",
            trigger_type: "manual",
            status: "succeeded",
            started_at: "2026-11-18T02:00:00Z",
            finished_at: "2026-11-18T02:01:00Z",
            tables: 2,
            tasks: 1,
            error: null,
            skipped_reason: null,
            warnings: [],
            commit: null,
            build_id: 22,
          },
        ]);
      }
      return new Response("", { status: 404 });
    }),
  );

  const { rerender } = render(
    <PullsPanel token="token" project="alpha" messages={getMessages("zh")} />,
  );
  await waitFor(() => expect(resolveSlowPulls).toBeTruthy());

  rerender(
    <PullsPanel token="token" project="beta" messages={getMessages("zh")} />,
  );
  expect(await screen.findByText("成功")).toBeTruthy();

  resolveSlowPulls?.(
    Response.json([
      {
        id: 11,
        instance_id: "dtb-main",
        plugin_id: "dtb",
        trigger_type: "manual",
        status: "failed",
        started_at: "2026-11-18T01:00:00Z",
        tables: 1,
        tasks: 0,
        error: "stale-alpha-error",
        skipped_reason: null,
        warnings: [],
        commit: null,
        build_id: null,
      },
    ]),
  );
  await waitFor(() => {
    expect(screen.queryByText("stale-alpha-error")).toBeNull();
    expect(screen.getByText("成功")).toBeTruthy();
  });
});
