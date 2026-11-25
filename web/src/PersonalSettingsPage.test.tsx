// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PersonalSettingsPage } from "./PersonalSettingsPage";
import { getMessages } from "./i18n";

beforeEach(() => {
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
  vi.restoreAllMocks();
});

test("个人设置页按项目写而不读地设置、替换与删除 Skill 个人密钥并展示备份边界", async () => {
  const t = getMessages("zh");
  let configured = false;
  let storedSecret = "";

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/api/v1/me/dtb-token") {
        return Response.json({ configured: false });
      }
      if (url === "/api/v1/projects") {
        return Response.json([
          {
            id: "default",
            name: "业务数据",
            type: "warehouse",
            status: "active",
            created_at: "2026-11-05T00:00:00Z",
            my_role: "member",
          },
        ]);
      }
      if (url === "/api/v1/skills/available?project=default") {
        return Response.json([
          {
            name: "jira-helper",
            description: "Jira helper",
            project_types: ["warehouse"],
            projects: ["default"],
            secrets: ["JIRA_TOKEN"],
            dependencies: [],
            trust_warning: "untrusted",
            source: "upload",
            protected: false,
            enabled: true,
            configured_secrets: configured ? ["JIRA_TOKEN"] : [],
            missing_secrets: configured ? [] : ["JIRA_TOKEN"],
            ready: configured,
          },
        ]);
      }
      if (
        url === "/api/v1/skills/jira-helper/secrets?project=default" &&
        method === "GET"
      ) {
        return Response.json({ JIRA_TOKEN: configured });
      }
      if (
        url ===
          "/api/v1/skills/jira-helper/secrets/JIRA_TOKEN?project=default" &&
        method === "PUT"
      ) {
        const body = JSON.parse(String(init?.body)) as { value: string };
        storedSecret = body.value;
        configured = true;
        return new Response(null, { status: 204 });
      }
      if (
        url ===
          "/api/v1/skills/jira-helper/secrets/JIRA_TOKEN?project=default" &&
        method === "DELETE"
      ) {
        storedSecret = "";
        configured = false;
        return new Response(null, { status: 204 });
      }
      return new Response("", { status: 404 });
    }),
  );

  render(
    <PersonalSettingsPage
      token="user-token"
      messages={t}
      language="zh"
      onChangeLanguage={() => undefined}
    />,
  );

  await screen.findByText("jira-helper");
  expect(
    screen.getByText(/历史备份中的加密副本按现有保留策略到期/),
  ).toBeTruthy();
  expect(screen.getByText(t.skillsSecretMissing)).toBeTruthy();

  const input = screen.getByLabelText(
    "jira-helper JIRA_TOKEN",
  ) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "my-top-secret-val" } });
  fireEvent.click(screen.getByRole("button", { name: t.skillsSecretSave }));

  await waitFor(() => expect(storedSecret).toBe("my-top-secret-val"));
  await screen.findByText(t.skillsSecretConfigured);
  // Plaintext is cleared from input and never displayed back
  expect(input.value).toBe("");
  expect(screen.queryByText("my-top-secret-val")).toBeNull();

  // Delete secret
  const deleteBtn = await screen.findByRole("button", {
    name: t.skillsSecretDelete,
  });
  fireEvent.click(deleteBtn);
  await waitFor(() => expect(storedSecret).toBe(""));
  await screen.findByText(t.skillsSecretMissing);
});
