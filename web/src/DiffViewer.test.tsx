// @vitest-environment jsdom
import { afterEach, describe, expect, test } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DiffViewer } from "./DiffViewer";
import { computeInlineDiff, parseUnifiedDiff } from "./diffParser";
import { getMessages } from "./i18n";

afterEach(() => {
  cleanup();
});

const REALISTIC_CHINESE_DIFF = `--- knowledge/business-scenarios/air/transaction-chain-analysis/buyer-api/README.md
+++ knowledge/business-scenarios/air/transaction-chain-analysis/buyer-api/README.md
@@ -58,9 +58,9 @@
 针对大盘航司同时分析验价、预订及转化时，开发题 c34 的参考 SQL 使用 \`dm.dm_air_api_conversion_rate_pb_d\`，按 \`main_carrier\`、\`is_lcc\`、\`gds\` 与航司所属洲分组；该参考答案未经业务确认，PB 表已被视图限制仍需遵守，不能作为所有大盘查询的通用定义。
-参考中以 \`is_lcc = 'Y'\` 判 LCC，其余为 FSC；航司与启用的 \`dimensions.dim_airline\` 按 \`main_carrier = airline\` 左连接，维表先选有效快照并对 \`airline, TRIM(country)\` 去重，再左连 \`dimensions.dim_country\` 的 \`country_code\` 取 \`continent_code\`，未匹配显示 \`UNKNOWN\`。维表条件放在子查询或 ON 中以保留左连接。
+参考中以 \`is_lcc = 'Y'\` 判 LCC，其余为 FSC；按原始 \`is_lcc\` 分组，不用航司维表 LCC 字段回填，也不先对同一航司的多个洲用 MAX 合并；航司与启用的 \`dimensions.dim_airline\` 按 \`main_carrier = airline\` 左连接，维表先选有效快照并对 \`airline, TRIM(country)\` 去重，再左连 \`dimensions.dim_country\` 的 \`country_code\` 取 \`continent_code\`，未匹配显示 \`UNKNOWN\`。维表条件放在子查询或 ON 中以保留左连接。
 
-数量分别为 \`SUM(pricing_count)\`、\`SUM(pricing_count - pricing_limit_count)\`、\`SUM(pricing_success_count)\` 和 \`SUM(booking_count)\`；成功率为成功数之和除以验价数之和，BoP 为预订数之和除以成功数之和，参考结果四舍五入四位。按验价量降序并明确设置足额的返回行数；实际执行还应检查接口是否截断。不要将 OTA 控制表当作 PB 表的等价替代，除非确认统计范围一致。
+数量分别为 \`SUM(pricing_count)\`、\`SUM(pricing_count - pricing_limit_count)\`、\`SUM(pricing_success_count)\` 和 \`SUM(booking_count)\`；成功率为成功数之和除以验价数之和，BoP 为预订数之和除以成功数之和，参考结果四舍五入四位。按验价量降序；该参考案例显式使用 \`LIMIT 2000\`，未要求前 N 行时不得自行缩减为 500 等更小范围。查询接口存在返回行数上限时须检查是否截断，并分页或明确告知结果不完整。不要将 OTA 控制表当作 PB 表的等价替代，除非确认统计范围一致。
 
 ## 5. 已有组合口径参考`;

