import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Alert,
  Button,
  ConfigProvider,
  Space,
  Table,
  Tag,
  Typography,
  theme as antdTheme,
} from "antd";
import { CorrectionsPage, type DestinationFilter } from "./CorrectionsPage";
import { DiscoveryPage } from "./DiscoveryPage";
import { EvaluationsPage } from "./EvaluationsPage";
import { EvolutionPage } from "./EvolutionPage";
import { HistoricalTicketsPage } from "./HistoricalTicketsPage";
import { BuildPage } from "./BuildPage";
import { getMessages, type Language, type Messages } from "./i18n";
import { ChatPage } from "./ChatPage";
import { KnowledgePage } from "./KnowledgePage";
import { SearchPage } from "./SearchPage";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { PluginsPage } from "./PluginsPage";
import { SourcePage } from "./SourcePage";
import { Login } from "./Login";
import { StatusPage, type Health } from "./StatusPage";
import { TokensPage } from "./TokensPage";
import { ProjectsPage } from "./ProjectsPage";
import { PersonalSettingsPage } from "./PersonalSettingsPage";
import { McpAccessPage } from "./McpAccessPage";
import { AgentProfilesPage } from "./AgentSettings";
import { AgentRunnersPage } from "./AgentRunnersPage";
import { ProviderSettingsPage } from "./chat/ProviderSettings";
import { PlatformSettings } from "./PlatformSettings";
import { SkillsPage } from "./SkillsPage";
import {
  ProjectSelector,
  loadProjects,
  storedProject,
} from "./ProjectSelector";
import type { components } from "./api";
import {
  CommentOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SettingOutlined,
  SyncOutlined,
  UserOutlined,
} from "@ant-design/icons";

type PrimaryGroupId = "chat" | "knowledge" | "data" | "projects" | "settings";
type ThemeMode = "system" | "light" | "dark";

interface SubNavItem {
  path: string;
  label: string;
  matchPaths?: string[];
}

interface SubNavGroup {
  title?: string;
  items: SubNavItem[];
}

