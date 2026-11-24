import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Select, Tag, Typography } from "antd";
import type { Language, Messages } from "./i18n";
import { LanguageSwitcher } from "./LanguageSwitcher";
import type { AvailableSkill, Project } from "./chat/types";
import { readErrorDetail } from "./httpErrors";

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
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [skillList, setSkillList] = useState<AvailableSkill[]>([]);
  const [secretStatus, setSecretStatus] = useState<
    Record<string, Record<string, boolean>>
  >({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [skillError, setSkillError] = useState("");

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
        const isConfigured = ((await r.json()) as { configured: boolean })
          .configured;
        setConfigured(isConfigured);
        onChanged?.(isConfigured);
      })
      .catch(() => setError(t.error));
  }, [token, t.error, onChanged]);

  useEffect(() => {
    void fetch("/api/v1/projects", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (r) => (r.ok ? ((await r.json()) as Project[]) : []))
      .then((rows) => {
        const active = Array.isArray(rows)
          ? rows.filter((p) => p.status === "active")
          : [];
        setProjects(active);
        const preferred =
          active.find((p) => p.id === "default") ?? active[0] ?? null;
        if (preferred) {
          setSelectedProjectId((current) =>
            active.some((p) => p.id === current) ? current : preferred.id,
          );
        }
      })
      .catch(() => setProjects([]));
  }, [token]);

  const loadProjectSecrets = useCallback(
    async (projectId: string) => {
      if (!projectId) {
        setSkillList([]);
        setSecretStatus({});
        return;
      }
      try {
        const auth = { Authorization: `Bearer ${token}` };
        const res = await fetch(
          `/api/v1/skills/available?project=${encodeURIComponent(projectId)}`,
          { headers: auth },
        );
        if (!res.ok) {
          setSkillList([]);
          return;
        }
        const available = ((await res.json()) as AvailableSkill[]).filter(
          (item) => item.secrets.length > 0,
        );
        setSkillList(available);
        const entries = await Promise.all(
          available.map(async (item) => {
            const r = await fetch(
              `/api/v1/skills/${encodeURIComponent(item.name)}/secrets?project=${encodeURIComponent(projectId)}`,
              { headers: auth },
            );
            if (!r.ok) {
              const fallback: Record<string, boolean> = {};
              for (const s of item.secrets) {
                fallback[s] = (item.configured_secrets ?? []).includes(s);
              }
              return [item.name, fallback] as const;
            }
            return [
              item.name,
              (await r.json()) as Record<string, boolean>,
            ] as const;
          }),
        );
        setSecretStatus(Object.fromEntries(entries));
        setSkillError("");
      } catch (caught) {
        setSkillError(caught instanceof Error ? caught.message : t.error);
      }
    },
    [token, t.error],
  );

  useEffect(() => {
    void loadProjectSecrets(selectedProjectId);
  }, [selectedProjectId, loadProjectSecrets]);

  async function save(next: string) {
    try {
      const response = await fetch("/api/v1/me/dtb-token", {
        method: "PUT",
        headers,
        body: JSON.stringify({ token: next }),
      });
      if (!response.ok) throw new Error();
      const isConfigured = ((await response.json()) as { configured: boolean })
        .configured;
      setConfigured(isConfigured);
      onChanged?.(isConfigured);
      setValue("");
      setError("");
    } catch {
      setError(t.error);
    }
  }

  async function saveSkillSecret(skillName: string, secretName: string) {
    const key = `${skillName}:${secretName}`;
    const secretVal = drafts[key] ?? "";
    if (!secretVal || !selectedProjectId) return;
    setBusyKey(`${key}:save`);
    setSkillError("");
    try {
      const response = await fetch(
        `/api/v1/skills/${encodeURIComponent(skillName)}/secrets/${encodeURIComponent(secretName)}?project=${encodeURIComponent(selectedProjectId)}`,
        {
          method: "PUT",
          headers,
          body: JSON.stringify({ value: secretVal }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      setDrafts((prev) => ({ ...prev, [key]: "" }));
      await loadProjectSecrets(selectedProjectId);
    } catch (caught) {
      setSkillError(caught instanceof Error ? caught.message : t.error);
    } finally {
      setBusyKey(null);
    }
  }

  async function deleteSkillSecret(skillName: string, secretName: string) {
    if (!selectedProjectId) return;
    const key = `${skillName}:${secretName}`;
    setBusyKey(`${key}:delete`);
    setSkillError("");
    try {
      const response = await fetch(
        `/api/v1/skills/${encodeURIComponent(skillName)}/secrets/${encodeURIComponent(secretName)}?project=${encodeURIComponent(selectedProjectId)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      setDrafts((prev) => ({ ...prev, [key]: "" }));
      await loadProjectSecrets(selectedProjectId);
    } catch (caught) {
      setSkillError(caught instanceof Error ? caught.message : t.error);
    } finally {
      setBusyKey(null);
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

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.skillsSecretsSectionTitle}</h2>
          {projects.length > 0 && (
            <Select
              aria-label={t.skillsSecretProjectLabel}
              value={selectedProjectId || undefined}
              onChange={(pid) => setSelectedProjectId(pid)}
              options={projects.map((p) => ({ value: p.id, label: p.name }))}
              style={{ minWidth: 180 }}
            />
          )}
        </div>
        <p className="field-hint">{t.skillsSecretsSectionHint}</p>
        <Alert type="warning" title={t.skillsTrustBanner} />
        {skillError && <Alert type="error" title={skillError} />}
        {skillList.length === 0 ? (
          <p className="muted">{t.skillsSecretsEmpty}</p>
        ) : (
          <div className="stack">
            {skillList.map((skill) => {
              const statusMap = secretStatus[skill.name] ?? {};
              return (
                <article className="skill-card" key={skill.name}>
                  <div className="card-title-row">
                    <strong className="mono">{skill.name}</strong>
                    <span className="muted">{skill.description}</span>
                  </div>
                  <div className="stack">
                    {skill.secrets.map((secretName) => {
                      const key = `${skill.name}:${secretName}`;
                      const isSet = Boolean(statusMap[secretName]);
                      const draft = drafts[key] ?? "";
                      return (
                        <div className="skill-secret-row" key={secretName}>
                          <div className="skill-secret-meta">
                            <span className="mono">{secretName}</span>
                            <Tag color={isSet ? "green" : "orange"}>
                              {isSet
                                ? t.skillsSecretConfigured
                                : t.skillsSecretMissing}
                            </Tag>
                          </div>
                          <Input.Password
                            aria-label={`${skill.name} ${secretName}`}
                            value={draft}
                            placeholder={
                              isSet
                                ? t.skillsSecretSavedPlaceholder
                                : t.skillsSecretPlaceholder
                            }
                            onChange={(event) =>
                              setDrafts((prev) => ({
                                ...prev,
                                [key]: event.target.value,
                              }))
                            }
                          />
                          <div className="save-bar">
                            <Button
                              type="primary"
                              disabled={!draft}
                              loading={busyKey === `${key}:save`}
                              onClick={() =>
                                void saveSkillSecret(skill.name, secretName)
                              }
                            >
                              {t.skillsSecretSave}
                            </Button>
                            <Button
                              danger
                              disabled={!isSet}
                              loading={busyKey === `${key}:delete`}
                              onClick={() =>
                                void deleteSkillSecret(skill.name, secretName)
                              }
                            >
                              {t.skillsSecretDelete}
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
