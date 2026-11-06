import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Table, Tree, Typography } from "antd";
import type { paths } from "./api";
import { TaskLineage, type TableLineage } from "./TaskLineage";
import type { Messages } from "./i18n";
import { ColumnLineage } from "./ColumnLineage";
import { DimensionStatus } from "./DimensionStatus";

type WarehouseTable =
  paths["/api/v1/tables"]["get"]["responses"][200]["content"]["application/json"][number];
type Category =
  paths["/api/v1/categories"]["get"]["responses"][200]["content"]["application/json"][number];
type Document =
  paths["/api/v1/document"]["get"]["responses"][200]["content"]["application/json"];

function groupTables(
  rows: WarehouseTable[],
  key: (row: WarehouseTable) => string,
): Record<string, WarehouseTable[]> {
  const groups: Record<string, WarehouseTable[]> = Object.create(
    null,
  ) as Record<string, WarehouseTable[]>;
  for (const row of rows) (groups[key(row)] ??= []).push(row);
  return groups;
}

export function KnowledgePage({
  token,
  messages,
}: {
  token: string;
  messages: Messages;
}) {
  const [tables, setTables] = useState<WarehouseTable[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [selected, setSelected] = useState<WarehouseTable | null>(null);
  const [readingDoc, setReadingDoc] = useState<Document | null>(null);
  const [linkedDocument, setLinkedDocument] = useState<Document | null>(null);
  const [expandedColumn, setExpandedColumn] = useState("");
  const [lineage, setLineage] = useState<TableLineage | null>(null);
  const [database, setDatabase] = useState("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [patch, setPatch] = useState("");
  const [reason, setReason] = useState("");
  const [proposalStatus, setProposalStatus] = useState("");
  const treeScrollRef = useRef<HTMLDivElement>(null);
  const treeLoaded = useRef(false);

  useEffect(() => {
    setLinkedDocument(null);
    setLineage(null);
    setPatch("");
    setReason("");
    setProposalStatus("");
  }, [selected]);

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
        if (!selected && !readingDoc && data.length > 0) setSelected(data[0]);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
    // The selection is intentionally read once when a result set arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, database, search]);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/categories", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Category[]>;
      })
      .then(setCategories)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [token]);

  useEffect(() => {
    if (treeLoaded.current || !treeScrollRef.current) return;
    if (!tables.length && !categories.length) return;
    treeLoaded.current = true;
    treeScrollRef.current.scrollTop = 0;
  }, [tables.length, categories.length]);

  useEffect(() => {
    const container = treeScrollRef.current;
    const key = selected?.ref ?? readingDoc?.ref;
    if (!container || !key) return;
    const frame = requestAnimationFrame(() => {
      const row =
        container
          .querySelector<HTMLElement>(".ant-tree-node-selected")
          ?.closest<HTMLElement>(".ant-tree-treenode") ??
        container.querySelector<HTMLElement>(".ant-tree-treenode-selected");
      if (typeof row?.scrollIntoView === "function") {
        row.scrollIntoView({ block: "nearest" });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [selected?.ref, readingDoc?.ref, tables.length, categories.length]);

  async function openDocument(path: string) {
    try {
      const response = await fetch(
        `/api/v1/document?${new URLSearchParams({ doc_id: path })}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      const document = (await response.json()) as Document;
      setReadingDoc(document);
      setSelected(null);
      setError(false);
    } catch {
      setError(true);
    }
  }

  async function openLinkedDocument(ref: string) {
    try {
      const response = await fetch(
        `/api/v1/document?${new URLSearchParams({ doc_id: ref })}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      setLinkedDocument((await response.json()) as Document);
      setError(false);
    } catch {
      setError(true);
    }
  }

  async function selectTableByName(name: string) {
    const found = tables.find((row) => `${row.database}.${row.name}` === name);
    if (found) {
      setSelected(found);
      setReadingDoc(null);
      return;
    }
    try {
      const response = await fetch(
        `/api/v1/tables?${new URLSearchParams({
          database: name.split(".")[0],
          search: name.split(".").slice(1).join("."),
        })}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      const rows = (await response.json()) as WarehouseTable[];
      const match = rows.find((row) => `${row.database}.${row.name}` === name);
      if (match) {
        setSelected(match);
        setReadingDoc(null);
      }
    } catch {
      setError(true);
    }
  }

  async function submitCorrection() {
    if (!selected) return;
    try {
      const response = await fetch("/api/v1/corrections", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ target: selected.ref, patch, reason }),
      });
      if (!response.ok) throw new Error();
      setProposalStatus(messages.correctionSubmitted);
      setPatch("");
      setReason("");
      setCorrectionOpen(false);
    } catch {
      setProposalStatus(messages.error);
    }
  }

  const treeData = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const tableChildren = Object.entries(
      groupTables(
        tables.filter(
          (row) =>
            !needle ||
            row.name.toLowerCase().includes(needle) ||
            (row.comment || "").toLowerCase().includes(needle) ||
            row.database.toLowerCase().includes(needle),
        ),
        (row) =>
          /^(ods|dwd|dws|ads|dim)(?:_|$)/i
            .exec(row.database)?.[1]
            ?.toUpperCase() ?? messages.otherLayer,
      ),
    ).map(([layer, layerTables]) => ({
      key: `layer:${layer}`,
      title: layer,
      children: Object.entries(
        groupTables(layerTables ?? [], (row) => row.database),
      ).map(([databaseName, rows]) => ({
        key: `database:${databaseName}`,
        title: databaseName,
        children: (rows ?? []).map((row) => ({
          key: row.ref,
          title: (
            <button
              type="button"
              className="tree-table-link"
              aria-label={row.name}
              onClick={() => {
                setSelected(row);
                setReadingDoc(null);
              }}
            >
              <span className="tree-table-name" title={row.name}>
                {row.name}
              </span>
              {row.comment && (
                <span className="category-tree-description" title={row.comment}>
                  {row.comment}
                </span>
              )}
            </button>
          ),
          isLeaf: true,
        })),
      })),
    }));
    const docChildren = categories
      .filter(
        (category) =>
          !needle ||
          category.name.toLowerCase().includes(needle) ||
          (category.description || "").toLowerCase().includes(needle) ||
          category.documents.some((document) =>
            document.path.toLowerCase().includes(needle),
          ),
      )
      .map((category) => ({
        key: category.ref,
        title: category.name,
        children: category.documents
          .filter(
            (document) =>
              !needle || document.path.toLowerCase().includes(needle),
          )
          .map((document) => ({
            key: document.ref,
            isLeaf: true,
            title: (
              <button
                type="button"
                className="tree-doc-link"
                aria-label={document.path}
                onClick={() => void openDocument(document.ref)}
              >
                {document.path}
              </button>
            ),
          })),
      }));
    return [
      {
        key: "group:docs",
        title: `${messages.knowledgeDocuments} (${categories.reduce(
          (sum, category) => sum + category.documents.length,
          0,
        )})`,
        children: docChildren,
      },
      {
        key: "group:tables",
        title: `${messages.dataTables} (${tables.length})`,
        children: tableChildren,
      },
    ];
  }, [categories, messages, readingDoc, search, tables]);

  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      <div className="knowledge-layout">
        <aside className="knowledge-tree-panel">
          <div className="knowledge-tree-head">
            <Input
              allowClear
              aria-label={messages.searchKnowledge}
              placeholder={messages.searchKnowledge}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <div className="knowledge-tree-scroll" ref={treeScrollRef}>
            <Tree
              key={`${tables.map((row) => row.ref).join("|")}-${categories
                .map((category) => category.ref)
                .join("|")}`}
              defaultExpandAll
              blockNode
              selectedKeys={
                selected?.ref
                  ? [selected.ref]
                  : readingDoc?.ref
                    ? [readingDoc.ref]
                    : []
              }
              treeData={treeData}
            />
          </div>
        </aside>
        <main className="knowledge-detail">
          {selected ? (
            <>
              <div className="detail-header">
                <div>
                  <h1 className="detail-title">
                    {selected.database}.{selected.name}
                  </h1>
                  <p className="detail-subtitle">
                    {selected.comment || messages.noDescription}
                  </p>
                </div>
                <Button onClick={() => setCorrectionOpen(true)}>
                  {messages.proposeCorrection}
                </Button>
              </div>
              {proposalStatus && (
                <Alert
                  type={proposalStatus === messages.error ? "error" : "success"}
                  title={proposalStatus}
                />
              )}
              <section className="detail-section">
                <h2 className="detail-section-title">
                  {messages.fields}
                  <span className="count">{selected.fields.length}</span>
                </h2>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{messages.field}</th>
                      <th>{messages.dataType}</th>
                      <th>{messages.comment}</th>
                      <th>{messages.dimStatus}</th>
                      <th>{messages.partition}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.fields.map((field) => (
                      <tr key={field.name}>
                        <td>
                          <button
                            type="button"
                            onClick={() =>
                              setExpandedColumn(
                                expandedColumn === field.name ? "" : field.name,
                              )
                            }
                          >
                            {field.name}
                          </button>
                          {expandedColumn === field.name && (
                            <ColumnLineage
                              table={`${selected.database}.${selected.name}`}
                              column={field.name}
                              token={token}
                            />
                          )}
                        </td>
                        <td>{field.type}</td>
                        <td>{field.comment}</td>
                        <td>
                          <DimensionStatus
                            count={field.dimension_count}
                            tooMany={field.dimension_too_many}
                            messages={messages}
                          />
                          {field.dimension_samples?.length ? (
                            <span className="subtle">
                              {" "}
                              {field.dimension_samples.join(", ")}
                            </span>
                          ) : null}
                        </td>
                        <td>{field.partition ? messages.yes : messages.no}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
              <section className="detail-section">
                <h2 className="detail-section-title">
                  {messages.contextLineage}
                </h2>
                <TaskLineage
                  table={`${selected.database}.${selected.name}`}
                  token={token}
                  messages={messages}
                  showTasks={false}
                  onData={setLineage}
                  onSelect={(name) => void selectTableByName(name)}
                />
              </section>
              <section className="detail-section">
                <h2 className="detail-section-title">
                  {messages.linkedDocuments}
                  <span className="count">
                    {(selected.documents ?? []).length}
                  </span>
                </h2>
                {(selected.documents ?? []).length === 0 ? (
                  <p className="muted">{messages.noLinkedDocuments}</p>
                ) : (
                  <div className="doc-link-list">
                    {(selected.documents ?? []).map((doc) => (
                      <Button
                        type="link"
                        key={doc.ref}
                        onClick={() => void openLinkedDocument(doc.ref)}
                      >
                        {doc.path}
                      </Button>
                    ))}
                  </div>
                )}
                {linkedDocument && (
                  <div className="doc-content">
                    <div className="context-ref">{linkedDocument.path}</div>
                    <pre>{linkedDocument.content}</pre>
                  </div>
                )}
              </section>
              {lineage && lineage.tasks.length > 0 && (
                <section className="detail-section">
                  <h2 className="detail-section-title">
                    {messages.producingTasks}
                    <span className="count">{lineage.tasks.length}</span>
                  </h2>
                  <ul>
                    {lineage.tasks.map((task) => (
                      <li key={task.ref}>{task.name}</li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          ) : readingDoc ? (
            <>
              <div className="detail-header">
                <div>
                  <h1 className="detail-title">{readingDoc.path}</h1>
                  <p className="detail-subtitle">
                    {readingDoc.category}
                    {readingDoc.generated ? ` · ${messages.generated}` : ""}
                  </p>
                </div>
              </div>
              <div className="doc-content">
                <pre>
                  {readingDoc.content
                    .split("\n")
                    .map(
                      (line, index) =>
                        `${String(readingDoc.start_line + index).padStart(
                          4,
                          " ",
                        )}  ${line}`,
                    )
                    .join("\n")}
                </pre>
              </div>
            </>
          ) : (
            <div className="empty-state">{messages.noSelection}</div>
          )}
        </main>
      </div>
      <Modal
        open={correctionOpen}
        title={messages.proposeCorrection}
        onCancel={() => setCorrectionOpen(false)}
        onOk={() => void submitCorrection()}
        okText={messages.proposeCorrection}
        cancelText={messages.cancel}
        okButtonProps={{ disabled: !patch.trim() || !reason.trim() }}
      >
        <div className="correction-form">
          <div className="field">
            <label className="field-label" htmlFor="correction-patch">
              {messages.correctionReplacement}
            </label>
            <Input.TextArea
              id="correction-patch"
              aria-label={messages.correctionReplacement}
              rows={5}
              value={patch}
              onChange={(event) => setPatch(event.target.value)}
            />
            <p className="field-hint">{messages.correctionPatchHint}</p>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="correction-reason">
              {messages.correctionReason}
            </label>
            <Input
              id="correction-reason"
              aria-label={messages.correctionReason}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <p className="field-hint">{messages.correctionReasonHint}</p>
          </div>
        </div>
      </Modal>
    </>
  );
}
