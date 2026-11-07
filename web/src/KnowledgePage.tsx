import { Fragment, useEffect, useMemo, useRef, useState } from "react";
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

type LayerId =
  | "raw"
  | "detail"
  | "summary"
  | "mart"
  | "application"
  | "dimension"
  | "temporary"
  | "other";

const TABLE_PAGE_SIZE = 100;

export const WAREHOUSE_LAYERS: {
  id: LayerId;
  pattern: RegExp | null;
  label: keyof Messages;
}[] = [
  { id: "raw", pattern: /^ods(?:_|$)/i, label: "layerRaw" },
  { id: "detail", pattern: /^dwd(?:_|$)/i, label: "layerDetail" },
  { id: "summary", pattern: /^(?:dws|dwt)(?:_|$)/i, label: "layerSummary" },
  { id: "mart", pattern: /^dm(?:_|$)/i, label: "layerMart" },
  { id: "application", pattern: /^app(?:_|$)/i, label: "layerApplication" },
  {
    id: "dimension",
    pattern: /^dimensions(?:_|$)/i,
    label: "layerDimension",
  },
  { id: "temporary", pattern: /^tmp(?:_|$)/i, label: "layerTemporary" },
  { id: "other", pattern: null, label: "otherLayer" },
];

export function warehouseLayer(database: string): LayerId {
  return (
    WAREHOUSE_LAYERS.find((layer) => layer.pattern?.test(database))?.id ??
    "other"
  );
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
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [tableLimits, setTableLimits] = useState<Record<string, number>>({});
  const [treeHeight, setTreeHeight] = useState(0);
  const [error, setError] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [patch, setPatch] = useState("");
  const [reason, setReason] = useState("");
  const [proposalStatus, setProposalStatus] = useState("");
  const treeScrollRef = useRef<HTMLDivElement>(null);
  const treeLoaded = useRef(false);
  const previousSearch = useRef("");

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

  const needle = search.trim().toLowerCase();
  const visibleTables = useMemo(
    () =>
      tables.filter(
        (row) =>
          !needle ||
          row.name.toLowerCase().includes(needle) ||
          (row.comment || "").toLowerCase().includes(needle) ||
          row.database.toLowerCase().includes(needle),
      ),
    [needle, tables],
  );
  const visibleCategories = useMemo(
    () =>
      categories
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
          ...category,
          documents: category.documents.filter(
            (document) =>
              !needle || document.path.toLowerCase().includes(needle),
          ),
        })),
    [categories, needle],
  );
  const tableLayers = useMemo(() => {
    const grouped = new Map<LayerId, Map<string, WarehouseTable[]>>();
    for (const row of visibleTables) {
      const layer = warehouseLayer(row.database);
      const databases =
        grouped.get(layer) ?? new Map<string, WarehouseTable[]>();
      grouped.set(layer, databases);
      const rows = databases.get(row.database) ?? [];
      rows.push(row);
      databases.set(row.database, rows);
    }
    return WAREHOUSE_LAYERS.map((layer) => {
      const databases = [...(grouped.get(layer.id)?.entries() ?? [])];
      return {
        id: layer.id,
        label: messages[layer.label] as string,
        count: databases.reduce((sum, [, rows]) => sum + rows.length, 0),
        databases,
      };
    }).filter((layer) => layer.count > 0);
  }, [messages, visibleTables]);
  const selectedRef = selected?.ref ?? "";
  const selectedDatabase = selected?.database ?? "";
  const selectedLayer = selectedDatabase
    ? warehouseLayer(selectedDatabase)
    : "";
  const selectedIndex = useMemo(() => {
    if (!selectedRef) return -1;
    let index = 0;
    for (const row of visibleTables) {
      if (row.database !== selectedDatabase) continue;
      if (row.ref === selectedRef) return index;
      index += 1;
    }
    return -1;
  }, [selectedDatabase, selectedRef, visibleTables]);
  const defaultExpandedKeys = useMemo(
    () => [
      "group:docs",
      ...categories.map((category) => category.ref),
      ...(selectedRef
        ? [
            "group:tables",
            `layer:${selectedLayer}`,
            `database:${selectedDatabase}`,
          ]
        : []),
    ],
    [categories, selectedDatabase, selectedLayer, selectedRef],
  );

  useEffect(() => {
    if (!selectedRef) return;
    setExpandedKeys((keys) =>
      Array.from(
        new Set([
          ...keys,
          "group:tables",
          `layer:${selectedLayer}`,
          `database:${selectedDatabase}`,
        ]),
      ),
    );
  }, [selectedDatabase, selectedLayer, selectedRef]);

  useEffect(() => {
    if (!selectedDatabase || selectedIndex < 0) return;
    setTableLimits((current) => {
      const limit = current[selectedDatabase] ?? TABLE_PAGE_SIZE;
      if (selectedIndex < limit) return current;
      return {
        ...current,
        [selectedDatabase]:
          Math.ceil((selectedIndex + 1) / TABLE_PAGE_SIZE) * TABLE_PAGE_SIZE,
      };
    });
  }, [selectedDatabase, selectedIndex]);

  useEffect(() => {
    if (needle) return;
    setExpandedKeys((keys) =>
      Array.from(
        new Set([
          ...keys,
          "group:tables",
          ...tableLayers.map((layer) => `layer:${layer.id}`),
        ]),
      ),
    );
  }, [needle, tableLayers]);

  useEffect(() => {
    if (needle || categories.length === 0) return;
    setExpandedKeys((keys) =>
      Array.from(
        new Set([...keys, "group:docs", ...categories.map((row) => row.ref)]),
      ),
    );
  }, [categories, needle]);

  useEffect(() => {
    if (needle) {
      const keys = ["group:tables", "group:docs"];
      for (const layer of tableLayers) {
        keys.push(`layer:${layer.id}`);
        for (const [databaseName] of layer.databases)
          keys.push(`database:${databaseName}`);
      }
      for (const category of visibleCategories) keys.push(category.ref);
      setExpandedKeys(keys);
    } else if (previousSearch.current) {
      setExpandedKeys(defaultExpandedKeys);
    }
    previousSearch.current = needle;
  }, [defaultExpandedKeys, needle, tableLayers, visibleCategories]);

  useEffect(() => {
    const node = treeScrollRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const update = () => setTreeHeight(node.clientHeight);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const treeData = useMemo(() => {
    const tableChildren = tableLayers.map((layer) => ({
      key: `layer:${layer.id}`,
      title: `${layer.label} (${layer.count})`,
      children: layer.databases.map(([databaseName, rows]) => {
        const limit = tableLimits[databaseName] ?? TABLE_PAGE_SIZE;
        const shown = rows.slice(0, limit);
        const remaining = rows.length - shown.length;
        return {
          key: `database:${databaseName}`,
          title: `${databaseName} (${rows.length})`,
          children: [
            ...shown.map((row) => ({
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
                    <span
                      className="category-tree-description"
                      title={row.comment}
                    >
                      {row.comment}
                    </span>
                  )}
                </button>
              ),
              isLeaf: true,
            })),
            ...(remaining > 0
              ? [
                  {
                    key: `more:${databaseName}`,
                    isLeaf: true,
                    title: (
                      <button
                        type="button"
                        className="tree-doc-link"
                        onClick={() =>
                          setTableLimits((current) => ({
                            ...current,
                            [databaseName]:
                              (current[databaseName] ?? TABLE_PAGE_SIZE) +
                              TABLE_PAGE_SIZE,
                          }))
                        }
                      >
                        {messages.showMore} ({remaining})
                      </button>
                    ),
                  },
                ]
              : []),
          ],
        };
      }),
    }));
    const docChildren = visibleCategories.map((category) => ({
      key: category.ref,
      title: category.name,
      children: category.documents.map((document) => ({
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
        title: `${messages.knowledgeDocuments} (${visibleCategories.reduce(
          (sum, category) => sum + category.documents.length,
          0,
        )})`,
        children: docChildren,
      },
      {
        key: "group:tables",
        title: `${messages.dataTables} (${visibleTables.length})`,
        children: tableChildren,
      },
    ];
  }, [
    messages,
    tableLayers,
    tableLimits,
    visibleCategories,
    visibleTables.length,
  ]);

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
              blockNode
              expandAction="click"
              expandedKeys={expandedKeys}
              onExpand={(keys) => setExpandedKeys(keys.map(String))}
              height={treeHeight || undefined}
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
                      <Fragment key={field.name}>
                        <tr>
                          <td>
                            <button
                              type="button"
                              aria-expanded={
                                expandedColumn ===
                                `${selected.database}.${selected.name}.${field.name}`
                              }
                              onClick={() => {
                                const key = `${selected.database}.${selected.name}.${field.name}`;
                                setExpandedColumn(
                                  expandedColumn === key ? "" : key,
                                );
                              }}
                            >
                              {field.name}
                            </button>
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
                          <td>
                            {field.partition ? messages.yes : messages.no}
                          </td>
                        </tr>
                        {expandedColumn ===
                          `${selected.database}.${selected.name}.${field.name}` && (
                          <tr>
                            <td colSpan={5}>
                              <ColumnLineage
                                table={`${selected.database}.${selected.name}`}
                                column={field.name}
                                token={token}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
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
