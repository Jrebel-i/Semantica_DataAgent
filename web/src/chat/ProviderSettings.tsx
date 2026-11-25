import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Checkbox, Input, Tag } from "antd";
import { CheckOutlined, CopyOutlined } from "@ant-design/icons";
import { modelProtocol } from "../agentProfiles";
import { EFFORT_LEVELS, PROVIDER_TYPES } from "./types";
import type { EffortLevel, Provider, ProviderProbeResult } from "./types";
import type { Messages } from "../i18n";
import { readErrorDetail } from "../httpErrors";

export type ProviderModelCapabilityForm = {
  description: string;
  vision: boolean;
  effort_levels: EffortLevel[];
};

export type ProviderForm = {
  name: string;
  type: string;
  base_url: string;
  api_key: string;
  clear_api_key: boolean;
  model_capabilities: Record<string, ProviderModelCapabilityForm>;
};

const EMPTY_CAPABILITY: ProviderModelCapabilityForm = {
  description: "",
  vision: false,
  effort_levels: [],
};

function normalizeBaseUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function protocolBadge(modelId: string, t: Messages): string {
  const proto = modelProtocol(modelId);
  return proto === "unknown" ? t.agentProfileProtocolUnknown : proto;
}

function capabilitiesFromProvider(
  provider: Provider,
): Record<string, ProviderModelCapabilityForm> {
  const catalogList = provider.catalog_models?.length
    ? provider.catalog_models
    : provider.models;
  return Object.fromEntries(
    catalogList.map((model) => {
      const capability = provider.model_capabilities?.[model];
      return [
        model,
        {
          description: capability?.description ?? "",
          vision: capability?.vision ?? false,
          effort_levels: [...(capability?.effort_levels ?? [])],
        },
      ];
    }),
  );
}

function catalogStatusFeedback(
  provider: Provider,
  t: Messages,
): { type: "success" | "warning" | "error"; text: string } | null {
  const status = provider.catalog_status;
  const detail = provider.catalog_error || t.providerError;
  if (status === "ok") {
    const count = (provider.catalog_models ?? provider.models ?? []).length;
    return {
      type: "success",
      text: t.providerRefreshSuccess.replace("{count}", String(count)),
    };
  }
  if (status === "empty") {
    return { type: "warning", text: t.providerCatalogEmpty };
  }
  if (status === "auth_failed") {
    return {
      type: "error",
      text: t.providerCatalogAuthFailed.replace("{detail}", detail),
    };
  }
  if (status === "timeout") {
    return {
      type: "error",
      text: t.providerCatalogTimeout.replace("{detail}", detail),
    };
  }
  if (status === "unsupported_protocol") {
    return {
      type: "error",
      text: t.providerCatalogUnsupported.replace("{detail}", detail),
    };
  }
  if (status === "error") {
    return {
      type: "error",
      text: t.providerCatalogError.replace("{detail}", detail),
    };
  }
  return null;
}

