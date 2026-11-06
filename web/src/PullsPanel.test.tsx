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
test("拉取器可用时显示记录并能手动拉取", async () => {
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
