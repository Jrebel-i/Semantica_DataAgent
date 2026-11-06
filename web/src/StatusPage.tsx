import { Button, Card, Table } from "antd";
import type { paths } from "./api";
import type { Messages } from "./i18n";

export type Health =
  paths["/healthz"]["get"]["responses"][200]["content"]["application/json"];
export function StatusPage({
  messages,
  health,
  onRefresh,
}: {
  messages: Messages;
  health: Health | null;
  onRefresh: () => void;
}) {
  return (
    <Card className="section-card" title={messages.statusPage}>
      <div className="page-actions" style={{ marginBottom: 12 }}>
        <Button onClick={onRefresh}>{messages.refresh}</Button>
      </div>
      <Table
        pagination={false}
        rowKey="name"
        dataSource={Object.entries(health || {}).map(([name, value]) => ({
          name,
          ...value,
        }))}
        columns={[
          { title: messages.service, dataIndex: "name" },
          { title: messages.status, render: (_, row) => messages[row.status] },
          { title: messages.time, dataIndex: "elapsed_ms" },
        ]}
      />
    </Card>
  );
}
