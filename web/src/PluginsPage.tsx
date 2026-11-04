import { useEffect, useState } from "react";
import type { Messages } from "./i18n";

type Schema = {
  type?: string;
  title?: string;
  default?: unknown;
  enum?: string[];
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
    ])
      .then(async ([p, f]) => {
        if (!p.ok || !f.ok) throw new Error();
        setPlugins((await p.json()) as Plugin[]);
        setRules((await f.json()) as Rules);
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
                          value={String(value ?? "")}
                          onChange={(e) => set(e.target.value)}
                        >
                          <option value=""></option>
                          {schema.enum.map((v) => (
                            <option key={v} value={v}>
                              {v}
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
