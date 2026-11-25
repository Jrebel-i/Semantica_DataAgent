import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Tag } from "antd";
import type { Messages } from "./i18n";
import type { SkillAuditView, SkillMetadata, SkillView } from "./chat/types";
import { readErrorDetail } from "./httpErrors";

function formatUtc(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

export function SkillsPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [skills, setSkills] = useState<SkillView[]>([]);
  const [audit, setAudit] = useState<SkillAuditView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<{
    bytes: ArrayBuffer;
    filename: string;
    meta: SkillMetadata;
  } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [togglingName, setTogglingName] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const [skillsRes, auditRes] = await Promise.all([
        fetch("/api/v1/skills", { headers }),
        fetch("/api/v1/skills/audit?limit=100&offset=0", { headers }),
      ]);
      if (!skillsRes.ok || !auditRes.ok) {
        throw new Error(await readErrorDetail(skillsRes, t.error));
      }
      setSkills((await skillsRes.json()) as SkillView[]);
      setAudit((await auditRes.json()) as SkillAuditView[]);
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t.error);
    } finally {
      setLoading(false);
    }
  }, [token, t.error]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleFileSelect(file: File | undefined) {
    if (!file) return;
    setError("");
    setSubmitting(true);
    try {
      const bytes = await file.arrayBuffer();
      const response = await fetch("/api/v1/skills/preview", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/zip",
        },
        body: bytes,
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.skillsUploadError));
      }
      const meta = (await response.json()) as SkillMetadata;
      setPreview({ bytes, filename: file.name, meta });
    } catch (caught) {
      setPreview(null);
      setError(caught instanceof Error ? caught.message : t.skillsUploadError);
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmUpload() {
    if (!preview) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/v1/skills", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/zip",
        },
        body: preview.bytes,
      });
      if (response.status === 409) {
        throw new Error(t.skillsConflictError);
      }
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.skillsUploadError));
      }
      setPreview(null);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t.skillsUploadError);
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleEnabled(skill: SkillView) {
    setTogglingName(skill.name);
    setError("");
    try {
      const response = await fetch(
        `/api/v1/skills/${encodeURIComponent(skill.name)}/enabled`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ enabled: !skill.enabled }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.operationFailed));
      }
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t.operationFailed);
    } finally {
      setTogglingName(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setError("");
    try {
      const response = await fetch(
        `/api/v1/skills/${encodeURIComponent(deleteTarget)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.operationFailed));
      }
      setDeleteTarget(null);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t.operationFailed);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.skills}</h1>
          <p className="page-subtitle">{t.skillsHint}</p>
        </div>
      </div>

      <Alert type="warning" title={t.skillsTrustBanner} />
      {error && <Alert type="error" title={error} />}

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.skillsUploadTitle}</h2>
          <div>
            <input
              ref={fileRef}
              type="file"
              accept=".zip,application/zip"
              className="composer-file-input"
              aria-label={t.skillsSelectZip}
              onChange={(event) => {
                const file = event.target.files?.[0];
                void handleFileSelect(file);
                event.target.value = "";
              }}
            />
            <Button
              type="primary"
              loading={submitting}
              onClick={() => fileRef.current?.click()}
            >
              {t.skillsSelectZip}
            </Button>
          </div>
        </div>

        {preview && (
          <div className="skill-preview-card">
            <h3 className="card-title">{t.skillsPreviewTitle}</h3>
            {preview.meta.trust_warning && (
              <Alert type="warning" title={preview.meta.trust_warning} />
            )}
            <dl className="skill-meta-grid">
              <dt>Name</dt>
              <dd className="mono">{preview.meta.name}</dd>
              <dt>{t.skillsProjectTypesLabel}</dt>
              <dd>{preview.meta.project_types.join(", ")}</dd>
              <dt>{t.skillsProjectsLabel}</dt>
              <dd>
                {preview.meta.projects.length
                  ? preview.meta.projects.join(", ")
                  : t.skillsProjectAll}
              </dd>
              <dt>{t.skillsSecretsLabel}</dt>
              <dd className="mono">
                {preview.meta.secrets.length
                  ? preview.meta.secrets.join(", ")
                  : t.skillsNone}
              </dd>
              <dt>{t.skillsDependenciesLabel}</dt>
              <dd>
                {preview.meta.dependencies.length
                  ? preview.meta.dependencies.join(", ")
                  : t.skillsNone}
              </dd>
            </dl>
            <p className="muted">{preview.meta.description}</p>
            <div className="save-bar">
              <Button
                type="primary"
                loading={submitting}
                onClick={() => void confirmUpload()}
              >
                {t.skillsConfirmUpload}
              </Button>
              <Button disabled={submitting} onClick={() => setPreview(null)}>
                {t.skillsCancelPreview}
              </Button>
            </div>
          </div>
        )}
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.skills}</h2>
        </div>
        {loading ? (
          <p className="muted">{t.skillsLoading}</p>
        ) : (
          <div className="stack">
            {skills.map((skill) => (
              <article className="skill-card" key={skill.name}>
                <div className="card-title-row">
                  <div className="skill-card-head">
                    <strong className="mono">{skill.name}</strong>
                    <Tag color={skill.source === "builtin" ? "blue" : "gold"}>
                      {skill.source === "builtin"
                        ? t.skillsBuiltin
                        : t.skillsUpload}
                    </Tag>
                    {skill.protected && <Tag>{t.skillsProtected}</Tag>}
                    <Tag color={skill.enabled ? "green" : "default"}>
                      {skill.enabled ? t.skillsEnabled : t.skillsDisabled}
                    </Tag>
                  </div>
                  <div className="skill-card-actions">
                    <Button
                      size="small"
                      autoInsertSpace={false}
                      disabled={skill.protected || togglingName === skill.name}
                      loading={togglingName === skill.name}
                      onClick={() => void toggleEnabled(skill)}
                    >
                      {skill.enabled ? t.skillsDisable : t.skillsEnable}
                    </Button>
                    <Button
                      size="small"
                      autoInsertSpace={false}
                      danger
                      disabled={skill.protected}
                      onClick={() => setDeleteTarget(skill.name)}
                    >
                      {t.skillsDelete}
                    </Button>
                  </div>
                </div>
                <p>{skill.description}</p>
                <div className="skill-meta-line">
                  <span>
                    {t.skillsProjectTypesLabel}:{" "}
                    {skill.project_types.join(", ")}
                  </span>
                  <span>
                    {t.skillsProjectsLabel}:{" "}
                    {skill.projects.length
                      ? skill.projects.join(", ")
                      : t.skillsProjectAll}
                  </span>
                  <span>
                    {t.skillsSecretsLabel}:{" "}
                    {skill.secrets.length
                      ? skill.secrets.join(", ")
                      : t.skillsNone}
                  </span>
                  <span>
                    {t.skillsDependenciesLabel}:{" "}
                    {skill.dependencies.length
                      ? skill.dependencies.join(", ")
                      : t.skillsNone}
                  </span>
                </div>
              </article>
            ))}
            {skills.filter((item) => item.source === "upload").length === 0 && (
              <p className="muted">{t.skillsEmpty}</p>
            )}
          </div>
        )}
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.skillsAuditTitle}</h2>
        </div>
        {audit.length === 0 ? (
          <p className="muted">{t.skillsAuditEmpty}</p>
        ) : (
          <div className="table-scroll-region" tabIndex={0}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Skill</th>
                  <th>Action</th>
                  <th>Actor</th>
                  <th>UTC</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((item, idx) => (
                  <tr key={`${item.skill_name}-${item.created_at}-${idx}`}>
                    <td className="mono">{item.skill_name}</td>
                    <td>{item.action}</td>
                    <td className="mono">{item.actor_id}</td>
                    <td className="mono">{formatUtc(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Modal
        open={deleteTarget !== null}
        title={t.skillsDeleteTitle}
        okText={t.skillsDelete}
        cancelText={t.skillsCancelPreview}
        okButtonProps={{ danger: true, loading: deleting }}
        onOk={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      >
        <p>
          {deleteTarget
            ? t.skillsDeleteWarning.replace("{name}", deleteTarget)
            : ""}
        </p>
      </Modal>
    </section>
  );
}
