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
    },
    {
      id: "ossie",
      name: "Apache Ossie",
      project_types: ["warehouse"],
      enabled: false,
      parameters: {},
      parameters_schema: {
        type: "object",
        properties: { path: { type: "string", default: "ossie" } },
      },
      compatible: true,
      reason: null,
    },
  ];
}

function dtbPullerPlugin() {
  return {
    id: "dtb",
    version: "1",
    display_name: "DTB",
    secret_keys: ["token"],
    parameters_schema: {
      type: "object",
      properties: {
        url: { type: "string", format: "uri", title: "DTB 服务地址" },
        token: { type: "string", format: "password", title: "DTB 访问凭据" },
        max_tables: { type: "integer", title: "最大拉取表数" },
      },
    },
  };
}

function samplePullInstance(overrides: Record<string, unknown> = {}) {
  return {
    id: "dtb-main",
    project_id: "default",
    plugin_id: "dtb",
    display_name: "主数仓 DTB",
    enabled: true,
    parameters: { url: "https://dtb.example" },
    configured_secrets: ["token"],
    secret_configured: true,
    schedule_cron: "30 2 * * *",
    timezone: "Asia/Shanghai",
    catalog_prefix: "hive_prod",
    task_namespace: "prod_etl",
    drop_threshold: 0.5,
    last_table_count: 120,
    last_triggered_at: created,
    created_at: created,
    updated_at: created,
    ...overrides,
  };
}

function sampleExecutors() {
  return [
    {
      id: "dtb",
      name: "DTB",
      enabled: false,
      parameters: {},
      parameters_schema: {
        type: "object",
        properties: {
          base_url: { type: "string", format: "uri", title: "DTB 地址" },
          row_limit: { type: "integer", default: 1000, title: "返回行数上限" },
        },
      },
      compatible: true,
      reason: null,
    },
    {
      id: "doris",
      name: "Doris",
      enabled: false,
      parameters: {},
      parameters_schema: {
        type: "object",
        properties: {
          endpoint: { type: "string", title: "Doris 地址" },
        },
      },
      compatible: true,
      reason: null,
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
  const ossie = screen.getByRole("checkbox", { name: "Apache Ossie" });
  expect((ossie as HTMLInputElement).disabled).toBe(true);
  expect(
    (screen.getByRole("checkbox", { name: "Markdown" }) as HTMLInputElement)
      .disabled,
  ).toBe(false);
});

test("saves enabled source adapters without legacy pull payloads", async () => {
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
      if (url === "/api/v1/puller-plugins") return Response.json([]);
      if (url === "/api/v1/projects/default/pull-instances")
        return Response.json([]);
      if (url === "/api/v1/projects/default/executors")
        return Response.json([]);
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  fireEvent.click(
    await screen.findByRole("checkbox", { name: "Apache Ossie" }),
  );
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
    const ossie = body.adapters.find((row) => row.id === "ossie");
    expect(ossie?.enabled).toBe(true);
    expect(ossie).not.toHaveProperty("pull");
  });
});

test("renders adapters, puller instances and executors read-only for project members", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/v1/projects")
        return Response.json([
          projectRow({ id: "default", name: "业务数据", my_role: "member" }),
        ]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/puller-plugins")
        return Response.json([dtbPullerPlugin()]);
      if (url === "/api/v1/projects/default/pull-instances")
        return Response.json([samplePullInstance()]);
      if (url === "/api/v1/projects/default/executors")
        return Response.json(sampleExecutors());
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
  expect(await screen.findByText("主数仓 DTB")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "新增拉取器实例" })).toBeNull();
  expect(screen.queryByRole("button", { name: "保存查询执行器" })).toBeNull();
  const dtbExec = screen.getByRole("checkbox", { name: "DTB" });
  expect((dtbExec as HTMLInputElement).disabled).toBe(true);
});

