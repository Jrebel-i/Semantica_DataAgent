// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SkillsPage } from "./SkillsPage";
import type { SkillView } from "./chat/types";
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

const builtinSkill: SkillView = {
  name: "dataagent-retrieval",
  description: "Retrieve warehouse knowledge",
  project_types: ["warehouse"],
  projects: [],
  secrets: [],
  dependencies: [],
  trust_warning: null,
  source: "builtin",
  protected: true,
  enabled: true,
};

const uploadedSkill: SkillView = {
  name: "jira-helper",
  description: "Inspect Jira tickets",
  project_types: ["warehouse", "document"],
  projects: ["default"],
  secrets: ["JIRA_TOKEN"],
  dependencies: ["httpx"],
  trust_warning: "Untrusted skill",
  source: "upload",
  protected: false,
  enabled: false,
};

test("管理员查看内置与上传 Skill、预览上传、启停与确认删除并显示备份边界", async () => {
  const t = getMessages("zh");
  let skills: SkillView[] = [builtinSkill];
  let uploadedEnabled = false;
  let deleted = false;
  const auditRows = [
    {
      actor_id: "admin-1",
      action: "upload",
      skill_name: "jira-helper",
      created_at: "2026-11-24T10:00:00Z",
    },
  ];
  const calls: { method: string; url: string; contentType?: string }[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers);
      calls.push({
        method,
        url,
        contentType: headers.get("Content-Type") ?? undefined,
      });

      if (url === "/api/v1/skills" && method === "GET") {
        return Response.json(
          deleted
            ? [builtinSkill]
            : skills.map((s) =>
                s.name === "jira-helper"
                  ? { ...s, enabled: uploadedEnabled }
                  : s,
              ),
        );
      }
      if (url.startsWith("/api/v1/skills/audit")) {
        return Response.json(auditRows);
      }
      if (url === "/api/v1/skills/preview" && method === "POST") {
        return Response.json({
          name: "jira-helper",
          description: "Inspect Jira tickets",
          project_types: ["warehouse", "document"],
          projects: ["default"],
          secrets: ["JIRA_TOKEN"],
          dependencies: ["httpx"],
          trust_warning: t.skillsTrustBanner,
        });
      }
      if (url === "/api/v1/skills" && method === "POST") {
        skills = [builtinSkill, uploadedSkill];
        return Response.json(uploadedSkill, { status: 201 });
      }
      if (url === "/api/v1/skills/jira-helper/enabled" && method === "PUT") {
        const body = JSON.parse(String(init?.body)) as { enabled: boolean };
        uploadedEnabled = body.enabled;
        return Response.json({ ...uploadedSkill, enabled: uploadedEnabled });
      }
      if (url === "/api/v1/skills/jira-helper" && method === "DELETE") {
        deleted = true;
        return new Response(null, { status: 204 });
      }
      return new Response("", { status: 404 });
    }),
  );

  render(<SkillsPage token="admin-token" messages={t} />);

  await screen.findByText("dataagent-retrieval");
  expect(screen.getByText(t.skillsBuiltin)).toBeTruthy();
  expect(screen.getByText(t.skillsProtected)).toBeTruthy();

  // Builtin skill cannot be disabled or deleted
  const builtinCard = screen
    .getByText("dataagent-retrieval")
    .closest(".skill-card")!;
  expect(
    builtinCard.querySelector(
      `button:not([disabled])`,
    ) as HTMLButtonElement | null,
  ).toBeNull();

  // Upload zip -> preview -> confirm upload
  const fileInput = screen.getByLabelText(t.skillsSelectZip);
  const zipFile = new File([new Uint8Array([80, 75, 5, 6])], "jira.zip", {
    type: "application/zip",
  });
  Object.defineProperty(zipFile, "arrayBuffer", {
    value: async () => new Uint8Array([80, 75, 5, 6]).buffer,
  });
  fireEvent.change(fileInput, { target: { files: [zipFile] } });

  await screen.findByText(t.skillsPreviewTitle);
  expect(screen.getByText("JIRA_TOKEN")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: t.skillsConfirmUpload }));
  await waitFor(() =>
    expect(
      screen.getByText("Inspect Jira tickets").closest(".skill-card"),
    ).toBeTruthy(),
  );

  expect(
    calls.some(
      (c) =>
        c.url === "/api/v1/skills" &&
        c.method === "POST" &&
        c.contentType === "application/zip",
    ),
  ).toBe(true);

  // Enable uploaded skill
  fireEvent.click(screen.getByRole("button", { name: t.skillsEnable }));
  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === "/api/v1/skills/jira-helper/enabled" && c.method === "PUT",
      ),
    ).toBe(true),
  );

  // Delete confirmation explains online deletion + backup retention boundary
  const jiraCard = screen
    .getByText("Inspect Jira tickets")
    .closest(".skill-card")!;
  const deleteButtons = jiraCard.querySelectorAll("button");
  fireEvent.click(deleteButtons[deleteButtons.length - 1]);

  await screen.findByText(t.skillsDeleteTitle);
  expect(screen.getByText(/历史数据库备份中的加密副本/)).toBeTruthy();

  const modalConfirm = Array.from(
    document.querySelectorAll(".ant-modal-footer button"),
  ).pop() as HTMLButtonElement;
  fireEvent.click(modalConfirm);

  await waitFor(() =>
    expect(
      calls.some(
        (c) => c.url === "/api/v1/skills/jira-helper" && c.method === "DELETE",
      ),
    ).toBe(true),
  );
  await waitFor(() =>
    expect(screen.queryByText("Inspect Jira tickets")).toBeNull(),
  );
});
