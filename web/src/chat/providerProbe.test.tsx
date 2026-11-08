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
import { json, provider, setupChatTestEnv } from "./testUtils";

setupChatTestEnv();

const configured = {
  ...provider,
  models: ["model-a", "model-b"],
  model_capabilities: {
    "model-a": { description: "带截图问答", vision: true, effort_levels: [] },
    "model-b": { description: "", vision: false, effort_levels: [] },
  },
};

const result = {
  model: "model-a",
  vision: "no",
  effort: {
    low: "yes",
    medium: "yes",
    high: "no",
    extra: "no",
    max: "unknown",
  },
  notes: "vision: HTTP 400; high: HTTP 400",
};

function fetcherFor(probe: () => Promise<Response>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/agents")
      return json([{ id: "pi", name: "pi", available: true }]);
    if (url === "/api/v1/providers") return json([configured]);
    if (url === "/api/v1/conversations") return json([]);
    if (url === `/api/v1/providers/${configured.id}/probe`) return probe();
    if (url === `/api/v1/providers/${configured.id}` && init?.method === "PUT")
      return json(configured);
    return new Response("", { status: 404 });
  });
}

async function openEditor(fetcher: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetcher);
  render(
    <ChatPage
      token="personal-token"
      role="admin"
      settingsOnly
      messages={getMessages("zh")}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
}

test("admins probe each model and apply the result before saving", async () => {
  let resolveProbe: (response: Response) => void = () => undefined;
  const pending = new Promise<Response>((resolve) => {
    resolveProbe = resolve;
  });
  const fetcher = fetcherFor(() => pending);
  await openEditor(fetcher);

  expect(screen.getByRole("button", { name: "model-a 探测" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "model-b 探测" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "model-a 探测" }));
  expect((await screen.findByRole("status")).textContent).toContain("探测中");
  await act(async () => {
    resolveProbe(json(result));
    await pending;
  });

  expect(await screen.findByLabelText("model-a 探测结果")).toBeTruthy();
  expect(screen.getByLabelText("model-a 图片 探测结果").textContent).toContain(
    "不支持",
  );
  expect(screen.getByLabelText("model-a 低 探测结果").textContent).toContain(
    "✓",
  );
  expect(screen.getByLabelText("model-a 高 探测结果").textContent).toContain(
    "✕",
  );
  expect(screen.getByLabelText("model-a 最高 探测结果").textContent).toContain(
    "?",
  );
  expect(screen.getByText(result.notes)).toBeTruthy();

  const probeCall = fetcher.mock.calls.find(
    ([input]) => String(input) === `/api/v1/providers/${configured.id}/probe`,
  );
  expect(probeCall).toBeTruthy();
  expect(JSON.parse(String(probeCall?.[1]?.body))).toEqual({
    model: "model-a",
  });

  fireEvent.click(screen.getByRole("button", { name: "model-a 应用探测结果" }));
  expect(
    (screen.getByLabelText("model-a 支持图片") as HTMLInputElement).checked,
  ).toBe(false);
  expect(
    (screen.getByLabelText("model-a 低") as HTMLInputElement).checked,
  ).toBe(true);
  expect(
    (screen.getByLabelText("model-a 中（推荐）") as HTMLInputElement).checked,
  ).toBe(true);
  expect(
    (screen.getByLabelText("model-a 高") as HTMLInputElement).checked,
  ).toBe(false);
  expect(
    (screen.getByLabelText("model-a 最高") as HTMLInputElement).checked,
  ).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => {
    const call = fetcher.mock.calls.find(
      ([input, init]) =>
        String(input) === `/api/v1/providers/${configured.id}` &&
        init?.method === "PUT",
    );
    expect(call).toBeTruthy();
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      model_capabilities: {
        "model-a": {
          description: "带截图问答",
          vision: false,
          effort_levels: ["low", "medium"],
        },
      },
    });
  });
});

test("probe errors are shown without changing the form", async () => {
  const fetcher = fetcherFor(async () => new Response("{}", { status: 409 }));
  await openEditor(fetcher);

  fireEvent.click(screen.getByRole("button", { name: "model-a 探测" }));

  expect(
    await screen.findByText("该 provider 正在探测，请稍后重试"),
  ).toBeTruthy();
  expect(
    (screen.getByLabelText("model-a 支持图片") as HTMLInputElement).checked,
  ).toBe(true);
  expect(screen.queryByLabelText("model-a 探测结果")).toBeNull();
});
