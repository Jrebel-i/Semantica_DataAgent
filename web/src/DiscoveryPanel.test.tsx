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
import { EvolutionPage } from "./EvolutionPage";
import { getMessages } from "./i18n";

beforeEach(() => {
  localStorage.clear();
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

const presetsPayload = {
  doris_audit_log: {
    preset: "doris_audit_log",
    source_table: "__internal_schema.audit_log",
    sql_column: "stmt",
    status_column: "state",
    time_column: "time",
    database_column: "db",
    caller_column: "user",
    dialect_column: null,
    success_statuses: ["EOF", "OK"],
  },
  dtb_tracking_table: {
    preset: "dtb_tracking_table",
    source_table: "mysql.data_tool_box_emr.query_history",
    sql_column: "query_sql",
    status_column: "status",
    time_column: "create_time",
    database_column: "database_name",
    caller_column: "user_name",
    dialect_column: "engine_type",
    success_statuses: ["SUCCESS", "SUCCEEDED", "FINISHED"],
  },
};

const baseConfig = {
  ...presetsPayload.doris_audit_log,
  lookback_days: 7,
  max_queries: 5000,
  min_frequency: 3,
  max_fk_candidates: 20,
  sample_limit: 1000,
  min_inclusion_ratio: 0.8,
  probe_timeout_seconds: 30,
  probe_concurrency: 2,
  provider_id: null as string | null,
  model: null as string | null,
  presets: presetsPayload,
};

const projects = [
  {
    id: "default",
    name: "业务数据",
    type: "warehouse",
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
  {
    id: "alpha",
    name: "项目 Alpha",
    type: "warehouse",
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
  {
    id: "docs",
    name: "平台文档",
    type: "document",
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
  {
    id: "oldwh",
    name: "归档数仓",
    type: "warehouse",
    status: "archived",
    created_at: "2026-01-01T00:00:00Z",
    my_role: "admin",
  },
];

const providers = [
  {
    id: "prov-1",
    name: "OpenAI",
    type: "openai",
    base_url: "https://api.example.com",
    models: ["gpt-4.1", "gpt-4.1-mini"],
    model_capabilities: {},
    has_api_key: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

test("switches presets, saves config, triggers discovery run with seed_eval_drafts off by default, and renders candidate probing breakdown + proposal link", async () => {
  let savedConfig = { ...baseConfig };
  const runs = [
    {
      id: "drun-1",
      project_id: "default",
      status: "partial",
      mine_query_history: true,
      probe_relationships: true,
      seed_eval_drafts: false,
      config_snapshot: {},
      queries_fetched: 120,
      templates_clustered: 14,
      candidates_recalled: 5,
      candidates_probed: 3,
      candidates_passed: 1,
      candidates_skipped: 1,
      candidates_failed: 1,
      proposals_created: 1,
      eval_drafts_created: 0,
      error: "探活超时：1 个候选跳过" as string | null,
      created_by: "admin",
      started_at: "2026-05-01T01:00:00Z",
      finished_at: "2026-05-01T01:02:00Z",
      created_at: "2026-05-01T01:00:00Z",
    },
  ];
  const runDetails: Record<string, unknown> = {
    "drun-1": {
      ...runs[0],
      candidates: [
        {
          id: "cand-1",
          run_id: "drun-1",
          project_id: "default",
          candidate_kind: "relationship",
          target: "relationship:shop.orders:shop.users:fk_orders_users",
          evidence: {
            query_history_frequency: 18,
            distinct_callers: 5,
            sample_distinct_count: 500,
            matched_count: 470,
            overlap_ratio: 0.94,
            join_matched: true,
            probing_status: "passed",
            probe_partitions: {
              "shop.orders": { dt: "2026-05-01" },
            },
          },
          decision: "proposed",
          reason: null,
          proposal_id: "prop-disc-99",
          eval_question_id: null,
          created_at: "2026-05-01T01:01:00Z",
        },
        {
          id: "cand-2",
          run_id: "drun-1",
          project_id: "default",
          candidate_kind: "relationship",
          target: "relationship:shop.orders:shop.ghost:fk_ghost",
          evidence: {
            probing_status: "skipped_missing_partition",
          },
          decision: "skipped",
          reason: "partition_missing_fail_closed",
          proposal_id: null,
          eval_question_id: null,
          created_at: "2026-05-01T01:01:20Z",
        },
      ],
    },
  };

  const requests: { url: string; method: string; body?: unknown }[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const method = options?.method ?? "GET";
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      requests.push({ url: input, method, body });

      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json(providers);
      if (input === "/api/v1/me/dtb-token") {
        return Response.json({ configured: true });
      }
      if (
        input === "/api/v1/projects/default/discovery-config" &&
        method === "GET"
      ) {
        return Response.json(savedConfig);
      }
      if (
        input === "/api/v1/projects/default/discovery-config" &&
        method === "PUT"
      ) {
        savedConfig = {
          ...savedConfig,
          ...(body as Partial<typeof savedConfig>),
        };
        return Response.json(savedConfig);
      }
      if (
        input === "/api/v1/discovery/runs?project=default" &&
        method === "GET"
      ) {
        return Response.json(runs);
      }
      if (
        input === "/api/v1/discovery/runs/drun-1?project=default" &&
        method === "GET"
      ) {
        return Response.json(runDetails["drun-1"]);
      }
      if (
        input === "/api/v1/discovery/runs?project=default" &&
        method === "POST"
      ) {
        const created = {
          ...runs[0],
          id: "drun-2",
          status: "completed",
          error: null,
          proposals_created: 0,
        };
        runs.unshift(created);
        runDetails["drun-2"] = { ...created, candidates: [] };
        return Response.json(created, { status: 202 });
      }
      if (
        input === "/api/v1/discovery/runs/drun-2?project=default" &&
        method === "GET"
      ) {
        return Response.json(runDetails["drun-2"]);
      }
      return Response.json({});
    }),
  );

  const onNavigateCorrections = vi.fn();
  render(
    <EvolutionPage
      token="token"
      messages={getMessages("zh")}
      initialTab="discovery"
      onNavigateCorrections={onNavigateCorrections}
    />,
  );

  // Run detail + candidate probing evidence + deep link
  expect(
    await screen.findByText(
      "relationship:shop.orders:shop.users:fk_orders_users",
    ),
  ).toBeTruthy();
  expect(screen.getByText(/94\.0%/)).toBeTruthy();
  expect(screen.getByText(/shop\.orders\(dt=2026-05-01\)/)).toBeTruthy();
  expect(screen.getByText(/partition_missing_fail_closed/)).toBeTruthy();
  expect(screen.getByText("探活超时：1 个候选跳过")).toBeTruthy();

  const proposalLink = screen.getByRole("link", { name: "前往纠错审批" });
  expect(proposalLink.getAttribute("href")).toBe(
    "/corrections?proposal_id=prop-disc-99",
  );
  fireEvent.click(proposalLink);
  expect(onNavigateCorrections).toHaveBeenCalledWith("prop-disc-99");

  // Preset switch auto-populates DTB tracking table defaults
  fireEvent.change(screen.getByLabelText("查询历史预设模板"), {
    target: { value: "dtb_tracking_table" },
  });
  expect((screen.getByLabelText("源表名") as HTMLInputElement).value).toBe(
    "mysql.data_tool_box_emr.query_history",
  );
  expect((screen.getByLabelText("SQL 列") as HTMLInputElement).value).toBe(
    "query_sql",
  );
  expect(
    (screen.getByLabelText("方言列（可选）") as HTMLInputElement).value,
  ).toBe("engine_type");
  expect(
    (screen.getByLabelText("成功状态值列表（逗号分隔）") as HTMLInputElement)
      .value,
  ).toBe("SUCCESS, SUCCEEDED, FINISHED");

  // Custom edit + provider/model + save
  fireEvent.change(screen.getByLabelText("回溯天数"), {
    target: { value: "14" },
  });
  fireEvent.change(screen.getByLabelText("发现模型供应方（可选）"), {
    target: { value: "prov-1" },
  });
  fireEvent.change(screen.getByLabelText("发现模型（可选）"), {
    target: { value: "gpt-4.1-mini" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存发现配置" }));

  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/api/v1/projects/default/discovery-config" &&
          r.method === "PUT" &&
          (r.body as Record<string, unknown>)?.preset ===
            "dtb_tracking_table" &&
          (r.body as Record<string, unknown>)?.lookback_days === 14 &&
          (r.body as Record<string, unknown>)?.provider_id === "prov-1" &&
          (r.body as Record<string, unknown>)?.model === "gpt-4.1-mini",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("冷启动发现配置已保存")).toBeTruthy();

  // seed_eval_drafts checkbox MUST be unchecked by default
  const mineBox = screen.getByLabelText(/查询历史挖掘/) as HTMLInputElement;
  const probeBox = screen.getByLabelText(
    /外键关系数据探活/,
  ) as HTMLInputElement;
  const seedBox = screen.getByLabelText(
    /同步生成待确认评测题集草稿/,
  ) as HTMLInputElement;
  expect(mineBox.checked).toBe(true);
  expect(probeBox.checked).toBe(true);
  expect(seedBox.checked).toBe(false);

  // Trigger run with optional per-run override
  fireEvent.change(screen.getByLabelText("本次覆盖：单次探活候选上限"), {
    target: { value: "8" },
  });
  fireEvent.click(screen.getByRole("button", { name: "触发知识发现运行" }));

  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/api/v1/discovery/runs?project=default" &&
          r.method === "POST" &&
          (r.body as Record<string, unknown>)?.mine_query_history === true &&
          (r.body as Record<string, unknown>)?.probe_relationships === true &&
          (r.body as Record<string, unknown>)?.seed_eval_drafts === false &&
          (r.body as Record<string, unknown>)?.max_fk_candidates === 8,
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("drun-2")).toBeTruthy();
});

test("missing personal DTB token displays alert with /personal-settings link and disables run trigger", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/me/dtb-token") {
        return Response.json({ configured: false });
      }
      if (input === "/api/v1/projects/default/discovery-config") {
        return Response.json(baseConfig);
      }
      if (input === "/api/v1/discovery/runs?project=default") {
        return Response.json([]);
      }
      return Response.json({});
    }),
  );

  const onNavigatePersonalSettings = vi.fn();
  render(
    <EvolutionPage
      token="token"
      messages={getMessages("zh")}
      initialTab="discovery"
      onNavigatePersonalSettings={onNavigatePersonalSettings}
    />,
  );

  expect(await screen.findByText("尚未配置个人 DTB 令牌")).toBeTruthy();
  const settingsLink = screen.getByRole("link", {
    name: "前往个人设置配置 DTB 令牌",
  });
  expect(settingsLink.getAttribute("href")).toBe("/personal-settings");
  fireEvent.click(settingsLink);
  expect(onNavigatePersonalSettings).toHaveBeenCalledTimes(1);

  const triggerBtn = screen.getByRole("button", {
    name: "触发知识发现运行",
  }) as HTMLButtonElement;
  expect(triggerBtn.disabled).toBe(true);
});

test("document projects show unsupported notice and archived warehouse projects are read-only", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/me/dtb-token") {
        return Response.json({ configured: true });
      }
      if (input.endsWith("/discovery-config")) {
        return Response.json(baseConfig);
      }
      if (input.startsWith("/api/v1/discovery/runs")) {
        return Response.json([]);
      }
      return Response.json({});
    }),
  );

  render(
    <EvolutionPage
      token="token"
      messages={getMessages("zh")}
      initialTab="discovery"
    />,
  );

  // Switch to document project
  const projectSelect = await screen.findByLabelText("项目");
  fireEvent.mouseDown(projectSelect);
  fireEvent.click(await screen.findByTitle("平台文档"));

  expect(
    await screen.findByText(/当前为文档型项目，不支持查询历史挖掘与数据探活/),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "保存发现配置" })).toBeNull();

  // Switch to archived warehouse project
  fireEvent.mouseDown(screen.getByLabelText("项目"));
  fireEvent.click(await screen.findByTitle("归档数仓"));

  expect(
    await screen.findByText(/当前为归档项目，已暂停配置修改与知识发现运行触发/),
  ).toBeTruthy();
  expect((screen.getByLabelText("源表名") as HTMLInputElement).disabled).toBe(
    true,
  );
  expect(
    (screen.getByRole("button", { name: "保存发现配置" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "触发知识发现运行",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

test("project switch discards stale delayed discovery config and run responses from previous project", async () => {
  let resolveDefaultConfig!: (res: Response) => void;
  const defaultConfigPromise = new Promise<Response>((resolve) => {
    resolveDefaultConfig = resolve;
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/me/dtb-token") {
        return Response.json({ configured: true });
      }
      if (input === "/api/v1/projects/default/discovery-config") {
        return defaultConfigPromise;
      }
      if (input === "/api/v1/discovery/runs?project=default") {
        return Response.json([]);
      }
      if (input === "/api/v1/projects/alpha/discovery-config") {
        return Response.json({
          ...baseConfig,
          source_table: "alpha_db.alpha_audit_log",
          lookback_days: 21,
        });
      }
      if (input === "/api/v1/discovery/runs?project=alpha") {
        return Response.json([]);
      }
      return Response.json({});
    }),
  );

  render(
    <EvolutionPage
      token="token"
      messages={getMessages("zh")}
      initialTab="discovery"
    />,
  );

  // Switch to alpha before default config resolves
  const projectSelect = await screen.findByLabelText("项目");
  fireEvent.mouseDown(projectSelect);
  fireEvent.click(await screen.findByTitle("项目 Alpha"));

  await waitFor(() =>
    expect((screen.getByLabelText("源表名") as HTMLInputElement).value).toBe(
      "alpha_db.alpha_audit_log",
    ),
  );
  expect((screen.getByLabelText("回溯天数") as HTMLInputElement).value).toBe(
    "21",
  );

  // Resolve stale default response AFTER alpha is loaded and flush async microtasks
  await act(async () => {
    resolveDefaultConfig(
      Response.json({
        ...baseConfig,
        source_table: "stale_default.audit_log",
        lookback_days: 88,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
  });

  expect((screen.getByLabelText("源表名") as HTMLInputElement).value).toBe(
    "alpha_db.alpha_audit_log",
  );
  expect((screen.getByLabelText("回溯天数") as HTMLInputElement).value).toBe(
    "21",
  );
});

test("switching from Self-Evolution tab to Cold-Start Discovery tab loads discovery workspace", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/projects/default/evolution-config") {
        return Response.json({
          auto_evolve_enabled: false,
          min_trajectories: 30,
          min_interval_hours: 24,
          max_idle_days: 7,
          max_rounds: 3,
          max_eval_cases: 20,
          evolution_provider_id: null,
          evolution_model: null,
          judge_provider_id: null,
          judge_model: null,
          checkpoint_trajectory_id: null,
          checkpoint_at: null,
          pending_trajectories: 0,
        });
      }
      if (input === "/api/v1/evolution/runs?project=default") {
        return Response.json([]);
      }
      if (input === "/api/v1/me/dtb-token") {
        return Response.json({ configured: true });
      }
      if (input === "/api/v1/projects/default/discovery-config") {
        return Response.json(baseConfig);
      }
      if (input === "/api/v1/discovery/runs?project=default") {
        return Response.json([]);
      }
      return Response.json({});
    }),
  );

  render(<EvolutionPage token="token" messages={getMessages("zh")} />);

  // Starts on 自主演化
  expect(
    await screen.findByRole("button", { name: "保存演化配置" }),
  ).toBeTruthy();

  // Switch to 冷启动发现 tab
  fireEvent.click(screen.getByRole("tab", { name: "冷启动发现" }));
  expect(
    await screen.findByRole("button", { name: "保存发现配置" }),
  ).toBeTruthy();
  expect(screen.getByText("个人 DTB 令牌已就绪")).toBeTruthy();
});
