/// <reference types="node" />
// @vitest-environment jsdom
import { expect, test, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatPage } from "../ChatPage";
import { getMessages } from "../i18n";
import { json, provider, setupChatTestEnv } from "./testUtils";

setupChatTestEnv();

test("admins create a provider without manual model input and copy internal provider ID", async () => {
  const createdProvider = {
    ...provider,
    id: "prov-created-123",
    name: "Company gateway",
    base_url: "https://gateway.example.test",
    models: [],
    catalog_models: [],
    catalog_fetched_at: null,
    catalog_status: null,
    catalog_error: null,
    has_api_key: true,
    model_capabilities: {},
  };
  let providersList: (typeof createdProvider)[] = [];
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/providers" && init?.method === "POST") {
        providersList = [createdProvider];
        return new Response(JSON.stringify(createdProvider), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/v1/providers") return json(providersList);
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );

  // Manual model input is removed
  expect(screen.queryByLabelText("模型列表（逗号分隔）")).toBeNull();

  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Company gateway" },
  });
  fireEvent.change(screen.getByLabelText("Base URL"), {
    target: { value: "https://gateway.example.test" },
  });
  expect((screen.getByLabelText("API key") as HTMLInputElement).type).toBe(
    "password",
  );
  fireEvent.click(screen.getByRole("button", { name: /添加\s*provider/ }));

  await waitFor(() => {
    const call = fetcher.mock.calls.find(
      ([input, init]) =>
        String(input) === "/api/v1/providers" && init?.method === "POST",
    );
    expect(call).toBeTruthy();
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      name: "Company gateway",
      type: "anthropic-messages",
      base_url: "https://gateway.example.test",
    });
  });

  expect(await screen.findByText("provider 已保存")).toBeTruthy();
  expect(
    (await screen.findAllByText("prov-created-123")).length,
  ).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("已配置 key")).toBeTruthy();

  fireEvent.click(
    screen.getByRole("button", { name: "复制 ID Company gateway" }),
  );
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
    "prov-created-123",
  );
});

test("refresh-models shows distinct states and preserves prior catalog on failure", async () => {
  const initialProvider = {
    ...provider,
    id: "prov-1",
    name: "Company gateway",
    models: ["glm-5.3-flash#claude", "custom-model"],
    catalog_models: ["glm-5.3-flash#claude", "custom-model"],
    catalog_fetched_at: "2026-11-25T08:00:00Z",
    catalog_status: "ok",
    catalog_error: null,
    has_api_key: true,
    model_capabilities: {},
  };
  let nextRefreshResponse: () => Promise<Response> = async () =>
    json({
      ...initialProvider,
      models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_status: "ok",
    });

  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/providers") return json([initialProvider]);
      if (
        url === "/api/v1/providers/prov-1/refresh-models" &&
        init?.method === "POST"
      ) {
        return nextRefreshResponse();
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);

  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));

  // Initial catalog shows protocol badges
  expect(screen.getAllByText("Claude").length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("未知")).toBeTruthy();

  // 1. Refresh ok with loading state
  let resolveSlow!: (res: Response) => void;
  nextRefreshResponse = () =>
    new Promise((resolve) => {
      resolveSlow = resolve;
    });
  fireEvent.click(screen.getByRole("button", { name: "重新获取模型" }));
  expect(await screen.findByText("正在获取模型目录…")).toBeTruthy();
  resolveSlow(
    json({
      ...initialProvider,
      models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_status: "ok",
    }),
  );
  expect(
    await screen.findByText(
      "已从供应商获取 3 个模型（各 Agent 的勾选配置保持不变）。",
    ),
  ).toBeTruthy();
  expect(screen.getByText("Codex")).toBeTruthy();
  expect(screen.getByText("OpenAI")).toBeTruthy();

  // 2. Auth failed preserves prior catalog
  nextRefreshResponse = async () =>
    json({
      ...initialProvider,
      models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_models: ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
      catalog_status: "auth_failed",
      catalog_error: "HTTP 401: invalid key",
    });
  fireEvent.click(screen.getByRole("button", { name: "重新获取模型" }));
  expect(
    await screen.findByText(/认证失败，已保留上次成功目录：HTTP 401/),
  ).toBeTruthy();
  expect(screen.getByText("gpt-6.2-sol#codex")).toBeTruthy();

  // 3. Timeout & unsupported_protocol & empty
  for (const [status, error, expected] of [
    ["timeout", "Request timed out", /获取模型目录超时，已保留上次成功目录/],
    [
      "unsupported_protocol",
      "HTTP 404",
      /上游接口协议不支持模型目录查询，已保留上次成功目录/,
    ],
    ["empty", null, /上游目录为空（0 个模型）/],
  ] as const) {
    nextRefreshResponse = async () =>
      json({
        ...initialProvider,
        models:
          status === "empty"
            ? []
            : ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
        catalog_models:
          status === "empty"
            ? []
            : ["glm-5.3-flash#claude", "gpt-6.2-sol#codex", "qwen#oai"],
        catalog_status: status,
        catalog_error: error,
      });
    fireEvent.click(screen.getByRole("button", { name: "重新获取模型" }));
    expect(await screen.findByText(expected)).toBeTruthy();
  }
});

