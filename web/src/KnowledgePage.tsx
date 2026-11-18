import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Table, Tag, Tree, Typography } from "antd";
import type { TreeDataNode } from "antd";
import type { paths } from "./api";
import { CorrectionDraftDrawer } from "./chat/CorrectionDraftDrawer";
import { TaskLineage, type TableLineage } from "./TaskLineage";
import type { Messages } from "./i18n";
import { ColumnLineage } from "./ColumnLineage";
import { DimensionStatus } from "./DimensionStatus";
import { Markdown } from "./Markdown";
import { downloadOssieModel, exportErrorMessage } from "./ossieExport";
import { safeDecodePath } from "./paths";
import { ProjectSelector, storedProject, useProjects } from "./ProjectSelector";
import {
  loadTableDetail,
  summaryFromDetail,
  type TableDetail,
  type TableSummary,
} from "./tableDetail";

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

type ProjectBound<T> = { project: string; value: T };

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
  const [project, setProject] = useState(storedProject);
  const { projects, loaded: projectsLoaded } = useProjects(token);
  const warehouse =
    projects.find((row) => row.id === project)?.type !== "document";
  const [tables, setTables] = useState<TableSummary[]>([]);
  const [hasAnyTables, setHasAnyTables] = useState(false);
  const [categories, setCategories] = useState<Category[]>([]);
  const [selection, setSelection] = useState<ProjectBound<TableSummary> | null>(
    null,
  );
  const [readingDocState, setReadingDocState] =
    useState<ProjectBound<Document> | null>(null);
  const selected = selection?.project === project ? selection.value : null;
  const readingDoc =
    readingDocState?.project === project ? readingDocState.value : null;
  const setSelected = (table: TableSummary | null) =>
    setSelection(table ? { project, value: table } : null);
  const setReadingDoc = (document: Document | null) =>
    setReadingDocState(document ? { project, value: document } : null);
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [detailRetry, setDetailRetry] = useState(0);
  const [linkedDocument, setLinkedDocument] = useState<Document | null>(null);
  const [expandedColumn, setExpandedColumn] = useState("");
  const [lineage, setLineage] = useState<TableLineage | null>(null);
  const [database, setDatabase] = useState("");
  const [search, setSearch] = useState("");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [tableLimits, setTableLimits] = useState<Record<string, number>>({});
  const [treeHeight, setTreeHeight] = useState(0);
  const [error, setError] = useState(false);
  const [correctionTarget, setCorrectionTarget] = useState<{
    target: string;
    label?: string;
    sourceGenerated?: boolean;
    category?: string;
  } | null>(null);
  const [proposalStatus, setProposalStatus] = useState("");
  const [showRawDoc, setShowRawDoc] = useState(false);
  const [exportingOssie, setExportingOssie] = useState(false);
  const [exportError, setExportError] = useState("");
  const treeScrollRef = useRef<HTMLDivElement>(null);
  const treeLoaded = useRef(false);
  const previousSearch = useRef("");
  const previousProject = useRef(project);
  const projectGeneration = useRef(0);
  const autoSelect = useRef(true);

  useEffect(() => {
    if (previousProject.current === project) return;
    previousProject.current = project;
    projectGeneration.current += 1;
    autoSelect.current = false;
    setTables([]);
    setHasAnyTables(false);
    setCategories([]);
    setSelected(null);
    setReadingDoc(null);
    setDetail(null);
    setLinkedDocument(null);
    setLineage(null);
    setExpandedColumn("");
    setDatabase("");
    setExpandedKeys([]);
    setTableLimits({});
    setCorrectionTarget(null);
    setProposalStatus("");
    setShowRawDoc(false);
    setExportError("");
    setError(false);
  }, [project]);

  useEffect(() => {
    setLinkedDocument(null);
    setLineage(null);
    setCorrectionTarget(null);
    setProposalStatus("");
  }, [selected]);

  useEffect(() => {
    if (!projectsLoaded) return;
    if (!warehouse) {
      setTables([]);
      setHasAnyTables(false);
      setError(false);
      return;
    }
    const controller = new AbortController();
    const generation = projectGeneration.current;
    const params = new URLSearchParams();
    if (database) params.set("database", database);
    if (search) params.set("search", search);
    params.set("view", "summary");
    params.set("project", project);
    fetch(`/api/v1/tables?${params}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<TableSummary[]>;
      })
      .then((data) => {
        if (
          controller.signal.aborted ||
          generation !== projectGeneration.current
        )
          return;
        setTables(data);
        if (!search.trim() && !database) {
          setHasAnyTables(data.length > 0);
        } else if (data.length > 0) {
          setHasAnyTables(true);
        }
        setError(false);
        if (autoSelect.current && !readingDoc && data.length > 0) {
          autoSelect.current = false;
          setSelected(data[0]);
        }
      })
      .catch(() => {
        if (
          !controller.signal.aborted &&
          generation === projectGeneration.current
        )
          setError(true);
      });
    return () => controller.abort();
    // The selection is intentionally read once when a result set arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, project, database, search, projectsLoaded, warehouse]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailLoading(false);
      setDetailError(false);
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailLoading(true);
    setDetailError(false);
    loadTableDetail(token, selected.database, selected.name, project)
      .then((row) => {
        if (!cancelled) setDetail(row);
      })
      .catch(() => {
        if (!cancelled) setDetailError(true);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, project, selected?.database, selected?.name, detailRetry]);

  useEffect(() => {
    const controller = new AbortController();
    const generation = projectGeneration.current;
    fetch(`/api/v1/categories?${new URLSearchParams({ project })}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json() as Promise<Category[]>;
      })
      .then((data) => {
        if (
          !controller.signal.aborted &&
          generation === projectGeneration.current
        )
          setCategories(data);
      })
      .catch(() => {
        if (
          !controller.signal.aborted &&
          generation === projectGeneration.current
        )
          setError(true);
      });
    return () => controller.abort();
  }, [project, token]);

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
    const generation = projectGeneration.current;
    try {
      const response = await fetch(
        `/api/v1/document?${new URLSearchParams({ doc_id: path, project })}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      const document = (await response.json()) as Document;
      if (generation !== projectGeneration.current) return;
      setShowRawDoc(false);
      setProposalStatus("");
      setReadingDoc(document);
      setSelected(null);
      setError(false);
    } catch {
      if (generation === projectGeneration.current) setError(true);
    }
  }

  async function openLinkedDocument(ref: string) {
    const generation = projectGeneration.current;
    try {
      const response = await fetch(
        `/api/v1/document?${new URLSearchParams({ doc_id: ref, project })}`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error();
      const document = (await response.json()) as Document;
      if (generation !== projectGeneration.current) return;
      setLinkedDocument(document);
      setError(false);
    } catch {
      if (generation === projectGeneration.current) setError(true);
    }
  }

  async function selectTableByName(name: string) {
    const found = tables.find(
      (row) =>
        `${row.database}.${row.name}` === name ||
        (row.catalog && `${row.catalog}.${row.database}.${row.name}` === name),
    );
    if (found) {
      setSelected(found);
      setReadingDoc(null);
      return;
    }
    const parts = name.split(".");
    if (parts.length < 2) return;
    const databaseName = parts.length >= 3 ? parts[1] : parts[0];
    const tableName = parts.length >= 3 ? parts.slice(2).join(".") : parts[1];
    const generation = projectGeneration.current;
    try {
      const row = await loadTableDetail(
        token,
        databaseName,
        tableName,
        project,
      );
      if (generation !== projectGeneration.current) return;
      setSelected(summaryFromDetail(row));
      setReadingDoc(null);
      setError(false);
    } catch {
      if (generation === projectGeneration.current) setError(true);
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
    const grouped = new Map<LayerId, Map<string, TableSummary[]>>();
    for (const row of visibleTables) {
      const layer = warehouseLayer(row.database);
      const databases = grouped.get(layer) ?? new Map<string, TableSummary[]>();
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
      ...(selectedRef
        ? [
            "group:tables",
            `layer:${selectedLayer}`,
            `database:${selectedDatabase}`,
          ]
        : []),
    ],
    [selectedDatabase, selectedLayer, selectedRef],
  );

  useEffect(() => {
    if (!selectedRef || !warehouse) return;
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
  }, [selectedDatabase, selectedLayer, selectedRef, warehouse]);

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
          ...(warehouse
            ? [
                "group:tables",
                ...tableLayers.map((layer) => `layer:${layer.id}`),
              ]
            : ["group:docs"]),
        ]),
      ),
    );
  }, [needle, tableLayers, warehouse]);

  useEffect(() => {
    if (needle) {
      const keys = warehouse ? ["group:tables", "group:docs"] : ["group:docs"];
      if (warehouse) {
        for (const layer of tableLayers) {
          keys.push(`layer:${layer.id}`);
          for (const [databaseName] of layer.databases)
            keys.push(`database:${databaseName}`);
        }
      }
      for (const category of visibleCategories) keys.push(category.ref);
      setExpandedKeys(keys);
    } else if (previousSearch.current) {
      setExpandedKeys(defaultExpandedKeys);
    }
    previousSearch.current = needle;
  }, [defaultExpandedKeys, needle, tableLayers, visibleCategories, warehouse]);

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
            aria-label={safeDecodePath(document.path)}
            onClick={() => void openDocument(document.ref)}
          >
            {safeDecodePath(document.path)}
          </button>
        ),
      })),
    }));
    const groups: TreeDataNode[] = [
      {
        key: "group:docs",
        title: `${messages.knowledgeDocuments} (${visibleCategories.reduce(
          (sum, category) => sum + category.documents.length,
          0,
        )})`,
        children: docChildren,
      },
    ];
    if (warehouse) {
      groups.push({
        key: "group:tables",
        title: `${messages.dataTables} (${visibleTables.length})`,
        children: tableChildren,
      });
    }
    return groups;
  }, [
    messages,
    tableLayers,
    tableLimits,
    visibleCategories,
    visibleTables.length,
    warehouse,
  ]);

  return (
    <>
      {error && <Alert type="error" title={messages.error} />}
      {exportError && <Alert type="error" title={exportError} />}
      <div className="knowledge-layout admin-page">
        <aside className="knowledge-tree-panel">
          <div className="knowledge-tree-head">
            <ProjectSelector
              token={token}
              value={project}
              onChange={setProject}
              messages={messages}
              includeMemberProjects
            />
            <Input
              allowClear
              aria-label={messages.searchKnowledge}
              placeholder={messages.searchKnowledge}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {warehouse && (hasAnyTables || tables.length > 0) && (
              <Button
                size="small"
                loading={exportingOssie}
                onClick={() => {
                  setExportError("");
                  setExportingOssie(true);
                  void downloadOssieModel(token, project, "yaml")
                    .catch((err: unknown) => {
                      setExportError(
                        exportErrorMessage(err, messages.exportOssieFailed),
                      );
                    })
                    .finally(() => setExportingOssie(false));
                }}
              >
                {messages.exportOssie}
              </Button>
            )}
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
                    {selected.catalog ? `${selected.catalog}.` : ""}
                    {selected.database}.{selected.name}
                  </h1>
                  <p className="detail-subtitle">
                    {selected.comment || messages.noDescription}
                  </p>
                </div>
                <Button
                  onClick={() =>
                    setCorrectionTarget({
                      target: selected.ref,
                      label: `${selected.database}.${selected.name}`,
                      category: "metadata",
                    })
                  }
                >
                  {messages.proposeCorrection}
                </Button>
              </div>
              {proposalStatus && (
                <Alert
                  type={proposalStatus === messages.error ? "error" : "success"}
                  title={proposalStatus}
                />
              )}
              {detailError && (
                <Alert
                  type="error"
                  title={messages.tableDetailError}
                  action={
                    <Button
                      size="small"
                      onClick={() => setDetailRetry((value) => value + 1)}
                    >
                      {messages.retry}
                    </Button>
                  }
                />
              )}
              {detailLoading && !detail && (
                <p className="muted">{messages.loadingTable}</p>
              )}
              {detail && (
                <>
                  <section className="detail-section">
                    <h2 className="detail-section-title">
                      {messages.fields}
                      <span className="count">{detail.fields.length}</span>
                    </h2>
                    <div
                      className="knowledge-table-scroll"
                      tabIndex={0}
                      role="region"
                      aria-label={messages.fields}
                    >
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th className="field-name">{messages.field}</th>
                            <th className="field-type">{messages.dataType}</th>
                            <th className="field-comment">
                              {messages.comment}
                            </th>
                            <th className="field-dimension">
                              {messages.dimStatus}
                            </th>
                            <th className="field-partition">
                              {messages.partition}
                            </th>
                            <th className="field-actions">
                              {messages.actions}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.fields.map((field) => (
                            <Fragment key={field.name}>
                              <tr>
                                <td className="field-name">
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
                                <td className="field-type">{field.type}</td>
                                <td className="field-comment">
                                  {field.comment}
                                </td>
                                <td className="field-dimension">
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
                                <td className="field-partition">
                                  {field.partition ? messages.yes : messages.no}
                                </td>
                                <td className="field-actions">
                                  <Button
                                    size="small"
                                    aria-label={`${messages.proposeCorrection} ${field.name}`}
                                    onClick={() =>
                                      setCorrectionTarget({
                                        target: `field:${selected.catalog ? `${selected.catalog}.` : ""}${selected.database}.${selected.name}.${field.name}`,
                                        label: `${selected.database}.${selected.name}.${field.name}`,
                                        category: "metadata",
                                      })
                                    }
                                  >
                                    {messages.proposeCorrectionShort}
                                  </Button>
                                </td>
                              </tr>
                              {expandedColumn ===
                                `${selected.database}.${selected.name}.${field.name}` && (
                                <tr>
                                  <td colSpan={6}>
                                    <ColumnLineage
                                      table={`${selected.database}.${selected.name}`}
                                      column={field.name}
                                      token={token}
                                      project={project}
                                    />
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                  {(detail.relationships ?? []).length > 0 && (
                    <section className="detail-section">
                      <h2 className="detail-section-title">
                        {messages.foreignKeys}
                        <span className="count">
                          {(detail.relationships ?? []).length}
                        </span>
                      </h2>
                      <ul className="fk-relationship-list">
                        {(detail.relationships ?? []).map((rel) => {
                          const twoPart = `${selected.database}.${selected.name}`;
                          const fullName = selected.catalog
                            ? `${selected.catalog}.${twoPart}`
                            : twoPart;
                          const isOutgoing =
                            rel.from_table === fullName ||
                            rel.from_table === twoPart;
                          const peer = isOutgoing
                            ? rel.to_table
                            : rel.from_table;
                          return (
                            <li key={rel.ref}>
                              <code>{rel.name}</code>:{" "}
                              <span>
                                {rel.from_table} ({rel.from_columns.join(", ")})
                                {" → "}
                                {rel.to_table} ({rel.to_columns.join(", ")})
                              </span>{" "}
                              <Button
                                type="link"
                                size="small"
                                onClick={() => void selectTableByName(peer)}
                              >
                                {peer}
                              </Button>{" "}
                              <Button
                                size="small"
                                aria-label={`${messages.proposeCorrection} ${rel.name}`}
                                onClick={() =>
                                  setCorrectionTarget({
                                    target: rel.ref,
                                    label: rel.name,
                                    category: "relationship",
                                  })
                                }
                              >
                                {messages.proposeCorrectionShort}
                              </Button>
                            </li>
                          );
                        })}
                      </ul>
                    </section>
                  )}
                  <section className="detail-section">
                    <h2 className="detail-section-title">
                      {messages.contextLineage}
                    </h2>
                    <TaskLineage
                      table={`${selected.database}.${selected.name}`}
                      token={token}
                      project={project}
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
                        {(detail.documents ?? []).length}
                      </span>
                    </h2>
                    {(detail.documents ?? []).length === 0 ? (
                      <p className="muted">{messages.noLinkedDocuments}</p>
                    ) : (
                      <div className="doc-link-list">
                        {(detail.documents ?? []).map((doc) => (
                          <Button
                            type="link"
                            key={doc.ref}
                            onClick={() => void openLinkedDocument(doc.ref)}
                          >
                            {safeDecodePath(doc.path)}
                          </Button>
                        ))}
                      </div>
                    )}
                    {linkedDocument && (
                      <div className="doc-content">
                        <div className="context-ref">
                          <span>{safeDecodePath(linkedDocument.path)}</span>
                          {(linkedDocument.generated ||
                            (detail.documents ?? []).find(
                              (item) => item.ref === linkedDocument.ref,
                            )?.generated) && (
                            <Tag>{messages.generatedReadOnly}</Tag>
                          )}
                          <Button
                            size="small"
                            aria-label={`${messages.proposeCorrection} ${safeDecodePath(linkedDocument.path)}`}
                            onClick={() => {
                              const isGen = Boolean(
                                linkedDocument.generated ||
                                (detail.documents ?? []).find(
                                  (item) => item.ref === linkedDocument.ref,
                                )?.generated,
                              );
                              setCorrectionTarget({
                                target:
                                  linkedDocument.ref ||
                                  `document:${linkedDocument.path}`,
                                label: safeDecodePath(linkedDocument.path),
                                sourceGenerated: isGen,
                                category: isGen ? "metadata" : "caliber",
                              });
                            }}
                          >
                            {messages.proposeCorrectionShort}
                          </Button>
                        </div>
                        <Markdown text={linkedDocument.content} />
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
              )}
            </>
          ) : readingDoc ? (
            <>
              <div className="detail-header">
                <div>
                  <h1 className="detail-title">
                    {safeDecodePath(readingDoc.path)}
                  </h1>
                  <p className="detail-subtitle">
                    {readingDoc.category}
                    {readingDoc.generated && (
                      <>
                        {" "}
                        <Tag>{messages.generatedReadOnly}</Tag>
                      </>
                    )}
                  </p>
                </div>
                <div className="page-actions">
                  <Button
                    size="small"
                    onClick={() => setShowRawDoc((value) => !value)}
                  >
                    {showRawDoc ? messages.readingView : messages.rawLineView}
                  </Button>
                  <Button
                    onClick={() =>
                      setCorrectionTarget({
                        target: readingDoc.ref || `document:${readingDoc.path}`,
                        label: safeDecodePath(readingDoc.path),
                        sourceGenerated: Boolean(readingDoc.generated),
                        category: readingDoc.generated ? "metadata" : "caliber",
                      })
                    }
                  >
                    {readingDoc.generated
                      ? messages.proposeOverrideCorrection
                      : messages.proposeCorrection}
                  </Button>
                </div>
              </div>
              {proposalStatus && (
                <Alert
                  type={proposalStatus === messages.error ? "error" : "success"}
                  title={proposalStatus}
                />
              )}
              <div className="doc-content">
                {showRawDoc ? (
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
                ) : (
                  <Markdown text={readingDoc.content} />
                )}
              </div>
            </>
          ) : (
            <div className="empty-state">{messages.noSelection}</div>
          )}
        </main>
      </div>
      <CorrectionDraftDrawer
        open={correctionTarget !== null}
        token={token}
        project={project}
        messages={messages}
        target={correctionTarget?.target}
        targetLabel={correctionTarget?.label}
        sourceGenerated={Boolean(correctionTarget?.sourceGenerated)}
        defaultCategory={correctionTarget?.category || "caliber"}
        allowDirectEdit={
          !correctionTarget?.sourceGenerated &&
          !correctionTarget?.target.startsWith("relationship:")
        }
        onClose={() => setCorrectionTarget(null)}
        onSubmitted={() => setProposalStatus(messages.correctionSubmitted)}
      />
    </>
  );
}
