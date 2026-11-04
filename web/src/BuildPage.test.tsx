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
        requests.push(
          JSON.parse(String(options.body)) as Record<string, unknown>,
        );
        const created = {
          ...builds[0],
          id: 2,
          build_type: "incremental",
          logs: [
            {
              timestamp: "2026-01-01T00:00:01+00:00",
              stage: "queue",
              level: "info",
              message: "incremental build queued",
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
  fireEvent.click(screen.getByRole("button", { name: "增量构建" }));
  await waitFor(() =>
    expect(requests).toEqual([{ build_type: "incremental" }]),
  );
  expect(await screen.findByText("incremental build queued")).toBeTruthy();
  expect(screen.getByText("构建已加入队列")).toBeTruthy();
});
