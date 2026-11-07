/// <reference types="node" />
import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";

function read(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

function declarations(css: string, selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? ""
  );
}

const css = read("./workbench.css");
const app = read("./App.tsx");
const newsreader = read("./fonts/newsreader.css");
const newsreaderFiles = readdirSync(
  new URL("./fonts/files", import.meta.url),
).filter((file) => file.startsWith("newsreader-"));

describe("typography", () => {
  test("font stacks are centralized in CSS variables", () => {
    const root = declarations(css, ":root").replace(/\s+/g, " ");
    expect(root).toContain(
      '--font-serif: "Newsreader Variable", "Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", "SimSun", serif;',
    );
    expect(root).toContain(
      '--font-sans: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;',
    );
    expect(root).toContain("--font-mono:");
    expect(root).not.toContain("--mono:");
    expect(app).toContain('fontFamily: "var(--font-sans)"');
  });

  test("assistant answers use the serif body with sans tables and mono code", () => {
    const markdown = declarations(css, ".markdown");
    expect(markdown).toContain("font-family: var(--font-serif);");
    expect(markdown).toContain("font-size: 16px;");
    expect(markdown).toContain("line-height: 1.7;");
    expect(declarations(css, ".markdown p")).toContain("margin: 0 0 0.9em;");
    expect(css).toMatch(
      /\.markdown h1,\s*\.markdown h2,\s*\.markdown h3,\s*\.markdown h4\s*\{[^}]*font-family: var\(--font-serif\)[^}]*font-weight: 600;/,
    );
    expect(declarations(css, ".md-table")).toContain(
      "font-family: var(--font-sans);",
    );
    expect(declarations(css, ".markdown code")).toContain("var(--font-mono)");
    expect(declarations(css, ".sql-block pre")).toContain("var(--font-mono)");
  });

  test("composer selectors look interactive", () => {
    const select = declarations(
      css,
      ".composer-controls .chat-selectors select",
    );
    expect(select).toContain("appearance: none;");
    expect(select).toContain('background-image: url("data:image/svg+xml,');
    expect(select).toContain("cursor: pointer;");
    expect(
      declarations(
        css,
        ".composer-controls .chat-selectors select:not(:disabled):hover",
      ),
    ).toContain("background-color: var(--panel);");
  });

  test("Newsreader ships only the latin subset and no external font URLs", () => {
    expect(newsreader.match(/@font-face/g)).toHaveLength(2);
    const urls = [...newsreader.matchAll(/url\('([^']+)'\)/g)].map(
      (match) => match[1],
    );
    expect(urls).toHaveLength(2);
    expect(urls.every((url) => url.includes("newsreader-latin-wght-"))).toBe(
      true,
    );
    expect(newsreader).not.toMatch(
      /latin-ext|vietnamese|cyrillic|japanese|korean|chinese-simplified/,
    );
    expect(newsreaderFiles.sort()).toEqual([
      "newsreader-latin-wght-italic.woff2",
      "newsreader-latin-wght-normal.woff2",
    ]);
    for (const source of [css, newsreader, app]) {
      expect(source).not.toMatch(/fonts\.googleapis|fonts\.gstatic/);
    }
    expect(css + newsreader).not.toMatch(/url\(["']?https?:/i);
  });
});
