// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { CorrectionsPage } from "./CorrectionsPage";
import { App } from "./App";
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
    const { container } = render(
      <CorrectionsPage token="token" messages={getMessages("en")} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /document:a/ }));
    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    expect(screen.getByText("+1")).toBeTruthy();
    expect(screen.getByText("-1")).toBeTruthy();
    expect(container.querySelector(".diff-code-del")?.textContent).toBe("old");
    expect(container.querySelector(".diff-code-add")?.textContent).toBe("new");
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

test("project members can open knowledge gaps from navigation", async () => {
  localStorage.setItem("token", "token");
  history.pushState(null, "", "/search");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/me") return Response.json({ role: "user" });
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "default",
            name: "Default",
            type: "document",
            status: "active",
            my_role: "member",
          },
        ]);
      return Response.json([]);
    }),
  );
  render(<App />);
  fireEvent.click(await screen.findByText("知识缺口"));
  expect(await screen.findByRole("heading", { name: "知识缺口" })).toBeTruthy();
  history.pushState(null, "", "/");
});

test("confirmed gap exposes follow-up actions and pending gap can be reviewed", async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/projects")
      return Response.json([
        {
          id: "default",
          name: "Default",
          type: "document",
          status: "active",
          my_role: "admin",
        },
      ]);
    if (options?.method === "POST") return Response.json({});
    return Response.json([
      {
        id: "g1",
        project_id: "default",
        question: "Q1",
        missing: "M1",
        status: "pending",
        can_review: true,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "g2",
        project_id: "default",
        question: "Q2",
        missing: "M2",
        status: "confirmed",
        can_review: true,
        created_at: "2026-01-01T00:00:00Z",
        reviewed_at: "2026-01-02T00:00:00Z",
      },
    ]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  fireEvent.click(await screen.findByRole("button", { name: "Confirm gap" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, options]) =>
          url === "/api/v1/knowledge-gaps/g1/review?project=default" &&
          options?.body === JSON.stringify({ status: "confirmed" }),
      ),
    ).toBe(true),
  );
  expect(
    screen.getByRole("button", { name: "Propose correction" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Add knowledge" }).getAttribute("href"),
  ).toBe("/source?project=default&gap=g2");
});

test("long gap draft posts bounded description with both contexts while card remains complete", async () => {
  const question = "QUESTION" + "😀".repeat(12000);
  const missing = "MISSING" + "m".repeat(12000);
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/projects")
      return Response.json([
        {
          id: "default",
          name: "Default",
          type: "document",
          status: "active",
          my_role: "admin",
        },
      ]);
    if (url === "/api/v1/corrections/draft") {
      const description = JSON.parse(String(options?.body)).description;
      return Response.json({
        target: "document:metrics/a.md",
        target_kind: "document",
        patch: "new",
        reason: "fix",
        caliber: description,
        gate_report: { passed: true },
      });
    }
    if (options?.method === "POST") {
      const proposal = JSON.parse(String(options.body));
      return Array.from(proposal.caliber as string).length > 5000
        ? Response.json({ detail: "caliber too long" }, { status: 422 })
        : Response.json({ id: "p", status: "pending" }, { status: 201 });
    }
    return Response.json([
      {
        id: "g",
        project_id: "default",
        question,
        missing,
        status: "confirmed",
        can_review: true,
      },
    ]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Propose correction" }),
  );
  expect(screen.getByText(question)).toBeTruthy();
  expect(screen.getByText(missing)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Generate draft" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(([url]) => url === "/api/v1/corrections/draft"),
    ).toBe(true),
  );
  const call = fetcher.mock.calls.find(
    ([url]) => url === "/api/v1/corrections/draft",
  )!;
  const description = JSON.parse(String(call[1]?.body)).description as string;
  expect(Array.from(description).length).toBeLessThanOrEqual(10000);
  expect(description).toContain("QUESTION");
  expect(description).toContain("MISSING");
  await screen.findByText("document:metrics/a.md");
  fireEvent.click(
    screen.getAllByRole("button", { name: "Propose correction" }).at(-1)!,
  );
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) => url === "/api/v1/corrections?project=default",
      ),
    ).toBe(true),
  );
  const proposal = JSON.parse(
    String(
      fetcher.mock.calls.find(
        ([url]) => url === "/api/v1/corrections?project=default",
      )![1]?.body,
    ),
  );
  expect(Array.from(proposal.caliber).length).toBeLessThanOrEqual(5000);
  expect(proposal.caliber).toContain("QUESTION");
  expect(proposal.caliber).toContain("MISSING");
  expect(proposal.knowledge_gap_id).toBe("g");
});

