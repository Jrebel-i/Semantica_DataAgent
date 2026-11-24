import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Select, Space, Tag } from "antd";
import type { components } from "./api";
import { DiscoveryPanel } from "./DiscoveryPanel";
import { readErrorDetail } from "./httpErrors";
import type { Messages } from "./i18n";
import { ProjectSelector, storedProject, useProjects } from "./ProjectSelector";

type TraceSourcePlugin = components["schemas"]["TraceSourcePluginView"];
type EvolutionConfig = components["schemas"]["EvolutionConfigView"];
type EvolutionConfigUpdate = components["schemas"]["EvolutionConfigUpdate"];
type EvolutionRun = components["schemas"]["EvolutionRunView"];
type EvolutionRunTrigger = components["schemas"]["EvolutionRunTrigger"];
type Provider = components["schemas"]["ProviderView"];

type ParamSchema = {
  type?: string;
  title?: string;
  description?: string;
  format?: string;
  default?: unknown;
  enum?: (string | number | boolean)[];
  properties?: Record<string, ParamSchema>;
  required?: string[];
};

const DEFAULT_CONFIG: EvolutionConfig = {
  project_id: "default",
  enabled: false,
  trace_source_plugin: null,
  trace_source_parameters: {},
  trace_source_secrets: {},
  configured_secrets: [],
  secret_configured: false,
  schedule_cron: null,
  timezone: "Asia/Shanghai",
  evolution_provider_id: null,
  evolution_model: null,
  max_traces_per_run: 200,
  last_trace_timestamp: null,
  last_triggered_at: null,
  next_run_at: null,
  global_langfuse_configured: false,
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function toIsoUtc(localOrIso: string): string | undefined {
  const trimmed = localOrIso.trim();
  if (!trimmed) return undefined;
  const parsed = new Date(trimmed);
  return Number.isNaN(parsed.getTime()) ? trimmed : parsed.toISOString();
}

function runStatusColor(status: EvolutionRun["status"]): string {
  if (status === "succeeded" || status === "accepted") return "green";
  if (status === "running") return "blue";
  if (status === "failed") return "red";
  return "orange";
}

function runStatusLabel(status: EvolutionRun["status"], t: Messages): string {
  if (status === "succeeded") return t.evolutionStatusSucceeded;
  if (status === "accepted") return t.evolutionStatusAccepted;
  if (status === "running") return t.evolutionStatusRunning;
  if (status === "failed") return t.evolutionStatusFailed;
  return t.evolutionStatusIncomplete;
}

function isSecretParam(
  key: string,
  field: ParamSchema,
  secretKeys: string[],
): boolean {
  return field.format === "password" || secretKeys.includes(key);
}

function splitTraceSourcePayload(
  schema: ParamSchema,
  paramValues: Record<string, unknown>,
  secretValues: Record<string, string>,
  secretKeys: string[],
  configuredSecrets: Set<string>,
): {
  parameters: Record<string, unknown>;
  secrets: Record<string, string | null>;
} {
  const parameters: Record<string, unknown> = {};
  const secrets: Record<string, string | null> = {};
  const properties = schema.properties || {};

  for (const [name, field] of Object.entries(properties)) {
    if (isSecretParam(name, field, secretKeys)) {
      const rawSecret = secretValues[name];
      const wasConfigured = configuredSecrets.has(name);
      if (rawSecret === "***") {
        if (wasConfigured) secrets[name] = "***";
      } else if (rawSecret === "") {
        if (wasConfigured) secrets[name] = "";
      } else if (typeof rawSecret === "string" && rawSecret.length > 0) {
        secrets[name] = rawSecret;
      }
      continue;
    }

    const raw = paramValues[name];
    const effective = raw !== undefined ? raw : field.default;
    if (effective === undefined || effective === null || effective === "") {
      continue;
    }
    if (field.type === "integer" || field.type === "number") {
      const num = Number(effective);
      if (Number.isFinite(num)) parameters[name] = num;
    } else {
      parameters[name] = effective;
    }
  }

  return { parameters, secrets };
}

function ProviderModelPair({
  idPrefix,
  providerLabel,
  modelLabel,
  defaultLabel,
  providerId,
  model,
  providers,
  disabled = false,
  onProviderChange,
  onModelChange,
}: {
  idPrefix: string;
  providerLabel: string;
  modelLabel: string;
  defaultLabel: string;
  providerId: string | null | undefined;
  model: string | null | undefined;
  providers: Provider[];
  disabled?: boolean;
  onProviderChange: (nextProviderId: string | null) => void;
  onModelChange: (nextModel: string | null) => void;
}) {
  const selectedProvider = providers.find((p) => p.id === providerId);
  const availableModels = selectedProvider?.models ?? [];
  const modelOptions =
    model && !availableModels.includes(model)
      ? [model, ...availableModels]
      : availableModels;

  return (
    <>
      <div className="evolution-field">
        <label htmlFor={`${idPrefix}-provider`}>{providerLabel}</label>
        <select
          id={`${idPrefix}-provider`}
          disabled={disabled}
          value={providerId ?? ""}
          onChange={(e) => onProviderChange(e.target.value || null)}
        >
          <option value="">{defaultLabel}</option>
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name}
            </option>
          ))}
        </select>
      </div>
      <div className="evolution-field">
        <label htmlFor={`${idPrefix}-model`}>{modelLabel}</label>
        <select
          id={`${idPrefix}-model`}
          disabled={disabled}
          value={model ?? ""}
          onChange={(e) => onModelChange(e.target.value || null)}
        >
          <option value="">{defaultLabel}</option>
          {modelOptions.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}

