// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { CorrectionsPage } from "./CorrectionsPage";
import { getMessages } from "./i18n";

beforeEach(() => {
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
});

test.each(["approve", "edited", "reject"])(
  "review %s uses the correct endpoint and patch",
  async (action) => {
    const fetcher = vi.fn(async (url: string, options?: RequestInit) =>
      options?.method === "POST"
        ? Response.json({})
        : url === "/api/v1/projects"
          ? Response.json([
              {
                id: "default",
                name: "业务数据",
                type: "warehouse",
                status: "active",
                created_at: "2026-01-01T00:00:00Z",
                my_role: "admin",
              },
            ])
          : Response.json([
              {
                id: "p1",
                target: "document:a",
                reason: "wrong",
                diff: "-old\n+new",
                patch: "new",
                status: "pending",
              },
            ]),
    );
    vi.stubGlobal("fetch", fetcher);
    render(<CorrectionsPage token="token" messages={getMessages("en")} />);
    fireEvent.click(await screen.findByRole("button", { name: /document:a/ }));
    expect(screen.getByText("-old +new")).toBeTruthy();
    if (action === "edited")
      fireEvent.change(screen.getByLabelText("Edited content"), {
        target: { value: "edited" },
      });
    fireEvent.click(
      screen.getByRole("button", {
        name:
          action === "edited"
            ? "Approve with edits"
            : action === "approve"
              ? "Approve"
              : "Reject",
      }),
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          ([url, options]) =>
            url ===
              `/api/v1/corrections/p1/${action === "reject" ? "reject" : "approve"}?project=default` &&
            options?.method === "POST" &&
            (action !== "edited" ||
              options.body === JSON.stringify({ patch: "edited" })),
        ),
      ).toBe(true),
    );
  },
);

test("knowledge gaps show question and missing information", async () => {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      if (url === "/api/v1/projects")
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
      return Response.json([
        { id: "g", question: "Which metric?", missing: "Definition" },
      ]);
    }),
  );
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  expect(await screen.findByText("Which metric?")).toBeTruthy();
  expect(screen.getByText("Definition")).toBeTruthy();
  expect(urls).toContain("/api/v1/knowledge-gaps?project=default");
});

test("filters correction proposals by status and displays status tags", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects")
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
      return Response.json([
        {
          id: "p1",
          target: "document:a",
          reason: "fix a",
          diff: "-old\n+new",
          patch: "new",
          status: "pending",
        },
        {
          id: "p2",
          target: "document:b",
          reason: "fix b",
          diff: "-old\n+new",
          patch: "new",
          status: "approved",
        },
        {
          id: "p3",
          target: "document:c",
          reason: "fix c",
          diff: "-old\n+new",
          patch: "new",
          status: "rejected",
        },
      ]);
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("en")} />);

  expect(
    await screen.findByRole("button", { name: /document:a/ }),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: /document:b/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /document:c/ })).toBeTruthy();

  expect(screen.getByText("Pending")).toBeTruthy();
  expect(screen.getByText("Approved")).toBeTruthy();
  expect(screen.getByText("Rejected")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Pending (1)" }));
  expect(screen.getByRole("button", { name: /document:a/ })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /document:b/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /document:c/ })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Resolved (2)" }));
  expect(screen.queryByRole("button", { name: /document:a/ })).toBeNull();
  expect(screen.getByRole("button", { name: /document:b/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /document:c/ })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "All (3)" }));
  expect(screen.getByRole("button", { name: /document:a/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /document:b/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /document:c/ })).toBeTruthy();
});

