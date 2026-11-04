import { useCallback, useEffect, useState } from "react";
import { Alert, Space, Typography } from "antd";
import { getMessages, type Language } from "./i18n";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { Login } from "./Login";
import { StatusPage, type Health } from "./StatusPage";

export function App() {
  const [lang, setLang] = useState<Language>(() =>
    localStorage.getItem("language") === "en" ? "en" : "zh",
  );
  const [token, setToken] = useState(() => localStorage.getItem("token") || "");
  const [loggedIn, setLoggedIn] = useState(false);
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
        <StatusPage
          messages={t}
          health={health}
          onRefresh={() => void refresh()}
          onLogout={logout}
        />
      )}
    </main>
  );
}
