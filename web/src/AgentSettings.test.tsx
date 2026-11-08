// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AgentSettings } from "./AgentSettings";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
test("读取配置失败时不能覆盖原有 agent 设置", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/settings"
        ? new Response("", { status: 503 })
        : Response.json([]),
    ),
  );
  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  expect(await screen.findByText("无法获取状态")).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "保存设置" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

test("配置格式损坏时不能把它当作空配置覆盖", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/api/v1/settings"
        ? Response.json({ assistant_agent_profiles: "{" })
        : Response.json([]),
    ),
  );
  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  expect(await screen.findByText("无法获取状态")).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "保存设置" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

test("没有 provider 时显示空提示且不重复页面标题", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/settings") return Response.json({});
      if (url === "/api/v1/agents")
        return Response.json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return Response.json([]);
      return new Response("", { status: 404 });
    }),
  );
  render(<AgentSettings token="token" messages={getMessages("zh")} />);
  expect(await screen.findByText("尚未配置 provider")).toBeTruthy();
  expect(
    screen.getByText("尚未配置 provider").closest(".empty-state"),
  ).not.toBeNull();
  expect(screen.getAllByRole("heading", { name: "Agent 配置" })).toHaveLength(
    1,
  );
});
