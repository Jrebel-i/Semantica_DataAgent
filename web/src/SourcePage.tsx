import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Switch, Tag, Upload } from "antd";
import type { components } from "./api";
import { DiffViewer } from "./DiffViewer";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import { ProjectSelector, storedProject, useProjects } from "./ProjectSelector";

type Version = components["schemas"]["SourceHistory"];
type SourceStatus = components["schemas"]["ProjectSourceStatus"];
type AutoBuildView = components["schemas"]["AutoBuildView"];
type Tree = { directories: Record<string, Tree>; files: string[] };
type Category = { name: string };
type Preview = {
  category: string;
  target: string;
  added: string[];
  overwritten: string[];
  unchanged: string[];
  deleted: string[];
  counts: {
    added: number;
    overwritten: number;
    unchanged: number;
    deleted: number;
  };
  truncated: boolean;
};

const statusColors: Record<SourceStatus["state"], string> = {
  pending: "gold",
  building: "blue",
  effective: "green",
  failed: "red",
  unknown: "default",
};
function statusLabel(status: SourceStatus, t: Messages) {
  const seconds = Math.max(0, Math.ceil(status.due_in_seconds ?? 0));
  switch (status.state) {
    case "pending":
      return status.due_in_seconds === null
        ? t.sourceStatusPendingNoTime
        : t.sourceStatusPending.replace("{seconds}", String(seconds));
    case "building":
      return t.sourceStatusBuilding;
    case "effective":
      return t.sourceStatusEffective;
    case "failed":
      return status.error
        ? t.sourceStatusFailed.replace("{reason}", status.error)
        : t.sourceStatusFailedNoReason;
    default:
      return t.sourceStatusUnknown;
  }
}
function tree(paths: string[]): Tree {
  const root: Tree = { directories: {}, files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    const name = parts.pop()!;
    let node = root;
    for (const part of parts)
      node = node.directories[part] ??= { directories: {}, files: [] };
    node.files.push(name);
  }
  return root;
}
function FileTree({
  node,
  prefix,
  defaultOpen,
  open,
}: {
  node: Tree;
  prefix: string;
  defaultOpen: boolean;
  open: (path: string) => void;
}) {
  return (
    <ul>
      {Object.entries(node.directories).map(([name, child]) => (
        <li key={name}>
          <details open={defaultOpen}>
            <summary>{name}</summary>
            <FileTree
              node={child}
              prefix={`${prefix}${name}/`}
              defaultOpen={defaultOpen}
              open={open}
            />
          </details>
        </li>
      ))}
      {node.files.map((name) => (
        <li key={name}>
          <button type="button" onClick={() => open(prefix + name)}>
            {name}
          </button>
        </li>
      ))}
    </ul>
  );
}
function FilePicker({
  label,
  files,
  emptyLabel,
  countLabel,
  chooseLabel,
  directory = false,
  onChange,
}: {
  label: string;
  files: File[];
  emptyLabel: string;
  countLabel: string;
  chooseLabel: string;
  directory?: boolean;
  onChange: (files: File[]) => void;
}) {
  const id = directory ? "source-folder-picker" : "source-file-picker";
  const value = files.length
    ? files.length === 1
      ? files[0].name
      : `${files.length} ${countLabel}`
    : emptyLabel;
  return (
    <Upload
      id={id}
      className="file-picker"
      aria-label={label}
      multiple
      directory={directory}
      showUploadList={false}
      beforeUpload={(_file, selected) => {
        onChange(selected);
        return false;
      }}
    >
      <span className="file-picker-button">{chooseLabel}</span>
      <span
        className={`file-picker-value${files.length ? "" : " is-empty"}`}
        title={
          files.length > 1
            ? files.map((file) => file.name).join(", ")
            : undefined
        }
      >
        {value}
      </span>
    </Upload>
  );
}
function encoded(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.readAsDataURL(file);
  });
}
export function SourcePage({
  token,
  messages: t,
  onNavigateBuilds,
}: {
  token: string;
  messages: Messages;
  onNavigateBuilds?: () => void;
}) {
  const params = new URLSearchParams(window.location.search);
  const [project, setProject] = useState(
    () => params.get("project") || storedProject(),
  );
  if (params.has("gap")) {
    return (
      <GapSourcePage
        project={params.get("project") || ""}
        gapId={params.get("gap") || ""}
        token={token}
        messages={t}
      />
    );
  }
  return (
    <ProjectSourcePage
      key={project}
      project={project}
      setProject={setProject}
      token={token}
      messages={t}
      onNavigateBuilds={onNavigateBuilds}
    />
  );
}

