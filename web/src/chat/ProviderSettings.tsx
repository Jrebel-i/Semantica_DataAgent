import type { FormEvent } from "react";
import { Alert, Button, Checkbox, Input, Tag } from "antd";
import type { Messages } from "../i18n";
import { PROVIDER_TYPES } from "./types";
import type { EffortLevel, Provider, ProviderProbeResult } from "./types";

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
  models: string;
  model_capabilities: Record<string, ProviderModelCapabilityForm>;
};

const EMPTY_CAPABILITY: ProviderModelCapabilityForm = {
  description: "",
  vision: false,
  effort_levels: [],
};

export function ProviderSettings({
  messages,
  role,
  providers,
  providerError,
  providerNotice,
  providerForm,
  setProviderForm,
  editingProviderId,
  probeResults,
  probeLoadingModel,
  probeError,
  onEditProvider,
  onDeleteProvider,
  onSaveProvider,
  onResetProviderForm,
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
  probeResults: Record<string, ProviderProbeResult>;
  probeLoadingModel: string;
  probeError: string;
  onEditProvider: (provider: Provider) => void;
  onDeleteProvider: (provider: Provider) => void;
  onSaveProvider: (event: FormEvent) => void;
  onResetProviderForm: () => void;
  onProbeModel: (providerId: string, model: string) => void;
  onApplyProbe: (model: string) => void;
}) {
  const t = messages;
  const models = providerForm.models
    .split(",")
    .map((model) => model.trim())
    .filter(Boolean)
    .filter((model, index, all) => all.indexOf(model) === index);
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
    <div className="stack">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.providerSettings}</h1>
          <p className="page-subtitle">{t.providerApiKeyHint}</p>
        </div>
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
            {providers.map((provider) => (
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
                <div className="subtle mono">{provider.models.join(" · ")}</div>
              </div>
            ))}
          </div>
        )}
      </section>
      {role === "admin" && (
        <section className="card section-card">
          <form onSubmit={(event) => void onSaveProvider(event)}>
            <div className="card-title-row">
              <h2 className="card-title">
                {editingProviderId ? t.providerEdit : t.providerAdd}
              </h2>
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
                    })
                  }
                />
                <span className="field-hint">{t.providerApiKeyHint}</span>
              </div>
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label className="field-label" htmlFor="provider-models">
                  {t.providerModels}
                </label>
                <Input
                  id="provider-models"
                  aria-label={t.providerModels}
                  value={providerForm.models}
                  onChange={(event) =>
                    setProviderForm({
                      ...providerForm,
                      models: event.target.value,
                    })
                  }
                  required
                />
              </div>
              {models.length > 0 && (
                <div className="field" style={{ gridColumn: "1 / -1" }}>
                  <span className="field-label">
                    {t.providerModelCapabilities}
                  </span>
                  <span className="field-hint">
                    {t.providerModelCapabilitiesHint}
                  </span>
                  {probeError && <Alert type="error" title={probeError} />}
                  <div className="provider-capability-list">
                    {models.map((model) => {
                      const capability =
                        providerForm.model_capabilities[model] ??
                        EMPTY_CAPABILITY;
                      const probing = probeLoadingModel === model;
                      const probeResult = probeResults[model];
                      return (
                        <div className="provider-capability-card" key={model}>
                          <code className="provider-capability-model">
                            {model}
                          </code>
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
                                loading={probing}
                                disabled={
                                  !editingProviderId ||
                                  (Boolean(probeLoadingModel) && !probing)
                                }
                                aria-label={`${model} ${t.providerProbe}`}
                                onClick={() =>
                                  onProbeModel(editingProviderId, model)
                                }
                              >
                                {probing
                                  ? t.providerProbeRunning
                                  : t.providerProbe}
                              </Button>
                              {probing && (
                                <span className="field-hint" role="status">
                                  {t.providerProbeRunning}
                                </span>
                              )}
                              {!editingProviderId && (
                                <span className="field-hint">
                                  {t.providerProbeNeedsSave}
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
                                  {probeResult.notes}
                                </span>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
            <div className="save-bar">
              <Button htmlType="submit" type="primary">
                {editingProviderId ? t.providerSave : t.providerAdd}
              </Button>
              {editingProviderId && (
                <Button onClick={onResetProviderForm}>{t.cancel}</Button>
              )}
            </div>
          </form>
        </section>
      )}
    </div>
  );
}
