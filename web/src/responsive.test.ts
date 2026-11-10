/// <reference types="node" />
import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";

const css = readFileSync(new URL("./workbench.css", import.meta.url), "utf8");
const narrowStart = css.indexOf("@media (max-width: 768px)");
const narrow =
  narrowStart === -1
    ? ""
    : css.slice(css.indexOf("{", narrowStart) + 1, css.length);

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
    const drawerOpen = declarations(".side-nav.side-nav--drawer-open");
    expect(drawerOpen).toContain("transform: translateX(0);");
    expect(drawerOpen).toContain("visibility: visible;");
    expect(declarations(".sidebar-scrim")).toContain("position: fixed;");
    expect(declarations(".sidebar-scrim")).toContain("inset: 0;");
  });

  test("content keeps a 16px side gutter and tables scroll inside themselves", () => {
    const content = declarations(".page-content");
    expect(content).toContain("padding: 16px;");
    expect(declarations(".page-content > *")).toContain("min-width: 0;");
    expect(declarations(".ant-table-content")).toContain("overflow-x: auto;");
  });

  test("table cells keep their text on one line inside a horizontal scroller", () => {
    for (const selector of [
      ".ant-table-wrapper .ant-table-thead > tr > th",
      ".ant-table-wrapper .ant-table-tbody > tr > td",
    ]) {
      const cell = declarations(selector);
      expect(cell).toContain("white-space: nowrap;");
      expect(cell).toContain("max-width: 320px;");
      expect(cell).toContain("overflow: hidden;");
      expect(cell).toContain("text-overflow: ellipsis;");
    }
    const table = declarations(".ant-table-wrapper .ant-table-content > table");
    expect(table).toContain("width: max-content;");
    expect(table).toContain("min-width: 100%;");
  });

  test("chat data tables keep cells on one line inside their own scrollers", () => {
    for (const selector of [
      ".query-result-preview th",
      ".query-result-preview td",
      ".context-schema .schema-table th",
      ".context-schema .schema-table td",
    ]) {
      const cell = declarations(selector);
      expect(cell).toContain("white-space: nowrap;");
      expect(cell).toContain("max-width: 320px;");
      expect(cell).toContain("overflow: hidden;");
      expect(cell).toContain("text-overflow: ellipsis;");
    }
    expect(declarations(".query-result-preview table")).toContain(
      "width: max-content;",
    );
    expect(declarations(".context-schema .schema-table")).toContain(
      "width: max-content;",
    );
    expect(declarations(".context-schema-body")).toContain("overflow-x: auto;");
  });

  test("the knowledge field table keeps cells on one line and scrolls itself", () => {
    for (const selector of [
      ".admin-page .knowledge-table-scroll .data-table th",
      ".admin-page .knowledge-table-scroll .data-table td",
    ]) {
      expect(declarations(selector)).toContain("white-space: nowrap;");
    }
    expect(
      declarations(
        ".admin-page .knowledge-table-scroll .data-table td.field-comment",
      ),
    ).toContain("max-width: 260px;");
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
