import type { FormEvent } from "react";
import { Alert, Button, Input, Tag } from "antd";
import type { Messages } from "../i18n";
import { PROVIDER_TYPES } from "./types";
import type { Provider } from "./types";

export type ProviderForm = {
  name: string;
  type: string;
  base_url: string;
  api_key: string;
  models: string;
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
  onEditProvider,
  onDeleteProvider,
  onSaveProvider,
  onResetProviderForm,
}: {
  messages: Messages;
  role: string;
  providers: Provider[];
  providerError: string;
  providerNotice: string;
  providerForm: ProviderForm;
  setProviderForm: (value: ProviderForm) => void;
  editingProviderId: string;
  onEditProvider: (provider: Provider) => void;
  onDeleteProvider: (provider: Provider) => void;
  onSaveProvider: (event: FormEvent) => void;
  onResetProviderForm: () => void;
}) {
  const t = messages;
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