function formatGenerationTime(value: string | null | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function conversationIdFromPath(pathname: string) {
  if (!pathname.startsWith("/chat/")) return "";
  const encoded = pathname.slice("/chat/".length).replace(/\/+$/, "");
  if (!encoded) return "";
  try {
    return decodeURIComponent(encoded);
  } catch {
    return encoded;
  }
}

function matchMediaQuery(query: string): MediaQueryList | null {
  try {
    return window.matchMedia(query);
  } catch {
    return null;
  }
}

function useMediaMatches(queryText: string) {
  const [matches, setMatches] = useState(
    () => matchMediaQuery(queryText)?.matches ?? false,
  );
  useEffect(() => {
    const query = matchMediaQuery(queryText);
    if (!query) return;
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(query.matches);
    if (query.addEventListener) query.addEventListener("change", onChange);
    else query.addListener(onChange);
    return () => {
      if (query.removeEventListener)
        query.removeEventListener("change", onChange);
      else query.removeListener(onChange);
    };
  }, [queryText]);
  return matches;
}

export function App() {
  const [lang, setLang] = useState<Language>(() =>
    localStorage.getItem("language") === "en" ? "en" : "zh",
  );
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    try {
      const saved = localStorage.getItem("theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch {
      /* Storage may be unavailable. */
    }
    return "system";
  });
  const systemDark = useMediaMatches("(prefers-color-scheme: dark)");
  const isDark = themeMode === "dark" || (themeMode === "system" && systemDark);
  useEffect(() => {
    if (themeMode === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", themeMode);
    }
  }, [themeMode]);

  const [token, setToken] = useState(() => localStorage.getItem("token") || "");
  const [loggedIn, setLoggedIn] = useState(false);
  const [role, setRole] = useState("");
  const [userName, setUserName] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem("navigationExpanded") !== "false";
    } catch {
      return true;
    }
  });
  const [mediumExpanded, setMediumExpanded] = useState(false);
  const narrow = useMediaMatches("(max-width: 768px)");
  const medium = useMediaMatches("(min-width: 769px) and (max-width: 1024px)");
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    if (!narrow) setDrawerOpen(false);
  }, [narrow]);

  const middleExpanded = narrow
    ? drawerOpen
    : medium
      ? mediumExpanded
      : expanded;
  const navigationExpanded = narrow ? drawerOpen : middleExpanded;
  const toggleNavigation = () => {
    if (narrow) {
      setDrawerOpen((value) => !value);
      return;
    }
    if (medium) {
      setMediumExpanded((value) => !value);
      return;
    }
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
  const [personalOpen, setPersonalOpen] = useState(false);
  const [path, setPath] = useState(window.location.pathname);
  const [globalProject, setGlobalProject] = useState(storedProject);
  const [correctionProposalId, setCorrectionProposalId] = useState<
    string | null
  >(null);
  const [correctionDestination, setCorrectionDestination] =
    useState<DestinationFilter | null>(null);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [generation, setGeneration] = useState<{
    name: string;
    createdAt: string | null;
  } | null>(null);
  const [dtbConfigured, setDtbConfigured] = useState<boolean | null>(null);
  const [projectAdmin, setProjectAdmin] = useState(false);
  const [projectMember, setProjectMember] = useState(false);
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
    if (!loggedIn || !token) return;
    let active = true;
    void loadProjects(token).then((projects) => {
      if (!active) return;
      setProjectAdmin(projects.some((project) => project.my_role === "admin"));
      setProjectMember(
        projects.some((project) => project.my_role === "member"),
      );
    });
    return () => {
      active = false;
    };
  }, [loggedIn, refreshKey, token]);

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

  const toggleTheme = () => {
    const next: ThemeMode = isDark ? "light" : "dark";
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* Storage may be unavailable. */
    }
    setThemeMode(next);
  };

  const logout = () => {
    localStorage.removeItem("token");
    setToken("");
    setLoggedIn(false);
  };

  const navigate = (next: string) => {
    const [nextPath, nextQuery] = next.split("?");
    if (nextPath === "/corrections" && nextQuery) {
      const params = new URLSearchParams(nextQuery);
      setCorrectionProposalId(params.get("proposal_id"));
      const destParam = params.get("destination");
      setCorrectionDestination(
        destParam === "historical_ticket" ||
          destParam === "knowledge_doc" ||
          destParam === "semantic_override" ||
          destParam === "ossie_model"
          ? destParam
          : null,
      );
    } else {
      setCorrectionProposalId(null);
      setCorrectionDestination(null);
    }
    history.pushState(null, "", next);
    setPath(nextPath || "/");
    setDrawerOpen(false);
    setPersonalOpen(false);
  };

  const canManageProject = role === "admin" || projectAdmin;
  const canViewProjects = role === "admin" || projectAdmin || projectMember;

  const isChat = path === "/" || path === "/chat" || path.startsWith("/chat/");
  const activeGroup: PrimaryGroupId = (() => {
    if (isChat) return "chat";
    if (
      path === "/knowledge" ||
      path === "/search" ||
      ((path === "/knowledge-gaps" || path === "/historical-tickets") &&
        !canManageProject)
    ) {
      return "knowledge";
    }
    if (
      [
        "/builds",
        "/source",
        "/corrections",
        "/historical-tickets",
        "/evaluations",
        "/knowledge-gaps",
        "/evolution",
        "/discovery",
      ].includes(path)
    ) {
      return "data";
    }
    if (path === "/projects") return "projects";
    return "settings";
  })();

  const topRailItems: {
    id: PrimaryGroupId;
    path: string;
    label: string;
    icon: ReactNode;
  }[] = [
    {
      id: "chat",
      path: "/chat",
      label: t.sidebarChat,
      icon: <CommentOutlined />,
    },
    {
      id: "knowledge",
      path: "/knowledge",
      label: t.sidebarKnowledge,
      icon: <DatabaseOutlined />,
    },
    ...(canManageProject
      ? [
          {
            id: "data" as const,
            path: "/builds",
            label: t.sidebarDataOps,
            icon: <SyncOutlined />,
          },
        ]
      : []),
    ...(canViewProjects
      ? [
          {
            id: "projects" as const,
            path: "/projects",
            label: t.sidebarProjectsNav,
            icon: <FolderOpenOutlined />,
          },
        ]
      : []),
  ];

  const settingsItem = {
    id: "settings" as const,
    path: role === "admin" ? "/agents" : "/personal-settings",
    label: t.sidebarSettings,
    icon: <SettingOutlined />,
  };

  const middleGroups: SubNavGroup[] = (() => {
    if (activeGroup === "knowledge") {
      return [
        {
          title: t.sidebarKnowledge,
          items: [
            { path: "/knowledge", label: t.knowledge },
            { path: "/search", label: t.searchDebug },
            ...(!canManageProject && projectMember
              ? [
                  {
                    path: "/historical-tickets",
                    label: t.historicalTicketsTab,
                  },
                  { path: "/knowledge-gaps", label: t.gapTitle },
                ]
              : []),
          ],
        },
      ];
    }
    if (activeGroup === "data") {
      if (!canManageProject) {
        return [
          {
            title: t.sidebarGovernance,
            items: [{ path: "/knowledge-gaps", label: t.gapTitle }],
          },
        ];
      }
      return [
        {
          title: t.sidebarDataOps,
          items: [
            { path: "/builds", label: t.builds },
            { path: "/source", label: t.source },
          ],
        },
        {
          title: t.sidebarGovernance,
          items: [
            { path: "/knowledge-gaps", label: t.gapTitle },
            { path: "/corrections", label: t.correctionTitle },
            { path: "/historical-tickets", label: t.historicalTicketsTab },
            { path: "/evaluations", label: t.evalQuestionsTab },
            { path: "/evolution", label: t.evolutionTitle },
            { path: "/discovery", label: t.discoveryTitle },
          ],
        },
      ];
    }
    if (activeGroup === "projects") {
      return [
        {
          title: t.projectManagement,
          items: [{ path: "/projects", label: t.sidebarProjects }],
        },
      ];
    }
    if (activeGroup === "settings") {
      return [
        ...(role === "admin"
          ? [
              {
                title: t.settingsGroupAgents,
                items: [
                  {
                    path: "/agents",
                    label: t.agentSettings,
                    matchPaths: ["/agents", "/agent-settings", "/config"],
                  },
                  { path: "/providers", label: t.providersSettingsNav },
                  { path: "/agent-runners", label: t.agentRunnersNav },
                ],
              },
              {
                title: t.settingsGroupPlatform,
                items: [
                  { path: "/platform", label: t.platformSettings },
                  { path: "/plugins", label: t.plugins },
                  { path: "/skills", label: t.skills },
                  { path: "/tokens", label: t.tokens },
                  { path: "/status", label: t.statusPage },
                ],
              },
            ]
          : []),
        {
          title: t.settingsGroupPersonal,
          items: [
            { path: "/personal-settings", label: t.personalSettings },
            { path: "/mcp-access", label: t.mcpAccess },
          ],
        },
      ];
    }
    return [];
  })();

  const matchedMiddleGroup = middleGroups.find((group) =>
    group.items.some(
      (item) => item.path === path || Boolean(item.matchPaths?.includes(path)),
    ),
  );
  const activeGroupLabel =
    (activeGroup === "data" ? matchedMiddleGroup?.title : undefined) ??
    topRailItems.find((item) => item.id === activeGroup)?.label ??
    settingsItem.label;
  const activePageLabel = (() => {
    if (isChat) return t.chat;
    for (const group of middleGroups) {
      for (const item of group.items) {
        if (item.path === path || item.matchPaths?.includes(path)) {
          return item.label;
        }
      }
    }
    if (path === "/status") return t.statusPage;
    return t.statusPage;
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
  const conversationId = conversationIdFromPath(path);
  const isFill = path === "/knowledge";

  const content =
    path === "/knowledge-gaps" ? (
      <CorrectionsPage token={token} gaps messages={t} />
    ) : path === "/historical-tickets" ? (
      canViewProjects ? (
        <HistoricalTicketsPage
          token={token}
          messages={t}
          onNavigateCorrections={
            canManageProject
              ? () => navigate("/corrections?destination=historical_ticket")
              : undefined
          }
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/corrections" ? (
      canManageProject ? (
        <CorrectionsPage
          token={token}
          messages={t}
          initialProposalId={correctionProposalId}
          initialDestinationFilter={correctionDestination}
          onNavigateGaps={() => navigate("/knowledge-gaps")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/evaluations" ? (
      canManageProject ? (
        <EvaluationsPage
          token={token}
          messages={t}
          onNavigateDiscovery={() => navigate("/discovery")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/evolution" ? (
      canManageProject ? (
        <EvolutionPage
          token={token}
          messages={t}
          onNavigateCorrections={(proposalId) =>
            navigate(
              proposalId
                ? `/corrections?proposal_id=${encodeURIComponent(proposalId)}`
                : "/corrections",
            )
          }
          onNavigatePersonalSettings={() => navigate("/personal-settings")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/discovery" ? (
      canManageProject ? (
        <DiscoveryPage
          token={token}
          messages={t}
          onNavigateCorrections={(proposalId) =>
            navigate(
              proposalId
                ? `/corrections?proposal_id=${encodeURIComponent(proposalId)}`
                : "/corrections",
            )
          }
          onNavigateEvaluations={() => navigate("/historical-tickets")}
          onNavigatePersonalSettings={() => navigate("/personal-settings")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : isChat ? null : path === "/builds" ? (
      canManageProject ? (
        <BuildPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/source" ? (
      canManageProject ? (
        <SourcePage
          token={token}
          messages={t}
          onNavigateBuilds={() => navigate("/builds")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/config" ||
      path === "/agent-settings" ||
      path === "/agents" ? (
      role === "admin" ? (
        <AgentProfilesPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/providers" ? (
      role === "admin" ? (
        <ProviderSettingsPage token={token} role={role} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/agent-runners" ? (
      role === "admin" ? (
        <AgentRunnersPage
          token={token}
          messages={t}
          onNavigateAgentConfig={() => navigate("/agents")}
        />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/platform" ? (
      role === "admin" ? (
        <PlatformSettings token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/plugins" ? (
      role === "admin" ? (
        <PluginsPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/skills" ? (
      role === "admin" ? (
        <SkillsPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/tokens" ? (
      role === "admin" ? (
        <TokensPage token={token} messages={t} />
      ) : (
        <Alert title={t.forbidden} type="warning" />
      )
    ) : path === "/projects" ? (
      canViewProjects ? (
        <ProjectsPage
          token={token}
          messages={t}
          canCreate={role === "admin"}
          includeMemberProjects={!projectAdmin}
        />
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

  const antThemeConfig = isDark
    ? {
        algorithm: antdTheme.darkAlgorithm,
        token: {
          colorPrimary: "#A9BCFF",
          colorInfo: "#A9BCFF",
          colorLink: "#A9BCFF",
          colorText: "#EDF1F8",
          colorTextSecondary: "#A9B5C6",
          colorBorder: "#394557",
          colorBgLayout: "#151B24",
          colorBgContainer: "#202733",
          borderRadius: 10,
          controlHeight: 38,
          fontFamily: "var(--font-sans)",
          fontSize: 14,
        },
        components: {
          Table: { cellPaddingBlock: 8, headerBg: "#262F3C" },
          Button: { primaryShadow: "none" },
          Tabs: { itemSelectedColor: "#A9BCFF", inkBarColor: "#A9BCFF" },
        },
      }
    : {
        algorithm: antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: "#315AC7",
          colorInfo: "#315AC7",
          colorLink: "#315AC7",
          colorText: "#202733",
          colorTextSecondary: "#627083",
          colorBorder: "#DCE2EA",
          colorBgLayout: "#EEF1F5",
          colorBgContainer: "#FFFFFF",
          borderRadius: 10,
          controlHeight: 38,
          fontFamily: "var(--font-sans)",
          fontSize: 14,
        },
        components: {
          Table: { cellPaddingBlock: 8, headerBg: "#F7F8FA" },
          Button: { primaryShadow: "none" },
          Tabs: { itemSelectedColor: "#315AC7", inkBarColor: "#315AC7" },
        },
      };

  return (
    <ConfigProvider theme={antThemeConfig}>
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
            {narrow && !drawerOpen && (
              <div className="mobile-topbar">
                <Button
                  type="text"
                  className="mobile-nav-toggle"
                  aria-label={t.openNavigation}
                  title={t.openNavigation}
                  icon={<MenuUnfoldOutlined />}
                  onClick={() => setDrawerOpen(true)}
                />
                <span className="mobile-brand">Semantica</span>
                <span className="mobile-crumb">{activePageLabel}</span>
              </div>
            )}
            {narrow && drawerOpen && (
              <button
                type="button"
                className="sidebar-scrim"
                aria-label={t.closeNavigation}
                onClick={() => setDrawerOpen(false)}
              />
            )}
            <div
              className={`side-nav ${navigationExpanded ? "expanded" : ""}${
                narrow
                  ? ` side-nav--drawer${drawerOpen ? " side-nav--drawer-open" : ""}`
                  : ""
              }`}
              aria-hidden={narrow && !drawerOpen ? true : undefined}
            >
              <aside className="primary-rail" aria-label={t.primaryNavigation}>
                <div className="rail-logo" title="Semantica">
                  S
                </div>
                <div className="rail-items">
                  {topRailItems.map((item) => (
                    <a
                      key={item.id}
                      className="nav-item rail-item"
                      href={item.path}
                      aria-current={
                        activeGroup === item.id ? "page" : undefined
                      }
                      title={item.label}
                      onClick={(event) => {
                        event.preventDefault();
                        navigate(item.path);
                      }}
                    >
                      <span className="rail-icon" aria-hidden="true">
                        {item.icon}
                      </span>
                      <span className="rail-label">{item.label}</span>
                    </a>
                  ))}
                </div>
                <div className="rail-bottom">
                  <a
                    className="nav-item rail-item"
                    href={settingsItem.path}
                    aria-current={
                      activeGroup === "settings" ? "page" : undefined
                    }
                    title={settingsItem.label}
                    onClick={(event) => {
                      event.preventDefault();
                      navigate(settingsItem.path);
                    }}
                  >
                    <span className="rail-icon" aria-hidden="true">
                      {settingsItem.icon}
                    </span>
                    <span className="rail-label">{settingsItem.label}</span>
                  </a>
                  <div className="personal-menu">
                    {personalOpen && (
                      <div className="account-popover">
                        <div className="account-popover-head">
                          <span className="account-name">
                            {userName}
                            <small>{role}</small>
                          </span>
                        </div>
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
                          onClick={toggleTheme}
                        >
                          {isDark ? t.themeSwitchLight : t.themeSwitchDark}
                        </Button>
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
                          {t.logout}
                        </Button>
                      </div>
                    )}
                    <Button
                      type="text"
                      className="nav-item account-toggle"
                      icon={<UserOutlined />}
                      title={t.accountMenu}
                      aria-label={t.accountMenu}
                      aria-expanded={personalOpen}
                      onClick={() => setPersonalOpen((value) => !value)}
                    />
                  </div>
                </div>
              </aside>

              <aside
                className={`context-pane${
                  !narrow && !middleExpanded ? " context-pane--collapsed" : ""
                }`}
                aria-label={t.contextNavigation}
              >
                <div className="context-pane-head">
                  <div className="context-pane-heading">
                    <h2 className="context-pane-title">{activeGroupLabel}</h2>
                    <span className="context-pane-subtitle">
                      {t.workspaceSubtitle}
                    </span>
                  </div>
                  {narrow && (
                    <Button
                      type="text"
                      size="small"
                      className="nav-toggle"
                      aria-label={t.collapseNavigation}
                      title={t.collapseNavigation}
                      onClick={() => setDrawerOpen(false)}
                    >
                      <MenuFoldOutlined />
                    </Button>
                  )}
                </div>
                <div className="context-pane-body">
                  <div
                    ref={setNewTarget}
                    className="context-new-chat"
                    hidden={!isChat}
                  />
                  <div
                    ref={setSessionTarget}
                    className="sidebar-sessions"
                    hidden={!isChat}
                  />
                  {!isChat && (
                    <nav className="context-subnav" aria-label={t.navigation}>
                      {middleGroups.map((group, groupIndex) => (
                        <div
                          key={group.title ?? groupIndex}
                          className="context-nav-group"
                        >
                          {group.title && (
                            <div className="context-group-label">
                              {group.title}
                            </div>
                          )}
                          {group.items.map((item) => {
                            const active =
                              path === item.path ||
                              Boolean(item.matchPaths?.includes(path));
                            return (
                              <a
                                key={item.path}
                                href={item.path}
                                className="context-nav-link"
                                aria-current={active ? "page" : undefined}
                                onClick={(event) => {
                                  event.preventDefault();
                                  navigate(item.path);
                                }}
                              >
                                {item.label}
                              </a>
                            );
                          })}
                        </div>
                      ))}
                    </nav>
                  )}
                </div>
              </aside>
            </div>

            <div className="workspace-content">
              <header className="workbench-topbar">
                <div className="workbench-topbar-left">
                  {!narrow && (
                    <Button
                      type="text"
                      size="small"
                      className="nav-toggle"
                      aria-label={
                        middleExpanded
                          ? t.collapseNavigation
                          : t.expandNavigation
                      }
                      title={
                        middleExpanded
                          ? t.collapseNavigation
                          : t.expandNavigation
                      }
                      onClick={toggleNavigation}
                    >
                      {middleExpanded ? (
                        <MenuFoldOutlined />
                      ) : (
                        <MenuUnfoldOutlined />
                      )}
                    </Button>
                  )}
                  <nav className="workbench-crumb" aria-label={t.breadcrumb}>
                    <span>{activeGroupLabel}</span>
                    <span className="workbench-crumb-sep" aria-hidden="true">
                      {" / "}
                    </span>
                    <strong>{activePageLabel}</strong>
                  </nav>
                </div>
                <div className="workbench-topbar-right">
                  {canViewProjects && (
                    <ProjectSelector
                      token={token}
                      value={globalProject}
                      onChange={setGlobalProject}
                      messages={t}
                      ariaLabel={t.currentProject}
                      includeMemberProjects={role !== "admin" && !projectAdmin}
                      includeArchived={
                        path === "/evolution" || path === "/discovery"
                      }
                      compact
                    />
                  )}
                </div>
              </header>

              <div
                className={`page-content${isChat ? " page-content--chat" : ""}${
                  isFill ? " page-content--fill" : ""
                }`}
              >
                <div className="chat-page-host" hidden={!isChat}>
                  <ChatPage
                    token={token}
                    role={role}
                    messages={t}
                    language={lang}
                    onChangeLanguage={changeLanguage}
                    refreshKey={refreshKey}
                    active={isChat}
                    sessionTarget={sessionTarget}
                    newTarget={newTarget}
                    initialConversationId={conversationId}
                    onNavigateChat={() => navigate("/chat")}
                    onNavigateKnowledge={() => navigate("/knowledge")}
                    onNavigatePersonalSettings={() =>
                      navigate("/personal-settings")
                    }
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
