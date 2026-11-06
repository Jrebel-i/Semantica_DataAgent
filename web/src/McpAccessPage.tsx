import { useState } from "react";
import { Alert, Button, Input, Space } from "antd";
import type { Messages } from "./i18n";

const SERVER_NAME = "semantica-dataagent";
const MASK = "••••••••";

export function defaultMcpUrl(location = window.location) {
  const directory = location.pathname.endsWith("/")
    ? location.pathname
    : location.pathname.slice(0, location.pathname.lastIndexOf("/") + 1);
  return new URL(`${directory}mcp/`, location.origin).toString();
}

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Plain HTTP deployments may expose the API but deny it; use the textarea fallback.
    }
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    area.remove();
  }
}

export function McpAccessPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [url, setUrl] = useState(defaultMcpUrl);
  const [revealed, setRevealed] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const connectionUrl = url.trim();
  const claudeCommand = `claude mcp add --transport http ${SERVER_NAME} ${connectionUrl} --header "Authorization: Bearer ${token}"`;
  const codexConfig = `[mcp_servers.${SERVER_NAME}]\nurl = "${connectionUrl}"\nhttp_headers = { "Authorization" = "Bearer ${token}" }`;
  const masked = (value: string) =>
    revealed || !token ? value : value.split(token).join(MASK);

  async function copy(value: string, message: string) {
    try {
      await copyText(value);
      setNotice(message);
      setError("");
    } catch {
      setError(t.copyError);
    }
  }

  async function downloadSkill() {
    try {
      const response = await fetch("/api/v1/skill", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const disposition = response.headers.get("Content-Disposition") || "";
      const filename =
        /filename="?([^";]+)"?/i.exec(disposition)?.[1] || "SKILL.md";
      const href = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = href;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
      setError("");
    } catch {
      setError(t.downloadError);
    }
  }

  return (
    <section className="stack">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.mcpAccess}</h1>
          <p className="page-subtitle">{t.mcpIntro}</p>
        </div>
      </div>
      {notice && <Alert type="success" title={notice} />}
      {error && <Alert type="error" title={error} />}
      <div className="mcp-grid">
        <section className="card section-card">
          <div className="field">
            <label className="field-label" htmlFor="mcp-url">
              {t.mcpUrl}
            </label>
            <Input
              id="mcp-url"
              aria-label={t.mcpUrl}
              value={url}
              onChange={(event) => setUrl(event.target.value)}
            />
            <p className="field-hint">{t.mcpUrlHint}</p>
          </div>
        </section>
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.currentToken}</h2>
            <Space wrap>
              <Button onClick={() => setRevealed((value) => !value)}>
                {revealed ? t.hideToken : t.showToken}
              </Button>
              <Button onClick={() => void copy(token, t.tokenCopied)}>
                {t.copyToken}
              </Button>
            </Space>
          </div>
          <div className="code-panel">
            <pre aria-label={t.currentToken}>
              <code>{revealed ? token : MASK}</code>
            </pre>
          </div>
        </section>
      </div>
      <div className="mcp-grid">
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.claudeCommand}</h2>
            <Button onClick={() => void copy(claudeCommand, t.claudeCopied)}>
              {t.copyClaude}
            </Button>
          </div>
          <div className="code-panel">
            <pre aria-label={t.claudeCommand}>
              <code>{masked(claudeCommand)}</code>
            </pre>
          </div>
        </section>
        <section className="card section-card">
          <div className="card-title-row">
            <h2 className="card-title">{t.codexConfig}</h2>
            <Button onClick={() => void copy(codexConfig, t.codexCopied)}>
              {t.copyCodex}
            </Button>
          </div>
          <div className="code-panel">
            <pre aria-label={t.codexConfig}>
              <code>{masked(codexConfig)}</code>
            </pre>
          </div>
        </section>
      </div>
      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.downloadSkill}</h2>
        </div>
        <Button onClick={() => void downloadSkill()}>{t.downloadSkill}</Button>
      </section>
    </section>
  );
}
