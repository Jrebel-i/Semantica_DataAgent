import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type TokenRow = components["schemas"]["TokenInfo"] & {
  projects?: { project_id: string; role: "admin" | "member" }[];
};
type ProjectRow = components["schemas"]["ProjectView"];
type Membership = { project_id: string; role: "admin" | "member" };

function localTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "";
}

export function TokensPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [rows, setRows] = useState<TokenRow[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<TokenRow["role"]>("user");
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [issued, setIssued] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/tokens", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      setRows((await response.json()) as TokenRow[]);
    } catch {
      setError(t.tokenError);
    }
  }, [token, t.tokenError]);
  const refreshProjects = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/projects", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      setProjects((await response.json()) as ProjectRow[]);
    } catch {
      setError(t.projectError);
    }
  }, [token, t.projectError]);
  useEffect(() => {
    void refresh();
    void refreshProjects();
  }, [refresh, refreshProjects]);
  const projectName = (projectId: string) =>
    projects.find((project) => project.id === projectId)?.name ?? projectId;
  const selectProjects = (ids: string[]) =>
    setMemberships((current) =>
      ids.map(
        (id) =>
          current.find((item) => item.project_id === id) ?? {
            project_id: id,
            role: "member",
          },
      ),
    );
  const setProjectRole = (projectId: string, value: Membership["role"]) =>
    setMemberships((current) =>
      current.map((item) =>
        item.project_id === projectId ? { ...item, role: value } : item,
      ),
    );
  async function create() {
    try {
      const response = await fetch("/api/v1/tokens", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name,
          role,
          ...(role === "user" && memberships.length > 0
            ? { projects: memberships }
            : {}),
        }),
      });
      if (!response.ok) throw new Error();
      const result =
        (await response.json()) as components["schemas"]["TokenIssued"];
      setIssued(result.token);
      setError("");
      void refresh();
    } catch {
      setError(t.tokenError);
    }
  }
  async function revoke(id: string) {
    try {
      const response = await fetch(`/api/v1/tokens/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        setError(response.status === 409 ? t.lastAdmin : t.tokenError);
        return;
      }
      setConfirm(null);
      setError("");
      void refresh();
    } catch {
      setError(t.tokenError);
    }
  }
  const closeIssue = () => {
    setOpen(false);
    setIssued("");
  };
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.tokens}</h1>
          <p className="page-subtitle">{t.tokenHint}</p>
        </div>
        <div className="page-actions">
          <Button
            autoInsertSpace={false}
            type="primary"
            onClick={() => {
              setOpen(true);
              setName("");
              setRole("user");
              setMemberships([]);
              setError("");
            }}
          >
            {t.issue}
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
              title: t.role,
              render: (_, row) =>
                row.role === "admin" ? t.roleAdmin : t.roleUser,
            },
            {
              title: t.tokenProjects,
              render: (_, row) =>
                row.projects && row.projects.length > 0 ? (
                  <Space wrap>
                    {row.projects.map((item) => (
                      <Tag key={item.project_id}>
                        {projectName(item.project_id)} ·{" "}
                        {item.role === "admin"
                          ? t.projectAdminRole
                          : t.projectMemberRole}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  t.noProjectMembership
                ),
            },
            {
              title: t.created,
              render: (_, row) => localTime(row.created_at),
            },
            {
              title: t.lastUsed,
              render: (_, row) =>
                row.last_used_at ? localTime(row.last_used_at) : t.never,
            },
            {
              title: t.status,
              render: (_, row) =>
                row.revoked ? (
                  <Tag color="red">{t.revoked}</Tag>
                ) : (
                  <Tag color="green">{t.active}</Tag>
                ),
            },
            {
              title: t.actions,
              render: (_, row) =>
                !row.revoked && (
                  <Button
                    autoInsertSpace={false}
                    danger
                    onClick={() => setConfirm(row.id)}
                  >
                    {t.revoke}
                  </Button>
                ),
            },
          ]}
        />
      </div>
      {open && (
        <Modal
          destroyOnHidden
          open
          title={t.issue}
          onCancel={closeIssue}
          footer={null}
        >
          {issued ? (
            <Space orientation="vertical">
              <Typography.Text>{t.once}</Typography.Text>
              <Typography.Text code>{issued}</Typography.Text>
              <Space>
                <Button
                  autoInsertSpace={false}
                  onClick={() => void navigator.clipboard.writeText(issued)}
                >
                  {t.copy}
                </Button>
                <Button autoInsertSpace={false} onClick={closeIssue}>
                  {t.close}
                </Button>
              </Space>
            </Space>
          ) : (
            <Space orientation="vertical" style={{ width: "100%" }}>
              <Typography.Text>{t.name}</Typography.Text>
              <Input
                aria-label={t.name}
                required
                maxLength={200}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <Typography.Text>{t.role}</Typography.Text>
              <Select
                aria-label={t.role}
                style={{ width: "100%" }}
                value={role}
                onChange={setRole}
                options={[
                  { value: "user", label: t.roleUser },
                  { value: "admin", label: t.roleAdmin },
                ]}
              />
              {role === "user" && (
                <>
                  <Typography.Text>{t.tokenProjects}</Typography.Text>
                  <Select
                    mode="multiple"
                    aria-label={t.selectProjects}
                    placeholder={t.selectProjects}
                    style={{ width: "100%" }}
                    value={memberships.map((item) => item.project_id)}
                    onChange={selectProjects}
                    optionFilterProp="label"
                    options={projects.map((project) => ({
                      value: project.id,
                      label: project.name,
                    }))}
                  />
                  {memberships.map((item) => (
                    <Space key={item.project_id} wrap>
                      <span>{projectName(item.project_id)}</span>
                      <Select
                        aria-label={`${t.selectProjectRole}: ${projectName(item.project_id)}`}
                        value={item.role}
                        onChange={(value) =>
                          setProjectRole(item.project_id, value)
                        }
                        options={[
                          { value: "member", label: t.projectMemberRole },
                          { value: "admin", label: t.projectAdminRole },
                        ]}
                      />
                    </Space>
                  ))}
                  <p className="field-hint">{t.tokenProjectsHint}</p>
                </>
              )}
              <Space>
                <Button
                  autoInsertSpace={false}
                  type="primary"
                  onClick={() => void create()}
                >
                  {t.create}
                </Button>
                <Button autoInsertSpace={false} onClick={closeIssue}>
                  {t.close}
                </Button>
              </Space>
            </Space>
          )}
        </Modal>
      )}
      {confirm !== null && (
        <Modal
          destroyOnHidden
          open
          title={t.confirmRevoke}
          onCancel={() => setConfirm(null)}
          footer={[
            <Button
              autoInsertSpace={false}
              key="cancel"
              onClick={() => setConfirm(null)}
            >
              {t.cancel}
            </Button>,
            <Button
              autoInsertSpace={false}
              key="revoke"
              danger
              type="primary"
              onClick={() => confirm && void revoke(confirm)}
            >
              {t.confirmRevoke}
            </Button>,
          ]}
        >
          <p>{`${t.confirmRevoke}?`}</p>
        </Modal>
      )}
    </section>
  );
}
