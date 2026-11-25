// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AgentSettings } from "./AgentSettings";
import { getMessages } from "./i18n";

const RUNNERS = [
  {
    id: "claude",
    display_name: "Claude",
    npm_package: "@anthropic-ai/claude-code",
    package_version: "2.1.318",
    executable: "claude",
    adapter_id: "claude",
    status_override: null,
    builtin: true,
    cli_deployed: true,
    adapter_registered: true,
    available: true,
    status: "available",
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
  {
    id: "codex",
    display_name: "Codex",
    npm_package: "@openai/codex",
    package_version: "0.166.0",
    executable: "codex",
    adapter_id: "codex",
    status_override: null,
    builtin: true,
    cli_deployed: true,
    adapter_registered: true,
    available: true,
    status: "available",
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
  {
    id: "custom-pending",
    display_name: "Custom Runner",
    npm_package: "@org/custom",
    package_version: "1.0.0",
    executable: "custom",
    adapter_id: "custom",
    status_override: "pending_build",
    builtin: false,
    cli_deployed: false,
    adapter_registered: false,
    available: false,
    status: "pending_build",
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
];

const PROVIDERS = [
  {
    id: "prov-1",
    name: "公司网关",
    type: "anthropic-messages",
    base_url: "https://gateway.example.invalid",
    models: [
      "glm-5.3-flash#claude",
      "gpt-6.2-sol#codex",
      "qwen3.8-flash#oai",
      "custom-very-long-enterprise-model-identifier-without-suffix-v2026",
    ],
    catalog_models: [
      "glm-5.3-flash#claude",
      "gpt-6.2-sol#codex",
      "qwen3.8-flash#oai",
      "custom-very-long-enterprise-model-identifier-without-suffix-v2026",
    ],
    catalog_fetched_at: "2026-11-25T08:00:00Z",
    catalog_status: "ok",
    catalog_error: null,
    model_capabilities: {},
    has_api_key: true,
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
];

const PROFILES = [
  {
    id: "agent-daily",
    name: "日常助手",
    runner_id: "claude",
    enabled: true,
    is_system_default: true,
    default_provider_id: "prov-1",
    default_model: "glm-5.3-flash#claude",
    provider_models: {
      "prov-1": ["glm-5.3-flash#claude", "removed-model#claude"],
    },
    missing_models: {
      "prov-1": ["removed-model#claude"],
    },
    pending_verification: {},
    runner_available: true,
    version: 2,
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
  {
    id: "agent-code",
    name: "代码助手",
    runner_id: "codex",
    enabled: false,
    is_system_default: false,
    default_provider_id: null,
    default_model: null,
    provider_models: {},
    missing_models: {},
    pending_verification: {},
    runner_available: true,
    version: 1,
    created_at: "2026-11-25T00:00:00Z",
    updated_at: "2026-11-25T00:00:00Z",
  },
];

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: undefined,
  });
});

test("loads named agent profiles table and displays runner, default model, and status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agent-profiles") return Response.json(PROFILES);
      if (url === "/api/v1/agent-runners") return Response.json(RUNNERS);
      if (url === "/api/v1/providers") return Response.json(PROVIDERS);
      return new Response("", { status: 404 });
    }),
  );
  render(<AgentSettings token="token" messages={getMessages("zh")} />);

  expect(await screen.findByText("日常助手")).toBeTruthy();
  expect(screen.getByText("代码助手")).toBeTruthy();
  expect(screen.getByText("系统默认")).toBeTruthy();
  expect(screen.getByText("停用")).toBeTruthy();
  expect(screen.getByText(/公司网关/)).toBeTruthy();
  expect(screen.getByText("glm-5.3-flash#claude")).toBeTruthy();
  expect(screen.getAllByRole("heading", { name: "Agent 配置" })).toHaveLength(
    1,
  );
});

test("shows load error cleanly when agent-profiles request fails", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agent-profiles")
        return new Response("", { status: 503 });
      return Response.json([]);
    }),
  );
  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  expect(await screen.findByText("加载 Agent 配置失败")).toBeTruthy();
});

