import { useEffect, useState } from "react";
import type { Messages } from "./i18n";

type Schema = {
  type?: string;
  title?: string;
  default?: unknown;
  enum?: (string | number | boolean)[];
  items?: Schema;
  properties?: Record<string, Schema>;
  required?: string[];
};
type Plugin = {
  category: string;
  id: string;
  version: string;
  display_name: string;
  parameters_schema: Schema;
  enabled: boolean;
  parameters: Record<string, unknown>;
};
type Rules = {
  database_include: string[];
  database_exclude: string[];
  task_include: string[];
  task_exclude: string[];
};
type Issue = { path: (string | number)[]; message: string };

const EMBEDDING_DEFAULTS = {
  embedding_concurrency: "2",
  embedding_timeout_seconds: "60",
  embedding_timeout_per_input_seconds: "20",
  embedding_retries: "2",
  embedding_backoff_seconds: "1",
};
type EmbeddingKey = keyof typeof EMBEDDING_DEFAULTS;

export function PluginsPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [rules, setRules] = useState<Rules | null>(null);
  const [errors, setErrors] = useState<Record<string, Issue[]>>({});
  const [sessionRetention, setSessionRetention] = useState("30");
  const [dimLimit, setDimLimit] = useState("1000");
  const [embedding, setEmbedding] = useState(EMBEDDING_DEFAULTS);
  const [failure, setFailure] = useState(false);
  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  useEffect(() => {
    const auth = { Authorization: `Bearer ${token}` };
    Promise.all([
      fetch("/api/v1/plugins", { headers: auth }),
      fetch("/api/v1/filters", { headers: auth }),
      fetch("/api/v1/settings", { headers: auth }),
    ])
      .then(async ([p, f, settings]) => {
        if (!p.ok || !f.ok || !settings.ok) throw new Error();
        setPlugins((await p.json()) as Plugin[]);
        setRules((await f.json()) as Rules);
        const data = (await settings.json()) as Record<string, string | null>;
        setDimLimit(data.dimension_value_limit || "1000");
        setSessionRetention(data.session_retention_days || "30");
        setEmbedding(
          Object.fromEntries(
            Object.entries(EMBEDDING_DEFAULTS).map(([key, fallback]) => [
              key,
              data[key] ?? fallback,
            ]),
          ) as Record<EmbeddingKey, string>,
        );
      })
      .catch(() => setFailure(true));
  }, [token]);
  function change(plugin: Plugin) {
    setPlugins((old) =>
      old.map((p) =>
        p.id === plugin.id && p.category === plugin.category ? plugin : p,
      ),
    );
  }
  async function save(plugin: Plugin) {
    try {
      const response = await fetch(
        `/api/v1/plugins/${encodeURIComponent(plugin.category)}/${encodeURIComponent(plugin.id)}`,
        {
          method: "PUT",
          headers,
          body: JSON.stringify({
            enabled: plugin.enabled,
            parameters: Object.fromEntries(
              Object.entries(plugin.parameters_schema.properties || {})
                .filter(
                  ([name, schema]) =>
                    name in plugin.parameters || schema.default !== undefined,
                )
                .map(([name, schema]) => [
                  name,
                  plugin.parameters[name] ?? schema.default,
                ]),
            ),
          }),
        },
      );
      const data: unknown = await response.json();
      const key = `${plugin.category}/${plugin.id}`;
      if (!response.ok) {
        const detail = (data as { detail?: unknown }).detail;
        if (Array.isArray(detail)) {
          setErrors((old) => ({ ...old, [key]: detail as Issue[] }));
          return false;
        }
        throw new Error();
      }
      setErrors((old) => ({ ...old, [key]: [] }));
      setFailure(false);
      return true;
    } catch {
      setFailure(true);
      return false;
    }
  }
  async function saveRules() {
    try {
      const response = await fetch("/api/v1/filters", {
        method: "PUT",
        headers,
        body: JSON.stringify(rules),
      });
      if (!response.ok) throw new Error();
      setFailure(false);
    } catch {
      setFailure(true);
    }
  }
  return (
    <section>
      <h2>{t.plugins}</h2>
      {failure && <p role="alert">{t.error}</p>}
      <fieldset>
        <label>
          {t.dimLimit}
          <input
            aria-label={t.dimLimit}
            type="number"
            min="1"
            max="1000000"
            value={dimLimit}
            onChange={(e) => setDimLimit(e.target.value)}
          />
        </label>
        <button
          onClick={() => {
            void fetch("/api/v1/settings", {
              method: "PUT",
              headers,
              body: JSON.stringify({
                values: { dimension_value_limit: dimLimit },
              }),
            })
              .then((response) => {
                if (!response.ok) throw new Error();
                setFailure(false);
              })
              .catch(() => setFailure(true));
          }}
        >
          {t.save}
        </button>
      </fieldset>
      <fieldset>
        <label>
          {t.sessionRetention}
          <input
            type="number"
            min="1"
            max="36500"
            value={sessionRetention}
            onChange={(e) => setSessionRetention(e.target.value)}
          />
        </label>
        <button
          onClick={() => {
            void fetch("/api/v1/settings", {
              method: "PUT",
              headers,
              body: JSON.stringify({
                values: { session_retention_days: sessionRetention },
              }),
            })
              .then((response) => {
                if (!response.ok) throw new Error();
                setFailure(false);
              })
              .catch(() => setFailure(true));
          }}
        >
          {t.save}
        </button>
      </fieldset>
      <fieldset>
        <legend>{t.embeddingSettings}</legend>
        {(
          [
            ["embedding_concurrency", t.embeddingConcurrency, "1"],
            ["embedding_timeout_seconds", t.embeddingTimeout, "0.001"],
            [
              "embedding_timeout_per_input_seconds",
              t.embeddingTimeoutPerInput,
              "0",
            ],
            ["embedding_retries", t.embeddingRetries, "0"],
            ["embedding_backoff_seconds", t.embeddingBackoff, "0"],
          ] as [EmbeddingKey, string, string][]
        ).map(([key, label, min]) => (
          <label key={key}>
            {label}
            <input
              aria-label={label}
              type="number"
              min={min}
              value={embedding[key]}
              onChange={(e) =>
                setEmbedding((old) => ({ ...old, [key]: e.target.value }))
              }
            />
          </label>
        ))}
        <button
          onClick={() => {
            void fetch("/api/v1/settings", {
              method: "PUT",
              headers,
              body: JSON.stringify({ values: embedding }),
            })
              .then((response) => {
                if (!response.ok) throw new Error();
                setFailure(false);
              })
              .catch(() => setFailure(true));
          }}
        >
          {t.save}
        </button>
      </fieldset>
      {plugins.map((plugin) => {
        const key = `${plugin.category}/${plugin.id}`;
        const issues = errors[key] || [];
        return (
          <fieldset key={key}>
            <legend>
              {plugin.display_name} ({plugin.category} / {plugin.id} /{" "}
              {plugin.version})
            </legend>
            <label>
              <input
                type="checkbox"
                checked={plugin.enabled}
                onChange={(e) => {
                  const next = { ...plugin, enabled: e.target.checked };
                  change(next);
                  void save(next).then((ok) => {
                    if (!ok)
                      setPlugins((old) =>
                        old.map((p) =>
                          p.id === plugin.id && p.category === plugin.category
                            ? { ...p, enabled: plugin.enabled }
                            : p,
                        ),
                      );
                  });
                }}
              />
              {t.enabled}
            </label>
            {Object.entries(plugin.parameters_schema.properties || {}).map(
              ([name, schema]) => {
                const value = plugin.parameters[name] ?? schema.default;
                const set = (v: unknown) =>
                  change({
                    ...plugin,
                    parameters: { ...plugin.parameters, [name]: v },
                  });
                return (
                  <div key={name}>
                    <label>
                      {schema.title || name}
                      {plugin.parameters_schema.required?.includes(name)
                        ? " *"
                        : " "}
                      {schema.enum ? (
                        <select
                          aria-label={name}
                          value={schema.enum.findIndex(
                            (item) => item === value,
                          )}
                          onChange={(e) => {
                            const index = Number(e.target.value);
                            if (index >= 0) set(schema.enum?.[index]);
                          }}
                        >
                          <option value={-1}></option>
                          {schema.enum.map((v, index) => (
                            <option key={index} value={index}>
                              {String(v)}
                            </option>
                          ))}
                        </select>
                      ) : schema.type === "boolean" ? (
                        <input
                          aria-label={name}
                          type="checkbox"
                          checked={value === true}
                          onChange={(e) => set(e.target.checked)}
                        />
                      ) : schema.type === "array" ? (
                        <textarea
                          aria-label={name}
                          value={Array.isArray(value) ? value.join("\n") : ""}
                          onChange={(e) =>
                            set(
                              e.target.value ? e.target.value.split("\n") : [],
                            )
                          }
                        />
                      ) : (
                        <input
                          aria-label={name}
                          type={schema.type === "integer" ? "number" : "text"}
                          value={value == null ? "" : String(value)}
                          onChange={(e) =>
                            set(
                              schema.type === "integer" && e.target.value !== ""
                                ? Number(e.target.value)
                                : e.target.value,
                            )
                          }
                        />
                      )}
                    </label>
                    {issues
                      .filter((issue) => issue.path[0] === name)
                      .map((issue, i) => (
                        <p role="alert" key={i}>
                          {issue.message}
                        </p>
                      ))}
                  </div>
                );
              },
            )}
            {issues
              .filter((issue) => !issue.path.length)
              .map((issue, i) => (
                <p role="alert" key={i}>
                  {issue.message}
                </p>
              ))}
            <button onClick={() => void save(plugin)}>{t.save}</button>
          </fieldset>
        );
      })}
      {rules && (
        <fieldset>
          <legend>{t.filters}</legend>
          {(Object.keys(rules) as (keyof Rules)[]).map((key) => (
            <div key={key}>
              <label>
                {t[key]}
                <textarea
                  aria-label={t[key]}
                  value={rules[key].join("\n")}
                  onChange={(e) =>
                    setRules({
                      ...rules,
                      [key]: e.target.value ? e.target.value.split("\n") : [],
                    })
                  }
                />
              </label>
            </div>
          ))}
          <p>{t.patternsHint}</p>
          <button onClick={() => void saveRules()}>{t.save}</button>
        </fieldset>
      )}
    </section>
  );
}
