import { useEffect, useState } from "react";
import { Alert, Button, Input } from "antd";
import type { Messages } from "./i18n";

const fields = [
  "neo4j_uri",
  "neo4j_user",
  "neo4j_password",
  "milvus_uri",
  "embedding_url",
  "embedding_model",
  "source_directory",
  "schedule_interval_seconds",
  "generation_retention",
] as const;

export function PlatformSettings({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const labels: Record<string, string> = {
    neo4j_uri: t.neo4jAddress,
    neo4j_user: t.neo4jUser,
    neo4j_password: t.neo4jPassword,
    milvus_uri: t.milvusAddress,
    embedding_url: t.embeddingAddress,
    embedding_model: t.embeddingModel,
    source_directory: t.sourceDirectory,
    schedule_interval_seconds: t.scheduleInterval,
    generation_retention: t.generationRetention,
  };
  const hints: Record<string, string> = {
    neo4j_uri: t.neo4jAddressHint,
    milvus_uri: t.milvusAddressHint,
    embedding_url: t.embeddingAddressHint,
    source_directory: t.sourceDirectoryHint,
    schedule_interval_seconds: t.scheduleIntervalHint,
    generation_retention: t.generationRetentionHint,
  };
  const [values, setValues] = useState<Record<string, string>>({});
  const [changed, setChanged] = useState<Record<string, string>>({});
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    void fetch("/api/v1/settings", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Record<string, string | null>>;
      })
      .then((data) => {
        setValues(
          Object.fromEntries(
            fields.map((field) => [
              field,
              field === "neo4j_password" ? "" : (data[field] ?? ""),
            ]),
          ),
        );
        setLoaded(true);
      })
      .catch(() => setStatus(t.error));
  }, [token, t.error]);
  async function save() {
    try {
      const response = await fetch("/api/v1/settings", {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ values: changed }),
      });
      if (!response.ok) throw new Error();
      setChanged({});
      setValues((current) => ({ ...current, neo4j_password: "" }));
      setStatus(t.settingsSaved);
    } catch {
      setStatus(t.error);
    }
  }
  return (
    <section className="config-section">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.platformSettings}</h1>
          <p className="page-subtitle">{t.platformSettingsHint}</p>
        </div>
      </div>
      {status && (
        <Alert
          type={status === t.settingsSaved ? "success" : "error"}
          title={status}
        />
      )}
      <div className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.platformSettings}</h2>
          <span className="muted">{t.platformSettingsSection}</span>
        </div>
        <div className="grid-2">
          {fields.map((field) => (
            <div className="field" key={field}>
              <label className="field-label" htmlFor={`platform-${field}`}>
                {labels[field] ?? field}
              </label>
              <Input
                id={`platform-${field}`}
                type={field === "neo4j_password" ? "password" : "text"}
                aria-label={labels[field] ?? field}
                value={values[field] ?? ""}
                placeholder={
                  field === "neo4j_password" && values[field] === ""
                    ? t.passwordSaved
                    : undefined
                }
                onChange={(event) => {
                  const value = event.target.value;
                  setValues({ ...values, [field]: value });
                  setChanged({ ...changed, [field]: value });
                }}
              />
              {hints[field] && <p className="field-hint">{hints[field]}</p>}
            </div>
          ))}
        </div>
        <div className="save-bar">
          <Button
            type="primary"
            disabled={!loaded || !Object.keys(changed).length}
            onClick={() => void save()}
          >
            {t.saveSettings}
          </Button>
          <span className="save-reason">
            {!loaded
              ? t.error
              : Object.keys(changed).length
                ? t.unsavedChanges
                : t.notModified}
          </span>
        </div>
      </div>
    </section>
  );
}
