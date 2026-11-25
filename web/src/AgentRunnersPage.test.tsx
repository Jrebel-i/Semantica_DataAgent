// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AgentRunnersPage } from "./AgentRunnersPage";
import { getMessages } from "./i18n";

const RUNNERS = [
  {
    id: "pi",
    display_name: "pi",
    npm_package: "@earendil-works/pi-coding-agent",
    package_version: "1.2.0",
    executable: "pi",
    adapter_id: "pi",
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
    cli_deployed: false,
    adapter_registered: true,
    available: false,
    status: "pending_build",
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
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("displays builtin and custom runners with npm package, version, CLI, and adapter status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/agent-runners")
        return Response.json(RUNNERS);
      return new Response("", { status: 404 });
    }),
  );

  render(
    <AgentRunnersPage
      token="token"
      messages={getMessages("zh")}
      onNavigateAgentConfig={() => undefined}
    />,
  );

  expect(await screen.findByText("Claude")).toBeTruthy();
  expect(screen.getByText("@anthropic-ai/claude-code@2.1.318")).toBeTruthy();
  expect(screen.getByText("@openai/codex@0.166.0")).toBeTruthy();
  expect(screen.getAllByText("可用").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("待构建")).toBeTruthy();
  expect(
    screen.getByText(/绝不在服务端或会话沙箱执行任意 shell 命令/),
  ).toBeTruthy();
});

test("registers a new runner with structured metadata and refreshes table", async () => {
  let rows = [...RUNNERS];
  let postedBody: Record<string, unknown> | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agent-runners" && init?.method === "POST") {
        postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        const created = {
          ...postedBody,
          status_override: null,
          builtin: false,
          cli_deployed: false,
          adapter_registered: false,
          available: false,
          status: "adapter_missing",
          created_at: "2026-11-25T01:00:00Z",
          updated_at: "2026-11-25T01:00:00Z",
        };
        rows = [...rows, created as (typeof RUNNERS)[number]];
        return Response.json(created, { status: 201 });
      }
      if (url === "/api/v1/agent-runners") return Response.json(rows);
      return new Response("", { status: 404 });
    }),
  );

  render(
    <AgentRunnersPage
      token="token"
      messages={getMessages("zh")}
      onNavigateAgentConfig={() => undefined}
    />,
  );
  await screen.findByText("Claude");

  fireEvent.change(screen.getByLabelText("运行器 ID"), {
    target: { value: "gemini-cli" },
  });
  fireEvent.change(screen.getByLabelText("显示名称"), {
    target: { value: "Gemini CLI" },
  });
  fireEvent.change(screen.getByLabelText("npm 包名"), {
    target: { value: "@google/gemini-cli" },
  });
  fireEvent.change(screen.getByLabelText("固定版本"), {
    target: { value: "0.5.0" },
  });
  fireEvent.change(screen.getByLabelText("可执行文件名"), {
    target: { value: "gemini" },
  });
  fireEvent.change(screen.getByLabelText("后端适配器 ID"), {
    target: { value: "gemini" },
  });

  fireEvent.click(screen.getByRole("button", { name: "登记运行器" }));

  await waitFor(() => expect(postedBody).not.toBeNull());
  expect(postedBody).toEqual({
    id: "gemini-cli",
    display_name: "Gemini CLI",
    npm_package: "@google/gemini-cli",
    package_version: "0.5.0",
    executable: "gemini",
    adapter_id: "gemini",
  });
  expect(await screen.findByText("运行器已登记")).toBeTruthy();
  expect(await screen.findByText("Gemini CLI")).toBeTruthy();
  expect(screen.getByText("@google/gemini-cli@0.5.0")).toBeTruthy();
  expect(screen.getByText("适配器未接入")).toBeTruthy();
});
