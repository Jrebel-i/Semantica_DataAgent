import { useCallback, useEffect, useState } from "react";
import { Alert } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type Version = components["schemas"]["SourceHistory"];
type Tree = { directories: Record<string, Tree>; files: string[] };

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
  open,
}: {
  node: Tree;
  prefix: string;
  open: (path: string) => void;
}) {
  return (
    <ul>
      {Object.entries(node.directories).map(([name, child]) => (
        <li key={name}>
          <details open>
            <summary>{name}</summary>
            <FileTree node={child} prefix={`${prefix}${name}/`} open={open} />
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
  const value = files.length
    ? files.length === 1
      ? files[0].name
      : `${files.length} ${countLabel}`
    : emptyLabel;
  return (
    <label className="file-picker">
      <input
        className="file-picker-input"
        aria-label={label}
        type="file"
        multiple
        {...(directory ? ({ webkitdirectory: "" } as object) : {})}
        onChange={(event) => onChange(Array.from(event.target.files ?? []))}
      />
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
    </label>
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
}: {
  token: string;
  messages: Messages;
}) {
  const [paths, setPaths] = useState<string[]>([]);
  const [target, setTarget] = useState("knowledge/");
  const [files, setFiles] = useState<File[]>([]);
  const [replace, setReplace] = useState(false);
  const [path, setPath] = useState("");
  const [content, setContent] = useState("");
  const [binary, setBinary] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [before, setBefore] = useState("");
  const [after, setAfter] = useState("");
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const request = useCallback(
    async (url: string, init?: RequestInit) => {
      const response = await fetch(url, {
        ...init,
        headers: { Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
      });
      if (!response.ok) throw new Error();
      return response.json();
    },
    [token],
  );
  const refresh = useCallback(async () => {
    setPaths((await request("/api/v1/source/files")) as string[]);
  }, [request]);
  useEffect(() => {
    void refresh().catch(() => setError(t.sourceError));
  }, [refresh, t.sourceError]);
  async function send() {
    try {
      if (!files.length) throw new Error();
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
      const result = (await request("/api/v1/source/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, replace, ...values }),
      })) as { commit: string };
      setError("");
      setSuccess(result.commit);
      await refresh();
    } catch {
      setError(t.sourceError);
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
  return (
    <section className="stack">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.source}</h1>
          <p className="page-subtitle">{t.sourceHint}</p>
        </div>
      </div>
      {error && <Alert type="error" title={error} />}
      {success && <Alert type="success" title={`${t.uploaded}: ${success}`} />}
      <div className="source-layout">
        <div className="source-stack">
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.sourceUpload}</h2>
            </div>
            <div className="stack">
              <label className="field">
                <span className="field-label">{t.sourceTarget}</span>
                <input
                  aria-label={t.sourceTarget}
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                />
              </label>
              <div className="field">
                <span className="field-label">{t.sourceFiles}</span>
                <FilePicker
                  label={t.sourceFiles}
                  files={files}
                  emptyLabel={t.sourceNoFile}
                  countLabel={t.sourceFileCount}
                  chooseLabel={t.sourceChoose}
                  onChange={(next) => setFiles(next)}
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
                  onChange={(next) => setFiles(next)}
                />
              </div>
              <label className="plugin-enable">
                <input
                  type="checkbox"
                  checked={replace}
                  onChange={(e) => setReplace(e.target.checked)}
                />
                {t.sourceReplace}
              </label>
              <div className="save-bar" style={{ marginTop: 0 }}>
                <button type="button" onClick={() => void send()}>
                  {t.sourceUpload}
                </button>
              </div>
            </div>
          </section>
          <section className="card section-card">
            <div className="card-title-row">
              <h2 className="card-title">{t.sourceBrowse}</h2>
              <span className="muted">{paths.length}</span>
            </div>
            <div className="source-tree">
              <FileTree
                node={tree(paths)}
                prefix=""
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
              {diff && <pre className="diff-block">{diff}</pre>}
            </section>
          ) : (
            <div className="empty-state">{t.sourceSelectFile}</div>
          )}
        </div>
      </div>
    </section>
  );
}
