// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { ResultTable } from "./ResultTable";

afterEach(cleanup);

test("long field names and values stay readable and the table scrolls by keyboard", () => {
  const column = "customer_lifetime_value_segment_code_with_a_long_name";
  const value = "1234567890.123456789-华东-线上-会员生命周期价值分层";
  render(
    <ResultTable
      result={{
        id: "query-long",
        columns: [column],
        rows: [[value]],
        row_count: 1,
      }}
      label="结果"
    />,
  );

  const region = screen.getByRole("region", { name: "结果" });
  expect(region.classList.contains("result-table-scroll")).toBe(true);
  expect(region.getAttribute("tabindex")).toBe("0");
  expect(within(region).getByText(column).getAttribute("title")).toBeNull();
  expect(within(region).getByText(value).getAttribute("title")).toBeNull();

  region.focus();
  expect(document.activeElement).toBe(region);
});
