import { useEffect, useState } from "react";
import { Alert, Button, Input, Typography } from "antd";
import type { Language, Messages } from "./i18n";
import { LanguageSwitcher } from "./LanguageSwitcher";

export function PersonalSettingsPage({
  token,
  messages: t,
  language,
  onChangeLanguage,
  onChanged,
}: {
  token: string;
  messages: Messages;
  language: Language;
  onChangeLanguage: () => void;
  onChanged?: (value: boolean) => void;
}) {
  const [value, setValue] = useState("");
  const [configured, setConfigured] = useState(false);
  const [error, setError] = useState("");
  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  useEffect(() => {
    void fetch("/api/v1/me/dtb-token", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error();
        const configured = ((await r.json()) as { configured: boolean })
          .configured;
        setConfigured(configured);
        onChanged?.(configured);
      })
      .catch(() => setError(t.error));
  }, [token, t.error, onChanged]);
  async function save(next: string) {
    try {
      const response = await fetch("/api/v1/me/dtb-token", {
        method: "PUT",
        headers,
        body: JSON.stringify({ token: next }),
      });
      if (!response.ok) throw new Error();
      const configured = ((await response.json()) as { configured: boolean })
        .configured;
      setConfigured(configured);
      onChanged?.(configured);
      setValue("");
      setError("");
    } catch {
      setError(t.error);
    }
  }
  return (
    <div className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.personalSettings}</h1>
          <p className="page-subtitle">{t.personalSettingsHint}</p>
        </div>
      </div>
      {error && <Alert type="error" title={error} />}
      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.language}</h2>
        </div>
        <div className="row">
          <Typography.Text type="secondary">{t.languageHint}</Typography.Text>
          <LanguageSwitcher language={language} onChange={onChangeLanguage} />
        </div>
      </section>
      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">DTB token</h2>
          <span className="muted">
            {configured ? t.providerConfigured : t.providerNotConfigured}
          </span>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="dtb-token">
            {t.dtbToken}
          </label>
          <Input.Password
            id="dtb-token"
            aria-label="DTB token"
            value={value}
            placeholder={configured ? t.dtbTokenSaved : t.dtbTokenPlaceholder}
            onChange={(event) => setValue(event.target.value)}
          />
          <p className="field-hint">{t.dtbTokenHint}</p>
        </div>
        <div className="save-bar">
          <Button
            type="primary"
            disabled={!value}
            onClick={() => void save(value)}
          >
            {t.saveSettings}
          </Button>
          <Button disabled={!configured} onClick={() => void save("")}>
            {t.deleteToken}
          </Button>
          <span className="save-reason">
            {!value ? t.notModified : t.unsavedChanges}
          </span>
        </div>
      </section>
    </div>
  );
}
