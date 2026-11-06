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
