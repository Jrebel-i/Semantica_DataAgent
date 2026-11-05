// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { BuildPage } from "./BuildPage";
import { getMessages } from "./i18n";

beforeEach(() => {
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

test("shows queue state, logs, stats and triggers both build types", async () => {
  const builds = [
    {
      id: 1,
      status: "queued",
      started_at: "2026-01-01T00:00:00+00:00",
      finished_at: null,
      counts: { added: 1, table: 2 },
      error: null,
      build_type: "full",
      triggered_by: "Alice",
      source_commit: "a".repeat(40),
      logs: [
        {
          timestamp: "2026-01-01T00:00:00+00:00",
          stage: "queue",
          level: "info",
          message: "full build queued",
        },
      ],
    },
  ];
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/builds" && options?.method === "POST") {
        const body = JSON.parse(String(options.body)) as {
          build_type: "full" | "incremental";
        };
        requests.push(body as unknown as Record<string, unknown>);
        const created = {
          ...builds[0],
          id: builds.length + 1,
          build_type: body.build_type,
          logs: [
            {
              timestamp: "2026-01-01T00:00:01+00:00",
              stage: "queue",
              level: "info",
              message: `${body.build_type} build queued`,
            },
          ],
        };
        builds.unshift(created as (typeof builds)[number]);
        return Response.json(created, { status: 202 });
      }
      return Response.json(builds);
    }),
  );
  render(<BuildPage token="token" messages={getMessages("zh")} />);
  expect((await screen.findAllByText("排队中")).length).toBeGreaterThan(0);
  expect(await screen.findByText("full build queued")).toBeTruthy();
  expect(await screen.findByText("新增文件")).toBeTruthy();
  expect(await screen.findByText("阶段")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "增量构建" }));
  await waitFor(() =>
    expect(requests).toEqual([{ build_type: "incremental" }]),
  );
  expect(await screen.findByText("incremental build queued")).toBeTruthy();
  expect(screen.getByText("构建已加入队列")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "全量构建" }));
  await waitFor(() =>
    expect(requests).toEqual([
      { build_type: "incremental" },
      { build_type: "full" },
    ]),
  );
  expect(await screen.findByText("full build queued")).toBeTruthy();
});

test("loads and saves the scheduled build interval", async () => {
  const requests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/settings" && options?.method === "PUT") {
        requests.push(JSON.parse(String(options.body)));
        return Response.json({});
      }
      if (url === "/api/v1/settings")
        return Response.json({ schedule_interval_seconds: "60" });
      if (url === "/api/v1/generations")
        return Response.json({
          generations: [],
          behind: false,
          source_head: null,
        });
      return Response.json([]);
    }),
  );
  render(<BuildPage token="token" messages={getMessages("zh")} />);
  const input = (await screen.findByLabelText(
    "间隔秒数（0 为关闭）",
  )) as HTMLInputElement;
  await waitFor(() => expect(input.value).toBe("60"));
  fireEvent.change(input, { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: "保存定时" }));
  await waitFor(() =>
    expect(requests).toEqual([{ values: { schedule_interval_seconds: "5" } }]),
  );
});
