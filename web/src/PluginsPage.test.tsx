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
                mode: { type: "string", enum: ["a", "b"] },
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
  expect((screen.getByLabelText("count") as HTMLInputElement).value).toBe("2");
  expect(screen.getByLabelText("active")).toBeTruthy();
  expect(screen.getByLabelText("mode")).toBeTruthy();
  expect(screen.getByLabelText("tags")).toBeTruthy();
  fireEvent.click(screen.getAllByText("Save")[0]);
  expect(await screen.findByText("Name is required")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("name"), {
    target: { value: "valid" },
  });
  fireEvent.click(screen.getByLabelText("Enabled"));
  await waitFor(() => expect(requests.at(-1)?.enabled).toBe(true));
  expect(requests.at(-1)?.parameters).toEqual({ name: "valid", count: 2 });
});
