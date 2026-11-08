/// <reference types="node" />
// @vitest-environment jsdom
import { expect, test, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ChatPage } from "../ChatPage";
import { getMessages } from "../i18n";
import {
  conversationFetcher,
  conversationRow,
  cssDeclarations,
  document,
  json,
  openHistory,
  provider,
  setupChatTestEnv,
  sse,
  table,
  toolStream,
  toolTurnEvents,
} from "./testUtils";

setupChatTestEnv();

const longDescription =
  "面向超长上下文与复杂取数的推理模型，支持多轮澄清、SQL 校验、结果解释和表格归纳；" +
  "在高峰期可能比普通模型更慢，建议只在复杂问题上选择这个模型，普通问数继续使用默认模型即可。";

const effortProvider = {
  ...provider,
  models: ["model-full", "model-partial", "model-none"],
  model_capabilities: {
    "model-full": {
      description: longDescription,
      vision: false,
      effort_levels: ["low", "medium", "high", "extra", "max"],
    },
    "model-partial": {
      description: "只支持部分强度",
      vision: false,
      effort_levels: ["low", "high"],
    },
    "model-none": {
      description: "",
      vision: false,
      effort_levels: [],
    },
  },
};

const effortAgents = [
  {
    id: "pi",
    name: "pi",
    available: true,
    effort_levels: ["low", "medium", "high", "extra", "max"],
  },
  {
    id: "claude",
    name: "Claude",
    available: true,
    effort_levels: [],
  },
];

function effortFetcher(onAsk?: (body: Record<string, unknown>) => void) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/agents") return json(effortAgents);
    if (url === "/api/v1/providers") return json([effortProvider]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === "/api/v1/assistant-defaults")
      return json({
        assistant_agent: "pi",
        assistant_provider_id: effortProvider.id,
        assistant_model: "model-full",
      });
    if (url === "/api/v1/conversations/ask" && init?.method === "POST") {
      onAsk?.(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(sse, {
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response("", { status: 404 });
  });
}

async function openComposerSelect(label: string) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  await screen.findByRole("listbox");
}

function composerSelectText(label: string) {
  const input = screen.getByLabelText(label);
  return (
    input
      .closest(".ant-select")
      ?.querySelector(".ant-select-selection-item")
      ?.textContent?.trim() ?? ""
  );
}

function optionDisabled(text: string) {
  const option = screen.getByRole("option", { name: new RegExp(text) });
  return (
    option.getAttribute("aria-disabled") === "true" ||
    option.classList.contains("ant-select-item-option-disabled")
  );
}

test("思考强度默认中并在提问请求里携带", async () => {
  let askBody: Record<string, unknown> = {};
  vi.stubGlobal("fetch", effortFetcher((body) => (askBody = body)));
  render(
    <ChatPage
      token="token"
      role="user"
      messages={getMessages("zh")}
    />,
  );

  await screen.findByLabelText("思考强度");
  expect(composerSelectText("思考强度")).toBe("中（推荐）");

  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "订单在哪里？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() => expect(askBody.question).toBe("订单在哪里？"));
  expect(askBody.effort).toBe("medium");
});

test("模型没有档位时不显示思考强度，有档位时置灰不支持的档位", async () => {
  vi.stubGlobal("fetch", effortFetcher());
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await screen.findByLabelText("思考强度");
  await openComposerSelect("模型");
  fireEvent.click(screen.getByRole("option", { name: "model-none" }));
  await waitFor(() => expect(screen.queryByLabelText("思考强度")).toBeNull());

  await openComposerSelect("模型");
  fireEvent.click(screen.getByRole("option", { name: "model-partial" }));
  await screen.findByLabelText("思考强度");
  expect(composerSelectText("思考强度")).toBe("默认");
  await openComposerSelect("思考强度");
  expect(optionDisabled("中（推荐）")).toBe(true);
  expect(optionDisabled("超高")).toBe(true);
  expect(optionDisabled("低")).toBe(false);
  expect(optionDisabled("高")).toBe(false);
});

