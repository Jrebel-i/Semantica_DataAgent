// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { KnowledgePage, warehouseLayer } from "./KnowledgePage";
import { getMessages } from "./i18n";
import { PROJECT_STORAGE_KEY } from "./ProjectSelector";
import { clearTableDetailCache } from "./tableDetail";

type TableRow = {
  database: string;
  name: string;
  comment: string;
  ref: string;
  field_count: number;
  document_count: number;
  has_partition: boolean;
  documents: {
    ref: string;
    path: string;
    category: string;
  }[];
  fields: {
    name: string;
    type: string;
    comment: string;
    partition: boolean;
  }[];
};

type ProjectRow = {
  id: string;
  name: string;
  type: "warehouse" | "document";
  status: "active" | "archived" | "deleting";
  created_at: string;
  my_role: "admin" | "member";
};

function project(
  id: string,
  name: string,
  type: ProjectRow["type"] = "warehouse",
  status: ProjectRow["status"] = "active",
): ProjectRow {
  return {
    id,
    name,
    type,
    status,
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  };
}

const projects = [
  project("default", "业务数据"),
  project("docs", "Platform Docs", "document"),
];

function table(database: string, name: string): TableRow {
  return {
    database,
    name,
    comment: `${name} 说明`,
    ref: `table:${database}.${name}`,
    field_count: 1,
    document_count: 0,
    has_partition: false,
    documents: [],
    fields: [
      {
        name: "dt",
        type: "string",
        comment: "日期分区",
        partition: true,
      },
      {
        name: "air_segment_income_cny",
        type: "decimal(18,2)",
        comment: "航段收入",
        partition: false,
      },
      {
        name: "manual_markup_cny",
        type: "decimal(18,2)",
        comment: "手工加价",
        partition: false,
      },
      {
        name: "service_shopping_request_count",
        type: "bigint",
        comment: "服务购物请求数",
        partition: false,
      },
    ],
  };
}

function installFetch(
  tables: TableRow[] | ((project: string) => TableRow[]),
  categories: unknown[] = [],
  detailFailure?: () => boolean,
  projectRows: ProjectRow[] = projects,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") return Response.json(projectRows);
      if (url.startsWith("/api/v1/categories"))
        return Response.json(categories);
      if (url.startsWith("/api/v1/tables?")) {
        const projectId =
          new URL(url, "http://localhost").searchParams.get("project") ?? "";
        return Response.json(
          typeof tables === "function" ? tables(projectId) : tables,
        );
      }
      const detail = url.match(
        /^\/api\/v1\/tables\/([^/?]+)\/([^/?]+)(?:\?.*)?$/,
      );
      if (detail) {
        if (detailFailure?.()) return new Response("", { status: 500 });
        const database = decodeURIComponent(detail[1]);
        const name = decodeURIComponent(detail[2]);
        const projectId =
          new URL(url, "http://localhost").searchParams.get("project") ?? "";
        const rows = typeof tables === "function" ? tables(projectId) : tables;
        const row = rows.find(
          (item) => item.database === database && item.name === name,
        );
        return row ? Response.json(row) : new Response("", { status: 404 });
      }
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

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function treeTitles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".ant-tree-title")).map(
    (node) => node.textContent?.trim() ?? "",
  );
}

beforeEach(() => {
  localStorage.clear();
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
  clearTableDetailCache();
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

test("列表请求带 summary 视图，点击表后请求详情并按 ref 缓存", async () => {
  const tables = [table("shop", "orders"), table("shop", "customers")];
  installFetch(tables);
  const fetcher = fetch as unknown as ReturnType<typeof vi.fn>;
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByRole("button", { name: "orders" })).toBeTruthy();
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(([url]) => {
        if (!String(url).startsWith("/api/v1/tables?")) return false;
        const params = new URL(String(url), "http://localhost").searchParams;
        return (
          params.get("view") === "summary" &&
          params.get("project") === "default"
        );
      }),
    ).toBe(true),
  );
  await screen.findByText("日期分区");
  const detailCalls = () =>
    fetcher.mock.calls.filter(
      ([url]) =>
        String(url).startsWith("/api/v1/tables/shop/orders?") ||
        String(url).startsWith("/api/v1/tables/shop/customers?"),
    );
  await waitFor(() => expect(detailCalls()).toHaveLength(1));

  fireEvent.click(screen.getByRole("button", { name: "customers" }));
  await waitFor(() => expect(detailCalls()).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: "orders" }));
  fireEvent.click(screen.getByRole("button", { name: "customers" }));
  await waitFor(() => expect(detailCalls()).toHaveLength(2));
});