export function EvolutionPage({
  token,
  messages: t,
  initialTab = "evolution",
  onNavigateCorrections,
  onNavigatePersonalSettings,
}: {
  token: string;
  messages: Messages;
  initialTab?: "evolution" | "discovery";
  onNavigateCorrections?: (proposalId?: string) => void;
  onNavigatePersonalSettings?: () => void;
}) {
  const [activeTab, setActiveTab] = useState<"evolution" | "discovery">(
    initialTab,
  );
  const [project, setProject] = useState(storedProject);
  const { projects } = useProjects(token);
  const currentProject = useMemo(
    () => projects.find((item) => item.id === project) ?? null,
    [projects, project],
  );
  const isArchivedProject =
    currentProject?.status === "archived" ||
    currentProject?.status === "deleting";
  const projectRef = useRef(project);
  projectRef.current = project;
  const requestGenRef = useRef(0);
  const pollInFlightRef = useRef(false);

  const [providers, setProviders] = useState<Provider[]>([]);
  const [plugins, setPlugins] = useState<TraceSourcePlugin[]>([]);
  const [config, setConfig] = useState<EvolutionConfig>(DEFAULT_CONFIG);
  const [runs, setRuns] = useState<EvolutionRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [sinceInput, setSinceInput] = useState("");
  const [untilInput, setUntilInput] = useState("");
  const [maxTracesOverride, setMaxTracesOverride] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [error, setError] = useState("");
  const [saveNotice, setSaveNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const headers = { Authorization: `Bearer ${token}` };
    Promise.all([
      fetch("/api/v1/providers", { headers, signal: controller.signal })
        .then(async (res) => (res.ok ? ((await res.json()) as unknown) : []))
        .catch(() => []),
      fetch("/api/v1/trace-source-plugins", {
        headers,
        signal: controller.signal,
      })
        .then(async (res) => (res.ok ? ((await res.json()) as unknown) : []))
        .catch(() => []),
    ]).then(([provData, pluginData]) => {
      if (controller.signal.aborted) return;
      if (Array.isArray(provData)) {
        setProviders(provData as Provider[]);
      }
      if (Array.isArray(pluginData)) {
        setPlugins(pluginData as TraceSourcePlugin[]);
      }
    });
    return () => controller.abort();
  }, [token]);

  const loadProjectData = useCallback(
    async (targetProject: string, signal?: AbortSignal) => {
      const gen = requestGenRef.current;
      const headers = { Authorization: `Bearer ${token}` };
      const encoded = encodeURIComponent(targetProject);
      const [cfgRes, runsRes] = await Promise.all([
        fetch(`/api/v1/projects/${encoded}/evolution/config`, {
          headers,
          signal,
        }),
        fetch(`/api/v1/projects/${encoded}/evolution/runs`, {
          headers,
          signal,
        }),
      ]);
      if (!cfgRes.ok) {
        throw new Error(await readErrorDetail(cfgRes, t.error));
      }
      if (!runsRes.ok) {
        throw new Error(await readErrorDetail(runsRes, t.error));
      }
      const nextConfig = (await cfgRes.json()) as EvolutionConfig;
      const nextRuns = (await runsRes.json()) as EvolutionRun[];
      if (
        signal?.aborted ||
        requestGenRef.current !== gen ||
        projectRef.current !== targetProject
      ) {
        return;
      }
      setConfig({
        ...DEFAULT_CONFIG,
        ...nextConfig,
        trace_source_parameters: nextConfig.trace_source_parameters ?? {},
        trace_source_secrets: nextConfig.trace_source_secrets ?? {},
        configured_secrets: nextConfig.configured_secrets ?? [],
      });
      const safeRuns = Array.isArray(nextRuns) ? nextRuns : [];
      setRuns(safeRuns);
      setSelectedRunId((prev) =>
        prev && safeRuns.some((r) => r.id === prev)
          ? prev
          : (safeRuns[0]?.id ?? ""),
      );
    },
    [token, t.error],
  );

  useEffect(() => {
    if (activeTab !== "evolution") return;
    const controller = new AbortController();
    requestGenRef.current += 1;
    setLoading(true);
    setError("");
    setSaveNotice("");
    void loadProjectData(project, controller.signal)
      .catch((failure) => {
        if (!controller.signal.aborted && projectRef.current === project) {
          setError(failure instanceof Error ? failure.message : t.error);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [activeTab, loadProjectData, project, t.error]);

  const hasRunning = runs.some((r) => r.status === "running");

  useEffect(() => {
    if (activeTab !== "evolution" || !hasRunning) return;
    const timer = window.setInterval(() => {
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      void loadProjectData(projectRef.current)
        .catch(() => {
          // Keep last valid state on transient poll error
        })
        .finally(() => {
          pollInFlightRef.current = false;
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [activeTab, hasRunning, loadProjectData, project]);

  function handleProjectChange(nextProject: string) {
    requestGenRef.current += 1;
    projectRef.current = nextProject;
    setSelectedRunId("");
    setRuns([]);
    setConfig({ ...DEFAULT_CONFIG, project_id: nextProject });
    setError("");
    setSaveNotice("");
    setProject(nextProject);
  }

  const activePlugin = useMemo(
    () =>
      plugins.find((item) => item.id === config.trace_source_plugin) ?? null,
    [plugins, config.trace_source_plugin],
  );

  const activeSchema = (activePlugin?.parameters_schema ?? {
    type: "object",
    properties: {},
  }) as ParamSchema;

  const activeSecretKeys = useMemo(() => {
    const keys = new Set<string>(activePlugin?.secret_parameter_keys ?? []);
    Object.entries(activeSchema.properties || {}).forEach(([key, prop]) => {
      if (prop.format === "password") keys.add(key);
    });
    return Array.from(keys);
  }, [activePlugin, activeSchema]);

  const configuredSecretsSet = useMemo(() => {
    const set = new Set<string>(config.configured_secrets ?? []);
    for (const [key, val] of Object.entries(
      config.trace_source_secrets ?? {},
    )) {
      if (val) set.add(key);
    }
    return set;
  }, [config.configured_secrets, config.trace_source_secrets]);

  async function saveConfig() {
    if (isArchivedProject) return;
    const targetProject = project;
    setSaving(true);
    setError("");
    setSaveNotice("");

    const payload: EvolutionConfigUpdate = {
      enabled: config.enabled,
      trace_source_plugin: config.trace_source_plugin || null,
      schedule_cron: config.schedule_cron?.trim() || null,
      timezone: config.timezone?.trim() || "Asia/Shanghai",
      max_traces_per_run: config.max_traces_per_run,
      evolution_provider_id: config.evolution_provider_id ?? null,
      evolution_model: config.evolution_model ?? null,
    };

    if (config.trace_source_plugin && activePlugin) {
      const split = splitTraceSourcePayload(
        activeSchema,
        config.trace_source_parameters ?? {},
        config.trace_source_secrets ?? {},
        activeSecretKeys,
        configuredSecretsSet,
      );
      payload.trace_source_parameters = split.parameters;
      if (Object.keys(split.secrets).length > 0) {
        payload.trace_source_secrets = split.secrets;
      }
    }

    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(targetProject)}/evolution/config`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const updated = (await response.json()) as EvolutionConfig;
      if (projectRef.current === targetProject) {
        setConfig({
          ...DEFAULT_CONFIG,
          ...updated,
          trace_source_parameters: updated.trace_source_parameters ?? {},
          trace_source_secrets: updated.trace_source_secrets ?? {},
          configured_secrets: updated.configured_secrets ?? [],
        });
        setSaveNotice(t.evolutionConfigSaved);
      }
    } catch (failure) {
      if (projectRef.current === targetProject) {
        setError(failure instanceof Error ? failure.message : t.error);
      }
    } finally {
      setSaving(false);
    }
  }

  async function triggerRun() {
    if (isArchivedProject) return;
    const targetProject = project;
    setTriggering(true);
    setError("");
    const payload: EvolutionRunTrigger = {};
    const parsedSince = toIsoUtc(sinceInput);
    const parsedUntil = toIsoUtc(untilInput);
    if (parsedSince) payload.since = parsedSince;
    if (parsedUntil) payload.until = parsedUntil;
    const parsedMax = Number.parseInt(maxTracesOverride.trim(), 10);
    if (Number.isFinite(parsedMax) && parsedMax > 0) {
      payload.max_traces = parsedMax;
    }

    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(targetProject)}/evolution/runs`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const created = (await response.json()) as EvolutionRun;
      if (projectRef.current === targetProject) {
        setRuns((prev) => [
          created,
          ...prev.filter((r) => r.id !== created.id),
        ]);
        setSelectedRunId(created.id);
      }
    } catch (failure) {
      if (projectRef.current === targetProject) {
        setError(failure instanceof Error ? failure.message : t.error);
      }
    } finally {
      setTriggering(false);
    }
  }

  const selectedRun = useMemo(
    () => runs.find((r) => r.id === selectedRunId) ?? runs[0] ?? null,
    [runs, selectedRunId],
  );

  const selectedProposalIds = useMemo(() => {
    if (!selectedRun) return [];
    const ids = selectedRun.proposal_ids ?? [];
    if (ids.length > 0) return ids;
    return selectedRun.proposal_id ? [selectedRun.proposal_id] : [];
  }, [selectedRun]);

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {activeTab === "discovery" ? t.discoveryTitle : t.evolutionTitle}
          </h1>
          <p className="page-subtitle">
            {activeTab === "discovery" ? t.discoveryHint : t.evolutionHint}
          </p>
        </div>
        {activeTab === "evolution" && (
          <Button
            autoInsertSpace={false}
            onClick={() => void loadProjectData(projectRef.current)}
            loading={loading}
          >
            {t.refresh}
          </Button>
        )}
        <ProjectSelector
          token={token}
          value={project}
          onChange={handleProjectChange}
          messages={t}
          includeArchived
        />
      </div>

      <div className="correction-filters" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "evolution"}
          className={activeTab === "evolution" ? "active" : ""}
          onClick={() => setActiveTab("evolution")}
        >
          {t.evolutionTitle}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "discovery"}
          className={activeTab === "discovery" ? "active" : ""}
          onClick={() => setActiveTab("discovery")}
        >
          {t.discoveryTitle}
        </button>
      </div>

      {activeTab === "discovery" ? (
        <DiscoveryPanel
          token={token}
          project={project}
          projectInfo={currentProject}
          providers={providers}
          messages={t}
          onNavigateCorrections={onNavigateCorrections}
          onNavigatePersonalSettings={onNavigatePersonalSettings}
        />
      ) : (
        <>
          {isArchivedProject && (
            <Alert type="warning" showIcon title={t.projectArchivedNotice} />
          )}
          {error && <Alert type="error" title={error} showIcon />}
          {saveNotice && <Alert type="success" title={saveNotice} showIcon />}

          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.evolutionTitle}</h2>
              <Space size={8} wrap>
                <Tag color="blue">
                  {t.evolutionLastWatermark}:{" "}
                  {formatDateTime(config.last_trace_timestamp)}
                </Tag>
                <Tag color="cyan">
                  {t.evolutionNextRunAt}: {formatDateTime(config.next_run_at)}
                </Tag>
                {config.last_triggered_at && (
                  <Tag>
                    {t.evolutionLastTriggeredAt}:{" "}
                    {formatDateTime(config.last_triggered_at)}
                  </Tag>
                )}
                {config.global_langfuse_configured &&
                  !config.trace_source_plugin && (
                    <Tag color="green">{t.evolutionGlobalLangfuseBadge}</Tag>
                  )}
              </Space>
            </div>

            <div className="evolution-config-grid">
              <label className="evolution-checkbox">
                <input
                  type="checkbox"
                  disabled={isArchivedProject}
                  checked={config.enabled}
                  onChange={(e) =>
                    setConfig((prev) => ({
                      ...prev,
                      enabled: e.target.checked,
                    }))
                  }
                />
                <span>{t.evolutionEnabled}</span>
              </label>

              <div className="evolution-field">
                <label htmlFor="evo-trace-plugin">
                  {t.evolutionTraceSourcePlugin}
                </label>
                <select
                  id="evo-trace-plugin"
                  disabled={isArchivedProject}
                  value={config.trace_source_plugin ?? ""}
                  onChange={(e) => {
                    const nextPlugin = e.target.value || null;
                    setConfig((prev) => ({
                      ...prev,
                      trace_source_plugin: nextPlugin,
                      trace_source_parameters:
                        nextPlugin === prev.trace_source_plugin
                          ? prev.trace_source_parameters
                          : {},
                      trace_source_secrets:
                        nextPlugin === prev.trace_source_plugin
                          ? prev.trace_source_secrets
                          : {},
                      configured_secrets:
                        nextPlugin === prev.trace_source_plugin
                          ? prev.configured_secrets
                          : [],
                    }));
                  }}
                >
                  <option value="">{t.evolutionTraceSourceNone}</option>
                  {plugins.map((plugin) => (
                    <option key={plugin.id} value={plugin.id}>
                      {plugin.display_name} ({plugin.id})
                    </option>
                  ))}
                </select>
              </div>

              <div className="evolution-field">
                <label htmlFor="evo-schedule-cron">
                  {t.evolutionScheduleCron}
                </label>
                <input
                  id="evo-schedule-cron"
                  type="text"
                  disabled={isArchivedProject}
                  placeholder="02:30"
                  value={config.schedule_cron ?? ""}
                  onChange={(e) =>
                    setConfig((prev) => ({
                      ...prev,
                      schedule_cron: e.target.value,
                    }))
                  }
                />
              </div>

              <div className="evolution-field">
                <label htmlFor="evo-timezone">{t.evolutionTimezone}</label>
                <input
                  id="evo-timezone"
                  type="text"
                  disabled={isArchivedProject}
                  value={config.timezone ?? "Asia/Shanghai"}
                  onChange={(e) =>
                    setConfig((prev) => ({
                      ...prev,
                      timezone: e.target.value,
                    }))
                  }
                />
              </div>

              <div className="evolution-field">
                <label htmlFor="evo-max-traces">
                  {t.evolutionMaxTracesPerRun}
                </label>
                <input
                  id="evo-max-traces"
                  type="number"
                  min={1}
                  max={2000}
                  disabled={isArchivedProject}
                  value={config.max_traces_per_run}
                  onChange={(e) =>
                    setConfig((prev) => ({
                      ...prev,
                      max_traces_per_run: Number(e.target.value) || 200,
                    }))
                  }
                />
              </div>

              <ProviderModelPair
                idPrefix="evo-evolver"
                providerLabel={t.evolutionProviderLabel}
                modelLabel={t.evolutionModelLabel}
                defaultLabel={t.evolutionModelDefault}
                providerId={config.evolution_provider_id}
                model={config.evolution_model}
                providers={providers}
                disabled={isArchivedProject}
                onProviderChange={(next) =>
                  setConfig((prev) => ({
                    ...prev,
                    evolution_provider_id: next,
                    evolution_model: null,
                  }))
                }
                onModelChange={(next) =>
                  setConfig((prev) => ({
                    ...prev,
                    evolution_model: next,
                  }))
                }
              />
            </div>

            <p className="field-hint" style={{ marginTop: 8 }}>
              {t.pullScheduleCronHint}
            </p>

            {activePlugin &&
              Object.keys(activeSchema.properties || {}).length > 0 && (
                <div
                  className="evolution-config-grid"
                  style={{ marginTop: 12 }}
                >
                  {Object.entries(activeSchema.properties || {}).map(
                    ([paramKey, field]) => {
                      const secret = isSecretParam(
                        paramKey,
                        field,
                        activeSecretKeys,
                      );
                      const isNumeric =
                        field.type === "integer" || field.type === "number";
                      const rawValue = secret
                        ? (config.trace_source_secrets?.[paramKey] ?? "")
                        : (config.trace_source_parameters?.[paramKey] ??
                          field.default ??
                          "");
                      const labelText = `${field.title || paramKey}${activeSchema.required?.includes(paramKey) ? " *" : ""}`;
                      const inputId = `evo-ts-${paramKey}`;
                      if (field.enum) {
                        return (
                          <div className="evolution-field" key={paramKey}>
                            <label htmlFor={inputId}>{labelText}</label>
                            <Select
                              id={inputId}
                              aria-label={labelText}
                              disabled={isArchivedProject}
                              value={
                                typeof rawValue === "string"
                                  ? rawValue
                                  : undefined
                              }
                              onChange={(val) =>
                                setConfig((prev) => ({
                                  ...prev,
                                  trace_source_parameters: {
                                    ...(prev.trace_source_parameters ?? {}),
                                    [paramKey]: val,
                                  },
                                }))
                              }
                              options={field.enum.map((opt) => ({
                                value: opt,
                                label: String(opt),
                              }))}
                            />
                          </div>
                        );
                      }
                      if (field.type === "boolean") {
                        return (
                          <label className="evolution-checkbox" key={paramKey}>
                            <Checkbox
                              aria-label={labelText}
                              disabled={isArchivedProject}
                              checked={rawValue === true}
                              onChange={(e) =>
                                setConfig((prev) => ({
                                  ...prev,
                                  trace_source_parameters: {
                                    ...(prev.trace_source_parameters ?? {}),
                                    [paramKey]: e.target.checked,
                                  },
                                }))
                              }
                            >
                              {labelText}
                            </Checkbox>
                          </label>
                        );
                      }
                      return (
                        <div className="evolution-field" key={paramKey}>
                          <label htmlFor={inputId}>{labelText}</label>
                          <Input
                            id={inputId}
                            aria-label={labelText}
                            disabled={isArchivedProject}
                            type={
                              secret
                                ? "password"
                                : isNumeric
                                  ? "number"
                                  : "text"
                            }
                            placeholder={field.description}
                            value={rawValue == null ? "" : String(rawValue)}
                            onChange={(e) => {
                              const nextVal = e.target.value;
                              if (secret) {
                                setConfig((prev) => ({
                                  ...prev,
                                  trace_source_secrets: {
                                    ...(prev.trace_source_secrets ?? {}),
                                    [paramKey]: nextVal,
                                  },
                                }));
                              } else {
                                setConfig((prev) => ({
                                  ...prev,
                                  trace_source_parameters: {
                                    ...(prev.trace_source_parameters ?? {}),
                                    [paramKey]:
                                      isNumeric && nextVal !== ""
                                        ? Number(nextVal)
                                        : nextVal,
                                  },
                                }));
                              }
                            }}
                          />
                        </div>
                      );
                    },
                  )}
                </div>
              )}

            {activePlugin && configuredSecretsSet.size > 0 && (
              <p className="field-hint" style={{ marginTop: 8 }}>
                {t.pullSecretMaskHint}
              </p>
            )}

            <div style={{ marginTop: 12 }}>
              <Button
                type="primary"
                autoInsertSpace={false}
                disabled={isArchivedProject}
                onClick={() => void saveConfig()}
                loading={saving}
              >
                {t.evolutionSaveConfig}
              </Button>
            </div>
          </section>

          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.evolutionManualSectionTitle}</h2>
            </div>
            <div className="evolution-config-grid">
              <div className="evolution-field">
                <label htmlFor="evo-trigger-since">
                  {t.evolutionSinceLabel}
                </label>
                <input
                  id="evo-trigger-since"
                  aria-label={t.evolutionSinceLabel}
                  type="datetime-local"
                  disabled={isArchivedProject}
                  value={sinceInput}
                  onChange={(e) => setSinceInput(e.target.value)}
                />
              </div>
              <div className="evolution-field">
                <label htmlFor="evo-trigger-until">
                  {t.evolutionUntilLabel}
                </label>
                <input
                  id="evo-trigger-until"
                  aria-label={t.evolutionUntilLabel}
                  type="datetime-local"
                  disabled={isArchivedProject}
                  value={untilInput}
                  onChange={(e) => setUntilInput(e.target.value)}
                />
              </div>
              <div className="evolution-field">
                <label htmlFor="evo-trigger-max">
                  {t.evolutionMaxTracesOverride}
                </label>
                <input
                  id="evo-trigger-max"
                  aria-label={t.evolutionMaxTracesOverride}
                  type="number"
                  min={1}
                  max={2000}
                  disabled={isArchivedProject}
                  placeholder={String(config.max_traces_per_run)}
                  value={maxTracesOverride}
                  onChange={(e) => setMaxTracesOverride(e.target.value)}
                />
              </div>
            </div>
            <div style={{ marginTop: 12 }}>
              <Button
                type="primary"
                autoInsertSpace={false}
                onClick={() => void triggerRun()}
                loading={triggering}
                disabled={isArchivedProject || hasRunning}
              >
                {t.triggerEvolution}
              </Button>
            </div>
          </section>

          <div className="evolution-layout">
            <section className="card section-card">
              <div className="card-title-row">
                <h2 className="card-title">{t.evolutionRunsTitle}</h2>
                <span className="muted">{runs.length}</span>
              </div>
              {runs.length === 0 ? (
                <div className="empty-state">{t.evolutionEmptyRuns}</div>
              ) : (
                <ul className="correction-list">
                  {runs.map((run) => (
                    <li key={run.id}>
                      <button
                        type="button"
                        className={selectedRun?.id === run.id ? "active" : ""}
                        onClick={() => setSelectedRunId(run.id)}
                      >
                        <span className="mono">
                          {run.trigger_type === "scheduled" ||
                          run.trigger_type === "scheduler"
                            ? t.evolutionTriggerScheduled
                            : t.evolutionTriggerManual}{" "}
                          · {formatDateTime(run.created_at)}
                        </span>
                        <Space size={4}>
                          <Tag>
                            {t.evolutionProposalsCreated}:{" "}
                            {run.proposals_created ?? 0}
                          </Tag>
                          <Tag color={runStatusColor(run.status)}>
                            {runStatusLabel(run.status, t)}
                          </Tag>
                        </Space>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="card section-card">
              {selectedRun ? (
                <div className="stack">
                  <div className="card-title-row">
                    <div>
                      <h2 className="card-title mono">{selectedRun.id}</h2>
                      <div className="muted">
                        {formatDateTime(selectedRun.started_at)} →{" "}
                        {formatDateTime(selectedRun.finished_at)}
                      </div>
                    </div>
                    <Space size={8} wrap>
                      {selectedRun.trace_source_plugin && (
                        <Tag>{selectedRun.trace_source_plugin}</Tag>
                      )}
                      <Tag color={runStatusColor(selectedRun.status)}>
                        {runStatusLabel(selectedRun.status, t)}
                      </Tag>
                    </Space>
                  </div>

                  {selectedRun.error && (
                    <Alert type="error" title={selectedRun.error} showIcon />
                  )}

                  <div className="discovery-metrics-grid">
                    <Tag>
                      {t.evolutionTracesFetched}:{" "}
                      {selectedRun.traces_fetched ?? 0}
                    </Tag>
                    <Tag color="blue">
                      {t.evolutionProposalsCreated}:{" "}
                      {selectedRun.proposals_created ?? 0}
                    </Tag>
                    <Tag>
                      {t.evolutionDuplicatesSkipped}:{" "}
                      {selectedRun.duplicates_skipped ?? 0}
                    </Tag>
                    <Tag color="orange">
                      {t.evolutionL1Rejected}: {selectedRun.l1_rejected ?? 0}
                    </Tag>
                  </div>

                  {(selectedRun.window_since || selectedRun.window_until) && (
                    <div className="muted mono">
                      {t.evolutionWindowLabel}:{" "}
                      {formatDateTime(selectedRun.window_since)} →{" "}
                      {formatDateTime(selectedRun.window_until)}
                    </div>
                  )}

                  {selectedRun.summary && (
                    <div className="evolution-round-card">
                      <strong>{t.evolutionSummaryLabel}</strong>
                      <div>{selectedRun.summary}</div>
                    </div>
                  )}

                  {selectedProposalIds.length > 0 && (
                    <Space size={8} wrap>
                      {selectedProposalIds.map((pid) => (
                        <Button
                          key={pid}
                          type="primary"
                          size="small"
                          autoInsertSpace={false}
                          onClick={() => onNavigateCorrections?.(pid)}
                        >
                          {selectedProposalIds.length === 1
                            ? t.viewEvolutionProposal
                            : `${t.viewEvolutionProposal} (${pid.slice(0, 8)})`}
                        </Button>
                      ))}
                    </Space>
                  )}
                </div>
              ) : (
                <div className="empty-state">{t.evolutionEmptyRuns}</div>
              )}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