test("切换 agent 后不支持的档位一起置灰", async () => {
  vi.stubGlobal("fetch", effortFetcher());
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await screen.findByLabelText("思考强度");
  fireEvent.change(screen.getByLabelText("Agent"), {
    target: { value: "claude" },
  });
  await openComposerSelect("思考强度");
  for (const label of ["低", "中（推荐）", "高", "超高", "最高"]) {
    expect(optionDisabled(label)).toBe(true);
  }
});

test("最高档在下拉里提示用量更大，长模型说明可悬停看全文", async () => {
  vi.stubGlobal("fetch", effortFetcher());
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await screen.findByLabelText("思考强度");
  await openComposerSelect("思考强度");
  expect(screen.getByText("用量更大")).toBeTruthy();

  await openComposerSelect("模型");
  const descriptions = screen.getAllByText(longDescription);
  expect(descriptions[0].getAttribute("title")).toBe(longDescription);
  expect(descriptions[0].className).toContain("model-option-description");
});

test("切换模型和强度后提问使用新的强度", async () => {
  let askBody: Record<string, unknown> = {};
  vi.stubGlobal("fetch", effortFetcher((body) => (askBody = body)));
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await screen.findByLabelText("思考强度");
  await openComposerSelect("思考强度");
  fireEvent.click(screen.getByRole("option", { name: "高" }));
  expect(composerSelectText("思考强度")).toBe("高");
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "继续追问" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() => expect(askBody.question).toBe("继续追问"));
  expect(askBody.effort).toBe("high");
});

test("新会话发送所选 agent 与模型且仅锁定 agent", async () => {
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([
          { id: "pi", name: "pi", available: true },
          { id: "claude", name: "Claude", available: true },
        ]);
      if (url === "/api/v1/providers")
        return json([{ ...provider, models: ["model-a", "model-b"] }]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_provider_id: provider.id,
          assistant_model: "model-a",
        });
      if (url === "/api/v1/conversations/ask" && init?.method === "POST")
        return new Response(sse, {
          headers: { "Content-Type": "text/event-stream" },
        });
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );
  await screen.findByRole("option", { name: "Claude" });
  fireEvent.change(screen.getByLabelText("Agent"), {
    target: { value: "claude" },
  });
  fireEvent.change(screen.getByLabelText("模型"), {
    target: { value: "model-b" },
  });
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "订单在哪里？" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url]) => String(url) === "/api/v1/conversations/ask",
      ),
    ).toBe(true),
  );
  const call = fetcher.mock.calls.find(
    ([url]) => String(url) === "/api/v1/conversations/ask",
  );
  expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
    agent: "claude",
    provider_id: provider.id,
    model: "model-b",
  });
  await waitFor(() =>
    expect((screen.getByLabelText("Agent") as HTMLSelectElement).disabled).toBe(
      true,
    ),
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).disabled).toBe(
    false,
  );
  expect(
    (screen.getByLabelText("Provider") as HTMLSelectElement).disabled,
  ).toBe(false);
});