test("create agent profile supports search, selected filter, bulk select, protocol badges, copy, and constrained default model", async () => {
  let createdBody: Record<string, unknown> | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agent-profiles" && init?.method === "POST") {
        createdBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Response.json(
          {
            ...PROFILES[0],
            id: "agent-new",
            name: String(createdBody.name),
            version: 1,
          },
          { status: 201 },
        );
      }
      if (url === "/api/v1/agent-profiles") return Response.json(PROFILES);
      if (url === "/api/v1/agent-runners") return Response.json(RUNNERS);
      if (url === "/api/v1/providers") return Response.json(PROVIDERS);
      return new Response("", { status: 404 });
    }),
  );

  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: /\+ 添加 Agent/ }));

  expect((screen.getByLabelText("运行器") as HTMLSelectElement).disabled).toBe(
    false,
  );
  expect(screen.getByText("已选 0 个 · 当前匹配 4 个")).toBeTruthy();

  // Protocol badges are informational only, never filter out models
  expect(screen.getAllByText("Claude").length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText("Codex").length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("OpenAI")).toBeTruthy();
  expect(screen.getByText("未知")).toBeTruthy();

  // Copy long model ID
  const longId =
    "custom-very-long-enterprise-model-identifier-without-suffix-v2026";
  fireEvent.click(screen.getByRole("button", { name: `复制 ${longId}` }));
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(longId);

  // Search and bulk select filtered
  fireEvent.change(screen.getByLabelText("搜索模型 ID…"), {
    target: { value: "#claude" },
  });
  expect(screen.getByText("已选 0 个 · 当前匹配 1 个")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "勾选当前筛选结果" }));
  expect(screen.getByText("已选 1 个 · 当前匹配 1 个")).toBeTruthy();

  // Clear search, switch to "仅看已选"
  fireEvent.change(screen.getByLabelText("搜索模型 ID…"), {
    target: { value: "" },
  });
  const selectedTab = screen.getByRole("button", { name: "仅看已选" });
  fireEvent.click(selectedTab);
  expect(selectedTab.getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByText("已选 1 个 · 当前匹配 1 个")).toBeTruthy();
  expect(screen.queryByText("gpt-6.2-sol#codex")).toBeNull();

  // Switch back to all and test bulk clear
  fireEvent.click(screen.getByRole("button", { name: "全部模型" }));
  expect(screen.getByText("gpt-6.2-sol#codex")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消勾选当前筛选结果" }));
  expect(screen.getByText("已选 0 个 · 当前匹配 4 个")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索模型 ID…"), {
    target: { value: "#claude" },
  });
  fireEvent.click(screen.getByRole("button", { name: "勾选当前筛选结果" }));
  fireEvent.change(screen.getByLabelText("搜索模型 ID…"), {
    target: { value: "" },
  });

  // Default model dropdown only contains checked models
  const defaultModelSelect = screen.getByLabelText(
    "默认模型",
  ) as HTMLSelectElement;
  const optionValues = Array.from(defaultModelSelect.options).map(
    (o) => o.value,
  );
  expect(optionValues).toEqual(["", "glm-5.3-flash#claude"]);

  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "长文档助手" },
  });
  fireEvent.change(screen.getByLabelText("运行器"), {
    target: { value: "codex" },
  });
  fireEvent.change(defaultModelSelect, {
    target: { value: "glm-5.3-flash#claude" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存 Agent" }));

  await waitFor(() => expect(createdBody).not.toBeNull());
  expect(createdBody).toEqual({
    name: "长文档助手",
    runner_id: "codex",
    enabled: true,
    is_system_default: false,
    default_provider_id: "prov-1",
    default_model: "glm-5.3-flash#claude",
    provider_models: {
      "prov-1": ["glm-5.3-flash#claude"],
    },
  });
});

test("edit agent locks runner, shows missing_models badge, and handles 409 conflict & affected_defaults", async () => {
  let mode: "affected" | "version" | "ok" = "affected";
  let profileList = [...PROFILES];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agent-profiles") return Response.json(profileList);
      if (url === "/api/v1/agent-profiles/agent-daily") {
        if (init?.method === "PUT") {
          if (mode === "affected") {
            return Response.json(
              {
                detail: {
                  message:
                    "Cannot remove active default model without replacement",
                  affected_defaults: [
                    {
                      kind: "system_default",
                      profile_id: "agent-daily",
                      profile_name: "日常助手",
                      provider_id: "prov-1",
                      model: "glm-5.3-flash#claude",
                    },
                  ],
                },
              },
              { status: 409 },
            );
          }
          if (mode === "version") {
            return Response.json(
              {
                detail: {
                  message: "Agent profile was modified concurrently",
                  current_version: 5,
                },
              },
              { status: 409 },
            );
          }
          return Response.json({ ...PROFILES[0], version: 6 });
        }
        return Response.json({ ...PROFILES[0], version: 5 });
      }
      if (url === "/api/v1/agent-runners") return Response.json(RUNNERS);
      if (url === "/api/v1/providers") return Response.json(PROVIDERS);
      return new Response("", { status: 404 });
    }),
  );

  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  const editButtons = await screen.findAllByRole("button", { name: "编辑" });
  fireEvent.click(editButtons[0]);

  expect((screen.getByLabelText("运行器") as HTMLSelectElement).disabled).toBe(
    true,
  );
  expect(screen.getByText("removed-model#claude")).toBeTruthy();
  expect(screen.getByText("目录中未找到")).toBeTruthy();

  // Unchecking missing model removes it since it is absent from the refreshed catalog
  const missingCheckbox = screen.getByLabelText(
    "公司网关 removed-model#claude",
  ) as HTMLInputElement;
  expect(missingCheckbox.checked).toBe(true);
  fireEvent.click(missingCheckbox);
  expect(screen.queryByText("removed-model#claude")).toBeNull();

  // Trigger 409 affected_defaults
  fireEvent.click(screen.getByRole("button", { name: "保存 Agent" }));
  expect(
    await screen.findByText(/以下默认配置正在使用将被撤下或停用的选项/),
  ).toBeTruthy();
  expect(screen.getAllByText(/系统默认/).length).toBeGreaterThanOrEqual(1);

  // Trigger 409 optimistic version conflict and click refresh
  mode = "version";
  fireEvent.click(screen.getByRole("button", { name: "保存 Agent" }));
  expect(await screen.findByText(/检测到并发冲突，请刷新后复核/)).toBeTruthy();
  expect(screen.getByText(/\(v5\)/)).toBeTruthy();

  profileList = [{ ...PROFILES[0], version: 5 }];
  fireEvent.click(screen.getByRole("button", { name: "刷新最新配置" }));
  expect(await screen.findByText("v5")).toBeTruthy();
});
