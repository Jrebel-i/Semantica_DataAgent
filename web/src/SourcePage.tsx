import { useCallback, useEffect, useState } from "react";
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
    <section>
      <h2>{t.source}</h2>
      {error && <p role="alert">{error}</p>}
      {success && (
        <p>
          {t.uploaded}: {success}
        </p>
      )}
      <label>
        {t.sourceTarget}{" "}
        <input value={target} onChange={(e) => setTarget(e.target.value)} />
      </label>
      <br />
      <label>
        {t.sourceFiles}{" "}
        <input
          type="file"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
      </label>
      <br />
      <label>
        {t.sourceFolder}{" "}
        <input
          type="file"
          multiple
          {...({ webkitdirectory: "" } as object)}
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
      </label>
      <br />
      <label>
        <input
          type="checkbox"
          checked={replace}
          onChange={(e) => setReplace(e.target.checked)}
        />
        {t.sourceReplace}
      </label>
      <br />
      <button type="button" onClick={() => void send()}>
        {t.sourceUpload}
      </button>
      <h3>{t.sourceBrowse}</h3>
      <FileTree node={tree(paths)} prefix="" open={(p) => void open(p)} />
      {path && (
        <>
          <h3>{path}</h3>
          {binary ? (
            <a
              href={`data:application/octet-stream;base64,${content}`}
              download={path.split("/").pop()}
            >
              {t.sourceDownload}
            </a>
          ) : (
            <pre style={{ whiteSpace: "pre-wrap" }}>{content}</pre>
          )}
          <h3>{t.sourceHistory}</h3>
          <ul>
            {versions.map((v) => (
              <li key={v.commit}>
                {v.commit.slice(0, 12)} {v.author} {v.date} {v.message}
              </li>
            ))}
          </ul>
          <label>
            {t.sourceBefore}
            <select value={before} onChange={(e) => setBefore(e.target.value)}>
              {versions.map((v) => (
                <option value={v.commit} key={v.commit}>
                  {v.commit.slice(0, 12)} {v.author}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t.sourceAfter}
            <select value={after} onChange={(e) => setAfter(e.target.value)}>
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
          <pre>{diff}</pre>
        </>
      )}
    </section>
  );
}
