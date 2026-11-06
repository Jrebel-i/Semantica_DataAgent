import { useCallback, useEffect, useState } from "react";
import { Alert, Space, Typography, ConfigProvider, Button } from "antd";
import { CorrectionsPage } from "./CorrectionsPage";
import { BuildPage } from "./BuildPage";
import { getMessages, type Language } from "./i18n";
import { ChatPage } from "./ChatPage";
import { KnowledgePage } from "./KnowledgePage";
import { SearchPage } from "./SearchPage";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { PluginsPage } from "./PluginsPage";
import { SourcePage } from "./SourcePage";
import { Login } from "./Login";
import { StatusPage, type Health } from "./StatusPage";
import { TokensPage } from "./TokensPage";
import { PersonalSettingsPage } from "./PersonalSettingsPage";
import { McpAccessPage } from "./McpAccessPage";
import { AgentSettings } from "./AgentSettings";
import { PlatformSettings } from "./PlatformSettings";
import {
  MessageOutlined,
  DatabaseOutlined,
  BuildOutlined,
  SettingOutlined,
  UserOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  LogoutOutlined,
} from "@ant-design/icons";

export function App() {
  const [lang, setLang] = useState<Language>(() =>
    localStorage.getItem("language") === "en" ? "en" : "zh",
  );
  const [token, setToken] = useState(() => localStorage.getItem("token") || "");
  const [loggedIn, setLoggedIn] = useState(false);
  const [role, setRole] = useState("");
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem("navigationExpanded") === "true";
    } catch {
      return false;
    }
  });
  const toggleNavigation = () => {
    setExpanded((value) => {
      try {
        localStorage.setItem("navigationExpanded", String(!value));
      } catch {
        /* Storage may be unavailable. */
      }
      return !value;
    });
  };
  const [path, setPath] = useState(window.location.pathname);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [generation, setGeneration] = useState("");
  const [dtbConfigured, setDtbConfigured] = useState<boolean | null>(null);
  const t = getMessages(lang);
  const authenticate = useCallback(async (value: string) => {
    try {
      const response = await fetch("/api/v1/me", {
        headers: { Authorization: `Bearer ${value}` },
      });
      if (!response.ok) {
        if (response.status === 401) {
          localStorage.removeItem("token");
          setToken("");
          setLoggedIn(false);
        }
        setError("invalid");
        return;
      }
      localStorage.setItem("token", value);
      const user = (await response.json()) as { role: string };
      setRole(user.role);
      setToken(value);
      setLoggedIn(true);
      setError("");
    } catch {
      setError("error");
    }
  }, []);
  useEffect(() => {
    if (token) void authenticate(token);
  }, [authenticate, token]);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/healthz");
      if (!response.ok) throw new Error();
      setHealth((await response.json()) as Health);
      setError("");
    } catch {
      setError("error");
    }
  }, []);
  useEffect(() => {
    if (loggedIn) {
      void refresh();
      const headers = { Authorization: `Bearer ${token}` };
      void fetch("/api/v1/generations", { headers })
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((data: { generations: { name: string; current: boolean }[] }) =>
          setGeneration(
            data.generations.find((row) => row.current)?.name || "",
          ),
        )
        .catch(() => setGeneration(""));
      void fetch("/api/v1/me/dtb-token", { headers })
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((data: { configured: boolean }) =>
          setDtbConfigured(data.configured),
        )
        .catch(() => setDtbConfigured(null));
    }
  }, [loggedIn, refresh, token, path]);
  const changeLanguage = () => {
    const next = lang === "zh" ? "en" : "zh";
    localStorage.setItem("language", next);
    setLang(next);
  };
  const logout = () => {
    localStorage.removeItem("token");
    setToken("");
    setLoggedIn(false);
  };

  const navigate = (next: string) => {
    history.pushState(null, "", next);
    setPath(next);
  };
  const navigation = [
    { path: "/chat", label: t.workbenchChat, icon: <MessageOutlined /> },
    {
      path: "/knowledge",
      label: t.workbenchKnowledge,
      icon: <DatabaseOutlined />,
    },
    ...(role === "admin"
      ? [
          { path: "/builds", label: t.workbenchBuild, icon: <BuildOutlined /> },
          {
            path: "/config",
            label: t.workbenchConfig,
            icon: <SettingOutlined />,
          },
        ]
      : []),
  ];
  const subNavigation = (() => {
    if (path === "/knowledge" || path === "/search")
      return [
        { path: "/knowledge", label: t.knowledge },
        { path: "/search", label: t.searchDebug },
      ];
    if (
      ["/builds", "/source", "/corrections", "/knowledge-gaps"].includes(path)
    )
      return [
        ...(role === "admin"
          ? [
              { path: "/builds", label: t.builds },
              { path: "/source", label: t.source },
              { path: "/corrections", label: t.correctionTitle },
            ]
          : []),
        { path: "/knowledge-gaps", label: t.gapTitle },
      ];
    if (["/config", "/plugins", "/tokens"].includes(path))
      return [
        { path: "/config", label: t.workbenchConfig },
        ...(role === "admin"
          ? [
              { path: "/plugins", label: t.plugins },
              { path: "/tokens", label: t.tokens },
            ]
          : []),
      ];
    if (path === "/personal-settings" || path === "/mcp-access")
      return [
        { path: "/personal-settings", label: t.personalSettings },
        { path: "/mcp-access", label: t.mcpAccess },
      ];
    return [];
  })();
  const shortGeneration =
    generation.length > 26 ? `${generation.slice(0, 24)}…` : generation || "—";
  const isChat = path === "/chat" || path === "/";
  const isFill = path === "/knowledge";
  const content =
    path === "/knowledge-gaps" ? (
      <CorrectionsPage token={token} gaps messages={t} />
    ) : path === "/corrections" ? (
      role === "admin" ? (
        <CorrectionsPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/chat" || path === "/" ? (
      <ChatPage token={token} role={role} messages={t} />
    ) : path === "/builds" ? (
      role === "admin" ? (
        <BuildPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/source" ? (
      role === "admin" ? (
        <SourcePage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/config" ? (
      role === "admin" ? (
        <>
          <AgentSettings token={token} messages={t} />
          <PlatformSettings token={token} messages={t} />
          <ChatPage token={token} role={role} messages={t} settingsOnly />
        </>
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/plugins" ? (
      role === "admin" ? (
        <PluginsPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/tokens" ? (
      role === "admin" ? (
        <TokensPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/personal-settings" ? (
      <PersonalSettingsPage
        token={token}
        messages={t}
        language={lang}
        onChangeLanguage={changeLanguage}
        onChanged={setDtbConfigured}
      />
    ) : path === "/search" ? (
      <SearchPage token={token} messages={t} />
    ) : path === "/mcp-access" ? (
      <McpAccessPage token={token} messages={t} />
    ) : path === "/knowledge" ? (
      <KnowledgePage token={token} messages={t} />
    ) : (
      <StatusPage
        messages={t}
        health={health}
        onRefresh={() => void refresh()}
      />
    );

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#0B6E63",
          colorInfo: "#0B6E63",
          colorLink: "#0B6E63",
          colorText: "#172021",
          colorTextSecondary: "#5C6B69",
          colorBorder: "#D6DEDC",
          colorBgLayout: "#EEF1F0",
          borderRadius: 6,
          controlHeight: 36,
          fontFamily: "IBM Plex Sans, Noto Sans SC, sans-serif",
          fontSize: 14,
        },
        components: {
          Table: { cellPaddingBlock: 8, headerBg: "#F1F4F3" },
          Button: { primaryShadow: "none" },
          Tabs: { itemSelectedColor: "#0B6E63", inkBarColor: "#0B6E63" },
        },
      }}
    >
      <main className="app-shell">
        {error && (
          <Alert
            type="error"
            title={error === "invalid" ? t.invalid : t.error}
          />
        )}
        {!loggedIn ? (
          <div className="login">
            <div className="side-brand">
              semantica<span className="brand-slash">/</span>workbench
            </div>
            <Typography.Title level={2}>{t.title}</Typography.Title>
            <Space wrap>
              <LanguageSwitcher language={lang} onChange={changeLanguage} />
            </Space>
            <Login messages={t} onLogin={(value) => void authenticate(value)} />
          </div>
        ) : (
          <div className="workspace">
            <nav
              className={`side-nav ${expanded ? "expanded" : ""}`}
              aria-label={t.navigation}
            >
              <div className="side-brand" aria-hidden="true">
                {expanded ? (
                  <>
                    semantica<span className="brand-slash">/</span>workbench
                  </>
                ) : (
                  <>
                    S<span className="brand-slash">/</span>W
                  </>
                )}
              </div>
              <Button
                type="text"
                className="nav-toggle"
                aria-label={
                  expanded ? t.collapseNavigation : t.expandNavigation
                }
                title={expanded ? t.collapseNavigation : t.expandNavigation}
                onClick={toggleNavigation}
              >
                {expanded ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />}
              </Button>
              {navigation.map((item) => (
                <a
                  key={item.path}
                  className="nav-item"
                  href={item.path}
                  aria-current={
                    path === item.path ||
                    (path === "/" && item.path === "/chat")
                      ? "page"
                      : undefined
                  }
                  title={item.label}
                  onClick={(event) => {
                    event.preventDefault();
                    navigate(item.path);
                  }}
                >
                  <span aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
                </a>
              ))}
              <div className="personal-menu">
                <a
                  className="nav-item"
                  href="/personal-settings"
                  aria-current={
                    path === "/personal-settings" ? "page" : undefined
                  }
                  title={t.personalSettings}
                  onClick={(event) => {
                    event.preventDefault();
                    navigate("/personal-settings");
                  }}
                >
                  <span aria-hidden="true">
                    <UserOutlined />
                  </span>
                  <span>{t.personalSettings}</span>
                </a>
                <Button
                  type="text"
                  className="nav-item"
                  aria-label={t.logout}
                  title={t.logout}
                  icon={<LogoutOutlined />}
                  onClick={logout}
                >
                  {expanded && t.logout}
                </Button>
              </div>
            </nav>
            <div className="workspace-content">
              <div
                className={`page-content${isChat ? " page-content--chat" : ""}${
                  isFill ? " page-content--fill" : ""
                }`}
              >
                {subNavigation.length > 0 && (
                  <nav className="sub-nav" aria-label={t.navigation}>
                    {subNavigation.map((item) => (
                      <a
                        key={item.path}
                        href={item.path}
                        aria-current={path === item.path ? "page" : undefined}
                        onClick={(event) => {
                          event.preventDefault();
                          navigate(item.path);
                        }}
                      >
                        {item.label}
                      </a>
                    ))}
                  </nav>
                )}
                {isFill ? <div className="page-fill">{content}</div> : content}
              </div>
              <footer className="status-bar">
                {Object.entries(health || {}).map(([name, value]) => (
                  <span className="status-item" key={name}>
                    <span
                      className={`status-dot ${value.status === "up" ? "" : "down"}`}
                    />
                    <span>
                      {name}: {t[value.status]} · {value.elapsed_ms} ms
                    </span>
                  </span>
                ))}
                <span className="status-item" title={generation || undefined}>
                  <span
                    className={`status-dot ${generation ? "" : "unknown"}`}
                  />
                  <span>
                    {t.generationCurrent}:{" "}
                    <span className="mono">{shortGeneration}</span>
                  </span>
                </span>
                <span className="status-item is-muted">
                  <span
                    className={`status-dot ${
                      dtbConfigured === null
                        ? "unknown"
                        : dtbConfigured
                          ? ""
                          : "down"
                    }`}
                  />
                  <span>
                    DTB token:{" "}
                    {dtbConfigured === null
                      ? "—"
                      : dtbConfigured
                        ? t.providerConfigured
                        : t.providerNotConfigured}
                  </span>
                </span>
                <span className="status-spacer" />
                <button onClick={() => void refresh()}>{t.refresh}</button>
              </footer>
            </div>
          </div>
        )}
      </main>
    </ConfigProvider>
  );
}