test("review conflict refreshes current project and shows conflict", async () => {
  let count = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "default",
            name: "Default",
            type: "document",
            status: "active",
            my_role: "admin",
          },
        ]);
      if (options?.method === "POST")
        return Response.json({ detail: "conflict" }, { status: 409 });
      count++;
      return Response.json([
        {
          id: "g",
          project_id: "default",
          question: "Q",
          missing: "M",
          status: count > 1 ? "rejected" : "pending",
          can_review: true,
        },
      ]);
    }),
  );
  render(<CorrectionsPage token="token" gaps messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "确认缺口" }));
  expect(await screen.findByText(/已被其他人审核/)).toBeTruthy();
  expect(screen.getByText("不接纳")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "确认缺口" })).toBeNull();
});

test("late conflict after switching projects cannot refresh or alert the new project", async () => {
  let resolveReview!: (response: Response) => void;
  const review = new Promise<Response>((resolve) => {
    resolveReview = resolve;
  });
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/v1/projects")
      return Response.json([
        {
          id: "default",
          name: "Default",
          type: "document",
          status: "active",
          my_role: "admin",
        },
        {
          id: "other",
          name: "Other",
          type: "document",
          status: "active",
          my_role: "admin",
        },
      ]);
    if (options?.method === "POST") return review;
    const project = url.includes("project=other") ? "other" : "default";
    return Response.json([
      {
        id: project,
        project_id: project,
        question: project,
        missing: "missing",
        status: "pending",
        can_review: true,
      },
    ]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  fireEvent.click(await screen.findByRole("button", { name: "Confirm gap" }));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "Projects" }));
  fireEvent.click(await screen.findByTitle("Other"));
  expect(await screen.findByText("other")).toBeTruthy();
  await act(async () => resolveReview(Response.json({}, { status: 409 })));
  expect(screen.queryByText(/already reviewed/)).toBeNull();
  expect(
    fetcher.mock.calls.filter(
      ([url]) => url === "/api/v1/knowledge-gaps?project=other",
    ),
  ).toHaveLength(1);
});

test("late gaps from a previous project do not reappear after switching", async () => {
  let resolveOld!: (value: Response) => void;
  const old = new Promise<Response>((resolve) => {
    resolveOld = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects")
        return Response.json([
          {
            id: "default",
            name: "Default",
            type: "document",
            status: "active",
            my_role: "admin",
          },
          {
            id: "other",
            name: "Other",
            type: "document",
            status: "active",
            my_role: "admin",
          },
        ]);
      if (url === "/api/v1/knowledge-gaps?project=default") return old;
      return Response.json([
        {
          id: "new",
          project_id: "other",
          question: "New question",
          missing: "New",
          status: "pending",
          can_review: true,
        },
      ]);
    }),
  );
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  fireEvent.mouseDown(
    await screen.findByRole("combobox", { name: "Projects" }),
  );
  fireEvent.click(await screen.findByTitle("Other"));
  expect(await screen.findByText("New question")).toBeTruthy();
  await act(async () => {
    resolveOld(
      Response.json([
        {
          id: "old",
          project_id: "default",
          question: "Old question",
          missing: "Old",
          status: "pending",
          can_review: true,
        },
      ]),
    );
  });
  await waitFor(() => expect(screen.queryByText("Old question")).toBeNull());
  expect(screen.getByText("New question")).toBeTruthy();
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
}, 15000);

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

