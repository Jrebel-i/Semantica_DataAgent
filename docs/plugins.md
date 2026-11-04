# Ingestion plugins

Install Python distributions in the application environment. Source adapters register a no-argument class in the `semantica_dataagent.source_adapters` entry point group; task parsers use `semantica_dataagent.task_parsers`. Each class exposes `id`, `version`, `display_name`, and a JSON Schema object `parameters_schema`. IDs must be unique within their group. The built-in `canonical` adapter is enabled by default; other plugins start disabled.

A source adapter implements `read(directory: Path, parameters: dict) -> ReadResult`, returning canonical `Record` objects and source hashes (see `canonical.py`). A task parser exposes `task_types` and implements `parse(task: Record, parameters: dict) -> ParseResult`. Dispatch selects the first enabled parser whose `task_types` contains the canonical task's `type`; absent a match, it returns `unsupported`. Parser results contain `reads`, `writes`, `status`, and an optional `reason`. No SQL parser is bundled.

Administrators use `/plugins` to configure plugins and database/task include and exclude wildcard patterns. Exclusions take precedence, empty inclusion lists match everything, and `tmp` and `test` are excluded by default. Changes apply to the next build. Build statistics include the number of filtered canonical records.

Build stages are listed in `semantica_dataagent.stages.STAGES`; each receives records, generation, and storage settings and returns counts. Table REST and MCP endpoints are in `table_api.py`.