test("详情请求失败显示错误，重试后恢复", async () => {
  let fail = true;
  installFetch([table("shop", "orders")], [], () => fail);
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("表详情加载失败")).toBeTruthy();
  expect(screen.getByRole("button", { name: /重\s*试/ })).toBeTruthy();
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
  expect(await screen.findByText("日期分区")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull();
});

test("字段表放在横向滚动容器里", async () => {
  installFetch([table("shop", "orders")]);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(await screen.findByText("日期分区")).toBeTruthy();
  const scroll = container.querySelector(".knowledge-table-scroll");
  expect(scroll).not.toBeNull();
  expect(scroll?.querySelector("table.data-table")).not.toBeNull();
});

test("长字段名和类型列使用不换行的列标记", async () => {
  installFetch([table("shop", "orders")]);
  const { container } = render(
    <KnowledgePage token="token" messages={getMessages("zh")} />,
  );

  expect(
    await screen.findByRole("button", { name: "air_segment_income_cny" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "service_shopping_request_count" }),
  ).toBeTruthy();
  const headers = container.querySelectorAll("th.field-name, th.field-type");
  expect(headers.length).toBe(2);
  const nameCell = screen
    .getByRole("button", { name: "air_segment_income_cny" })
    .closest("td");
  expect(nameCell?.classList.contains("field-name")).toBe(true);
  const typeCell = container.querySelector("td.field-type");
  expect(typeCell?.textContent).toBe("string");
});

test("搜索仍按表名过滤，并继续使用 summary 视图", async () => {
  const tables = [table("quick", "quick_orders"), table("dm", "dm_customers")];
  installFetch(tables);
  const fetcher = fetch as unknown as ReturnType<typeof vi.fn>;
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

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
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(([url]) => {
        const value = String(url);
        return (
          value.includes("search=customer") && value.includes("view=summary")
        );
      }),
    ).toBe(true),
  );
});

test("切换项目后请求带新项目并清空已选表、详情和纠错草稿", async () => {
  installFetch(
    (projectId) =>
      projectId === "finance"
        ? [table("dwd", "finance_orders")]
        : [table("shop", "orders")],
    [],
    undefined,
    [project("default", "业务数据"), project("finance", "财务数据")],
  );
  const fetcher = fetch as unknown as ReturnType<typeof vi.fn>;
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByRole("button", { name: "orders" })).toBeTruthy();
  expect(await screen.findByText("日期分区")).toBeTruthy();

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("财务数据"));

  expect(await screen.findByText("dwd (1)")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "orders" })).toBeNull();
  expect(await screen.findByText("从左侧选择一张表或一份文档")).toBeTruthy();
  expect(screen.queryByText("日期分区")).toBeNull();
  fireEvent.click(screen.getByText("dwd (1)"));
  expect(
    await screen.findByRole("button", { name: "finance_orders" }),
  ).toBeTruthy();

  await waitFor(() => {
    const urls = fetcher.mock.calls.map(([url]) => String(url));
    const tableList = urls.filter((url) => url.startsWith("/api/v1/tables?"));
    const categories = urls.filter((url) =>
      url.startsWith("/api/v1/categories"),
    );
    expect(tableList.at(-1)).toContain("project=finance");
    expect(categories.at(-1)).toContain("project=finance");
  });

  fireEvent.click(screen.getByRole("button", { name: "finance_orders" }));
  expect(await screen.findByText("日期分区")).toBeTruthy();
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/tables/dwd/finance_orders?") &&
          String(url).includes("project=finance"),
      ),
    ).toBe(true),
  );
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/table-lineage?") &&
          String(url).includes("project=finance"),
      ),
    ).toBe(true),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "air_segment_income_cny" }),
  );
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/column-lineage?") &&
          String(url).includes("project=finance"),
      ),
    ).toBe(true),
  );

  fireEvent.click(screen.getByRole("button", { name: "提出纠错" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("替换后的说明"), {
    target: { value: "新说明" },
  });
  fireEvent.change(within(dialog).getByLabelText("纠错理由"), {
    target: { value: "项目切换后仍应带项目" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "提出纠错" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          String(url) === "/api/v1/corrections?project=finance" &&
          init?.method === "POST",
      ),
    ).toBe(true),
  );
});

