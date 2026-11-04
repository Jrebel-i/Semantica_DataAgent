import { useEffect, useState } from "react";
import { Alert, Button, Input, Space, Table, Typography } from "antd";
import type { paths } from "./api";
import type { Messages } from "./i18n";
import { DimensionStatus } from "./DimensionStatus";

type WarehouseTable =
  paths["/api/v1/tables"]["get"]["responses"][200]["content"]["application/json"][number];

export function KnowledgePage({
  token,
  messages,
}: {
  token: string;
  messages: Messages;
}) {
  const [tables, setTables] = useState<WarehouseTable[]>([]);
  const [selected, setSelected] = useState<WarehouseTable | null>(null);
  const [database, setDatabase] = useState("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams();
    if (database) params.set("database", database);
    if (search) params.set("search", search);
    fetch(`/api/v1/tables?${params}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<WarehouseTable[]>;
      })
      .then((data) => {
        setTables(data);
        setError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [token, database, search]);
  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      <Space>
        <Input
          aria-label={messages.database}
          placeholder={messages.database}
          value={database}
          onChange={(e) => setDatabase(e.target.value)}
        />
        <Input
          aria-label={messages.search}
          placeholder={messages.search}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </Space>
      <Table
        rowKey="ref"
        dataSource={tables}
        columns={[
          { title: messages.database, dataIndex: "database" },
          {
            title: messages.tableName,
            render: (_, row) => (
              <Button type="link" onClick={() => setSelected(row)}>
                {row.name}
              </Button>
            ),
          },
          { title: messages.comment, dataIndex: "comment" },
        ]}
      />
      {selected && (
        <>
          <Typography.Title level={3}>
            {selected.database}.{selected.name}
          </Typography.Title>
          <Table
            rowKey="name"
            pagination={false}
            dataSource={selected.fields}
            columns={[
              { title: messages.field, dataIndex: "name" },
              { title: messages.dataType, dataIndex: "type" },
              { title: messages.comment, dataIndex: "comment" },
              {
                title: messages.dimStatus,
                render: (_, row) => (
                  <DimensionStatus
                    count={row.dimension_count}
                    tooMany={row.dimension_too_many}
                    messages={messages}
                  />
                ),
              },
              {
                title: messages.partition,
                render: (_, row) =>
                  row.partition ? messages.yes : messages.no,
              },
            ]}
          />
        </>
      )}
    </>
  );
}
