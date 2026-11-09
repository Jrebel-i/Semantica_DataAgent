/// <reference types="node" />
import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";

const css = readFileSync(new URL("./workbench.css", import.meta.url), "utf8");
const narrowStart = css.indexOf("@media (max-width: 768px)");
const narrow = narrowStart === -1 ? "" : css.slice(narrowStart, css.length);

function declarations(selector: string) {
  for (const match of narrow.matchAll(/(?:^|\n)\s*([^{}]+)\{([^}]*)\}/g)) {
    const selectors = match[1].split(",").map((item) => item.trim());
    if (selectors.includes(selector)) return match[2];
  }
  return "";
}

describe("narrow screen layout", () => {
  test("the mobile top bar sits above the content instead of beside it", () => {
    expect(narrow).not.toBe("");
    expect(declarations(".workspace")).toContain("flex-direction: column;");
    expect(declarations(".mobile-topbar")).toContain("display: flex;");
  });

  test("the sidebar is an overlay drawer with a full-screen scrim", () => {
    const sidebar = declarations(".side-nav");
    expect(sidebar).toContain("position: fixed;");
    expect(sidebar).toContain("transform: translateX(-100%);");
    expect(sidebar).toContain("visibility: hidden;");
    expect(declarations(".side-nav--drawer-open")).toContain(
      "transform: translateX(0);",
    );
    expect(declarations(".sidebar-scrim")).toContain("position: fixed;");
    expect(declarations(".sidebar-scrim")).toContain("inset: 0;");
  });

  test("content keeps a 16px side gutter and tables scroll inside themselves", () => {
    const content = declarations(".page-content");
    expect(content).toContain("padding: 16px;");
    expect(declarations(".page-content > *")).toContain("min-width: 0;");
    expect(declarations(".ant-table-content")).toContain("overflow-x: auto;");
  });

  test("wide multi-column layouts collapse to one column", () => {
    for (const selector of [
      ".source-layout",
      ".build-grid",
      ".correction-layout",
      ".search-form",
    ]) {
      expect(declarations(selector)).toContain(
        "grid-template-columns: minmax(0, 1fr);",
      );
    }
    expect(declarations(".knowledge-layout")).toContain(
      "grid-template-columns: minmax(0, 1fr);",
    );
  });
});