export function ProviderSettingsPage({
  token,
  role,
  messages: t,
}: {
  token: string;
  role: string;
  messages: Messages;
}) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [editingProviderId, setEditingProviderId] = useState("");
  const [originalBaseUrl, setOriginalBaseUrl] = useState("");
  const [providerForm, setProviderForm] = useState<ProviderForm>({
    name: "",
    type: "anthropic-messages",
    base_url: "",
    api_key: "",
    clear_api_key: false,
    model_capabilities: {},
  });
  const [providerError, setProviderError] = useState("");
  const [providerNotice, setProviderNotice] = useState("");
  const [refreshingId, setRefreshingId] = useState("");
  const [catalogNotice, setCatalogNotice] = useState<{
    type: "success" | "warning" | "error";
    text: string;
  } | null>(null);
  const [probeResults, setProbeResults] = useState<
    Record<string, ProviderProbeResult>
  >({});
  const [probeLoadingModel, setProbeLoadingModel] = useState("");
  const [probeError, setProbeError] = useState("");

  const loadProviders = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const data = (await response.json()) as Provider[];
      setProviders(Array.isArray(data) ? data : []);
    } catch {
      setProviderError(t.providerError);
    }
  }, [token, t.providerError]);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  function editProvider(provider: Provider) {
    setEditingProviderId(provider.id);
    setOriginalBaseUrl(provider.base_url);
    setProviderForm({
      name: provider.name,
      type: provider.type,
      base_url: provider.base_url,
      api_key: "",
      clear_api_key: false,
      model_capabilities: capabilitiesFromProvider(provider),
    });
    setProviderError("");
    setProviderNotice("");
    setCatalogNotice(null);
    setProbeResults({});
    setProbeLoadingModel("");
    setProbeError("");
  }

  function resetProviderForm() {
    setEditingProviderId("");
    setOriginalBaseUrl("");
    setProviderForm({
      name: "",
      type: "anthropic-messages",
      base_url: "",
      api_key: "",
      clear_api_key: false,
      model_capabilities: {},
    });
    setCatalogNotice(null);
    setProbeResults({});
    setProbeLoadingModel("");
    setProbeError("");
  }

  async function refreshModels(providerId: string) {
    if (!providerId) return;
    setRefreshingId(providerId);
    setCatalogNotice(null);
    setProviderError("");
    try {
      const response = await fetch(
        `/api/v1/providers/${encodeURIComponent(providerId)}/refresh-models`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(response, t.providerError);
        setCatalogNotice({
          type: "error",
          text: t.providerCatalogError.replace("{detail}", detail),
        });
        return;
      }
      const updated = (await response.json()) as Provider;
      setProviders((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      if (editingProviderId === updated.id) {
        setProviderForm((current) => ({
          ...current,
          model_capabilities: capabilitiesFromProvider(updated),
        }));
      }
      const feedback = catalogStatusFeedback(updated, t);
      if (feedback) setCatalogNotice(feedback);
    } catch {
      setCatalogNotice({
        type: "error",
        text: t.providerCatalogError.replace("{detail}", t.providerError),
      });
    } finally {
      setRefreshingId("");
    }
  }

  async function probeProviderModel(providerId: string, model: string) {
    if (!providerId) return;
    setProbeError("");
    setProbeLoadingModel(model);
    try {
      const response = await fetch(
        `/api/v1/providers/${encodeURIComponent(providerId)}/probe`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ model }),
        },
      );
      if (response.status === 409) {
        setProbeError(t.providerProbeBusy);
        return;
      }
      if (!response.ok) throw new Error();
      const data = (await response.json()) as ProviderProbeResult;
      setProbeResults((current) => ({ ...current, [model]: data }));
    } catch {
      setProbeError(t.providerProbeFailed);
    } finally {
      setProbeLoadingModel("");
    }
  }

  function applyProbeResult(model: string) {
    const result = probeResults[model];
    if (!result) return;
    setProviderForm((current) => ({
      ...current,
      model_capabilities: {
        ...current.model_capabilities,
        [model]: {
          ...(current.model_capabilities[model] ?? EMPTY_CAPABILITY),
          vision: result.vision === "yes",
          effort_levels: EFFORT_LEVELS.filter(
            (level) => result.effort[level] === "yes",
          ),
        },
      },
    }));
  }

  async function saveProvider(event: FormEvent) {
    event.preventDefault();
    setProviderError("");
    setProviderNotice("");

    const baseUrlChanged =
      Boolean(editingProviderId) &&
      normalizeBaseUrl(providerForm.base_url) !==
        normalizeBaseUrl(originalBaseUrl);
    if (
      baseUrlChanged &&
      !providerForm.api_key.trim() &&
      !providerForm.clear_api_key
    ) {
      setProviderError(t.providerBaseUrlChangeKeyRequired);
      return;
    }

    const payload: Record<string, unknown> = {
      name: providerForm.name.trim(),
      type: providerForm.type,
      base_url: providerForm.base_url.trim(),
    };
    if (editingProviderId) {
      payload.model_capabilities = providerForm.model_capabilities;
    }
    if (providerForm.api_key.trim()) {
      payload.api_key = providerForm.api_key;
    } else if (baseUrlChanged && providerForm.clear_api_key) {
      payload.api_key = "";
    }

    try {
      const response = await fetch(
        editingProviderId
          ? `/api/v1/providers/${encodeURIComponent(editingProviderId)}`
          : "/api/v1/providers",
        {
          method: editingProviderId ? "PUT" : "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(response, t.providerError);
        setProviderError(detail);
        return;
      }
      const saved = (await response.json()) as Provider;
      setProviderNotice(t.providerSaved);
      await loadProviders();
      if (!editingProviderId && saved?.id) {
        editProvider(saved);
        setProviderNotice(t.providerSaved);
      } else {
        setOriginalBaseUrl(saved.base_url);
        setProviderForm((current) => ({
          ...current,
          api_key: "",
          clear_api_key: false,
        }));
      }
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteProvider(provider: Provider) {
    if (!window.confirm(t.providerDeleteConfirm)) return;
    setProviderError("");
    try {
      const response = await fetch(
        `/api/v1/providers/${encodeURIComponent(provider.id)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) throw new Error();
      if (editingProviderId === provider.id) resetProviderForm();
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  const editingProvider = useMemo(
    () => providers.find((p) => p.id === editingProviderId) ?? null,
    [providers, editingProviderId],
  );

  return (
    <ProviderSettings
      messages={t}
      role={role}
      providers={providers}
      providerError={providerError}
      providerNotice={providerNotice}
      providerForm={providerForm}
      setProviderForm={setProviderForm}
      editingProviderId={editingProviderId}
      editingProvider={editingProvider}
      originalBaseUrl={originalBaseUrl}
      refreshingId={refreshingId}
      catalogNotice={catalogNotice}
      probeResults={probeResults}
      probeLoadingModel={probeLoadingModel}
      probeError={probeError}
      onEditProvider={editProvider}
      onDeleteProvider={(provider) => void deleteProvider(provider)}
      onSaveProvider={saveProvider}
      onResetProviderForm={resetProviderForm}
      onRefreshModels={(id) => void refreshModels(id)}
      onProbeModel={(id, model) => void probeProviderModel(id, model)}
      onApplyProbe={applyProbeResult}
    />
  );
}

export function ProviderSettings({
  messages: t,
  role,
  providers,
  providerError,
  providerNotice,
  providerForm,
  setProviderForm,
  editingProviderId,
  editingProvider = null,
  originalBaseUrl = "",
  refreshingId = "",
  catalogNotice = null,
  probeResults,
  probeLoadingModel,
  probeError,
  onEditProvider,
  onDeleteProvider,
  onSaveProvider,
  onResetProviderForm,
  onRefreshModels,
  onProbeModel,
  onApplyProbe,
}: {
  messages: Messages;
  role: string;
  providers: Provider[];
  providerError: string;
  providerNotice: string;
  providerForm: ProviderForm;
  setProviderForm: (value: ProviderForm) => void;
  editingProviderId: string;
  editingProvider?: Provider | null;
  originalBaseUrl?: string;
  refreshingId?: string;
  catalogNotice?: {
    type: "success" | "warning" | "error";
    text: string;
  } | null;
  probeResults: Record<string, ProviderProbeResult>;
  probeLoadingModel: string;
  probeError: string;
  onEditProvider: (provider: Provider) => void;
  onDeleteProvider: (provider: Provider) => void;
  onSaveProvider: (event: FormEvent) => void;
  onResetProviderForm: () => void;
  onRefreshModels?: (providerId: string) => void;
  onProbeModel: (providerId: string, model: string) => void;
  onApplyProbe: (model: string) => void;
}) {
  const [copiedId, setCopiedId] = useState("");

  async function copyTextValue(key: string, value: string) {
    try {
      await navigator.clipboard?.writeText(value);
      setCopiedId(key);
      window.setTimeout(() => {
        setCopiedId((curr) => (curr === key ? "" : curr));
      }, 1500);
    } catch {
      /* Clipboard unavailable */
    }
  }

  const resolvedEditingProvider =
    editingProvider ??
    providers.find((item) => item.id === editingProviderId) ??
    null;
  const catalogModels = resolvedEditingProvider
    ? resolvedEditingProvider.catalog_models?.length
      ? resolvedEditingProvider.catalog_models
      : resolvedEditingProvider.models
    : [];

  const baseUrlChanged =
    Boolean(editingProviderId) &&
    normalizeBaseUrl(providerForm.base_url) !==
      normalizeBaseUrl(originalBaseUrl);

  const effortOptions: { value: EffortLevel; label: string }[] = [
    { value: "low", label: t.effortLow },
    { value: "medium", label: t.effortMedium },
    { value: "high", label: t.effortHigh },
    { value: "extra", label: t.effortExtra },
    { value: "max", label: t.effortMax },
  ];
  const updateCapability = (
    model: string,
    patch: Partial<ProviderModelCapabilityForm>,
  ) =>
    setProviderForm({
      ...providerForm,
      model_capabilities: {
        ...providerForm.model_capabilities,
        [model]: {
          ...(providerForm.model_capabilities[model] ?? EMPTY_CAPABILITY),
          ...patch,
        },
      },
    });
  const outcomeLabel = (outcome: ProviderProbeResult["vision"]) =>
    outcome === "yes"
      ? t.providerProbeYes
      : outcome === "no"
        ? t.providerProbeNo
        : t.providerProbeUnknown;
  const outcomeMark = (outcome: ProviderProbeResult["vision"]) =>
    outcome === "yes" ? "✓" : outcome === "no" ? "✕" : "?";

  return (
    <div className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.providerSettings}</h1>
          <p className="page-subtitle">{t.providerSelectToEditHint}</p>
        </div>
        {role === "admin" && editingProviderId && (
          <div className="page-actions">
            <Button onClick={onResetProviderForm}>{t.providerAdd}</Button>
          </div>
        )}
      </div>

      {providerError && <Alert type="error" title={providerError} />}
      {providerNotice && <Alert type="success" title={providerNotice} />}

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.providerSettings}</h2>
          <span className="muted">{providers.length}</span>
        </div>
        {providers.length === 0 ? (
          <p className="muted">{t.noProviders}</p>
        ) : (
          <div className="provider-list">
            {providers.map((provider) => {
              const count = (
                provider.catalog_models?.length
                  ? provider.catalog_models
                  : provider.models
              ).length;
              return (
                <div className="provider-card" key={provider.id}>
                  <div className="provider-line">
                    <strong>{provider.name}</strong>
                    <span className="subtle">{provider.type}</span>
                    <Tag color={provider.has_api_key ? "green" : "default"}>
                      {provider.has_api_key
                        ? t.providerConfigured
                        : t.providerNotConfigured}
                    </Tag>
                    <code className="muted">{provider.base_url}</code>
                    <code className="muted">{provider.id}</code>
                    <Button
                      size="small"
                      type="text"
                      icon={
                        copiedId === provider.id ? (
                          <CheckOutlined />
                        ) : (
                          <CopyOutlined />
                        )
                      }
                      aria-label={`${t.providerCopyId} ${provider.name}`}
                      onClick={() =>
                        void copyTextValue(provider.id, provider.id)
                      }
                    >
                      {copiedId === provider.id
                        ? t.agentProfileCopied
                        : t.providerCopyId}
                    </Button>
                    <span className="subtle">
                      {t.providerCatalogCount.replace("{count}", String(count))}
                    </span>
                    {role === "admin" && (
                      <>
                        <Button
                          type="link"
                          onClick={() => onEditProvider(provider)}
                        >
                          {t.providerEdit}
                        </Button>
                        <Button
                          type="link"
                          danger
                          onClick={() => onDeleteProvider(provider)}
                        >
                          {t.providerDelete}
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {role === "admin" && (
        <section className="card section-card stack">
          <form onSubmit={(event) => void onSaveProvider(event)}>
            <div className="card-title-row">
              <h2 className="card-title">
                {editingProviderId ? t.providerEdit : t.providerAdd}
              </h2>
              {editingProviderId && (
                <div className="row">
                  <span className="small muted">{t.providerInternalId}:</span>
                  <code>{editingProviderId}</code>
                  <Button
                    size="small"
                    type="text"
                    icon={<CopyOutlined />}
                    aria-label={t.providerCopyId}
                    onClick={() =>
                      void copyTextValue("edit-id", editingProviderId)
                    }
                  >
                    {copiedId === "edit-id"
                      ? t.agentProfileCopied
                      : t.providerCopyId}
                  </Button>
                </div>
              )}
            </div>

            <div className="grid-2">
              <div className="field">
                <label className="field-label" htmlFor="provider-name">
                  {t.providerName}
                </label>
                <Input
                  id="provider-name"
                  aria-label={t.providerName}
                  value={providerForm.name}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      name: event.target.value,
                    })
                  }
                  required
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="provider-type">
                  {t.providerType}
                </label>
                <select
                  id="provider-type"
                  aria-label={t.providerType}
                  value={providerForm.type}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      type: event.target.value,
                    })
                  }
                >
                  {PROVIDER_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label className="field-label" htmlFor="provider-url">
                  {t.providerBaseUrl}
                </label>
                <Input
                  id="provider-url"
                  aria-label={t.providerBaseUrl}
                  value={providerForm.base_url}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      base_url: event.target.value,
                    })
                  }
                  required
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="provider-key">
                  {t.providerApiKey}
                </label>
                <Input.Password
                  id="provider-key"
                  aria-label={t.providerApiKey}
                  value={providerForm.api_key}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      api_key: event.target.value,
                      ...(event.target.value.trim()
                        ? { clear_api_key: false }
                        : {}),
                    })
                  }
                />
                <span className="field-hint">{t.providerApiKeyHint}</span>
              </div>
            </div>

            {baseUrlChanged && (
              <div className="stack" style={{ marginTop: 12 }}>
                <Alert
                  type="warning"
                  showIcon
                  title={t.providerBaseUrlChangedNotice}
                />
                <Checkbox
                  checked={providerForm.clear_api_key}
                  disabled={Boolean(providerForm.api_key.trim())}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      clear_api_key: event.target.checked,
                    })
                  }
                >
                  {t.providerClearApiKeyOnUrlChange}
                </Checkbox>
              </div>
            )}

            <div className="save-bar">
              <Button htmlType="submit" type="primary">
                {editingProviderId ? t.providerSave : t.providerAdd}
              </Button>
              {editingProviderId && (
                <Button onClick={onResetProviderForm}>{t.cancel}</Button>
              )}
            </div>
          </form>

          {editingProviderId && (
            <div className="stack" style={{ marginTop: 12 }}>
              <div className="card-title-row">
                <div>
                  <h3 className="card-title">{t.providerCatalogTitle}</h3>
                  {resolvedEditingProvider?.catalog_fetched_at && (
                    <span className="field-hint">
                      {t.providerCatalogLastFetched.replace(
                        "{time}",
                        resolvedEditingProvider.catalog_fetched_at,
                      )}
                    </span>
                  )}
                </div>
                <Button
                  disabled={Boolean(refreshingId)}
                  onClick={() => onRefreshModels?.(editingProviderId)}
                >
                  {t.providerRefreshModels}
                </Button>
              </div>

              {refreshingId === editingProviderId && (
                <div className="field-hint" role="status">
                  {t.providerRefreshingModels}
                </div>
              )}

              {catalogNotice && (
                <Alert type={catalogNotice.type} title={catalogNotice.text} />
              )}

              <Alert type="info" title={t.providerProbeUsageNotice} />
              {probeError && <Alert type="error" title={probeError} />}

              {catalogModels.length === 0 ? (
                <div className="empty-state">{t.providerCatalogEmptyHint}</div>
              ) : (
                <div className="provider-capability-list">
                  {catalogModels.map((model) => {
                    const capability =
                      providerForm.model_capabilities[model] ??
                      EMPTY_CAPABILITY;
                    const probing = probeLoadingModel === model;
                    const probeResult = probeResults[model];
                    return (
                      <div className="provider-capability-card" key={model}>
                        <div className="stack" style={{ gap: 6 }}>
                          <code className="provider-capability-model model-id-wrap">
                            {model}
                          </code>
                          <div className="row">
                            <Tag>{protocolBadge(model, t)}</Tag>
                            <Button
                              size="small"
                              type="text"
                              className="model-copy-btn"
                              icon={<CopyOutlined />}
                              aria-label={`${t.agentProfileCopyId} ${model}`}
                              onClick={() =>
                                void copyTextValue(`model:${model}`, model)
                              }
                            >
                              {copiedId === `model:${model}`
                                ? t.agentProfileCopied
                                : t.agentProfileCopyId}
                            </Button>
                          </div>
                        </div>
                        <div className="provider-capability-fields">
                          <Input
                            aria-label={`${model} ${t.providerModelDescription}`}
                            placeholder={t.providerModelDescription}
                            maxLength={200}
                            value={capability.description}
                            onChange={(event) =>
                              updateCapability(model, {
                                description: event.target.value,
                              })
                            }
                          />
                          <Checkbox
                            aria-label={`${model} ${t.providerModelVision}`}
                            checked={capability.vision}
                            onChange={(event) =>
                              updateCapability(model, {
                                vision: event.target.checked,
                              })
                            }
                          >
                            {t.providerModelVision}
                          </Checkbox>
                          <div className="provider-effort-group">
                            <span className="subtle">
                              {t.providerModelEffortLevels}
                            </span>
                            {effortOptions.map((option) => (
                              <Checkbox
                                key={option.value}
                                aria-label={`${model} ${option.label}`}
                                checked={capability.effort_levels.includes(
                                  option.value,
                                )}
                                onChange={(event) =>
                                  updateCapability(model, {
                                    effort_levels: event.target.checked
                                      ? [
                                          ...capability.effort_levels,
                                          option.value,
                                        ]
                                      : capability.effort_levels.filter(
                                          (level) => level !== option.value,
                                        ),
                                  })
                                }
                              >
                                {option.label}
                              </Checkbox>
                            ))}
                          </div>
                          <div className="provider-probe-row">
                            <Button
                              size="small"
                              disabled={Boolean(probeLoadingModel) && !probing}
                              aria-label={`${model} ${t.providerProbeButton}`}
                              onClick={() =>
                                onProbeModel(editingProviderId, model)
                              }
                            >
                              {probing
                                ? t.providerProbeRunning
                                : t.providerProbeButton}
                            </Button>
                            {probing && (
                              <span className="field-hint" role="status">
                                {t.providerProbeRunning}
                              </span>
                            )}
                          </div>
                          {probeResult && (
                            <div
                              className="provider-probe-result"
                              aria-label={`${model} ${t.providerProbeResult}`}
                              aria-live="polite"
                            >
                              <div className="provider-probe-row">
                                <span
                                  aria-label={`${model} ${t.providerProbeVision} ${t.providerProbeResult}`}
                                >
                                  {t.providerProbeVision}:{" "}
                                  {outcomeLabel(probeResult.vision)}
                                </span>
                                <Button
                                  size="small"
                                  aria-label={`${model} ${t.providerProbeApply}`}
                                  onClick={() => onApplyProbe(model)}
                                >
                                  {t.providerProbeApply}
                                </Button>
                              </div>
                              <div className="provider-probe-effort">
                                {effortOptions.map((option) => (
                                  <Tag
                                    key={option.value}
                                    aria-label={`${model} ${option.label} ${t.providerProbeResult}`}
                                  >
                                    {option.label}{" "}
                                    {outcomeMark(
                                      probeResult.effort[option.value],
                                    )}
                                  </Tag>
                                ))}
                              </div>
                              <span className="field-hint">
                                {t.providerProbeEffortHint}
                              </span>
                              <span className="field-hint">
                                {probeResult.notes}
                              </span>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