test("manages puller instances CRUD with JSON Schema form and *** secret masking", async () => {
  let instances = [samplePullInstance()];
  const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const parsedBody = options?.body
        ? JSON.parse(String(options.body))
        : undefined;
      calls.push({ url, method: options?.method, body: parsedBody });
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url === "/api/v1/puller-plugins")
        return Response.json([dtbPullerPlugin()]);
      if (url === "/api/v1/projects/default/executors")
        return Response.json(sampleExecutors());
      if (url === "/api/v1/projects/default/pull-instances") {
        if (options?.method === "POST") {
          const createdInst = samplePullInstance({
            id: parsedBody.id,
            display_name: parsedBody.display_name,
            schedule_cron: "30 2 * * *",
            catalog_prefix: parsedBody.catalog_prefix,
            task_namespace: parsedBody.task_namespace,
            drop_threshold: parsedBody.drop_threshold,
            parameters: { url: parsedBody.parameters.url },
          });
          instances = [...instances, createdInst];
          return Response.json(createdInst, { status: 201 });
        }
        return Response.json(instances);
      }
      if (url === "/api/v1/projects/default/pull-instances/dtb-main") {
        if (options?.method === "PUT") {
          instances = instances.map((item) =>
            item.id === "dtb-main"
              ? {
                  ...item,
                  display_name: parsedBody.display_name,
                  secret_configured:
                    parsedBody.parameters?.token === ""
                      ? false
                      : item.secret_configured,
                  configured_secrets:
                    parsedBody.parameters?.token === ""
                      ? []
                      : item.configured_secrets,
                }
              : item,
          );
          return Response.json(instances[0]);
        }
        if (options?.method === "DELETE") {
          instances = instances.filter((item) => item.id !== "dtb-main");
          return new Response(null, { status: 204 });
        }
      }
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const section = await screen.findByTestId("puller-instances-section");
  expect(within(section).getByText("dtb-main")).toBeTruthy();
  expect(within(section).getByText("主数仓 DTB")).toBeTruthy();
  expect(within(section).getByText("hive_prod / prod_etl")).toBeTruthy();
  expect(within(section).getByText("已配置")).toBeTruthy();

  // Create new puller instance
  fireEvent.click(
    within(section).getByRole("button", { name: "新增拉取器实例" }),
  );
  const createModal = await screen.findByRole("dialog");
  fireEvent.change(within(createModal).getByLabelText("实例标识"), {
    target: { value: "dtb-dim" },
  });
  fireEvent.change(within(createModal).getByLabelText("显示名称"), {
    target: { value: "维度拉取" },
  });
  fireEvent.change(within(createModal).getByLabelText("定时规则"), {
    target: { value: "02:30" },
  });
  fireEvent.change(within(createModal).getByLabelText("Catalog 前缀"), {
    target: { value: "hive_dim" },
  });
  fireEvent.change(within(createModal).getByLabelText("任务命名空间"), {
    target: { value: "dim_ns" },
  });
  fireEvent.change(within(createModal).getByLabelText("url"), {
    target: { value: "https://dtb-dim.example" },
  });
  fireEvent.change(within(createModal).getByLabelText("token"), {
    target: { value: "secret-123" },
  });
  fireEvent.click(
    within(createModal).getByRole("button", { name: /^保\s*存$/ }),
  );

  await waitFor(() =>
    expect(
      calls.some(
        (call) =>
          call.url === "/api/v1/projects/default/pull-instances" &&
          call.method === "POST",
      ),
    ).toBe(true),
  );
  const createCall = calls.find(
    (call) =>
      call.url === "/api/v1/projects/default/pull-instances" &&
      call.method === "POST",
  );
  expect(createCall?.body).toMatchObject({
    id: "dtb-dim",
    plugin_id: "dtb",
    display_name: "维度拉取",
    schedule_cron: "02:30",
    timezone: "Asia/Shanghai",
    catalog_prefix: "hive_dim",
    task_namespace: "dim_ns",
    drop_threshold: 0.5,
    parameters: {
      url: "https://dtb-dim.example",
      token: "secret-123",
    },
  });
  expect(await within(section).findByText("dtb-dim")).toBeTruthy();

  // Edit dtb-main preserving *** secret first
  const mainRow = within(section)
    .getByText("dtb-main")
    .closest("tr") as HTMLElement;
  fireEvent.click(within(mainRow).getByRole("button", { name: "编辑" }));
  const editModal = await screen.findByRole("dialog");
  const tokenInput = within(editModal).getByLabelText(
    "token",
  ) as HTMLInputElement;
  expect(tokenInput.value).toBe("***");
  fireEvent.change(within(editModal).getByLabelText("显示名称"), {
    target: { value: "主数仓 DTB v2" },
  });
  fireEvent.click(within(editModal).getByRole("button", { name: /^保\s*存$/ }));
  await waitFor(() =>
    expect(
      calls.some(
        (call) =>
          call.url === "/api/v1/projects/default/pull-instances/dtb-main" &&
          call.method === "PUT",
      ),
    ).toBe(true),
  );
  expect(
    calls.find(
      (call) =>
        call.url === "/api/v1/projects/default/pull-instances/dtb-main" &&
        call.method === "PUT",
    )?.body,
  ).toMatchObject({
    display_name: "主数仓 DTB v2",
    parameters: { url: "https://dtb.example", token: "***" },
  });

  // Edit dtb-main and explicitly clear token -> sends ""
  const updatedMainRow = (
    await within(section).findByText("主数仓 DTB v2")
  ).closest("tr") as HTMLElement;
  fireEvent.click(within(updatedMainRow).getByRole("button", { name: "编辑" }));
  const clearModal = await screen.findByRole("dialog");
  fireEvent.change(within(clearModal).getByLabelText("token"), {
    target: { value: "" },
  });
  fireEvent.click(
    within(clearModal).getByRole("button", { name: /^保\s*存$/ }),
  );
  await waitFor(() =>
    expect(
      calls.filter(
        (call) =>
          call.url === "/api/v1/projects/default/pull-instances/dtb-main" &&
          call.method === "PUT",
      ),
    ).toHaveLength(2),
  );
  const clearCall = calls
    .filter(
      (call) =>
        call.url === "/api/v1/projects/default/pull-instances/dtb-main" &&
        call.method === "PUT",
    )
    .at(-1);
  expect(clearCall?.body).toMatchObject({
    parameters: { url: "https://dtb.example", token: "" },
  });

  // Delete dtb-main with confirmation modal
  fireEvent.click(within(updatedMainRow).getByRole("button", { name: "删除" }));
  const delModal = await screen.findByRole("dialog");
  fireEvent.click(within(delModal).getByRole("button", { name: /^删\s*除$/ }));
  await waitFor(() =>
    expect(
      calls.some(
        (call) =>
          call.url === "/api/v1/projects/default/pull-instances/dtb-main" &&
          call.method === "DELETE",
      ),
    ).toBe(true),
  );
});