test("filters by source, renders gate_report and expanded statuses, and supports admin revert", async () => {
  const calls: { url: string; method?: string }[] = [];
  let failRevert = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({ url, method: options?.method });
      if (url === "/api/v1/projects") {
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
      if (
        url === "/api/v1/corrections/p-auto/revert?project=default" &&
        options?.method === "POST"
      ) {
        if (failRevert) {
          return Response.json(
            { detail: "源目录在提交后已发生变化，无法直接回滚" },
            { status: 409 },
          );
        }
        return Response.json({
          id: "p-auto",
          status: "reverted",
        });
      }
      return Response.json([
        {
          id: "p-manual",
          target: "document:a",
          reason: "manual fix",
          diff: "-old\n+new",
          patch: "new",
          status: "pending",
          source: "manual",
          target_kind: "document",
          gate_report: {
            passed: false,
            checks: [
              { name: "secrets_and_injection", passed: false, details: [] },
            ],
            errors: ["检测到潜在密钥或注入片段"],
          },
        },
        {
          id: "p-auto",
          target: "table:shop.orders",
          reason: "mcp fix",
          diff: "--- a\n+++ b\n+comment: 订单",
          patch: "version: 1",
          status: "auto_applied",
          source: "mcp",
          target_kind: "override",
          gate_report: {
            passed: true,
            checks: [{ name: "json_schema", passed: true, details: [] }],
            errors: [],
          },
        },
        {
          id: "p-evo",
          target: "field:shop.orders.status",
          reason: "evolution fix",
          diff: "-a\n+b",
          patch: "version: 1",
          status: "reverted",
          source: "evolution",
          target_kind: "override",
          gate_report: { passed: true, checks: [], errors: [] },
        },
      ]);
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("自动生效")).toBeTruthy();
  expect(screen.getByText("已回滚")).toBeTruthy();

  // Filter by source: mcp
  fireEvent.click(screen.getByRole("button", { name: "MCP" }));
  expect(
    screen.getByRole("button", { name: /table:shop.orders/ }),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: /document:a/ })).toBeNull();
  expect(
    screen.queryByRole("button", { name: /field:shop.orders.status/ }),
  ).toBeNull();

  // Inspect p-auto gate_report & trigger revert
  fireEvent.click(screen.getByRole("button", { name: /table:shop.orders/ }));
  expect(screen.getByText("L1 预检通过")).toBeTruthy();

  failRevert = true;
  fireEvent.click(screen.getByRole("button", { name: "回滚此纠错" }));
  expect(
    await screen.findByText("源目录在提交后已发生变化，无法直接回滚"),
  ).toBeTruthy();

  failRevert = false;
  fireEvent.click(screen.getByRole("button", { name: "回滚此纠错" }));
  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/corrections/p-auto/revert?project=default" &&
          c.method === "POST",
      ),
    ).toBe(true),
  );

  // Switch source back to All and check failed gate_report error
  fireEvent.click(screen.getByRole("button", { name: "全部来源" }));
  fireEvent.click(screen.getByRole("button", { name: /document:a/ }));
  expect(screen.getByText("L1 预检未通过")).toBeTruthy();
  expect(screen.getByText("检测到潜在密钥或注入片段")).toBeTruthy();
});

