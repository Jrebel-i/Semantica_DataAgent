import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Select, Space, Table, Tag } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type ProjectRow = components["schemas"]["ProjectView"];
type ProjectRole = "admin" | "member";

type MemberRow = {
  id: string;
  name: string;
  role: ProjectRole;
};

type UserRow = {
  id: string;
  name: string;
  role: "user" | "admin";
};

function localTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "";
}

export function ProjectsPage({
  token,
  messages: t,
  canCreate = true,
}: {
  token: string;
  messages: Messages;
  canCreate?: boolean;
}) {
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<ProjectRow["type"]>("warehouse");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ProjectRow | null>(null);
  const [members, setMembers] = useState<MemberRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [addUserId, setAddUserId] = useState<string>();
  const [addRole, setAddRole] = useState<ProjectRole>("member");
  const [memberError, setMemberError] = useState("");
  const [confirmMember, setConfirmMember] = useState<MemberRow | null>(null);

  const authHeaders = { Authorization: `Bearer ${token}` };
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/projects", {
        headers: authHeaders,
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

  const loadMembers = useCallback(
    async (projectId: string) => {
      try {
        const [memberResponse, userResponse] = await Promise.all([
          fetch(`/api/v1/projects/${projectId}/members`, {
            headers: authHeaders,
          }),
          fetch("/api/v1/users", { headers: authHeaders }),
        ]);
        if (!memberResponse.ok || !userResponse.ok) throw new Error();
        setMembers((await memberResponse.json()) as MemberRow[]);
        setUsers((await userResponse.json()) as UserRow[]);
        setMemberError("");
      } catch {
        setMemberError(t.memberError);
      }
    },
    [token, t.memberError],
  );

  async function openProject(project: ProjectRow) {
    setSelected(project);
    setMemberError("");
    setAddUserId(undefined);
    setAddRole("member");
    await loadMembers(project.id);
  }

  async function create() {
    try {
      const response = await fetch("/api/v1/projects", {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
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

  async function putRole(member: MemberRow, role: ProjectRole) {
    if (!selected) return;
    try {
      const response = await fetch(
        `/api/v1/projects/${selected.id}/members/${member.id}`,
        {
          method: "PUT",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        },
      );
      if (response.status === 409) {
        setMemberError(t.lastProjectAdmin);
        return;
      }
      if (!response.ok) throw new Error();
      await loadMembers(selected.id);
    } catch {
      setMemberError(t.memberError);
    }
  }

  async function addMember() {
    if (!selected || !addUserId) return;
    const user = users.find((item) => item.id === addUserId);
    if (!user) return;
    await putRole({ id: user.id, name: user.name, role: addRole }, addRole);
    setAddUserId(undefined);
    setAddRole("member");
  }

  async function removeMember() {
    if (!selected || confirmMember === null) return;
    try {
      const response = await fetch(
        `/api/v1/projects/${selected.id}/members/${confirmMember.id}`,
        { method: "DELETE", headers: authHeaders },
      );
      if (response.status === 409) {
        setMemberError(t.lastProjectAdmin);
        setConfirmMember(null);
        return;
      }
      if (!response.ok) throw new Error();
      setConfirmMember(null);
      await loadMembers(selected.id);
    } catch {
      setMemberError(t.memberError);
    }
  }

  if (selected) {
    const adminCount = members.filter((item) => item.role === "admin").length;
    const lastAdmin = (member: MemberRow) =>
      member.role === "admin" && adminCount <= 1;
    const availableUsers = users.filter(
      (user) => !members.some((member) => member.id === user.id),
    );
    return (
      <section className="stack admin-page">
        <div className="page-header">
          <div>
            <h1 className="page-title">{selected.name}</h1>
            <p className="page-subtitle">{t.projectMembersHint}</p>
          </div>
          <div className="page-actions">
            <Button autoInsertSpace={false} onClick={() => setSelected(null)}>
              {t.backToProjects}
            </Button>
          </div>
        </div>
        {memberError && <Alert type="error" title={memberError} />}
        <div className="card section-card">
          <h2 className="card-title">{t.projectMembers}</h2>
          <Space wrap className="member-add">
            <Select
              aria-label={t.selectUser}
              placeholder={t.selectUser}
              value={addUserId}
              onChange={setAddUserId}
              style={{ minWidth: 220 }}
              options={availableUsers.map((user) => ({
                value: user.id,
                label: `${user.name} (${user.role === "admin" ? t.roleAdmin : t.roleUser})`,
              }))}
            />
            <Select
              aria-label={t.selectProjectRole}
              value={addRole}
              onChange={setAddRole}
              options={[
                { value: "member", label: t.projectMemberRole },
                { value: "admin", label: t.projectAdminRole },
              ]}
            />
            <Button
              autoInsertSpace={false}
              type="primary"
              disabled={!addUserId}
              onClick={() => void addMember()}
            >
              {t.addMember}
            </Button>
          </Space>
          <Table
            rowKey="id"
            dataSource={members}
            pagination={false}
            locale={{ emptyText: t.noMembers }}
            columns={[
              { title: t.name, dataIndex: "name" },
              {
                title: t.role,
                render: (_, member) => (
                  <Select
                    aria-label={`${t.selectProjectRole}: ${member.name}`}
                    value={member.role}
                    disabled={lastAdmin(member)}
                    title={lastAdmin(member) ? t.lastProjectAdmin : undefined}
                    onChange={(role) => void putRole(member, role)}
                    options={[
                      { value: "member", label: t.projectMemberRole },
                      { value: "admin", label: t.projectAdminRole },
                    ]}
                  />
                ),
              },
              {
                title: t.actions,
                render: (_, member) => (
                  <Button
                    autoInsertSpace={false}
                    danger
                    disabled={lastAdmin(member)}
                    title={lastAdmin(member) ? t.lastProjectAdmin : undefined}
                    onClick={() => setConfirmMember(member)}
                  >
                    {t.removeMember}
                  </Button>
                ),
              },
            ]}
          />
        </div>
        {confirmMember !== null && (
          <Modal
            open
            title={t.confirmRemoveMember}
            okText={t.confirmRemoveMember}
            cancelText={t.cancel}
            onOk={() => void removeMember()}
            onCancel={() => setConfirmMember(null)}
          >
            <p>{`${t.confirmRemoveMember}: ${confirmMember.name}`}</p>
          </Modal>
        )}
      </section>
    );
  }

  const visibleRows = canCreate
    ? rows
    : rows.filter((row) => row.my_role === "admin");
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.projects}</h1>
          <p className="page-subtitle">{t.projectsHint}</p>
        </div>
        {canCreate && (
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
        )}
      </div>
      {error && <Alert type="error" title={error} />}
      <div className="card section-card token-table-card">
        <Table
          rowKey="id"
          dataSource={visibleRows}
          pagination={false}
          onRow={(row) => ({
            onClick: () => void openProject(row),
            style: { cursor: "pointer" },
          })}
          columns={[
            {
              title: t.name,
              render: (_, row) => (
                <a
                  onClick={(event) => {
                    event.stopPropagation();
                    void openProject(row);
                  }}
                >
                  {row.name}
                </a>
              ),
            },
            {
              title: t.projectType,
              render: (_, row) =>
                row.type === "document"
                  ? t.projectTypeDocument
                  : t.projectTypeWarehouse,
            },
            {
              title: t.role,
              render: (_, row) =>
                row.my_role === "admin"
                  ? t.projectAdminRole
                  : t.projectMemberRole,
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
            {
              title: t.actions,
              render: (_, row) => (
                <Button
                  autoInsertSpace={false}
                  onClick={(event) => {
                    event.stopPropagation();
                    void openProject(row);
                  }}
                >
                  {t.projectMembers}
                </Button>
              ),
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