function GapSourcePage({
  project,
  gapId,
  token,
  messages: t,
}: {
  project: string;
  gapId: string;
  token: string;
  messages: Messages;
}) {
  const { projects, loaded } = useProjects(token);
  const [validGap, setValidGap] = useState<boolean | null>(null);
  useEffect(() => {
    let active = true;
    setValidGap(null);
    if (!project || !gapId) {
      setValidGap(false);
      return;
    }
    void fetch(
      `/api/v1/knowledge-gaps?project=${encodeURIComponent(project)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
      .then((response) => (response.ok ? response.json() : Promise.reject()))
      .then((rows: { id: string; status: string }[]) => {
        if (active)
          setValidGap(
            rows.some((row) => row.id === gapId && row.status === "confirmed"),
          );
      })
      .catch(() => {
        if (active) setValidGap(false);
      });
    return () => {
      active = false;
    };
  }, [project, gapId, token]);
  if (!loaded) return null;
  if (
    !project ||
    !gapId ||
    validGap === false ||
    !projects.some(
      (p) => p.id === project && p.status === "active" && p.my_role === "admin",
    )
  ) {
    return <Alert type="error" title={t.gapSourceUnavailable} />;
  }
  if (validGap === null) return null;
  return (
    <ProjectSourcePage
      key={project}
      project={project}
      setProject={() => {}}
      token={token}
      messages={t}
      appendOnly
      gapId={gapId}
    />
  );
}

function ProjectSourcePage({
  project,
  setProject,
  token,
  messages: t,
  onNavigateBuilds,
  appendOnly = false,
  gapId,
}: {
  project: string;
  setProject: (project: string) => void;
  token: string;
  messages: Messages;
  onNavigateBuilds?: () => void;
  appendOnly?: boolean;
  gapId?: string;
}) {
  const [paths, setPaths] = useState<string[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [uploadMode, setUploadMode] = useState<"category" | "target">(
    "category",
  );
  const [targetDir, setTargetDir] = useState("ossie");
  const [category, setCategory] = useState("");
  const [newCategory, setNewCategory] = useState("");
  const [subfolder, setSubfolder] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [replace, setReplace] = useState(false);
  const [fileFilter, setFileFilter] = useState("");
  const [treeExpand, setTreeExpand] = useState({ open: true, seq: 0 });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [path, setPath] = useState("");
  const [content, setContent] = useState("");
  const [binary, setBinary] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [before, setBefore] = useState("");
  const [after, setAfter] = useState("");
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [buildNotice, setBuildNotice] = useState("");
  const [status, setStatus] = useState<SourceStatus | null>(null);
  const [autoBuild, setAutoBuild] = useState<boolean | null>(null);
  const [statusRefresh, setStatusRefresh] = useState(0);
  const [projectUnavailable, setProjectUnavailable] = useState(false);
  const unavailable = useRef(false);
  const previewVersion = useRef(0);
  const approvedPreview = useRef<{
    version: number;
    project: string;
    body: string;
  } | null>(null);
  const invalidatePreview = () => {
    previewVersion.current++;
    approvedPreview.current = null;
    setPreview(null);
  };
  const request = useCallback(
    async (url: string, init?: RequestInit) => {
      const separator = url.includes("?") ? "&" : "?";
      const response = await fetch(
        `${url}${separator}project=${encodeURIComponent(project)}`,
        {
          ...init,
          headers: {
            Authorization: `Bearer ${token}`,
            ...(init?.headers ?? {}),
          },
        },
      );
      if (!response.ok) {
        if (appendOnly && [401, 403, 404, 409].includes(response.status)) {
          unavailable.current = true;
          previewVersion.current++;
          approvedPreview.current = null;
          setProjectUnavailable(true);
        }
        throw new Error();
      }
      return response.json();
    },
    [project, token, appendOnly],
  );
  const refresh = useCallback(async () => {
    setPaths((await request("/api/v1/source/files")) as string[]);
  }, [request]);
  const loadCategories = useCallback(async () => {
    setCategories((await request("/api/v1/categories")) as Category[]);
  }, [request]);
  useEffect(() => {
    void refresh().catch(() => setError(t.sourceError));
  }, [refresh, t.sourceError]);
  useEffect(() => {
    void loadCategories().catch(() => setCategories([]));
  }, [loadCategories]);
  useEffect(
    () => () => {
      previewVersion.current++;
      approvedPreview.current = null;
    },
    [],
  );
  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    async function poll() {
      try {
        const next = (await request(
          `/api/v1/projects/${encodeURIComponent(project)}/source-status`,
        )) as SourceStatus;
        if (!active) return;
        setStatus(next);
        const wait =
          next.state === "pending" || next.state === "building" ? 3000 : 30000;
        timer = window.setTimeout(() => void poll(), wait);
      } catch {
        if (!active) return;
        setStatus(null);
        timer = window.setTimeout(() => void poll(), 30000);
      }
    }
    void poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [project, request, statusRefresh]);
  useEffect(() => {
    let active = true;
    void request(`/api/v1/projects/${encodeURIComponent(project)}/auto-build`)
      .then((value) => {
        if (active) setAutoBuild(Boolean((value as AutoBuildView).enabled));
      })
      .catch(() => {
        if (active) setAutoBuild(null);
      });
    return () => {
      active = false;
    };
  }, [project, request, statusRefresh]);
  const selectedCategory = newCategory.trim() || category;
  const canPreviewUpload = Boolean(
    files.length &&
    (uploadMode === "target" ? targetDir.trim() : selectedCategory),
  );
  function chooseFiles(next: File[]) {
    invalidatePreview();
    setFiles(next);
    if (!next.length || uploadMode !== "category") return;
    const root = next[0].webkitRelativePath?.split("/")[0];
    if (root) {
      setNewCategory(root);
      setCategory("");
    }
  }
  async function body(): Promise<Record<string, unknown>> {
    const archive =
      files.length === 1 && files[0].name.toLowerCase().endsWith(".zip");
    const values = archive
      ? { archive_base64: await encoded(files[0]) }
      : {
          files: await Promise.all(
            files.map(async (file) => ({
              path: file.webkitRelativePath
                ? file.webkitRelativePath.split("/").slice(1).join("/")
                : file.name,
              content_base64: await encoded(file),
            })),
          ),
        };
    if (uploadMode === "target") {
      return {
        target: targetDir.trim(),
        replace,
        ...(appendOnly ? { append_only: true, knowledge_gap_id: gapId } : {}),
        ...values,
      };
    }
    return {
      category: selectedCategory,
      ...(subfolder.trim() ? { subfolder: subfolder.trim() } : {}),
      replace,
      ...(appendOnly ? { append_only: true, knowledge_gap_id: gapId } : {}),
      ...values,
    };
  }
  async function loadPreview() {
    if (!canPreviewUpload) return;
    invalidatePreview();
    const version = previewVersion.current;
    setBusy(true);
    try {
      const requestBody = JSON.stringify(await body());
      if (version !== previewVersion.current || unavailable.current) return;
      const result = (await request("/api/v1/source/upload/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: requestBody,
      })) as Preview;
      if (version !== previewVersion.current || unavailable.current) return;
      approvedPreview.current = { version, project, body: requestBody };
      setPreview(result);
      setError("");
    } catch {
      setError(t.sourceError);
    } finally {
      setBusy(false);
    }
  }
  async function triggerQuickBuild() {
    setBusy(true);
    try {
      await request("/api/v1/builds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ build_type: "incremental" }),
      });
      setError("");
      setBuildNotice(t.sourceBuildQueued);
      setStatusRefresh((value) => value + 1);
    } catch {
      setBuildNotice("");
      setError(t.sourceError);
    } finally {
      setBusy(false);
    }
  }
  async function send() {
    try {
      const approved = approvedPreview.current;
      if (
        !preview ||
        !approved ||
        unavailable.current ||
        approved.version !== previewVersion.current ||
        approved.project !== project
      )
        throw new Error();
      setBusy(true);
      const result = (await request("/api/v1/source/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: approved.body,
      })) as { commit: string };
      setError("");
      setSuccess(result.commit);
      setPreview(null);
      approvedPreview.current = null;
      await refresh();
      setStatusRefresh((value) => value + 1);
    } catch {
      setError(t.sourceError);
    } finally {
      setBusy(false);
    }
  }
  async function open(pathname: string) {
    try {
      const query = new URLSearchParams({ path: pathname });
      const [file, history] = (await Promise.all([
        request(`/api/v1/source/file?${query}`),
        request(`/api/v1/source/history?${query}`),
      ])) as [{ content: string; encoding: string }, Version[]];
      setPath(pathname);
      setContent(file.content);
      setBinary(file.encoding === "base64");
      setVersions(history);
      setBefore(history[1]?.commit ?? history[0]?.commit ?? "");
      setAfter(history[0]?.commit ?? "");
      setDiff("");
      setError("");
    } catch {
      setError(t.sourceError);
    }
  }
  async function compare() {
    try {
      const result = (await request(
        `/api/v1/source/diff?${new URLSearchParams({ path, before, after })}`,
      )) as { diff: string };
      setDiff(result.diff);
      setError("");
    } catch {
      setError(t.sourceError);
    }
  }
  const trimmedFilter = fileFilter.trim().toLowerCase();
  const visiblePaths = trimmedFilter
    ? paths.filter((p) => p.toLowerCase().includes(trimmedFilter))
    : paths;
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.source}</h1>
          <p className="page-subtitle">{t.sourceHint}</p>
        </div>
        <div className="page-actions source-status-actions">
          {status && (
            <Tag
              color={statusColors[status.state]}
              title={statusLabel(status, t)}
            >
              {statusLabel(status, t)}
            </Tag>
          )}
          {autoBuild !== null && (
            <label className="source-auto-build">
              <span className="muted">{t.autoBuild}</span>
              <Switch
                aria-label={t.autoBuild}
                checked={autoBuild}
                onChange={async (enabled) => {
                  try {
                    const saved = (await request(
                      `/api/v1/projects/${encodeURIComponent(project)}/auto-build`,
                      {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled }),
                      },
                    )) as AutoBuildView;
                    setAutoBuild(Boolean(saved.enabled));
                    setStatusRefresh((value) => value + 1);
                  } catch {
                    setError(t.sourceError);
                  }
                }}
              />
            </label>
          )}
          <Button
            autoInsertSpace={false}
            disabled={busy}
            onClick={() => void triggerQuickBuild()}
          >
            {t.sourceQuickBuild}
          </Button>
          {onNavigateBuilds && (
            <Button autoInsertSpace={false} onClick={onNavigateBuilds}>
              {t.sourceViewBuilds}
            </Button>
          )}
          {!appendOnly && (
            <ProjectSelector
              token={token}
              value={project}
              onChange={setProject}
              messages={t}
            />
          )}
        </div>
      </div>
      {projectUnavailable && (
        <Alert type="error" title={t.gapSourceUnavailable} />
      )}
      {error && <Alert type="error" title={error} />}
      {buildNotice && <Alert type="success" title={buildNotice} />}
      {success && <Alert type="success" title={`${t.uploaded}: ${success}`} />}
      <div className="source-layout">
        <div className="source-stack">
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.sourceUpload}</h2>
              <div className="filter-pills">
                <Button
                  size="small"
                  type={uploadMode === "category" ? "primary" : "default"}
                  onClick={() => {
                    invalidatePreview();
                    setUploadMode("category");
                  }}
                >
                  {t.sourceModeCategory}
                </Button>
                <Button
                  size="small"
                  type={uploadMode === "target" ? "primary" : "default"}
                  onClick={() => {
                    invalidatePreview();
                    setUploadMode("target");
                  }}
                >
                  {t.sourceModeTarget}
                </Button>
              </div>
            </div>
            <div className="stack">
              <div className="field">
                <span className="field-label">{t.sourceFiles}</span>
                <FilePicker
                  label={t.sourceFiles}
                  files={files}
                  emptyLabel={t.sourceNoFile}
                  countLabel={t.sourceFileCount}
                  chooseLabel={t.sourceChoose}
                  onChange={chooseFiles}
                />
              </div>
              <div className="field">
                <span className="field-label">{t.sourceFolder}</span>
                <FilePicker
                  label={t.sourceFolder}
                  files={files}
                  emptyLabel={t.sourceNoFile}
                  countLabel={t.sourceFileCount}
                  chooseLabel={t.sourceChoose}
                  directory
                  onChange={chooseFiles}
                />
              </div>
              {uploadMode === "category" ? (
                <>
                  <label className="field">
                    <span className="field-label">{t.sourceCategory}</span>
                    <select
                      aria-label={t.sourceCategory}
                      value={category}
                      onChange={(e) => {
                        invalidatePreview();
                        setCategory(e.target.value);
                        setNewCategory("");
                      }}
                    >
                      <option value="">{t.sourceCategoryPlaceholder}</option>
                      {categories.map((item) => (
                        <option value={item.name} key={item.name}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span className="field-label">{t.sourceNewCategory}</span>
                    <input
                      aria-label={t.sourceNewCategory}
                      placeholder={t.sourceNewCategoryPlaceholder}
                      value={newCategory}
                      onChange={(e) => {
                        invalidatePreview();
                        setNewCategory(e.target.value);
                        setCategory("");
                      }}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">{t.sourceSubfolder}</span>
                    <input
                      aria-label={t.sourceSubfolder}
                      placeholder={t.sourceSubfolderPlaceholder}
                      value={subfolder}
                      onChange={(e) => {
                        invalidatePreview();
                        setSubfolder(e.target.value);
                      }}
                    />
                  </label>
                </>
              ) : (
                <div className="stack">
                  <div className="filter-pills">
                    {(["ossie", "canonical"] as const).map((preset) => (
                      <Button
                        key={preset}
                        size="small"
                        type={targetDir === preset ? "primary" : "default"}
                        onClick={() => {
                          invalidatePreview();
                          setTargetDir(preset);
                        }}
                      >
                        {preset}/
                      </Button>
                    ))}
                  </div>
                  <label className="field">
                    <span className="field-label">{t.sourceTargetDir}</span>
                    <input
                      aria-label={t.sourceTargetDir}
                      placeholder={t.sourceTargetDirPlaceholder}
                      value={targetDir}
                      onChange={(e) => {
                        invalidatePreview();
                        setTargetDir(e.target.value);
                      }}
                    />
                  </label>
                </div>
              )}
              {!appendOnly && (
                <details className="advanced-options">
                  <summary>{t.sourceAdvanced}</summary>
                  <Checkbox
                    className="plugin-enable"
                    checked={replace}
                    onChange={(e) => {
                      invalidatePreview();
                      setReplace(e.target.checked);
                    }}
                  >
                    {t.sourceReplace}
                  </Checkbox>
                </details>
              )}
              <p className="field-hint">{t.sourceIndexHint}</p>
              {preview && (
                <div className="upload-preview">
                  <p className="preview-counts">
                    <span className="preview-added">
                      {t.sourcePreviewAdded.replace(
                        "{count}",
                        String(preview.counts.added),
                      )}
                    </span>{" "}
                    <span className="preview-overwritten">
                      {t.sourcePreviewOverwritten.replace(
                        "{count}",
                        String(preview.counts.overwritten),
                      )}
                    </span>{" "}
                    <span className="muted">
                      {t.sourcePreviewUnchanged.replace(
                        "{count}",
                        String(preview.counts.unchanged),
                      )}
                    </span>
                  </p>
                  {preview.counts.deleted > 0 && (
                    <Alert
                      type="error"
                      title={t.sourcePreviewDeleted.replace(
                        "{count}",
                        String(preview.counts.deleted),
                      )}
                    />
                  )}
                  <details>
                    <summary>{t.sourcePreviewPaths}</summary>
                    {(
                      [
                        ["added", t.sourcePreviewAddedTitle],
                        ["overwritten", t.sourcePreviewOverwrittenTitle],
                        ["unchanged", t.sourcePreviewUnchangedTitle],
                        ["deleted", t.sourcePreviewDeletedTitle],
                      ] as const
                    ).map(([key, label]) =>
                      preview[key].length ? (
                        <div key={key}>
                          <h4 className={`preview-${key}`}>{label}</h4>
                          <ul>
                            {preview[key].map((item) => (
                              <li className="mono" key={item}>
                                {item}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null,
                    )}
                  </details>
                </div>
              )}
              <div className="save-bar" style={{ marginTop: 0 }}>
                <button
                  type="button"
                  disabled={busy || projectUnavailable || !canPreviewUpload}
                  onClick={() => void loadPreview()}
                >
                  {t.sourcePreview}
                </button>
                <button
                  type="button"
                  className={replace && preview ? "danger" : undefined}
                  disabled={busy || projectUnavailable || !preview}
                  onClick={() => void send()}
                >
                  {replace && preview
                    ? t.sourceConfirmDelete.replace(
                        "{count}",
                        String(preview.counts.deleted),
                      )
                    : t.sourceConfirm}
                </button>
              </div>
            </div>
          </section>
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.sourceBrowse}</h2>
              <span className="muted">
                {trimmedFilter
                  ? `${visiblePaths.length}/${paths.length}`
                  : paths.length}
              </span>
            </div>
            <div className="stack" style={{ marginBottom: 12 }}>
              <input
                aria-label={t.sourceFilterFiles}
                placeholder={t.sourceFilterFiles}
                value={fileFilter}
                onChange={(e) => setFileFilter(e.target.value)}
              />
              <div className="filter-pills">
                <Button
                  size="small"
                  onClick={() =>
                    setTreeExpand((prev) => ({ open: true, seq: prev.seq + 1 }))
                  }
                >
                  {t.sourceExpandAll}
                </Button>
                <Button
                  size="small"
                  onClick={() =>
                    setTreeExpand((prev) => ({
                      open: false,
                      seq: prev.seq + 1,
                    }))
                  }
                >
                  {t.sourceCollapseAll}
                </Button>
              </div>
            </div>
            <div className="source-tree" key={treeExpand.seq}>
              <FileTree
                node={tree(visiblePaths)}
                prefix=""
                defaultOpen={treeExpand.open}
                open={(p) => void open(p)}
              />
            </div>
          </section>
        </div>
        <div className="source-stack">
          {path ? (
            <section className="card section-card">
              <div className="card-title-row">
                <h2 className="card-title mono">{path}</h2>
              </div>
              {binary ? (
                <a
                  href={`data:application/octet-stream;base64,${content}`}
                  download={path.split("/").pop()}
                >
                  {t.sourceDownload}
                </a>
              ) : (
                <pre className="source-preview">{content}</pre>
              )}
              <h3 className="card-title" style={{ marginTop: 16 }}>
                {t.sourceHistory}
              </h3>
              <ul className="source-history">
                {versions.map((v) => (
                  <li key={v.commit}>
                    {v.commit.slice(0, 12)} {v.author} {v.date} {v.message}
                  </li>
                ))}
              </ul>
              <div className="source-compare" style={{ marginTop: 12 }}>
                <label className="field">
                  <span className="field-label">{t.sourceBefore}</span>
                  <select
                    aria-label={t.sourceBefore}
                    value={before}
                    onChange={(e) => setBefore(e.target.value)}
                  >
                    {versions.map((v) => (
                      <option value={v.commit} key={v.commit}>
                        {v.commit.slice(0, 12)} {v.author}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">{t.sourceAfter}</span>
                  <select
                    aria-label={t.sourceAfter}
                    value={after}
                    onChange={(e) => setAfter(e.target.value)}
                  >
                    {versions.map((v) => (
                      <option value={v.commit} key={v.commit}>
                        {v.commit.slice(0, 12)} {v.author}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  disabled={!before || !after}
                  onClick={() => void compare()}
                >
                  {t.sourceCompare}
                </button>
              </div>
              {diff && (
                <DiffViewer
                  diff={diff}
                  messages={t}
                  title={safeDecodePath(path)}
                  defaultMode="split"
                />
              )}
            </section>
          ) : (
            <div className="empty-state">{t.sourceSelectFile}</div>
          )}
        </div>
      </div>
    </section>
  );
}
