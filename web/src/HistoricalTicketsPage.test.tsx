// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { HistoricalTicketsPage } from "./HistoricalTicketsPage";
import { getMessages } from "./i18n";

let originalGetComputedStyle: PropertyDescriptor | undefined;

beforeEach(() => {
  localStorage.clear();
  originalGetComputedStyle = Object.getOwnPropertyDescriptor(
    window,
    "getComputedStyle",
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
  Object.defineProperty(window, "getComputedStyle", {
    configurable: true,
    writable: true,
    value: () => ({ getPropertyValue: () => "" }),
  });
  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true,
    value: class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (originalGetComputedStyle) {
    Object.defineProperty(window, "getComputedStyle", originalGetComputedStyle);
  } else {
    delete (window as unknown as Record<string, unknown>).getComputedStyle;
  }
});

test("lists historical tickets, navigates to pending proposals, and supports create/edit/delete", async () => {
  const tickets = [
    {
      slug: "air-sales-gmv",
      path: "knowledge/historical-tickets/air-sales-gmv.md",
      title: "机票销售额按 finished_time 统计",
      summary: "按 finished_time 过滤并取最新快照",
      sql_preview: "SELECT SUM(gmv_cny) FROM dm.dm_air_orders",
      dialect: "doris",
      tables: ["dm.dm_air_orders"],
      source_label: "对话点赞沉淀",
      content:
        "# 机票销售额按 finished_time 统计\n\n## 业务口径与上下文\n\n按 finished_time 过滤并取最新快照\n\n## 参考 SQL\n\n```sql\nSELECT SUM(gmv_cny) FROM dm.dm_air_orders\n```\n",
      size_bytes: 200,
      updated_at: "2026-11-26T10:00:00Z",
    },
  ];
  const calls: { url: string; method: string; body?: unknown }[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const method = options?.method ?? "GET";
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      calls.push({ url: input, method, body });
      if (input === "/api/v1/projects") {
        return Response.json([
          {
            id: "default",
            name: "业务数据",
            type: "warehouse",
            status: "active",
            created_at: "2026-01-01T00:00:00Z",
            my_role: "admin",
          },
        ]);
      }
      if (input.startsWith("/api/v1/historical-tickets?") && method === "GET") {
        return Response.json({
          items: tickets,
          pending_proposal_count: 2,
          total_tables: 1,
        });
      }
      if (
        input === "/api/v1/historical-tickets?project=default" &&
        method === "POST"
      ) {
        const created = {
          slug: "refund-orders",
          path: "knowledge/historical-tickets/refund-orders.md",
          title: (body as { title: string }).title,
          summary: (body as { caliber: string }).caliber,
          sql_preview: (body as { reference_sql: string }).reference_sql,
          dialect: "doris",
          tables: ["dm.dm_refunds"],
          source_label: "人工录入",
          content: `# ${(body as { title: string }).title}\n`,
          size_bytes: 120,
          updated_at: "2026-11-26T11:00:00Z",
        };
        tickets.push(created);
        return Response.json(created, { status: 201 });
      }
      if (
        input.startsWith("/api/v1/historical-tickets/") &&
        method === "DELETE"
      ) {
        tickets.shift();
        return new Response(null, { status: 204 });
      }
      return Response.json({});
    }),
  );

  const onNavigateCorrections = vi.fn();
  render(
    <HistoricalTicketsPage
      token="token"
      messages={getMessages("zh")}
      onNavigateCorrections={onNavigateCorrections}
    />,
  );

  expect(
    await screen.findAllByText("机票销售额按 finished_time 统计"),
  ).toBeTruthy();
  expect(screen.getByText("dm.dm_air_orders")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "前往纠错审批 →" }));
  expect(onNavigateCorrections).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "源码编辑" }));
  expect(
    screen.getByText(/SELECT SUM\(gmv_cny\) FROM dm\.dm_air_orders/),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "新增历史工单" }));
  fireEvent.change(screen.getByLabelText("工单标题（标准业务问题）"), {
    target: { value: "近7日退款单量" },
  });
  fireEvent.change(screen.getByLabelText("业务口径与上下文"), {
    target: { value: "仅统计已退款状态" },
  });
  fireEvent.change(screen.getByLabelText("参考 SQL"), {
    target: { value: "SELECT COUNT(1) FROM dm.dm_refunds" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存工单" }));

  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/historical-tickets?project=default" &&
          c.method === "POST",
      ),
    ).toBe(true),
  );
  expect(await screen.findAllByText("近7日退款单量")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "删除工单" }));
  await waitFor(() =>
    expect(calls.some((c) => c.method === "DELETE")).toBe(true),
  );
});
