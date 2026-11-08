/// <reference types="node" />
import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";

const css = readFileSync(new URL("./workbench.css", import.meta.url), "utf8");

describe("admin page polish", () => {
  test("page titles use the serif face while card titles and labels stay sans", () => {
    expect(css).toMatch(
      /\.admin-page \.page-title,\s*\.admin-page \.detail-title\s*\{[^}]*font-family: var\(--font-serif\);/,
    );
    expect(css).toMatch(
      /\.admin-page \.card-title,\s*\.admin-page \.detail-section-title,\s*\.admin-page \.ant-card-head-title,\s*\.admin-page \.agent-card-head h3,\s*\.admin-page label,\s*\.admin-page \.field-label\s*\{[^}]*font-family: var\(--font-sans\);/,
    );
  });
});