test("configures project-level query executors with mutual exclusion and disabled hint", async () => {
  const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
  let executors = sampleExecutors();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const parsedBody = options?.body
        ? JSON.parse(String(options.body))
        : undefined;
      calls.push({ url, method: options?.method, body: parsedBody });
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url === "/api/v1/puller-plugins") return Response.json([]);
      if (url === "/api/v1/projects/default/pull-instances")
        return Response.json([]);
      if (url === "/api/v1/projects/default/executors") {
        if (options?.method === "PUT") {
          executors = executors.map((item) => {
            const next = (
              parsedBody.executors as Array<{
                id: string;
                enabled: boolean;
                parameters: Record<string, unknown>;
              }>
            ).find((e) => e.id === item.id);
            return next
              ? { ...item, enabled: next.enabled, parameters: next.parameters }
              : item;
          });
        }
        return Response.json(executors);
      }
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const execSection = await screen.findByTestId("executors-section");
  expect(
    within(execSection).getByText(
      "未启用查询执行器时仅支持知识问答，不开放取数",
    ),
  ).toBeTruthy();

  const dtbBox = within(execSection).getByRole("checkbox", {
    name: "DTB",
  }) as HTMLInputElement;
  const dorisBox = within(execSection).getByRole("checkbox", {
    name: "Doris",
  }) as HTMLInputElement;
  fireEvent.click(dtbBox);
  expect(dtbBox.checked).toBe(true);
  expect(
    within(execSection).queryByText(
      "未启用查询执行器时仅支持知识问答，不开放取数",
    ),
  ).toBeNull();
  fireEvent.change(within(execSection).getByLabelText("base_url"), {
    target: { value: "https://dtb-exec.example" },
  });

  // Toggling Doris turns off DTB automatically (at most 1 enabled)
  fireEvent.click(dorisBox);
  expect(dorisBox.checked).toBe(true);
  expect(dtbBox.checked).toBe(false);
  fireEvent.click(dtbBox);
  expect(dtbBox.checked).toBe(true);
  expect(dorisBox.checked).toBe(false);

  fireEvent.click(
    within(execSection).getByRole("button", { name: "保存查询执行器" }),
  );
  await waitFor(() =>
    expect(
      calls.some(
        (call) =>
          call.url === "/api/v1/projects/default/executors" &&
          call.method === "PUT",
      ),
    ).toBe(true),
  );
  const put = calls.find(
    (call) =>
      call.url === "/api/v1/projects/default/executors" &&
      call.method === "PUT",
  );
  expect(put?.body).toEqual({
    executors: [
      {
        id: "dtb",
        enabled: true,
        parameters: { base_url: "https://dtb-exec.example" },
      },
    ],
  });
});