test("filters by discovery source and renders structured discovery_evidence verification card", async () => {
  const calls: { url: string; method?: string }[] = [];
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
      if (url === "/api/v1/corrections?project=default") {
        return Response.json([
          {
            id: "prop-manual",
            target: "document:guide.md",
            reason: "人工修正",
            diff: "-old\n+new",
            patch: "new",
            status: "pending",
            source: "manual",
            target_kind: "document",
          },
          {
            id: "prop-disc",
            target: "relationship:shop.orders:shop.users:fk_orders_users",
            reason: "冷启动知识发现探活核验通过的外键关系",
            diff: "--- a\n+++ b\n+fk_orders_users",
            patch: "version: 1",
            status: "pending",
            source: "discovery",
            target_kind: "override",
            gate_report: {
              passed: true,
              checks: [{ name: "yaml_lint", passed: true, details: [] }],
              errors: [],
              discovery_evidence: {
                run_id: "drun-42",
                candidate_kind: "relationship",
                query_history_frequency: 25,
                distinct_callers: 6,
                sample_distinct_count: 1000,
                matched_count: 925,
                overlap_ratio: 0.925,
                join_matched: true,
                probing_status: "passed",
                probe_partitions: {
                  "shop.orders": { dt: "2026-05-01" },
                  "shop.users": { dt: "2026-05-01" },
                },
              },
            },
          },
        ]);
      }
      return Response.json({ id: "prop-disc", status: "approved" });
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("zh")} />);

  expect(
    await screen.findByRole("button", { name: /fk_orders_users/ }),
  ).toBeTruthy();

  // Filter by 历史发现 (discovery)
  fireEvent.click(screen.getByRole("button", { name: "历史发现" }));
  expect(screen.getByRole("button", { name: /fk_orders_users/ })).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: /document:guide.md/ }),
  ).toBeNull();

  // Open detail and verify structured discovery_evidence card
  fireEvent.click(screen.getByRole("button", { name: /fk_orders_users/ }));
  expect(screen.getByText("历史发现核验证据")).toBeTruthy();
  expect(screen.getByText(/drun-42/)).toBeTruthy();
  expect(screen.getByText(/历史查询频次: 25/)).toBeTruthy();
  expect(screen.getByText(/去重调用方: 6/)).toBeTruthy();
  expect(screen.getByText(/采样去重数: 1000/)).toBeTruthy();
  expect(screen.getByText(/命中父表数: 925/)).toBeTruthy();
  expect(screen.getByText(/包含度: 92\.5%/)).toBeTruthy();
  expect(screen.getByText(/探活 JOIN 状态: 命中通过/)).toBeTruthy();
  expect(
    screen.getByText(
      /shop\.orders\(dt=2026-05-01\) · shop\.users\(dt=2026-05-01\)/,
    ),
  ).toBeTruthy();

  // Approve button remains functional for discovery proposals
  fireEvent.click(screen.getByRole("button", { name: "批准" }));
  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/corrections/prop-disc/approve?project=default" &&
          c.method === "POST",
      ),
    ).toBe(true),
  );
});

test("renders git-style before/after diff viewer for Chinese markdown proposals and supports mode switch", async () => {
  const diff = [
    "--- knowledge/business-scenarios/air/README.md",
    "+++ knowledge/business-scenarios/air/README.md",
    "@@ -58,3 +58,3 @@",
    " 针对大盘航司同时分析验价、预订及转化时：",
    "-按验价量降序并明确设置足额的返回行数。",
    "+按验价量降序；该参考案例显式使用 `LIMIT 2000`。",
  ].join("\n");

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
          id: "prop-md",
          target: "document:business-scenarios/air/README.md",
          reason: "补充 LIMIT 2000 要求",
          diff,
          patch: "updated",
          status: "approved",
          source: "manual",
          target_kind: "document",
        },
      ]);
    }),
  );

  const { container } = render(
    <CorrectionsPage token="token" messages={getMessages("zh")} />,
  );

  fireEvent.click(
    await screen.findByRole("button", {
      name: /document:business-scenarios\/air\/README\.md/,
    }),
  );

  expect(screen.getByText("修改前")).toBeTruthy();
  expect(screen.getByText("修改后")).toBeTruthy();
  expect(screen.getByText("+1")).toBeTruthy();
  expect(screen.getByText("-1")).toBeTruthy();
  expect(screen.getByText("第 58 行 → 第 58 行")).toBeTruthy();
  expect(container.textContent).not.toContain("--- knowledge/");
  expect(container.textContent).not.toContain("+++ knowledge/");
  expect(container.textContent).not.toContain("@@ -58,3 +58,3 @@");
  expect(
    container.querySelectorAll("mark.diff-mark-add").length,
  ).toBeGreaterThan(0);

  fireEvent.click(screen.getByRole("button", { name: "行内对比" }));
  expect(
    container.querySelector(".diff-viewer")?.getAttribute("data-diff-mode"),
  ).toBe("unified");
  expect(screen.getByText("变更内容")).toBeTruthy();
});

