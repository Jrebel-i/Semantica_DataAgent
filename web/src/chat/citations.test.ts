/// <reference types="node" />
// @vitest-environment jsdom
import { expect, test, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  ChatPage,
  citationLabel,
  mergeCitations,
  stripCitationRefs,
} from "../ChatPage";
import { getMessages } from "../i18n";
import {
  conversationFetcher,
  conversationRow,
  cssDeclarations,
  document,
  json,
  openHistory,
  provider,
  setupChatTestEnv,
  sse,
  table,
  toolStream,
  toolTurnEvents,
} from "./testUtils";

setupChatTestEnv();

test("末尾纯引用行被移除，引用回收后按对象合并", () => {
  const answer = [
    "结论如下。",
    "",
    "引用：`document:metrics/definitions/sales.md#chunk-1`、`document:metrics/definitions/sales.md#chunk-2`",
    "`table:shop.payments`，`field:shop.payments.dt`",
  ].join("\n");
  expect(stripCitationRefs(answer)).toEqual({
    text: "结论如下。",
    refs: [
      "document:metrics/definitions/sales.md#chunk-1",
      "document:metrics/definitions/sales.md#chunk-2",
      "table:shop.payments",
      "field:shop.payments.dt",
    ],
  });
  expect(
    stripCitationRefs(
      "结论\n\n引用：\n\n- `document:a/b.md#chunk-1`\n- `field:x.y.z`",
    ),
  ).toEqual({
    text: "结论",
    refs: ["document:a/b.md#chunk-1", "field:x.y.z"],
  });
  expect(
    stripCitationRefs("结论\nCitations: document:a.md, document:b.md#chunk-2"),
  ).toEqual({
    text: "结论",
    refs: ["document:a.md", "document:b.md#chunk-2"],
  });
});

test("引用行识别只移除末尾的纯引用块", () => {
  expect(stripCitationRefs("引用：这份文档说明了口径")).toEqual({
    text: "引用：这份文档说明了口径",
    refs: [],
  });
  expect(stripCitationRefs("正文\n引用：document:a.md\n后面还有正文")).toEqual({
    text: "正文\n引用：document:a.md\n后面还有正文",
    refs: [],
  });
  expect(stripCitationRefs("正文\ndocument:a.md")).toEqual({
    text: "正文\ndocument:a.md",
    refs: [],
  });
  expect(
    stripCitationRefs("正文\nReferences: document:a.md\n引用：table:x.y"),
  ).toEqual({
    text: "正文",
    refs: ["document:a.md", "table:x.y"],
  });
});

test("citationLabel 生成文档与数据表的友好名称", () => {
  expect(citationLabel("document:%E6%8C%87%E6%A0%87/sales.md#chunk-1")).toEqual(
    {
      kind: "document",
      label: "sales",
      category: "指标",
      title: "指标/sales.md",
    },
  );
  expect(
    citationLabel("document:%E6%8C%87%E6%A0%87/%E5%BA%93/_overview.md#chunk-1"),
  ).toEqual({
    kind: "document",
    label: "库 · 概览",
    category: "指标",
    title: "指标/库/_overview.md",
  });
  expect(citationLabel("document:指标/订单/variant_001.md#chunk-2")).toEqual({
    kind: "document",
    label: "订单 · 口径 1",
    category: "指标",
    title: "指标/订单/variant_001.md",
  });
  expect(citationLabel("document:指标/订单/variant_012.md#chunk-2")).toEqual({
    kind: "document",
    label: "订单 · 口径 12",
    category: "指标",
    title: "指标/订单/variant_012.md",
  });
  const tricky = citationLabel(
    `document:指标/${encodeURIComponent("销售 50% #报告.md")}#chunk-1`,
  );
  expect(tricky).toEqual({
    kind: "document",
    label: "销售 50% #报告",
    category: "指标",
    title: "指标/销售 50% #报告.md",
  });
  expect(citationLabel("document:definitions/%E9%94.md#chunk-1")).toEqual({
    kind: "document",
    label: "%E9%94.md",
    category: "definitions",
    title: "definitions/%E9%94.md",
  });
  expect(citationLabel("table:shop.orders")).toEqual({
    kind: "table",
    label: "shop.orders",
    title: "shop.orders",
  });
  expect(citationLabel("field:shop.orders.amount")).toEqual({
    kind: "table",
    label: "shop.orders",
    title: "shop.orders · amount",
  });
  const long = citationLabel(
    "table:warehouse.a_very_long_table_name_that_should_be_middle_ellipsized",
  );
  expect(long.label).toContain("…");
  expect(long.title).toBe(
    "warehouse.a_very_long_table_name_that_should_be_middle_ellipsized",
  );
});

test("同一文档的多个 chunk 与同表字段合并为同一个引用", () => {
  const groups = mergeCitations([
    { ref: "document:metrics/a.md#chunk-1", type: "document" },
    { ref: "document:metrics/a.md#chunk-9", type: "document" },
    { ref: "table:shop.orders", type: "table" },
    { ref: "field:shop.orders.amount", type: "field" },
    { ref: "field:shop.orders.status", type: "field" },
  ]);
  expect(groups).toHaveLength(2);
  const document = groups.find((group) => group.label.kind === "document");
  const table = groups.find((group) => group.label.kind === "table");
  expect(document?.refs).toHaveLength(2);
  expect(document?.label.label).toBe("a");
  expect(table?.refs).toHaveLength(3);
  expect(table?.openRef).toBe("table:shop.orders");
  expect(table?.label.label).toBe("shop.orders");
  expect(table?.fields).toEqual(["amount", "status"]);
  expect(table?.label.title).toBe("shop.orders · 字段：amount、status");
});
