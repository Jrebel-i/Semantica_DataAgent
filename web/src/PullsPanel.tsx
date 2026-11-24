import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Space, Table, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { TableScroll } from "./TableScroll";

type Pull = components["schemas"]["PullView"];
type PullInstance = components["schemas"]["PullInstanceView"];

const STATUS_COLORS: Record<string, string> = {
  succeeded: "green",
  running: "blue",
  failed: "red",
  blocked: "orange",
};

function pullStatusLabel(status: string, t: Messages): string {
  switch (status) {
    case "succeeded":
      return t.pullStatusSucceeded;
    case "failed":
      return t.pullStatusFailed;
    case "running":
      return t.pullStatusRunning;
    case "blocked":
      return t.pullStatusBlocked;
    default:
      return status;
  }
}

function triggerTypeLabel(triggerType: string, t: Messages): string {
  switch (triggerType) {
    case "scheduled":
      return t.pullTriggerScheduled;
    case "forced":
      return t.pullTriggerForced;
    case "manual":
      return t.pullTriggerManual;
    default:
      return triggerType;
  }
}

function localTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function latestBlockedPulls(pulls: Pull[]): Pull[] {
  const seenInstances = new Set<string>();
  const blocked: Pull[] = [];
  for (const row of pulls) {
    const key = row.instance_id ?? `__legacy_${row.id}`;
    if (seenInstances.has(key)) continue;
    seenInstances.add(key);
    if (row.status === "blocked") {
      blocked.push(row);
    }
  }
  return blocked;
}

async function errorMessage(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    // Ignore non-JSON error bodies
  }
  return fallback;
}