test("renders unified evidence card across manual, evolution, and discovery proposals plus new-file /dev/null diff", async () => {
  const newDocDiff = [
    "--- /dev/null",
    "+++ b/knowledge/metrics/refund_caliber.md",
    "@@ -0,0 +1,2 @@",
    "+# 退款率口径",
    "+仅统计已完成退款的订单。",
  ].join("\n");

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
          id: "prop-new-doc",
          target: "document:knowledge/metrics/refund_caliber.md",
          reason: "新建退款率专题口径文档",
          diff: newDocDiff,
          patch: "# 退款率口径\n仅统计已完成退款的订单。\n",
          status: "pending",
          source: "evolution",
          target_kind: "document",
          is_new_file: true,
          evidence: {
            source_type: "evolution",
            summary_reason: "近 3 条对话中用户反复追问退款率口径定义",
            trigger_evidence: {
              evolution_run_id: "evo-run-9",
              trace_source_plugin: "langfuse",
              trace_ids: ["tr-101", "tr-102"],
              window_since: "2026-06-01T00:00:00Z",
              window_until: "2026-06-01T12:00:00Z",
              conversation_count: 2,
              message_summary: [
                {
                  trace_id: "tr-101",
                  started_at: "2026-06-01T01:00:00Z",
                  messages: [
                    { role: "user", content: "退款率怎么算？" },
                    { role: "assistant", content: "仅统计已完成退款的订单" },
                  ],
                },
              ],
            },
          },
        },
        {
          id: "prop-disc-clusters",
          target: "table:shop.orders",
          reason: "高频过滤默认条件归纳",
          diff: "--- overrides/semantic_overrides.yaml\n+++ overrides/semantic_overrides.yaml\n+instructions: 默认过滤测试单",
          patch: "version: 1",
          status: "pending",
          source: "discovery",
          target_kind: "override",
          is_new_file: false,
          evidence: {
            source_type: "discovery",
            summary_reason: "历史查询模板普遍带 is_test = 0 过滤",
            trigger_evidence: {
              run_id: "drun-88",
              candidate_kind: "table",
              query_history_frequency: 42,
              distinct_callers: 9,
              sql_clusters: [
                {
                  fingerprint: "fp-1",
                  template_sql:
                    "SELECT COUNT(*) FROM shop.orders WHERE is_test = ?",
                  tables: ["shop.orders"],
                  execution_count: 42,
                  distinct_callers: 9,
                  dialect: "doris",
                },
              ],
              fk_probe_evidence: [
                {
                  from_table: "shop.orders",
                  from_columns: ["user_id"],
                  to_table: "shop.users",
                  to_columns: ["id"],
                  sample_distinct_count: 200,
                  matched_count: 196,
                  overlap_ratio: 0.98,
                },
              ],
            },
          },
        },
      ]);
    }),
  );

  render(<CorrectionsPage token="token" messages={getMessages("zh")} />);

  const newDocBtn = await screen.findByRole("button", {
    name: /refund_caliber\.md/,
  });
  expect(newDocBtn.textContent).toContain("新建文档");
  fireEvent.click(newDocBtn);

  expect(screen.getByText("触发证据与归纳依据")).toBeTruthy();
  expect(
    screen.getByText("近 3 条对话中用户反复追问退款率口径定义"),
  ).toBeTruthy();
  expect(screen.getByText(/tr-101, tr-102/)).toBeTruthy();
  expect(screen.getByText(/退款率怎么算？/)).toBeTruthy();
  expect(screen.getByText("knowledge/metrics/refund_caliber.md")).toBeTruthy();
  expect(screen.getByText("# 退款率口径")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /table:shop\.orders/ }));
  expect(screen.getByText("历史查询模板普遍带 is_test = 0 过滤")).toBeTruthy();
  expect(
    screen.getByText("SELECT COUNT(*) FROM shop.orders WHERE is_test = ?"),
  ).toBeTruthy();
  expect(screen.getByText(/外键探活聚合证据/)).toBeTruthy();
  expect(screen.getByText(/98\.0%/)).toBeTruthy();
});
