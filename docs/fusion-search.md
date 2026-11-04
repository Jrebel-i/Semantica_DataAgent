# Fusion search and MCP result contract

Ranking version `1` uses reciprocal rank fusion with k=60 and weights 2, 1, 1 and 0.5 for exact, full text, vector and graph hits respectively. Tasks without an output are multiplied by 0.5. Confidence is high for an exact hit or hits from at least two sources, medium for a full-text-only hit, and low otherwise; it is a ranking signal, not a probability.

A matching query term in passages from at least two distinct documents is flagged as a *possible* ambiguity, with passage references. This conservative rule calls for inspection and does not prove that the definitions conflict. Detection is limited to retrieved passages and literal query matches.

All MCP tools accept `cursor`. Results are capped at 25 items or 12,000 characters per page. Large individual values are returned as `result_fragment` pieces containing JSON text; concatenate the fragments and decode the JSON after the final page. Cursors must be used with the same tool and arguments. Since the tool executes again on each page, callers should restart pagination if the underlying knowledge changes.
