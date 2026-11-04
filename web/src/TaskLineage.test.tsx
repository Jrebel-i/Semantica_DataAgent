// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { TaskLineage } from "./TaskLineage";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("shows table dependencies, evidence and producing tasks", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        upstream: [
          {
            upstream: "raw.orders",
            downstream: "shop.orders",
            evidence: "both",
            parse_status: "success",
            task_id: "load",
            ref: "lineage:1",
          },
        ],
        downstream: [
          {
            upstream: "shop.orders",
            downstream: "shop.report",
            evidence: "parsed",
            parse_status: "success",
            task_id: "report",
            ref: "lineage:2",
          },
        ],
        tasks: [{ name: "load", ref: "task:load" }],
      }),
    ),
  );
  render(
    <TaskLineage
      table="shop.orders"
      token="token"
      messages={getMessages("en")}
    />,
  );
  expect(await screen.findByText(/raw.orders.*both/)).toBeTruthy();
  expect(screen.getByText(/shop.report.*parsed/)).toBeTruthy();
  expect(screen.getByText("load")).toBeTruthy();
});
