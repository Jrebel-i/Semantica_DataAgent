// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SearchPage } from "./SearchPage";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("searches selected scope and category and displays fusion evidence", async () => {
  const fetcher = vi.fn(async (url: string) =>
    url === "/api/v1/categories"
      ? Response.json([
          {
            name: "metrics",
            ref: "category:metrics",
            description: "",
            documents: [],
          },
        ])
      : Response.json([
          {
            ref: "document:metrics/a#chunk-1",
            type: "document",
            content: "销售额",
            score: 0.0321,
            sources: ["fulltext", "vector"],
            confidence: "high",
            ambiguous: true,
            ambiguity_refs: [
              "document:metrics/a#chunk-1",
              "document:metrics/b#chunk-1",
            ],
          },
        ]),
  );
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("zh")} />);
  await screen.findByRole("option", { name: "metrics" });
  fireEvent.change(screen.getByLabelText("查询内容"), {
    target: { value: "销售额" },
  });
  fireEvent.change(screen.getByLabelText("检索范围"), {
    target: { value: "docs" },
  });
  fireEvent.change(screen.getByLabelText("知识类"), {
    target: { value: "metrics" },
  });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  expect(await screen.findByText(/0.0321/)).toBeTruthy();
  expect(screen.getByText(/全文.*向量/)).toBeTruthy();
  expect(screen.getByText("高")).toBeTruthy();
  expect(screen.getByText("可能存在歧义")).toBeTruthy();
  expect(screen.getByText("document:metrics/b#chunk-1")).toBeTruthy();
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      expect.stringContaining("scope=docs&category=metrics"),
      expect.objectContaining({ headers: { Authorization: "Bearer token" } }),
    ),
  );
});

test("clears category when changing scope and reports search failure", async () => {
  const fetcher = vi.fn(async (url: string) =>
    url === "/api/v1/categories"
      ? Response.json([
          {
            name: "metrics",
            ref: "category:metrics",
            description: "",
            documents: [],
          },
        ])
      : new Response("failure", { status: 503 }),
  );
  vi.stubGlobal("fetch", fetcher);
  render(<SearchPage token="token" messages={getMessages("en")} />);
  await screen.findByRole("option", { name: "metrics" });
  fireEvent.change(screen.getByLabelText("Categories"), {
    target: { value: "metrics" },
  });
  fireEvent.change(screen.getByLabelText("Scope"), {
    target: { value: "tables" },
  });
  expect(
    (screen.getByLabelText("Categories") as HTMLSelectElement).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("Query"), {
    target: { value: "orders" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(
    await screen.findByText("Search failed. Please try again."),
  ).toBeTruthy();
  expect(fetcher).toHaveBeenCalledWith(
    expect.stringContaining("scope=tables"),
    expect.anything(),
  );
  const searchUrl = fetcher.mock.calls.find(([url]) =>
    url.startsWith("/api/v1/search"),
  )?.[0];
  expect(searchUrl).not.toContain("category=");
});
