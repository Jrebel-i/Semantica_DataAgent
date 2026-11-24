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
import { CorrectionDraftDrawer } from "./CorrectionDraftDrawer";
import { getMessages } from "../i18n";

beforeEach(() => {
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
  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true,
    value: class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const providers = [
  {
    id: "prov-1",
    name: "OpenAI",
    type: "openai-completions",
    base_url: "https://api.example.com",
    models: ["gpt-4.1", "gpt-4.1-mini"],
    model_capabilities: {},
    has_api_key: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

test("auto-target mode drafts without pre-selected target, switches provider/model, and shows new-file badge & diff", async () => {
  const requests: { url: string; method?: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ url: input, method: init?.method, body });
      if (input === "/api/v1/providers") return Response.json(providers);
      if (input === "/api/v1/corrections/draft") {
        return Response.json({
          target: "document:knowledge/metrics/refund_rate.md",
          target_kind: "document",
          patch: "# 退款率\n按实付退款金额占比计算。\n",
          structured_patch: null,
          diff: "--- /dev/null\n+++ b/knowledge/metrics/refund_rate.md\n@@ -0,0 +1,2 @@\n+# 退款率\n+按实付退款金额占比计算。",
          gate_report: { passed: true, checks: [], errors: [] },
          resolved_path: "knowledge/metrics/refund_rate.md",
          reason: "新建退款率专题文档",
          source_generated: false,
          is_new_file: true,
          used_llm: true,
          is_duplicate: false,
        });
      }
      return Response.json(
        { id: "prop-new", status: "pending" },
        { status: 201 },
      );
    }),
  );

  render(
    <CorrectionDraftDrawer
      open
      token="token"
      project="default"
      messages={getMessages("zh")}
      onClose={() => {}}
    />,
  );

  const dialog = await screen.findByRole("dialog");
  // Auto-target mode is default; manual target input is hidden until switched
  expect(within(dialog).queryByLabelText("目标引用（可选）")).toBeNull();
  expect(within(dialog).getByText(/无需预先手选表或文档/)).toBeTruthy();

  // Select provider & model
  fireEvent.mouseDown(within(dialog).getByLabelText("归纳模型供应方（可选）"));
  fireEvent.click(await screen.findByTitle("OpenAI"));
  fireEvent.mouseDown(within(dialog).getByLabelText("归纳模型（可选）"));
  fireEvent.click(await screen.findByTitle("gpt-4.1-mini"));

  fireEvent.change(within(dialog).getByLabelText("纠错问题描述"), {
    target: { value: "新增退款率指标说明" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "生成草稿预览" }));

  await waitFor(() =>
    expect(
      requests.some((r) => {
        if (r.url !== "/api/v1/corrections/draft") return false;
        const b = r.body as Record<string, unknown>;
        return (
          b.target === undefined &&
          b.provider_id === "prov-1" &&
          b.model === "gpt-4.1-mini" &&
          b.description === "新增退款率指标说明"
        );
      }),
    ).toBe(true),
  );

  expect(await within(dialog).findByText("新建文档")).toBeTruthy();
  expect(within(dialog).getByText("大模型归纳")).toBeTruthy();
  expect(
    within(dialog).getByText(/目标文件:\s*knowledge\/metrics\/refund_rate\.md/),
  ).toBeTruthy();
});

test("503 llm_unavailable displays actionable notice and lets user switch to manual edit without concatenating raw prose", async () => {
  const requests: { url: string; method?: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ url: input, method: init?.method, body });
      if (input === "/api/v1/providers") return Response.json([]);
      if (input === "/api/v1/corrections/draft") {
        return Response.json(
          {
            detail: "未配置可用大模型，请先配置模型或切换为手工编辑",
            code: "llm_unavailable",
            fallback: "manual_edit",
            manual_edit_recommended: true,
          },
          { status: 503 },
        );
      }
      if (input === "/api/v1/corrections?project=default") {
        return Response.json(
          { id: "prop-manual-1", status: "pending" },
          { status: 201 },
        );
      }
      return Response.json({});
    }),
  );

  const onSubmitted = vi.fn();
  render(
    <CorrectionDraftDrawer
      open
      token="token"
      project="default"
      messages={getMessages("zh")}
      onClose={() => {}}
      onSubmitted={onSubmitted}
    />,
  );

  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("纠错问题描述"), {
    target: { value: "口语纠错描述不应被直接拼接" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "生成草稿预览" }));

  expect(
    await within(dialog).findByText("大模型不可用，无法自动归纳纠错草稿"),
  ).toBeTruthy();
  expect(
    within(dialog).getByText("未配置可用大模型，请先配置模型或切换为手工编辑"),
  ).toBeTruthy();

  // Click switch to manual edit
  fireEvent.click(
    within(dialog).getAllByRole("button", { name: "切换为手工编辑" })[0],
  );
  const targetInput = await within(dialog).findByLabelText("目标引用（可选）");
  const patchTextarea = within(dialog).getByLabelText(
    "替换后的说明",
  ) as HTMLTextAreaElement;
  // Must NEVER silently concatenate raw prose description into patch
  expect(patchTextarea.value).toBe("");

  fireEvent.change(targetInput, { target: { value: "table:shop.orders" } });
  fireEvent.change(patchTextarea, { target: { value: "仅统计已支付订单" } });
  fireEvent.change(within(dialog).getByLabelText("纠错理由"), {
    target: { value: "手工补充表说明" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "提出纠错" }));

  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/api/v1/corrections?project=default" &&
          r.method === "POST" &&
          (r.body as Record<string, unknown>)?.target === "table:shop.orders" &&
          (r.body as Record<string, unknown>)?.patch === "仅统计已支付订单",
      ),
    ).toBe(true),
  );
  expect(onSubmitted).toHaveBeenCalledWith("prop-manual-1");
});