test("omits unconfigured disabled executors while sending previously-enabled executors on switch and disable", async () => {
  const puts: Array<{
    executors: Array<{
      id: string;
      enabled: boolean;
      parameters: Record<string, unknown>;
    }>;
  }> = [];
  const [dtbExec, dorisExec] = sampleExecutors();
  let executors: Array<{
    id: string;
    name: string;
    enabled: boolean;
    parameters: Record<string, unknown>;
    parameters_schema: Record<string, unknown>;
    compatible: boolean;
    reason: string | null;
  }> = [
    {
      ...dtbExec,
      parameters_schema: {
        ...dtbExec.parameters_schema,
        required: ["base_url"],
      },
    },
    dorisExec,
  ];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const parsedBody = options?.body
        ? JSON.parse(String(options.body))
        : undefined;
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url === "/api/v1/puller-plugins") return Response.json([]);
      if (url === "/api/v1/projects/default/pull-instances")
        return Response.json([]);
      if (url === "/api/v1/projects/default/executors") {
        if (options?.method === "PUT") {
          puts.push(parsedBody);
          executors = executors.map((item) => {
            const next = (
              parsedBody.executors as Array<{
                id: string;
                enabled: boolean;
                parameters: Record<string, unknown>;
              }>
            ).find((e) => e.id === item.id);
            return next
              ? { ...item, enabled: next.enabled, parameters: next.parameters }
              : item;
          });
        }
        return Response.json(executors);
      }
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const execSection = await screen.findByTestId("executors-section");
  const dtbBox = within(execSection).getByRole("checkbox", {
    name: "DTB",
  }) as HTMLInputElement;
  const dorisBox = within(execSection).getByRole("checkbox", {
    name: "Doris",
  }) as HTMLInputElement;
  const saveBtn = within(execSection).getByRole("button", {
    name: "保存查询执行器",
  });

  // 1. Saving with all unconfigured executors disabled sends empty array (omits unconfigured DTB & Doris)
  fireEvent.click(saveBtn);
  await waitFor(() => expect(puts).toHaveLength(1));
  expect(puts[0]).toEqual({ executors: [] });

  // 2. Enabling only Doris while DTB is unconfigured & disabled omits DTB
  fireEvent.click(dorisBox);
  fireEvent.change(within(execSection).getByLabelText("endpoint"), {
    target: { value: "https://doris.example" },
  });
  fireEvent.click(saveBtn);
  await waitFor(() => expect(puts).toHaveLength(2));
  expect(puts[1]).toEqual({
    executors: [
      {
        id: "doris",
        enabled: true,
        parameters: { endpoint: "https://doris.example" },
      },
    ],
  });

  // 3. Switching from server-saved Doris to DTB sends previously-enabled Doris as enabled: false
  fireEvent.click(dtbBox);
  fireEvent.change(within(execSection).getByLabelText("base_url"), {
    target: { value: "https://dtb-exec.example" },
  });
  fireEvent.click(saveBtn);
  await waitFor(() => expect(puts).toHaveLength(3));
  expect(puts[2]).toEqual({
    executors: [
      {
        id: "dtb",
        enabled: true,
        parameters: { base_url: "https://dtb-exec.example" },
      },
      {
        id: "doris",
        enabled: false,
        parameters: { endpoint: "https://doris.example" },
      },
    ],
  });

  // 4. Disabling DTB sends DTB with enabled: false and omits Doris (no longer server-saved enabled)
  fireEvent.click(dtbBox);
  fireEvent.click(saveBtn);
  await waitFor(() => expect(puts).toHaveLength(4));
  expect(puts[3]).toEqual({
    executors: [
      {
        id: "dtb",
        enabled: false,
        parameters: { base_url: "https://dtb-exec.example" },
      },
    ],
  });
});

