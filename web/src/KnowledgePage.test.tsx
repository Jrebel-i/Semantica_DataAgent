// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { KnowledgePage, warehouseLayer } from "./KnowledgePage";
import { getMessages } from "./i18n";

type TableRow = {
  database: string;
  name: string;
  comment: string;
  ref: string;
  documents: never[];
  fields: never[];
};

function table(database: string, name: string): TableRow {
  return {
    database,
    name,
    comment: `${name} 说明`,
    ref: `table:${database}.${name}`,
    documents: [],
    fields: [],
  };
}

function installFetch(tables: TableRow[], categories: unknown[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/categories") return Response.json(categories);
      if (url.startsWith("/api/v1/tables")) return Response.json(tables);
      if (url.startsWith("/api/v1/table-lineage"))
        return Response.json({ upstream: [], downstream: [], tasks: [] });
      if (url.startsWith("/api/v1/column-lineage"))
        return Response.json({ upstream: [], downstream: [] });
      if (url.startsWith("/api/v1/document"))
        return Response.json({
          path: "metrics/sales.md",
          category: "metrics",
          content: "订单定义",
          start_line: 1,
          generated: false,
        });
      return Response.json({});
    }),
  );
}

function treeTitles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".ant-tree-title")).map(
    (node) => node.textContent?.trim() ?? "",
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: () => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
    }),
  });
  Object.defineProperty(window, "getComputedStyle", {
    value: () => ({ getPropertyValue: () => "" }),
    writable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("按数仓层次分组，层和库都显示表数", async () => {
  const tables = [
    table("ods", "ods_order"),
    table("ods", "ods_user"),
    table("dwd", "dwd_order"),
    table("dws", "dws_order"),
    table("dwt", "dwt_order"),
    table("dm", "dm_sales"),
    table("app", "app_report"),
    table("dimensions", "dim_date"),
    table("tmp", "tmp_import"),
    table("quick", "quick_lookup"),
  ];
  installFetch(tables);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(await screen.findByText("数据表 (10)")).toBeTruthy();
  await waitFor(() => expect(treeTitles(container)).toContain("ods (2)"));
  const titles = treeTitles(container);
  expect(titles).toEqual(
    expect.arrayContaining([
      "原始层 (2)",
      "ods (2)",
      "明细层 (1)",
      "dwd (1)",
      "汇总层 (2)",
      "dws (1)",
      "dwt (1)",
      "集市层 (1)",
      "dm (1)",
      "应用层 (1)",
      "app (1)",
      "维度层 (1)",
      "dimensions (1)",
      "临时层 (1)",
      "tmp (1)",
      "其他层 (1)",
      "quick (1)",
    ]),
  );
  expect(titles).not.toContain("其他层 (9)");

  const order = [
    "原始层 (2)",
    "明细层 (1)",
    "汇总层 (2)",
    "集市层 (1)",
    "应用层 (1)",
    "维度层 (1)",
    "临时层 (1)",
    "其他层 (1)",
  ].map((title) => titles.indexOf(title));
  expect(order).toEqual([...order].sort((left, right) => left - right));
});

test("库名到数仓层的映射固定，未知库名归入其他层", () => {
  expect(warehouseLayer("ods")).toBe("raw");
  expect(warehouseLayer("ods_archive")).toBe("raw");
  expect(warehouseLayer("dwd")).toBe("detail");
  expect(warehouseLayer("dws")).toBe("summary");
  expect(warehouseLayer("dwt")).toBe("summary");
  expect(warehouseLayer("dm")).toBe("mart");
  expect(warehouseLayer("app")).toBe("application");
  expect(warehouseLayer("dimensions")).toBe("dimension");
  expect(warehouseLayer("tmp")).toBe("temporary");
  expect(warehouseLayer("quick")).toBe("other");
  expect(warehouseLayer("test")).toBe("other");
});

test("默认只展开选中表所在的库，折叠的库不渲染子节点", async () => {
  installFetch([table("ods", "ods_selected"), table("app", "app_hidden")]);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(
    await screen.findByRole("button", { name: "ods_selected" }),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "app_hidden" })).toBeNull();

  fireEvent.click(screen.getByText("app (1)"));
  expect(
    await screen.findByRole("button", { name: "app_hidden" }),
  ).toBeTruthy();
  expect(container.querySelectorAll("*").length).toBeGreaterThan(0);
});

test("大量表时首次只渲染选中库，DOM 节点低于 3000", async () => {
  const distribution: [string, number][] = [
    ["quick", 518],
    ["app", 462],
    ["dm", 265],
    ["tmp", 118],
    ["dimensions", 114],
    ["ods", 63],
    ["dwd", 24],
    ["test", 14],
    ["dws", 9],
    ["dwt", 207],
  ];
  const tables = distribution.flatMap(([database, count]) =>
    Array.from({ length: count }, (_, index) =>
      table(database, `${database}_table_${index}`),
    ),
  );
  expect(tables).toHaveLength(1794);
  installFetch(tables);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(await screen.findByText("数据表 (1794)")).toBeTruthy();
  await waitFor(() =>
    expect(container.querySelectorAll("*").length).toBeLessThan(3000),
  );
  expect(screen.getByRole("button", { name: "quick_table_0" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "app_table_0" })).toBeNull();
  expect(screen.getByRole("button", { name: "quick_table_99" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "quick_table_100" })).toBeNull();
  fireEvent.click(screen.getByText("更多 (418)"));
  expect(
    await screen.findByRole("button", { name: "quick_table_199" }),
  ).toBeTruthy();
});

test("搜索在树数据层过滤，并展开命中的库", async () => {
  installFetch([table("quick", "quick_orders"), table("dm", "dm_customers")]);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(
    await screen.findByRole("button", { name: "quick_orders" }),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索表或文档"), {
    target: { value: "customer" },
  });

  expect(
    await screen.findByRole("button", { name: "dm_customers" }),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "quick_orders" })).toBeNull();
  await waitFor(() => expect(treeTitles(container)).toContain("数据表 (1)"));
  expect(treeTitles(container)).toContain("集市层 (1)");
  expect(treeTitles(container)).toContain("dm (1)");
  expect(treeTitles(container)).not.toContain("quick (1)");
});

test("文档分组仍可展开并打开文档", async () => {
  installFetch(
    [],
    [
      {
        ref: "category:metrics",
        name: "指标",
        description: "",
        documents: [
          { ref: "document:metrics/sales.md", path: "metrics/sales.md" },
        ],
      },
    ],
  );
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByText("知识文档 (1)"));
  fireEvent.click(await screen.findByText("指标"));
  fireEvent.click(
    await screen.findByRole("button", { name: "metrics/sales.md" }),
  );
  expect(await screen.findByText(/订单定义/)).toBeTruthy();
});
