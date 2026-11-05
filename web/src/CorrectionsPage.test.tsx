// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { CorrectionsPage } from "./CorrectionsPage";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test.each(["approve", "edited", "reject"])(
  "review %s uses the correct endpoint and patch",
  async (action) => {
    const fetcher = vi.fn(async (_url: string, options?: RequestInit) =>
      options?.method === "POST"
        ? Response.json({})
        : Response.json([
            {
              id: "p1",
              target: "document:a",
              reason: "wrong",
              diff: "-old\n+new",
              patch: "new",
              status: "pending",
            },
          ]),
    );
    vi.stubGlobal("fetch", fetcher);
    render(<CorrectionsPage token="token" messages={getMessages("en")} />);
    fireEvent.click(await screen.findByRole("button", { name: /document:a/ }));
    expect(screen.getByText("-old +new")).toBeTruthy();
    if (action === "edited")
      fireEvent.change(screen.getByLabelText("Edited content"), {
        target: { value: "edited" },
      });
    fireEvent.click(
      screen.getByRole("button", {
        name:
          action === "edited"
            ? "Approve with edits"
            : action === "approve"
              ? "Approve"
              : "Reject",
      }),
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          ([url, options]) =>
            url ===
              `/api/v1/corrections/p1/${action === "reject" ? "reject" : "approve"}` &&
            options?.method === "POST" &&
            (action !== "edited" ||
              options.body === JSON.stringify({ patch: "edited" })),
        ),
      ).toBe(true),
    );
  },
);

test("knowledge gaps show question and missing information", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json([
        { id: "g", question: "Which metric?", missing: "Definition" },
      ]),
    ),
  );
  render(<CorrectionsPage token="token" gaps messages={getMessages("en")} />);
  expect(await screen.findByText("Which metric?")).toBeTruthy();
  expect(screen.getByText("Definition")).toBeTruthy();
});
