from semantica_dataagent.markdown import MarkdownAdapter, chunks, table_name


def test_draft_and_nested_documents(tmp_path):
    root = tmp_path / "knowledge" / "metrics"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "sales.md").write_text("# 销售额\n\n正文\n")
    result = MarkdownAdapter().read(tmp_path, {})
    docs = [r.data for r in result.records]
    index = next(d for d in docs if d["path"] == "index.md")
    assert (
        "自动生成" in index["content"]
        and "nested/sales.md" in index["content"]
        and "销售额" in index["content"]
    )
    assert not (root / "index.md").exists()


def test_chunks_preserve_lines_headings_tables_and_fences():
    text = (
        "# 标题\n\n## 明细\n\n| 字段 | 描述 |\n| --- | --- |\n"
        + "".join(f"| 字段{i} | 描述{i} |\n" for i in range(100))
        + "\n```sql\n"
        + "select 1;\n" * 300
        + "```\n"
    )
    parts = chunks(text, limit=300)
    assert all(p["section"] == ["标题", "明细"] for p in parts if p["start_line"] >= 3)
    tables = [
        p for p in parts if "| 字段0 |" in p["content"] or "| 字段99 |" in p["content"]
    ]
    assert len(tables) == 2 and all("| --- | --- |" in p["content"] for p in tables)
    code = [p for p in parts if "```sql" in p["content"]]
    assert len(code) > 1 and all(p["content"].rstrip().endswith("```") for p in code)
    assert all(len(p["content"]) <= 300 for p in code)
    assert all(p["start_line"] <= p["end_line"] for p in parts)
    assert next(p for p in parts if "| 字段0 |" in p["content"])["start_line"] == 5


def test_chunks_split_single_lines_larger_than_the_limit():
    row = "| " + "x" * 2000 + " |"
    parts = chunks(f"| 字段 | 描述 |\n| --- | --- |\n{row}\n", limit=300)
    assert all(len(p["content"]) <= 300 for p in parts)
    assert all(p["start_line"] <= p["end_line"] for p in parts)
    assert "".join(p["content"] for p in parts).count("x") == 2000

    plain = chunks("y" * 2000, limit=300)
    assert all(len(p["content"]) <= 300 for p in plain)
    assert "".join(p["content"] for p in plain) == "y" * 2000


def test_dictionary_filename_matches_qualified_table():
    assert table_name("nested/shop.orders.md") == ("shop", "orders")
    assert table_name("nested/orders.md") is None


def test_section_ignores_headings_inside_code():
    from semantica_dataagent.markdown import section_range

    lines = ["# 指标", "```markdown", "## 干扰", "```", "## 正文", "内容", "## 另一节"]
    assert section_range(lines, "正文") == (4, 6)
