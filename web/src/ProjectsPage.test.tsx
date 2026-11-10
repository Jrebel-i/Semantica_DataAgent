// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
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
  const { container } = render(
    <ProjectsPage token="admin" messages={getMessages("zh")} />,
  );

  expect(await screen.findByText("Platform Docs")).toBeTruthy();
  await waitFor(() => {
    const table = container.querySelector(".ant-table-content");
    expect(table?.getAttribute("tabindex")).toBe("0");
    expect(table?.getAttribute("role")).toBe("region");
    expect(table?.getAttribute("aria-label")).toBe("项目");
  });
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
      if (url === "/api/v1/projects/default/adapters") return Response.json([]);
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
      if (url === "/api/v1/projects/default/adapters") return Response.json([]);
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

test("archives and restores a project from the management table", async () => {
  const calls: Array<{ url: string; method?: string }> = [];
  let rows = [projectRow()];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === "POST" && url.endsWith("/archive")) {
        calls.push({ url, method: options.method });
        rows = rows.map((row) => ({ ...row, status: "archived" }));
        return Response.json(rows[0]);
      }
      if (options?.method === "POST" && url.endsWith("/restore")) {
        calls.push({ url, method: options.method });
        rows = rows.map((row) => ({ ...row, status: "active" }));
        return Response.json(rows[0]);
      }
      return Response.json(rows);
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  fireEvent.click(await screen.findByRole("button", { name: "归档项目" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/确认归档/)).toBeTruthy();
  fireEvent.click(
    within(dialog).getByRole("button", { name: /归\s*档\s*项\s*目/ }),
  );

  await waitFor(() =>
    expect(calls).toContainEqual({
      url: "/api/v1/projects/default/archive",
      method: "POST",
    }),
  );
  const row = (await screen.findByText("业务数据")).closest("tr");
  expect(row?.className).toContain("project-archived-row");

  fireEvent.click(screen.getByRole("button", { name: "恢复项目" }));
  await waitFor(() =>
    expect(calls).toContainEqual({
      url: "/api/v1/projects/default/restore",
      method: "POST",
    }),
  );
  await waitFor(() =>
    expect(
      (screen.getByText("业务数据").closest("tr") as HTMLElement).className,
    ).not.toContain("project-archived-row"),
  );
});

test("requires the exact project name before permanent deletion", async () => {
  const calls: Array<{ url: string; method?: string; body?: string }> = [];
  let rows = [
    projectRow({ status: "archived" }),
    projectRow({ id: "old", name: "旧项目", status: "archived" }),
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === "POST" && url.endsWith("/delete")) {
        calls.push({
          url,
          method: options.method,
          body: options.body as string | undefined,
        });
        rows = rows.filter((row) => row.id !== "old");
        return new Response(null, { status: 204 });
      }
      return Response.json(rows);
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);

  const defaultRow = (await screen.findByText("业务数据")).closest(
    "tr",
  ) as HTMLElement;
  const defaultDelete = within(defaultRow)
    .getAllByRole("button")
    .find((button) => /永\s*久\s*删\s*除/.test(button.textContent ?? ""));
  expect((defaultDelete as HTMLButtonElement).disabled).toBe(true);
  expect(defaultDelete?.getAttribute("title")).toBe("默认项目不可删除");

  const oldRow = screen.getByText("旧项目").closest("tr") as HTMLElement;
  expect(oldRow.className).toContain("project-archived-row");
  fireEvent.click(
    within(oldRow)
      .getAllByRole("button")
      .find((button) => /永\s*久\s*删\s*除/.test(button.textContent ?? ""))!,
  );

  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog)
    .getAllByRole("button")
    .find((button) => /永\s*久\s*删\s*除/.test(button.textContent ?? ""))!;
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(within(dialog).getByLabelText("名称"), {
    target: { value: "旧" },
  });
  expect(within(dialog).getByRole("alert").textContent).toBe("项目名不匹配");
  expect((confirm as HTMLButtonElement).disabled).toBe(true);

  fireEvent.change(within(dialog).getByLabelText("名称"), {
    target: { value: "旧项目" },
  });
  expect((confirm as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(confirm);

  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toEqual({
    url: "/api/v1/projects/old/delete",
    method: "POST",
    body: JSON.stringify({ name: "旧项目" }),
  });
  await waitFor(() => expect(screen.queryByText("旧项目")).toBeNull());
});

