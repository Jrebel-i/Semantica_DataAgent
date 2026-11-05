// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { McpAccessPage } from "./McpAccessPage";
import { getMessages } from "./i18n";

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  history.replaceState(null, "", "/mcp-access");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalCreateObjectURL) URL.createObjectURL = originalCreateObjectURL;
  else delete (URL as Partial<typeof URL>).createObjectURL;
  if (originalRevokeObjectURL) URL.revokeObjectURL = originalRevokeObjectURL;
  else delete (URL as Partial<typeof URL>).revokeObjectURL;
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: undefined,
  });
});

test("renders masked token, copies secrets, and updates both client examples", async () => {
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const token = "personal-token-value";
  render(<McpAccessPage token={token} messages={getMessages("zh")} />);

  const url = screen.getByLabelText("MCP 连接地址") as HTMLInputElement;
  expect(url.value).toBe(`${window.location.origin}/mcp/`);
  expect(
    within(screen.getByLabelText("当前用户令牌")).queryByText(token),
  ).toBeNull();
  expect(screen.getByLabelText("Claude Code 命令").textContent).not.toContain(
    token,
  );

  fireEvent.click(screen.getByRole("button", { name: "显示令牌" }));
  expect(
    within(screen.getByLabelText("当前用户令牌")).getByText(token),
  ).toBeTruthy();
  expect(screen.getByLabelText("Claude Code 命令").textContent).toContain(
    token,
  );

  fireEvent.click(screen.getByRole("button", { name: "复制令牌" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(token));
  expect(await screen.findByText("令牌已复制")).toBeTruthy();

  fireEvent.change(url, {
    target: { value: "https://data.example.test/mcp/" },
  });
  expect(screen.getByLabelText("Claude Code 命令").textContent).toContain(
    "claude mcp add --transport http semantica-dataagent https://data.example.test/mcp/",
  );
  expect(screen.getByLabelText("Codex config.toml").textContent).toContain(
    'url = "https://data.example.test/mcp/"',
  );
  fireEvent.click(
    screen.getByRole("button", { name: "复制 Claude Code 命令" }),
  );
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(
      'claude mcp add --transport http semantica-dataagent https://data.example.test/mcp/ --header "Authorization: Bearer personal-token-value"',
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "复制 Codex 配置" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith(
      '[mcp_servers.semantica-dataagent]\nurl = "https://data.example.test/mcp/"\nhttp_headers = { "Authorization" = "Bearer personal-token-value" }',
    ),
  );
});

test("falls back to a textarea when the clipboard API is unavailable", async () => {
  const execCommand = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: execCommand,
  });
  render(<McpAccessPage token="fallback-token" messages={getMessages("en")} />);
  fireEvent.click(screen.getByRole("button", { name: "Copy token" }));
  await waitFor(() => expect(execCommand).toHaveBeenCalledWith("copy"));
  expect(await screen.findByText("Token copied")).toBeTruthy();
});

test("downloads the authenticated skill using the attachment filename", async () => {
  const fetcher = vi.fn(
    async () =>
      new Response("# skill", {
        status: 200,
        headers: {
          "Content-Disposition":
            'attachment; filename="dataagent-retrieval-SKILL.md"',
        },
      }),
  );
  vi.stubGlobal("fetch", fetcher);
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:skill"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  let downloaded = "";
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloaded = this.download;
  });

  render(<McpAccessPage token="secret" messages={getMessages("en")} />);
  fireEvent.click(
    screen.getByRole("button", { name: "Download retrieval skill" }),
  );

  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith("/api/v1/skill", {
      headers: { Authorization: "Bearer secret" },
    }),
  );
  await waitFor(() => expect(downloaded).toBe("dataagent-retrieval-SKILL.md"));
  expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:skill");
});