test("manages eval questions tab (confirm, create, delete)", async () => {
  const evalQuestions = [
    {
      id: "eq-1",
      question: "昨日成交订单数是多少？",
      caliber: "仅统计已支付订单",
      reference_sql: "SELECT COUNT(*) FROM shop.orders WHERE status = 'paid'",
      expected_tables: ["shop.orders"],
      expected_fields: ["shop.orders.status"],
      notes: "",
      dialect: "hive",
      clarify: false,
      category: "单表聚合",
      split: "dev",
      answer_confirmed: false,
      source: "feedback",
      conversation_id: "c1",
      turn_id: 1,
      proposal_id: null,
      created_by: "u1",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      redacted: false,
    },
  ];
  const calls: { url: string; method: string; body?: unknown }[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? "GET";
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      calls.push({ url, method, body });
      if (url === "/api/v1/projects") {
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
      if (url.startsWith("/api/v1/corrections")) {
        return Response.json([]);
      }
      if (
        url === "/api/v1/eval-questions?project=default" &&
        method === "GET"
      ) {
        return Response.json(evalQuestions);
      }
      if (
        url === "/api/v1/eval-questions/eq-1?project=default" &&
        method === "PATCH"
      ) {
        evalQuestions[0] = { ...evalQuestions[0], ...body };
        return Response.json(evalQuestions[0]);
      }
      if (
        url === "/api/v1/eval-questions?project=default" &&
        method === "POST"
      ) {
        const created = {
          ...evalQuestions[0],
          id: "eq-2",
          ...body,
          source: "manual",
        };
        evalQuestions.push(created);
        return Response.json(created, { status: 201 });
      }
      if (
        url === "/api/v1/eval-questions/eq-1?project=default" &&
        method === "DELETE"
      ) {
        evalQuestions.splice(0, 1);
        return new Response(null, { status: 204 });
      }
      return Response.json({});
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("zh")} />);

  // Eval questions are lazy-loaded when switching tabs
  expect(calls.some((c) => c.url.startsWith("/api/v1/eval-questions"))).toBe(
    false,
  );

  fireEvent.click(screen.getByRole("tab", { name: "评测题集" }));
  expect(await screen.findByText("昨日成交订单数是多少？")).toBeTruthy();
  expect(screen.getByText("待确认")).toBeTruthy();

  // Confirm draft question
  fireEvent.click(screen.getByRole("button", { name: "确认纳入" }));
  await waitFor(() =>
    expect(calls).toContainEqual({
      url: "/api/v1/eval-questions/eq-1?project=default",
      method: "PATCH",
      body: { answer_confirmed: true },
    }),
  );
  expect(await screen.findByText("已确认")).toBeTruthy();

  // Create new eval question with doris dialect (and confirm spark option is available)
  fireEvent.click(screen.getByRole("button", { name: "新增评测题" }));
  fireEvent.change(screen.getByLabelText("评测问题"), {
    target: { value: "近7日退款金额" },
  });
  fireEvent.change(screen.getByLabelText("口径描述"), {
    target: { value: "统计已退款订单金额之和" },
  });
  fireEvent.change(screen.getByLabelText("参考 SQL"), {
    target: { value: "SELECT SUM(refund_amount) FROM shop.refunds" },
  });
  fireEvent.mouseDown(screen.getByLabelText("SQL 方言"));
  expect(await screen.findByTitle("spark")).toBeTruthy();
  fireEvent.click(await screen.findByTitle("doris"));
  fireEvent.click(screen.getByRole("button", { name: "保存评测题" }));

  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/eval-questions?project=default" &&
          c.method === "POST" &&
          (c.body as { dialect?: string })?.dialect === "doris",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("近7日退款金额")).toBeTruthy();

  // Delete eq-1
  const deleteButtons = screen.getAllByRole("button", { name: "删除" });
  fireEvent.click(deleteButtons[0]);
  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/eval-questions/eq-1?project=default" &&
          c.method === "DELETE",
      ),
    ).toBe(true),
  );
});

test("auto-selects initialProposalId and displays evolution provenance and L2 gate metrics", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") {
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
      if (url === "/api/v1/corrections?project=default") {
        return Response.json([
          {
            id: "prop-other",
            target: "semantic_overrides:other",
            reason: "人工修正",
            diff: "-a\n+b",
            patch: "b",
            status: "pending",
            source: "manual",
          },
          {
            id: "prop-evo",
            target: "semantic_overrides:gmv",
            reason: "演化提升 GMV 口径准确率",
            diff: "-old\n+new",
            patch: "new",
            status: "pending",
            source: "evolution",
            gate_report: {
              passed: true,
              checks: [{ name: "yaml_lint", passed: true, details: [] }],
              errors: [],
              l2_gate: {
                passed: true,
                mode: "cold_start_ab",
                evaluated_cases: 6,
                W_c: 5,
                W_p: 1,
                E_c: 0,
              },
              provenance: {
                evolution_run_id: "run-abc",
                round_number: 2,
                signal_kind: "sql_self_repair",
                hypothesis: "补充 GMV 过滤条件减少 SQL 自修",
                evidence_trajectory_ids: [10, 11],
              },
            },
          },
        ]);
      }
      return Response.json([]);
    }),
  );

  render(
    <CorrectionsPage
      token="token"
      messages={getMessages("zh")}
      initialProposalId="prop-evo"
    />,
  );

  expect(
    await screen.findByText("补充 GMV 过滤条件减少 SQL 自修"),
  ).toBeTruthy();
  expect(screen.getByText(/run-abc/)).toBeTruthy();
  expect(screen.getByText(/W_c=5/)).toBeTruthy();
});

