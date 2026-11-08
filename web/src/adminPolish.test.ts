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
      /\.admin-page \.card-title,\s*\.admin-page \.detail-section-title,\s*\.admin-page \.ant-card-head-title,\s*\.admin-page \.agent-card-head h3,\s*\.admin-page \.search-result h3,\s*\.admin-page \.gap-card h3,\s*\.admin-page \.plugin-card h3,\s*\.admin-page label,\s*\.admin-page \.field-label\s*\{[^}]*font-family: var\(--font-sans\);/,
    );
    expect(css).toMatch(
      /\.admin-page \.card-title,\s*\.admin-page \.detail-section-title,\s*\.admin-page \.agent-card-head h3,\s*\.admin-page \.search-result h3,\s*\.admin-page \.gap-card h3,\s*\.admin-page \.plugin-card h3\s*\{[^}]*font-size: 15px;[^}]*font-weight: 600;/,
    );
    expect(css).toMatch(
      /\.page-content:has\(> \.config-section\.admin-page\) \.card-title,\s*\.page-content:has\(> \.config-section\.admin-page\) \.ant-card-head-title,\s*\.page-content:has\(> \.config-section\.admin-page\) label,\s*\.page-content:has\(> \.config-section\.admin-page\) \.field-label\s*\{[^}]*font-family: var\(--font-sans\);/,
    );
  });

  test("form rows do not turn checkbox wrappers into two-column grids", () => {
    expect(css).toMatch(
      /\.form-card > label:not\(\.ant-checkbox-wrapper\):not\(\.ant-radio-wrapper\)\s*\{[^}]*display: grid;/,
    );
    expect(css).toMatch(
      /\.admin-page \.form-card > \.ant-checkbox-wrapper\s*\{[^}]*align-self: flex-start;/,
    );
  });

  test("admin scroll areas reserve the status bar height at the bottom", () => {
    expect(css).toMatch(
      /\.workspace-content:has\(> \.status-bar\) > \.page-content:not\(\.page-content--chat\)\s*\{[^}]*padding-bottom: calc\(var\(--status-bar-height\) \+ var\(--admin-space-24\)\);/,
    );
    expect(css).toMatch(/--status-bar-height: 32px;/);
    expect(css).toMatch(/\.status-bar\s*\{[^}]*height: 32px;/);
  });

  test("long knowledge tables scroll horizontally inside their own container", () => {
    expect(css).toMatch(
      /\.admin-page \.knowledge-table-scroll\s*\{[^}]*overflow-x: auto;/,
    );
    expect(css).toMatch(
      /\.admin-page \.knowledge-table-scroll \.data-table\s*\{[^}]*min-width: 760px;/,
    );
    expect(css).toMatch(
      /\.admin-page \.knowledge-table-scroll \.data-table th\.field-name,\s*\.admin-page \.knowledge-table-scroll \.data-table td\.field-name\s*\{[^}]*min-width: 180px;[^}]*white-space: nowrap;/,
    );
    expect(css).toMatch(
      /\.admin-page \.knowledge-table-scroll \.data-table th\.field-type,\s*\.admin-page \.knowledge-table-scroll \.data-table td\.field-type\s*\{[^}]*min-width: 130px;[^}]*white-space: nowrap;/,
    );
  });

  test("admin cards, fields and empty states use one spacing scale", () => {
    for (const size of ["8", "12", "16", "24"]) {
      expect(css).toContain(`--admin-space-${size}: ${size}px;`);
    }
    expect(css).toMatch(
      /\.admin-page \.section-card\s*\{[^}]*padding: var\(--admin-space-16\) var\(--admin-space-24\);/,
    );
    expect(css).toMatch(
      /\.admin-page \.field\s*\{[^}]*gap: var\(--admin-space-8\);/,
    );
    expect(css).toMatch(
      /\.admin-page \.empty-state,\s*\.admin-page \.ant-empty\s*\{[^}]*border: 1px dashed var\(--line\);/,
    );
  });

  test("provider capability checkboxes stay inline with their labels", () => {
    expect(css).toMatch(
      /\.provider-capability-card \.ant-checkbox-wrapper\s*\{[^}]*display: inline-flex;[^}]*flex-direction: row;[^}]*align-items: center;/,
    );
    expect(css).toMatch(
      /\.provider-capability-card \.ant-checkbox \+ span\s*\{[^}]*padding-inline-start: 0;/,
    );
  });
});
