import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Select, Space, Tag } from "antd";
import type { components } from "./api";
import { readErrorDetail } from "./httpErrors";
import type { Messages } from "./i18n";
import { Markdown } from "./Markdown";
import { ProjectSelector, storedProject, useProjects } from "./ProjectSelector";

type HistoricalTicket = components["schemas"]["HistoricalTicketView"];
type HistoricalTicketList = components["schemas"]["HistoricalTicketListView"];

function extractMarkdownSection(content: string, heading: string): string {
  const marker = `## ${heading}`;
  const idx = content.indexOf(marker);
  if (idx < 0) return "";
  const after = content.slice(idx + marker.length);
  const nextIdx = after.search(/\n##\s+/);
  return (nextIdx >= 0 ? after.slice(0, nextIdx) : after).trim();
}

export function HistoricalTicketsPage({
  token,
  messages: t,
  onNavigateCorrections,
}: {
  token: string;
  messages: Messages;
  onNavigateCorrections?: () => void;
}) {
  const [project, setProject] = useState(storedProject);
  const { projects } = useProjects(token);
  const currentProject = useMemo(
    () => projects.find((item) => item.id === project) ?? null,
    [projects, project],
  );
  const canManage = currentProject?.my_role === "admin";
  const isArchived = currentProject?.status === "archived";

  const currentProjectRef = useRef(project);
  const requestGeneration = useRef(0);
  if (currentProjectRef.current !== project) {
    requestGeneration.current++;
  }
  currentProjectRef.current = project;

  const [tickets, setTickets] = useState<HistoricalTicket[]>([]);
  const [pendingProposalCount, setPendingProposalCount] = useState(0);
  const [totalTables, setTotalTables] = useState(0);
  const [selectedSlug, setSelectedSlug] = useState("");
  const [search, setSearch] = useState("");
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");
  const [error, setError] = useState("");

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<HistoricalTicket | null>(null);
  const [formTitle, setFormTitle] = useState("");
  const [formCaliber, setFormCaliber] = useState("");
  const [formSql, setFormSql] = useState("");
  const [formDialect, setFormDialect] = useState<"doris" | "hive" | "spark">(
    "doris",
  );
  const [formCaveats, setFormCaveats] = useState("");
  const [saving, setSaving] = useState(false);

  const projectQuery = `project=${encodeURIComponent(project)}`;

  const loadTickets = useCallback(async () => {
    const generation = ++requestGeneration.current;
    try {
      const response = await fetch(
        `/api/v1/historical-tickets?${projectQuery}`,
        {
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(
          await readErrorDetail(response, t.historicalTicketsLoadError),
        );
      }
      const data = (await response.json()) as HistoricalTicketList;
      if (
        currentProjectRef.current !== project ||
        generation !== requestGeneration.current
      ) {
        return;
      }
      const items = Array.isArray(data.items) ? data.items : [];
      setTickets(items);
      setPendingProposalCount(data.pending_proposal_count ?? 0);
      setTotalTables(data.total_tables ?? 0);
      setError("");
    } catch (failure) {
      if (
        currentProjectRef.current === project &&
        generation === requestGeneration.current
      ) {
        setError(
          failure instanceof Error
            ? failure.message
            : t.historicalTicketsLoadError,
        );
      }
    }
  }, [project, projectQuery, token, t.historicalTicketsLoadError]);

  useEffect(() => {
    void loadTickets();
  }, [loadTickets]);

  const filteredTickets = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return tickets;
    return tickets.filter(
      (item) =>
        item.title.toLowerCase().includes(needle) ||
        item.summary.toLowerCase().includes(needle) ||
        item.sql_preview.toLowerCase().includes(needle) ||
        (item.tables ?? []).some((tbl) => tbl.toLowerCase().includes(needle)) ||
        item.slug.toLowerCase().includes(needle),
    );
  }, [tickets, search]);

  const selected =
    filteredTickets.find((item) => item.slug === selectedSlug) ??
    filteredTickets[0] ??
    null;

  function openCreateModal() {
    setEditing(null);
    setFormTitle("");
    setFormCaliber("");
    setFormSql("");
    setFormDialect("doris");
    setFormCaveats("");
    setModalOpen(true);
  }

  function openEditModal(ticket: HistoricalTicket) {
    setEditing(ticket);
    setFormTitle(ticket.title);
    setFormCaliber(
      extractMarkdownSection(ticket.content, "业务口径与上下文") ||
        ticket.summary,
    );
    setFormSql(ticket.sql_preview);
    setFormDialect(
      ticket.dialect === "hive" || ticket.dialect === "spark"
        ? ticket.dialect
        : "doris",
    );
    setFormCaveats(extractMarkdownSection(ticket.content, "注意事项与避坑点"));
    setModalOpen(true);
  }

  async function saveTicket() {
    setSaving(true);
    setError("");
    try {
      const url = editing
        ? `/api/v1/historical-tickets/${encodeURIComponent(editing.slug)}?${projectQuery}`
        : `/api/v1/historical-tickets?${projectQuery}`;
      const response = await fetch(url, {
        method: editing ? "PATCH" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          title: formTitle,
          caliber: formCaliber,
          reference_sql: formSql,
          dialect: formDialect,
          caveats: formCaveats,
        }),
      });
      if (!response.ok) {
        throw new Error(
          await readErrorDetail(response, t.historicalTicketsSaveError),
        );
      }
      const saved = (await response.json()) as HistoricalTicket;
      setModalOpen(false);
      setSelectedSlug(saved.slug);
      await loadTickets();
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : t.historicalTicketsSaveError,
      );
    } finally {
      setSaving(false);
    }
  }

  async function deleteTicket(ticket: HistoricalTicket) {
    setError("");
    try {
      const response = await fetch(
        `/api/v1/historical-tickets/${encodeURIComponent(ticket.slug)}?${projectQuery}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(
          await readErrorDetail(response, t.historicalTicketsSaveError),
        );
      }
      await loadTickets();
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : t.historicalTicketsSaveError,
      );
    }
  }

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.historicalTicketsTab}</h1>
          <p className="page-subtitle">{t.historicalTicketsSubtitle}</p>
        </div>
        <Button autoInsertSpace={false} onClick={() => void loadTickets()}>
          {t.refresh}
        </Button>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
          includeArchived
        />
      </div>

      <section className="card gov-metrics-card">
        <div className="gov-metrics">
          <div className="gov-metric">
            <span className="gov-metric-label">
              {t.historicalTicketsMetricTotal}
            </span>
            <strong>{tickets.length}</strong>
          </div>
          <div className="gov-metric">
            <span className="gov-metric-label">
              {t.historicalTicketsMetricPending}
            </span>
            <strong>{pendingProposalCount}</strong>
          </div>
          <div className="gov-metric">
            <span className="gov-metric-label">
              {t.historicalTicketsMetricTables}
            </span>
            <strong>{totalTables}</strong>
          </div>
        </div>
      </section>

      {error && <Alert type="error" title={error} showIcon />}

      {onNavigateCorrections && canManage && (
        <div className="gov-info-banner">
          <span>
            {t.historicalTicketsMetricPending}: {pendingProposalCount}
          </span>
          <Button type="link" size="small" onClick={onNavigateCorrections}>
            {t.historicalTicketsViewPending}
          </Button>
        </div>
      )}

      <div className="gov-split-layout">
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.historicalTicketsTab}</h2>
            <Space size={6}>
              <span className="muted">{filteredTickets.length}</span>
              {canManage && !isArchived && (
                <Button
                  size="small"
                  type="primary"
                  autoInsertSpace={false}
                  onClick={openCreateModal}
                >
                  {t.historicalTicketsAdd}
                </Button>
              )}
            </Space>
          </div>
          <Input
            style={{ marginBottom: 10 }}
            aria-label={t.historicalTicketsSearchPlaceholder}
            placeholder={t.historicalTicketsSearchPlaceholder}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          {filteredTickets.length === 0 ? (
            <div className="empty-state">{t.historicalTicketsEmpty}</div>
          ) : (
            <ul className="correction-list">
              {filteredTickets.map((item) => (
                <li key={item.slug}>
                  <button
                    type="button"
                    className={selected?.slug === item.slug ? "active" : ""}
                    onClick={() => setSelectedSlug(item.slug)}
                  >
                    <span title={item.title}>{item.title}</span>
                    <Space size={4}>
                      <Tag>{item.source_label}</Tag>
                      {(item.tables ?? []).length > 0 && (
                        <Tag color="blue">{(item.tables ?? []).length} 表</Tag>
                      )}
                    </Space>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card section-card">
          {selected ? (
            <div className="stack">
              <div className="card-title-row">
                <div>
                  <h2 className="card-title">{selected.title}</h2>
                  <div className="muted mono">{selected.path}</div>
                </div>
                <Space size={6} wrap>
                  <Tag color="geekblue">{selected.source_label}</Tag>
                  <Tag>{selected.dialect}</Tag>
                  <Button
                    size="small"
                    autoInsertSpace={false}
                    onClick={() =>
                      setViewMode((prev) =>
                        prev === "rendered" ? "raw" : "rendered",
                      )
                    }
                  >
                    {viewMode === "rendered"
                      ? t.historicalTicketsViewRaw
                      : t.historicalTicketsViewRendered}
                  </Button>
                </Space>
              </div>
              {(selected.tables ?? []).length > 0 && (
                <div className="discovery-evidence-pills">
                  {(selected.tables ?? []).map((tbl) => (
                    <Tag key={tbl} className="mono" color="blue">
                      {tbl}
                    </Tag>
                  ))}
                </div>
              )}
              {viewMode === "rendered" ? (
                <div className="source-doc">
                  <Markdown text={selected.content} />
                </div>
              ) : (
                <pre className="source-raw mono">{selected.content}</pre>
              )}
              {canManage && !isArchived && (
                <Space wrap>
                  <Button
                    size="small"
                    autoInsertSpace={false}
                    onClick={() => openEditModal(selected)}
                  >
                    {t.historicalTicketsEdit}
                  </Button>
                  <Button
                    size="small"
                    danger
                    autoInsertSpace={false}
                    onClick={() => void deleteTicket(selected)}
                  >
                    {t.historicalTicketsDelete}
                  </Button>
                </Space>
              )}
            </div>
          ) : (
            <div className="empty-state">{t.historicalTicketsEmpty}</div>
          )}
        </section>
      </div>

      <Modal
        open={modalOpen}
        title={editing ? t.historicalTicketsEdit : t.historicalTicketsAdd}
        okText={t.historicalTicketsSave}
        okButtonProps={{ autoInsertSpace: false, disabled: !formTitle.trim() }}
        cancelText={t.cancel}
        confirmLoading={saving}
        onOk={() => void saveTicket()}
        onCancel={() => setModalOpen(false)}
      >
        <div className="stack">
          <div className="field">
            <label className="field-label" htmlFor="ticket-title-input">
              {t.historicalTicketsTitleLabel}
            </label>
            <Input
              id="ticket-title-input"
              value={formTitle}
              onChange={(e) => setFormTitle(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ticket-caliber-input">
              {t.historicalTicketsCaliberLabel}
            </label>
            <Input.TextArea
              id="ticket-caliber-input"
              rows={3}
              value={formCaliber}
              onChange={(e) => setFormCaliber(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ticket-sql-input">
              {t.historicalTicketsSqlLabel}
            </label>
            <Input.TextArea
              id="ticket-sql-input"
              rows={4}
              value={formSql}
              onChange={(e) => setFormSql(e.target.value)}
            />
          </div>
          <div className="field">
            <span className="field-label">{t.evalDialectLabel}</span>
            <Select
              aria-label={t.evalDialectLabel}
              value={formDialect}
              onChange={setFormDialect}
              style={{ minWidth: 140 }}
              options={[
                { value: "doris", label: "doris" },
                { value: "hive", label: "hive" },
                { value: "spark", label: "spark" },
              ]}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ticket-caveats-input">
              {t.historicalTicketsCaveatsLabel}
            </label>
            <Input.TextArea
              id="ticket-caveats-input"
              rows={2}
              value={formCaveats}
              onChange={(e) => setFormCaveats(e.target.value)}
            />
          </div>
        </div>
      </Modal>
    </section>
  );
}