test("displays FastAPI { loc, msg } 422 validation errors in puller instance modal alert", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url === "/api/v1/puller-plugins")
        return Response.json([dtbPullerPlugin()]);
      if (url === "/api/v1/projects/default/executors")
        return Response.json(sampleExecutors());
      if (url === "/api/v1/projects/default/pull-instances") {
        if (options?.method === "POST") {
          return Response.json(
            {
              detail: [
                {
                  loc: ["body", "display_name"],
                  msg: "String should have at least 1 character",
                },
              ],
            },
            { status: 422 },
          );
        }
        return Response.json([]);
      }
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const section = await screen.findByTestId("puller-instances-section");
  fireEvent.click(
    within(section).getByRole("button", { name: "新增拉取器实例" }),
  );
  const modal = await screen.findByRole("dialog");
  fireEvent.click(within(modal).getByRole("button", { name: /^保\s*存$/ }));

  const alert = await within(modal).findByRole("alert");
  expect(alert.textContent).toContain(
    "body.display_name: String should have at least 1 character",
  );
});

test("hides puller instances and executors on document projects and enforces archived read-only", async () => {
  const fetchedUrls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      fetchedUrls.push(url);
      if (url === "/api/v1/projects")
        return Response.json([
          projectRow({ id: "docs", name: "Platform Docs", type: "document" }),
          projectRow({
            id: "arch",
            name: "归档数仓",
            type: "warehouse",
            status: "archived",
          }),
        ]);
      if (url.endsWith("/adapters")) return Response.json(adapterRows());
      if (url.endsWith("/members")) return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url === "/api/v1/puller-plugins")
        return Response.json([dtbPullerPlugin()]);
      if (url === "/api/v1/projects/arch/pull-instances")
        return Response.json([samplePullInstance({ project_id: "arch" })]);
      if (url === "/api/v1/projects/arch/executors")
        return Response.json(sampleExecutors());
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  const docsRow = (await screen.findByText("Platform Docs")).closest(
    "tr",
  ) as HTMLElement;
  fireEvent.click(within(docsRow).getByRole("button", { name: "项目成员" }));

  await screen.findByText("来源适配器");
  expect(screen.queryByTestId("puller-instances-section")).toBeNull();
  expect(screen.queryByTestId("executors-section")).toBeNull();
  expect(
    fetchedUrls.some(
      (url) => url.includes("/pull-instances") || url.includes("/executors"),
    ),
  ).toBe(false);

  // Return to project list and open archived warehouse project
  fireEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
  const archRow = (await screen.findByText("归档数仓")).closest(
    "tr",
  ) as HTMLElement;
  fireEvent.click(within(archRow).getByRole("button", { name: "项目成员" }));

  expect(await screen.findByText("项目已归档")).toBeTruthy();
  const archPullSection = await screen.findByTestId("puller-instances-section");
  expect(
    (
      within(archPullSection).getByRole("button", {
        name: "新增拉取器实例",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      within(archPullSection).getByRole("button", {
        name: "编辑",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  const archExecSection = screen.getByTestId("executors-section");
  expect(
    (
      within(archExecSection).getByRole("checkbox", {
        name: "DTB",
      }) as HTMLInputElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      within(archExecSection).getByRole("button", {
        name: "保存查询执行器",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

test("drops stale pull-instances and executors responses on rapid project switch", async () => {
  let resolveAlphaPull: ((res: Response) => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url === "/api/v1/projects") {
        return Promise.resolve(
          Response.json([
            projectRow({ id: "alpha", name: "Alpha Warehouse" }),
            projectRow({ id: "beta", name: "Beta Warehouse" }),
          ]),
        );
      }
      if (url.endsWith("/adapters")) return Promise.resolve(Response.json([]));
      if (url.endsWith("/members")) return Promise.resolve(Response.json([]));
      if (url === "/api/v1/users") return Promise.resolve(Response.json([]));
      if (url === "/api/v1/puller-plugins")
        return Promise.resolve(Response.json([dtbPullerPlugin()]));
      if (url === "/api/v1/projects/alpha/pull-instances") {
        return new Promise<Response>((resolve) => {
          resolveAlphaPull = resolve;
        });
      }
      if (url === "/api/v1/projects/beta/pull-instances") {
        return Promise.resolve(
          Response.json([
            samplePullInstance({
              id: "beta-inst",
              project_id: "beta",
              display_name: "Beta 实例",
            }),
          ]),
        );
      }
      if (url.endsWith("/executors")) {
        return Promise.resolve(Response.json(sampleExecutors()));
      }
      return Promise.resolve(Response.json({}));
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  const alphaRow = (await screen.findByText("Alpha Warehouse")).closest(
    "tr",
  ) as HTMLElement;
  fireEvent.click(within(alphaRow).getByRole("button", { name: "项目成员" }));
  await waitFor(() => expect(resolveAlphaPull).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
  const betaRow = (await screen.findByText("Beta Warehouse")).closest(
    "tr",
  ) as HTMLElement;
  fireEvent.click(within(betaRow).getByRole("button", { name: "项目成员" }));

  expect(await screen.findByText("Beta 实例")).toBeTruthy();

  resolveAlphaPull?.(
    Response.json([
      samplePullInstance({
        id: "alpha-stale",
        project_id: "alpha",
        display_name: "Alpha 迟到实例",
      }),
    ]),
  );
  await waitFor(() => {
    expect(screen.queryByText("Alpha 迟到实例")).toBeNull();
    expect(screen.getByText("Beta 实例")).toBeTruthy();
  });
});

test("exports Ossie YAML and JSON from active warehouse project detail and surfaces 409 errors", async () => {
  const urls: string[] = [];
  const createObjectURL = vi.fn(() => "blob:ossie-url");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
  let failExport = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (url.startsWith("/api/v1/projects/default/export/ossie")) {
        if (failExport) {
          return Response.json(
            { detail: "当前知识代暂无可导出的物理表" },
            { status: 409 },
          );
        }
        return new Response("version: 0.3.0.dev0\n", { status: 200 });
      }
      return Response.json({});
    }),
  );
  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const yamlBtn = await screen.findByRole("button", {
    name: "导出 Ossie (YAML)",
  });
  const jsonBtn = screen.getByRole("button", { name: "导出 Ossie (JSON)" });
  fireEvent.click(yamlBtn);
  await waitFor(() =>
    expect(urls).toContain("/api/v1/projects/default/export/ossie?format=yaml"),
  );
  expect(createObjectURL).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:ossie-url");

  fireEvent.click(jsonBtn);
  await waitFor(() =>
    expect(urls).toContain("/api/v1/projects/default/export/ossie?format=json"),
  );

  failExport = true;
  fireEvent.click(yamlBtn);
  expect(await screen.findByText("当前知识代暂无可导出的物理表")).toBeTruthy();
});

test("admin can view and toggle correction and evolution review policy switches", async () => {
  const requests: { url: string; method: string; body: unknown }[] = [];
  let policy = {
    correction_require_review: true,
    evolution_require_review: false,
  };
  let failPut = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/projects") return Response.json([projectRow()]);
      if (url === "/api/v1/projects/default/adapters")
        return Response.json(adapterRows());
      if (url === "/api/v1/projects/default/members") return Response.json([]);
      if (url === "/api/v1/users") return Response.json([]);
      if (
        url === "/api/v1/projects/default/evolution-policy" &&
        options?.method === "PUT"
      ) {
        const body = JSON.parse(String(options.body));
        requests.push({ url, method: "PUT", body });
        if (failPut) {
          return Response.json({ detail: "项目已归档" }, { status: 409 });
        }
        policy = { ...policy, ...body };
        return Response.json(policy);
      }
      if (url === "/api/v1/projects/default/evolution-policy")
        return Response.json(policy);
      return Response.json({});
    }),
  );

  render(<ProjectsPage token="admin" messages={getMessages("zh")} />);
  fireEvent.click(await screen.findByRole("button", { name: "项目成员" }));

  const corrSwitch = await screen.findByRole("switch", {
    name: "人工纠错需要审核",
  });
  const evoSwitch = screen.getByRole("switch", {
    name: "知识演化需要审核",
  });
  expect(corrSwitch.getAttribute("aria-checked")).toBe("true");
  expect(evoSwitch.getAttribute("aria-checked")).toBe("false");

  fireEvent.click(evoSwitch);
  await waitFor(() =>
    expect(requests).toContainEqual({
      url: "/api/v1/projects/default/evolution-policy",
      method: "PUT",
      body: { evolution_require_review: true },
    }),
  );
  await waitFor(() =>
    expect(evoSwitch.getAttribute("aria-checked")).toBe("true"),
  );

  failPut = true;
  fireEvent.click(corrSwitch);
  expect(await screen.findByText("项目已归档")).toBeTruthy();
  expect(corrSwitch.getAttribute("aria-checked")).toBe("true");
});
