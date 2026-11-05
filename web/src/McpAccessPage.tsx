import { useState } from "react";
import { Alert, Button, Input, Space, Typography } from "antd";
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
    <section>
      <Typography.Title level={2}>{t.mcpAccess}</Typography.Title>
      <Typography.Paragraph>{t.mcpIntro}</Typography.Paragraph>
      <label>
        {t.mcpUrl}
        <Input
          aria-label={t.mcpUrl}
          value={url}
          onChange={(event) => setUrl(event.target.value)}
        />
      </label>
      <Typography.Paragraph type="secondary">
        {t.mcpUrlHint}
      </Typography.Paragraph>

      <Typography.Title level={4}>{t.currentToken}</Typography.Title>
      <Space wrap>
        <Button onClick={() => setRevealed((value) => !value)}>
          {revealed ? t.hideToken : t.showToken}
        </Button>
        <Button onClick={() => void copy(token, t.tokenCopied)}>
          {t.copyToken}
        </Button>
      </Space>
      <pre aria-label={t.currentToken}>
        <code>{revealed ? token : MASK}</code>
      </pre>

      <Typography.Title level={4}>{t.claudeCommand}</Typography.Title>
      <pre aria-label={t.claudeCommand}>
        <code>{masked(claudeCommand)}</code>
      </pre>
      <Button onClick={() => void copy(claudeCommand, t.claudeCopied)}>
        {t.copyClaude}
      </Button>

      <Typography.Title level={4}>{t.codexConfig}</Typography.Title>
      <pre aria-label={t.codexConfig}>
        <code>{masked(codexConfig)}</code>
      </pre>
      <Button onClick={() => void copy(codexConfig, t.codexCopied)}>
        {t.copyCodex}
      </Button>

      <div>
        <Button onClick={() => void downloadSkill()}>{t.downloadSkill}</Button>
      </div>
      {notice && <Alert type="success" title={notice} />}
      {error && <Alert type="error" title={error} />}
    </section>
  );
}
