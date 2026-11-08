/// <reference types="node" />
// @vitest-environment jsdom
import { beforeEach, expect, test, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ChatPage } from "../ChatPage";
import { getMessages } from "../i18n";
import { PROJECT_STORAGE_KEY } from "../ProjectSelector";
import { json, provider, setupChatTestEnv, sse } from "./testUtils";

setupChatTestEnv();

beforeEach(() => {
  localStorage.clear();
});

const longName =
  "全国线下零售与电商渠道销售分析知识库（含促销、退货、会员与跨境业务口径说明）";

function project(
  id: string,
  name: string,
  role: "admin" | "member" = "member",
) {
  return {
    id,
    name,
    type: "document" as const,
    status: "active" as const,
    created_at: "2026-11-05T00:00:00Z",
    my_role: role,
  };
}

const eightProjects = [
  project("alpha", longName),
  project("beta", "电商渠道"),
  project("gamma", "门店运营"),
  project("delta", "供应链"),
  project("epsilon", "财务"),
  project("zeta", "会员"),
  project("eta", "跨境"),
  project("theta", "市场"),
];

const turn = {
  id: 1,
  sequence: 1,
  provider_id: provider.id,
  model: provider.models[0],
  effort: null,
  question: "销售额多少？",
  status: "completed",
  answer: "已按项目知识回答。",
  sql: [],
  citations: [],
  error: null,
  started_at: "2026-11-05T00:00:00Z",
  finished_at: "2026-11-05T00:00:01Z",
  events: [],
};

function conversation(
  id: string,
  title: string,
  projectId: string,
  projectName: string,
  turnCount = 1,
) {
  return {
    id,
    project_id: projectId,
    project_name: projectName,
    title,
    agent: "pi",
    provider_id: provider.id,
    model: provider.models[0],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-05T00:00:00Z",
    turn_count: turnCount,
    resumable: true,
    pinned: false,
    archived: false,
  };
}

function fetcher({
  projects = eightProjects,
  conversations = [] as ReturnType<typeof conversation>[],
  archived = [] as ReturnType<typeof conversation>[],
  onAsk,
}: {
  projects?: ReturnType<typeof project>[];
  conversations?: ReturnType<typeof conversation>[];
  archived?: ReturnType<typeof conversation>[];
  onAsk?: (body: Record<string, unknown>) => void;
}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/projects") return json(projects);
    if (url === "/api/v1/conversations") return json(conversations);
    if (url === "/api/v1/conversations?archived=true") return json(archived);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([provider]);
    if (url === "/api/v1/assistant-defaults")
      return json({
        assistant_agent: "pi",
        assistant_provider_id: provider.id,
        assistant_model: provider.models[0],
      });
    if (url === "/api/v1/conversations/ask" && init?.method === "POST") {
      onAsk?.(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(sse, {
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    const match = url.match(/^\/api\/v1\/conversations\/([^/?]+)/);
    if (match) {
      const id = decodeURIComponent(match[1]);
      const row = [...conversations, ...archived].find(
        (item) => item.id === id,
      );
      if (row) {
        return json({
          ...row,
          turns: row.turn_count > 0 ? [turn] : [],
        });
      }
    }
    return new Response("", { status: 404 });
  });
}

function selectText(label: string) {
  const element = screen.getByLabelText(label) as HTMLSelectElement;
  return element.selectedOptions[0]?.textContent?.trim() ?? "";
}

async function chooseProject(label: string, text: string) {
  fireEvent.change(screen.getByLabelText(label), {
    target: { value: text },
  });
}

test("输入区只列出可访问项目，默认选上次使用的项目", async () => {
  localStorage.setItem(PROJECT_STORAGE_KEY, "gamma");
  vi.stubGlobal("fetch", fetcher({}));
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await waitFor(() => expect(selectText("项目")).toBe("门店运营"));
  const options = Array.from(
    (screen.getByLabelText("项目") as HTMLSelectElement).options,
  ).map((option) => option.textContent);
  expect(options).toEqual(eightProjects.map((item) => item.name));
  expect(options[0]).toBe(longName);
});

test("上次使用的项目失效时回退默认项目", async () => {
  localStorage.setItem(PROJECT_STORAGE_KEY, "removed");
  vi.stubGlobal(
    "fetch",
    fetcher({
      projects: [project("default", "业务数据"), project("beta", "电商渠道")],
    }),
  );
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await waitFor(() =>
    expect(localStorage.getItem(PROJECT_STORAGE_KEY)).toBe("default"),
  );
  expect(selectText("项目")).toBe("业务数据");
});

test("只有一个项目时显示为只读标签", async () => {
  vi.stubGlobal(
    "fetch",
    fetcher({ projects: [project("only", "唯一的项目")] }),
  );
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );

  await waitFor(() =>
    expect(
      container.querySelector(".composer-project-label")?.textContent,
    ).toBe("唯一的项目"),
  );
  expect(screen.queryByLabelText("项目")).toBeNull();
});

test("新对话使用当前选择的项目提问并记住选择", async () => {
  let askBody: Record<string, unknown> = {};
  vi.stubGlobal("fetch", fetcher({ onAsk: (body) => (askBody = body) }));
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  await waitFor(() => expect(selectText("项目")).toBe(longName));
  await chooseProject("项目", "epsilon");
  fireEvent.change(screen.getByLabelText("问题"), {
    target: { value: "订单在哪里？" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /提\s*问/ }));
  });

  await waitFor(() => expect(askBody.project).toBe("epsilon"));
  expect(localStorage.getItem(PROJECT_STORAGE_KEY)).toBe("epsilon");
});

