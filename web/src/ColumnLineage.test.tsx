// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ColumnLineage } from "./ColumnLineage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("说明没有字段血缘的可能原因", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ upstream: [], downstream: [] })),
  );
  render(<ColumnLineage table="dst.orders" column="id" token="token" />);
  expect(await screen.findByText(/暂无字段级血缘.*未能解析/)).toBeTruthy();
});

test("展示字段之间的任务及来源", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        upstream: [
          {
            upstream_table: "src.orders",
            upstream_field: "id",
            downstream_table: "dst.orders",
            downstream_field: "id",
            task_id: "load",
            task_type: "spark",
            evidence: "parsed",
            parse_status: "success",
          },
        ],
        downstream: [],
      }),
    ),
  );
  render(<ColumnLineage table="dst.orders" column="id" token="token" />);
  expect(
    (await screen.findByRole("list", { name: "字段血缘明细" })).textContent,
  ).toContain(
    "src.orders.id → dst.orders.id · load (spark) · parsed · success",
  );
});