test("切换项目后重置表分页与树的展开状态", async () => {
  const many = Array.from({ length: 101 }, (_, index) =>
    table("quick", `quick_${index}`),
  );
  installFetch(
    (projectId) =>
      projectId === "finance" ? [table("dwd", "finance_orders")] : many,
    [],
    undefined,
    [project("default", "业务数据"), project("finance", "财务数据")],
  );
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("更多 (1)")).toBeTruthy();
  fireEvent.click(screen.getByText("更多 (1)"));
  expect(await screen.findByRole("button", { name: "quick_100" })).toBeTruthy();

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("财务数据"));
  expect(await screen.findByText("dwd (1)")).toBeTruthy();

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("业务数据"));
  expect(await screen.findByText("quick (101)")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "quick_100" })).toBeNull();
  fireEvent.click(screen.getByText("quick (101)"));
  expect(await screen.findByText("更多 (1)")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "quick_100" })).toBeNull();
});

test("文档型项目不渲染数据表，知识文档请求带项目", async () => {
  localStorage.setItem(PROJECT_STORAGE_KEY, "docs");
  installFetch(
    [table("shop", "orders")],
    [
      {
        ref: "category:platform",
        name: "平台",
        description: "",
        documents: [{ ref: "document:platform/a.md", path: "platform/a.md" }],
      },
    ],
  );
  const fetcher = fetch as unknown as ReturnType<typeof vi.fn>;
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  expect(await screen.findByText("知识文档 (1)")).toBeTruthy();
  expect(screen.queryByText(/数据表/)).toBeNull();
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/categories?") &&
          String(url).includes("project=docs"),
      ),
    ).toBe(true),
  );
  expect(
    fetcher.mock.calls.some(([url]) =>
      String(url).startsWith("/api/v1/tables?"),
    ),
  ).toBe(false);

  fireEvent.click(screen.getByText("平台"));
  fireEvent.click(await screen.findByRole("button", { name: "platform/a.md" }));
  expect(await screen.findByText(/订单定义/)).toBeTruthy();
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/document?") &&
          String(url).includes("project=docs"),
      ),
    ).toBe(true),
  );
});

test("切换项目后丢弃旧项目迟到的文档响应", async () => {
  const pendingDocument = deferredResponse();
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects")
      return Response.json([
        project("docs", "Platform Docs", "document"),
        project("finance", "财务数据"),
      ]);
    if (url.startsWith("/api/v1/categories"))
      return Response.json([
        {
          ref: "category:platform",
          name: "平台",
          description: "",
          documents: [{ ref: "document:platform/a.md", path: "platform/a.md" }],
        },
      ]);
    if (url.startsWith("/api/v1/tables?")) return Response.json([]);
    if (url.startsWith("/api/v1/document")) return pendingDocument.promise;
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  localStorage.setItem(PROJECT_STORAGE_KEY, "docs");
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByText("平台"));
  fireEvent.click(await screen.findByRole("button", { name: "platform/a.md" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).startsWith("/api/v1/document"),
      ),
    ).toBe(true),
  );

  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("财务数据"));

  await act(async () => {
    pendingDocument.resolve(
      Response.json({
        path: "platform/a.md",
        category: "platform",
        content: "旧项目文档",
        start_line: 1,
        generated: false,
      }),
    );
    await pendingDocument.promise;
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  expect(screen.queryByText("旧项目文档")).toBeNull();
  expect(screen.getByText("从左侧选择一张表或一份文档")).toBeTruthy();
});

test("切换项目后丢弃旧项目迟到的表列表响应", async () => {
  const pendingTables = deferredResponse();
  const fetcher = vi.fn(async (url: string) => {
    if (url === "/api/v1/projects")
      return Response.json([
        project("default", "业务数据"),
        project("finance", "财务数据"),
      ]);
    if (url.startsWith("/api/v1/categories")) return Response.json([]);
    if (url.startsWith("/api/v1/tables?")) {
      const projectId =
        new URL(url, "http://localhost").searchParams.get("project") ?? "";
      return projectId === "default"
        ? pendingTables.promise
        : Response.json([table("dwd", "finance_orders")]);
    }
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<KnowledgePage token="token" messages={getMessages("zh")} />);

  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) =>
          String(url).startsWith("/api/v1/tables?") &&
          String(url).includes("project=default"),
      ),
    ).toBe(true),
  );
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "项目" }));
  fireEvent.click(await screen.findByTitle("财务数据"));
  expect(await screen.findByText("dwd (1)")).toBeTruthy();
  fireEvent.click(screen.getByText("dwd (1)"));
  expect(
    await screen.findByRole("button", { name: "finance_orders" }),
  ).toBeTruthy();

  await act(async () => {
    pendingTables.resolve(Response.json([table("dwd", "orders")]));
    await pendingTables.promise;
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  expect(screen.queryByRole("button", { name: "orders" })).toBeNull();
  expect(screen.getByRole("button", { name: "finance_orders" })).toBeTruthy();
});
