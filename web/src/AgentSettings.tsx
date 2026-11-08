import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Input } from "antd";
import type { Messages } from "./i18n";
import type { components } from "./api";
import { parseAgentProfiles, type AgentProfiles } from "./agentProfiles";

const keys = [
  "assistant_agent",
  "assistant_provider_id",
  "assistant_model",
  "assistant_timeout_seconds",
] as const;
type Key = (typeof keys)[number];

export function AgentSettings({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [profiles, setProfiles] = useState<AgentProfiles>({});
  const [baseline, setBaseline] = useState("");
  const [agents, setAgents] = useState<components["schemas"]["AgentView"][]>(
    [],
  );
  const [providers, setProviders] = useState<
    components["schemas"]["ProviderView"][]
  >([]);
  useEffect(() => {
    fetch("/api/v1/settings", { headers: { Authorization: `Bearer ${token}` } })
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Record<string, string | null>>;
      })
      .then((data) => {
        const parsedProfiles = parseAgentProfiles(
          data.assistant_agent_profiles,
        );
        const loadedValues = Object.fromEntries(
          keys.map((key) => [key, data[key] ?? ""]),
        );
        setProfiles(parsedProfiles);
        setValues(loadedValues);
        setBaseline(
          JSON.stringify({ values: loadedValues, profiles: parsedProfiles }),
        );
        setLoaded(true);
      })
      .catch(() => setStatus(t.error));
  }, [token, t.error]);
  useEffect(() => {
    const headers = { Authorization: `Bearer ${token}` };
    void Promise.all([
      fetch("/api/v1/agents", { headers }),
      fetch("/api/v1/providers", { headers }),
    ])
      .then(async ([a, p]) => {
        if (!a.ok || !p.ok) throw new Error();
        setAgents((await a.json()) as components["schemas"]["AgentView"][]);
        setProviders(
          (await p.json()) as components["schemas"]["ProviderView"][],
        );
      })
      .catch(() => setStatus(t.error));
  }, [token, t.error]);
  const dirty = useMemo(
    () => loaded && JSON.stringify({ values, profiles }) !== baseline,
    [baseline, loaded, profiles, values],
  );
  async function save() {
    try {
      const response = await fetch("/api/v1/settings", {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          values: {
            ...values,
            assistant_agent_profiles: JSON.stringify(profiles),
          },
        }),
      });
      if (!response.ok) throw new Error();
      setBaseline(
        JSON.stringify({
          values,
          profiles,
        }),
      );
      setStatus(t.settingsSaved);
    } catch {
      setStatus(t.error);
    }
  }
  const labels: Record<Key, string> = {
    assistant_agent: t.defaultAgent,
    assistant_provider_id: t.defaultProvider,
    assistant_model: t.defaultModel,
    assistant_timeout_seconds: t.agentTimeout,
  };
  const modelOptions =
    providers.find((provider) => provider.id === values.assistant_provider_id)
      ?.models ?? [];
  return (
    <section className="config-section admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.agentSettings}</h1>
          <p className="page-subtitle">{t.agentSettingsHint}</p>
        </div>
      </div>
      {status && (
        <Alert
          type={status === t.settingsSaved ? "success" : "error"}
          title={status}
        />
      )}
      <div className="card section-card">
        <div className="grid-2">
          {keys.map((key) => (
            <div className="field" key={key}>
              <label className="field-label" htmlFor={`agent-default-${key}`}>
                {labels[key]}
              </label>
              {key === "assistant_timeout_seconds" ? (
                <Input
                  id={`agent-default-${key}`}
                  aria-label={labels[key]}
                  type="number"
                  value={values[key] ?? ""}
                  onChange={(event) =>
                    setValues({ ...values, [key]: event.target.value })
                  }
                />
              ) : (
                <select
                  id={`agent-default-${key}`}
                  aria-label={labels[key]}
                  value={values[key] ?? ""}
                  onChange={(event) =>
                    setValues({
                      ...values,
                      [key]: event.target.value,
                      ...(key === "assistant_provider_id"
                        ? { assistant_model: "" }
                        : {}),
                    })
                  }
                >
                  <option value="">{t.useDefault}</option>
                  {(key === "assistant_agent"
                    ? agents.map((agent) => ({
                        value: agent.id,
                        label: agent.name,
                      }))
                    : key === "assistant_provider_id"
                      ? providers.map((provider) => ({
                          value: provider.id,
                          label: provider.name,
                        }))
                      : modelOptions.map((model) => ({
                          value: model,
                          label: model,
                        }))
                  ).map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
          ))}
        </div>
      </div>
      {agents.map((agent) => {
        const profile = profiles[agent.id] ?? {
          providers: [],
          default_provider: "",
          default_model: "",
        };
        const update = (next: typeof profile) =>
          setProfiles({ ...profiles, [agent.id]: next });
        const availableProviders = profile.providers.length
          ? providers.filter((provider) =>
              profile.providers.includes(provider.id),
            )
          : providers;
        return (
          <div className="card agent-card" key={agent.id}>
            <div className="agent-card-head">
              <h3>{agent.name}</h3>
              <span className="muted mono">{agent.id}</span>
              {!agent.available && (
                <span className="subtle">{t.unavailable}</span>
              )}
            </div>
            <div className="field" style={{ marginBottom: 14 }}>
              <span className="field-label">{t.allowedProviders}</span>
              <div className="provider-grid">
                {providers.length === 0 && (
                  <p className="muted">{t.noProviders}</p>
                )}
                {providers.map((provider) => {
                  const checked = profile.providers.includes(provider.id);
                  return (
                    <Checkbox
                      className={`provider-option${checked ? " selected" : ""}`}
                      key={provider.id}
                      checked={checked}
                      onChange={(event) => {
                        const ids = event.target.checked
                          ? [...profile.providers, provider.id]
                          : profile.providers.filter(
                              (id) => id !== provider.id,
                            );
                        update({
                          providers: ids,
                          default_provider: ids.includes(
                            profile.default_provider,
                          )
                            ? profile.default_provider
                            : "",
                          default_model: ids.includes(profile.default_provider)
                            ? profile.default_model
                            : "",
                        });
                      }}
                    >
                      {provider.name}
                    </Checkbox>
                  );
                })}
              </div>
              <p className="field-hint">{t.allowedProvidersHint}</p>
            </div>
            <div className="grid-2">
              <div className="field">
                <label
                  className="field-label"
                  htmlFor={`agent-provider-${agent.id}`}
                >
                  {t.defaultProvider}
                </label>
                <select
                  id={`agent-provider-${agent.id}`}
                  aria-label={`${agent.name} ${t.defaultProvider}`}
                  value={profile.default_provider}
                  onChange={(event) =>
                    update({
                      ...profile,
                      default_provider: event.target.value,
                      default_model: "",
                    })
                  }
                >
                  <option value="">{t.useDefault}</option>
                  {availableProviders.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label
                  className="field-label"
                  htmlFor={`agent-model-${agent.id}`}
                >
                  {t.defaultModel}
                </label>
                <select
                  id={`agent-model-${agent.id}`}
                  aria-label={`${agent.name} ${t.defaultModel}`}
                  value={profile.default_model}
                  onChange={(event) =>
                    update({ ...profile, default_model: event.target.value })
                  }
                >
                  <option value="">{t.useDefault}</option>
                  {providers
                    .find(
                      (provider) => provider.id === profile.default_provider,
                    )
                    ?.models.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                </select>
              </div>
            </div>
          </div>
        );
      })}
      <div className="save-bar">
        <Button
          type="primary"
          disabled={!loaded || !dirty}
          onClick={() => void save()}
        >
          {t.saveSettings}
        </Button>
        <span className="save-reason">
          {!loaded ? t.error : dirty ? t.unsavedChanges : t.notModified}
        </span>
      </div>
    </section>
  );
}
