# Synthetic DTB source fixture

All content here is invented. File names, headers, column order and value
shapes follow the real dtb export described in issue #10:

- `dtb/<UTC timestamp>-<hash>/manifest.json`: snapshot metadata.
- `dtb/<snapshot>/任务信息.json`: `{columns, data}` where each data row is a
  positional string array.
- `dtb/<snapshot>/表信息.xlsx`: one row per field, with `seq_no >= 10001`
  marking a partition column.
- `dimensions/<table>/meta.json` and `dimensions/<table>/current/rows.jsonl`:
  flat string-valued dimension rows.

The `20260101T000000Z-aaaa` snapshot is an older, smaller export so tests can
prove that the adapter picks the newest one by default. The newest snapshot
covers spark, shell + Livy, shell + beeline, shell-only fallback, merge-onself,
an unsupported task type, a short row, a missing task id, a double-encoded
`task_conf`, an unreadable `task_conf`, an invalid `seq_no` and a missing
`column_business_comment` header. Dimension fixtures cover whitespace values,
empty cells, truncation, missing metadata/rows and a malformed JSONL row.

`表信息.xlsx` is generated from the row list in the issue fixture generator
with `openpyxl`; the first sheet is the only sheet, matching the real export.