export function PullsPanel({
  token,
  project,
  messages: t,
}: {
  token: string;
  project?: string;
  messages: Messages;
}) {
  const effectiveProject = project || "default";
  const [instances, setInstances] = useState<PullInstance[]>([]);
  const [pulls, setPulls] = useState<Pull[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [runningInstanceId, setRunningInstanceId] = useState<string | null>(
    null,
  );
  const [forceTarget, setForceTarget] = useState<Pull | null>(null);
  const generation = useRef(0);

  const instancesUrl = `/api/v1/projects/${encodeURIComponent(effectiveProject)}/pull-instances`;
  const pullsUrl = `/api/v1/pulls?project=${encodeURIComponent(effectiveProject)}`;

  const loadAll = useCallback(
    async (signal?: AbortSignal, expectedGen = generation.current) => {
      const headers = { Authorization: `Bearer ${token}` };
      const [instRes, pullsRes] = await Promise.all([
        fetch(instancesUrl, { headers, signal }),
        fetch(pullsUrl, { headers, signal }),
      ]);
      if (!instRes.ok || !pullsRes.ok) throw new Error();
      const [nextInstances, nextPulls] = (await Promise.all([
        instRes.json(),
        pullsRes.json(),
      ])) as [PullInstance[], Pull[]];
      if (signal?.aborted || generation.current !== expectedGen) return;
      setInstances(Array.isArray(nextInstances) ? nextInstances : []);
      setPulls(Array.isArray(nextPulls) ? nextPulls : []);
      setLoaded(true);
    },
    [instancesUrl, pullsUrl, token],
  );

  useEffect(() => {
    const controller = new AbortController();
    const expectedGen = generation.current + 1;
    generation.current = expectedGen;
    setInstances([]);
    setPulls([]);
    setLoaded(false);
    setError("");
    setRunningInstanceId(null);
    setForceTarget(null);

    void loadAll(controller.signal, expectedGen).catch(() => {
      if (!controller.signal.aborted && generation.current === expectedGen) {
        setError(t.error);
      }
    });
    return () => controller.abort();
  }, [effectiveProject, loadAll, t.error]);

  async function triggerPull(instanceId: string, force: boolean) {
    const expectedGen = generation.current;
    setRunningInstanceId(instanceId);
    try {
      const targetUrl = `/api/v1/projects/${encodeURIComponent(effectiveProject)}/pull-instances/${encodeURIComponent(instanceId)}/pull`;
      const response = await fetch(targetUrl, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ force }),
      });
      if (!response.ok) {
        throw new Error(await errorMessage(response, t.error));
      }
      if (generation.current !== expectedGen) return;
      setForceTarget(null);
      await loadAll(undefined, expectedGen);
      if (generation.current === expectedGen) setError("");
    } catch (failure) {
      if (generation.current === expectedGen) {
        setError(failure instanceof Error ? failure.message : t.error);
      }
    } finally {
      if (generation.current === expectedGen) setRunningInstanceId(null);
    }
  }

  if (!loaded || (instances.length === 0 && pulls.length === 0)) {
    return error ? <Alert type="error" title={error} /> : null;
  }

  const blockedAlerts = latestBlockedPulls(pulls);

  return (
    <section className="stack">
      <div className="page-header">
        <h2 className="page-title">{t.pullRecords}</h2>
      </div>
      {error && <Alert type="error" title={error} />}
      {blockedAlerts.map((row) => (
        <Alert
          key={`blocked-${row.id}`}
          type="warning"
          showIcon
          title={
            row.instance_id
              ? `${t.pullCircuitBreakerTitle} (${row.instance_id})`
              : t.pullCircuitBreakerTitle
          }
          description={row.error || row.warnings?.join("；") || undefined}
          action={
            row.instance_id ? (
              <Button
                danger
                type="primary"
                disabled={runningInstanceId !== null}
                onClick={() => setForceTarget(row)}
              >
                {t.pullForceButton}
              </Button>
            ) : undefined
          }
        />
      ))}
      {instances.length > 0 && (
        <div className="card section-card">
          <div className="stack">
            {instances.map((inst) => (
              <div
                key={inst.id}
                data-testid={`pull-instance-${inst.id}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  flexWrap: "wrap",
                }}
              >
                <Space wrap>
                  <strong>{inst.display_name}</strong>
                  <Tag>{inst.id}</Tag>
                  <Tag color="blue">{inst.plugin_id}</Tag>
                  <Tag color={inst.enabled ? "green" : "default"}>
                    {inst.enabled ? t.enabled : t.disabled}
                  </Tag>
                  <span className="muted">
                    {inst.schedule_cron
                      ? `${inst.schedule_cron} (${inst.timezone})`
                      : t.pullManualOnly}
                  </span>
                  {inst.last_table_count !== null && (
                    <span className="muted">
                      {`${t.pullLastTableCount}: ${inst.last_table_count}`}
                    </span>
                  )}
                </Space>
                <Button
                  disabled={runningInstanceId !== null}
                  loading={runningInstanceId === inst.id}
                  onClick={() => void triggerPull(inst.id, false)}
                >
                  {t.pullNow}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="card token-table-card">
        <TableScroll label={t.pullRecords}>
          <Table
            rowKey="id"
            dataSource={pulls}
            pagination={false}
            locale={{ emptyText: t.pullHistoryEmpty }}
            columns={[
              {
                title: t.pullInstance,
                render: (_, row) => (
                  <Space wrap>
                    <span>{row.instance_id || "—"}</span>
                    {row.plugin_id && <Tag>{row.plugin_id}</Tag>}
                  </Space>
                ),
              },
              {
                title: t.pullTriggerType,
                render: (_, row) => (
                  <Tag>{triggerTypeLabel(row.trigger_type, t)}</Tag>
                ),
              },
              {
                title: t.buildStartedAt,
                render: (_, row) => localTime(row.started_at),
              },
              {
                title: t.buildStatus,
                render: (_, row) => (
                  <Space wrap>
                    <Tag color={STATUS_COLORS[row.status] ?? "default"}>
                      {pullStatusLabel(row.status, t)}
                    </Tag>
                    {row.skipped_reason === "no_changes" && (
                      <Tag>{t.pullSkippedNoChanges}</Tag>
                    )}
                  </Space>
                ),
              },
              { title: t.entityTable, dataIndex: "tables" },
              { title: t.entityTask, dataIndex: "tasks" },
              {
                title: t.pullWarnings,
                render: (_, row) =>
                  row.warnings && row.warnings.length > 0 ? (
                    <Alert
                      type="warning"
                      showIcon
                      title={row.warnings.join("；")}
                    />
                  ) : (
                    "—"
                  ),
              },
              {
                title: t.pullFailure,
                render: (_, row) => row.error || "—",
              },
            ]}
          />
        </TableScroll>
      </div>
      {forceTarget !== null && (
        <Modal
          open
          title={t.pullForceConfirmTitle}
          okText={t.pullForceButton}
          cancelText={t.cancel}
          okButtonProps={{
            danger: true,
            loading: runningInstanceId !== null,
          }}
          onOk={() => {
            if (forceTarget.instance_id) {
              void triggerPull(forceTarget.instance_id, true);
            }
          }}
          onCancel={() => setForceTarget(null)}
        >
          <div className="stack">
            <p>
              {t.pullForceConfirmDesc.replace(
                "{instance}",
                forceTarget.instance_id ?? "",
              )}
            </p>
            {forceTarget.error && (
              <Alert type="warning" showIcon title={forceTarget.error} />
            )}
          </div>
        </Modal>
      )}
    </section>
  );
}
