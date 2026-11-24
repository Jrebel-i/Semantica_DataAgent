// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
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

const traceSourcePlugins = [
  {
    id: "langfuse",
    version: "1.0.0",
    display_name: "Langfuse",
    secret_parameter_keys: ["secret_key"],
    parameters_schema: {
      type: "object",
      properties: {
        base_url: {
          type: "string",
          format: "uri",
          title: "Langfuse Base URL",
        },
        public_key: {
          type: "string",
          title: "Public Key",
        },
        secret_key: {
          type: "string",
          format: "password",
          title: "Secret Key",
        },
      },
      required: ["base_url", "public_key", "secret_key"],
    },
  },
];

test("configures TraceSource plugin with masked secrets, Cron schedule, watermark, manual window trigger, and proposal link", async () => {
  let config = {
    project_id: "default",
    enabled: false,
    trace_source_plugin: "langfuse" as string | null,
    trace_source_parameters: {
      base_url: "https://langfuse.example.com",
      public_key: "pk-lf-1",
    },
    trace_source_secrets: {
      secret_key: "***",
    },
    configured_secrets: ["secret_key"],
    secret_configured: true,
    schedule_cron: "02:30" as string | null,
    timezone: "Asia/Shanghai",
    evolution_provider_id: null as string | null,
    evolution_model: null as string | null,
    max_traces_per_run: 200,
    last_trace_timestamp: "2026-06-01T02:00:00Z",
    last_triggered_at: "2026-06-01T02:30:00Z",
    next_run_at: "2026-06-02T02:30:00Z",
    global_langfuse_configured: false,
  };
  const runs = [
    {
      id: "run-1",
      project_id: "default",
      trigger_type: "manual",
      status: "succeeded" as const,
      trace_source_plugin: "langfuse",
      traces_fetched: 12,
      proposals_created: 1,
      duplicates_skipped: 2,
      l1_rejected: 0,
      proposal_ids: ["prop-88"] as string[],
      proposal_id: "prop-88" as string | null,
      window_since: "2026-06-01T00:00:00Z",
      window_until: "2026-06-01T02:00:00Z",
      summary:
        "已分析 12 条对话追踪记录，生成 1 条待审批纠错提案，跳过 2 条重复提案。",
      error: null,
      created_by: "admin",
      started_at: "2026-06-01T02:01:00Z",
      finished_at: "2026-06-01T02:02:00Z",
      created_at: "2026-06-01T02:01:00Z",
    },
  ];

  const requests: { url: string; method: string; body?: unknown }[] = [];
  let failNextTrigger = false;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const method = options?.method ?? "GET";
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      requests.push({ url: input, method, body });

      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json(providers);
      if (input === "/api/v1/trace-source-plugins") {
        return Response.json(traceSourcePlugins);
      }
      if (
        input === "/api/v1/projects/default/evolution/config" &&
        method === "GET"
      ) {
        return Response.json(config);
      }
      if (
        input === "/api/v1/projects/default/evolution/config" &&
        method === "PUT"
      ) {
        config = {
          ...config,
          ...(body as Partial<typeof config>),
          trace_source_secrets: { secret_key: "***" },
          configured_secrets: ["secret_key"],
        };
        return Response.json(config);
      }
      if (
        input === "/api/v1/projects/default/evolution/runs" &&
        method === "GET"
      ) {
        return Response.json(runs);
      }
      if (
        input === "/api/v1/projects/default/evolution/runs" &&
        method === "POST"
      ) {
        if (failNextTrigger) {
          return Response.json(
            { detail: "该项目已有正在进行的演化运行" },
            { status: 409 },
          );
        }
        const created = {
          ...runs[0],
          id: "run-2",
          status: "succeeded" as const,
          traces_fetched: 0,
          proposals_created: 0,
          duplicates_skipped: 0,
          proposal_ids: [],
          proposal_id: null,
          summary: "时间窗内未获取到新对话追踪记录，无需生成纠错提案。",
        };
        runs.unshift(created);
        return Response.json(created, { status: 202 });
      }
      return Response.json({});
    }),
  );

  const onNavigateCorrections = vi.fn();
  render(
    <EvolutionPage
      token="token"
      messages={getMessages("zh")}
      onNavigateCorrections={onNavigateCorrections}
    />,
  );

  expect(await screen.findByText(/已分析 12 条对话追踪记录/)).toBeTruthy();
  expect(screen.getByText(/拉取 Trace: 12/)).toBeTruthy();
  expect(screen.getAllByText(/生成提案: 1/).length).toBeGreaterThan(0);
  expect(screen.getByText(/跳过重复: 2/)).toBeTruthy();
  expect(screen.getByText(/水位线时间:/)).toBeTruthy();
  expect(screen.getByText(/下次定时运行:/)).toBeTruthy();

  // Masked secret preserves "***"
  const secretInput = screen.getByLabelText("Secret Key *") as HTMLInputElement;
  expect(secretInput.value).toBe("***");

  // Navigate to generated proposal
  fireEvent.click(screen.getByRole("button", { name: "查看产出纠错提案" }));
  expect(onNavigateCorrections).toHaveBeenCalledWith("prop-88");

  // Update schedule, max_traces_per_run, provider/model, and save
  const autoCheckbox = screen.getByLabelText(
    "启用定时演化",
  ) as HTMLInputElement;
  expect(autoCheckbox.checked).toBe(false);
  fireEvent.click(autoCheckbox);
  fireEvent.change(screen.getByLabelText("定时规则"), {
    target: { value: "03:00" },
  });
  fireEvent.change(screen.getByLabelText("单次最大拉取 Trace 数"), {
    target: { value: "100" },
  });
  fireEvent.change(screen.getByLabelText("演化模型供应方"), {
    target: { value: "prov-1" },
  });
  fireEvent.change(screen.getByLabelText("演化模型"), {
    target: { value: "gpt-4.1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存演化配置" }));

  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/api/v1/projects/default/evolution/config" &&
          r.method === "PUT" &&
          (r.body as Record<string, unknown>)?.enabled === true &&
          (r.body as Record<string, unknown>)?.schedule_cron === "03:00" &&
          (r.body as Record<string, unknown>)?.max_traces_per_run === 100 &&
          (r.body as Record<string, unknown>)?.evolution_provider_id ===
            "prov-1" &&
          (r.body as Record<string, unknown>)?.evolution_model === "gpt-4.1" &&
          (r.body as Record<string, Record<string, unknown>>)
            ?.trace_source_secrets?.secret_key === "***",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("演化配置已保存")).toBeTruthy();

  // Manual trigger with optional time window & max_traces
  fireEvent.change(screen.getByLabelText("开始时间 (since)"), {
    target: { value: "2026-06-01T00:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间 (until)"), {
    target: { value: "2026-06-01T06:00" },
  });
  fireEvent.change(screen.getByLabelText("本次最大 Trace 数（可选）"), {
    target: { value: "50" },
  });
  fireEvent.click(screen.getByRole("button", { name: /立即演化/ }));

  await waitFor(() =>
    expect(
      requests.some((r) => {
        if (
          r.url !== "/api/v1/projects/default/evolution/runs" ||
          r.method !== "POST"
        ) {
          return false;
        }
        const b = r.body as Record<string, unknown>;
        return (
          typeof b.since === "string" &&
          typeof b.until === "string" &&
          b.max_traces === 50
        );
      }),
    ).toBe(true),
  );
  expect(await screen.findByText("run-2")).toBeTruthy();

  // Conflict 409 displays error alert
  failNextTrigger = true;
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: /立即演化/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: /立即演化/ }));
  expect(await screen.findByText("该项目已有正在进行的演化运行")).toBeTruthy();
});

