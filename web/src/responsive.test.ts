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
      expect(cell).toContain("max-width: none;");
      expect(cell).toContain("overflow: visible;");
      expect(cell).toContain("text-overflow: clip;");
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
      expect(cell).toContain("max-width: none;");
      expect(cell).toContain("overflow: visible;");
      expect(cell).toContain("text-overflow: clip;");
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
    for (const selector of [
      ".admin-page .knowledge-table-scroll .data-table td.field-comment",
      ".admin-page .knowledge-table-scroll .data-table td.field-dimension",
    ]) {
      const cell = declarations(selector);
      expect(cell).toContain("max-width: none;");
      expect(cell).toContain("overflow: visible;");
      expect(cell).toContain("text-overflow: clip;");
    }
  });

  test("table scrollers expose a visible keyboard focus target", () => {
    for (const selector of [
      ".result-table-scroll:focus-visible",
      ".context-schema-body:focus-visible",
      ".knowledge-table-scroll:focus-visible",
      ".table-scroll-region:focus-within",
    ]) {
      expect(css).toContain(selector);
    }
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

  test("desktop AntD select inputs and correction list targets prevent visual bleed and overflow", () => {
    const desktop = narrowStart === -1 ? css : css.slice(0, narrowStart);
    expect(desktop).toContain(".ant-select .ant-select-input");
    expect(desktop).toContain("background: transparent !important;");
    expect(desktop).toContain(".correction-layout > *");
    expect(desktop).toContain(".correction-list button > .mono");
    expect(desktop).toContain("text-overflow: ellipsis;");
    expect(desktop).toContain(".discovery-candidate-card");
    expect(desktop).toContain(".discovery-evidence-card");
  });

  test("three-pane shell fills 100vw x 100dvh without fixed max-width or outer margins", () => {
    expect(css).not.toMatch(/max-width:\s*1600px/);
    expect(css).toMatch(
      /\.app-shell\s*\{[^}]*width:\s*100vw;[^}]*height:\s*100dvh;/,
    );
    expect(css).toMatch(/body\s*\{[^}]*margin:\s*0;/);
    expect(css).toMatch(/\.primary-rail\s*\{[^}]*width:\s*64px;/);
    expect(css).toMatch(/\.context-pane\s*\{[^}]*width:\s*236px;/);
    expect(css).toMatch(
      /\.context-pane\.context-pane--collapsed\s*\{[^}]*width:\s*0;/,
    );
    expect(css).toMatch(/\.workspace-content\s*\{[^}]*min-width:\s*0;/);
  });

  test("medium screen (769px-1024px) narrows and allows collapsing the middle pane", () => {
    const mediumMatch = css.match(
      /@media\s*\(min-width:\s*769px\)\s*and\s*\(max-width:\s*1024px\)\s*\{([\s\S]*?)\n\}/,
    );
    expect(mediumMatch).not.toBeNull();
    const medium = mediumMatch?.[1] ?? "";
    expect(medium).toContain(".primary-rail");
    expect(medium).toContain(".context-pane");
  });

  test("theme tokens define cool neutral light and dark palettes", () => {
    expect(css).toMatch(/--bg:\s*#eef1f5;/i);
    expect(css).toMatch(/--panel:\s*#ffffff;/i);
    expect(css).toMatch(/--sub:\s*#f7f8fa;/i);
    expect(css).toMatch(/--text:\s*#202733;/i);
    expect(css).toMatch(/--muted:\s*#627083;/i);
    expect(css).toMatch(/--line:\s*#dce2ea;/i);
    expect(css).toMatch(/--accent:\s*#315ac7;/i);
    expect(css).toMatch(/--accent-bg:\s*#edf2ff;/i);
    expect(css).toMatch(
      /:root\[data-theme="dark"\]\s*\{[^}]*--bg:\s*#151b24;/i,
    );
    expect(css).toMatch(
      /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{[^}]*--bg:\s*#151b24;/i,
    );
  });
});
