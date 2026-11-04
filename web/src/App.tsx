import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Space, Table, Typography } from 'antd';
import type { paths } from './api';

type Health = paths['/healthz']['get']['responses'][200]['content']['application/json'];
type Language = 'zh' | 'en';
const words = {
  zh: { title: '系统状态', login: '登录', token: '粘贴个人令牌', invalid: '令牌无效', logout: '退出', service: '服务', status: '状态', time: '耗时（毫秒）', up: '正常', down: '不可用', not_configured: '未配置', error: '无法获取状态', refresh: '刷新' },
  en: { title: 'System status', login: 'Sign in', token: 'Paste your personal token', invalid: 'Invalid token', logout: 'Sign out', service: 'Service', status: 'Status', time: 'Elapsed (ms)', up: 'Up', down: 'Down', not_configured: 'Not configured', error: 'Unable to load status', refresh: 'Refresh' },
};
export function App() {
  const [lang, setLang] = useState<Language>(() => localStorage.getItem('language') === 'en' ? 'en' : 'zh');
  const [token, setToken] = useState(() => localStorage.getItem('token') || '');
  const [input, setInput] = useState('');
  const [loggedIn, setLoggedIn] = useState(false);
  const [error, setError] = useState('');
  const [health, setHealth] = useState<Health | null>(null);
  const t = words[lang];
  const authenticate = useCallback(async (value: string) => {
    try {
      const response = await fetch('/api/v1/me', { headers: { Authorization: `Bearer ${value}` } });
      if (!response.ok) { if (response.status === 401) { localStorage.removeItem('token'); setToken(''); setLoggedIn(false); } setError('invalid'); return; }
      localStorage.setItem('token', value); setToken(value); setLoggedIn(true); setError('');
    } catch { setError('error'); }
  }, []);
  useEffect(() => { if (token) void authenticate(token); }, [authenticate, token]);
  const refresh = useCallback(async () => {
    try { const response = await fetch('/healthz'); if (!response.ok) throw new Error(); setHealth(await response.json() as Health); setError(''); }
    catch { setError('error'); }
  }, []);
  useEffect(() => { if (loggedIn) void refresh(); }, [loggedIn, refresh]);
  const changeLanguage = () => { const next = lang === 'zh' ? 'en' : 'zh'; localStorage.setItem('language', next); setLang(next); };
  return <main style={{ maxWidth: 760, margin: '48px auto', padding: 24 }}><Space style={{ display: 'flex', justifyContent: 'space-between' }}><Typography.Title level={2}>{t.title}</Typography.Title><Button onClick={changeLanguage}>{lang === 'zh' ? 'English' : '中文'}</Button></Space>
    {error && <Alert type="error" title={error === 'invalid' ? t.invalid : t.error} />}
    {!loggedIn ? <Card><form onSubmit={e => { e.preventDefault(); void authenticate(input); }}><Space><Input.Password aria-label={t.token} placeholder={t.token} value={input} onChange={e => setInput(e.target.value)} /><Button htmlType="submit" type="primary">{t.login}</Button></Space></form></Card> : <Card><Space><Button onClick={() => void refresh()}>{t.refresh}</Button><Button onClick={() => { localStorage.removeItem('token'); setToken(''); setLoggedIn(false); }}>{t.logout}</Button></Space><Table pagination={false} rowKey="name" dataSource={Object.entries(health || {}).map(([name, value]) => ({ name, ...value }))} columns={[{ title: t.service, dataIndex: 'name' }, { title: t.status, render: (_, row) => t[row.status] }, { title: t.time, dataIndex: 'elapsed_ms' }]} /></Card>}
  </main>;
}