test("changing base_url requires re-entering API key or confirming clear", async () => {
  const configured = {
    ...provider,
    id: "prov-url",
    name: "Gateway",
    base_url: "https://old.example.test",
    models: ["model-a"],
    catalog_models: ["model-a"],
    has_api_key: true,
    model_capabilities: {},
  };
  let putCalls: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (
        url === "/api/v1/providers" &&
        (!init?.method || init.method === "GET")
      )
        return json([configured]);
      if (url === "/api/v1/providers/prov-url" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        putCalls.push(body);
        return json({ ...configured, ...body });
      }
      return new Response("", { status: 404 });
    }),
  );

  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));

  fireEvent.change(screen.getByLabelText("Base URL"), {
    target: { value: "https://new.example.test" },
  });
  expect(await screen.findByText(/检测到 API 地址已变更/)).toBeTruthy();

  // Submitting without key or confirm clear is blocked client-side
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  expect(
    await screen.findByText(
      "修改 Base URL 时必须重新输入 API Key 或确认清空原密钥",
    ),
  ).toBeTruthy();
  expect(putCalls).toHaveLength(0);

  // Confirm clearing stored key submits api_key: ""
  fireEvent.click(
    screen.getByLabelText("确认清空原 API Key（不再保留旧地址密钥）"),
  );
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

  await waitFor(() => expect(putCalls).toHaveLength(1));
  expect(putCalls[0]).toMatchObject({
    base_url: "https://new.example.test",
    api_key: "",
  });
});

test("admins edit per-model capabilities and save them", async () => {
  const configured = {
    ...provider,
    models: ["model-a", "model-b"],
    catalog_models: ["model-a", "model-b"],
    model_capabilities: {
      "model-a": {
        description: "带截图问答",
        vision: true,
        effort_levels: ["low", "high"],
      },
      "model-b": {
        description: "",
        vision: false,
        effort_levels: [],
      },
    },
  };
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/providers") return json([configured]);
      if (
        url === `/api/v1/providers/${configured.id}` &&
        init?.method === "PUT"
      )
        return json(configured);
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );

  fireEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));
  expect(
    ((await screen.findByLabelText("model-a 说明")) as HTMLInputElement).value,
  ).toBe("带截图问答");
  fireEvent.change(screen.getByLabelText("model-b 说明"), {
    target: { value: "纯文本模型" },
  });
  fireEvent.click(screen.getByLabelText("model-b 支持图片"));
  fireEvent.click(screen.getByLabelText("model-b 中（推荐）"));
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

  await waitFor(() => {
    const call = fetcher.mock.calls.find(
      ([input, init]) =>
        String(input) === `/api/v1/providers/${configured.id}` &&
        init?.method === "PUT",
    );
    expect(call).toBeTruthy();
    const sent = JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>;
    expect(sent.api_key).toBeUndefined();
    expect(sent.model_capabilities).toEqual({
      "model-a": {
        description: "带截图问答",
        vision: true,
        effort_levels: ["low", "high"],
      },
      "model-b": {
        description: "纯文本模型",
        vision: true,
        effort_levels: ["medium"],
      },
    });
  });
});
