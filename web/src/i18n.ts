export type Language = "zh" | "en";

const messages = {
  zh: {
    title: "系统状态",
    login: "登录",
    token: "粘贴个人令牌",
    invalid: "令牌无效",
    logout: "退出",
    service: "服务",
    status: "状态",
    time: "耗时（毫秒）",
    up: "正常",
    down: "不可用",
    not_configured: "未配置",
    error: "无法获取状态",
    refresh: "刷新",
  },
  en: {
    title: "System status",
    login: "Sign in",
    token: "Paste your personal token",
    invalid: "Invalid token",
    logout: "Sign out",
    service: "Service",
    status: "Status",
    time: "Elapsed (ms)",
    up: "Up",
    down: "Down",
    not_configured: "Not configured",
    error: "Unable to load status",
    refresh: "Refresh",
  },
};

export function getMessages(language: Language) {
  return messages[language];
}
export type Messages = ReturnType<typeof getMessages>;
