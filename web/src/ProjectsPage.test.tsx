// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ProjectsPage } from "./ProjectsPage";
import { getMessages } from "./i18n";

const created = "2026-11-05T19:27:00+08:00";

async function chooseOption(label: string, option: string) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  const element = await waitFor(() => {
    const dropdowns = Array.from(
      document.querySelectorAll(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      ),
    );
    const found = dropdowns
      .at(-1)
      ?.querySelector(`.ant-select-item-option[title="${option}"]`);
    if (!found) throw new Error(`missing option ${option}`);
    return found;
  });
  fireEvent.click(element);
}

function projectRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "default",
    name: "业务数据",
    type: "warehouse",
    status: "active",
    created_at: created,
    my_role: "admin",
    ...overrides,
  };
}

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
    configurable: true,
    value: () => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
    }),
  });
  Object.defineProperty(window, "getComputedStyle", {
    configurable: true,
    value: () => ({ getPropertyValue: () => "" }),
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists projects with type, status and creation time", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json([
        projectRow(),
        projectRow({
          id: "docs",
          name: "Platform Docs",
          type: "document",
        }),
      ]),
    ),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  expect(screen.getByText("数仓型")).toBeTruthy();
  expect(screen.getByText("文档型")).toBeTruthy();
  expect(screen.getAllByText("有效").length).toBe(2);
  expect(screen.getAllByText(new Date(created).toLocaleString()).length).toBe(
    2,
  );
});

test("creates a document project and refreshes the list", async () => {
  let rows = [projectRow()];
  const calls: Array<{ url: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({ url, body: options?.body as string | undefined });
      if (options?.method === "POST") {
        rows = [
          ...rows,
          projectRow({
            id: "platformdocs",
            name: "Platform Docs",
            type: "document",
          }),
        ];
        return Response.json(rows[rows.length - 1], { status: 201 });
      }
      return Response.json(rows);
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "创建项目" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "Platform Docs" },
  });
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByTitle("文档型"));
  fireEvent.click(screen.getByRole("button", { name: /^创\s*建$/ }));

  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  const post = calls.find((call) => call.body);
  expect(JSON.parse(post?.body ?? "{}")).toEqual({
    name: "Platform Docs",
    type: "document",
  });
});

test("shows the duplicate-name message returned by the API", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, options?: RequestInit) =>
      options?.method === "POST"
        ? new Response(
            JSON.stringify({ detail: "Project name already exists" }),
            {
              status: 409,
            },
          )
        : Response.json([projectRow()]),
    ),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "创建项目" }));
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "业务数据" },
  });
  fireEvent.click(screen.getByRole("button", { name: /^创\s*建$/ }));
  expect(await screen.findByText("已有同名项目")).toBeTruthy();
});

test("project admins only see projects they administer and cannot create", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json([
        projectRow(),
        projectRow({ id: "docs", name: "Platform Docs", my_role: "member" }),
      ]),
    ),
  );
  render(
    <ProjectsPage
      token="user"
      messages={getMessages("zh")}
      canCreate={false}
    />,
  );

  expect(await screen.findByText("业务数据")).toBeTruthy();
  expect(screen.queryByText("Platform Docs")).toBeNull();
  expect(screen.queryByRole("button", { name: "创建项目" })).toBeNull();
});

test("lists members, adds a user, changes roles and confirms removal", async () => {
  let members = [{ id: "pa", name: "Pa", role: "admin" }];
  const calls: Array<{ url: string; method?: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({
        url,
        method: options?.method,
        body: options?.body as string | undefined,
      });
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/members") {
        return Response.json(members);
      }
      if (url === "/api/v1/users") {
        return Response.json([
          { id: "ua", name: "Ua", role: "user" },
          { id: "pa", name: "Pa", role: "user" },
        ]);
      }
      if (url === "/api/v1/projects/default/members/ua") {
        members = [...members, { id: "ua", name: "Ua", role: "admin" }];
        return Response.json(members[members.length - 1]);
      }
      if (url === "/api/v1/projects/default/members/pa") {
        if (options?.method === "DELETE") {
          members = members.filter((member) => member.id !== "pa");
          return new Response(null, { status: 204 });
        }
        members = members.map((member) =>
          member.id === "pa" ? { ...member, role: "member" } : member,
        );
        return Response.json(members[0]);
      }
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));
  expect(await screen.findByText("Pa")).toBeTruthy();

  await chooseOption("选择用户", "Ua (用户)");
  await chooseOption("项目角色", "项目管理员");
  fireEvent.click(screen.getByRole("button", { name: "添加成员" }));
  expect(await screen.findByText("Ua")).toBeTruthy();
  expect(
    calls.find(
      (call) =>
        call.url === "/api/v1/projects/default/members/ua" &&
        call.method === "PUT",
    )?.body,
  ).toBe(JSON.stringify({ role: "admin" }));

  await chooseOption("项目角色: Pa", "成员");
  await waitFor(() =>
    expect(
      calls.some(
        (call) =>
          call.url === "/api/v1/projects/default/members/pa" &&
          call.method === "PUT",
      ),
    ).toBe(true),
  );

  fireEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
  fireEvent.click(await screen.findByRole("button", { name: "确认移除" }));
  await waitFor(() =>
    expect(
      calls.find(
        (call) =>
          call.url === "/api/v1/projects/default/members/pa" &&
          call.method === "DELETE",
      ),
    ).toBeTruthy(),
  );
});

test("disables demoting or removing the last project admin", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/members")
        return Response.json([{ id: "pa", name: "Pa", role: "admin" }]);
      if (url === "/api/v1/users") return Response.json([]);
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));
  const role = await screen.findByLabelText("项目角色: Pa");
  expect((role as HTMLInputElement).disabled).toBe(true);
  expect(
    (screen.getByRole("button", { name: "移除" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    screen.getAllByTitle("不能移除或降级最后一个项目管理员").length,
  ).toBeGreaterThan(0);
});
