// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PluginsPage } from "./PluginsPage";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("renders schema, reports validation errors and saves enabled state", async () => {
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === "/api/v1/settings") {
        if (!options?.method)
          return Response.json({ dimension_value_limit: "7" });
        requests.push(
          JSON.parse(String(options.body)) as Record<string, unknown>,
        );
        return Response.json({});
      }
      if (url === "/api/v1/filters")
        return Response.json({
          database_include: [],
          database_exclude: ["tmp", "test"],
          task_include: [],
          task_exclude: [],
        });
      if (!options?.method)
        return Response.json([
          {
            category: "source",
            id: "sample",
            version: "1",
            display_name: "Sample",
            enabled: false,
            parameters: {},
            parameters_schema: {
              type: "object",
              required: ["name"],
              properties: {
                name: { type: "string" },
                count: { type: "integer", default: 2 },
                active: { type: "boolean" },
                mode: { type: "string", enum: [1, 2] },
                tags: { type: "array", items: { type: "string" } },
              },
            },
          },
        ]);
      const body = JSON.parse(String(options.body)) as Record<string, unknown>;
      requests.push(body);
      if (!(body.parameters as Record<string, unknown>).name)
        return Response.json(
          { detail: [{ path: ["name"], message: "Name is required" }] },
          { status: 422 },
        );
      return Response.json(body);
    }),
  );
  render(<PluginsPage token="token" messages={getMessages("en")} />);
  expect(await screen.findByLabelText("name")).toBeTruthy();
  expect(
    (screen.getByLabelText("Dimension value limit") as HTMLInputElement).value,
  ).toBe("7");
  fireEvent.change(screen.getByLabelText("Dimension value limit"), {
    target: { value: "2" },
  });
  fireEvent.click(screen.getAllByText("Save")[0]);
  await waitFor(() =>
    expect(requests).toContainEqual({ values: { dimension_value_limit: "2" } }),
  );
  expect(
    (screen.getByLabelText("Concurrency limit") as HTMLInputElement).value,
  ).toBe("2");
  fireEvent.change(screen.getByLabelText("Concurrency limit"), {
    target: { value: "1" },
  });
  fireEvent.click(screen.getAllByText("Save")[1]);
  await waitFor(() =>
    expect(requests).toContainEqual({
      values: {
        embedding_concurrency: "1",
        embedding_timeout_seconds: "60",
        embedding_timeout_per_input_seconds: "20",
        embedding_retries: "2",
        embedding_backoff_seconds: "1",
      },
    }),
  );
  fireEvent.change(screen.getByLabelText("Session retention days"), {
    target: { value: "12" },
  });
  fireEvent.click(screen.getAllByText("Save").at(-1)!);
  await waitFor(() =>
    expect(requests).toContainEqual({
      values: { session_retention_days: "12" },
    }),
  );
  expect((screen.getByLabelText("count") as HTMLInputElement).value).toBe("2");
  expect(screen.getByLabelText("active")).toBeTruthy();
  expect(screen.getByLabelText("mode")).toBeTruthy();
  expect(screen.getByLabelText("tags")).toBeTruthy();
  fireEvent.click(screen.getAllByText("Save")[2]);
  expect(await screen.findByText("Name is required")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("name"), {
    target: { value: "valid" },
  });
  fireEvent.change(screen.getByLabelText("mode"), { target: { value: "1" } });
  fireEvent.click(screen.getByLabelText("Enabled"));
  await waitFor(() => expect(requests.at(-1)?.enabled).toBe(true));
  expect(requests.at(-1)?.parameters).toEqual({
    name: "valid",
    count: 2,
    mode: 2,
  });
});