function adapterRows() {
  return [
    {
      id: "markdown",
      name: "Markdown",
      project_types: ["warehouse", "document"],
      enabled: true,
      parameters: {},
      parameters_schema: {
        type: "object",
        properties: { path: { type: "string", default: "knowledge" } },
      },
      compatible: true,
      reason: null,
      pull: null,
    },
    {
      id: "dtb",
      name: "DTB export",
      project_types: ["warehouse"],
      enabled: false,
      parameters: {},
      parameters_schema: { type: "object", properties: {} },
      compatible: true,
      reason: null,
      pull: {
        enabled: false,
        parameters: {},
        parameters_schema: {
          type: "object",
          properties: {
            url: { type: "string" },
            token: { type: "string", format: "password" },
            interval_seconds: { type: "integer" },
          },
        },
        token_configured: false,
      },
    },
  ];
}

test("shows incompatible adapters disabled with a reason", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects")
        return Response.json([
          projectRow({ id: "docs", name: "Platform Docs", type: "document" }),
        ]);
      if (url === "/api/v1/projects/docs/adapters") {
        return Response.json([
          {
            ...adapterRows()[0],
            project_types: ["warehouse", "document"],
          },
          {
            ...adapterRows()[1],
            compatible: false,
            reason: "该适配器仅支持数仓型项目，不能用于文档型项目",
          },
        ]);
      }
      if (url === "/api/v1/projects/docs/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  expect(await screen.findByText("来源适配器")).toBeTruthy();
  expect(
    await screen.findByText("该适配器仅支持数仓型项目，不能用于文档型项目"),
  ).toBeTruthy();
  const dtb = screen.getByRole("checkbox", { name: "DTB export" });
  expect((dtb as HTMLInputElement).disabled).toBe(true);
  expect(
    (screen.getByRole("checkbox", { name: "Markdown" }) as HTMLInputElement)
      .disabled,
  ).toBe(false);
});

test("saves enabled adapters and DTB pull configuration", async () => {
  let rows = adapterRows();
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
      if (url === "/api/v1/projects/default/adapters") {
        if (options?.method === "PUT") {
          const body = JSON.parse(String(options.body)) as {
            adapters: Array<{ id: string; enabled: boolean }>;
          };
          rows = rows.map((row) => ({
            ...row,
            enabled:
              body.adapters.find((item) => item.id === row.id)?.enabled ??
              row.enabled,
          }));
        }
        return Response.json(rows);
      }
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  fireEvent.click(await screen.findByRole("checkbox", { name: "DTB export" }));
  fireEvent.change(await screen.findByLabelText("url"), {
    target: { value: "https://dtb.example" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存适配器配置" }));

  await waitFor(() => {
    const put = calls.find(
      (call) =>
        call.url === "/api/v1/projects/default/adapters" &&
        call.method === "PUT",
    );
    expect(put).toBeTruthy();
    const body = JSON.parse(put?.body ?? "{}") as {
      adapters: Array<Record<string, unknown>>;
    };
    const dtb = body.adapters.find((row) => row.id === "dtb");
    expect(dtb?.enabled).toBe(true);
    expect(dtb?.pull).toEqual({
      enabled: false,
      parameters: { url: "https://dtb.example" },
    });
  });
});

test("renders adapters read-only for project members", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects")
        return Response.json([
          projectRow({ id: "default", name: "业务数据", my_role: "member" }),
        ]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      return Response.json({});
    }),
  );
  render(
    <ProjectsPage
      token="user"
      messages={getMessages("zh")}
      canCreate={false}
      includeMemberProjects
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const markdown = await screen.findByRole("checkbox", { name: "Markdown" });
  expect((markdown as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "保存适配器配置" })).toBeNull();
});
