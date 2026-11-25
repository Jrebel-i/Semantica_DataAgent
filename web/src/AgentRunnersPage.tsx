import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Input, Table, Tag } from "antd";
import type { Messages } from "./i18n";
import type { AgentRunnerCreate, AgentRunnerView } from "./chat/types";
import { readErrorDetail } from "./httpErrors";

const STATUS_COLORS: Record<AgentRunnerView["status"], string> = {
  available: "green",
  pending_build: "gold",
  building: "processing",
  image_deployed: "blue",
  adapter_missing: "orange",
  failed: "red",
};

export function AgentRunnersPage({
  token,
  messages: t,
  onNavigateAgentConfig,
}: {
  token: string;
  messages: Messages;
  onNavigateAgentConfig: () => void;
}) {
  const [runners, setRunners] = useState<AgentRunnerView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const [id, setId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [npmPackage, setNpmPackage] = useState("");
  const [packageVersion, setPackageVersion] = useState("");
  const [executable, setExecutable] = useState("");
  const [adapterId, setAdapterId] = useState("");

  const loadRunners = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const response = await fetch("/api/v1/agent-runners", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const rows = (await response.json()) as AgentRunnerView[];
      setRunners(Array.isArray(rows) ? rows : []);
    } catch {
      setLoadError(t.error);
    } finally {
      setLoading(false);
    }
  }, [token, t.error]);

  useEffect(() => {
    void loadRunners();
  }, [loadRunners]);

  function statusLabel(status: AgentRunnerView["status"]): string {
    switch (status) {
      case "available":
        return t.runnerStatusAvailable;
      case "pending_build":
        return t.runnerStatusPendingBuild;
      case "building":
        return t.runnerStatusBuilding;
      case "image_deployed":
        return t.runnerStatusImageDeployed;
      case "adapter_missing":
        return t.runnerStatusAdapterMissing;
      case "failed":
        return t.runnerStatusFailed;
    }
  }

  async function handleRegister(event: FormEvent) {
    event.preventDefault();
    setNotice("");
    setSubmitError("");
    setSubmitting(true);
    try {
      const payload: AgentRunnerCreate = {
        id: id.trim(),
        display_name: displayName.trim(),
        npm_package: npmPackage.trim(),
        package_version: packageVersion.trim(),
        executable: executable.trim(),
        adapter_id: adapterId.trim(),
      };
      const response = await fetch("/api/v1/agent-runners", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response, t.runnerRegisterError);
        setSubmitError(detail);
        return;
      }
      setId("");
      setDisplayName("");
      setNpmPackage("");
      setPackageVersion("");
      setExecutable("");
      setAdapterId("");
      setNotice(t.runnerRegisteredNotice);
      await loadRunners();
    } catch {
      setSubmitError(t.runnerRegisterError);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.agentRunnersNav}</h1>
          <p className="page-subtitle">{t.agentRunnersHint}</p>
        </div>
        <div className="page-actions">
          <Button type="primary" onClick={onNavigateAgentConfig}>
            {t.goToAgentConfig}
          </Button>
        </div>
      </div>

      {loadError && <Alert type="error" title={loadError} />}

      <div className="card section-card token-table-card">
        <Table
          rowKey="id"
          loading={loading}
          pagination={false}
          dataSource={runners}
          columns={[
            {
              title: t.runnerColumn,
              dataIndex: "display_name",
              key: "display_name",
              render: (value: string, row: AgentRunnerView) => (
                <div className="row">
                  <strong>{value}</strong>
                  <code className="muted">{row.id}</code>
                  <Tag color={row.builtin ? "blue" : "default"}>
                    {row.builtin ? t.runnerBuiltInTag : t.runnerCustomTag}
                  </Tag>
                </div>
              ),
            },
            {
              title: t.runnerPackageColumn,
              key: "package",
              render: (_: unknown, row: AgentRunnerView) => (
                <code className="model-id-wrap">
                  {row.npm_package}@{row.package_version}
                </code>
              ),
            },
            {
              title: t.runnerExecutableColumn,
              dataIndex: "executable",
              key: "executable",
              render: (value: string) => <code>{value}</code>,
            },
            {
              title: t.runnerAdapterColumn,
              dataIndex: "adapter_id",
              key: "adapter_id",
              render: (value: string) => <code>{value}</code>,
            },
            {
              title: t.runnerCliStatusColumn,
              dataIndex: "cli_deployed",
              key: "cli_deployed",
              render: (deployed: boolean) => (
                <Tag color={deployed ? "green" : "default"}>
                  {deployed ? t.runnerCliDeployed : t.runnerCliMissing}
                </Tag>
              ),
            },
            {
              title: t.runnerAdapterStatusColumn,
              dataIndex: "adapter_registered",
              key: "adapter_registered",
              render: (registered: boolean) => (
                <Tag color={registered ? "green" : "orange"}>
                  {registered ? t.runnerAdapterReady : t.runnerAdapterMissing}
                </Tag>
              ),
            },
            {
              title: t.runnerStatusColumn,
              dataIndex: "status",
              key: "status",
              render: (status: AgentRunnerView["status"]) => (
                <Tag color={STATUS_COLORS[status] ?? "default"}>
                  {statusLabel(status)}
                </Tag>
              ),
            },
          ]}
        />
      </div>

      <section className="card section-card stack">
        <div className="card-title-row">
          <h2 className="card-title">{t.runnerRegisterTitle}</h2>
        </div>
        <Alert type="info" title={t.runnerRegisterHint} />
        {notice && <Alert type="success" title={notice} />}
        {submitError && <Alert type="error" title={submitError} />}
        <form
          className="stack"
          onSubmit={(event) => void handleRegister(event)}
        >
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor="runner-id">
                {t.runnerIdLabel}
              </label>
              <Input
                id="runner-id"
                aria-label={t.runnerIdLabel}
                placeholder={t.runnerIdPlaceholder}
                value={id}
                onChange={(event) => setId(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="runner-display-name">
                {t.runnerDisplayNameLabel}
              </label>
              <Input
                id="runner-display-name"
                aria-label={t.runnerDisplayNameLabel}
                placeholder={t.runnerDisplayNamePlaceholder}
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="runner-npm-package">
                {t.runnerNpmPackageLabel}
              </label>
              <Input
                id="runner-npm-package"
                aria-label={t.runnerNpmPackageLabel}
                placeholder={t.runnerNpmPackagePlaceholder}
                value={npmPackage}
                onChange={(event) => setNpmPackage(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="runner-package-version">
                {t.runnerPackageVersionLabel}
              </label>
              <Input
                id="runner-package-version"
                aria-label={t.runnerPackageVersionLabel}
                placeholder={t.runnerPackageVersionPlaceholder}
                value={packageVersion}
                onChange={(event) => setPackageVersion(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="runner-executable">
                {t.runnerExecutableLabel}
              </label>
              <Input
                id="runner-executable"
                aria-label={t.runnerExecutableLabel}
                placeholder={t.runnerExecutablePlaceholder}
                value={executable}
                onChange={(event) => setExecutable(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="runner-adapter-id">
                {t.runnerAdapterIdLabel}
              </label>
              <Input
                id="runner-adapter-id"
                aria-label={t.runnerAdapterIdLabel}
                placeholder={t.runnerAdapterIdPlaceholder}
                value={adapterId}
                onChange={(event) => setAdapterId(event.target.value)}
                required
              />
            </div>
          </div>
          <div className="save-bar">
            <Button htmlType="submit" type="primary" disabled={submitting}>
              {t.runnerRegisterSubmit}
            </Button>
          </div>
        </form>
      </section>
    </section>
  );
}
