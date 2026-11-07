import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button } from "antd";
import type { paths } from "./api";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import { citationDisplayName, citationLabel } from "./ChatPage";

type Category =
  paths["/api/v1/categories"]["get"]["responses"][200]["content"]["application/json"][number];
type Hit =
  paths["/api/v1/search"]["get"]["responses"][200]["content"]["application/json"][number];
type Scope = "all" | "tables" | "fields" | "tasks" | "docs";

export function SearchPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<Scope>("all");
  const [category, setCategory] = useState("");
  const [categories, setCategories] = useState<Category[]>([]);
  const [categoryError, setCategoryError] = useState(false);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<Hit[] | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/categories", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json() as Promise<Category[]>;
      })
      .then((data) => {
        setCategories(data);
        setCategoryError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setCategoryError(true);
      });
    return () => {
      controller.abort();
      pending.current?.abort();
    };
  }, [token]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError(false);
    setResults(null);
    const params = new URLSearchParams({ query: query.trim(), scope });
    if (category && (scope === "docs" || scope === "all"))
      params.set("category", category);
    try {
      const response = await fetch(`/api/v1/search?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error();
      const hits = (await response.json()) as Hit[];
      if (!controller.signal.aborted) setResults(hits);
    } catch {
      if (!controller.signal.aborted) setError(true);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  const sources = {
    exact: t.sourceExact,
    fulltext: t.sourceFulltext,
    vector: t.sourceVector,
    graph: t.sourceGraph,
  };
  const confidence = {
    high: t.confidenceHigh,
    medium: t.confidenceMedium,
    low: t.confidenceLow,
  };
  return (
    <section className="stack">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.searchDebug}</h1>
          <p className="page-subtitle">{t.searchHint}</p>
        </div>
      </div>
      {categoryError && <Alert type="error" title={t.categoryError} />}
      {error && <Alert type="error" title={t.searchError} />}
      <form className="search-form" onSubmit={(event) => void submit(event)}>
        <label className="field">
          <span className="field-label">{t.queryText}</span>
          <input
            aria-label={t.queryText}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span className="field-label">{t.searchScope}</span>
          <select
            aria-label={t.searchScope}
            value={scope}
            onChange={(event) => {
              const value = event.target.value as Scope;
              setScope(value);
              if (value !== "all" && value !== "docs") setCategory("");
            }}
          >
            <option value="all">{t.allScopes}</option>
            <option value="tables">{t.scopeTables}</option>
            <option value="fields">{t.scopeFields}</option>
            <option value="tasks">{t.scopeTasks}</option>
            <option value="docs">{t.scopeDocs}</option>
          </select>
        </label>
        <label className="field">
          <span className="field-label">{t.categories}</span>
          <select
            aria-label={t.categories}
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            disabled={scope !== "all" && scope !== "docs"}
          >
            <option value="">{t.allCategories}</option>
            {categories.map((item) => (
              <option key={item.ref} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <Button
          htmlType="submit"
          type="primary"
          disabled={loading || !query.trim()}
        >
          {loading ? t.searching : t.searchSubmit}
        </Button>
      </form>
      {results &&
        (results.length === 0 ? (
          <div className="empty-state">{t.noResults}</div>
        ) : (
          <ol className="search-results">
            {results.map((hit) => {
              const citation = citationLabel(hit.ref);
              return (
                <li key={hit.ref}>
                  <article className="search-result">
                    <h3>
                      {hit.name ||
                        (hit.path
                          ? safeDecodePath(hit.path)
                          : citationDisplayName(citation))}
                    </h3>
                    <p className="ref" title={citation.title}>
                      {citationDisplayName(citation)} ({hit.type})
                    </p>
                    {(hit.content || hit.comment || hit.description) && (
                      <p>{hit.content || hit.comment || hit.description}</p>
                    )}
                    <p>
                      {t.score}: {hit.score.toFixed(4)}
                    </p>
                    <p>
                      {t.sources}:{" "}
                      {hit.sources.map((source) => sources[source]).join(", ")}
                    </p>
                    <p>
                      {t.confidence}:{" "}
                      <strong>{confidence[hit.confidence]}</strong>
                    </p>
                    {hit.ambiguous && (
                      <div className="search-ambiguity">
                        <strong>{t.possibleAmbiguity}</strong>
                        <p>{t.ambiguityRefs}</p>
                        <ul>
                          {hit.ambiguity_refs.map((ref) => {
                            const label = citationLabel(ref);
                            return (
                              <li key={ref} title={label.title}>
                                {citationDisplayName(label)}
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    )}
                  </article>
                </li>
              );
            })}
          </ol>
        ))}
    </section>
  );
}
