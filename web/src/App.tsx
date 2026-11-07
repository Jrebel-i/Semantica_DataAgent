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
  DatabaseOutlined,
  UserOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  LogoutOutlined,
  SearchOutlined,
} from "@ant-design/icons";

function formatGenerationTime(value: string | null | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export function App() {
  const [lang, setLang] = useState<Language>(() =>
    localStorage.getItem("language") === "en" ? "en" : "zh",
  );
  const [token, setToken] = useState(() => localStorage.getItem("token") || "");
  const [loggedIn, setLoggedIn] = useState(false);
  const [role, setRole] = useState("");
  const [userName, setUserName] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem("navigationExpanded") !== "false";
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
  const [sessionTarget, setSessionTarget] = useState<HTMLDivElement | null>(
    null,
  );
  const [newTarget, setNewTarget] = useState<HTMLDivElement | null>(null);
  const [managementOpen, setManagementOpen] = useState(false);
  const [personalOpen, setPersonalOpen] = useState(false);
  const [path, setPath] = useState(window.location.pathname);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [generation, setGeneration] = useState<{
    name: string;
    createdAt: string | null;
  } | null>(null);
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
      const user = (await response.json()) as {
        role: string;
        username?: string;
        name?: string;
      };
      setUserName(user.username || user.name || user.role);
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
        .then(
          (data: {
            generations: {
              name: string;
              current: boolean;
              created_at?: string | null;
            }[];
          }) => {
            const current = data.generations.find((row) => row.current);
            setGeneration(
              current
                ? { name: current.name, createdAt: current.created_at ?? null }
                : null,
            );
          },
        )
        .catch(() => setGeneration(null));
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
    { path: "/search", label: t.sidebarSearch, icon: <SearchOutlined /> },
    {
      path: "/knowledge",
      label: t.sidebarKnowledge,
      icon: <DatabaseOutlined />,
    },
  ];
  const management = [
    { path: "/builds", label: t.builds },
    { path: "/source", label: t.source },
    { path: "/corrections", label: t.correctionTitle },
    { path: "/config", label: t.workbenchConfig },
    { path: "/plugins", label: t.plugins },
    { path: "/tokens", label: t.tokens },
    { path: "/mcp-access", label: t.mcpAccess },
    { path: "/status", label: t.status },
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
    if (["/config", "/plugins", "/tokens"].includes(path) && role === "admin")
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
  const generationName = generation?.name ?? "";
  const shortGeneration = generationName
    ? generationName.length > 8
      ? `…${generationName.slice(-8)}`
      : generationName
    : "—";
  const generationTime = formatGenerationTime(generation?.createdAt);
  const generationLabel = generationTime
    ? `${shortGeneration} · ${generationTime}`
    : shortGeneration;
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
    ) : path === "/chat" || path === "/" ? null : path === "/builds" ? (
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
          colorPrimary: "#D97757",
          colorInfo: "#D97757",
          colorLink: "#D97757",
          colorText: "#30302E",
          colorTextSecondary: "#6F6B64",
          colorBorder: "#DEDBD2",
          colorBgLayout: "#FAF9F5",
          borderRadius: 10,
          controlHeight: 38,
          fontFamily: "var(--font-sans)",
          fontSize: 14,
        },
        components: {
          Table: { cellPaddingBlock: 8, headerBg: "#F5F3ED" },
          Button: { primaryShadow: "none" },
          Tabs: { itemSelectedColor: "#D97757", inkBarColor: "#D97757" },
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
              <div className="side-heading">
                {expanded && <div className="side-brand">Semantica</div>}
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
              </div>
              <div ref={setNewTarget} />
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
              <div ref={setSessionTarget} className="sidebar-sessions" />
              <div className="personal-menu">
                {personalOpen && (
                  <div className="account-popover">
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
                    {role === "admin" && (
                      <>
                        <Button
                          type="text"
                          className="nav-item management-toggle"
                          aria-label={t.management}
                          aria-expanded={managementOpen}
                          onClick={() => setManagementOpen(!managementOpen)}
                        >
                          {t.management}
                          <span className="management-caret" aria-hidden="true">
                            ›
                          </span>
                        </Button>
                        {managementOpen && (
                          <div className="management-links">
                            {management.map((item) => (
                              <a
                                key={item.path}
                                href={item.path}
                                className="nav-item"
                                aria-current={
                                  path === item.path ? "page" : undefined
                                }
                                onClick={(event) => {
                                  event.preventDefault();
                                  navigate(item.path);
                                  setManagementOpen(false);
                                  setPersonalOpen(false);
                                }}
                              >
                                {item.label}
                              </a>
                            ))}
                          </div>
                        )}
                      </>
                    )}
                    <Button
                      type="text"
                      className="nav-item"
                      onClick={changeLanguage}
                    >
                      {lang === "zh" ? "English" : "中文"}
                    </Button>
                    <Button
                      type="text"
                      className="nav-item"
                      onClick={() => {
                        void refresh();
                        setRefreshKey((value) => value + 1);
                      }}
                    >
                      {t.refresh}
                    </Button>
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
                )}
                <Button
                  type="text"
                  className="nav-item account-toggle"
                  icon={<UserOutlined />}
                  aria-label={t.accountMenu}
                  aria-expanded={personalOpen}
                  onClick={() => {
                    if (!personalOpen) setManagementOpen(false);
                    setPersonalOpen(!personalOpen);
                  }}
                >
                  {expanded && (
                    <span className="account-name">
                      {userName}
                      <small>{role}</small>
                    </span>
                  )}
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
                <div className="chat-page-host" hidden={!isChat}>
                  <ChatPage
                    token={token}
                    role={role}
                    messages={t}
                    language={lang}
                    onChangeLanguage={changeLanguage}
                    refreshKey={refreshKey}
                    sessionTarget={sessionTarget}
                    newTarget={newTarget}
                    onNavigateChat={() => navigate("/chat")}
                  />
                </div>
                {!isChat &&
                  (isFill ? (
                    <div className="page-fill">{content}</div>
                  ) : (
                    content
                  ))}
              </div>
              {role === "admin" && (
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
                  <span
                    className="status-item"
                    title={generationName || undefined}
                  >
                    <span
                      className={`status-dot ${generationName ? "" : "unknown"}`}
                    />
                    <span>
                      {t.generationCurrent}:{" "}
                      <span className="mono">{generationLabel}</span>
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
                </footer>
              )}
            </div>
          </div>
        )}
      </main>
    </ConfigProvider>
  );
}