describe("diffParser", () => {
  test("parses realistic Chinese unified diff with line numbers, stats, and inline highlights", () => {
    const parsed = parseUnifiedDiff(REALISTIC_CHINESE_DIFF);
    expect(parsed.oldFile).toBe(
      "knowledge/business-scenarios/air/transaction-chain-analysis/buyer-api/README.md",
    );
    expect(parsed.newFile).toBe(
      "knowledge/business-scenarios/air/transaction-chain-analysis/buyer-api/README.md",
    );
    expect(parsed.additions).toBe(2);
    expect(parsed.deletions).toBe(2);
    expect(parsed.hunks).toHaveLength(1);

    const hunk = parsed.hunks[0];
    expect(hunk.oldStart).toBe(58);
    expect(hunk.newStart).toBe(58);
    expect(hunk.lines[0]).toMatchObject({
      kind: "context",
      oldLine: 58,
      newLine: 58,
    });
    expect(hunk.lines[1]).toMatchObject({
      kind: "del",
      oldLine: 59,
      newLine: null,
    });
    expect(hunk.lines[2]).toMatchObject({
      kind: "add",
      oldLine: null,
      newLine: 59,
    });

    const pairedDel = hunk.splitRows[1].left;
    const pairedAdd = hunk.splitRows[1].right;
    expect(pairedDel.kind).toBe("del");
    expect(pairedAdd.kind).toBe("add");
    expect(
      pairedAdd.segments?.some(
        (seg) => seg.kind === "add" && seg.text.includes("按原始"),
      ),
    ).toBe(true);
    expect(pairedDel.segments?.some((seg) => seg.kind === "equal")).toBe(true);
  });

  test("handles headerless diffs, pure additions, pure deletions, multiple hunks, and no-newline markers", () => {
    const headerless = parseUnifiedDiff("-old line\n+new line");
    expect(headerless.additions).toBe(1);
    expect(headerless.deletions).toBe(1);
    expect(headerless.hunks).toHaveLength(1);
    expect(headerless.hunks[0].splitRows).toHaveLength(1);

    const multi = parseUnifiedDiff(
      [
        "--- a/doc.md",
        "+++ b/doc.md",
        "@@ -1,2 +1,4 @@",
        " first",
        "+added one",
        "+added two",
        " second",
        "@@ -10,2 +12,1 @@ section header",
        "-removed line",
        " kept",
        "\\ No newline at end of file",
      ].join("\n"),
    );
    expect(multi.oldFile).toBe("doc.md");
    expect(multi.newFile).toBe("doc.md");
    expect(multi.additions).toBe(2);
    expect(multi.deletions).toBe(1);
    expect(multi.hunks).toHaveLength(2);
    expect(multi.hunks[0].splitRows[1].left.kind).toBe("empty");
    expect(multi.hunks[0].splitRows[1].right.kind).toBe("add");
    expect(multi.hunks[1].section).toBe("section header");
    expect(multi.hunks[1].splitRows[0].left.kind).toBe("del");
    expect(multi.hunks[1].splitRows[0].right.kind).toBe("empty");
  });

  test("computeInlineDiff isolates token-level changes", () => {
    const inline = computeInlineDiff(
      "使用 `LIMIT 500` 查询",
      "使用 `LIMIT 2000` 查询",
    );
    expect(inline.left).toEqual([
      { kind: "equal", text: "使用 `LIMIT " },
      { kind: "del", text: "500" },
      { kind: "equal", text: "` 查询" },
    ]);
    expect(inline.right).toEqual([
      { kind: "equal", text: "使用 `LIMIT " },
      { kind: "add", text: "2000" },
      { kind: "equal", text: "` 查询" },
    ]);
  });
});

describe("DiffViewer", () => {
  test("renders git-style split view by default without raw diff headers and toggles to unified", () => {
    const { container } = render(
      <DiffViewer
        diff={REALISTIC_CHINESE_DIFF}
        messages={getMessages("zh")}
        title="document:fallback"
      />,
    );

    const root = container.querySelector(".diff-viewer");
    expect(root?.getAttribute("data-diff-mode")).toBe("split");
    expect(
      container.querySelectorAll(".diff-table-split colgroup col"),
    ).toHaveLength(6);
    expect(
      screen.getByText(
        "knowledge/business-scenarios/air/transaction-chain-analysis/buyer-api/README.md",
      ),
    ).toBeTruthy();
    expect(screen.getByText("+2")).toBeTruthy();
    expect(screen.getByText("-2")).toBeTruthy();
    expect(screen.getByText("修改前")).toBeTruthy();
    expect(screen.getByText("修改后")).toBeTruthy();
    expect(screen.getByText("第 58 行 → 第 58 行")).toBeTruthy();

    // Raw diff file/hunk prefixes should never appear directly in rendered view
    expect(container.textContent).not.toContain("--- knowledge/");
    expect(container.textContent).not.toContain("+++ knowledge/");
    expect(container.textContent).not.toContain("@@ -58,9 +58,9 @@");

    // Inline word-level marks highlight only the changed prose
    const addMarks = container.querySelectorAll("mark.diff-mark-add");
    expect(addMarks.length).toBeGreaterThan(0);
    expect(
      Array.from(addMarks).some((el) => el.textContent?.includes("LIMIT 2000")),
    ).toBe(true);

    // Switch to Unified mode and back
    fireEvent.click(screen.getByRole("button", { name: "行内对比" }));
    expect(root?.getAttribute("data-diff-mode")).toBe("unified");
    expect(
      container.querySelectorAll(".diff-table-unified colgroup col"),
    ).toHaveLength(4);
    expect(screen.getByText("旧")).toBeTruthy();
    expect(screen.getByText("新")).toBeTruthy();
    expect(screen.getByText("变更内容")).toBeTruthy();
    expect(
      container.querySelectorAll("mark.diff-mark-add").length,
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "左右对比" }));
    expect(root?.getAttribute("data-diff-mode")).toBe("split");
  });
});