test("对话中可切换 provider 与模型并在下一条消息使用", async () => {
  const messages = getMessages("zh");
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second gateway",
    models: ["model-b", "model-c"],
  };
  const row = conversationRow("conv-1", "订单分析");
  let askBody = "";
  const fetcher = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations?archived=true") return json([]);
      if (url === "/api/v1/conversations") return json([row]);
      if (url === "/api/v1/assistant-defaults") return json({});
      if (url === "/api/v1/conversations/conv-1")
        return json({ ...row, turns: [] });
      if (url === "/api/v1/conversations/ask" && init?.method === "POST") {
        askBody = String(init.body);
        return new Response(sse, {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return new Response("", { status: 404 });
    },
  );
  vi.stubGlobal("fetch", fetcher);
  render(<ChatPage token="token" role="user" messages={messages} />);
  await screen.findByRole("button", { name: "订单分析" });
  fireEvent.click(screen.getByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(
      (screen.getByLabelText(messages.askProvider) as HTMLSelectElement).value,
    ).toBe(provider.id),
  );
  const agent = screen.getByLabelText(messages.askAgent) as HTMLSelectElement;
  expect(agent.disabled).toBe(true);
  expect(screen.getByText(messages.agentLockedHint)).toBeTruthy();
  fireEvent.change(screen.getByLabelText(messages.askProvider), {
    target: { value: second.id },
  });
  expect(
    (screen.getByLabelText(messages.askModel) as HTMLSelectElement).value,
  ).toBe("model-b");
  fireEvent.change(screen.getByLabelText(messages.askModel), {
    target: { value: "model-c" },
  });
  expect(
    await screen.findByText(
      messages.nextMessageModel.replace("{model}", "model-c"),
    ),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText(messages.askQuestion), {
    target: { value: "继续追问" },
  });
  fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  await waitFor(() => expect(askBody).not.toBe(""));
  expect(JSON.parse(askBody)).toMatchObject({
    question: "继续追问",
    conversation_id: "conv-1",
    agent: "pi",
    provider_id: second.id,
    model: "model-c",
  });
  expect(
    (screen.getByLabelText(messages.askProvider) as HTMLSelectElement).disabled,
  ).toBe(false);
  expect(
    (screen.getByLabelText(messages.askModel) as HTMLSelectElement).disabled,
  ).toBe(false);
});

test("新会话切换 agent 时使用该 agent 的 provider 和默认模型", async () => {
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second",
    models: ["model-b"],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([
          { id: "pi", name: "pi", available: true },
          { id: "claude", name: "Claude", available: true },
        ]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_agent_profiles: JSON.stringify({
            claude: {
              providers: [second.id],
              default_provider: second.id,
              default_model: "model-b",
            },
          }),
        });
      return new Response("", { status: 404 });
    }),
  );
  render(
    <ChatPage
      token="personal-token"
      role="user"
      messages={getMessages("zh")}
    />,
  );
  await screen.findByRole("option", { name: "Claude" });
  await waitFor(() =>
    expect(screen.getByRole("option", { name: "Second" })).toBeTruthy(),
  );
  fireEvent.change(screen.getByLabelText("Agent"), {
    target: { value: "claude" },
  });
  expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
    "provider-2",
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
    "model-b",
  );
  expect(screen.queryByRole("option", { name: "Local gateway" })).toBeNull();
});

test("新建会话不会恢复不允许使用的默认 provider", async () => {
  const second = {
    ...provider,
    id: "provider-2",
    name: "Second",
    models: ["model-b"],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/agents")
        return json([{ id: "pi", name: "pi", available: true }]);
      if (url === "/api/v1/providers") return json([provider, second]);
      if (url === "/api/v1/conversations") return json([]);
      if (url === "/api/v1/assistant-defaults")
        return json({
          assistant_agent: "pi",
          assistant_provider_id: provider.id,
          assistant_model: provider.models[0],
          assistant_agent_profiles: JSON.stringify({
            pi: {
              providers: [second.id],
              default_provider: "",
              default_model: "",
            },
          }),
        });
      return new Response("", { status: 404 });
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
      "provider-2",
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "新对话" }));
  expect((screen.getByLabelText("Provider") as HTMLSelectElement).value).toBe(
    "provider-2",
  );
  expect((screen.getByLabelText("模型") as HTMLSelectElement).value).toBe(
    "model-b",
  );
});

test("示例问题只填入输入框而不发送请求", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL) => json([]));
  vi.stubGlobal("fetch", fetcher);
  const messages = getMessages("zh");
  render(<ChatPage token="token" role="user" messages={messages} />);
  fireEvent.click(
    await screen.findByRole("button", { name: messages.examples[0] }),
  );
  expect(
    (screen.getByLabelText(messages.askQuestion) as HTMLTextAreaElement).value,
  ).toBe(messages.examples[0]);
  expect(
    fetcher.mock.calls.some(
      ([url]) => String(url) === "/api/v1/conversations/ask",
    ),
  ).toBe(false);
});