test("project switch discards stale delayed fetch response from previous project", async () => {
  let resolveDefaultConfig!: (res: Response) => void;
  const defaultConfigPromise = new Promise<Response>((resolve) => {
    resolveDefaultConfig = resolve;
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/projects") return Response.json(projects);
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/trace-source-plugins") {
        return Response.json(traceSourcePlugins);
      }
      if (input === "/api/v1/projects/default/evolution/config") {
        return defaultConfigPromise;
      }
      if (input === "/api/v1/projects/default/evolution/runs") {
        return Response.json([]);
      }
      if (input === "/api/v1/projects/alpha/evolution/config") {
        return Response.json({
          project_id: "alpha",
          enabled: true,
          trace_source_plugin: "langfuse",
          trace_source_parameters: {},
          trace_source_secrets: {},
          configured_secrets: [],
          secret_configured: false,
          schedule_cron: "04:00",
          timezone: "UTC",
          evolution_provider_id: null,
          evolution_model: null,
          max_traces_per_run: 42,
          last_trace_timestamp: null,
          last_triggered_at: null,
          next_run_at: null,
          global_langfuse_configured: false,
        });
      }
      if (input === "/api/v1/projects/alpha/evolution/runs") {
        return Response.json([
          {
            id: "alpha-run",
            project_id: "alpha",
            trigger_type: "scheduler",
            status: "succeeded",
            trace_source_plugin: "langfuse",
            traces_fetched: 5,
            proposals_created: 1,
            duplicates_skipped: 0,
            l1_rejected: 0,
            proposal_ids: [],
            proposal_id: null,
            window_since: null,
            window_until: null,
            summary: "Alpha 专属演化摘要",
            error: null,
            created_by: "scheduler",
            started_at: "2026-01-02T00:00:00Z",
            finished_at: "2026-01-02T00:01:00Z",
            created_at: "2026-01-02T00:00:00Z",
          },
        ]);
      }
      return Response.json({});
    }),
  );

  render(<EvolutionPage token="token" messages={getMessages("zh")} />);

  const projectSelect = await screen.findByLabelText("项目");
  fireEvent.mouseDown(projectSelect);
  fireEvent.click(await screen.findByTitle("项目 Alpha"));

  expect(await screen.findByText("Alpha 专属演化摘要")).toBeTruthy();
  const maxInput = screen.getByLabelText(
    "单次最大拉取 Trace 数",
  ) as HTMLInputElement;
  expect(maxInput.value).toBe("42");

  resolveDefaultConfig(
    Response.json({
      project_id: "default",
      enabled: false,
      trace_source_plugin: null,
      trace_source_parameters: {},
      trace_source_secrets: {},
      configured_secrets: [],
      secret_configured: false,
      schedule_cron: null,
      timezone: "Asia/Shanghai",
      evolution_provider_id: null,
      evolution_model: null,
      max_traces_per_run: 99,
      last_trace_timestamp: null,
      last_triggered_at: null,
      next_run_at: null,
      global_langfuse_configured: false,
    }),
  );

  await waitFor(() => {
    expect(
      (screen.getByLabelText("单次最大拉取 Trace 数") as HTMLInputElement)
        .value,
    ).toBe("42");
  });
});
