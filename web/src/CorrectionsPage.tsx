import { useCallback, useEffect, useState } from "react";
import { Alert, Tag } from "antd";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import { ProjectSelector, storedProject } from "./ProjectSelector";

type Proposal = {
  id: string;
  target: string;
  reason: string;
  diff: string;
  patch: string;
  status: string;
};
type Gap = { id: string; question: string; missing: string };
type StatusFilter = "all" | "pending" | "resolved";

function statusMeta(
  status: string,
  t: Messages,
): { label: string; color: string } {
  if (status === "pending") return { label: t.statusPending, color: "gold" };
  if (status === "approved") return { label: t.statusApproved, color: "green" };
  if (status === "rejected") return { label: t.statusRejected, color: "red" };
  return { label: status, color: "default" };
}

export function CorrectionsPage({
  token,
  gaps = false,
  messages: t,
}: {
  token: string;
  gaps?: boolean;
  messages: Messages;
}) {
  const [project, setProject] = useState(storedProject);
  const [rows, setRows] = useState<Proposal[]>([]);
  const [missing, setMissing] = useState<Gap[]>([]);
  const [selected, setSelected] = useState<Proposal | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [patch, setPatch] = useState("");
  const [error, setError] = useState("");
  const projectQuery = `project=${encodeURIComponent(project)}`;
  const pendingCount = rows.filter((row) => row.status === "pending").length;
  const resolvedCount = rows.length - pendingCount;
  const filteredRows = rows.filter((row) => {
    if (statusFilter === "pending") return row.status === "pending";
    if (statusFilter === "resolved") return row.status !== "pending";
    return true;
  });
  const selectedMeta = selected ? statusMeta(selected.status, t) : null;
  const refresh = useCallback(async () => {
    try {
      const response = await fetch(
        `${
          gaps ? "/api/v1/knowledge-gaps" : "/api/v1/corrections"
        }?${projectQuery}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      if (gaps) setMissing((await response.json()) as Gap[]);
      else setRows((await response.json()) as Proposal[]);
      setError("");
    } catch {
      setError(t.correctionLoadError);
    }
  }, [gaps, projectQuery, token, t.correctionLoadError]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    setSelected(null);
  }, [project]);
  async function review(action: "approve" | "reject", edited = false) {
    if (!selected) return;
    try {
      const response = await fetch(
        `/api/v1/corrections/${selected.id}/${action}?${projectQuery}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body:
            action === "approve" && edited
              ? JSON.stringify({ patch })
              : undefined,
        },
      );
      if (!response.ok) throw new Error();
      setSelected(null);
      await refresh();
    } catch {
      setError(t.correctionReviewError);
    }
  }
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {gaps ? t.gapTitle : t.correctionTitle}
          </h1>
          <p className="page-subtitle">{gaps ? t.gapHint : t.correctionHint}</p>
        </div>
        <button type="button" onClick={() => void refresh()}>
          {t.refresh}
        </button>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
          includeMemberProjects={gaps}
        />
      </div>
      {error && <Alert type="error" title={error} />}
      {gaps ? (
        missing.length === 0 ? (
          <div className="empty-state">{t.emptyContext}</div>
        ) : (
          missing.map((gap) => (
            <article className="gap-card" key={gap.id}>
              <h3>{gap.question}</h3>
              <p>{gap.missing}</p>
            </article>
          ))
        )
      ) : (
        <div className="correction-layout">
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.correctionTitle}</h2>
              <span className="muted">{filteredRows.length}</span>
            </div>
            {rows.length > 0 && (
              <div className="correction-filters" role="group">
                {(
                  [
                    ["all", `${t.filterAll} (${rows.length})`],
                    ["pending", `${t.filterPending} (${pendingCount})`],
                    ["resolved", `${t.filterResolved} (${resolvedCount})`],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    className={statusFilter === key ? "active" : ""}
                    aria-pressed={statusFilter === key}
                    onClick={() => setStatusFilter(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
            {filteredRows.length === 0 ? (
              <div className="empty-state">{t.emptyContext}</div>
            ) : (
              <ul className="correction-list">
                {filteredRows.map((row) => {
                  const meta = statusMeta(row.status, t);
                  return (
                    <li key={row.id}>
                      <button
                        type="button"
                        className={selected?.id === row.id ? "active" : ""}
                        onClick={() => {
                          setSelected(row);
                          setPatch(row.patch);
                        }}
                      >
                        <span className="mono">
                          {safeDecodePath(row.target)}
                        </span>
                        <Tag color={meta.color}>{meta.label}</Tag>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
          <section className="card section-card">
            {selected ? (
              <>
                <div className="card-title-row">
                  <h2 className="card-title mono">
                    {safeDecodePath(selected.target)}
                  </h2>
                  {selectedMeta && (
                    <Tag color={selectedMeta.color}>{selectedMeta.label}</Tag>
                  )}
                </div>
                <p className="muted">{selected.reason}</p>
                <pre className="diff-block">{selected.diff}</pre>
                {selected.status === "pending" && (
                  <>
                    <div className="field">
                      <label className="field-label" htmlFor="review-patch">
                        {t.correctionPatch}
                      </label>
                      <textarea
                        id="review-patch"
                        aria-label={t.correctionPatch}
                        rows={6}
                        value={patch}
                        onChange={(event) => setPatch(event.target.value)}
                      />
                      <p className="field-hint">{t.correctionPatchHint}</p>
                    </div>
                    <div className="save-bar">
                      <button
                        type="button"
                        className="primary"
                        onClick={() => void review("approve")}
                      >
                        {t.correctionApprove}
                      </button>
                      <button
                        type="button"
                        onClick={() => void review("approve", true)}
                      >
                        {t.correctionApproveEdited}
                      </button>
                      <button
                        type="button"
                        className="danger"
                        onClick={() => void review("reject")}
                      >
                        {t.correctionReject}
                      </button>
                    </div>
                  </>
                )}
              </>
            ) : (
              <div className="empty-state">{t.noSelection}</div>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
