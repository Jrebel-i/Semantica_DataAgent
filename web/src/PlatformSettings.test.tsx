// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PlatformSettings } from "./PlatformSettings";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
test("平台设置保存变更时保留已配置的密码", async () => {
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) =>
    options?.method === "PUT"
      ? Response.json({})
      : Response.json({
          neo4j_uri: "bolt://old",
          neo4j_user: "neo4j",
          neo4j_password: "configured",
          milvus_uri: "http://old",
          embedding_url: "http://embed",
          embedding_model: "model",
        }),
  );
  vi.stubGlobal("fetch", fetcher);
  render(<PlatformSettings token="token" messages={getMessages("zh")} />);
  await waitFor(() =>
    expect(
      (screen.getByLabelText("Neo4j 地址") as HTMLInputElement).value,
    ).toBe("bolt://old"),
  );
  expect((screen.getByLabelText("Neo4j 密码") as HTMLInputElement).value).toBe(
    "",
  );
  fireEvent.change(screen.getByLabelText("Neo4j 地址"), {
    target: { value: "bolt://new" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(([, options]) => options?.method === "PUT"),
    ).toBe(true),
  );
  const call = fetcher.mock.calls.find(
    ([, options]) => options?.method === "PUT",
  );
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({
    values: { neo4j_uri: "bolt://new" },
  });
});
