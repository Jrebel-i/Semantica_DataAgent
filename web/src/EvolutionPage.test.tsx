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

test("renders config, saves updates, triggers manual run, shows rounds/L1/L2 and links to proposal", async () => {
  let config = {
    auto_evolve_enabled: false,
    min_trajectories: 30,
    min_interval_hours: 24,
    max_idle_days: 7,
    max_rounds: 3,
    max_eval_cases: 20,
    evolution_provider_id: null as string | null,
    evolution_model: null as string | null,
    judge_provider_id: null as string | null,
    judge_model: null as string | null,
    checkpoint_trajectory_id: 12,
    checkpoint_at: "2026-01-01T00:00:00Z",
    pending_trajectories: 8,
  };
  const runs = [
    {
      id: "run-1",
      project_id: "default",
      status: "accepted" as const,
      trigger_type: "manual",
      created_by: "admin",
      checkpoint_before_trajectory_id: 1,
      checkpoint_after_trajectory_id: 12,
      frozen_max_trajectory_id: 12,
      diagnosis_trajectory_ids: [2, 3, 4],
      reserve_trajectory_ids: [5, 6],
      max_rounds: 3,
      max_eval_cases: 20,
      rounds_completed: 2,
      proposal_id: "prop-88",
      stop_reason: "accepted",
      error: null,
      signal_summary: {
        clusters: { search_detour: 2, sql_self_repair: 1 },
      },
      created_at: "2026-01-01T01:00:00Z",
      started_at: "2026-01-01T01:00:00Z",
      finished_at: "2026-01-01T01:02:00Z",
      rounds: [
        {
          id: "round-1",
          run_id: "run-1",
          project_id: "default",
          round_number: 1,
          signal_kind: "search_detour",
          target_kind: "override",
          target: "orders.gmv",
          hypothesis: "补充 GMV 别名映射减少检索绕路",
          candidate_change: {},
          evidence_trajectory_ids: [2, 3],
          l1_gate_report: {
            passed: false,
            checks: [{ name: "yaml_lint", passed: false, details: ["syntax"] }],
            errors: ["L1 静态校验失败"],
          },
          l2_gate_report: null,
          decision: "rejected",
          reject_reason: "L1 failed",
          proposal_id: null,
          started_at: "2026-01-01T01:00:05Z",
          finished_at: "2026-01-01T01:00:20Z",
        },
        {
          id: "round-2",
          run_id: "run-1",
          project_id: "default",
          round_number: 2,
          signal_kind: "sql_self_repair",
          target_kind: "override",
          target: "orders.status",
          hypothesis: "明确已支付订单状态编码",
          candidate_change: {},
          evidence_trajectory_ids: [4],
          l1_gate_report: {
            passed: true,
            checks: [{ name: "yaml_lint", passed: true, details: [] }],
            errors: [],
          },
          l2_gate_report: {
            passed: true,
            mode: "cold_start_ab",
            evaluated_cases: 5,
            W_c: 4,
            W_p: 0,
            E_c: 0,
            new_validate_errors: 0,
          },
          decision: "accepted",
          reject_reason: null,
          proposal_id: "prop-88",
          started_at: "2026-01-01T01:00:30Z",
          finished_at: "2026-01-01T01:01:55Z",
        },
      ],
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
      if (
        input === "/api/v1/projects/default/evolution-config" &&
        method === "GET"
      ) {
        return Response.json(config);
      }
      if (
        input === "/api/v1/projects/default/evolution-config" &&
        method === "PUT"
      ) {
        config = { ...config, ...(body as Partial<typeof config>) };
        return Response.json(config);
      }
      if (
        input === "/api/v1/evolution/runs?project=default" &&
        method === "GET"
      ) {
        return Response.json(runs);
      }
      if (
        input === "/api/v1/evolution/runs?project=default" &&
        method === "POST"
      ) {
        if (failNextTrigger) {
          return Response.json(
            { detail: "An evolution run is already running" },
            { status: 409 },
          );
        }
        const created = {
          ...runs[0],
          id: "run-2",
          status: "incomplete" as const,
          proposal_id: null,
          stop_reason: "no_signals",
          rounds_completed: 0,
          rounds: [],
        };
        runs.unshift(created as unknown as (typeof runs)[number]);
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

  // Config form shows defaults & run round details
  expect(await screen.findByText("明确已支付订单状态编码")).toBeTruthy();
  expect(screen.getByText(/W_c=4/)).toBeTruthy();
  expect(screen.getByText(/L1 静态校验失败/)).toBeTruthy();

  // Navigate to generated proposal
  fireEvent.click(screen.getByRole("button", { name: "查看产出纠错提案" }));
  expect(onNavigateCorrections).toHaveBeenCalledWith("prop-88");

  // Update config: enable auto-evolution, adjust min_trajectories, select provider & model
  const autoCheckbox = screen.getByLabelText(
    "启用自动演化",
  ) as HTMLInputElement;
  expect(autoCheckbox.checked).toBe(false);
  fireEvent.click(autoCheckbox);
  fireEvent.change(screen.getByLabelText("最小新增问答轨迹数"), {
    target: { value: "50" },
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
          r.url === "/api/v1/projects/default/evolution-config" &&
          r.method === "PUT" &&
          (r.body as Record<string, unknown>)?.auto_evolve_enabled === true &&
          (r.body as Record<string, unknown>)?.min_trajectories === 50 &&
          (r.body as Record<string, unknown>)?.evolution_provider_id ===
            "prov-1" &&
          (r.body as Record<string, unknown>)?.evolution_model === "gpt-4.1",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("演化配置已保存")).toBeTruthy();

  // Manual trigger works even when auto_evolve_enabled was false / after save
  const triggerBtn = screen.getByRole("button", { name: /立即演化/ });
  fireEvent.click(triggerBtn);
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/api/v1/evolution/runs?project=default" &&
          r.method === "POST",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("run-2")).toBeTruthy();

  // Conflict 409 displays error alert (wait for triggering state to finish)
  failNextTrigger = true;
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: /立即演化/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: /立即演化/ }));
  expect(
    await screen.findByText("An evolution run is already running"),
  ).toBeTruthy();
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
      if (input === "/api/v1/projects/default/evolution-config") {
        return defaultConfigPromise;
      }
      if (input === "/api/v1/evolution/runs?project=default") {
        return Response.json([]);
      }
      if (input === "/api/v1/projects/alpha/evolution-config") {
        return Response.json({
          auto_evolve_enabled: true,
          min_trajectories: 42,
          min_interval_hours: 12,
          max_idle_days: 3,
          max_rounds: 2,
          max_eval_cases: 15,
          evolution_provider_id: null,
          evolution_model: null,
          judge_provider_id: null,
          judge_model: null,
          checkpoint_trajectory_id: null,
          checkpoint_at: null,
          pending_trajectories: 5,
        });
      }
      if (input === "/api/v1/evolution/runs?project=alpha") {
        return Response.json([
          {
            id: "alpha-run",
            project_id: "alpha",
            status: "accepted",
            trigger_type: "scheduled",
            created_by: null,
            checkpoint_before_trajectory_id: null,
            checkpoint_after_trajectory_id: 5,
            frozen_max_trajectory_id: 5,
            diagnosis_trajectory_ids: [1, 2],
            reserve_trajectory_ids: [3],
            max_rounds: 2,
            max_eval_cases: 15,
            rounds_completed: 1,
            proposal_id: null,
            stop_reason: "accepted",
            error: null,
            signal_summary: {},
            created_at: "2026-01-02T00:00:00Z",
            started_at: "2026-01-02T00:00:00Z",
            finished_at: "2026-01-02T00:01:00Z",
            rounds: [
              {
                id: "ar-1",
                run_id: "alpha-run",
                project_id: "alpha",
                round_number: 1,
                signal_kind: "knowledge_gap_or_failure",
                target_kind: "knowledge",
                target: "metrics.md",
                hypothesis: "Alpha 专属口径假设",
                candidate_change: {},
                evidence_trajectory_ids: [1],
                l1_gate_report: { passed: true, checks: [], errors: [] },
                l2_gate_report: {
                  passed: true,
                  mode: "confirmed_regression",
                  evaluated_cases: 3,
                  regressions: [],
                },
                decision: "accepted",
                reject_reason: null,
                proposal_id: null,
                started_at: "2026-01-02T00:00:01Z",
                finished_at: "2026-01-02T00:00:50Z",
              },
              {
                id: "ar-2",
                run_id: "alpha-run",
                project_id: "alpha",
                round_number: 2,
                signal_kind: "sql_self_repair",
                target_kind: "override",
                target: "orders.dt",
                hypothesis: "回归列表渲染校验",
                candidate_change: {},
                evidence_trajectory_ids: [2],
                l1_gate_report: { passed: true, checks: [], errors: [] },
                l2_gate_report: {
                  passed: false,
                  mode: "confirmed_regression",
                  evaluated_cases: 3,
                  regressions: ["q-3", "q-4"],
                },
                decision: "rejected",
                reject_reason: "L2 regressions",
                proposal_id: null,
                started_at: "2026-01-02T00:00:51Z",
                finished_at: "2026-01-02T00:00:59Z",
              },
            ],
          },
        ]);
      }
      return Response.json({});
    }),
  );

  render(<EvolutionPage token="token" messages={getMessages("zh")} />);

  // Switch project to alpha before default config resolves
  const projectSelect = await screen.findByLabelText("项目");
  fireEvent.mouseDown(projectSelect);
  fireEvent.click(await screen.findByTitle("项目 Alpha"));

  expect(await screen.findByText("Alpha 专属口径假设")).toBeTruthy();
  expect(screen.getByText(/regressions=0/)).toBeTruthy();
  expect(screen.getByText(/regressions=2 \(q-3, q-4\)/)).toBeTruthy();
  const minInput = screen.getByLabelText(
    "最小新增问答轨迹数",
  ) as HTMLInputElement;
  expect(minInput.value).toBe("42");

  // Resolve stale default config response AFTER alpha is loaded
  resolveDefaultConfig(
    Response.json({
      auto_evolve_enabled: false,
      min_trajectories: 99,
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
      pending_trajectories: 99,
    }),
  );

  // Give microtasks a chance to settle; stale 99 must NEVER overwrite alpha's 42
  await waitFor(() => {
    expect(
      (screen.getByLabelText("最小新增问答轨迹数") as HTMLInputElement).value,
    ).toBe("42");
  });
});
