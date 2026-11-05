import { useCallback, useEffect, useState } from "react";
import { Alert, Space, Typography } from "antd";
import { getMessages, type Language } from "./i18n";
import { BuildsPage } from "./BuildsPage";
import { KnowledgePage } from "./KnowledgePage";
import { SearchPage } from "./SearchPage";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { PluginsPage } from "./PluginsPage";
import { Login } from "./Login";
import { StatusPage, type Health } from "./StatusPage";
import { TokensPage } from "./TokensPage";
import { McpAccessPage } from "./McpAccessPage";

export function App() {
  const [lang, setLang] = useState<Language>(() =>
    localStorage.getItem("language") === "en" ? "en" : "zh",
  );
  const [token, setToken] = useState(() => localStorage.getItem("token") || "");
  const [loggedIn, setLoggedIn] = useState(false);
  const [role, setRole] = useState("");
  const [path, setPath] = useState(window.location.pathname);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
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
    if (loggedIn) void refresh();
  }, [loggedIn, refresh]);
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
  return (
    <main style={{ maxWidth: 760, margin: "48px auto", padding: 24 }}>
      <Space style={{ display: "flex", justifyContent: "space-between" }}>
        <Typography.Title level={2}>{t.title}</Typography.Title>
        <LanguageSwitcher language={lang} onChange={changeLanguage} />
      </Space>
      {error && (
        <Alert type="error" title={error === "invalid" ? t.invalid : t.error} />
      )}
      {!loggedIn ? (
        <Login messages={t} onLogin={(value) => void authenticate(value)} />
      ) : (
        <>
          <nav aria-label={t.navigation}>
            {[
              { path: "/", label: t.statusPage },
              { path: "/knowledge", label: t.knowledge },
              { path: "/search", label: t.searchDebug },
              { path: "/mcp-access", label: t.mcpAccess },
              { path: "/builds", label: t.builds },
              ...(role === "admin"
                ? [
                    { path: "/tokens", label: t.tokens },
                    { path: "/plugins", label: t.plugins },
                  ]
                : []),
            ].map((item) => (
              <a
                key={item.path}
                href={item.path}
                style={{ marginRight: 16 }}
                onClick={(event) => {
                  event.preventDefault();
                  history.pushState(null, "", item.path);
                  setPath(item.path);
                }}
              >
                {item.label}
              </a>
            ))}
          </nav>
          {path === "/plugins" ? (
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
          ) : path === "/builds" ? (
            <BuildsPage token={token} messages={t} />
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
              onLogout={logout}
            />
          )}
        </>
      )}
    </main>
  );
}
