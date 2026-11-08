import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Select, Table, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type ProjectRow = components["schemas"]["ProjectView"];

function localTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "";
}

export function ProjectsPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<ProjectRow["type"]>("warehouse");
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/projects", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      setRows((await response.json()) as ProjectRow[]);
    } catch {
      setError(t.projectError);
    }
  }, [token, t.projectError]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  async function create() {
    try {
      const response = await fetch("/api/v1/projects", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name, type }),
      });
      if (response.status === 409) {
        setError(t.projectNameTaken);
        return;
      }
      if (!response.ok) throw new Error();
      setOpen(false);
      setName("");
      setType("warehouse");
      setError("");
      void refresh();
    } catch {
      setError(t.projectError);
    }
  }
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.projects}</h1>
          <p className="page-subtitle">{t.projectsHint}</p>
        </div>
        <div className="page-actions">
          <Button
            autoInsertSpace={false}
            type="primary"
            onClick={() => {
              setOpen(true);
              setName("");
              setType("warehouse");
              setError("");
            }}
          >
            {t.createProject}
          </Button>
        </div>
      </div>
      {error && <Alert type="error" title={error} />}
      <div className="card section-card token-table-card">
        <Table
          rowKey="id"
          dataSource={rows}
          pagination={false}
          columns={[
            { title: t.name, dataIndex: "name" },
            {
              title: t.projectType,
              render: (_, row) =>
                row.type === "document"
                  ? t.projectTypeDocument
                  : t.projectTypeWarehouse,
            },
            {
              title: t.status,
              render: (_, row) =>
                row.status === "active" ? (
                  <Tag color="green">{t.active}</Tag>
                ) : (
                  <Tag>{t.archived}</Tag>
                ),
            },
            {
              title: t.created,
              render: (_, row) => localTime(row.created_at),
            },
          ]}
        />
      </div>
      {open && (
        <Modal
          open
          title={t.createProject}
          okText={t.create}
          cancelText={t.cancel}
          onOk={() => void create()}
          onCancel={() => setOpen(false)}
        >
          <div className="stack">
            <label className="field">
              <span className="field-label">{t.name}</span>
              <Input
                aria-label={t.name}
                value={name}
                maxLength={200}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">{t.projectType}</span>
              <Select
                aria-label={t.projectType}
                value={type}
                onChange={(value) => setType(value)}
                options={[
                  { value: "warehouse", label: t.projectTypeWarehouse },
                  { value: "document", label: t.projectTypeDocument },
                ]}
              />
              <p className="field-hint">{t.projectTypeHint}</p>
            </label>
          </div>
        </Modal>
      )}
    </section>
  );
}