test("已有轮次的对话锁定项目并给出换项目提示", async () => {
  const rows = [conversation("conv-a", "订单分析", "beta", "电商渠道")];
  vi.stubGlobal("fetch", fetcher({ conversations: rows }));
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "订单分析" }));

  await waitFor(() => expect(selectText("项目")).toBe("电商渠道"));
  expect((screen.getByLabelText("项目") as HTMLSelectElement).disabled).toBe(
    true,
  );
  expect(screen.getByText("对话已绑定项目，换项目请新建对话")).toBeTruthy();
});

test("回复中的新对话沿用当前项目选择", async () => {
  const rows = [conversation("conv-a", "订单分析", "beta", "电商渠道")];
  vi.stubGlobal("fetch", fetcher({ conversations: rows }));
  render(<ChatPage token="token" role="user" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "订单分析" }));
  await waitFor(() => expect(selectText("项目")).toBe("电商渠道"));
  fireEvent.click(screen.getByRole("button", { name: "新对话" }));

  await waitFor(() =>
    expect((screen.getByLabelText("项目") as HTMLSelectElement).disabled).toBe(
      false,
    ),
  );
  expect(selectText("项目")).toBe("电商渠道");
});

test("侧栏显示会话项目并可按项目筛选，筛选不改当前对话", async () => {
  const rows = [
    conversation("conv-a", "订单分析", "beta", "电商渠道"),
    conversation("conv-b", "会员分析", "zeta", "会员"),
  ];
  vi.stubGlobal("fetch", fetcher({ conversations: rows }));
  const { container } = render(
    <ChatPage token="token" role="user" messages={getMessages("zh")} />,
  );

  await screen.findByRole("button", { name: "订单分析" });
  expect(container.querySelectorAll(".session-project")).toHaveLength(2);
  expect(screen.getAllByText("电商渠道").length).toBeGreaterThan(0);

  fireEvent.click(screen.getByRole("button", { name: "订单分析" }));
  await waitFor(() =>
    expect(container.querySelector(".chat-header strong")?.textContent).toBe(
      "订单分析",
    ),
  );

  fireEvent.change(screen.getByLabelText("项目筛选"), {
    target: { value: "zeta" },
  });

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "订单分析" })).toBeNull(),
  );
  expect(screen.getByRole("button", { name: "会员分析" })).toBeTruthy();
  expect(container.querySelector(".chat-header strong")?.textContent).toBe(
    "订单分析",
  );
});
