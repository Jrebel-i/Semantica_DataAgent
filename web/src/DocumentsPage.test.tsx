// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DocumentsPage } from "./DocumentsPage";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("browses categories, generated index and numbered document", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/categories")
        return Response.json([
          {
            ref: "category:metrics",
            name: "metrics",
            description: "指标说明",
            documents: [{ ref: "document:metrics/sales.md", path: "sales.md" }],
          },
        ]);
      if (url.startsWith("/api/v1/category-index"))
        return Response.json({
          ref: "index",
          category: "metrics",
          path: "index.md",
          generated: true,
          content: "# 自动生成\n销售目录",
          start_line: 1,
          end_line: 2,
        });
      return Response.json({
        ref: "document:metrics/sales.md",
        category: "metrics",
        path: "sales.md",
        generated: false,
        content: "# 销售\n支付金额",
        start_line: 1,
        end_line: 2,
      });
    }),
  );
  render(<DocumentsPage token="token" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByText("metrics"));
  expect(
    await screen.findByRole("heading", { name: /index.md.*自动生成/ }),
  ).toBeTruthy();
  expect(screen.getByText(/1 # 自动生成/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "sales.md" }));
  expect(await screen.findByText(/2 支付金额/)).toBeTruthy();
});