test("displays confirmed_regression regressions list on evolution proposal", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") {
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
      if (url === "/api/v1/corrections?project=default") {
        return Response.json([
          {
            id: "prop-reg",
            target: "semantic_overrides:orders",
            reason: "回归门禁结果展示",
            diff: "-a\n+b",
            patch: "b",
            status: "pending",
            source: "evolution",
            gate_report: {
              passed: false,
              checks: [],
              errors: [],
              l2_gate: {
                passed: false,
                mode: "confirmed_regression",
                evaluated_cases: 4,
                regressions: ["q-3"],
              },
              provenance: {
                evolution_run_id: "run-reg",
                round_number: 1,
                signal_kind: "search_detour",
                hypothesis: "展示回归题目列表",
              },
            },
          },
        ]);
      }
      return Response.json([]);
    }),
  );

  render(
    <CorrectionsPage
      token="token"
      messages={getMessages("zh")}
      initialProposalId="prop-reg"
    />,
  );

  expect(await screen.findByText("展示回归题目列表")).toBeTruthy();
  expect(screen.getByText(/regressions=1 \(q-3\)/)).toBeTruthy();
});

test("deep-link initialProposalId populates patch and supports Edit & Approve on structured relationship proposal", async () => {
  const calls: { url: string; method?: string; body?: unknown }[] = [];
  const structuredPatch = {
    version: 1,
    relationships: {
      orders_to_users: {
        name: "orders_to_users",
        from_database: "shop",
        from_table: "orders",
        from_columns: ["user_id"],
        to_database: "shop",
        to_table: "users",
        to_columns: ["id"],
        instructions: "原始连接说明",
      },
    },
  };
  const initialYaml =
    "version: 1\nrelationships:\n  orders_to_users:\n    name: orders_to_users\n    instructions: 原始连接说明\n";

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      calls.push({ url, method: options?.method, body });
      if (url === "/api/v1/projects") {
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
      if (url === "/api/v1/corrections?project=default") {
        return Response.json([
          {
            id: "prop-rel",
            target: "relationship:orders_to_users",
            target_kind: "override",
            reason: "外键关联",
            diff: "-old\n+new",
            patch: initialYaml,
            structured_patch: structuredPatch,
            status: "pending",
            source: "evolution",
          },
        ]);
      }
      return Response.json({ id: "prop-rel", status: "approved" });
    }),
  );

  render(
    <CorrectionsPage
      token="token"
      messages={getMessages("zh")}
      initialProposalId="prop-rel"
    />,
  );

  const patchInput = (await screen.findByLabelText(
    "修改内容",
  )) as HTMLTextAreaElement;
  expect(patchInput.value).toBe(initialYaml);

  // Edit JSON structured patch directly and approve with edits -> sends both patch and structured_patch
  const editedObj = {
    version: 1,
    relationships: {
      orders_to_users: {
        ...structuredPatch.relationships.orders_to_users,
        instructions: "界面编辑后的连接说明",
      },
    },
  };
  const editedJson = JSON.stringify(editedObj, null, 2);
  fireEvent.change(patchInput, { target: { value: editedJson } });
  fireEvent.click(screen.getByRole("button", { name: "修改后批准" }));

  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/corrections/prop-rel/approve?project=default" &&
          c.method === "POST" &&
          (c.body as { patch?: string; structured_patch?: unknown })?.patch ===
            editedJson &&
          JSON.stringify(
            (c.body as { structured_patch?: unknown })?.structured_patch,
          ) === JSON.stringify(editedObj),
      ),
    ).toBe(true),
  );
});

test("long proposal targets expose decoded title tooltips for ellipsis overflow", async () => {
  const longTarget =
    "relationship:hive_prod.dwd.dwd_orders:hive_prod.dim.dim_city:fk_orders_city";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") {
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
      return Response.json([
        {
          id: "prop-long",
          target: longTarget,
          reason: "强化外键说明",
          diff: "-old\n+new",
          patch: "new",
          status: "pending",
          source: "evolution",
        },
      ]);
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("zh")} />);
  const itemBtn = await screen.findByRole("button", {
    name: /relationship:hive_prod\.dwd\.dwd_orders/,
  });
  expect(itemBtn.getAttribute("title")).toBe(longTarget);
  const monoSpan = itemBtn.querySelector(".mono");
  expect(monoSpan?.getAttribute("title")).toBe(longTarget);
});
